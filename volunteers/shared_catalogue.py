"""Explicit, versioned changes to the catering catalogue shared by all events."""

from decimal import Decimal, InvalidOperation

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .catalogue_import import CATALOGUE_KINDS, _fingerprint
from .models import EventDay, Menu, User

SHARED_VERSION_SALT = "volunteers.shared-catalogue-version"


def authorize_catalogue_actor(actor):
    if not actor or not getattr(actor, "is_authenticated", False):
        raise PermissionDenied("Cette opération est réservée aux organisateurs.")
    current = User.objects.select_for_update(of=("self",), no_key=True).filter(
        pk=actor.pk, is_active=True,
    ).first()
    if not current or not (current.is_staff or current.is_organizer):
        raise PermissionDenied("Cette opération est réservée aux organisateurs actifs.")
    return current


def catalogue_kind_for_option(option):
    return "menus" if isinstance(option, Menu) else "boissons"


def shared_option_version(option, *, action="edit"):
    return signing.dumps({
        "id": str(option.pk),
        "kind": catalogue_kind_for_option(option),
        "source_day": str(option.event_day_id) if option.event_day_id else None,
        "fingerprint": _fingerprint(option),
        "action": action,
    }, salt=SHARED_VERSION_SALT)


def _check_version(option, version, *, action):
    try:
        data = signing.loads(version, salt=SHARED_VERSION_SALT, max_age=60 * 60)
    except (signing.BadSignature, TypeError) as error:
        raise ValidationError("Cette confirmation a expiré ou est invalide. Rechargez la page.") from error
    if (
        data.get("id") != str(option.pk)
        or data.get("kind") != catalogue_kind_for_option(option)
        or data.get("source_day") != (str(option.event_day_id) if option.event_day_id else None)
        or data.get("fingerprint") != _fingerprint(option)
        or data.get("action") != action
    ):
        raise ValidationError("Cette option a été modifiée depuis l'affichage de la page. Rechargez-la.")


def _check_global_conflict(option):
    query = type(option).objects.filter(event_day__isnull=True).exclude(pk=option.pk)
    if isinstance(option, Menu):
        query = query.filter(meal_type=option.meal_type)
    if any(name.casefold() == option.name.casefold() for name in query.values_list("name", flat=True)):
        raise ValidationError({"name": "Une option commune de ce nom et de ce type existe déjà. Modifiez-la directement ou choisissez un autre nom."})


def _model_for_kind(kind):
    try:
        return CATALOGUE_KINDS[kind]
    except KeyError as error:
        raise ValidationError("Choisissez un menu ou une boisson.") from error


@transaction.atomic
def save_shared_option(*, kind, data, actor, option_id=None, version=None):
    authorize_catalogue_actor(actor)
    model = _model_for_kind(kind)
    if option_id:
        option = model.objects.select_for_update().get(pk=option_id, event_day__isnull=True)
        _check_version(option, version, action="edit")
    else:
        option = model(event_day=None)
    option.name = data["name"].strip()
    option.description = data.get("description", "").strip()
    option.is_active = bool(data.get("is_active"))
    try:
        amount = Decimal(data["surcharge_euros"])
        if not amount.is_finite() or amount < 0 or amount * 100 != (amount * 100).to_integral_value():
            raise ValidationError("Le prix doit être positif et comporter au plus deux décimales.")
        option.surcharge_cents = int(amount * 100)
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValidationError("Le prix est invalide.") from error
    if isinstance(option, Menu):
        option.meal_type = data["meal_type"]
    _check_global_conflict(option)
    option.full_clean()
    option.save()
    return option


@transaction.atomic
def promote_local_option(*, kind, option_id, version, actor):
    authorize_catalogue_actor(actor)
    model = _model_for_kind(kind)
    source_day_id = model.objects.filter(pk=option_id).values_list("event_day_id", flat=True).first()
    if source_day_id is None:
        raise ValidationError("Cette option n'est plus propre à un événement. Rechargez la page.")
    # Event deletion locks its days before collecting related option IDs. Keep
    # the same order so its collector cannot later delete a promoted global ID.
    try:
        EventDay.objects.select_for_update(of=("self",)).get(pk=source_day_id)
        option = model.objects.select_for_update(of=("self",)).get(pk=option_id)
    except (EventDay.DoesNotExist, model.DoesNotExist) as error:
        raise ValidationError("Cette option ou son événement a été supprimé. Rechargez la page.") from error
    if option.event_day_id != source_day_id:
        raise ValidationError("Cette option a changé d'événement. Rechargez la page.")
    _check_version(option, version, action="promote")
    option.event_day = None
    _check_global_conflict(option)
    option.full_clean()
    # Preserve its primary key, order references, prices and original activity.
    option.save(update_fields=("event_day", "updated_at"))
    return option
