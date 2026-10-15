"""Services métier transactionnels de l'application.

Les vues ne doivent pas reproduire les règles de ce module. En particulier, une
valeur issue d'un formulaire n'est jamais considérée comme fiable pour les
clôtures, les capacités ou l'appartenance à un événement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models import Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from .eligibility import validate_adult_coverage_for_confirmation
from .models import (
    Assignment,
    AssignmentCommunication,
    Beverage,
    EmailAttempt,
    EmailDelivery,
    Event,
    EventDay,
    Family,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
)


class BusinessRuleError(ValidationError):
    """Erreur métier sûre à présenter à un utilisateur."""

    def __init__(self, message, *, code: str = "business_rule"):
        super().__init__(message, code=code)


def _business_error(exc: ValidationError, *, code: str = "invalid") -> BusinessRuleError:
    if hasattr(exc, "message_dict"):
        messages = [message for values in exc.message_dict.values() for message in values]
    else:
        messages = exc.messages
    return BusinessRuleError(messages, code=code)


def _is_organizer(actor=None, *, explicit: bool = False) -> bool:
    return explicit or bool(
        actor
        and getattr(actor, "is_authenticated", False)
        and (getattr(actor, "is_organizer", False) or getattr(actor, "is_superuser", False))
    )


def _check_member_access(actor, member: Member, *, organizer: bool = False) -> None:
    """Contrôle optionnel pour les appels provenant directement d'une vue.

    ``actor=None`` est réservé aux traitements internes (commandes de gestion,
    imports et tests). Dès qu'un acteur est fourni, l'isolation des familles est
    appliquée ici en plus du filtrage fait dans la vue.
    """

    if actor is None or _is_organizer(actor, explicit=organizer):
        return
    if not getattr(actor, "is_authenticated", False) or member.family.account_id != actor.pk:
        raise PermissionDenied("Vous ne pouvez pas modifier les données de cette famille.")


def _check_organizer_access(actor) -> None:
    if actor is not None and not _is_organizer(actor):
        raise PermissionDenied("Cette opération est réservée aux organisateurs.")


def _check_registration_window(event: Event, *, organizer: bool) -> None:
    if not organizer and not event.registration_is_open():
        raise BusinessRuleError(
            "Les inscriptions ne sont pas ouvertes pour cet événement.",
            code="registration_closed",
        )


def _check_meal_window(event: Event, *, organizer: bool) -> None:
    if not organizer and not event.meal_orders_are_open():
        raise BusinessRuleError(
            "Les réservations de repas sont clôturées pour cet événement.",
            code="meal_orders_closed",
        )


def _validate_preferred_station(event_day: EventDay, station: Station | None) -> None:
    if station is None:
        return
    if station.event_id != event_day.event_id:
        raise BusinessRuleError(
            "Le poste souhaité n'appartient pas à cet événement.", code="invalid_station"
        )
    if not station.is_active:
        raise BusinessRuleError(
            "Ce poste n'est plus proposé.", code="inactive_station"
        )


def _lock_assignment_registrations(registration_ids):
    """Un ordre unique, aussi compatible avec l'inscription personnelle."""
    coordinates = list(Registration.objects.filter(pk__in=registration_ids).values_list(
        "member_id", "event_day_id"
    ))
    list(Member.objects.select_for_update(of=("self",)).filter(
        pk__in={item[0] for item in coordinates}
    ).order_by("pk"))
    list(EventDay.objects.select_for_update(of=("self",)).filter(
        pk__in={item[1] for item in coordinates}
    ).order_by("pk"))
    return {item.pk: item for item in Registration.objects.select_for_update(of=("self",))
            .filter(pk__in=registration_ids)
            .select_related("member__family", "event_day__event").order_by("pk")}


def _cancel_incompatible_assignments(
    registration: Registration,
    *,
    actor=None,
    reason: str,
) -> list[dict[str, Any]]:
    """Retire les affectations devenues incohérentes sans effacer leur audit."""

    active_statuses = (Assignment.Status.DRAFT, Assignment.Status.CONFIRMED)
    slot_ids = list(registration.assignments.filter(status__in=active_statuses).values_list(
        "station_slot_id", flat=True
    ))
    list(StationSlot.objects.select_for_update(of=("self",)).filter(
        pk__in=slot_ids
    ).order_by("pk"))
    assignments = list(
        Assignment.objects.select_for_update(of=("self",))
        .filter(registration=registration, status__in=active_statuses)
        .select_related("station_slot__station")
        .order_by("created_at")
    )
    invalidated: list[dict[str, Any]] = []
    coverage_invalidated = set()
    now = timezone.now()
    for assignment in assignments:
        if (
            registration.status == Registration.Status.ACTIVE
            and registration.covers_period(assignment.period)
        ):
            continue
        was_communicated = assignment.communications.exists()
        assignment.status = Assignment.Status.CANCELLED
        assignment.cancelled_at = now
        assignment.cancelled_by = actor if getattr(actor, "is_authenticated", False) else None
        assignment.cancellation_reason = reason
        assignment.save(
            update_fields=(
                "status",
                "cancelled_at",
                "cancelled_by",
                "cancellation_reason",
                "period",
                "updated_at",
            )
        )
        coverage_invalidated.update(getattr(assignment, "coverage_invalidated_assignment_ids", []))
        invalidated.append(
            {
                "assignment_id": assignment.pk,
                "station": assignment.station_slot.station.name,
                "period": assignment.period,
                "was_confirmed": assignment.confirmed_at is not None,
                "was_communicated": was_communicated,
                "needs_communication": was_communicated,
            }
        )
    registration.coverage_invalidated_assignment_ids = list(coverage_invalidated)
    return invalidated


@transaction.atomic
def save_registration(
    *,
    member: Member,
    event_day: EventDay,
    availability: str,
    preferred_station: Station | None = None,
    note: str = "",
    actor=None,
    actor_is_organizer: bool = False,
) -> Registration:
    """Crée ou modifie l'unique inscription d'un membre pour une journée.

    Une réduction de disponibilité retire atomiquement toute affectation devenue
    impossible. Les affectations restent en base avec le statut ``cancelled`` :
    les versions déjà communiquées sont donc conservées et une mise à jour peut
    être envoyée.
    """

    organizer = _is_organizer(actor, explicit=actor_is_organizer)
    locked_member = Member.objects.select_for_update(of=("self",)).select_related("family").get(pk=member.pk)
    locked_day = EventDay.objects.select_for_update(of=("self",)).select_related("event").get(pk=event_day.pk)
    _check_member_access(actor, locked_member, organizer=organizer)
    _check_registration_window(locked_day.event, organizer=organizer)
    if locked_member.is_archived:
        raise BusinessRuleError(
            "Ce membre est archivé et ne peut plus être inscrit.", code="archived_member"
        )
    if preferred_station is not None:
        try:
            preferred_station = Station.objects.get(pk=preferred_station.pk)
        except Station.DoesNotExist as error:
            raise BusinessRuleError("Ce poste n'est plus proposé.", code="invalid_station") from error
    _validate_preferred_station(locked_day, preferred_station)

    registration = (
        Registration.objects.select_for_update(of=("self",))
        .filter(member=locked_member, event_day=locked_day)
        .first()
    )
    if registration is None:
        registration = Registration(member=locked_member, event_day=locked_day)

    registration.availability = availability
    registration.preferred_station = preferred_station
    registration.note = (note or "").strip()
    registration.status = Registration.Status.ACTIVE
    try:
        registration.full_clean()
        registration.save()
    except ValidationError as exc:
        raise _business_error(exc, code="invalid_registration") from exc
    except IntegrityError as exc:
        raise BusinessRuleError(
            "Cette personne est déjà inscrite pour cette journée.",
            code="duplicate_registration",
        ) from exc

    invalidated = _cancel_incompatible_assignments(
        registration,
        actor=actor,
        reason="Disponibilité modifiée : une nouvelle affectation est nécessaire.",
    )
    # Attributs non persistés utiles aux vues pour afficher un avertissement.
    registration.invalidated_assignments = invalidated
    registration.reassignment_required = bool(invalidated)
    return registration


@transaction.atomic
def cancel_registration(
    *,
    registration: Registration,
    actor=None,
    actor_is_organizer: bool = False,
) -> Registration:
    """Annule une inscription et toutes ses affectations actives."""

    organizer = _is_organizer(actor, explicit=actor_is_organizer)
    locked = _lock_assignment_registrations({registration.pk})[registration.pk]
    _check_member_access(actor, locked.member, organizer=organizer)
    _check_registration_window(locked.event_day.event, organizer=organizer)
    if locked.status == Registration.Status.CANCELLED:
        locked.invalidated_assignments = []
        locked.reassignment_required = False
        return locked

    locked.status = Registration.Status.CANCELLED
    locked.save(update_fields=("status", "updated_at"))
    invalidated = _cancel_incompatible_assignments(
        locked,
        actor=actor,
        reason="Inscription annulée par la famille ou un organisateur.",
    )
    locked.invalidated_assignments = invalidated
    locked.reassignment_required = bool(invalidated)
    return locked


def _validate_slot(slot: StationSlot, registration: Registration) -> None:
    if not slot.station.is_active:
        raise BusinessRuleError("Ce poste est inactif.", code="inactive_station")
    if slot.event_day_id != registration.event_day_id:
        raise BusinessRuleError(
            "Le créneau n'appartient pas à la journée d'inscription.", code="wrong_event_day"
        )


@transaction.atomic
def create_assignment(
    *,
    registration: Registration,
    station_slot: StationSlot,
    actor=None,
) -> Assignment:
    """Crée une affectation brouillon après verrouillage du créneau.

    Le verrou de créneau sérialise les contrôles de capacité. Le verrou
    d'inscription sérialise les contrôles de doublon et de chevauchement.
    """

    _check_organizer_access(actor)
    locked_registration = _lock_assignment_registrations({registration.pk})[registration.pk]
    locked_slot = (
        StationSlot.objects.select_for_update(of=("self",))
        .select_related("station", "event_day")
        .get(pk=station_slot.pk)
    )
    _validate_slot(locked_slot, locked_registration)
    assignment = Assignment(
        registration=locked_registration,
        station_slot=locked_slot,
        status=Assignment.Status.DRAFT,
    )
    try:
        assignment.save()
    except ValidationError as exc:
        raise _business_error(exc, code="invalid_assignment") from exc
    except IntegrityError as exc:
        raise BusinessRuleError(
            "Cette personne possède déjà une affectation sur cette période.",
            code="duplicate_assignment",
        ) from exc
    return assignment


@transaction.atomic
def update_assignment(
    *,
    assignment: Assignment,
    station_slot: StationSlot,
    actor=None,
) -> Assignment:
    """Déplace une affectation active tout en revalidant toutes les règles."""

    _check_organizer_access(actor)
    registration_id = Assignment.objects.values_list("registration_id", flat=True).get(
        pk=assignment.pk
    )
    _lock_assignment_registrations({registration_id})
    original_slot_id = Assignment.objects.values_list("station_slot_id", flat=True).get(pk=assignment.pk)
    slots = {slot.pk: slot for slot in StationSlot.objects.select_for_update(of=("self",))
             .filter(pk__in={station_slot.pk, original_slot_id})
             .select_related("station", "event_day").order_by("pk")}
    destination = slots[station_slot.pk]
    locked = (
        Assignment.objects.select_for_update(of=("self",))
        .select_related("registration__event_day", "station_slot")
        .get(pk=assignment.pk)
    )
    if locked.status == Assignment.Status.CANCELLED:
        raise BusinessRuleError(
            "Une affectation retirée ne peut pas être modifiée.", code="cancelled_assignment"
        )
    _validate_slot(destination, locked.registration)
    locked.station_slot = destination
    try:
        locked.save()
    except ValidationError as exc:
        raise _business_error(exc, code="invalid_assignment") from exc
    except IntegrityError as exc:
        raise BusinessRuleError(
            "Cette modification créerait une double affectation.",
            code="duplicate_assignment",
        ) from exc
    return locked


@transaction.atomic
def remove_assignment(
    *,
    assignment: Assignment,
    actor=None,
    reason: str = "Retirée par un organisateur.",
) -> Assignment:
    """Retire une affectation sans supprimer son historique de communication."""

    _check_organizer_access(actor)
    registration_id = Assignment.objects.values_list("registration_id", flat=True).get(
        pk=assignment.pk
    )
    _lock_assignment_registrations({registration_id})
    slot_id = Assignment.objects.values_list("station_slot_id", flat=True).get(pk=assignment.pk)
    StationSlot.objects.select_for_update(of=("self",)).get(pk=slot_id)
    locked = Assignment.objects.select_for_update(of=("self",)).get(pk=assignment.pk)
    if locked.status == Assignment.Status.CANCELLED:
        return locked
    locked.status = Assignment.Status.CANCELLED
    locked.cancelled_at = timezone.now()
    locked.cancelled_by = actor if getattr(actor, "is_authenticated", False) else None
    locked.cancellation_reason = (reason or "Retirée par un organisateur.").strip()
    locked.save(
        update_fields=(
            "status",
            "cancelled_at",
            "cancelled_by",
            "cancellation_reason",
            "period",
            "updated_at",
        )
    )
    return locked


# Nom explicite conservé pour les vues/API : il s'agit volontairement d'une
# annulation historisée, jamais d'un DELETE SQL.
delete_assignment = remove_assignment


def _assignment_ids(assignments: Iterable[Assignment | Any]) -> list[Any]:
    ids = []
    for assignment in assignments:
        ids.append(getattr(assignment, "pk", assignment))
    # Préserve l'ordre tout en retirant les doublons.
    return list(dict.fromkeys(ids))


@transaction.atomic
def confirm_assignments(
    *,
    assignments: Iterable[Assignment | Any],
    confirmed_by,
) -> list[Assignment]:
    """Confirme atomiquement une sélection d'affectations."""

    _check_organizer_access(confirmed_by)
    ids = _assignment_ids(assignments)
    if not ids:
        return []
    preflight = list(
        Assignment.objects.filter(pk__in=ids).values_list("pk", "registration_id")
    )
    if len(preflight) != len(ids):
        raise BusinessRuleError(
            "Au moins une affectation est introuvable.", code="assignment_not_found"
        )
    registration_ids = {registration_id for _, registration_id in preflight}
    # Ordre identique aux créations et modifications concurrentes.
    _lock_assignment_registrations(registration_ids)
    slot_ids = set(
        Assignment.objects.filter(pk__in=ids).values_list("station_slot_id", flat=True)
    )
    list(StationSlot.objects.select_for_update(of=("self",)).filter(pk__in=slot_ids).order_by("pk"))
    locked = list(
        Assignment.objects.select_for_update(of=("self",))
        .filter(pk__in=ids)
        .select_related("registration__member", "registration__event_day", "station_slot__station")
        .order_by("created_at")
    )
    if len(locked) != len(ids):
        raise BusinessRuleError(
            "Au moins une affectation est introuvable.", code="assignment_not_found"
        )

    now = timezone.now()
    for item in locked:
        if item.status == Assignment.Status.CANCELLED:
            raise BusinessRuleError(
                "Une affectation retirée ne peut pas être confirmée.",
                code="cancelled_assignment",
            )
        try:
            item.full_clean()
        except ValidationError as exc:
            raise _business_error(exc, code="invalid_assignment") from exc
    try:
        validate_adult_coverage_for_confirmation(locked)
    except ValidationError as exc:
        raise _business_error(exc, code="adult_coverage_required") from exc
    for item in locked:
        item.status = Assignment.Status.CONFIRMED
        item.confirmed_at = now
        item.confirmed_by = confirmed_by
        item.updated_at = now
    # Un seul état final : l'adulte et les mineurs d'un même lot deviennent
    # confirmés ensemble, quel que soit l'ordre de leur sélection.
    Assignment.objects.bulk_update(
        locked, ("status", "confirmed_at", "confirmed_by", "period", "updated_at")
    )
    return locked


def confirm_assignment(*, assignment: Assignment, confirmed_by) -> Assignment:
    return confirm_assignments(assignments=(assignment,), confirmed_by=confirmed_by)[0]


@transaction.atomic
def save_meal_order(
    *,
    registration: Registration,
    menu: Menu | None = None,
    beverage: Beverage | None = None,
    drink: Beverage | None = None,
    no_meal: bool = False,
    payment_method: str | None = None,
    actor=None,
    actor_is_organizer: bool = False,
) -> MealOrder:
    """Enregistre un choix et fige les tarifs courants des options choisies."""

    organizer = _is_organizer(actor, explicit=actor_is_organizer)
    if beverage is not None and drink is not None and beverage.pk != drink.pk:
        raise BusinessRuleError(
            "Deux boissons différentes ont été transmises.", code="conflicting_beverage"
        )
    # ``drink`` est l'alias historique utilisé par les premières vues.
    beverage = beverage or drink
    locked_registration = (
        Registration.objects.select_for_update(of=("self",))
        .select_related("member__family", "event_day__event")
        .get(pk=registration.pk)
    )
    _check_member_access(actor, locked_registration.member, organizer=organizer)
    _check_meal_window(locked_registration.event_day.event, organizer=organizer)
    if locked_registration.status != Registration.Status.ACTIVE:
        raise BusinessRuleError(
            "Un repas ne peut être réservé que pour une inscription active.",
            code="inactive_registration",
        )
    order = MealOrder.objects.select_for_update(of=("self",)).filter(registration=locked_registration).first()
    try:
        if menu is not None:
            menu = Menu.objects.select_for_update(of=("self",)).get(pk=menu.pk)
        if beverage is not None:
            beverage = Beverage.objects.select_for_update(of=("self",)).get(pk=beverage.pk)
    except (Menu.DoesNotExist, Beverage.DoesNotExist) as error:
        raise BusinessRuleError("Cette option de restauration n’est plus disponible. Choisissez à nouveau.", code="removed_meal_option") from error
    if no_meal and (menu is not None or beverage is not None):
        raise BusinessRuleError(
            "Un choix « pas de repas » ne peut contenir ni menu ni boisson.",
            code="incoherent_meal",
        )
    if not no_meal and menu is None:
        raise BusinessRuleError(
            "Choisissez un menu ou indiquez « pas de repas ».", code="missing_menu"
        )
    for option, label in ((menu, "menu"), (beverage, "boisson")):
        if option is None:
            continue
        if option.event_day_id != locked_registration.event_day_id:
            raise BusinessRuleError(
                f"Cette {label} n'appartient pas à la journée d'inscription.",
                code="wrong_meal_day",
            )
        preserved = order is not None and option.pk == (
            order.menu_id if label == "menu" else order.beverage_id
        )
        if not option.is_active and not preserved:
            raise BusinessRuleError(
                f"Cette {label} n'est plus proposée.", code="inactive_meal_option"
            )

    if order is None:
        order = MealOrder(registration=locked_registration)
    if order.legacy_availability == "legacy":
        order.legacy_availability = locked_registration.availability
    if order.menu_id != (menu.pk if menu is not None and not no_meal else None) or (order.legacy_availability and order.legacy_availability != locked_registration.availability):
        order.legacy_availability = ""
    order.wants_meal = not no_meal
    order.menu = None if no_meal else menu
    order.beverage = None if no_meal else beverage
    if payment_method is not None:
        if payment_method not in MealOrder.PaymentMethod.values:
            raise BusinessRuleError("Le mode de paiement souhaité est invalide.", code="invalid_payment_method")
        order.payment_method = payment_method
    order.corrected_by = actor if organizer and getattr(actor, "is_authenticated", False) else None
    try:
        # MealOrder.save() réalise le snapshot uniquement si le choix change.
        order.save()
    except ValidationError as exc:
        raise _business_error(exc, code="invalid_meal_order") from exc
    except IntegrityError as exc:
        raise BusinessRuleError(
            "Une réservation de repas existe déjà pour cette inscription.",
            code="duplicate_meal_order",
        ) from exc

    balance = family_event_balance(
        family=locked_registration.member.family,
        event=locked_registration.event_day.event,
    )
    order.balance_after_change = balance
    order.overpayment_cents = balance["overpayment_cents"]
    return order


# Alias correspondant au vocabulaire des formulaires et du cahier des charges.
save_meal_reservation = save_meal_order


def family_event_balance(*, family: Family, event: Event) -> dict[str, int | str]:
    """Calcule les montants depuis les snapshots, jamais depuis les tarifs actuels."""

    due_cents = (
        MealOrder.objects.filter(
            registration__member__family=family,
            registration__event_day__event=event,
            registration__status=Registration.Status.ACTIVE,
        ).aggregate(total=Coalesce(Sum("total_surcharge_cents"), 0))["total"]
        or 0
    )
    paid_cents = (
        Payment.objects.filter(family=family, event=event).aggregate(
            total=Coalesce(Sum("amount_cents"), 0)
        )["total"]
        or 0
    )
    signed_balance = int(due_cents) - int(paid_cents)
    if due_cents == 0 and paid_cents == 0:
        status = "no_surcharge"
    elif paid_cents == 0:
        status = "unpaid"
    elif paid_cents < due_cents:
        status = "partial"
    elif paid_cents == due_cents:
        status = "paid"
    else:
        status = "overpaid"
    return {
        "due_cents": int(due_cents),
        "paid_cents": int(paid_cents),
        "balance_cents": signed_balance,
        "amount_remaining_cents": max(signed_balance, 0),
        "overpayment_cents": max(-signed_balance, 0),
        "status": status,
    }


@transaction.atomic
def record_payment(
    *,
    family: Family,
    event: Event,
    amount_cents: int,
    method: str,
    paid_at: datetime | None = None,
    note: str = "",
    recorded_by=None,
) -> Payment:
    """Ajoute un paiement manuel ; aucun clic SumUp n'appelle ce service seul."""

    _check_organizer_access(recorded_by)
    if isinstance(amount_cents, bool) or not isinstance(amount_cents, int) or amount_cents <= 0:
        raise BusinessRuleError(
            "Le montant du paiement doit être un nombre entier positif de centimes.",
            code="invalid_payment_amount",
        )
    if method not in Payment.Method.values:
        raise BusinessRuleError("Le mode de paiement est invalide.", code="invalid_payment_method")
    locked_family = Family.objects.select_for_update(of=("self",)).get(pk=family.pk)
    locked_event = Event.objects.get(pk=event.pk)
    payment = Payment(
        family=locked_family,
        event=locked_event,
        amount_cents=amount_cents,
        method=method,
        paid_at=paid_at or timezone.now(),
        note=(note or "").strip(),
        recorded_by=recorded_by,
    )
    try:
        payment.full_clean()
        payment.save()
    except ValidationError as exc:
        raise _business_error(exc, code="invalid_payment") from exc
    payment.balance_after_payment = family_event_balance(family=locked_family, event=locked_event)
    return payment


def birthday_alerts_for_event_day(event_day: EventDay) -> list[dict[str, Any]]:
    registrations = (
        Registration.objects.filter(
            event_day=event_day,
            status=Registration.Status.ACTIVE,
        )
        .select_related("member")
        .order_by("member__last_name", "member__first_name")
    )
    alerts = []
    for registration in registrations:
        member = registration.member
        if not member.has_birthday_on(event_day.date):
            continue
        age = member.age_on(event_day.date)
        alerts.append(
            {
                "registration_id": registration.pk,
                "member_id": member.pk,
                "member_name": member.full_name,
                "event_day_id": event_day.pk,
                "date": event_day.date,
                "age": age,
                "message": (
                    f"Anniversaire de {member.full_name} le "
                    f"{event_day.date:%d/%m/%Y} — {age} ans"
                ),
            }
        )
    return alerts


def birthday_alerts_for_event(event: Event) -> list[dict[str, Any]]:
    """Retourne les anniversaires des journées réellement inscrites.

    ``Member.birthday_in_year`` applique la règle documentée : une naissance le
    29 février est célébrée le 28 février lors d'une année non bissextile.
    """

    alerts: list[dict[str, Any]] = []
    for event_day in event.days.order_by("date"):
        alerts.extend(birthday_alerts_for_event_day(event_day))
    return alerts


def assignments_requiring_communication(*, event: Event, family: Family):
    """Affectations confirmées jamais envoyées ou modifiées depuis le dernier envoi."""

    assignments = list(
        Assignment.objects.filter(
            registration__event_day__event=event,
            registration__member__family=family,
        )
        .filter(
            Q(
                registration__status=Registration.Status.ACTIVE,
                status=Assignment.Status.CONFIRMED,
            )
            | Q(status=Assignment.Status.CANCELLED, communications__isnull=False)
        )
        .select_related(
            "registration__member",
            "registration__event_day__event",
            "station_slot__station",
            "station_slot__event_day",
        )
        .prefetch_related("communications")
        .distinct()
        .order_by(
            "registration__member__last_name",
            "registration__member__first_name",
            "station_slot__event_day__date",
            "period",
        )
    )
    return [assignment for assignment in assignments if assignment.needs_communication]


def build_assignment_email_preview(
    *, event: Event, family: Family, extra_instructions: str = ""
) -> dict[str, Any]:
    """Construit un aperçu sans envoyer ni marquer quoi que ce soit comme envoyé."""

    assignments = assignments_requiring_communication(event=event, family=family)
    has_previous_version = any(assignment.communications.exists() for assignment in assignments)
    kind = (
        EmailDelivery.Kind.ASSIGNMENT_UPDATE
        if has_previous_version
        else EmailDelivery.Kind.ASSIGNMENT_CONFIRMATION
    )
    heading = (
        "Mise à jour de vos affectations"
        if kind == EmailDelivery.Kind.ASSIGNMENT_UPDATE
        else "Confirmation de vos affectations"
    )
    lines = [
        f"Bonjour {family.account.first_name or family.display_name},",
        "",
        f"{heading} pour l'événement « {event.name} » :",
        "",
    ]
    for assignment in assignments:
        slot = assignment.station_slot
        member = assignment.registration.member
        schedule = (
            f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"
            if slot.start_time and slot.end_time
            else slot.get_period_display()
        )
        if assignment.status == Assignment.Status.CANCELLED:
            lines.extend(
                [
                    f"• {member.full_name}",
                    f"  AFFECTATION RETIRÉE : {assignment.registration.event_day.date:%d/%m/%Y} — "
                    f"{slot.station.name} — {schedule}",
                ]
            )
        else:
            lines.extend(
                [
                    f"• {member.full_name}",
                    f"  {assignment.registration.event_day.date:%d/%m/%Y} — "
                    f"{slot.station.name} — {schedule}",
                ]
            )
        if slot.station.instructions:
            lines.append(f"  Consignes : {slot.station.instructions}")
    if event.general_instructions:
        lines.extend(["", f"Consignes générales : {event.general_instructions}"])
    if extra_instructions.strip():
        lines.extend(["", f"Informations complémentaires : {extra_instructions.strip()}"])
    lines.extend(
        [
            "",
            "Consultez vos affectations à jour dans votre espace famille :",
            f"{settings.SITE_URL}/famille/affectations/",
            "",
            "L'équipe organisatrice",
        ]
    )
    return {
        "family": family,
        "assignments": assignments,
        "kind": kind,
        "subject": f"{heading} — {event.name}",
        "body_text": "\n".join(lines),
        "to_email": family.account.email,
        "extra_instructions": extra_instructions.strip(),
    }


@transaction.atomic
def prepare_assignment_delivery(
    *, event: Event, family: Family, extra_instructions: str = "", actor=None
) -> EmailDelivery | None:
    """Fige un message prêt à envoyer; retourne None si rien n'a changé."""

    _check_organizer_access(actor)
    locked_family = Family.objects.select_for_update(of=("self",)).select_related("account").get(pk=family.pk)
    preview = build_assignment_email_preview(
        event=event, family=locked_family, extra_instructions=extra_instructions
    )
    if not preview["assignments"]:
        return None
    signatures = sorted(item.communication_signature() for item in preview["assignments"])
    stable_material = "|".join(
        [str(event.pk), str(locked_family.pk), preview["kind"], preview["body_text"], *signatures]
    )
    idempotency_key = uuid.uuid5(uuid.NAMESPACE_URL, f"amelone:assignment-email:{stable_material}")
    delivery, created = EmailDelivery.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={
            "event": event,
            "family": locked_family,
            "kind": preview["kind"],
            "status": EmailDelivery.Status.DRAFT,
            "to_email": preview["to_email"],
            "subject": preview["subject"],
            "body_text": preview["body_text"],
            "extra_instructions": preview["extra_instructions"],
        },
    )
    # Un échec existant reste disponible pour l'action explicite de relance.
    if not created and delivery.status == EmailDelivery.Status.FAILED:
        return None
    return delivery


@transaction.atomic
def send_assignment_delivery(*, delivery: EmailDelivery, actor=None) -> EmailDelivery:
    """Envoie un brouillon/échec et journalise l'acceptation SMTP ou l'erreur.

    ``ACCEPTED`` signifie que le backend d'envoi a accepté le message. Aucune
    livraison finale n'est inventée en l'absence de webhook fournisseur.
    """

    _check_organizer_access(actor)
    locked = (
        EmailDelivery.objects.select_for_update(of=("self",))
        .select_related("event", "family__account")
        .get(pk=delivery.pk)
    )
    if locked.status in (EmailDelivery.Status.ACCEPTED, EmailDelivery.Status.DELIVERED):
        return locked
    if not settings.EMAIL_ENABLED:
        raise BusinessRuleError(
            "Le service d'e-mail n'est pas configuré. Aucun message n'a été marqué envoyé.",
            code="email_not_configured",
        )
    preview = build_assignment_email_preview(
        event=locked.event,
        family=locked.family,
        extra_instructions=locked.extra_instructions,
    )
    if not preview["assignments"]:
        raise BusinessRuleError(
            "Aucune affectation nouvelle ou modifiée ne nécessite d'envoi.",
            code="nothing_to_communicate",
        )
    # Une relance d'échec repart toujours de l'état actuel afin que le corps et
    # les instantanés enregistrés après acceptation décrivent la même version.
    locked.kind = preview["kind"]
    locked.subject = preview["subject"]
    locked.body_text = preview["body_text"]
    locked.to_email = preview["to_email"]
    attempt_number = (locked.attempts.aggregate(last=Max("attempt_number"))["last"] or 0) + 1
    now = timezone.now()
    locked.status = EmailDelivery.Status.PENDING
    locked.last_attempt_at = now
    locked.failure_reason = ""
    locked.save(
        update_fields=(
            "kind",
            "subject",
            "body_text",
            "to_email",
            "status",
            "last_attempt_at",
            "failure_reason",
            "updated_at",
        )
    )
    try:
        accepted_count = EmailMessage(
            subject=locked.subject,
            body=locked.body_text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[locked.to_email],
        ).send(fail_silently=False)
        if accepted_count != 1:
            raise RuntimeError("Le service d'envoi n'a pas accepté le message.")
    except Exception as exc:
        locked.status = EmailDelivery.Status.FAILED
        locked.failure_reason = str(exc)[:2000]
        locked.save(update_fields=("status", "failure_reason", "updated_at"))
        EmailAttempt.objects.create(
            delivery=locked,
            attempt_number=attempt_number,
            status=EmailAttempt.Status.FAILED,
            error_message=locked.failure_reason,
            attempted_at=now,
        )
        return locked

    locked.status = EmailDelivery.Status.ACCEPTED
    locked.accepted_at = now
    locked.failure_reason = ""
    locked.save(update_fields=("status", "accepted_at", "failure_reason", "updated_at"))
    EmailAttempt.objects.create(
        delivery=locked,
        attempt_number=attempt_number,
        status=EmailAttempt.Status.ACCEPTED,
        attempted_at=now,
    )
    assignments = assignments_requiring_communication(event=locked.event, family=locked.family)
    for assignment in assignments:
        last_version = (
            AssignmentCommunication.objects.filter(assignment=assignment).aggregate(
                last=Max("version")
            )["last"]
            or 0
        )
        AssignmentCommunication.objects.create(
            assignment=assignment,
            delivery=locked,
            version=last_version + 1,
            snapshot=assignment.communication_payload(),
            signature=assignment.communication_signature(),
            communicated_at=now,
        )
    return locked


def send_assignment_confirmations(
    *,
    event: Event,
    families: Iterable[Family],
    extra_instructions: str = "",
    actor=None,
) -> list[EmailDelivery]:
    """Prépare puis envoie au plus un message groupé par famille."""

    _check_organizer_access(actor)
    deliveries = []
    for family in families:
        delivery = prepare_assignment_delivery(
            event=event,
            family=family,
            extra_instructions=extra_instructions,
            actor=actor,
        )
        if delivery is not None:
            try:
                delivery = send_assignment_delivery(delivery=delivery, actor=actor)
            except BusinessRuleError:
                # Le brouillon reste visible et surtout n'est pas marqué envoyé.
                delivery.refresh_from_db()
            deliveries.append(delivery)
    return deliveries


def retry_failed_deliveries(
    *, event: Event, families: Iterable[Family] | None = None, actor=None
) -> list[EmailDelivery]:
    """Relance uniquement les messages en échec, sans dupliquer les succès."""

    _check_organizer_access(actor)
    query = EmailDelivery.objects.filter(event=event, status=EmailDelivery.Status.FAILED)
    if families is not None:
        query = query.filter(family__in=list(families))
    return [send_assignment_delivery(delivery=item, actor=actor) for item in query]

