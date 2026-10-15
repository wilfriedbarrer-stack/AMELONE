from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import patch

from django.apps import apps
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from volunteers.models import Beverage, EventDay, MealOrder, Member, Menu, Payment, Registration
from volunteers.services import (
    BusinessRuleError,
    cancel_registration,
    family_event_balance,
    save_meal_order,
    save_registration,
)

from .factories import DataMixin


class OfficialPricingTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()
        self.menu = Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=500)
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=150)

    def order(self, **kwargs):
        return save_meal_order(
            registration=self.reg, menu=kwargs.pop("menu", self.menu),
            beverage=kwargs.pop("beverage", self.drink), **kwargs,
        )

    def official(self, enabled=True):
        self.member.is_competition_official = enabled
        self.member.save(update_fields=("is_competition_official", "updated_at"))

    def test_official_menu_and_beverage_are_free_but_keep_their_tariffs(self):
        self.official()
        order = self.order(actor=self.family_user)
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.menu_surcharge_cents, 0)
        self.assertEqual(order.beverage_surcharge_cents, 0)
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(order.menu_standard_surcharge_cents, 500)
        self.assertEqual(order.beverage_standard_surcharge_cents, 150)
        self.assertEqual(self.menu.surcharge_cents, 500)
        self.assertEqual(family_event_balance(family=self.family, event=self.event)["due_cents"], 0)

    def test_actor_user_role_or_stale_member_flag_never_exempts_the_participant(self):
        self.organizer.is_competition_official = True
        self.organizer.is_superuser = True
        self.organizer.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.reg.member.is_competition_official = True
        order = self.order(actor=self.organizer)
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 650)
        order.official_exemption_applied = True
        order.menu_surcharge_cents = 0
        order.menu_standard_surcharge_cents = 0
        order.beverage_standard_surcharge_cents = 0
        order.registration.member.is_competition_official = True
        order.save()
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 650)

    def test_one_official_does_not_exempt_another_member_of_the_same_family(self):
        sibling = Member.objects.create(
            family=self.family, first_name="Adèle", last_name="Durand", birth_date=self.member.birth_date,
        )
        sibling_reg = self.registration(member=sibling)
        self.official()
        self.order()
        sibling_order = save_meal_order(registration=sibling_reg, menu=self.menu, beverage=self.drink)
        self.assertFalse(sibling_order.official_exemption_applied)
        self.assertEqual(sibling_order.total_surcharge_cents, 650)
        self.assertEqual(family_event_balance(family=self.family, event=self.event)["due_cents"], 650)

    def test_grant_and_withdrawal_restore_original_prices_without_touching_payment(self):
        order = self.order()
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=650, method=Payment.Method.SUMUP,
        )
        payment_snapshot = Payment.objects.filter(pk=payment.pk).values().get()
        self.menu.surcharge_cents = 999
        self.menu.save()
        self.drink.surcharge_cents = 333
        self.drink.save()
        self.official()
        order.refresh_from_db()
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)
        balance = family_event_balance(family=self.family, event=self.event)
        self.assertEqual(balance["status"], "overpaid")
        self.assertEqual(balance["overpayment_cents"], 650)
        order = self.order()
        self.assertEqual(order.menu_standard_surcharge_cents, 500)
        self.assertEqual(order.beverage_standard_surcharge_cents, 150)
        self.official(False)
        order.refresh_from_db()
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.menu_surcharge_cents, 500)
        self.assertEqual(order.beverage_surcharge_cents, 150)
        self.assertEqual(order.total_surcharge_cents, 650)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), payment_snapshot)
        self.assertEqual(Payment.objects.count(), 1)

    def test_option_change_while_exempted_takes_new_snapshot_only_for_changed_component(self):
        self.official()
        self.order()
        self.drink.surcharge_cents = 999
        self.drink.save()
        second_menu = Menu.objects.create(event_day=self.day, name="Autre", surcharge_cents=800)
        order = self.order(menu=second_menu)
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(order.menu_standard_surcharge_cents, 800)
        self.assertEqual(order.beverage_standard_surcharge_cents, 150)
        self.official(False)
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 950)

    def test_non_official_snapshots_stay_unchanged_and_do_not_accept_forged_prices(self):
        self.order()
        self.menu.surcharge_cents = 999
        self.menu.save()
        order = self.order()
        order.menu_standard_surcharge_cents = 1
        order.beverage_standard_surcharge_cents = 2
        order.save(update_fields=("payment_method",))
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 650)
        self.assertEqual(order.menu_standard_surcharge_cents, 500)

    def test_partial_save_restores_omitted_choices_from_locked_not_initial_snapshot(self):
        order = self.order()
        second = Menu.objects.create(event_day=self.day, name="Second menu", surcharge_cents=900)
        original_select_for_update = Member.objects.select_for_update
        changed = False

        def concurrent_change(*args, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                MealOrder.objects.filter(pk=order.pk).update(
                    menu=second, menu_standard_surcharge_cents=900,
                    menu_surcharge_cents=900, total_surcharge_cents=1050,
                )
            return original_select_for_update(*args, **kwargs)

        order.payment_method = MealOrder.PaymentMethod.SUMUP
        with patch.object(Member.objects, "select_for_update", side_effect=concurrent_change):
            order.save(update_fields=("payment_method",))
        order.refresh_from_db()
        self.assertEqual(order.payment_method, MealOrder.PaymentMethod.SUMUP)
        self.assertEqual(order.menu_id, second.pk)
        self.assertEqual(order.menu_standard_surcharge_cents, 900)
        self.assertEqual(order.menu_surcharge_cents, 900)
        self.assertEqual(order.total_surcharge_cents, 1050)

    def test_no_meal_clears_snapshot_and_can_be_chosen_while_exempted(self):
        self.official()
        self.order()
        order = save_meal_order(registration=self.reg, no_meal=True)
        self.assertTrue(order.official_exemption_applied)
        self.assertFalse(order.wants_meal)
        self.assertIsNone(order.menu_id)
        self.assertEqual(order.menu_standard_surcharge_cents, 0)
        self.assertEqual(order.beverage_standard_surcharge_cents, 0)
        self.official(False)
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_today_is_exempted_and_past_orders_stay_historical_on_resave(self):
        today = timezone.localdate()
        self.day.date = today
        self.day.save()
        current = self.order()
        past_day = EventDay.objects.create(event=self.event, date=today - timedelta(days=1))
        past_reg = Registration.objects.create(
            member=self.member, event_day=past_day, availability=Registration.Availability.FULL_DAY,
        )
        past_menu = Menu.objects.create(event_day=past_day, name="Passé", surcharge_cents=700)
        past = save_meal_order(registration=past_reg, menu=past_menu)
        self.official()
        current.refresh_from_db()
        past.refresh_from_db()
        self.assertEqual(current.total_surcharge_cents, 0)
        self.assertEqual(past.total_surcharge_cents, 700)
        self.assertFalse(past.official_exemption_applied)
        past.save()
        self.assertEqual(past.total_surcharge_cents, 700)
        self.assertFalse(past.official_exemption_applied)

    def test_past_exempt_order_stays_free_after_withdrawal_and_resave(self):
        self.official()
        order = self.order()
        self.day.date = timezone.localdate() - timedelta(days=1)
        self.day.save()
        self.official(False)
        order.refresh_from_db()
        order.save()
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_cancelled_orders_are_historical_but_reactivation_uses_current_habilitation(self):
        order = self.order()
        cancel_registration(registration=self.reg)
        self.official()
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 650)
        self.assertFalse(order.official_exemption_applied)
        save_registration(
            member=self.member, event_day=self.day,
            availability=Registration.Availability.FULL_DAY,
        )
        order.refresh_from_db()
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_cancelled_free_order_is_charged_again_when_reactivated_without_habilitation(self):
        self.official()
        order = self.order()
        cancel_registration(registration=self.reg)
        self.official(False)
        order.refresh_from_db()
        order.save()
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)
        save_registration(
            member=self.member, event_day=self.day,
            availability=Registration.Availability.FULL_DAY,
        )
        order.refresh_from_db()
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 650)

    def test_foreign_day_inactive_options_and_wrong_meal_type_still_refused(self):
        self.official()
        wrong_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Menu.objects.create(event_day=wrong_day, name="Autre journée", surcharge_cents=800)
        snack = Menu.objects.create(event_day=self.day, name="Collation", meal_type=Menu.MealType.SNACK)
        for menu in (foreign, snack):
            with self.assertRaises(BusinessRuleError):
                self.order(menu=menu)
        self.menu.is_active = False
        self.menu.save()
        with self.assertRaises(BusinessRuleError):
            self.order()
        self.assertFalse(MealOrder.objects.exists())

    def test_direct_model_save_uses_current_database_member_not_cached_role(self):
        self.official()
        self.reg.member.is_competition_official = False
        order = MealOrder.objects.create(
            registration=self.reg, wants_meal=True, menu=self.menu, beverage=self.drink,
        )
        self.assertTrue(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertEqual(order.menu_standard_surcharge_cents, 500)

    def test_unrelated_member_update_does_not_apply_a_stale_official_flag(self):
        order = self.order()
        self.member.is_competition_official = True
        self.member.first_name = "Léna"
        self.member.save(update_fields=("first_name",))
        order.refresh_from_db()
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.total_surcharge_cents, 650)

    def test_legacy_type_exception_and_prices_survive_grant_and_withdrawal(self):
        order = self.order()
        Registration.objects.filter(pk=self.reg.pk).update(availability=Registration.Availability.MORNING)
        MealOrder.objects.filter(pk=order.pk).update(
            legacy_availability="morning", beverage_availability="morning",
        )
        self.official()
        order = self.order()
        self.assertEqual(order.legacy_availability, "morning")
        self.assertEqual(order.total_surcharge_cents, 0)
        self.official(False)
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 650)
        with self.assertRaises(BusinessRuleError):
            different_full = Menu.objects.create(event_day=self.day, name="Nouveau repas", surcharge_cents=900)
            self.order(menu=different_full)

    def test_migration_preserves_reserved_prices_and_exempts_only_current_active_members(self):
        current = self.order()
        cancelled = self.registration(member=self.other_member, status=Registration.Status.CANCELLED)
        cancelled_order = MealOrder.objects.create(registration=cancelled, wants_meal=True, menu=self.menu)
        past_day = EventDay.objects.create(event=self.event, date=timezone.localdate() - timedelta(days=1))
        past_reg = Registration.objects.create(
            member=self.member, event_day=past_day, availability=Registration.Availability.FULL_DAY,
        )
        past_menu = Menu.objects.create(event_day=past_day, name="Historique", surcharge_cents=700)
        past = MealOrder.objects.create(registration=past_reg, wants_meal=True, menu=past_menu)
        Member.objects.filter(pk__in=(self.member.pk, self.other_member.pk)).update(is_competition_official=True)
        MealOrder.objects.all().update(menu_standard_surcharge_cents=None, beverage_standard_surcharge_cents=None)
        self.menu.surcharge_cents = 999
        self.menu.save()
        Payment.objects.create(family=self.family, event=self.event, amount_cents=650, method=Payment.Method.SUMUP)
        migrate = import_module("volunteers.migrations.0007_official_meal_surcharge_exemption")
        migrate.exempt_current_official_orders(apps, SimpleNamespace(connection=SimpleNamespace(alias="default")))
        current.refresh_from_db()
        past.refresh_from_db()
        cancelled_order.refresh_from_db()
        self.assertEqual(current.total_surcharge_cents, 0)
        self.assertEqual(current.menu_standard_surcharge_cents, 500)
        self.assertEqual(current.beverage_standard_surcharge_cents, 150)
        self.assertEqual(past.total_surcharge_cents, 700)
        self.assertFalse(past.official_exemption_applied)
        self.assertEqual(cancelled_order.total_surcharge_cents, 500)
        self.assertFalse(cancelled_order.official_exemption_applied)
        self.assertEqual(Payment.objects.get().amount_cents, 650)


class ConcurrentOfficialPricingTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.member.is_competition_official = True
        self.member.save()
        self.reg = self.registration()
        self.menu = Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=500)
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=150)
        self.order = save_meal_order(registration=self.reg, menu=self.menu, beverage=self.drink)
        self.second_menu = Menu.objects.create(event_day=self.day, name="Autre menu", surcharge_cents=900)

    def test_concurrent_withdrawal_and_meal_edit_use_final_member_state_without_deadlock(self):
        barrier = Barrier(2)

        def action(withdraw):
            close_old_connections()
            try:
                member = Member.objects.get(pk=self.member.pk)
                registration = Registration.objects.get(pk=self.reg.pk)
                menu = Menu.objects.get(pk=self.second_menu.pk)
                beverage = Beverage.objects.get(pk=self.drink.pk)
                barrier.wait(timeout=5)
                if withdraw:
                    member.is_competition_official = False
                    member.save(update_fields=("is_competition_official",))
                else:
                    save_meal_order(registration=registration, menu=menu, beverage=beverage)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(action, (False, True)))
        self.member.refresh_from_db()
        self.order.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)
        self.assertFalse(self.order.official_exemption_applied)
        self.assertEqual(self.order.menu_id, self.second_menu.pk)
        self.assertEqual(self.order.total_surcharge_cents, 1050)
        self.assertEqual(self.order.menu_standard_surcharge_cents, 900)
