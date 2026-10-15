"""Une présence et son choix de restauration sont enregistrés ensemble."""
from django.db import transaction

from .models import MealOrder, Member, Menu, Registration
from .services import BusinessRuleError, save_meal_order, save_registration


@transaction.atomic
def save_participation(*, member, event_day, availability, preferred_station=None, note="", actor,
                       meal_choices=None):
    has_locked_snacks = False
    if meal_choices is None and availability != Registration.Availability.FULL_DAY:
        # Le verrou membre suit l'ordre du service d'inscription et empêche une
        # commande concurrente de modifier les choix avant leur vérification.
        Member.objects.select_for_update(of=("self",)).get(pk=member.pk)
        current_order = MealOrder.objects.filter(
            registration__member=member, registration__event_day=event_day, wants_meal=True,
        ).only("morning_snack_id", "afternoon_snack_id").first()
        has_locked_snacks = bool(current_order and (
            current_order.morning_snack_id or current_order.afternoon_snack_id
        ))
    registration = save_registration(
        member=member, event_day=event_day, availability=availability,
        preferred_station=preferred_station, note=note, actor=actor, actor_is_organizer=False,
    )
    if has_locked_snacks:
        # Lever après le contrôle d'accès de save_registration et annuler toute
        # modification de présence/tarif dans la transaction courante.
        raise BusinessRuleError(
            "Les repas sont clôturés. Contactez l’organisation pour modifier la présence et les collations ensemble."
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
