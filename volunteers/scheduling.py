"""Vérification des horaires d'une même personne sur l'ensemble des événements."""

from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import combinations

from django.core.exceptions import ValidationError
from django.db import transaction

_schedule_context = ContextVar("station_schedule_context", default=None)


def _active_assignments():
    from .models import Assignment, Event, Registration

    return Assignment.objects.filter(
        status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
        registration__status=Registration.Status.ACTIVE,
    ).exclude(registration__event_day__event__status=Event.Status.ARCHIVED)


def _with_schedule(queryset):
    return queryset.select_related(
        "registration__member", "registration__event_day__event",
        "station_slot__station__event", "station_slot__event_day",
    ).order_by("pk")


def _slot_member_ids(slots):
    slot_ids = {slot.pk for slot in slots if slot.pk}
    return set(_active_assignments().filter(station_slot_id__in=slot_ids).values_list(
        "registration__member_id", flat=True,
    )) if slot_ids else set()


def lock_schedule_members(slots):
    """À appeler dans une transaction, avant tout verrou Event/Day/Slot."""
    from .models import Member

    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Le verrouillage des horaires nécessite une transaction.")
    member_ids = _slot_member_ids(list(slots))
    list(Member.objects.select_for_update(of=("self",)).filter(
        pk__in=member_ids,
    ).order_by("pk"))
    return member_ids


def _assert_schedule_members_locked(slots, member_ids):
    if _slot_member_ids(slots) - set(member_ids):
        raise ValidationError(
            "Les affectations de ce poste ont changé pendant la modification. "
            "Actualisez la page et réessayez, sans retirer les personnes inscrites.",
            code="schedule_changed",
        )


def _conflict_message(member_name, day, first_slot, second_slot):
    first = first_slot.station
    second = second_slot.station
    first_name = f"{first.name} ({first.event.name})"
    second_name = f"{second.name} ({second.event.name})"
    return (
        f"Les horaires de {member_name} se chevauchent le {day:%d/%m/%Y} : "
        f"« {first_name} » et « {second_name} ». Modifiez les horaires ou les affectations."
    )


def validate_assignment_schedule(assignment):
    """L'appelant sérialise les écritures de la personne avec son verrou Member."""
    from .models import Event, Registration

    registration = assignment.registration
    if (
        registration.status != Registration.Status.ACTIVE
        or registration.event_day.event.status == Event.Status.ARCHIVED
    ):
        return
    others = _active_assignments().filter(
        registration__member_id=registration.member_id,
        registration__event_day__date=registration.event_day.date,
    ).exclude(pk=assignment.pk)
    # Preserve the existing one-post-per-period rule for one registration.
    if others.filter(registration=registration, period=assignment.station_slot.period).exists():
        raise ValidationError(
            "Cette personne est déjà affectée sur cette période.", code="duplicate_assignment_period",
        )
    for other in _with_schedule(others):
        if assignment.station_slot.overlaps(other.station_slot):
            raise ValidationError(
                _conflict_message(
                    registration.member.full_name, registration.event_day.date,
                    assignment.station_slot, other.station_slot,
                ), code="assignment_overlap",
            )


def _proposed_slots(slots):
    context = _schedule_context.get()
    proposed = dict(context["slots"]) if context else {}
    proposed.update({slot.pk: slot for slot in slots if slot.pk})
    return proposed


def validate_station_schedule(slots, *, locked_member_ids=None):
    """Compare l'état final de toutes les lignes, sans modifier les affectations.

    Le contrôle de formulaire est indicatif ; le même contrôle doit être relancé
    sous les verrous Member, puis Day et Slot, avant l'enregistrement.
    """
    from .models import StationSlot

    proposed = _proposed_slots(list(slots))
    if locked_member_ids is not None:
        _assert_schedule_members_locked(proposed.values(), locked_member_ids)
    originals = {slot.pk: slot for slot in StationSlot.objects.filter(
        pk__in=proposed,
    )}
    fields = ("event_day_id", "period", "start_time", "end_time")
    changed_ids = {
        pk for pk, slot in proposed.items() if pk in originals and any(
            getattr(slot, field) != getattr(originals[pk], field) for field in fields
        )
    }
    if not changed_ids:
        return
    affected = list(_with_schedule(_active_assignments().filter(station_slot_id__in=changed_ids)))
    if not affected:
        return
    pairs = {(item.registration.member_id, item.registration.event_day.date) for item in affected}
    candidates = _with_schedule(_active_assignments().filter(
        registration__member_id__in={pair[0] for pair in pairs},
        registration__event_day__date__in={pair[1] for pair in pairs},
    ))
    grouped = defaultdict(list)
    for item in candidates:
        key = (item.registration.member_id, item.registration.event_day.date)
        if key in pairs:
            grouped[key].append(item)
    for (_, day), assignments in grouped.items():
        for first, second in combinations(assignments, 2):
            if first.station_slot_id not in changed_ids and second.station_slot_id not in changed_ids:
                continue
            first_slot = proposed.get(first.station_slot_id, first.station_slot)
            second_slot = proposed.get(second.station_slot_id, second.station_slot)
            if first_slot.overlaps(second_slot):
                raise ValidationError(
                    _conflict_message(first.registration.member.full_name, day, first_slot, second_slot),
                    code="assignment_overlap",
                )


@contextmanager
def station_schedule_context(slots, *, locked_member_ids):
    """Conserve les horaires finaux pendant les saves successifs d'un formset."""
    slots = list(slots)
    validate_station_schedule(slots, locked_member_ids=locked_member_ids)
    token = _schedule_context.set({
        "slots": {slot.pk: slot for slot in slots if slot.pk},
        "member_ids": set(locked_member_ids),
    })
    try:
        yield
    finally:
        _schedule_context.reset(token)


def current_station_schedule(slot):
    """Contexte interne à StationSlot.save ; les membres sont déjà verrouillés."""
    context = _schedule_context.get()
    return context if context and slot.pk in context["slots"] else None


def event_assignment_conflicts(event):
    """Avertissements privés, sans annulation ni réaffectation automatique."""
    affected = list(_with_schedule(_active_assignments().filter(
        registration__event_day__event=event,
    )))
    if not affected:
        return []
    pairs = {(item.registration.member_id, item.registration.event_day.date) for item in affected}
    candidates = _with_schedule(_active_assignments().filter(
        registration__member_id__in={pair[0] for pair in pairs},
        registration__event_day__date__in={pair[1] for pair in pairs},
    ))
    grouped = defaultdict(list)
    for item in candidates:
        key = (item.registration.member_id, item.registration.event_day.date)
        if key in pairs:
            grouped[key].append(item)
    conflicts = []
    for (_, day), assignments in grouped.items():
        for first, second in combinations(assignments, 2):
            if event.pk not in (first.registration.event_day.event_id, second.registration.event_day.event_id):
                continue
            if not first.station_slot.overlaps(second.station_slot):
                continue
            member = first.registration.member
            conflicts.append({
                "member_id": member.pk, "member_name": member.full_name, "date": day,
                "first_assignment_id": first.pk, "second_assignment_id": second.pk,
                "first_station": first.station_slot.station.name,
                "second_station": second.station_slot.station.name,
                "first_event": first.registration.event_day.event.name,
                "second_event": second.registration.event_day.event.name,
                "message": _conflict_message(member.full_name, day, first.station_slot, second.station_slot),
            })
    return sorted(conflicts, key=lambda item: (item["date"], item["member_name"], item["first_assignment_id"]))
