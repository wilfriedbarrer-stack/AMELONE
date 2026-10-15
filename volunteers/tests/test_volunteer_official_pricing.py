from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace
from unittest import skipUnless

from django.apps import apps
from django.db import close_old_connections, connection, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Beverage,
    Event,
    EventDay,
    MealOrder,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
)
from volunteers.official_pricing import registration_has_official_meal_exemption
from volunteers.services import (
    cancel_registration,
    create_assignment,
    family_event_balance,
    remove_assignment,
    save_meal_order,
    save_registration,
    update_assignment,
)

from .factories import DataMixin


class OfficialVolunteerPricingTests(DataMixin, TestCase):
    def setUp(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=("is_competition_official",))
        self.reg = self.registration()
        self.menu = Menu.objects.create(name="Repas", surcharge_cents=500)
        self.drink = Beverage.objects.create(name="Jus", surcharge_cents=100)
        self.snack = Menu.objects.create(name="Goûter", meal_type=Menu.MealType.SNACK, surcharge_cents=150)
        self.order = save_meal_order(
            registration=self.reg, menu=self.menu, beverage=self.drink,
            morning_snack=self.snack, afternoon_snack=self.snack,
        )
        self.official_station = Station.objects.create(
            event=self.event, name="Juge", category=Station.Category.OFFICIAL,
        )
        self.official_slot = StationSlot.objects.create(
            station=self.official_station, event_day=self.day,
            period=StationSlot.Period.MORNING, capacity=2,
        )

    def assert_price(self, cents, *, exempt=False):
        self.order.refresh_from_db()
        self.assertEqual(self.order.total_surcharge_cents, cents)
        self.assertEqual(self.order.official_exemption_applied, exempt)

    def test_volunteer_draft_immediately_restores_all_reserved_components(self):
        self.assert_price(0, exempt=True)
        self.menu.surcharge_cents = 999
        self.menu.save()
        assignment = create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.assertEqual(assignment.status, Assignment.Status.DRAFT)
        self.assert_price(900)
        self.assertEqual(self.order.menu_surcharge_cents, 500)
        self.assertEqual(self.order.beverage_surcharge_cents, 100)
        self.assertEqual(self.order.morning_snack_surcharge_cents, 150)
        self.assertEqual(self.order.afternoon_snack_surcharge_cents, 150)

    def test_mixed_official_and_volunteer_posts_charge_the_whole_day(self):
        create_assignment(registration=self.reg, station_slot=self.official_slot)
        self.assert_price(0, exempt=True)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        self.assert_price(900)

    def test_actual_official_post_overrides_obsolete_volunteer_preference(self):
        self.reg.preferred_station = self.station
        self.reg.save(update_fields=("preferred_station",))
        self.assert_price(900)
        create_assignment(registration=self.reg, station_slot=self.official_slot)
        self.assert_price(0, exempt=True)

    def test_direct_registration_save_and_service_change_preference_price_immediately(self):
        self.reg.preferred_station = self.station
        self.reg.save(update_fields=("preferred_station",))
        self.assert_price(900)
        save_registration(
            member=self.member, event_day=self.day, availability=Registration.Availability.FULL_DAY,
            preferred_station=self.official_station,
        )
        self.assert_price(0, exempt=True)

    def test_direct_assignment_save_and_move_reconcile_without_a_meal_save(self):
        assignment = Assignment.objects.create(registration=self.reg, station_slot=self.morning_slot)
        self.assert_price(900)
        update_assignment(assignment=assignment, station_slot=self.official_slot)
        self.assert_price(0, exempt=True)

    def test_cancelled_volunteer_assignment_does_not_remove_official_exemption(self):
        official = create_assignment(registration=self.reg, station_slot=self.official_slot)
        volunteer = create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        self.assert_price(900)
        remove_assignment(assignment=volunteer)
        self.assert_price(0, exempt=True)
        remove_assignment(assignment=official)
        self.assert_price(0, exempt=True)

    def test_removing_last_actual_assignment_returns_to_volunteer_preference(self):
        self.reg.preferred_station = self.station
        self.reg.save()
        official = create_assignment(registration=self.reg, station_slot=self.official_slot)
        self.assert_price(0, exempt=True)
        remove_assignment(assignment=official)
        self.assert_price(900)

    def test_other_day_of_same_member_stays_exempt(self):
        day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        registration = Registration.objects.create(
            member=self.member, event_day=day, availability=Registration.Availability.FULL_DAY,
        )
        other_order = save_meal_order(registration=registration, menu=self.menu)
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.assert_price(900)
        other_order.refresh_from_db()
        self.assertEqual(other_order.total_surcharge_cents, 0)
        self.assertTrue(other_order.official_exemption_applied)

    def test_station_category_change_reprices_assignments_and_preferences(self):
        create_assignment(registration=self.reg, station_slot=self.official_slot)
        self.official_station.category = Station.Category.VOLUNTEER
        self.official_station.save(update_fields=("category",))
        self.assert_price(900)
        self.official_station.category = Station.Category.OFFICIAL
        self.official_station.save(update_fields=("category",))
        self.assert_price(0, exempt=True)
        remove_assignment(assignment=self.reg.assignments.get())
        self.reg.preferred_station = self.official_station
        self.reg.save()
        self.official_station.category = Station.Category.VOLUNTEER
        self.official_station.save()
        self.assert_price(900)

    def test_granting_role_does_not_exempt_existing_volunteer_assignment(self):
        self.member.is_competition_official = False
        self.member.save()
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.member.is_competition_official = True
        self.member.save()
        self.assert_price(900)

    def test_payments_are_never_changed_and_family_balance_is_updated(self):
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=300, method=Payment.Method.ON_SITE,
        )
        before = Payment.objects.filter(pk=payment.pk).values().get()
        assignment = create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.assertEqual(family_event_balance(family=self.family, event=self.event)["amount_remaining_cents"], 600)
        remove_assignment(assignment=assignment)
        self.assertEqual(family_event_balance(family=self.family, event=self.event)["overpayment_cents"], 300)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), before)

    def test_cancelled_past_and_archived_orders_keep_their_financial_history(self):
        cancel_registration(registration=self.reg)
        self.reg.refresh_from_db()
        self.reg.preferred_station = self.station
        self.reg.save()
        self.assert_price(0, exempt=True)
        self.reg = save_registration(
            member=self.member, event_day=self.day,
            availability=Registration.Availability.FULL_DAY, preferred_station=self.station,
        )
        self.assert_price(900)
        self.reg.preferred_station = None
        self.reg.save()
        self.assert_price(0, exempt=True)
        self.event.status = Event.Status.ARCHIVED
        self.event.save()
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.assert_price(0, exempt=True)
        self.event.status = Event.Status.PUBLISHED
        self.event.save()
        self.day.date = timezone.localdate() - timedelta(days=1)
        self.day.save()
        self.order.save()
        self.assert_price(0, exempt=True)

    def test_preview_can_simulate_selected_slots_before_they_are_saved(self):
        self.assertFalse(registration_has_official_meal_exemption(
            self.reg, station_slots=[self.official_slot, self.afternoon_slot],
        ))
        self.assertTrue(registration_has_official_meal_exemption(
            self.reg, preferred_station=self.station, station_slots=[self.official_slot],
        ))
        self.assertFalse(registration_has_official_meal_exemption(
            self.reg, preferred_station=self.station, station_slots=[],
        ))

    def test_migration_restores_future_volunteer_snapshots_only_and_is_idempotent(self):
        # Simulate the preceding server version, which exempted every official.
        Assignment.objects.bulk_create([
            Assignment(registration=self.reg, station_slot=self.morning_slot, period=self.morning_slot.period),
        ])
        migration = import_module("volunteers.migrations.0010_official_volunteer_meal_pricing")
        editor = SimpleNamespace(connection=connection)
        migration.restore_volunteer_prices(apps, editor)
        self.assert_price(900)
        before = MealOrder.objects.values().get(pk=self.order.pk)
        migration.restore_volunteer_prices(apps, editor)
        self.assertEqual(MealOrder.objects.values().get(pk=self.order.pk), before)

    def test_migration_preserves_cancelled_past_and_archived_orders_and_payments(self):
        self.other_member.is_competition_official = True
        self.other_member.save()
        cancelled = self.registration(member=self.other_member, preferred_station=self.station)
        cancelled_order = save_meal_order(registration=cancelled, menu=self.menu)
        # Prior server's exempt snapshot, preserved after cancellation.
        MealOrder.objects.filter(pk=cancelled_order.pk).update(
            official_exemption_applied=True, menu_surcharge_cents=0, total_surcharge_cents=0,
        )
        Registration.objects.filter(pk=cancelled.pk).update(status=Registration.Status.CANCELLED)
        past_day = EventDay.objects.create(event=self.event, date=timezone.localdate() - timedelta(days=1))
        past_reg = Registration.objects.create(
            member=self.member, event_day=past_day, availability=Registration.Availability.FULL_DAY,
            preferred_station=self.station,
        )
        past_order = save_meal_order(registration=past_reg, menu=self.menu)
        archived_event = Event.objects.create(
            name="Archive", registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at, status=Event.Status.ARCHIVED,
        )
        archived_day = EventDay.objects.create(event=archived_event, date=self.day.date)
        archived_station = Station.objects.create(event=archived_event, name="Buvette archivée")
        archived_reg = Registration.objects.create(
            member=self.member, event_day=archived_day, availability=Registration.Availability.FULL_DAY,
            preferred_station=archived_station,
        )
        archived_order = save_meal_order(registration=archived_reg, menu=self.menu, actor=self.organizer)
        order_ids = [cancelled_order.pk, past_order.pk, archived_order.pk]
        before = list(MealOrder.objects.filter(pk__in=order_ids).values().order_by("pk"))
        Payment.objects.create(
            family=self.family, event=self.event, amount_cents=900, method=Payment.Method.SUMUP,
        )
        payments = list(Payment.objects.values().order_by("pk"))
        migration = import_module("volunteers.migrations.0010_official_volunteer_meal_pricing")
        migration.restore_volunteer_prices(apps, SimpleNamespace(connection=connection))
        self.assertEqual(list(MealOrder.objects.filter(pk__in=order_ids).values().order_by("pk")), before)
        self.assertEqual(list(Payment.objects.values().order_by("pk")), payments)


@skipUnless(connection.vendor == "postgresql", "Verrous PostgreSQL nécessaires")
class ConcurrentOfficialVolunteerPricingTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.member.is_competition_official = True
        self.member.save()
        self.reg = self.registration()
        self.menu = Menu.objects.create(name="Repas initial", surcharge_cents=500)
        self.other_menu = Menu.objects.create(name="Nouveau repas", surcharge_cents=800)
        self.order = save_meal_order(registration=self.reg, menu=self.menu)

    def test_assignment_and_meal_edit_finish_with_paid_volunteer_price_without_deadlock(self):
        barrier = Barrier(2)

        def action(assign):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                registration = Registration.objects.get(pk=self.reg.pk)
                barrier.wait(timeout=5)
                if assign:
                    create_assignment(registration=registration, station_slot=self.morning_slot)
                else:
                    save_meal_order(registration=registration, menu=self.other_menu)
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(action, assign) for assign in (True, False)]
            for future in futures:
                future.result(timeout=15)
        self.order.refresh_from_db()
        self.assertEqual(self.order.menu_id, self.other_menu.pk)
        self.assertEqual(self.order.total_surcharge_cents, 800)
        self.assertFalse(self.order.official_exemption_applied)
