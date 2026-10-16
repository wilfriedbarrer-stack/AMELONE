"""Places simultanées partagées entre journée et demi-journées d'un poste."""

from collections import defaultdict
from dataclasses import dataclass, replace

from django.core.exceptions import ValidationError
from django.db.models import Count, Q


@dataclass(frozen=True)
class SlotCapacity:
    """Compteurs scalaires, sans inscription ni donnée personnelle."""

    capacity: int
    occupied: int
    remaining: int
    morning_capacity: int
    afternoon_capacity: int
    morning_occupied: int
    afternoon_occupied: int


@dataclass(frozen=True)
class _SlotState:
    pk: object
    station_id: object
    event_day_id: object
    period: str
    capacity: int
    occupied: int


def _load_states(slots, *, statuses=None, exclude_assignment_id=None):
    from .models import Assignment, Registration, StationSlot

    groups = {(slot.station_id, slot.event_day_id) for slot in slots}
    occupied = Q(
        assignments__status__in=(
            (Assignment.Status.DRAFT, Assignment.Status.CONFIRMED)
            if statuses is None else tuple(statuses)
        ),
        assignments__registration__status=Registration.Status.ACTIVE,
    )
    if exclude_assignment_id is not None:
        occupied &= ~Q(assignments__pk=exclude_assignment_id)
    rows = StationSlot.objects.filter(
        station_id__in={group[0] for group in groups},
        event_day_id__in={group[1] for group in groups},
    ).values("pk", "station_id", "event_day_id", "period", "capacity").annotate(
        occupied=Count("assignments", filter=occupied),
    )
    return {
        row["pk"]: _SlotState(**row) for row in rows
        if (row["station_id"], row["event_day_id"]) in groups
    }


def _overlay(states, slots):
    proposed = dict(states)
    for slot in slots:
        original = states.get(slot.pk)
        proposed[slot.pk] = _SlotState(
            pk=slot.pk, station_id=slot.station_id, event_day_id=slot.event_day_id,
            period=slot.period, capacity=slot.capacity,
            occupied=original.occupied if original else 0,
        )
    return proposed


def _snapshots(states):
    from .models import StationSlot

    grouped = defaultdict(list)
    for state in states.values():
        grouped[(state.station_id, state.event_day_id)].append(state)
    snapshots = {}
    for group in grouped.values():
        capacities = defaultdict(int)
        occupations = defaultdict(int)
        for state in group:
            capacities[state.period] = max(capacities[state.period], state.capacity)
            occupations[state.period] += state.occupied
        full = StationSlot.Period.FULL_DAY
        morning = StationSlot.Period.MORNING
        afternoon = StationSlot.Period.AFTERNOON
        morning_capacity = max(capacities[full], capacities[morning])
        afternoon_capacity = max(capacities[full], capacities[afternoon])
        morning_occupied = occupations[full] + occupations[morning]
        afternoon_occupied = occupations[full] + occupations[afternoon]
        for state in group:
            margins = [state.capacity - state.occupied]
            if state.period in (full, morning):
                margins.append(morning_capacity - morning_occupied)
            if state.period in (full, afternoon):
                margins.append(afternoon_capacity - afternoon_occupied)
            snapshots[state.pk] = SlotCapacity(
                capacity=state.capacity, occupied=state.occupied,
                remaining=max(min(margins), 0),
                morning_capacity=morning_capacity, afternoon_capacity=afternoon_capacity,
                morning_occupied=morning_occupied, afternoon_occupied=afternoon_occupied,
            )
    return snapshots


def get_slot_capacities(slots, *, statuses=None):
    """Charge aussi les créneaux frères absents de la sélection, en une requête.

    Les quotas locaux restent valables. Chaque budget de demi-journée est le
    maximum de son quota et du quota journée, jamais leur somme.
    Les capacités des instances fournies remplacent leurs valeurs persistées :
    le même calcul sert aux formulaires et à la validation de leur état final.
    """
    slots = list(slots)
    if not slots:
        return {}
    states = _load_states(slots, statuses=statuses)
    snapshots = _snapshots(_overlay(states, slots))
    return {slot.pk: snapshots[slot.pk] for slot in slots}


def get_slot_capacity(slot, *, exclude_assignment_id=None, statuses=None):
    states = _load_states(
        [slot], statuses=statuses, exclude_assignment_id=exclude_assignment_id,
    )
    return _snapshots(_overlay(states, [slot]))[slot.pk]


def _validate_excess(states, proposed, error):
    before = _snapshots(states)
    after = _snapshots(proposed)
    for pk, current in after.items():
        previous = before.get(pk)
        if previous is None:
            continue
        for occupied, capacity in (
            ("occupied", "capacity"),
            ("morning_occupied", "morning_capacity"),
            ("afternoon_occupied", "afternoon_capacity"),
        ):
            previous_excess = max(getattr(previous, occupied) - getattr(previous, capacity), 0)
            current_excess = max(getattr(current, occupied) - getattr(current, capacity), 0)
            if current_excess > previous_excess:
                raise ValidationError(error)


def validate_slot_capacity_changes(slots):
    """Refuse seulement une aggravation des dépassements déjà présents.

    L'appelant verrouille les journées avant de persister les modifications.
    Un lot de formulaires doit fournir toutes ses capacités finales ensemble.
    """
    slots = [slot for slot in slots if (
        slot.station_id and slot.event_day_id and slot.capacity is not None
        and slot.capacity >= 0
    )]
    if not slots:
        return
    states = _load_states(slots)
    _validate_excess(states, _overlay(states, slots), {
        "capacity": (
            "Cette capacité est insuffisante pour les affectations actives du poste. "
            "Réorganisez les affectations avant de réduire le nombre de places."
        ),
    })


def validate_registration_reactivation(registration):
    """Une inscription réactivée ne doit pas ressusciter un dépassement."""
    from .models import Assignment

    assignments = list(Assignment.objects.filter(
        registration_id=registration.pk,
        status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
    ).select_related("station_slot"))
    if not assignments:
        return
    states = _load_states([assignment.station_slot for assignment in assignments])
    proposed = dict(states)
    for assignment in assignments:
        state = proposed[assignment.station_slot_id]
        proposed[state.pk] = replace(state, occupied=state.occupied + 1)
    _validate_excess(states, proposed, (
        "Cette réactivation dépasserait les places disponibles du poste. "
        "Réorganisez ou retirez ses affectations avant de réactiver l’inscription."
    ))
