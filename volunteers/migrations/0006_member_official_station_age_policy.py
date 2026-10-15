from django.db import migrations, models
from django.utils import timezone


def preserve_official_permissions(apps, schema_editor):
    Member = apps.get_model("volunteers", "Member")
    Assignment = apps.get_model("volunteers", "Assignment")
    database = schema_editor.connection.alias
    Member.objects.using(database).filter(user__is_competition_official=True).update(
        is_competition_official=True
    )
    # L'ancien rôle du compte ne doit jamais habiliter toute sa famille.
    # Les confirmations incompatibles restent en historique privé, à revoir.
    Assignment.objects.using(database).filter(
        status="confirmed",
        station_slot__station__category="official",
        registration__event_day__date__gte=timezone.localdate(),
        registration__member__is_competition_official=False,
    ).update(status="draft")


class Migration(migrations.Migration):
    dependencies = [
        ("volunteers", "0005_competition_official_personal_member"),
    ]

    operations = [
        migrations.AddField(
            model_name="member",
            name="is_competition_official",
            field=models.BooleanField(
                db_default=False, default=False, verbose_name="officiel de compétition"
            ),
        ),
        migrations.AddField(
            model_name="station",
            name="age_policy",
            field=models.CharField(
                choices=[
                    ("none", "Aucune condition d'âge"),
                    ("at_least_one_adult", "Au moins un adulte (18 ans ou plus)"),
                    ("adults_only", "Uniquement des adultes (18 ans ou plus)"),
                ],
                db_default="none", default="none", max_length=24, verbose_name="règle d'âge",
            ),
        ),
        migrations.RunPython(preserve_official_permissions, migrations.RunPython.noop),
    ]
