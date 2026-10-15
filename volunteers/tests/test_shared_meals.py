from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase

from volunteers.meal_catalogue import beverage_options_for_day, menu_options_for_day
from volunteers.models import Beverage, EventDay, MealOrder, Menu, Payment, Registration
from volunteers.participation import save_participation
from volunteers.services import BusinessRuleError, family_event_balance, save_meal_order

from .factories import DataMixin


class SharedMealCatalogueTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()
        self.main = Menu.objects.create(name="Repas commun", surcharge_cents=500)
        self.snack = Menu.objects.create(name="Collation commune", meal_type=Menu.MealType.SNACK, surcharge_cents=150)
        self.drink = Beverage.objects.create(name="Jus commun", surcharge_cents=100)

    def order(self, **changes):
        choices = {
            "registration": self.reg, "menu": self.main, "beverage": self.drink,
            "morning_snack": self.snack, "afternoon_snack": self.snack,
        }
        choices.update(changes)
        return save_meal_order(**choices)

    def test_catalogue_options_combine_shared_and_current_day_without_other_days(self):
        local = Menu.objects.create(event_day=self.day, name="Menu local")
        local_drink = Beverage.objects.create(event_day=self.day, name="Boisson locale")
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Menu.objects.create(event_day=other_day, name="Autre jour")
        foreign_drink = Beverage.objects.create(event_day=other_day, name="Boisson autre jour")
        inactive = Menu.objects.create(name="Ancien menu", is_active=False)
        inactive_drink = Beverage.objects.create(name="Ancienne boisson", is_active=False)
        self.assertSetEqual(set(menu_options_for_day(self.day)), {self.main, self.snack, local})
        self.assertSetEqual(set(beverage_options_for_day(self.day)), {self.drink, local_drink})
        self.assertIn(inactive, menu_options_for_day(self.day, active_only=False))
        self.assertIn(inactive_drink, beverage_options_for_day(self.day, active_only=False))
        self.assertNotIn(foreign, menu_options_for_day(self.day, active_only=False))
        self.assertNotIn(foreign_drink, beverage_options_for_day(self.day, active_only=False))
        self.assertIn("Catalogue commun", str(self.main))
        self.assertIn("Catalogue commun", str(self.drink))

    def test_shared_options_are_reusable_on_another_date_without_copying(self):
        first = self.order()
        second_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        second_reg = Registration.objects.create(member=self.other_member, event_day=second_day, availability=Registration.Availability.FULL_DAY)
        second = self.order(registration=second_reg)
        self.assertEqual(first.menu_id, second.menu_id)
        self.assertEqual(first.morning_snack_id, second.morning_snack_id)
        self.assertEqual(first.beverage_id, second.beverage_id)
        self.assertIsNone(self.main.event_day_id)
        self.assertEqual(Menu.objects.count(), 2)
        self.assertEqual(Beverage.objects.count(), 1)
        self.assertEqual(first.total_surcharge_cents, 900)
        self.assertEqual(second.total_surcharge_cents, 900)

    def test_uniqueness_is_name_and_type_for_shared_menus_but_name_for_shared_drinks(self):
        Menu.objects.create(name=self.main.name, meal_type=Menu.MealType.SNACK)
        with self.assertRaises(ValidationError):
            Menu(name=self.main.name).full_clean()
        with self.assertRaises(ValidationError):
            Beverage(name=self.drink.name).full_clean()
        Menu.objects.create(event_day=self.day, name=self.main.name)
        with self.assertRaises(ValidationError):
            Menu(event_day=self.day, name=self.main.name, meal_type=Menu.MealType.SNACK).full_clean()
        Beverage.objects.create(event_day=self.day, name=self.drink.name)

    def test_four_component_prices_are_snapshotted_and_only_changed_choice_is_repriced(self):
        order = self.order()
        self.assertEqual(order.total_surcharge_cents, 900)
        self.assertEqual(order.morning_snack_surcharge_cents, 150)
        self.assertEqual(order.afternoon_snack_standard_surcharge_cents, 150)
        self.main.surcharge_cents = 900
        self.main.save()
        self.snack.surcharge_cents = 400
        self.snack.save()
        self.drink.surcharge_cents = 300
        self.drink.save()
        preserved = self.order()
        self.assertEqual(preserved.total_surcharge_cents, 900)
        changed_snack = Menu.objects.create(name="Autre collation", meal_type=Menu.MealType.SNACK, surcharge_cents=250)
        updated = self.order(morning_snack=changed_snack)
        self.assertEqual(updated.menu_surcharge_cents, 500)
        self.assertEqual(updated.beverage_surcharge_cents, 100)
        self.assertEqual(updated.morning_snack_surcharge_cents, 250)
        self.assertEqual(updated.afternoon_snack_surcharge_cents, 150)
        self.assertEqual(updated.total_surcharge_cents, 1000)
        other = self.registration(member=self.other_member)
        fresh = self.order(registration=other)
        self.assertEqual(fresh.total_surcharge_cents, 2000)

    def test_official_exemption_and_withdrawal_cover_all_four_components_without_changing_payments(self):
        order = self.order()
        payment = Payment.objects.create(family=self.family, event=self.event, amount_cents=900, method=Payment.Method.SUMUP)
        before = Payment.objects.filter(pk=payment.pk).values().get()
        self.member.is_competition_official = True
        self.member.save(update_fields=("is_competition_official",))
        order.refresh_from_db()
        self.assertTrue(order.official_exemption_applied)
        for component in MealOrder.PRICE_COMPONENTS:
            self.assertEqual(getattr(order, f"{component}_surcharge_cents"), 0)
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(family_event_balance(family=self.family, event=self.event)["overpayment_cents"], 900)
        self.snack.surcharge_cents = 999
        self.snack.save()
        free = self.order()
        self.assertEqual(free.total_surcharge_cents, 0)
        self.assertEqual(free.morning_snack_standard_surcharge_cents, 150)
        self.member.is_competition_official = False
        self.member.save(update_fields=("is_competition_official",))
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 900)
        self.assertEqual(order.morning_snack_surcharge_cents, 150)
        self.assertEqual(order.afternoon_snack_surcharge_cents, 150)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), before)

    def test_new_official_order_has_zero_total_and_standard_prices_for_all_choices(self):
        self.member.is_competition_official = True
        self.member.save()
        order = self.order()
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(order.menu_standard_surcharge_cents, 500)
        self.assertEqual(order.beverage_standard_surcharge_cents, 100)
        self.assertEqual(order.morning_snack_standard_surcharge_cents, 150)
        self.assertEqual(order.afternoon_snack_standard_surcharge_cents, 150)

    def test_extra_collations_are_optional_and_no_meal_clears_every_choice_and_snapshot(self):
        order = self.order(morning_snack=None, afternoon_snack=None)
        self.assertEqual(order.total_surcharge_cents, 600)
        self.assertIsNone(order.morning_snack_id)
        self.assertEqual(order.morning_snack_standard_surcharge_cents, 0)
        self.order()
        empty = save_meal_order(registration=self.reg, no_meal=True)
        for component in MealOrder.PRICE_COMPONENTS:
            self.assertIsNone(getattr(empty, f"{component}_id"))
            self.assertEqual(getattr(empty, f"{component}_surcharge_cents"), 0)
            self.assertEqual(getattr(empty, f"{component}_standard_surcharge_cents"), 0)
        self.assertEqual(empty.total_surcharge_cents, 0)

    def test_extra_collations_still_require_full_day_snack_type_correct_day_and_active_choice(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Menu.objects.create(event_day=other_day, name="Collation étrangère", meal_type=Menu.MealType.SNACK)
        inactive = Menu.objects.create(name="Collation inactive", meal_type=Menu.MealType.SNACK, is_active=False)
        for snack in (self.main, foreign, inactive):
            with self.subTest(snack=snack), self.assertRaises(BusinessRuleError):
                self.order(morning_snack=snack)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=self.reg, morning_snack=self.snack)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=self.reg, no_meal=True, afternoon_snack=self.snack)
        self.reg.availability = Registration.Availability.MORNING
        self.reg.save()
        with self.assertRaises(BusinessRuleError):
            self.order(menu=self.snack)
        valid_half = save_meal_order(registration=self.reg, menu=self.snack)
        self.assertEqual(valid_half.total_surcharge_cents, 150)

    def test_preserved_inactive_extra_is_allowed_but_not_newly_selected_in_another_component(self):
        self.order(morning_snack=self.snack, afternoon_snack=None)
        self.snack.is_active = False
        self.snack.save()
        preserved = self.order(morning_snack=self.snack, afternoon_snack=None)
        self.assertEqual(preserved.morning_snack_id, self.snack.pk)
        with self.assertRaises(BusinessRuleError):
            self.order(morning_snack=None, afternoon_snack=self.snack)

    def test_menu_type_change_cannot_invalidate_either_extra_collation(self):
        for morning in (True, False):
            self.order(morning_snack=self.snack if morning else None, afternoon_snack=None if morning else self.snack)
            self.snack.meal_type = Menu.MealType.FULL_MEAL
            with self.subTest(morning=morning), self.assertRaises(ValidationError):
                self.snack.full_clean()
            self.snack.refresh_from_db()
            self.assertEqual(self.snack.meal_type, Menu.MealType.SNACK)

    def test_full_to_half_day_with_open_choices_removes_extras_and_updates_total_atomically(self):
        self.order()
        save_participation(
            member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
            actor=self.family_user, meal_choices={"menu": self.snack, "no_meal": False},
        )
        order = MealOrder.objects.get(registration=self.reg)
        self.assertIsNone(order.morning_snack_id)
        self.assertIsNone(order.afternoon_snack_id)
        self.assertEqual(order.morning_snack_standard_surcharge_cents, 0)
        self.assertEqual(order.afternoon_snack_surcharge_cents, 0)
        self.assertEqual(order.total_surcharge_cents, 150)

    def test_invalid_half_day_main_choice_rolls_back_extra_removal_and_presence(self):
        original = self.order()
        with self.assertRaises(BusinessRuleError):
            save_participation(
                member=self.member, event_day=self.day, availability=Registration.Availability.MORNING,
                actor=self.family_user, meal_choices={"menu": self.main, "no_meal": False},
            )
        self.reg.refresh_from_db()
        original.refresh_from_db()
        self.assertEqual(self.reg.availability, Registration.Availability.FULL_DAY)
        self.assertEqual(original.morning_snack_id, self.snack.pk)
        self.assertEqual(original.afternoon_snack_id, self.snack.pk)
        self.assertEqual(original.total_surcharge_cents, 900)

    def test_partial_model_save_does_not_let_forged_extra_snapshots_change_prices(self):
        order = self.order()
        order.morning_snack_standard_surcharge_cents = 1
        order.afternoon_snack_surcharge_cents = 0
        order.save(update_fields=("payment_method",))
        order.refresh_from_db()
        self.assertEqual(order.morning_snack_standard_surcharge_cents, 150)
        self.assertEqual(order.afternoon_snack_surcharge_cents, 150)
        self.assertEqual(order.total_surcharge_cents, 900)


@skipUnless(connection.vendor == "postgresql", "Nécessite les verrous PostgreSQL.")
class ConcurrentSharedMealTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.main_a = Menu.objects.create(name="Repas A", surcharge_cents=300)
        self.main_b = Menu.objects.create(name="Repas B", surcharge_cents=400)
        self.snack_a = Menu.objects.create(name="Collation A", meal_type=Menu.MealType.SNACK, surcharge_cents=100)
        self.snack_b = Menu.objects.create(name="Collation B", meal_type=Menu.MealType.SNACK, surcharge_cents=200)
        self.reg_a = self.registration()
        second_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        self.reg_b = Registration.objects.create(member=self.other_member, event_day=second_day, availability=Registration.Availability.FULL_DAY)

    def test_shared_snacks_selected_in_opposite_components_on_different_days_do_not_deadlock(self):
        barrier = Barrier(2)

        def action(second):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                registration = Registration.objects.get(pk=self.reg_b.pk if second else self.reg_a.pk)
                menu = Menu.objects.get(pk=self.main_b.pk if second else self.main_a.pk)
                morning = Menu.objects.get(pk=self.snack_b.pk if second else self.snack_a.pk)
                afternoon = Menu.objects.get(pk=self.snack_a.pk if second else self.snack_b.pk)
                barrier.wait(timeout=5)
                return save_meal_order(registration=registration, menu=menu, morning_snack=morning, afternoon_snack=afternoon).total_surcharge_cents
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(action, second) for second in (False, True)]
            results = [future.result(timeout=15) for future in futures]
        self.assertCountEqual(results, (600, 700))
        self.assertEqual(MealOrder.objects.count(), 2)
