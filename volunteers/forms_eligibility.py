"""Choix de postes et mentions d’habilitation partagés entre les formulaires."""

from .eligibility import member_can_choose_station
from .models import Station

OFFICIAL_NOTICE = "Licence et formation à jour nécessaires."


def eligible_stations(queryset, member, event_day):
    if member is None:
        return queryset.exclude(category=Station.Category.OFFICIAL).exclude(
            age_policy=Station.AgePolicy.ADULTS_ONLY,
        )
    allowed_ids = [station.pk for station in queryset
                   if member_can_choose_station(member, station, event_day)]
    return queryset.filter(pk__in=allowed_ids)


def eligible_slots(queryset, member, event_day=None):
    allowed_ids = [slot.pk for slot in queryset if member is not None and
                   member_can_choose_station(member, slot.station, event_day or slot.event_day)]
    return queryset.filter(pk__in=allowed_ids)


def station_requirements_label(station):
    notes = []
    if station.age_policy != Station.AgePolicy.NONE:
        notes.append(station.get_age_policy_display())
    if station.category == Station.Category.OFFICIAL:
        notes.append(OFFICIAL_NOTICE)
    return " · ".join(notes)


def station_choice_label(station):
    requirements = station_requirements_label(station)
    return station.name + (f" — {requirements}" if requirements else "")


def slot_choice_label(slot):
    requirements = station_requirements_label(slot.station)
    return str(slot) + (f" — {requirements}" if requirements else "")
