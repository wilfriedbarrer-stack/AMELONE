"""Exonération de restauration selon le rôle effectivement tenu sur une journée."""

from django.utils import timezone

_UNSET = object()


def registration_has_official_meal_exemption(
    registration, *, member=None, preferred_station=_UNSET, station_slots=_UNSET,
):
    """Un officiel inscrit sur un poste bénévole paie comme un bénévole.

    Les affectations actives priment sur le souhait initial. À défaut
    d'affectation, le poste souhaité permet de tarifer dès l'inscription.
    """
    from .models import Assignment, Station

    member = member or registration.member
    if not member.is_competition_official:
        return False
    categories = (
        {slot.station.category for slot in station_slots}
        if station_slots is not _UNSET else
        set(Assignment.objects.filter(
            registration_id=registration.pk,
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
        ).values_list("station_slot__station__category", flat=True))
    )
    if categories:
        return Station.Category.VOLUNTEER not in categories
    if preferred_station is not _UNSET:
        return preferred_station is None or preferred_station.category != Station.Category.VOLUNTEER
    return not (
        registration.preferred_station_id
        and Station.objects.filter(
            pk=registration.preferred_station_id, category=Station.Category.VOLUNTEER,
        ).exists()
    )


def reconcile_official_meal_orders(orders, *, member=None):
    """Met à jour les snapshots appliqués sous les verrous pris par l'appelant.

    Les tarifs réservés sont conservés ; aucun paiement n'est modifié. Les
    commandes historiques et inscriptions annulées restent inchangées.
    """
    from .models import Event, MealOrder, Registration

    changed = []
    today = timezone.localdate()
    for order in orders:
        registration = order.registration
        if (
            registration.status != Registration.Status.ACTIVE
            or registration.event_day.date < today
            or registration.event_day.event.status == Event.Status.ARCHIVED
        ):
            continue
        exempt = registration_has_official_meal_exemption(registration, member=member)
        if order.official_exemption_applied == exempt:
            continue
        order.apply_official_pricing(exempt)
        order.updated_at = timezone.now()
        changed.append(order)
    if changed:
        MealOrder.objects.bulk_update(changed, (*MealOrder.pricing_update_fields(), "updated_at"))
    return changed
