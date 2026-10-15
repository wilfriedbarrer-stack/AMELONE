"""Restaurer les tarifs réservés aux officiels inscrits comme bénévoles."""

from django.db import migrations
from django.utils import timezone

COMPONENTS = ("menu", "beverage", "morning_snack", "afternoon_snack")


def restore_volunteer_prices(apps, schema_editor):
    database = schema_editor.connection.alias
    Member = apps.get_model("volunteers", "Member")
    EventDay = apps.get_model("volunteers", "EventDay")
    Registration = apps.get_model("volunteers", "Registration")
    Assignment = apps.get_model("volunteers", "Assignment")
    MealOrder = apps.get_model("volunteers", "MealOrder")
    coordinates = list(Registration.objects.using(database).filter(
        status="active", event_day__date__gte=timezone.localdate(),
        member__is_competition_official=True, meal_order__official_exemption_applied=True,
    ).exclude(event_day__event__status="archived").values_list("pk", "member_id", "event_day_id"))
    list(Member.objects.using(database).select_for_update(of=("self",)).filter(
        pk__in={item[1] for item in coordinates}
    ).order_by("pk"))
    list(EventDay.objects.using(database).select_for_update(of=("self",)).filter(
        pk__in={item[2] for item in coordinates}
    ).order_by("pk"))
    registrations = {registration.pk: registration for registration in
                     Registration.objects.using(database).select_for_update(of=("self",)).filter(
                         pk__in={item[0] for item in coordinates}, status="active",
                         member__is_competition_official=True,
                         event_day__date__gte=timezone.localdate(),
                     ).exclude(event_day__event__status="archived")
                     .select_related("preferred_station").order_by("pk")}
    orders = list(MealOrder.objects.using(database).select_for_update(of=("self",)).filter(
        registration_id__in=registrations, official_exemption_applied=True,
    ).order_by("pk"))
    changed = []
    for order in orders:
        registration = registrations[order.registration_id]
        categories = set(Assignment.objects.using(database).filter(
            registration_id=registration.pk, status__in=("draft", "confirmed"),
        ).values_list("station_slot__station__category", flat=True))
        volunteer = (
            "volunteer" in categories if categories else
            bool(registration.preferred_station_id and registration.preferred_station.category == "volunteer")
        )
        if not volunteer:
            continue
        for component in COMPONENTS:
            standard_field = f"{component}_standard_surcharge_cents"
            applied_field = f"{component}_surcharge_cents"
            standard = getattr(order, standard_field)
            if standard is None:
                standard = getattr(order, applied_field)
                setattr(order, standard_field, standard)
            setattr(order, applied_field, standard)
        order.total_surcharge_cents = sum(getattr(order, f"{field}_surcharge_cents") for field in COMPONENTS)
        order.official_exemption_applied = False
        order.updated_at = timezone.now()
        changed.append(order)
    if changed:
        fields = [field for component in COMPONENTS for field in (
            f"{component}_standard_surcharge_cents", f"{component}_surcharge_cents",
        )]
        MealOrder.objects.using(database).bulk_update(
            changed, [*fields, "total_surcharge_cents", "official_exemption_applied", "updated_at"],
        )


class Migration(migrations.Migration):
    dependencies = [("volunteers", "0009_share_existing_beverages")]

    operations = [migrations.RunPython(restore_volunteer_prices, migrations.RunPython.noop)]
