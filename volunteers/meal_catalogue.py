"""Options communes à tous les événements et anciens choix propres à un jour."""

from django.db.models import Q

from .models import Beverage, Menu


def _options_for_day(model, event_day, *, active_only):
    day_id = getattr(event_day, "pk", event_day)
    options = model.objects.filter(Q(event_day_id=day_id) | Q(event_day__isnull=True))
    return options.filter(is_active=True) if active_only else options


def menu_options_for_day(event_day, *, active_only=True):
    return _options_for_day(Menu, event_day, active_only=active_only)


def beverage_options_for_day(event_day, *, active_only=True):
    return _options_for_day(Beverage, event_day, active_only=active_only)
