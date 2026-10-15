"""Rendre les boissons existantes communes sans réécrire leur historique."""

from collections import defaultdict
from copy import deepcopy

from django.db import OperationalError, migrations, transaction
from django.utils import timezone


def _normalized_name(value):
    return value.strip().casefold() if isinstance(value, str) else None


def _commercial_key(value):
    if (
        not isinstance(value, dict)
        or not _normalized_name(value.get("name"))
        or not isinstance(value.get("description"), str)
        or type(value.get("surcharge_cents")) is not int
    ):
        return None
    return (
        _normalized_name(value["name"]),
        value["description"],
        value["surcharge_cents"],
    )


def _without_common_beverages(snapshot, common_keys):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("days"), list):
        return snapshot, False
    updated = deepcopy(snapshot)
    changed = False
    for day in updated["days"]:
        if not isinstance(day, dict) or not isinstance(day.get("beverages"), list):
            continue
        kept = [entry for entry in day["beverages"] if _commercial_key(entry) not in common_keys]
        if len(kept) != len(day["beverages"]):
            day["beverages"] = kept
            changed = True
    return updated, changed


def share_existing_beverages(apps, schema_editor):
    Beverage = apps.get_model("volunteers", "Beverage")
    EventDay = apps.get_model("volunteers", "EventDay")
    EventProfile = apps.get_model("volunteers", "EventProfile")
    connection = schema_editor.connection
    database = connection.alias
    try:
        with transaction.atomic(using=database):
            # Un clone tient son profil avant de créer journées et boissons.
            # Prendre les profils d'abord empêche un cycle Profile/Beverage.
            profiles = list(EventProfile.objects.using(database).select_for_update(
                of=("self",)
            ).order_by("pk"))
            list(EventDay.objects.using(database).select_for_update(
                of=("self",)
            ).order_by("pk"))
            if connection.vendor == "postgresql":
                # Empêche aussi les nouveaux noms communs d'apparaître entre
                # validation et promotion. Ne jamais attendre cette table en
                # tenant Day : un INSERT admin pourrait attendre sa FK Day.
                table = schema_editor.quote_name(Beverage._meta.db_table)
                with connection.cursor() as cursor:
                    cursor.execute(f"LOCK TABLE {table} IN EXCLUSIVE MODE NOWAIT")
            beverages = list(Beverage.objects.using(database).select_for_update(
                of=("self",)
            ).order_by("pk"))
            groups = defaultdict(list)
            for beverage in beverages:
                name = _normalized_name(beverage.name)
                if not name:
                    raise RuntimeError("Une boisson sans nom empêche la mise en commun du catalogue.")
                groups[name].append(beverage)

            # Vérifier tous les groupes avant la première écriture : aucun
            # prix ou descriptif ne doit être choisi arbitrairement.
            for group in groups.values():
                if sum(item.event_day_id is None for item in group) > 1:
                    raise RuntimeError(
                        f"Plusieurs boissons communes portent le nom « {group[0].name} ». "
                        "Harmonisez ces doublons avant de relancer la migration."
                    )
                variants = {(item.description, item.surcharge_cents) for item in group}
                if len(variants) > 1:
                    raise RuntimeError(
                        f"La boisson « {group[0].name} » possède plusieurs prix ou descriptions. "
                        "Harmonisez ces variantes avant de relancer la migration."
                    )

            promote_ids, duplicate_ids, common_keys = [], [], set()
            for name, group in groups.items():
                globals_existing = [item for item in group if item.event_day_id is None]
                # Les options déjà communes gardent intégralement leur état.
                # À défaut, privilégier une offre locale active ; un groupe
                # entièrement inactif reste inactif après sa promotion.
                representative = next(iter(globals_existing), None)
                if representative is None:
                    representative = min(group, key=lambda item: (not item.is_active, str(item.pk)))
                    promote_ids.append(representative.pk)
                duplicate_ids.extend(
                    item.pk for item in group
                    if item.event_day_id is not None and item.pk != representative.pk
                )
                common_keys.add((name, representative.description, representative.surcharge_cents))

            now = timezone.now()
            promotions = Beverage.objects.using(database).filter(pk__in=promote_ids).update(
                event_day=None, updated_at=now,
            )
            deactivations = Beverage.objects.using(database).filter(
                pk__in=duplicate_ids, is_active=True,
            ).update(is_active=False, updated_at=now)
            cleaned_profiles = 0
            for profile in profiles:
                snapshot, changed = _without_common_beverages(profile.snapshot, common_keys)
                if changed:
                    EventProfile.objects.using(database).filter(pk=profile.pk).update(
                        snapshot=snapshot, updated_at=now,
                    )
                    cleaned_profiles += 1
            # Aucun MealOrder, paiement, identifiant ou tarif n'est réécrit.
            return {"promoted": promotions, "deactivated": deactivations, "profiles_cleaned": cleaned_profiles}
    except OperationalError as error:
        if getattr(error.__cause__, "sqlstate", None) == "55P03":
            raise RuntimeError(
                "Le catalogue est en cours de modification. Relancez la migration après cette opération."
            ) from error
        raise


class Migration(migrations.Migration):
    dependencies = [
        ("volunteers", "0008_shared_catalogue_and_extra_snacks"),
    ]

    operations = [
        migrations.RunPython(share_existing_beverages, migrations.RunPython.noop),
    ]
