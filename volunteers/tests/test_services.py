from concurrent.futures import ThreadPoolExecutor
from datetime import date

from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from volunteers.models import (
    Assignment,
    AssignmentCommunication,
    Beverage,
    EmailAttempt,
    EmailDelivery,
    Event,
    Family,
    Member,
    Menu,
    Payment,
    Registration,
    StationSlot,
    User,
)
from volunteers.services import (
    BusinessRuleError,
    birthday_alerts_for_event,
    build_assignment_email_preview,
    cancel_registration,
    confirm_assignment,
    create_assignment,
    family_event_balance,
    prepare_assignment_delivery,
    record_payment,
    save_meal_order,
    save_registration,
    send_assignment_delivery,
)

from .factories import DataMixin


class RegistrationAndAssignmentServicesTests(DataMixin, TestCase):
    def test_family_cannot_modify_another_family(self):
        with self.assertRaises(PermissionDenied):
            save_registration(
                member=self.other_member,
                event_day=self.day,
                availability=Registration.Availability.MORNING,
                actor=self.family_user,
            )

    def test_closed_registration_blocks_family_but_not_organizer(self):
        self.event.registration_closes_at = timezone.now() - timezone.timedelta(minutes=1)
        self.event.save(update_fields=["registration_closes_at"])
        with self.assertRaises(BusinessRuleError):
            save_registration(
                member=self.member,
                event_day=self.day,
                availability=Registration.Availability.MORNING,
                actor=self.family_user,
            )
        result = save_registration(
            member=self.member,
            event_day=self.day,
            availability=Registration.Availability.MORNING,
            actor=self.organizer,
        )
        self.assertEqual(result.availability, Registration.Availability.MORNING)

    def test_capacity_and_double_assignment_are_enforced(self):
        first = self.registration()
        second = self.registration(member=self.other_member)
        create_assignment(registration=first, station_slot=self.morning_slot, actor=self.organizer)
        with self.assertRaises(BusinessRuleError):
            create_assignment(registration=second, station_slot=self.morning_slot, actor=self.organizer)
        other_slot = StationSlot.objects.create(
            station=self.other_station,
            event_day=self.day,
            period=StationSlot.Period.AFTERNOON,
            capacity=3,
        )
        create_assignment(registration=first, station_slot=other_slot, actor=self.organizer)
        with self.assertRaises(BusinessRuleError):
            create_assignment(registration=first, station_slot=self.afternoon_slot, actor=self.organizer)

    def test_availability_reduction_cancels_incompatible_assignment(self):
        registration = self.registration()
        assignment = create_assignment(
            registration=registration, station_slot=self.afternoon_slot, actor=self.organizer
        )
        updated = save_registration(
            member=self.member,
            event_day=self.day,
            availability=Registration.Availability.MORNING,
            actor=self.family_user,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Assignment.Status.CANCELLED)
        self.assertTrue(updated.reassignment_required)
        self.assertEqual(len(updated.invalidated_assignments), 1)


class MealPaymentAndBirthdayTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()
        self.menu = Menu.objects.create(
            event_day=self.day, name="Végétarien", surcharge_cents=500
        )
        self.drink = Beverage.objects.create(
            event_day=self.day, name="Jus", surcharge_cents=150
        )

    def test_meal_deadline_and_organizer_override(self):
        self.event.meal_orders_close_at = timezone.now() - timezone.timedelta(minutes=1)
        self.event.save(update_fields=["meal_orders_close_at"])
        with self.assertRaises(BusinessRuleError):
            save_meal_order(
                registration=self.reg,
                menu=self.menu,
                beverage=self.drink,
                actor=self.family_user,
            )
        order = save_meal_order(
            registration=self.reg,
            menu=self.menu,
            beverage=self.drink,
            actor=self.organizer,
        )
        self.assertEqual(order.total_surcharge_cents, 650)

    def test_applied_prices_do_not_change_silently(self):
        order = save_meal_order(
            registration=self.reg,
            menu=self.menu,
            beverage=self.drink,
            actor=self.family_user,
        )
        self.menu.surcharge_cents = 900
        self.menu.save(update_fields=["surcharge_cents"])
        order = save_meal_order(
            registration=self.reg,
            menu=self.menu,
            beverage=self.drink,
            actor=self.family_user,
        )
        self.assertEqual(order.menu_surcharge_cents, 500)
        self.assertEqual(order.total_surcharge_cents, 650)

    def test_partial_paid_and_overpaid_balances(self):
        save_meal_order(registration=self.reg, menu=self.menu, actor=self.family_user)
        partial = record_payment(
            family=self.family,
            event=self.event,
            amount_cents=200,
            method=Payment.Method.ON_SITE,
            recorded_by=self.organizer,
        )
        self.assertEqual(partial.balance_after_payment["status"], "partial")
        record_payment(
            family=self.family,
            event=self.event,
            amount_cents=400,
            method=Payment.Method.SUMUP,
            recorded_by=self.organizer,
        )
        balance = family_event_balance(family=self.family, event=self.event)
        self.assertEqual(balance["status"], "overpaid")
        self.assertEqual(balance["overpayment_cents"], 100)

    def test_birthday_alert_only_for_registered_day(self):
        alerts = birthday_alerts_for_event(self.event)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["age"], 27)
        self.assertIn("Anniversaire de Lina Durand", alerts[0]["message"])


class EmailServiceTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()
        self.assignment = create_assignment(
            registration=self.reg, station_slot=self.morning_slot, actor=self.organizer
        )
        confirm_assignment(assignment=self.assignment, confirmed_by=self.organizer)

    def test_disabled_email_never_marks_message_sent(self):
        delivery = prepare_assignment_delivery(
            event=self.event, family=self.family, actor=self.organizer
        )
        with self.assertRaises(BusinessRuleError):
            send_assignment_delivery(delivery=delivery, actor=self.organizer)
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, EmailDelivery.Status.DRAFT)
        self.assertEqual(EmailAttempt.objects.count(), 0)

    @override_settings(
        EMAIL_ENABLED=True,
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        SITE_URL="https://amelone.example.test",
    )
    def test_email_snapshot_and_update_to_communicate(self):
        delivery = prepare_assignment_delivery(
            event=self.event, family=self.family, actor=self.organizer
        )
        sent = send_assignment_delivery(delivery=delivery, actor=self.organizer)
        self.assertEqual(sent.status, EmailDelivery.Status.ACCEPTED)
        self.assertEqual(AssignmentCommunication.objects.count(), 1)
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.needs_communication)
        self.station.name = "Accueil déplacé"
        self.station.save(update_fields=["name"])
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.needs_communication)
        preview = build_assignment_email_preview(event=self.event, family=self.family)
        self.assertEqual(preview["kind"], EmailDelivery.Kind.ASSIGNMENT_UPDATE)
        update = prepare_assignment_delivery(
            event=self.event, family=self.family, actor=self.organizer
        )
        send_assignment_delivery(delivery=update, actor=self.organizer)
        cancel_registration(registration=self.reg, actor=self.family_user)
        withdrawal_preview = build_assignment_email_preview(
            event=self.event, family=self.family
        )
        self.assertIn("AFFECTATION RETIRÉE", withdrawal_preview["body_text"])
        withdrawal = prepare_assignment_delivery(
            event=self.event, family=self.family, actor=self.organizer
        )
        send_assignment_delivery(delivery=withdrawal, actor=self.organizer)
        self.assertEqual(
            list(
                AssignmentCommunication.objects.filter(assignment=self.assignment)
                .order_by("version")
                .values_list("version", flat=True)
            ),
            [1, 2, 3],
        )


class ConcurrentCapacityTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        now = timezone.now()
        self.organizer = User.objects.create_user(
            email="thread-orga@example.test", password="x", is_organizer=True
        )
        self.event = Event.objects.create(
            name="Concurrence",
            location="Salle",
            registration_opens_at=now - timezone.timedelta(days=1),
            registration_closes_at=now + timezone.timedelta(days=1),
            meal_orders_close_at=now + timezone.timedelta(days=1),
            status=Event.Status.PUBLISHED,
        )
        from volunteers.models import EventDay, Station

        day = EventDay.objects.create(event=self.event, date=date(2027, 6, 1))
        station = Station.objects.create(event=self.event, name="Unique")
        self.slot = StationSlot.objects.create(
            station=station,
            event_day=day,
            period=StationSlot.Period.MORNING,
            capacity=1,
        )
        self.registrations = []
        for index in range(2):
            user = User.objects.create_user(email=f"thread-{index}@example.test", password="x")
            family = Family.objects.create(account=user, name=f"F{index}")
            member = Member.objects.create(
                family=family,
                first_name=f"M{index}",
                last_name="Test",
                birth_date=date(1990, 1, 1),
            )
            self.registrations.append(
                Registration.objects.create(
                    member=member,
                    event_day=day,
                    availability=Registration.Availability.MORNING,
                )
            )

    def _assign(self, registration_id):
        close_old_connections()
        try:
            create_assignment(
                registration=Registration.objects.get(pk=registration_id),
                station_slot=StationSlot.objects.get(pk=self.slot.pk),
                actor=User.objects.get(pk=self.organizer.pk),
            )
            return "created"
        except BusinessRuleError:
            return "rejected"
        finally:
            connections.close_all()

    def test_simultaneous_capacity_is_serialized(self):
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(self._assign, [item.pk for item in self.registrations]))
        self.assertCountEqual(results, ["created", "rejected"])
        self.assertEqual(
            Assignment.objects.filter(
                station_slot=self.slot,
                status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
            ).count(),
            1,
        )

