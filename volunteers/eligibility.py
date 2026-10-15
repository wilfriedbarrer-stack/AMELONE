"""Conditions individuelles de poste et encadrement des mineurs par créneau."""

from collections import defaultdict

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Assignment, Registration, Station, StationSlot


def validate_member_station(member, station, event_day):
    """L'habilitation d'officiel appartient au participant, jamais à l'acteur."""
    if station.category == Station.Category.OFFICIAL and not member.is_competition_official:
        raise ValidationError(
            "Ce poste est réservé aux membres habilités comme officiels de compétition "
            "par un administrateur. Licence et formation à jour nécessaires.",
            code="official_required",
        )
    if station.age_policy == Station.AgePolicy.ADULTS_ONLY and member.age_on(event_day.date) < 18:
        raise ValidationError(
            "Ce poste est réservé aux personnes ayant 18 ans révolus le jour de l'événement.",
            code="adult_required",
        )


def member_can_choose_station(member, station, event_day):
    try:
        validate_member_station(member, station, event_day)
    except ValidationError:
        return False
    return True


def validate_adult_coverage_for_confirmation(assignments):
    """Valide l'état final d'un lot, y compris un adulte confirmé avec un mineur.

    L'appelant verrouille les créneaux avant ce contrôle. Une affectation à la
    journée ne couvre pas implicitement un autre créneau matin ou après-midi.
    """
    assignments = list(assignments)
    restricted = {
        item.station_slot_id for item in assignments
        if item.station_slot.station.age_policy == Station.AgePolicy.AT_LEAST_ONE_ADULT
    }
    if not restricted:
        return
    proposed_ids = {item.pk for item in assignments}
    grouped = defaultdict(list)
    existing = Assignment.objects.filter(
        station_slot_id__in=restricted, status=Assignment.Status.CONFIRMED,
        registration__status=Registration.Status.ACTIVE,
    ).exclude(pk__in=proposed_ids).select_related(
        "registration__member", "registration__event_day", "station_slot__station"
    )
    for item in [*existing, *assignments]:
        if item.station_slot_id in restricted:
            grouped[item.station_slot_id].append(item)
    for group in grouped.values():
        if group and not any(
            item.registration.member.age_on(item.registration.event_day.date) >= 18
            for item in group
        ):
            name = group[0].station_slot.station.name
            raise ValidationError(
                f"Le poste « {name} » nécessite au moins un adulte de 18 ans "
                "confirmé sur ce même créneau. Sélectionnez aussi une affectation adulte.",
                code="adult_coverage_required",
            )


@transaction.atomic
def ensure_confirmed_adult_coverage(slot_ids):
    """Repasse en brouillon les confirmations privées du dernier adulte.

    L'historique de confirmation et les communications restent conservés.
    Aucun verrou d'inscription n'est acquis après celui d'un créneau.
    """
    slots = list(StationSlot.objects.select_for_update(of=("self",)).filter(
        pk__in=slot_ids, station__age_policy=Station.AgePolicy.AT_LEAST_ONE_ADULT
    ).order_by("pk"))
    if not slots:
        return []
    grouped = defaultdict(list)
    assignments = Assignment.objects.select_for_update(of=("self",)).filter(
        station_slot_id__in=[slot.pk for slot in slots], status=Assignment.Status.CONFIRMED,
        registration__status=Registration.Status.ACTIVE,
    ).select_related("registration__member", "registration__event_day").order_by("pk")
    for item in assignments:
        grouped[item.station_slot_id].append(item)
    invalidated = []
    for group in grouped.values():
        if not any(item.registration.member.age_on(item.registration.event_day.date) >= 18 for item in group):
            invalidated.extend(item.pk for item in group)
    if invalidated:
        Assignment.objects.filter(pk__in=invalidated).update(
            status=Assignment.Status.DRAFT, updated_at=timezone.now()
        )
    return invalidated
