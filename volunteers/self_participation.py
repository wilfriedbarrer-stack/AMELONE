"""Inscription personnelle, avec affectation directe réservée aux administrateurs."""

from django.core.exceptions import PermissionDenied
from django.db import transaction

from .models import Assignment, Event, EventDay, Member, StationSlot, User
from .participation import save_participation
from .services import BusinessRuleError, confirm_assignments, create_assignment, remove_assignment


@transaction.atomic
def save_personal_participation(*, actor, event_day, availability, preferred_station=None,
                                note="", meal_choices=None, station_slots=None):
    user = User.objects.select_for_update().get(pk=actor.pk)
    if not user.is_active or not (user.is_superuser or user.is_competition_official):
        raise PermissionDenied("Un profil administrateur ou officiel de compétition est nécessaire.")
    if station_slots is not None and not user.is_superuser:
        raise PermissionDenied("Seul un administrateur peut confirmer ses propres postes.")
    member = Member.objects.select_for_update(of=("self",)).select_related("family").filter(
        user=user, family__account=user, archived_at__isnull=True,
    ).first()
    if member is None:
        raise PermissionDenied("Votre profil personnel doit appartenir à votre compte et être actif.")
    # Verrouiller événement puis journée, comme dans les modifications d’événement.
    # Un archivage concurrent doit attendre la fin de cette inscription.
    try:
        event_id = EventDay.objects.values_list("event_id", flat=True).get(pk=event_day.pk)
        event = Event.objects.select_for_update().get(pk=event_id)
        event_day = EventDay.objects.select_for_update(of=("self",)).select_related("event").get(pk=event_day.pk)
    except (Event.DoesNotExist, EventDay.DoesNotExist) as error:
        raise BusinessRuleError("Cette journée n’est plus disponible.") from error
    if event_day.event_id != event.pk:
        raise BusinessRuleError("L’événement de cette journée a changé. Rechargez le formulaire.")
    event_status = event.status
    if event_status == Event.Status.ARCHIVED or (
        not user.is_superuser and event_status != Event.Status.PUBLISHED
    ):
        raise BusinessRuleError("Cet événement n’est pas disponible pour votre inscription.")
    registration = save_participation(
        member=member, event_day=event_day, availability=availability,
        preferred_station=preferred_station, note=note, actor=user, meal_choices=meal_choices,
    )
    if station_slots is None:
        return registration
    selected_ids = {slot.pk for slot in station_slots}
    current = list(registration.assignments.exclude(status=Assignment.Status.CANCELLED))
    slot_ids = selected_ids | {item.station_slot_id for item in current}
    slots = {slot.pk: slot for slot in StationSlot.objects.select_for_update(of=("self",))
             .filter(pk__in=slot_ids).select_related("station", "event_day").order_by("pk")}
    if len(slots) != len(slot_ids):
        raise BusinessRuleError("Un des créneaux n’est plus disponible.")
    confirmed_slot_ids = {item.station_slot_id for item in current if item.status == Assignment.Status.CONFIRMED}
    for slot_id in selected_ids:
        slot = slots[slot_id]
        if slot.event_day_id != registration.event_day_id:
            raise BusinessRuleError("Choisissez un poste actif de cette journée.")
        if not slot.station.is_active and slot_id not in confirmed_slot_ids:
            raise BusinessRuleError("Ce poste est inactif. Choisissez un autre poste ou retirez cette affectation.")
    retained = []
    for item in current:
        if item.station_slot_id in selected_ids:
            retained.append(item)
        else:
            remove_assignment(assignment=item, actor=user, reason="Poste retiré de ma participation.")
    retained_ids = {item.station_slot_id for item in retained}
    for slot_id in sorted(selected_ids - retained_ids):
        retained.append(create_assignment(registration=registration, station_slot=slots[slot_id], actor=user))
    drafts = [item for item in retained if item.status == Assignment.Status.DRAFT]
    if drafts:
        confirm_assignments(assignments=drafts, confirmed_by=user)
    return registration
