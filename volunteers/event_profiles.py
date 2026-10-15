"""Profils d'événement : configuration indépendante des participants.

Les dates sont conservées relativement au premier jour, en heure locale.
Réutiliser un profil décale ainsi les échéances et toutes les dates ensemble,
sans copier les inscriptions, affectations, repas commandés ou paiements.
"""

from datetime import datetime, time, timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import Beverage, Event, EventDay, EventProfile, Menu, Station, StationSlot

_WINDOW_FIELDS = (
    "registration_opens_at",
    "registration_closes_at",
    "meal_orders_close_at",
)
_EVENT_FIELDS = (
    "description",
    "location",
    "general_instructions",
    "sumup_payment_url",
)
_STATION_FIELDS = ("name", "description", "instructions", "category", "age_policy", "is_active", "position")
_CATERING_FIELDS = ("name", "description", "surcharge_cents", "is_active")


def build_event_snapshot(event):
    """Capture uniquement la configuration métier de l'événement."""

    days = list(event.days.order_by("date").prefetch_related("menus", "beverages"))
    if not days:
        raise ValidationError("Ajoutez une date à l'événement avant d'enregistrer son profil.")
    anchor = datetime.combine(days[0].date, time.min)
    default_timezone = timezone.get_default_timezone()
    windows = {
        field: (
            timezone.localtime(getattr(event, field), default_timezone).replace(tzinfo=None)
            - anchor
        ).total_seconds()
        for field in _WINDOW_FIELDS
    }
    day_offsets = {day.pk: (day.date - days[0].date).days for day in days}
    snapshot = {
        "version": 1,
        "event": {field: getattr(event, field) for field in _EVENT_FIELDS},
        "event_name": event.name,
        "windows": windows,
        "days": [],
        "stations": [],
    }
    for day in days:
        snapshot["days"].append(
            {
                "offset": day_offsets[day.pk],
                "menus": [
                    {
                        **{field: getattr(menu, field) for field in _CATERING_FIELDS},
                        "meal_type": menu.meal_type,
                    }
                    for menu in day.menus.all()
                ],
                "beverages": [
                    {field: getattr(drink, field) for field in _CATERING_FIELDS}
                    for drink in day.beverages.all()
                ],
            }
        )
    for station in event.stations.prefetch_related("slots").order_by("position", "name"):
        snapshot["stations"].append(
            {
                **{field: getattr(station, field) for field in _STATION_FIELDS},
                "slots": [
                    {
                        "day_offset": day_offsets[slot.event_day_id],
                        "period": slot.period,
                        "capacity": slot.capacity,
                        "start_time": slot.start_time.isoformat() if slot.start_time else None,
                        "end_time": slot.end_time.isoformat() if slot.end_time else None,
                    }
                    for slot in station.slots.all()
                ],
            }
        )
    return snapshot


@transaction.atomic
def save_event_profile(*, event, name, description=""):
    """Enregistre un profil nommé à partir de l'état courant d'un événement."""

    event = Event.objects.select_for_update().get(pk=event.pk)
    profile = EventProfile(
        name=name.strip(), description=description, snapshot=build_event_snapshot(event)
    )
    profile.full_clean()
    profile.save()
    return profile


def _profile_configuration(profile, event_date):
    snapshot = profile.snapshot
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1 or not snapshot.get("days"):
        raise ValidationError("Ce profil ne contient pas une configuration utilisable.")
    try:
        event_fields = {field: snapshot["event"].get(field, "") for field in _EVENT_FIELDS}
        anchor = datetime.combine(event_date, time.min)
        windows = {
            field: timezone.make_aware(
                anchor + timedelta(seconds=snapshot["windows"][field]),
                timezone.get_default_timezone(),
            )
            for field in _WINDOW_FIELDS
        }
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Les dates enregistrées dans ce profil sont invalides.") from error
    return snapshot, event_fields, windows


@transaction.atomic
def create_event_from_profile(*, profile, name, event_date):
    """Crée un nouveau brouillon et ses postes/menus, sans données de familles."""

    snapshot, event_fields, windows = _profile_configuration(profile, event_date)
    event = Event(name=name.strip(), status=Event.Status.DRAFT, **event_fields, **windows)
    event.full_clean()
    event.save()
    try:
        day_map = {}
        for day_config in snapshot["days"]:
            offset = day_config["offset"]
            if not isinstance(offset, int) or offset < 0 or offset in day_map:
                raise ValueError("Invalid day offset")
            day = EventDay(event=event, date=event_date + timedelta(days=offset))
            day.full_clean()
            day.save()
            day_map[offset] = day
            for menu_data in day_config.get("menus", []):
                menu = Menu(
                    event_day=day,
                    **{field: menu_data[field] for field in _CATERING_FIELDS},
                    meal_type=menu_data.get("meal_type", Menu.MealType.FULL_MEAL),
                )
                menu.full_clean()
                menu.save()
            for drink_data in day_config.get("beverages", []):
                drink = Beverage(
                    event_day=day, **{field: drink_data[field] for field in _CATERING_FIELDS}
                )
                drink.full_clean()
                drink.save()
        if 0 not in day_map:
            raise ValueError("Missing first event day")
        for station_config in snapshot.get("stations", []):
            station = Station(
                event=event, **{field: station_config.get(field, Station.AgePolicy.NONE)
                               if field == "age_policy" else station_config[field]
                               for field in _STATION_FIELDS}
            )
            station.full_clean()
            station.save()
            for slot_config in station_config.get("slots", []):
                slot = StationSlot(
                    station=station,
                    event_day=day_map[slot_config["day_offset"]],
                    period=slot_config["period"],
                    capacity=slot_config["capacity"],
                    start_time=time.fromisoformat(slot_config["start_time"])
                    if slot_config.get("start_time")
                    else None,
                    end_time=time.fromisoformat(slot_config["end_time"])
                    if slot_config.get("end_time")
                    else None,
                )
                slot.full_clean()
                slot.save()
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise ValidationError("Le profil contient une configuration invalide.") from error
    return event
