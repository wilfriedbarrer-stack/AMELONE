from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event as ThreadEvent
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import close_old_connections, connection, connections
from django.db.models.deletion import ProtectedError
from django.test import TestCase, TransactionTestCase

from volunteers import deletion
from volunteers.deletion import (
    delete_event_or_archive,
    delete_reusable_event_profile,
    get_event_deletion_summary,
)
from volunteers.eligibility import validate_adult_coverage_for_confirmation
from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import (
    Assignment,
    Attendance,
    Beverage,
    EmailAttempt,
    EmailDelivery,
    Event,
    EventDay,
    EventProfile,
    MealOrder,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
    User,
)
from volunteers.profile_editor import (
    delete_profile_station,
    save_profile_station,
    update_reusable_profile,
)
from volunteers.services import confirm_assignment, create_assignment, save_meal_order

from .factories import DataMixin


class DeletionServicesTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=500)
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=100)

    def delete_event(self, **changes):
        arguments = {
            "event": self.event, "actor": self.organizer,
            "expected_updated_at": self.event.updated_at.isoformat(),
        }
        arguments.update(changes)
        return delete_event_or_archive(**arguments)

    def delete_profile(self, profile, **changes):
        arguments = {
            "profile": profile, "actor": self.organizer,
            "expected_updated_at": profile.updated_at.isoformat(),
        }
        arguments.update(changes)
        return delete_reusable_event_profile(**arguments)

    def email(self):
        return EmailDelivery.objects.create(
            event=self.event, family=self.family,
            kind=EmailDelivery.Kind.REGISTRATION_RECEIPT,
            status=EmailDelivery.Status.DRAFT,
            to_email=self.family_user.email, subject="Confirmation", body_text="Contenu conservé",
        )

    def test_empty_event_summary_counts_configuration_and_real_delete_keeps_other_entities(self):
        profile = save_event_profile(event=self.event, name="Modèle conservé")
        counts = get_event_deletion_summary(self.event)
        self.assertEqual(counts, {
            "counts": {"days": 1, "stations": 2, "slots": 2, "menus": 1, "drinks": 1,
                       "registrations": 0, "assignments": 0, "orders": 0, "payments": 0,
                       "attendances": 0, "emails": 0},
            "has_history": False, "mode": "delete",
        })
        event_id = self.event.pk
        result = self.delete_event()
        self.assertEqual(result["mode"], "delete")
        self.assertEqual(result["id"], event_id)
        self.assertFalse(Event.objects.filter(pk=event_id).exists())
        for model in (EventDay, Station, StationSlot, Menu, Beverage):
            self.assertFalse(model.objects.exists())
        self.assertTrue(EventProfile.objects.filter(pk=profile.pk).exists())
        self.assertTrue(User.objects.filter(pk=self.family_user.pk).exists())
        self.assertTrue(self.family.members.filter(pk=self.member.pk).exists())

    def test_active_registration_archives_and_preserves_assignment_meal_and_money(self):
        registration = self.registration()
        assignment = create_assignment(registration=registration, station_slot=self.morning_slot)
        order = save_meal_order(registration=registration, menu=self.menu, beverage=self.drink)
        payment = Payment.objects.create(family=self.family, event=self.event, amount_cents=600, method=Payment.Method.SUMUP)
        original = Payment.objects.filter(pk=payment.pk).values().get()
        result = self.delete_event()
        self.assertEqual(result["mode"], "archive")
        self.assertTrue(result["summary"]["has_history"])
        self.assertEqual(result["summary"]["counts"]["registrations"], 1)
        self.assertEqual(result["summary"]["counts"]["assignments"], 1)
        self.assertEqual(result["summary"]["counts"]["orders"], 1)
        self.assertEqual(result["summary"]["counts"]["payments"], 1)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertTrue(Registration.objects.filter(pk=registration.pk).exists())
        self.assertTrue(Assignment.objects.filter(pk=assignment.pk).exists())
        self.assertEqual(MealOrder.objects.get(pk=order.pk).total_surcharge_cents, 600)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), original)
        self.assertEqual(StationSlot.objects.count(), 2)

    def test_cancelled_registration_alone_still_preserves_event_history(self):
        registration = self.registration(status=Registration.Status.CANCELLED)
        self.assertEqual(get_event_deletion_summary(self.event)["mode"], "archive")
        self.assertEqual(self.delete_event()["mode"], "archive")
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.CANCELLED)

    def test_payment_without_registration_archives(self):
        payment = Payment.objects.create(family=self.family, event=self.event, amount_cents=200, method=Payment.Method.ON_SITE)
        self.assertEqual(self.delete_event()["mode"], "archive")
        self.assertTrue(Payment.objects.filter(pk=payment.pk, amount_cents=200).exists())

    def test_attendance_without_registration_archives(self):
        attendance = Attendance.objects.create(member=self.member, event_day=self.day, status=Attendance.Status.ABSENT)
        self.assertEqual(self.delete_event()["mode"], "archive")
        self.assertTrue(Attendance.objects.filter(pk=attendance.pk).exists())

    def test_draft_email_and_its_attempts_without_registration_archive(self):
        delivery = self.email()
        attempt = EmailAttempt.objects.create(delivery=delivery, attempt_number=1, status=EmailAttempt.Status.FAILED)
        self.assertEqual(self.delete_event()["mode"], "archive")
        self.assertTrue(EmailDelivery.objects.filter(pk=delivery.pk).exists())
        self.assertTrue(EmailAttempt.objects.filter(pk=attempt.pk).exists())

    def test_history_created_since_confirmation_is_rechecked_even_if_event_token_unchanged(self):
        preview = get_event_deletion_summary(self.event)
        self.assertFalse(preview["has_history"])
        registration = self.registration()
        self.assertEqual(self.delete_event()["mode"], "archive")
        self.assertTrue(Registration.objects.filter(pk=registration.pk).exists())

    def test_unexpected_protection_rolls_back_partial_delete_then_archives(self):
        def protected_after_partial_delete(current):
            EventDay.objects.filter(event=current).delete()
            raise ProtectedError("Historique protégé", {current})

        with patch.object(Event, "delete", protected_after_partial_delete):
            result = self.delete_event()
        self.assertEqual(result["mode"], "archive")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertTrue(EventDay.objects.filter(pk=self.day.pk).exists())
        self.assertEqual(StationSlot.objects.count(), 2)
        self.assertTrue(Menu.objects.filter(pk=self.menu.pk).exists())
        self.assertTrue(Beverage.objects.filter(pk=self.drink.pk).exists())

    def test_event_stale_missing_naive_or_malformed_token_cannot_delete(self):
        old_token = self.event.updated_at.isoformat()
        self.event.name = "Événement modifié"
        self.event.save()
        for token in (old_token, None, "", "wrong", "2027-01-01T10:00:00"):
            with self.subTest(token=token), self.assertRaises(ValidationError):
                self.delete_event(expected_updated_at=token)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_actor_rights_are_reloaded_and_forged_or_revoked_rights_do_not_allow_delete(self):
        self.family_user.is_staff = True
        self.family_user.is_organizer = True
        self.family_user.is_superuser = True
        for actor in (self.family_user, AnonymousUser(), None):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                self.delete_event(actor=actor)
        User.objects.filter(pk=self.organizer.pk).update(is_active=False)
        with self.assertRaises(PermissionDenied):
            self.delete_event()
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())

    def test_staff_permission_is_rechecked_in_database(self):
        User.objects.filter(pk=self.family_user.pk).update(is_staff=True)
        self.assertEqual(self.delete_event(actor=self.family_user)["mode"], "delete")

    def test_profile_delete_is_independent_from_original_and_cloned_events(self):
        profile = save_event_profile(event=self.event, name="Concours type")
        clone = create_event_from_profile(profile=profile, name="Concours copié", event_date=date(2027, 5, 1))
        result = self.delete_profile(profile)
        self.assertEqual(result["mode"], "delete")
        self.assertFalse(EventProfile.objects.filter(pk=profile.pk).exists())
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(Event.objects.filter(pk=clone.pk).exists())
        self.assertEqual(clone.stations.count(), 2)
        self.assertEqual(clone.days.get().menus.get().surcharge_cents, 500)

    def test_profile_token_and_actor_are_required(self):
        profile = save_event_profile(event=self.event, name="Concours type")
        old_token = profile.updated_at.isoformat()
        profile.description = "Version modifiée"
        profile.save()
        with self.assertRaises(ValidationError):
            self.delete_profile(profile, expected_updated_at=old_token)
        for token in (None, "", "wrong", "2027-01-01T10:00:00"):
            with self.subTest(token=token), self.assertRaises(ValidationError):
                self.delete_profile(profile, expected_updated_at=token)
        with self.assertRaises(PermissionDenied):
            self.delete_profile(profile, actor=self.family_user)
        self.assertTrue(EventProfile.objects.filter(pk=profile.pk).exists())

    def test_already_deleted_event_or_profile_produces_a_validation_error(self):
        profile = save_event_profile(event=self.event, name="Concours type")
        self.delete_profile(profile)
        with self.assertRaises(ValidationError):
            self.delete_profile(profile)
        self.delete_event()
        with self.assertRaises(ValidationError):
            self.delete_event()

    def test_deleted_profile_cannot_be_cloned_edited_or_have_stations_changed(self):
        profile = save_event_profile(event=self.event, name="Concours type")
        token = profile.updated_at.isoformat()
        self.delete_profile(profile)
        before = Event.objects.count()
        actions = (
            lambda: create_event_from_profile(profile=profile, name="Refusé", event_date=date(2027, 5, 1)),
            lambda: update_reusable_profile(
                profile=profile, expected_updated_at=token, name="Modifié", description="",
                event_description="", location="Centre", general_instructions="",
            ),
            lambda: save_profile_station(profile=profile, expected_updated_at=token, station_data={"name": "Nouveau"}, slots=[]),
            lambda: delete_profile_station(profile=profile, expected_updated_at=token, station_index=0),
        )
        for action in actions:
            with self.assertRaises(ValidationError):
                action()
        self.assertEqual(Event.objects.count(), before)
        self.assertFalse(EventProfile.objects.exists())


@skipUnless(connection.vendor == "postgresql", "Nécessite les verrous PostgreSQL et les clés étrangères différées.")
class ConcurrentDeletionServicesTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.reg = self.registration()
        self.assignment = create_assignment(registration=self.reg, station_slot=self.morning_slot)

    def test_confirmation_audit_user_reference_and_deletion_do_not_deadlock(self):
        day_held = ThreadEvent()
        actor_held = ThreadEvent()
        original_check_version = deletion._check_version

        def confirmation_holds_day(assignments):
            day_held.set()
            if not actor_held.wait(timeout=5):
                raise AssertionError("La suppression n'a pas verrouillé son acteur.")
            return validate_adult_coverage_for_confirmation(assignments)

        def deletion_holds_actor(current, expected_updated_at):
            original_check_version(current, expected_updated_at)
            actor_held.set()

        def action(delete):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                actor = User.objects.get(pk=self.organizer.pk)
                if delete:
                    if not day_held.wait(timeout=5):
                        raise AssertionError("La confirmation n'a pas verrouillé sa journée.")
                    result = delete_event_or_archive(
                        event=Event.objects.get(pk=self.event.pk), actor=actor,
                        expected_updated_at=self.event.updated_at.isoformat(),
                    )
                    return result["mode"]
                confirm_assignment(
                    assignment=Assignment.objects.get(pk=self.assignment.pk), confirmed_by=actor,
                )
                return "confirmed"
            finally:
                connections.close_all()

        with (
            patch("volunteers.services.validate_adult_coverage_for_confirmation", confirmation_holds_day),
            patch("volunteers.deletion._check_version", deletion_holds_actor),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [pool.submit(action, delete) for delete in (False, True)]
            results = [future.result(timeout=15) for future in futures]
        self.assertCountEqual(results, ["confirmed", "archive"])
        self.assignment.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.assignment.status, Assignment.Status.CONFIRMED)
        self.assertEqual(self.assignment.confirmed_by_id, self.organizer.pk)
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
