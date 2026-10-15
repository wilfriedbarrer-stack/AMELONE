"""Édition indépendante des configurations réutilisables d'événements.

Les postes et créneaux restent dans le JSON du modèle. Aucune modification
n'est répercutée sur un événement déjà créé à partir de cette configuration.
"""

from collections.abc import Mapping
from copy import deepcopy
from datetime import time

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .event_profiles import _STATION_FIELDS
from .models import EventProfile, Station, StationSlot


def _text(value, *, field, maximum=None, strip=False):
    if not isinstance(value, str):
        raise ValidationError({field: "Renseignez un texte valide."})
    value = value.strip() if strip else value
    if maximum is not None and len(value) > maximum:
        raise ValidationError({field: f"Ce champ est limité à {maximum} caractères."})
    return value


def _metadata(*, name, description, event_description, location, general_instructions):
    return {
        "name": _text(name, field="name", maximum=200, strip=True),
        "description": _text(description, field="description"),
        "event_description": _text(event_description, field="event_description"),
        "location": _text(location, field="location", maximum=255, strip=True),
        "general_instructions": _text(general_instructions, field="general_instructions"),
    }


def _beverage_settings(morning_hot_beverage_enabled, afternoon_cold_beverage_enabled, *, previous=None):
    settings = {}
    previous = previous or {}
    for name, value in (
        ("morning_hot_beverage_enabled", morning_hot_beverage_enabled),
        ("afternoon_cold_beverage_enabled", afternoon_cold_beverage_enabled),
    ):
        value = previous.get(name, True) if value is None else value
        if type(value) is not bool:
            raise ValidationError({name: "Renseignez un réglage d’activation valide."})
        settings[name] = value
    return settings


def _snapshot(profile):
    snapshot = deepcopy(profile.snapshot)
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("version")) is not int
        or snapshot["version"] != 1
        or not isinstance(snapshot.get("event"), dict)
        or not isinstance(snapshot.get("windows"), dict)
        or not isinstance(snapshot.get("days"), list)
        or not isinstance(snapshot.get("stations", []), list)
    ):
        raise ValidationError("Ce modèle ne contient pas une configuration utilisable.")
    offsets = set()
    for day in snapshot["days"]:
        if (
            not isinstance(day, dict)
            or type(day.get("offset")) is not int
            or day["offset"] < 0
            or day["offset"] in offsets
        ):
            raise ValidationError("Les journées enregistrées dans ce modèle sont invalides.")
        offsets.add(day["offset"])
    if 0 not in offsets:
        raise ValidationError("Le modèle doit contenir sa première journée.")
    snapshot.setdefault("stations", [])
    return snapshot


def _locked_profile(profile, expected_updated_at):
    try:
        current = EventProfile.objects.select_for_update(of=("self",)).get(pk=profile.pk)
    except EventProfile.DoesNotExist as error:
        raise ValidationError("Ce modèle d'événement a été supprimé. Rechargez la liste.") from error
    try:
        expected = parse_datetime(expected_updated_at) if isinstance(expected_updated_at, str) else None
    except ValueError:
        expected = None
    if expected is None or timezone.is_naive(expected) or expected != current.updated_at:
        raise ValidationError(
            "Ce modèle a été modifié depuis l'ouverture du formulaire. "
            "Rechargez la page avant de réessayer."
        )
    return current


def _save(profile):
    profile.full_clean()
    try:
        with transaction.atomic():
            profile.save()
    except IntegrityError as error:
        raise ValidationError({"name": "Un modèle porte déjà ce nom."}) from error
    return profile


@transaction.atomic
def create_reusable_profile(*, name, description="", event_description="", location="",
                            general_instructions="", morning_hot_beverage_enabled=True,
                            afternoon_cold_beverage_enabled=True):
    """Crée un modèle d'une journée, sans événement daté ni données familiales."""

    data = _metadata(
        name=name, description=description, event_description=event_description,
        location=location, general_instructions=general_instructions,
    )
    snapshot = {
        "version": 1,
        "event_name": data["name"],
        "event": {
            "description": data["event_description"],
            "location": data["location"],
            "general_instructions": data["general_instructions"],
            "sumup_payment_url": "",
            **_beverage_settings(morning_hot_beverage_enabled, afternoon_cold_beverage_enabled),
        },
        "windows": {
            "registration_opens_at": -30 * 86400 + 9 * 3600,
            "registration_closes_at": -86400 + 18 * 3600,
            "meal_orders_close_at": -86400 + 12 * 3600,
        },
        "days": [{"offset": 0, "menus": [], "beverages": []}],
        "stations": [],
    }
    return _save(EventProfile(name=data["name"], description=data["description"], snapshot=snapshot))


@transaction.atomic
def update_reusable_profile(*, profile, expected_updated_at, name, description,
                            event_description, location, general_instructions,
                            morning_hot_beverage_enabled=None, afternoon_cold_beverage_enabled=None):
    """Modifie les métadonnées sans remplacer jours, échéances ou catalogues."""

    current = _locked_profile(profile, expected_updated_at)
    snapshot = _snapshot(current)
    data = _metadata(
        name=name, description=description, event_description=event_description,
        location=location, general_instructions=general_instructions,
    )
    current.name = data["name"]
    current.description = data["description"]
    snapshot["event"].update(
        description=data["event_description"], location=data["location"],
        general_instructions=data["general_instructions"],
        **_beverage_settings(morning_hot_beverage_enabled, afternoon_cold_beverage_enabled,
                             previous=snapshot["event"]),
    )
    current.snapshot = snapshot
    return _save(current)


def _index(index, stations):
    if type(index) is not int or not 0 <= index < len(stations):
        raise ValidationError("Ce poste n'existe plus dans le modèle. Rechargez la page.")
    return index


def _station_data(data):
    if not isinstance(data, Mapping) or set(data) - set(_STATION_FIELDS):
        raise ValidationError("La configuration du poste est invalide.")
    station = Station()
    for field in _STATION_FIELDS:
        if field in data:
            setattr(station, field, data[field])
    station.name = _text(station.name, field="name", maximum=160, strip=True)
    station.description = _text(station.description, field="description")
    station.instructions = _text(station.instructions, field="instructions")
    if type(station.is_active) is not bool:
        raise ValidationError({"is_active": "Indiquez si le poste est actif."})
    if type(station.position) is not int or not 0 <= station.position <= 32767:
        raise ValidationError({"position": "L'ordre d'affichage doit être compris entre 0 et 32767."})
    # Valider les champs du vrai modèle sans créer un Event artificiel.
    station.full_clean(exclude=("event",), validate_unique=False, validate_constraints=False)
    return {field: getattr(station, field) for field in _STATION_FIELDS}


def _time(value, field):
    if value in (None, ""):
        return None
    try:
        parsed = time.fromisoformat(value) if isinstance(value, str) else value
    except ValueError as error:
        raise ValidationError({field: "Renseignez une heure valide."}) from error
    if not isinstance(parsed, time) or parsed.tzinfo is not None:
        raise ValidationError({field: "Renseignez une heure locale valide."})
    return parsed


def _slots(slots, day_offsets):
    if not isinstance(slots, (list, tuple)) or len(slots) > 1000:
        raise ValidationError("La liste des créneaux est invalide ou trop longue.")
    normalized, seen = [], set()
    for data in slots:
        if not isinstance(data, Mapping) or set(data) - {
            "day_offset", "period", "capacity", "start_time", "end_time",
        }:
            raise ValidationError("La configuration d'un créneau est invalide.")
        offset = data.get("day_offset")
        period = data.get("period")
        capacity = data.get("capacity")
        if type(offset) is not int or offset not in day_offsets:
            raise ValidationError("Ce créneau ne correspond à aucune journée du modèle.")
        if not isinstance(period, str) or period not in StationSlot.Period.values:
            raise ValidationError({"period": "Choisissez une période valide."})
        if (offset, period) in seen:
            raise ValidationError("Un seul créneau par poste, journée et période est autorisé.")
        if type(capacity) is not int or not 0 <= capacity <= 32767:
            raise ValidationError({"capacity": "Le nombre de personnes doit être compris entre 0 et 32767."})
        slot = StationSlot(
            period=period, capacity=capacity,
            start_time=_time(data.get("start_time"), "start_time"),
            end_time=_time(data.get("end_time"), "end_time"),
        )
        slot.full_clean(
            exclude=("station", "event_day"), validate_unique=False, validate_constraints=False,
        )
        normalized.append({
            "day_offset": offset, "period": slot.period, "capacity": slot.capacity,
            "start_time": slot.start_time.isoformat() if slot.start_time else None,
            "end_time": slot.end_time.isoformat() if slot.end_time else None,
        })
        seen.add((offset, period))
    return normalized


@transaction.atomic
def save_profile_station(*, profile, expected_updated_at, station_data, slots, station_index=None):
    """Ajoute ou remplace un poste dans le modèle, indépendamment des événements."""

    current = _locked_profile(profile, expected_updated_at)
    snapshot = _snapshot(current)
    if station_index is not None:
        _index(station_index, snapshot["stations"])
    data = _station_data(station_data)
    for index, other in enumerate(snapshot["stations"]):
        if index != station_index and isinstance(other, dict) and (
            str(other.get("name", "")).strip().casefold() == data["name"].casefold()
        ):
            raise ValidationError({"name": "Un poste porte déjà ce nom dans le modèle."})
    data["slots"] = _slots(slots, {day["offset"] for day in snapshot["days"]})
    if station_index is None:
        snapshot["stations"].append(data)
    else:
        snapshot["stations"][station_index] = data
    current.snapshot = snapshot
    return _save(current)


@transaction.atomic
def delete_profile_station(*, profile, expected_updated_at, station_index):
    """Retire un poste du modèle sans toucher à ses copies déjà utilisées."""

    current = _locked_profile(profile, expected_updated_at)
    snapshot = _snapshot(current)
    del snapshot["stations"][_index(station_index, snapshot["stations"])]
    current.snapshot = snapshot
    return _save(current)
