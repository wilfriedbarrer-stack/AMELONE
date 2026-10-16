from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase

from volunteers.models import Assignment, Event, EventDay, Member, Registration, StationSlot
from volunteers.registration_attention import (
    annotate_registration_attention,
    get_registration_attention,
)

from .factories import DataMixin


class RegistrationAttentionTests(DataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.full_day_slot = StationSlot.objects.create(
            station=cls.other_station,
            event_day=cls.day,
            period=StationSlot.Period.FULL_DAY,
            capacity=20,
        )

    def make_registration(self, *, day=None, member=None, availability=None, status=None):
        if member is None:
            member = Member.objects.create(
                family=self.family,
                first_name=f"Participant {Member.objects.count()}",
                last_name="Test",
                birth_date=self.member.birth_date,
            )
        return Registration.objects.create(
            member=member,
            event_day=day or self.day,
            availability=availability or Registration.Availability.FULL_DAY,
            status=status or Registration.Status.ACTIVE,
        )

    def add_assignments(self, registration, *slots_and_statuses):
        # These read-only indicators also tolerate legacy combinations that the
        # current write services prevent (for example, a changed availability).
        Assignment.objects.bulk_create([
            Assignment(
                registration=registration,
                station_slot=slot,
                period=slot.period,
                status=status,
            )
            for slot, status in slots_and_statuses
        ])

    def attention_for(self, registration, *, today=None):
        return annotate_registration_attention(
            Registration.objects.filter(pk=registration.pk), today=today or self.day.date
        ).get()

    def test_full_day_requires_both_half_days_or_a_full_day_assignment(self):
        cases = (
            ((), True),
            (((self.morning_slot, Assignment.Status.DRAFT),), True),
            (((self.afternoon_slot, Assignment.Status.CONFIRMED),), True),
            ((
                (self.morning_slot, Assignment.Status.DRAFT),
                (self.afternoon_slot, Assignment.Status.CONFIRMED),
            ), False),
            (((self.full_day_slot, Assignment.Status.DRAFT),), False),
            (((self.full_day_slot, Assignment.Status.CONFIRMED),), False),
        )
        for assignments, needs_assignment in cases:
            with self.subTest(assignments=assignments):
                registration = self.make_registration()
                self.add_assignments(registration, *assignments)
                self.assertEqual(self.attention_for(registration).needs_assignment, needs_assignment)

        summary = get_registration_attention(today=self.day.date)
        self.assertEqual(summary["pending_count"], 3)
        self.assertEqual(summary["unassigned_count"], 1)
        self.assertEqual(summary["partial_count"], 2)
        self.assertEqual(summary["draft_registration_count"], 3)

    def test_half_day_uses_matching_active_assignment_period(self):
        cases = (
            (Registration.Availability.MORNING, (), True),
            (Registration.Availability.MORNING, (
                (self.morning_slot, Assignment.Status.CANCELLED),
            ), True),
            (Registration.Availability.MORNING, (
                (self.morning_slot, Assignment.Status.DRAFT),
            ), False),
            (Registration.Availability.AFTERNOON, (
                (self.afternoon_slot, Assignment.Status.CONFIRMED),
            ), False),
            (Registration.Availability.MORNING, (
                (self.afternoon_slot, Assignment.Status.DRAFT),
            ), True),
            (Registration.Availability.AFTERNOON, (
                (self.morning_slot, Assignment.Status.CONFIRMED),
            ), True),
            (Registration.Availability.MORNING, (
                (self.full_day_slot, Assignment.Status.DRAFT),
            ), False),
            (Registration.Availability.AFTERNOON, (
                (self.full_day_slot, Assignment.Status.CONFIRMED),
            ), False),
        )
        for availability, assignments, needs_assignment in cases:
            with self.subTest(availability=availability, assignments=assignments):
                registration = self.make_registration(availability=availability)
                self.add_assignments(registration, *assignments)
                self.assertEqual(self.attention_for(registration).needs_assignment, needs_assignment)

    def test_cancelled_afternoon_does_not_complete_an_active_morning(self):
        registration = self.make_registration()
        self.add_assignments(
            registration,
            (self.morning_slot, Assignment.Status.CONFIRMED),
            (self.afternoon_slot, Assignment.Status.CANCELLED),
        )
        row = self.attention_for(registration)
        self.assertTrue(row.has_active_assignment)
        self.assertFalse(row.has_draft_assignment)
        self.assertTrue(row.needs_assignment)
        summary = get_registration_attention(today=self.day.date)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["partial_count"], 1)

    def test_summary_excludes_cancelled_past_and_archived_registrations(self):
        today = self.day.date
        self.make_registration()
        self.make_registration(status=Registration.Status.CANCELLED)
        past_day = EventDay.objects.create(event=self.event, date=today - timedelta(days=1))
        self.make_registration(day=past_day)
        draft_event = Event.objects.create(
            name="Préparation",
            location=self.event.location,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
            status=Event.Status.DRAFT,
        )
        draft_day = EventDay.objects.create(event=draft_event, date=today + timedelta(days=1))
        self.make_registration(day=draft_day)
        archived_event = Event.objects.create(
            name="Archive",
            location=self.event.location,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
            status=Event.Status.ARCHIVED,
        )
        archived_day = EventDay.objects.create(event=archived_event, date=today)
        self.make_registration(day=archived_day)

        with patch("volunteers.registration_attention.timezone.localdate", return_value=today):
            summary = get_registration_attention()
        self.assertEqual(summary["pending_count"], 2)
        self.assertEqual(summary["unassigned_count"], 2)
        self.assertEqual(summary["partial_count"], 0)
        self.assertEqual(
            {row["event_id"] for row in summary["events"]}, {self.event.pk, draft_event.pk}
        )
        scoped = get_registration_attention(event_id=self.event.pk, today=today)
        self.assertEqual(scoped["pending_count"], 1)
        self.assertEqual([row["event_id"] for row in scoped["events"]], [self.event.pk])
        archived = get_registration_attention(event_id=archived_event.pk, today=today)
        self.assertEqual(archived["pending_count"], 0)
        self.assertEqual(archived["events"], [])

    def test_annotations_never_request_assignment_for_past_cancelled_or_archived_rows(self):
        cancelled = self.make_registration(status=Registration.Status.CANCELLED)
        self.assertFalse(self.attention_for(cancelled).needs_assignment)
        active = self.make_registration()
        self.assertFalse(
            self.attention_for(active, today=self.day.date + timedelta(days=1)).needs_assignment
        )
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.ARCHIVED)
        self.assertFalse(self.attention_for(active).needs_assignment)

    def test_counts_registration_days_and_correlates_coverage_to_each_registration(self):
        covered = self.make_registration(member=self.member)
        self.add_assignments(
            covered,
            (self.morning_slot, Assignment.Status.DRAFT),
            (self.afternoon_slot, Assignment.Status.CONFIRMED),
        )
        second_day = EventDay.objects.create(
            event=self.event, date=self.day.date + timedelta(days=1)
        )
        third_day = EventDay.objects.create(
            event=self.event, date=self.day.date + timedelta(days=2)
        )
        self.make_registration(day=second_day, member=self.member)
        self.make_registration(day=third_day, member=self.member)
        summary = get_registration_attention(event_id=self.event.pk, today=self.day.date)
        self.assertEqual(summary["pending_count"], 2)
        self.assertEqual(summary["unassigned_count"], 2)
        self.assertEqual(summary["draft_registration_count"], 1)
        self.assertEqual(len(summary["events"]), 1)
        self.assertEqual(summary["events"][0]["pending_count"], 2)

    def test_multiple_assignments_do_not_multiply_counts_and_summary_is_one_query(self):
        covered = self.make_registration()
        self.add_assignments(
            covered,
            (self.morning_slot, Assignment.Status.DRAFT),
            (self.afternoon_slot, Assignment.Status.DRAFT),
            (self.morning_slot, Assignment.Status.CANCELLED),
            (self.afternoon_slot, Assignment.Status.CANCELLED),
        )
        self.make_registration()
        with self.assertNumQueries(1):
            summary = get_registration_attention(today=self.day.date)
        self.assertEqual(summary["pending_count"], 1)
        self.assertEqual(summary["unassigned_count"], 1)
        self.assertEqual(summary["partial_count"], 0)
        self.assertEqual(summary["draft_registration_count"], 1)
        self.assertEqual(summary["events"][0]["event_name"], self.event.name)
        self.assertEqual(
            annotate_registration_attention(Registration.objects.all(), today=self.day.date).count(),
            2,
        )

    def test_empty_summary_contains_zero_counts(self):
        self.assertEqual(get_registration_attention(today=self.day.date), {
            "pending_count": 0,
            "unassigned_count": 0,
            "partial_count": 0,
            "draft_registration_count": 0,
            "events": [],
        })
