from concurrent.futures import ThreadPoolExecutor
from datetime import date
from importlib import import_module
from threading import Barrier
from types import SimpleNamespace

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from volunteers.eligibility import member_can_choose_station
from volunteers.models import Assignment, Member, Registration, Station, User
from volunteers.services import (
    BusinessRuleError,
    cancel_registration,
    confirm_assignment,
    confirm_assignments,
    create_assignment,
    remove_assignment,
    save_registration,
    update_assignment,
)

from .factories import DataMixin


class EligibilityTests(DataMixin, TestCase):
    def setUp(self):
        self.minor = Member.objects.create(
            family=self.family, first_name="Mineur", last_name="Test",
            birth_date=date(2009, 3, 1),
        )
        self.minor_registration = self.registration(member=self.minor)
        self.adult_registration = self.registration()
        self.morning_slot.capacity = 4
        self.morning_slot.save()

    def set_policy(self, policy):
        self.station.age_policy = policy
        self.station.save()

    def pair(self):
        self.set_policy(Station.AgePolicy.AT_LEAST_ONE_ADULT)
        minor = create_assignment(registration=self.minor_registration, station_slot=self.morning_slot)
        adult = create_assignment(registration=self.adult_registration, station_slot=self.morning_slot)
        return minor, adult

    def confirmed_pair(self):
        minor, adult = self.pair()
        confirm_assignments(assignments=[minor, adult], confirmed_by=self.organizer)
        minor.refresh_from_db()
        adult.refresh_from_db()
        return minor, adult

    def test_age_is_evaluated_on_event_date_and_eighteenth_birthday_is_adult(self):
        self.set_policy(Station.AgePolicy.ADULTS_ONLY)
        self.assertFalse(member_can_choose_station(self.minor, self.station, self.day))
        self.minor.birth_date = date(2009, 2, 28)
        self.minor.save()
        self.assertTrue(member_can_choose_station(self.minor, self.station, self.day))
        assignment = create_assignment(registration=self.minor_registration, station_slot=self.morning_slot)
        self.assertEqual(assignment.registration.age_on_event_day, 18)

    def test_adults_only_rejects_preference_even_for_administrator(self):
        self.set_policy(Station.AgePolicy.ADULTS_ONLY)
        administrator = User.objects.create_superuser(email="admin-elig@example.test", password="x")
        with self.assertRaises(BusinessRuleError):
            save_registration(
                member=self.minor, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                preferred_station=self.station, actor=administrator,
            )
        self.minor_registration.refresh_from_db()
        self.assertIsNone(self.minor_registration.preferred_station_id)

    def test_adults_only_rejects_draft_and_direct_model_assignment(self):
        self.set_policy(Station.AgePolicy.ADULTS_ONLY)
        with self.assertRaises(BusinessRuleError):
            create_assignment(registration=self.minor_registration, station_slot=self.morning_slot)
        with self.assertRaises(ValidationError):
            Assignment.objects.create(registration=self.minor_registration, station_slot=self.morning_slot)

    def test_official_requires_individual_permission_not_admin_actor_or_family_role(self):
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        administrator = User.objects.create_superuser(email="admin-official@example.test", password="x")
        with self.assertRaises(BusinessRuleError):
            create_assignment(
                registration=self.adult_registration, station_slot=self.morning_slot, actor=administrator,
            )
        self.member.is_competition_official = True
        self.member.save()
        assignment = create_assignment(
            registration=self.adult_registration, station_slot=self.morning_slot, actor=administrator,
        )
        self.assertEqual(assignment.status, Assignment.Status.DRAFT)
        self.assertFalse(member_can_choose_station(self.minor, self.station, self.day))

    def test_stale_preference_station_is_reloaded(self):
        stale = Station.objects.get(pk=self.station.pk)
        self.set_policy(Station.AgePolicy.ADULTS_ONLY)
        with self.assertRaises(BusinessRuleError):
            save_registration(
                member=self.minor, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                preferred_station=stale, actor=self.organizer,
            )

    def test_minor_draft_allowed_but_confirmation_alone_refused(self):
        minor, _ = self.pair()
        with self.assertRaises(BusinessRuleError):
            confirm_assignment(assignment=minor, confirmed_by=self.organizer)
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)
        self.assertIsNone(minor.confirmed_at)

    def test_batch_adult_and_minor_confirm_atomically_in_either_selection_order(self):
        minor, adult = self.pair()
        for selection in ([minor, adult], [adult, minor]):
            Assignment.objects.filter(pk__in=[minor.pk, adult.pk]).update(status=Assignment.Status.DRAFT)
            confirmed = confirm_assignments(assignments=selection, confirmed_by=self.organizer)
            self.assertTrue(all(item.status == Assignment.Status.CONFIRMED for item in confirmed))

    def test_adult_on_different_slot_does_not_cover_minor(self):
        minor, _ = self.pair()
        adult = create_assignment(registration=self.adult_registration, station_slot=self.afternoon_slot)
        with self.assertRaises(BusinessRuleError):
            confirm_assignments(assignments=[minor, adult], confirmed_by=self.organizer)
        self.assertFalse(Assignment.objects.filter(status=Assignment.Status.CONFIRMED).exists())

    def test_direct_model_confirmation_cannot_bypass_adult_rule(self):
        minor, _ = self.pair()
        minor.status = Assignment.Status.CONFIRMED
        with self.assertRaises(ValidationError):
            minor.save()

    def test_direct_model_confirmation_accepts_existing_confirmed_adult(self):
        minor, adult = self.pair()
        confirm_assignment(assignment=adult, confirmed_by=self.organizer)
        minor.status = Assignment.Status.CONFIRMED
        minor.save()
        self.assertEqual(minor.status, Assignment.Status.CONFIRMED)

    def test_cancelled_adult_registration_never_covers_minor_confirmation(self):
        minor, adult = self.pair()
        confirm_assignment(assignment=adult, confirmed_by=self.organizer)
        Registration.objects.filter(pk=self.adult_registration.pk).update(status=Registration.Status.CANCELLED)
        with self.assertRaises(BusinessRuleError):
            confirm_assignment(assignment=minor, confirmed_by=self.organizer)

    def test_partial_assignment_save_validates_the_slot_actually_written(self):
        minor, _ = self.pair()
        minor.station_slot = self.afternoon_slot
        minor.status = Assignment.Status.CONFIRMED
        with self.assertRaises(ValidationError):
            minor.save(update_fields=("status",))

    def test_removing_last_adult_downgrades_minors_and_preserves_confirmation_audit(self):
        minor, adult = self.confirmed_pair()
        confirmed_at = minor.confirmed_at
        remove_assignment(assignment=adult, actor=self.organizer)
        minor.refresh_from_db()
        adult.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)
        self.assertEqual(minor.confirmed_at, confirmed_at)
        self.assertEqual(minor.confirmed_by_id, self.organizer.pk)
        self.assertEqual(adult.status, Assignment.Status.CANCELLED)

    def test_cancelling_last_adult_registration_downgrades_minors(self):
        minor, _ = self.confirmed_pair()
        cancel_registration(registration=self.adult_registration, actor=self.organizer)
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)

    def test_reducing_last_adult_availability_downgrades_minors(self):
        minor, _ = self.confirmed_pair()
        save_registration(
            member=self.member, event_day=self.day, availability=Registration.Availability.AFTERNOON,
            actor=self.organizer,
        )
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)

    def test_moving_last_adult_downgrades_old_slot_minors(self):
        minor, adult = self.confirmed_pair()
        update_assignment(assignment=adult, station_slot=self.afternoon_slot, actor=self.organizer)
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)

    def test_direct_model_deconfirmation_downgrades_minors(self):
        minor, adult = self.confirmed_pair()
        adult.status = Assignment.Status.DRAFT
        adult.save()
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.DRAFT)

    def test_second_confirmed_adult_keeps_minors_confirmed(self):
        minor, adult = self.confirmed_pair()
        second = create_assignment(
            registration=self.registration(member=self.other_member), station_slot=self.morning_slot,
        )
        confirm_assignment(assignment=second, confirmed_by=self.organizer)
        remove_assignment(assignment=adult, actor=self.organizer)
        minor.refresh_from_db()
        self.assertEqual(minor.status, Assignment.Status.CONFIRMED)

    def test_revoke_official_with_future_assignment_requires_removal_first(self):
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        self.member.is_competition_official = True
        self.member.save()
        assignment = create_assignment(registration=self.adult_registration, station_slot=self.morning_slot)
        self.member.is_competition_official = False
        with self.assertRaises(ValidationError) as raised:
            self.member.save()
        self.assertIn("is_competition_official", raised.exception.message_dict)
        remove_assignment(assignment=assignment)
        self.member.save()
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_birth_date_change_cannot_break_active_adult_only_assignment(self):
        self.set_policy(Station.AgePolicy.ADULTS_ONLY)
        create_assignment(registration=self.adult_registration, station_slot=self.morning_slot)
        self.member.birth_date = date(2010, 1, 1)
        with self.assertRaises(ValidationError) as raised:
            self.member.save()
        self.assertIn("birth_date", raised.exception.message_dict)

    def test_birth_date_change_cannot_remove_last_confirmed_adult(self):
        self.confirmed_pair()
        self.member.birth_date = date(2010, 1, 1)
        with self.assertRaises(ValidationError):
            self.member.save()

    def test_station_rule_change_cannot_leave_confirmed_minors_alone(self):
        assignment = create_assignment(registration=self.minor_registration, station_slot=self.morning_slot)
        confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        self.station.age_policy = Station.AgePolicy.AT_LEAST_ONE_ADULT
        with self.assertRaises(ValidationError):
            self.station.save()

    def test_migration_preserves_only_linked_official_and_downgrades_unqualified_confirmation(self):
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        self.member.user = self.family_user
        self.member.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        now = self.member.created_at
        assignments = [
            Assignment(registration=registration, station_slot=self.morning_slot,
                       period=self.morning_slot.period, status=Assignment.Status.CONFIRMED,
                       confirmed_at=now, confirmed_by=self.organizer)
            for registration in [self.adult_registration, self.minor_registration]
        ]
        Assignment.objects.bulk_create(assignments)
        migration = import_module("volunteers.migrations.0006_member_official_station_age_policy")
        migration.preserve_official_permissions(
            apps, SimpleNamespace(connection=SimpleNamespace(alias="default")),
        )
        self.member.refresh_from_db()
        self.minor.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertFalse(self.minor.is_competition_official)
        assignments[0].refresh_from_db()
        assignments[1].refresh_from_db()
        self.assertEqual(assignments[0].status, Assignment.Status.CONFIRMED)
        self.assertEqual(assignments[1].status, Assignment.Status.DRAFT)
        self.assertEqual(assignments[1].confirmed_at, now)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentEligibilityTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.station.age_policy = Station.AgePolicy.AT_LEAST_ONE_ADULT
        self.station.save()
        self.morning_slot.capacity = 3
        self.morning_slot.save()
        self.adult_registration = self.registration()
        self.minor = Member.objects.create(
            family=self.family, first_name="Mineur", last_name="Concurrence", birth_date=date(2010, 1, 1),
        )
        self.minor_registration = self.registration(member=self.minor)
        self.adult = create_assignment(registration=self.adult_registration, station_slot=self.morning_slot)
        self.minor_assignment = create_assignment(
            registration=self.minor_registration, station_slot=self.morning_slot,
        )
        confirm_assignment(assignment=self.adult, confirmed_by=self.organizer)

    def test_concurrent_confirmation_and_last_adult_removal_never_leave_minor_confirmed_alone(self):
        barrier = Barrier(2)

        def action(remove):
            close_old_connections()
            try:
                barrier.wait(timeout=5)
                if remove:
                    remove_assignment(assignment=Assignment.objects.get(pk=self.adult.pk))
                    return "removed"
                try:
                    confirm_assignment(
                        assignment=Assignment.objects.get(pk=self.minor_assignment.pk),
                        confirmed_by=User.objects.get(pk=self.organizer.pk),
                    )
                    return "confirmed"
                except BusinessRuleError:
                    return "refused"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(action, [False, True]))
        self.assertIn("removed", results)
        self.minor_assignment.refresh_from_db()
        self.assertEqual(self.minor_assignment.status, Assignment.Status.DRAFT)

    def test_concurrent_birth_date_edits_cannot_remove_both_adults(self):
        second = create_assignment(
            registration=self.registration(member=self.other_member), station_slot=self.morning_slot,
        )
        confirm_assignments(
            assignments=[self.minor_assignment, second], confirmed_by=self.organizer,
        )
        barrier = Barrier(2)

        def make_minor(member_id):
            close_old_connections()
            try:
                member = Member.objects.get(pk=member_id)
                member.birth_date = date(2010, 1, 1)
                barrier.wait(timeout=5)
                try:
                    member.save()
                    return "changed"
                except ValidationError:
                    return "refused"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(make_minor, [self.member.pk, self.other_member.pk]))
        self.assertCountEqual(results, ["changed", "refused"])
        self.minor_assignment.refresh_from_db()
        self.assertEqual(self.minor_assignment.status, Assignment.Status.CONFIRMED)

