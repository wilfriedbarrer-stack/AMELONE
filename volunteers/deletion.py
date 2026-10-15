"""Suppression des configurations vides et conservation des historiques."""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    Assignment,
    Attendance,
    Beverage,
    EmailDelivery,
    Event,
    EventDay,
    EventNotificationCampaign,
    EventNotificationDelivery,
    EventProfile,
    MealOrder,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
    User,
)


def _lock_actor(actor):
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("Cette opération est réservée aux administrateurs et organisateurs.")
    try:
        # Les références d'audit (confirmed_by, recorded_by…) prennent KEY SHARE
        # sur User après Day. NO KEY UPDATE évite un cycle User→Day→User tout en
        # bloquant une modification concurrente des droits de l'acteur.
        current = User.objects.select_for_update(of=("self",), no_key=True).get(pk=actor.pk)
    except User.DoesNotExist as error:
        raise PermissionDenied("Votre compte n'est plus disponible.") from error
    if not current.is_active or not (current.is_superuser or current.is_staff or current.is_organizer):
        raise PermissionDenied("Cette opération est réservée aux administrateurs et organisateurs.")
    return current


def _check_version(current, expected_updated_at):
    try:
        expected = parse_datetime(expected_updated_at) if isinstance(expected_updated_at, str) else None
    except ValueError:
        expected = None
    if expected is None or timezone.is_naive(expected) or expected != current.updated_at:
        raise ValidationError(
            "Cet élément a été modifié depuis l'ouverture de la confirmation. "
            "Rechargez la page avant de réessayer."
        )


def get_event_deletion_summary(event):
    """Prépare le récapitulatif ; le service le recalcule sous verrou au POST."""

    event_id = event.pk
    assignments = Assignment.objects.filter(
        Q(registration__event_day__event_id=event_id)
        | Q(station_slot__event_day__event_id=event_id)
        | Q(station_slot__station__event_id=event_id)
    )
    orders = MealOrder.objects.filter(
        Q(registration__event_day__event_id=event_id)
        | Q(menu__event_day__event_id=event_id)
        | Q(beverage__event_day__event_id=event_id)
    )
    counts = {
        "days": EventDay.objects.filter(event_id=event_id).count(),
        "stations": Station.objects.filter(event_id=event_id).count(),
        "slots": StationSlot.objects.filter(
            Q(event_day__event_id=event_id) | Q(station__event_id=event_id)
        ).count(),
        "menus": Menu.objects.filter(event_day__event_id=event_id).count(),
        "drinks": Beverage.objects.filter(event_day__event_id=event_id).count(),
        "registrations": Registration.objects.filter(event_day__event_id=event_id).count(),
        "assignments": assignments.count(),
        "orders": orders.count(),
        "payments": Payment.objects.filter(event_id=event_id).count(),
        "attendances": Attendance.objects.filter(event_day__event_id=event_id).count(),
        "emails": (
            EmailDelivery.objects.filter(event_id=event_id).count()
            + EventNotificationDelivery.objects.filter(campaign__event_id=event_id).count()
        ),
    }
    has_history = any(counts[field] for field in (
        "registrations", "assignments", "orders", "payments", "attendances", "emails",
    ))
    return {"counts": counts, "has_history": has_history, "mode": "archive" if has_history else "delete"}


@transaction.atomic
def delete_event_or_archive(*, event, actor, expected_updated_at):
    """Supprime un événement vide ; archive toute configuration avec historique.

    Une protection résiduelle inattendue déclenche aussi l'archivage, après
    annulation complète de la tentative de suppression dans un savepoint.
    """

    _lock_actor(actor)
    try:
        current = Event.objects.select_for_update(of=("self",)).get(pk=event.pk)
    except Event.DoesNotExist as error:
        raise ValidationError("Cet événement a déjà été supprimé. Rechargez la liste.") from error
    _check_version(current, expected_updated_at)
    list(EventDay.objects.select_for_update(of=("self",)).filter(event=current).order_by("pk"))
    list(EventNotificationCampaign.objects.select_for_update(of=("self",)).filter(
        event=current,
    ).order_by("pk"))
    summary = get_event_deletion_summary(current)
    result = {"id": current.pk, "name": current.name, "mode": summary["mode"], "summary": summary}
    if not summary["has_history"]:
        try:
            with transaction.atomic():
                EventNotificationCampaign.objects.filter(event=current).delete()
                current.delete()
        except ProtectedError:
            # Aucun objet protégé ou paiement ne doit être détruit, même si un
            # lien historique inhabituel n'était pas compté dans le résumé.
            result["mode"] = "archive"
            summary["mode"] = "archive"
            summary["has_history"] = True
        else:
            return result
    current.status = Event.Status.ARCHIVED
    current.save(update_fields=("status", "updated_at"))
    return result


@transaction.atomic
def delete_reusable_event_profile(*, profile, actor, expected_updated_at):
    """Supprime le modèle seul : ses événements déjà créés sont indépendants."""

    _lock_actor(actor)
    try:
        current = EventProfile.objects.select_for_update(of=("self",)).get(pk=profile.pk)
    except EventProfile.DoesNotExist as error:
        raise ValidationError("Ce profil d'événement a déjà été supprimé. Rechargez la liste.") from error
    _check_version(current, expected_updated_at)
    result = {"id": current.pk, "name": current.name, "mode": "delete"}
    current.delete()
    return result
