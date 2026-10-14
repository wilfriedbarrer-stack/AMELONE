from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from volunteers.models import Assignment, EventDay, Registration, StationSlot
from volunteers.services import (
    BusinessRuleError,
    confirm_assignment,
    create_assignment,
    remove_assignment,
    save_registration,
    update_assignment,
)

from .factories import DataMixin


class FullDayAssignmentTests(DataMixin, TestCase):
    def setUp(self):
        # Horaires volontairement distincts des créneaux matin/après-midi :
        # la période Journée reste exclusive sur toute la date.
        self.full_day_slot = StationSlot.objects.create(
            station=self.other_station,
            event_day=self.day,
            period=StationSlot.Period.FULL_DAY,
            capacity=1,
            start_time=time(19, 0),
            end_time=time(20, 0),
        )

    def test_full_day_presence_can_be_assigned_and_confirmed(self):
        assignment = create_assignment(
            registration=self.registration(),
            station_slot=self.full_day_slot,
            actor=self.organizer,
        )
        confirmed = confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        self.assertEqual(confirmed.period, StationSlot.Period.FULL_DAY)
        self.assertEqual(confirmed.status, Assignment.Status.CONFIRMED)
        self.assertEqual(self.full_day_slot.remaining_capacity, 0)
        with self.assertRaises(BusinessRuleError):
            create_assignment(
                registration=self.registration(member=self.other_member),
                station_slot=self.full_day_slot,
                actor=self.organizer,
            )

    def test_half_day_presence_cannot_cover_full_day_post(self):
        for member, availability in (
            (self.member, Registration.Availability.MORNING),
            (self.other_member, Registration.Availability.AFTERNOON),
        ):
            with self.subTest(availability=availability):
                with self.assertRaises(BusinessRuleError):
                    create_assignment(
                        registration=self.registration(member=member, availability=availability),
                        station_slot=self.full_day_slot,
                        actor=self.organizer,
                    )
        self.assertFalse(Assignment.objects.exists())

    def test_full_day_blocks_both_half_days_despite_distinct_hours(self):
        registration = self.registration()
        create_assignment(
            registration=registration, station_slot=self.full_day_slot, actor=self.organizer
        )
        for slot in (self.morning_slot, self.afternoon_slot):
            with self.subTest(period=slot.period), self.assertRaises(BusinessRuleError):
                create_assignment(
                    registration=registration, station_slot=slot, actor=self.organizer
                )
        self.assertEqual(registration.assignments.count(), 1)

    def test_either_half_day_blocks_full_day_despite_distinct_hours(self):
        for member, slot in (
            (self.member, self.morning_slot),
            (self.other_member, self.afternoon_slot),
        ):
            with self.subTest(period=slot.period):
                registration = self.registration(member=member)
                create_assignment(
                    registration=registration, station_slot=slot, actor=self.organizer
                )
                with self.assertRaises(BusinessRuleError):
                    create_assignment(
                        registration=registration,
                        station_slot=self.full_day_slot,
                        actor=self.organizer,
                    )
                self.assertEqual(registration.assignments.count(), 1)

    def test_morning_and_afternoon_remain_possible_when_hours_touch(self):
        self.afternoon_slot.start_time = self.morning_slot.end_time
        self.afternoon_slot.save(update_fields=("start_time",))
        registration = self.registration()
        for slot in (self.morning_slot, self.afternoon_slot):
            create_assignment(
                registration=registration, station_slot=slot, actor=self.organizer
            )
        self.assertEqual(registration.assignments.count(), 2)

    def test_updating_half_day_to_full_day_rechecks_existing_assignment(self):
        registration = self.registration()
        morning = create_assignment(
            registration=registration, station_slot=self.morning_slot, actor=self.organizer
        )
        afternoon = create_assignment(
            registration=registration, station_slot=self.afternoon_slot, actor=self.organizer
        )
        with self.assertRaises(BusinessRuleError):
            update_assignment(
                assignment=morning, station_slot=self.full_day_slot, actor=self.organizer
            )
        morning.refresh_from_db()
        self.assertEqual(morning.station_slot_id, self.morning_slot.pk)
        remove_assignment(assignment=afternoon, actor=self.organizer)
        updated = update_assignment(
            assignment=morning, station_slot=self.full_day_slot, actor=self.organizer
        )
        self.assertEqual(updated.period, StationSlot.Period.FULL_DAY)

    def test_reducing_availability_cancels_confirmed_full_day_assignment(self):
        assignment = create_assignment(
            registration=self.registration(),
            station_slot=self.full_day_slot,
            actor=self.organizer,
        )
        confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        updated = save_registration(
            member=self.member,
            event_day=self.day,
            availability=Registration.Availability.MORNING,
            actor=self.family_user,
        )
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Assignment.Status.CANCELLED)
        self.assertIsNotNone(assignment.confirmed_at)
        self.assertIsNotNone(assignment.cancelled_at)
        self.assertTrue(updated.reassignment_required)
        self.assertEqual(updated.invalidated_assignments[0]["period"], StationSlot.Period.FULL_DAY)

    def test_cancelled_full_day_assignment_does_not_block_half_day(self):
        registration = self.registration()
        removed = create_assignment(
            registration=registration, station_slot=self.full_day_slot, actor=self.organizer
        )
        remove_assignment(assignment=removed, actor=self.organizer)
        created = create_assignment(
            registration=registration, station_slot=self.morning_slot, actor=self.organizer
        )
        self.assertEqual(created.status, Assignment.Status.DRAFT)
        self.assertEqual(self.full_day_slot.remaining_capacity, 1)

    def test_full_day_does_not_overlap_another_date(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        slot = StationSlot.objects.create(
            station=self.station,
            event_day=other_day,
            period=StationSlot.Period.MORNING,
            capacity=1,
        )
        self.assertFalse(self.full_day_slot.overlaps(slot))
        self.assertFalse(slot.overlaps(self.full_day_slot))

    def test_assigned_slot_cannot_change_period_day_or_station(self):
        create_assignment(
            registration=self.registration(),
            station_slot=self.full_day_slot,
            actor=self.organizer,
        )
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        for field, value in (
            ("period", StationSlot.Period.MORNING),
            ("event_day", other_day),
            ("station", self.station),
        ):
            with self.subTest(field=field):
                slot = StationSlot.objects.get(pk=self.full_day_slot.pk)
                setattr(slot, field, value)
                with self.assertRaises(ValidationError) as caught:
                    slot.full_clean()
                self.assertIn(field, caught.exception.message_dict)
        self.full_day_slot.full_clean()

    def test_slot_period_can_change_after_assignments_are_removed(self):
        assignment = create_assignment(
            registration=self.registration(),
            station_slot=self.full_day_slot,
            actor=self.organizer,
        )
        remove_assignment(assignment=assignment, actor=self.organizer)
        self.full_day_slot.period = StationSlot.Period.MORNING
        self.full_day_slot.full_clean()
        self.full_day_slot.save(update_fields=("period",))
        self.full_day_slot.refresh_from_db()
        self.assertEqual(self.full_day_slot.period, StationSlot.Period.MORNING)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentFullDayAssignmentTests(DataMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.registration_record = self.registration()
        self.full_day_slot = StationSlot.objects.create(
            station=self.other_station,
            event_day=self.day,
            period=StationSlot.Period.FULL_DAY,
            capacity=1,
        )

    def test_concurrent_full_day_and_half_day_assignments_are_serialized(self):
        barrier = Barrier(2)

        def assign(slot_id):
            close_old_connections()
            try:
                registration = Registration.objects.get(pk=self.registration_record.pk)
                slot = StationSlot.objects.get(pk=slot_id)
                barrier.wait(timeout=10)
                create_assignment(registration=registration, station_slot=slot)
                return "created"
            except BusinessRuleError:
                return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(assign, (self.full_day_slot.pk, self.morning_slot.pk)))
        self.assertCountEqual(results, ["created", "rejected"])
        self.assertEqual(self.registration_record.assignments.count(), 1)
