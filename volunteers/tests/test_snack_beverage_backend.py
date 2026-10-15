from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import MagicMock

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import OperationalError, close_old_connections, connection, connections
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase
from django.utils import timezone

from volunteers.meal_catalogue import beverage_options_for_day
from volunteers.models import Beverage, Event, EventDay, MealOrder, Menu, Payment, Registration
from volunteers.participation import save_participation
from volunteers.services import (
    BusinessRuleError,
    create_assignment,
    save_meal_order,
    save_registration,
)

from .factories import DataMixin


class SnackBeverageBackendTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()
        self.menu = Menu.objects.create(name="Repas", surcharge_cents=500)
        self.snack = Menu.objects.create(name="Collation", meal_type=Menu.MealType.SNACK, surcharge_cents=150)
        self.drink = Beverage.objects.create(name="Boisson repas", surcharge_cents=100)
        self.hot = Beverage.objects.create(name="Café", temperature=Beverage.Temperature.HOT, surcharge_cents=80)
        self.cold = Beverage.objects.create(name="Jus frais", temperature=Beverage.Temperature.COLD, surcharge_cents=120)

    def full_choices(self, **overrides):
        return {
            "menu": self.menu, "beverage": self.drink,
            "morning_snack": self.snack, "afternoon_snack": self.snack,
            "morning_beverage": self.hot, "afternoon_beverage": self.cold,
        } | overrides

    def order(self, **overrides):
        return save_meal_order(registration=self.reg, **self.full_choices(**overrides))

    def half_day(self, availability=Registration.Availability.MORNING):
        self.reg = save_registration(member=self.member, event_day=self.day, availability=availability)
        return self.reg

    def test_six_components_and_prices_are_saved_in_one_order(self):
        order = self.order()
        self.assertEqual(order.total_surcharge_cents, 1100)
        self.assertEqual(order.morning_beverage_standard_surcharge_cents, 80)
        self.assertEqual(order.afternoon_beverage_standard_surcharge_cents, 120)
        self.assertEqual(order.beverage_choice_availability, Registration.Availability.FULL_DAY)
        self.assertEqual(order.morning_beverage_id, self.hot.pk)

    def test_official_assignment_exempts_all_six_and_volunteer_restores_reserved_prices(self):
        self.member.is_competition_official = True
        self.member.save()
        order = self.order()
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=1100, method=Payment.Method.ON_SITE,
        )
        before = Payment.objects.filter(pk=payment.pk).values().get()
        self.hot.surcharge_cents = 999
        self.hot.save()
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        order.refresh_from_db()
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 1100)
        self.assertEqual(order.morning_beverage_surcharge_cents, 80)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), before)

    def test_extra_drinks_require_correct_temperature_and_associated_snack(self):
        for overrides in (
            {"morning_beverage": self.cold}, {"afternoon_beverage": self.hot},
            {"morning_beverage": self.drink}, {"morning_snack": None}, {"afternoon_snack": None},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(BusinessRuleError):
                self.order(**overrides)
        self.assertFalse(MealOrder.objects.exists())

    def test_direct_model_save_enforces_drink_type_and_extra_snack_link(self):
        with self.assertRaises(ValidationError):
            MealOrder.objects.create(
                registration=self.reg, wants_meal=True, menu=self.menu, morning_beverage=self.hot,
            )
        with self.assertRaises(ValidationError):
            MealOrder.objects.create(
                registration=self.reg, wants_meal=True, menu=self.menu,
                morning_snack=self.snack, morning_beverage=self.cold,
            )

    def test_event_can_disable_each_period_independently(self):
        self.event.morning_hot_beverage_enabled = False
        self.event.save(update_fields=("morning_hot_beverage_enabled",))
        with self.assertRaises(BusinessRuleError):
            self.order()
        order = self.order(morning_beverage=None)
        self.assertEqual(order.afternoon_beverage_id, self.cold.pk)
        self.event.afternoon_cold_beverage_enabled = False
        self.event.save(update_fields=("afternoon_cold_beverage_enabled",))
        # Already reserved afternoon drink is retained and priced exactly as before.
        repeated = self.order(morning_beverage=None)
        self.assertEqual(repeated.total_surcharge_cents, order.total_surcharge_cents)

    def test_disabled_offers_and_changed_temperature_do_not_erase_existing_choices(self):
        order = self.order()
        before = MealOrder.objects.filter(pk=order.pk).values().get()
        self.event.morning_hot_beverage_enabled = False
        self.event.afternoon_cold_beverage_enabled = False
        self.event.save()
        self.hot.temperature = Beverage.Temperature.UNSPECIFIED
        self.hot.surcharge_cents = 777
        self.hot.save()
        order.refresh_from_db()
        self.assertEqual(MealOrder.objects.filter(pk=order.pk).values().get(), before)
        repeated = self.order()
        self.assertEqual(repeated.total_surcharge_cents, 1100)
        self.assertEqual(repeated.morning_beverage_id, self.hot.pk)

    def test_removed_extra_cannot_be_added_again_while_offer_is_disabled(self):
        self.order()
        self.event.morning_hot_beverage_enabled = False
        self.event.save()
        self.order(morning_beverage=None)
        with self.assertRaises(BusinessRuleError):
            self.order()

    def test_half_day_primary_drink_uses_temperature_and_event_flag(self):
        self.half_day()
        for beverage in (self.cold, self.drink):
            with self.assertRaises(BusinessRuleError):
                save_meal_order(registration=self.reg, menu=self.snack, beverage=beverage)
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        self.assertEqual(order.total_surcharge_cents, 230)
        self.assertEqual(order.beverage_choice_availability, Registration.Availability.MORNING)
        self.event.morning_hot_beverage_enabled = False
        self.event.save()
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        self.assertEqual(order.total_surcharge_cents, 230)
        save_meal_order(registration=self.reg, menu=self.snack)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)

    def test_same_legacy_primary_drink_is_preserved_only_in_its_reserved_period(self):
        self.half_day()
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        self.hot.temperature = Beverage.Temperature.UNSPECIFIED
        self.hot.save()
        order.save()
        self.assertEqual(order.beverage_choice_availability, Registration.Availability.MORNING)
        self.half_day(Registration.Availability.AFTERNOON)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        order.refresh_from_db()
        self.assertEqual(order.beverage_availability, Registration.Availability.MORNING)
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.cold)
        self.assertEqual(order.beverage_availability, Registration.Availability.AFTERNOON)

    def test_extra_drinks_are_not_allowed_in_half_day(self):
        self.half_day()
        with self.assertRaises(BusinessRuleError):
            save_meal_order(
                registration=self.reg, menu=self.snack, morning_beverage=self.hot,
            )

    def test_primary_full_day_drink_is_independent_of_snack_flags_and_temperature(self):
        self.event.morning_hot_beverage_enabled = False
        self.event.afternoon_cold_beverage_enabled = False
        self.event.save()
        for beverage in (self.drink, self.hot, self.cold):
            order = save_meal_order(registration=self.reg, menu=self.menu, beverage=beverage)
            self.assertEqual(order.beverage_id, beverage.pk)

    def test_half_day_transition_clears_all_extra_components_and_their_prices(self):
        self.order()
        self.half_day()
        order = MealOrder.objects.get(registration=self.reg)
        for component in ("morning_snack", "afternoon_snack", "morning_beverage", "afternoon_beverage"):
            self.assertIsNone(getattr(order, f"{component}_id"))
            self.assertEqual(getattr(order, f"{component}_standard_surcharge_cents"), 0)
            self.assertEqual(getattr(order, f"{component}_surcharge_cents"), 0)

    def test_no_meal_clears_all_six_components_and_snapshots(self):
        order = self.order()
        order.wants_meal = False
        order.save()
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(order.beverage_availability, "")
        for component in MealOrder.PRICE_COMPONENTS:
            self.assertIsNone(getattr(order, f"{component}_id"))
            self.assertEqual(getattr(order, f"{component}_standard_surcharge_cents"), 0)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=self.reg, no_meal=True, morning_beverage=self.hot)

    def test_inactive_and_foreign_day_drinks_are_rejected_except_identical_old_inactive_choice(self):
        self.order()
        self.hot.is_active = False
        self.hot.save()
        self.order()
        other_hot = Beverage.objects.create(name="Thé retiré", temperature=Beverage.Temperature.HOT, is_active=False)
        with self.assertRaises(BusinessRuleError):
            self.order(morning_beverage=other_hot)
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Beverage.objects.create(event_day=other_day, name="Autre café", temperature=Beverage.Temperature.HOT)
        with self.assertRaises(BusinessRuleError):
            self.order(morning_beverage=foreign)

    def test_closed_meals_prevent_silent_loss_of_drinks_when_changing_presence(self):
        order = self.order()
        self.event.meal_orders_close_at = timezone.now() - timedelta(minutes=1)
        self.event.save()
        before = MealOrder.objects.filter(pk=order.pk).values().get()
        with self.assertRaises(BusinessRuleError):
            save_participation(
                member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
                actor=self.family_user, meal_choices=None,
            )
        self.reg.refresh_from_db()
        self.assertEqual(self.reg.availability, Registration.Availability.FULL_DAY)
        self.assertEqual(MealOrder.objects.filter(pk=order.pk).values().get(), before)

    def test_temperature_filter_keeps_common_and_local_options_and_excludes_other_temperatures(self):
        local = Beverage.objects.create(event_day=self.day, name="Thé", temperature=Beverage.Temperature.HOT)
        choices = beverage_options_for_day(self.day, temperature=Beverage.Temperature.HOT)
        self.assertSetEqual(set(choices.values_list("pk", flat=True)), {self.hot.pk, local.pk})
        self.assertIn(self.drink, beverage_options_for_day(self.day))

    def test_partial_save_keeps_all_drinks_and_ignores_stale_cached_choices(self):
        order = self.order()
        order.morning_beverage = None
        order.afternoon_beverage = None
        order.payment_method = MealOrder.PaymentMethod.SUMUP
        order.save(update_fields=("payment_method",))
        order.refresh_from_db()
        self.assertEqual(order.morning_beverage_id, self.hot.pk)
        self.assertEqual(order.afternoon_beverage_id, self.cold.pk)
        self.assertEqual(order.total_surcharge_cents, 1100)

    def test_migration_backfills_period_without_changing_choices_prices_or_payments(self):
        order = self.order()
        MealOrder.objects.filter(pk=order.pk).update(beverage_availability="legacy")
        before = MealOrder.objects.filter(pk=order.pk).values().get()
        migration = import_module("volunteers.migrations.0011_snack_beverages")
        migration.preserve_beverage_periods(apps, SimpleNamespace(connection=connection))
        after = MealOrder.objects.filter(pk=order.pk).values().get()
        self.assertEqual(after.pop("beverage_availability"), Registration.Availability.FULL_DAY)
        before.pop("beverage_availability")
        self.assertEqual(after, before)

    def other_registration(self, *, availability=Registration.Availability.FULL_DAY, morning_enabled=False):
        event = Event.objects.create(
            name="Autre événement sans boisson", registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at, status=Event.Status.PUBLISHED,
            morning_hot_beverage_enabled=morning_enabled,
        )
        day = EventDay.objects.create(event=event, date=self.day.date)
        return Registration.objects.create(member=self.other_member, event_day=day, availability=availability)

    def test_moving_existing_extra_drinks_to_disabled_event_is_not_a_preserved_choice(self):
        order = self.order()
        order.registration = self.other_registration()
        with self.assertRaises(ValidationError):
            order.save()
        order.refresh_from_db()
        self.assertEqual(order.registration_id, self.reg.pk)
        self.assertEqual(order.total_surcharge_cents, 1100)

    def test_moving_existing_primary_drink_to_disabled_event_is_not_a_preserved_choice(self):
        self.half_day()
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        order.registration = self.other_registration(availability=Registration.Availability.MORNING)
        with self.assertRaises(ValidationError):
            order.save()
        order.refresh_from_db()
        self.assertEqual(order.registration_id, self.reg.pk)
        self.assertEqual(order.total_surcharge_cents, 230)

    def test_moving_order_is_allowed_when_new_event_normally_accepts_all_choices(self):
        order = self.order()
        target = self.other_registration(morning_enabled=True)
        order.registration = target
        order.save()
        order.refresh_from_db()
        self.assertEqual(order.registration_id, target.pk)
        self.assertEqual(order.total_surcharge_cents, 1100)

    def test_moving_order_does_not_preserve_an_inactive_global_drink_for_another_registration(self):
        order = self.order()
        self.hot.is_active = False
        self.hot.save()
        order.registration = self.other_registration(morning_enabled=True)
        with self.assertRaises(ValidationError):
            order.save()
        order.refresh_from_db()
        self.assertEqual(order.registration_id, self.reg.pk)

    def test_rollout_legacy_drink_cannot_change_to_disabled_or_incompatible_morning_offer(self):
        order = save_meal_order(registration=self.reg, menu=self.menu, beverage=self.cold)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                MealOrder.objects.filter(pk=order.pk).update(beverage_availability="legacy")
                self.event.morning_hot_beverage_enabled = enabled
                self.event.save(update_fields=("morning_hot_beverage_enabled",))
                with self.assertRaises(BusinessRuleError):
                    save_participation(
                        member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
                        actor=self.family_user, meal_choices={"menu": self.snack, "beverage": self.cold},
                    )
                self.reg.refresh_from_db()
                order.refresh_from_db()
                self.assertEqual(self.reg.availability, Registration.Availability.FULL_DAY)
                self.assertEqual(order.beverage_id, self.cold.pk)
                self.assertEqual(order.total_surcharge_cents, 620)

    def test_rollout_legacy_same_period_disabled_drink_remains_preserved(self):
        self.half_day()
        order = save_meal_order(registration=self.reg, menu=self.snack, beverage=self.hot)
        MealOrder.objects.filter(pk=order.pk).update(beverage_availability="legacy")
        self.event.morning_hot_beverage_enabled = False
        self.event.save(update_fields=("morning_hot_beverage_enabled",))
        save_participation(
            member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
            actor=self.family_user, meal_choices={"menu": self.snack, "beverage": self.hot},
        )
        order.refresh_from_db()
        self.assertEqual(order.beverage_availability, Registration.Availability.MORNING)
        self.assertEqual(order.total_surcharge_cents, 230)

    def test_rollout_legacy_drink_can_move_to_an_enabled_period_with_correct_temperature(self):
        order = save_meal_order(registration=self.reg, menu=self.menu, beverage=self.hot)
        MealOrder.objects.filter(pk=order.pk).update(beverage_availability="legacy")
        save_participation(
            member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
            actor=self.family_user, meal_choices={"menu": self.snack, "beverage": self.hot},
        )
        order.refresh_from_db()
        self.assertEqual(order.beverage_availability, Registration.Availability.MORNING)
        self.assertEqual(order.total_surcharge_cents, 230)

    def test_rollout_freezing_phase_preserves_order_prices_timestamps_and_payments(self):
        order = save_meal_order(registration=self.reg, menu=self.menu, beverage=self.cold)
        MealOrder.objects.filter(pk=order.pk).update(beverage_availability="legacy")
        Payment.objects.create(
            family=self.family, event=self.event, amount_cents=620, method=Payment.Method.SUMUP,
        )
        before = MealOrder.objects.values().get(pk=order.pk)
        payments = list(Payment.objects.values().order_by("pk"))
        self.reg.note = "Autre remarque"
        self.reg.save(update_fields=("note",))
        after = MealOrder.objects.values().get(pk=order.pk)
        self.assertEqual(after.pop("beverage_availability"), Registration.Availability.FULL_DAY)
        before.pop("beverage_availability")
        self.assertEqual(after, before)
        self.assertEqual(list(Payment.objects.values().order_by("pk")), payments)


class SnackBeverageMigrationTests(TransactionTestCase):
    def test_real_migration_preserves_existing_prices_and_payments_and_records_drink_period(self):
        previous = [("volunteers", "0010_official_volunteer_meal_pricing")]
        target = [("volunteers", "0011_snack_beverages")]
        executor = MigrationExecutor(connection)
        try:
            executor.migrate(previous)
            old_apps = executor.loader.project_state(previous).apps
            User = old_apps.get_model("volunteers", "User")
            Family = old_apps.get_model("volunteers", "Family")
            Member = old_apps.get_model("volunteers", "Member")
            Event = old_apps.get_model("volunteers", "Event")
            EventDay = old_apps.get_model("volunteers", "EventDay")
            Registration = old_apps.get_model("volunteers", "Registration")
            Menu = old_apps.get_model("volunteers", "Menu")
            Beverage = old_apps.get_model("volunteers", "Beverage")
            MealOrder = old_apps.get_model("volunteers", "MealOrder")
            Payment = old_apps.get_model("volunteers", "Payment")
            now = timezone.now()
            user = User.objects.create(email="migration-snack@example.test")
            family = Family.objects.create(account=user, name="Migration")
            member = Member.objects.create(
                family=family, first_name="Alex", last_name="Migration", birth_date=date(1990, 1, 1),
            )
            event = Event.objects.create(
                name="Avant boissons", registration_opens_at=now - timedelta(days=1),
                registration_closes_at=now + timedelta(days=1),
                meal_orders_close_at=now + timedelta(days=1), status="published",
            )
            day = EventDay.objects.create(event=event, date=timezone.localdate() + timedelta(days=2))
            registration = Registration.objects.create(member=member, event_day=day, availability="morning")
            menu = Menu.objects.create(name="Ancienne collation", meal_type="snack", surcharge_cents=999)
            beverage = Beverage.objects.create(name="Ancienne boisson", surcharge_cents=888)
            order = MealOrder.objects.create(
                registration=registration, wants_meal=True, menu=menu, beverage=beverage,
                menu_surcharge_cents=500, beverage_surcharge_cents=200, total_surcharge_cents=700,
                menu_standard_surcharge_cents=500, beverage_standard_surcharge_cents=200,
                legacy_availability="morning",
            )
            payment = Payment.objects.create(
                family=family, event=event, method="sumup", amount_cents=700,
            )
            old_order = MealOrder.objects.values().get(pk=order.pk)
            old_payment = Payment.objects.values().get(pk=payment.pk)
            executor = MigrationExecutor(connection)
            executor.migrate(target)
            new_apps = executor.loader.project_state(target).apps
            current_order = new_apps.get_model("volunteers", "MealOrder").objects.values().get(pk=order.pk)
            self.assertEqual({field: current_order[field] for field in old_order}, old_order)
            self.assertEqual(current_order["beverage_availability"], "morning")
            self.assertIsNone(current_order["morning_beverage_id"])
            self.assertIsNone(current_order["afternoon_beverage_id"])
            self.assertEqual(current_order["morning_beverage_surcharge_cents"], 0)
            self.assertEqual(current_order["afternoon_beverage_surcharge_cents"], 0)
            self.assertEqual(new_apps.get_model("volunteers", "Payment").objects.values().get(pk=payment.pk), old_payment)
            self.assertEqual(new_apps.get_model("volunteers", "Beverage").objects.get(pk=beverage.pk).temperature, "unspecified")
            current_event = new_apps.get_model("volunteers", "Event").objects.get(pk=event.pk)
            self.assertTrue(current_event.morning_hot_beverage_enabled)
            self.assertTrue(current_event.afternoon_cold_beverage_enabled)
        finally:
            MigrationExecutor(connection).migrate(target)


class SnackBeverageSchemaGuardTests(SimpleTestCase):
    def editor(self, vendor="postgresql"):
        connection = MagicMock()
        connection.vendor = vendor
        return SimpleNamespace(connection=connection, quote_name=lambda name: f'"{name}"')

    def test_postgres_acquires_all_table_locks_with_one_nowait_statement(self):
        migration = import_module("volunteers.migrations.0011_snack_beverages")
        editor = self.editor()
        migration.lock_schema_tables(apps, editor)
        cursor = editor.connection.cursor.return_value.__enter__.return_value
        cursor.execute.assert_called_once_with(
            'LOCK TABLE "volunteers_event", "volunteers_beverage", "volunteers_mealorder" '
            'IN ACCESS EXCLUSIVE MODE NOWAIT',
        )

    def test_busy_schema_fails_with_retry_instruction_before_making_changes(self):
        migration = import_module("volunteers.migrations.0011_snack_beverages")
        editor = self.editor()
        cursor = editor.connection.cursor.return_value.__enter__.return_value
        cursor.execute.side_effect = OperationalError("table busy")
        with self.assertRaisesMessage(RuntimeError, "Réessayez le déploiement"):
            migration.lock_schema_tables(apps, editor)
        self.assertEqual(cursor.execute.call_count, 1)

    def test_sqlite_does_not_execute_postgres_lock_sql(self):
        migration = import_module("volunteers.migrations.0011_snack_beverages")
        editor = self.editor(vendor="sqlite")
        migration.lock_schema_tables(apps, editor)
        editor.connection.cursor.assert_not_called()


@skipUnless(connection.vendor == "postgresql", "Verrous PostgreSQL nécessaires")
class ConcurrentSnackBeverageTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.reg = self.registration()
        self.menu = Menu.objects.create(name="Repas concurrent", surcharge_cents=500)
        self.snack = Menu.objects.create(name="Collation concurrente", meal_type=Menu.MealType.SNACK, surcharge_cents=150)
        self.hot = Beverage.objects.create(name="Café concurrent", temperature=Beverage.Temperature.HOT, surcharge_cents=80)

    def test_disabling_offer_and_reserving_drink_are_serialized_without_deadlock(self):
        barrier = Barrier(2)

        def action(disable):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                registration = Registration.objects.get(pk=self.reg.pk)
                event = registration.event_day.event
                barrier.wait(timeout=5)
                if disable:
                    event.morning_hot_beverage_enabled = False
                    event.save(update_fields=("morning_hot_beverage_enabled",))
                    return "disabled"
                try:
                    order = save_meal_order(
                        registration=registration, menu=self.menu, morning_snack=self.snack,
                        morning_beverage=self.hot,
                    )
                    return order.pk
                except BusinessRuleError:
                    return "offer_disabled"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            disabled = pool.submit(action, True)
            reserved = pool.submit(action, False)
            self.assertEqual(disabled.result(timeout=15), "disabled")
            result = reserved.result(timeout=15)
        self.event.refresh_from_db()
        self.assertFalse(self.event.morning_hot_beverage_enabled)
        if result == "offer_disabled":
            self.assertFalse(MealOrder.objects.filter(registration=self.reg).exists())
        else:
            order = MealOrder.objects.get(pk=result)
            self.assertEqual(order.morning_beverage_id, self.hot.pk)
            self.assertEqual(order.total_surcharge_cents, 730)

    def test_opposite_drink_component_order_across_days_uses_sorted_locks(self):
        other_hot = Beverage.objects.create(name="Thé concurrent", temperature=Beverage.Temperature.HOT, surcharge_cents=120)
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        other_reg = Registration.objects.create(
            member=self.other_member, event_day=other_day, availability=Registration.Availability.FULL_DAY,
        )
        barrier = Barrier(2)

        def action(second):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                registration = Registration.objects.get(pk=other_reg.pk if second else self.reg.pk)
                barrier.wait(timeout=5)
                return save_meal_order(
                    registration=registration, menu=self.menu, morning_snack=self.snack,
                    beverage=other_hot if second else self.hot,
                    morning_beverage=self.hot if second else other_hot,
                ).pk
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(action, second) for second in (False, True)]
            ids = [future.result(timeout=15) for future in futures]
        self.assertEqual(list(MealOrder.objects.filter(pk__in=ids).values_list("total_surcharge_cents", flat=True)), [850, 850])
