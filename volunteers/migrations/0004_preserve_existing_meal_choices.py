from django.db import migrations


def preserve_choices(apps, schema_editor):
    MealOrder = apps.get_model("volunteers", "MealOrder")
    alias = schema_editor.connection.alias
    MealOrder.objects.using(alias).update(legacy_availability="")
    orders = MealOrder.objects.using(alias).filter(
        wants_meal=True,
        registration__availability__in=("morning", "afternoon"),
        menu__meal_type="full_meal",
    ).values_list("pk", "registration__availability")
    for order_id, availability in orders.iterator():
        MealOrder.objects.using(alias).filter(pk=order_id).update(legacy_availability=availability)


def unmark_choices(apps, schema_editor):
    MealOrder = apps.get_model("volunteers", "MealOrder")
    MealOrder.objects.using(schema_editor.connection.alias).update(legacy_availability="")


class Migration(migrations.Migration):
    dependencies = [("volunteers", "0003_eventprofile_mealorder_legacy_availability_and_more")]
    operations = [migrations.RunPython(preserve_choices, unmark_choices)]
