"""Une présence et son choix de restauration sont enregistrés ensemble."""
from django.db import transaction

from .models import MealOrder, Menu, Registration
from .services import BusinessRuleError, save_meal_order, save_registration


@transaction.atomic
def save_participation(*, member, event_day, availability, preferred_station=None, note="", actor,
                       meal_choices=None):
    registration = save_registration(
        member=member, event_day=event_day, availability=availability,
        preferred_station=preferred_station, note=note, actor=actor, actor_is_organizer=False,
    )
    if meal_choices is not None:
        save_meal_order(registration=registration, actor=actor, actor_is_organizer=False, **meal_choices)
    else:
        # Une fermeture des repas ne doit pas transformer un repas en collation silencieusement.
        order = MealOrder.objects.filter(registration=registration, wants_meal=True).select_related("menu").first()
        expected = Menu.MealType.FULL_MEAL if availability == Registration.Availability.FULL_DAY else Menu.MealType.SNACK
        if order and order.menu.meal_type != expected and order.legacy_choice_availability != availability:
            raise BusinessRuleError("Les repas sont clôturés. Contactez l’organisation pour modifier la présence et le repas ensemble.")
    return registration
