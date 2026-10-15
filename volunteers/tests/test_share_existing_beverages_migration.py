from copy import deepcopy
from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID

from django.db import OperationalError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase

from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.forms_family import FamilyMealForm
from volunteers.meal_catalogue import beverage_options_for_day
from volunteers.models import (
    Beverage,
    Event,
    EventDay,
    EventProfile,
    MealOrder,
    Menu,
    Payment,
    Registration,
)
from volunteers.services import save_meal_order

from .factories import DataMixin


class ShareExistingBeveragesMigrationTests(DataMixin, TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.migration = import_module("volunteers.migrations.0009_share_existing_beverages")
        # Exécuter avec les modèles historiques de 0008, comme le vrai migrate.
        cls.historical_apps = MigrationExecutor(connection).loader.project_state([
            ("volunteers", "0008_shared_catalogue_and_extra_snacks"),
        ]).apps

    def migrate(self, schema_editor=None):
        return self.migration.share_existing_beverages(
            self.historical_apps,
            schema_editor or SimpleNamespace(connection=connection, quote_name=connection.ops.quote_name),
        )

    def production_shape(self):
        days = [self.day] + [
            EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=index))
            for index in range(1, 7)
        ]
        for index in range(6):
            for day in days:
                Beverage.objects.create(
                    event_day=day, name=f"Boisson répétée {index}",
                    description=f"Description {index}", surcharge_cents=100 * index,
                )
        existing_global = Beverage.objects.create(name="BIERE", surcharge_cents=200)
        for index in range(8):
            Beverage.objects.create(event_day=self.day, name=f"Boisson unique {index}", surcharge_cents=50 * index)
        return existing_global

    def test_production_shape_promotes_fourteen_and_deactivates_thirty_six_without_deletion(self):
        existing_global = self.production_shape()
        before = {row["id"]: row for row in Beverage.objects.values()}
        self.assertEqual(len(before), 51)
        result = self.migrate()
        self.assertEqual(result, {"promoted": 14, "deactivated": 36, "profiles_cleaned": 0})
        after = {row["id"]: row for row in Beverage.objects.values()}
        self.assertEqual(set(before), set(after))
        self.assertEqual(Beverage.objects.filter(event_day__isnull=True).count(), 15)
        self.assertEqual(Beverage.objects.filter(event_day__isnull=False, is_active=False).count(), 36)
        self.assertFalse(Beverage.objects.filter(event_day__isnull=False, is_active=True).exists())
        self.assertEqual(before[existing_global.pk], after[existing_global.pk])
        for key, original in before.items():
            self.assertEqual(after[key]["name"], original["name"])
            self.assertEqual(after[key]["description"], original["description"])
            self.assertEqual(after[key]["surcharge_cents"], original["surcharge_cents"])
            if key != existing_global.pk:
                self.assertGreater(after[key]["updated_at"], original["updated_at"])

    def test_all_order_fields_snapshots_and_payments_remain_exactly_unchanged(self):
        self.production_shape()
        beverage = Beverage.objects.filter(name="Boisson répétée 1").order_by("-pk").first()
        menu = Menu.objects.create(event_day=beverage.event_day, name="Menu", surcharge_cents=500)
        registration = Registration.objects.create(
            event_day=beverage.event_day, member=self.member, availability=Registration.Availability.FULL_DAY,
        )
        order = save_meal_order(registration=registration, menu=menu, beverage=beverage)
        payment = Payment.objects.create(family=self.family, event=self.event, amount_cents=600, method=Payment.Method.SUMUP)
        order_before = MealOrder.objects.filter(pk=order.pk).values().get()
        payment_before = Payment.objects.filter(pk=payment.pk).values().get()
        events_before = list(Event.objects.values().order_by("pk"))
        self.migrate()
        self.assertEqual(MealOrder.objects.filter(pk=order.pk).values().get(), order_before)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), payment_before)
        self.assertEqual(list(Event.objects.values().order_by("pk")), events_before)
        self.assertEqual(order_before["beverage_id"], beverage.pk)
        beverage.refresh_from_db()
        order.refresh_from_db()
        self.assertFalse(beverage.is_active)
        self.assertIsNotNone(beverage.event_day_id)
        form = FamilyMealForm(registration=registration, meal_order=order)
        self.assertIn(beverage, form.fields["beverage"].queryset)

    def test_new_dates_receive_exactly_the_globals_and_cleaned_profile_clones_no_local_drinks(self):
        self.production_shape()
        profile = save_event_profile(event=self.event, name="Concours réutilisable")
        self.migrate()
        profile.refresh_from_db()
        self.assertTrue(all(not day["beverages"] for day in profile.snapshot["days"]))
        clone = create_event_from_profile(profile=profile, name="Nouveau concours", event_date=self.day.date + timedelta(days=30))
        self.assertFalse(Beverage.objects.filter(event_day__event=clone).exists())
        expected = set(Beverage.objects.filter(event_day__isnull=True, is_active=True).values_list("pk", flat=True))
        self.assertEqual(len(expected), 15)
        for day in clone.days.all():
            self.assertEqual(set(beverage_options_for_day(day).values_list("pk", flat=True)), expected)

    def test_all_profile_matches_are_removed_but_other_configuration_and_variants_are_preserved(self):
        beverage = Beverage.objects.create(event_day=self.day, name="Jus", description="Pomme", surcharge_cents=150)
        Menu.objects.create(event_day=self.day, name="Menu conservé", surcharge_cents=500)
        profile = save_event_profile(event=self.event, name="Profil nettoyé")
        original = deepcopy(profile.snapshot)
        variant_price = {"name": "Jus", "description": "Pomme", "surcharge_cents": 999, "is_active": True}
        variant_description = {"name": "Jus", "description": "Orange", "surcharge_cents": 150, "is_active": True}
        normalized_match = {"name": " JUS ", "description": "Pomme", "surcharge_cents": 150, "is_active": False}
        malformed = {"name": "Jus", "surcharge_cents": 150}
        profile.snapshot["days"][0]["beverages"].extend([
            variant_price, variant_description, normalized_match, malformed,
        ])
        profile.save()
        profile_before_time = profile.updated_at
        another = EventProfile.objects.create(name="Autre profil nettoyé", snapshot={
            "version": 1, "event": {"location": "Lieu conservé"}, "days": [{"offset": 2, "beverages": [normalized_match]}],
        })
        untouched_snapshot = {"version": 1, "days": [{"offset": 0, "beverages": [variant_price]}], "unknown": [1, 2]}
        untouched = EventProfile.objects.create(name="Autre profil conservé", snapshot=untouched_snapshot)
        untouched_before = EventProfile.objects.filter(pk=untouched.pk).values().get()
        result = self.migrate()
        self.assertEqual(result["profiles_cleaned"], 2)
        profile.refresh_from_db()
        another.refresh_from_db()
        self.assertEqual(profile.snapshot["days"][0]["beverages"], [variant_price, variant_description, malformed])
        self.assertEqual(profile.snapshot["days"][0]["menus"], original["days"][0]["menus"])
        for field in ("event", "event_name", "windows", "stations", "version"):
            self.assertEqual(profile.snapshot[field], original[field])
        self.assertGreater(profile.updated_at, profile_before_time)
        self.assertEqual(another.snapshot["days"], [{"offset": 2, "beverages": []}])
        self.assertEqual(another.snapshot["event"], {"location": "Lieu conservé"})
        self.assertEqual(EventProfile.objects.filter(pk=untouched.pk).values().get(), untouched_before)
        self.assertTrue(Beverage.objects.filter(pk=beverage.pk).exists())

    def test_migration_is_idempotent_including_all_updated_at_fields(self):
        self.production_shape()
        save_event_profile(event=self.event, name="Profil nettoyé")
        self.migrate()
        beverages = list(Beverage.objects.values().order_by("pk"))
        profiles = list(EventProfile.objects.values().order_by("pk"))
        result = self.migrate()
        self.assertEqual(result, {"promoted": 0, "deactivated": 0, "profiles_cleaned": 0})
        self.assertEqual(list(Beverage.objects.values().order_by("pk")), beverages)
        self.assertEqual(list(EventProfile.objects.values().order_by("pk")), profiles)

    def test_inactive_group_remains_inactive_and_active_local_is_preferred_without_reactivation(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        inactive = Beverage.objects.create(id=UUID(int=1), event_day=self.day, name="Jus", is_active=False)
        active = Beverage.objects.create(id=UUID(int=2), event_day=other_day, name="Jus", is_active=True)
        all_inactive = Beverage.objects.create(event_day=self.day, name="Ancienne boisson", is_active=False)
        inactive_before = Beverage.objects.filter(pk=inactive.pk).values().get()
        result = self.migrate()
        active.refresh_from_db()
        all_inactive.refresh_from_db()
        self.assertEqual(result["promoted"], 2)
        self.assertEqual(result["deactivated"], 0)
        self.assertIsNone(active.event_day_id)
        self.assertTrue(active.is_active)
        self.assertIsNone(all_inactive.event_day_id)
        self.assertFalse(all_inactive.is_active)
        self.assertEqual(Beverage.objects.filter(pk=inactive.pk).values().get(), inactive_before)

    def test_existing_global_keeps_activity_and_timestamp_while_equivalent_local_is_disabled(self):
        existing_global = Beverage.objects.create(name="Jus", description="Pomme", is_active=False)
        local = Beverage.objects.create(event_day=self.day, name=" JUS ", description="Pomme", is_active=True)
        before = Beverage.objects.filter(pk=existing_global.pk).values().get()
        result = self.migrate()
        self.assertEqual(result["promoted"], 0)
        self.assertEqual(result["deactivated"], 1)
        self.assertEqual(Beverage.objects.filter(pk=existing_global.pk).values().get(), before)
        local.refresh_from_db()
        self.assertFalse(local.is_active)
        self.assertEqual(local.event_day_id, self.day.pk)

    def test_incompatible_prices_or_descriptions_abort_before_any_mutation(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        Beverage.objects.create(event_day=self.day, name="Sans conflit")
        Beverage.objects.create(event_day=self.day, name="Jus", description="Pomme", surcharge_cents=150)
        collision = Beverage.objects.create(event_day=other_day, name=" JUS ", description="Pomme", surcharge_cents=200)
        profile = save_event_profile(event=self.event, name="À conserver")
        for values in ({"surcharge_cents": 200, "description": "Pomme"}, {"surcharge_cents": 150, "description": "Orange"}):
            Beverage.objects.filter(pk=collision.pk).update(**values)
            before = list(Beverage.objects.values().order_by("pk"))
            profile_before = EventProfile.objects.filter(pk=profile.pk).values().get()
            with self.subTest(values=values), self.assertRaisesMessage(RuntimeError, "plusieurs prix ou descriptions"):
                self.migrate()
            self.assertEqual(list(Beverage.objects.values().order_by("pk")), before)
            self.assertEqual(EventProfile.objects.filter(pk=profile.pk).values().get(), profile_before)

    def test_multiple_normalized_globals_abort_instead_of_selecting_an_arbitrary_offer(self):
        Beverage.objects.create(name="BIERE", surcharge_cents=200)
        Beverage.objects.create(name="biere", surcharge_cents=200)
        Beverage.objects.create(event_day=self.day, name="Autre boisson")
        before = list(Beverage.objects.values().order_by("pk"))
        with self.assertRaisesMessage(RuntimeError, "Plusieurs boissons communes"):
            self.migrate()
        self.assertEqual(list(Beverage.objects.values().order_by("pk")), before)

    def test_empty_catalogue_preserves_unrelated_and_malformed_profiles(self):
        snapshots = ({}, [], {"days": [None, {"offset": 0, "beverages": [None, {"name": "Jus"}]}]})
        for index, snapshot in enumerate(snapshots):
            EventProfile.objects.create(name=f"Profil {index}", snapshot=snapshot)
        before = list(EventProfile.objects.values().order_by("pk"))
        self.assertEqual(self.migrate(), {"promoted": 0, "deactivated": 0, "profiles_cleaned": 0})
        self.assertEqual(list(EventProfile.objects.values().order_by("pk")), before)

    def test_busy_postgres_catalogue_aborts_cleanly_and_can_be_retried(self):
        Beverage.objects.create(event_day=self.day, name="Jus")
        before = list(Beverage.objects.values().order_by("pk"))

        class LockBusy(Exception):
            sqlstate = "55P03"

        error = OperationalError("lock not available")
        error.__cause__ = LockBusy()
        cursor = MagicMock()
        cursor.__enter__.return_value.execute.side_effect = error
        stub_connection = SimpleNamespace(alias=connection.alias, vendor="postgresql", cursor=lambda: cursor)
        editor = SimpleNamespace(connection=stub_connection, quote_name=connection.ops.quote_name)
        with self.assertRaisesMessage(RuntimeError, "catalogue est en cours de modification"):
            self.migrate(editor)
        self.assertEqual(list(Beverage.objects.values().order_by("pk")), before)
        self.assertEqual(self.migrate()["promoted"], 1)

    def test_reverse_is_noop_and_does_not_relocalize_shared_historical_ids(self):
        Beverage.objects.create(event_day=self.day, name="Jus")
        self.migrate()
        before = list(Beverage.objects.values().order_by("pk"))
        self.migration.Migration.operations[0].reverse_code(
            self.historical_apps, SimpleNamespace(connection=connection),
        )
        self.assertEqual(list(Beverage.objects.values().order_by("pk")), before)
