import re
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Event,
    EventDay,
    Member,
    Registration,
    Station,
    StationSlot,
    User,
)
from volunteers.registration_attention import get_registration_attention

from .factories import DataMixin


class RegistrationAttentionViewTests(DataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.day.date = timezone.localdate() + timedelta(days=7)
        cls.day.save(update_fields=("date",))
        cls.full_day_slot = StationSlot.objects.create(
            station=cls.other_station,
            event_day=cls.day,
            period=StationSlot.Period.FULL_DAY,
            capacity=20,
        )

    def make_registration(self, *, day=None, availability=None, status=None):
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
        Assignment.objects.bulk_create([
            Assignment(
                registration=registration,
                station_slot=slot,
                period=slot.period,
                status=status,
            )
            for slot, status in slots_and_statuses
        ])

    def make_event(self, *, name, status=Event.Status.PUBLISHED):
        event = Event.objects.create(
            name=name,
            location=self.event.location,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
            status=status,
        )
        day = EventDay.objects.create(event=event, date=self.day.date)
        return event, day

    def test_anonymous_and_family_never_see_badge_or_query_attention(self):
        self.make_registration()
        for actor in (None, self.family_user):
            with self.subTest(actor=actor):
                if actor is None:
                    self.client.logout()
                else:
                    self.client.force_login(actor)
                with patch(
                    "volunteers.context_processors.get_registration_attention",
                    wraps=get_registration_attention,
                ) as attention:
                    response = self.client.get(reverse("home"))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "assignment-attention-link")
                self.assertEqual(response.context["pending_assignment_count"], 0)
                attention.assert_not_called()

    def test_organizer_staff_and_administrator_see_pending_badge(self):
        self.make_registration()
        organizer = User.objects.create_user(
            email="attention-organizer@example.test", password=None, is_organizer=True
        )
        staff = User.objects.create_user(
            email="attention-staff@example.test", password=None, is_staff=True
        )
        administrator = User.objects.create_superuser(
            email="attention-admin@example.test", password=None
        )
        for actor in (organizer, staff, administrator):
            with self.subTest(actor=actor):
                self.client.force_login(actor)
                response = self.client.get(reverse("home"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, "assignment-attention-link")
                self.assertContains(
                    response, f'href="{reverse("organizer_dashboard")}#affectations-a-faire"'
                )
                self.assertEqual(response.context["pending_assignment_count"], 1)

    def test_dashboard_lists_only_events_with_actionable_presences(self):
        self.client.force_login(self.organizer)
        self.make_registration()
        partial = self.make_registration()
        self.add_assignments(partial, (self.morning_slot, Assignment.Status.DRAFT))
        self.make_registration(status=Registration.Status.CANCELLED)
        past_day = EventDay.objects.create(
            event=self.event, date=timezone.localdate() - timedelta(days=1)
        )
        self.make_registration(day=past_day)
        _, archived_day = self.make_event(name="Archive à ignorer", status=Event.Status.ARCHIVED)
        self.make_registration(day=archived_day)
        covered_event, covered_day = self.make_event(name="Événement déjà réparti")
        station = Station.objects.create(event=covered_event, name="Accueil couvert")
        slot = StationSlot.objects.create(
            station=station, event_day=covered_day,
            period=StationSlot.Period.FULL_DAY, capacity=1,
        )
        covered = self.make_registration(day=covered_day)
        self.add_assignments(covered, (slot, Assignment.Status.DRAFT))

        response = self.client.get(reverse("organizer_dashboard"))
        self.assertEqual(response.context["pending_assignment_count"], 2)
        summary = response.context["assignment_attention"]
        self.assertEqual(summary["unassigned_count"], 1)
        self.assertEqual(summary["partial_count"], 1)
        tasks = response.context["assignment_attention_events"]
        self.assertEqual([event["event_id"] for event in tasks], [self.event.pk])
        self.assertEqual(tasks[0]["pending_count"], 2)
        self.assertContains(response, 'id="affectations-a-faire"')
        self.assertContains(
            response,
            f'{reverse("registration_list", args=[self.event.pk])}?affectation=a_faire',
        )

    def test_event_pages_share_the_same_event_pending_count(self):
        self.client.force_login(self.organizer)
        self.make_registration()
        partial = self.make_registration()
        self.add_assignments(partial, (self.morning_slot, Assignment.Status.CONFIRMED))
        _, other_day = self.make_event(name="Autre événement")
        self.make_registration(day=other_day)

        response = self.client.get(reverse("event_list"))
        counts = {event.pk: event.pending_assignment_count for event in response.context["events"]}
        self.assertEqual(counts[self.event.pk], 2)
        self.assertEqual(response.context["pending_assignment_count"], 3)
        for route in ("event_detail", "registration_list"):
            with self.subTest(route=route):
                response = self.client.get(reverse(route, args=[self.event.pk]))
                self.assertEqual(response.context["pending_event_assignment_count"], 2)
                self.assertEqual(response.context["pending_assignment_count"], 3)

    def test_registration_filter_includes_unassigned_and_partial_presences_only(self):
        self.client.force_login(self.organizer)
        unassigned = self.make_registration()
        partial = self.make_registration()
        self.add_assignments(partial, (self.morning_slot, Assignment.Status.DRAFT))
        covered = self.make_registration()
        self.add_assignments(covered, (self.full_day_slot, Assignment.Status.DRAFT))
        cancelled = self.make_registration(status=Registration.Status.CANCELLED)
        past_day = EventDay.objects.create(
            event=self.event, date=timezone.localdate() - timedelta(days=1)
        )
        past = self.make_registration(day=past_day)
        url = reverse("registration_list", args=[self.event.pk])

        response = self.client.get(url, {"affectation": "a_faire"})
        rows = list(response.context["registrations"])
        self.assertEqual({row.pk for row in rows}, {unassigned.pk, partial.pk})
        self.assertTrue(all(row.needs_assignment for row in rows))
        self.assertEqual(response.context["pending_event_assignment_count"], 2)

        response = self.client.get(url)
        rows = {row.pk: row for row in response.context["registrations"]}
        self.assertEqual(set(rows), {unassigned.pk, partial.pk, covered.pk, cancelled.pk, past.pk})
        self.assertTrue(rows[unassigned.pk].needs_assignment)
        self.assertTrue(rows[partial.pk].needs_assignment)
        self.assertFalse(rows[covered.pk].needs_assignment)
        self.assertFalse(rows[cancelled.pk].needs_assignment)
        self.assertFalse(rows[past.pk].needs_assignment)

    def test_archiving_an_event_removes_its_badge_and_filtered_presences(self):
        self.client.force_login(self.organizer)
        self.make_registration()
        before = self.client.get(reverse("home"))
        self.assertContains(before, "assignment-attention-link")
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.ARCHIVED)
        after = self.client.get(reverse("home"))
        self.assertNotContains(after, "assignment-attention-link")
        self.assertEqual(after.context["pending_assignment_count"], 0)
        response = self.client.get(
            reverse("registration_list", args=[self.event.pk]), {"affectation": "a_faire"}
        )
        self.assertEqual(list(response.context["registrations"]), [])
        self.assertEqual(response.context["pending_event_assignment_count"], 0)

    def test_badge_disappears_when_drafts_cover_the_full_day(self):
        self.client.force_login(self.organizer)
        registration = self.make_registration()
        initial = self.client.get(reverse("home"))
        self.assertEqual(initial.context["pending_assignment_count"], 1)
        self.assertContains(initial, "assignment-attention-link")

        self.add_assignments(registration, (self.morning_slot, Assignment.Status.DRAFT))
        partial = self.client.get(reverse("home"))
        self.assertEqual(partial.context["pending_assignment_count"], 1)
        self.assertContains(partial, "assignment-attention-link")

        self.add_assignments(registration, (self.afternoon_slot, Assignment.Status.DRAFT))
        covered = self.client.get(reverse("home"))
        self.assertEqual(covered.context["pending_assignment_count"], 0)
        self.assertNotContains(covered, "assignment-attention-link")
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.DRAFT).count(), 2)

    def test_dashboard_and_context_processor_reuse_one_summary_per_request(self):
        self.client.force_login(self.organizer)
        self.make_registration()
        with patch(
            "volunteers.context_processors.get_registration_attention",
            wraps=get_registration_attention,
        ) as attention:
            first = self.client.get(reverse("organizer_dashboard"))
            self.assertEqual(first.context["pending_assignment_count"], 1)
            self.assertEqual(attention.call_count, 1)
            second = self.client.get(reverse("organizer_dashboard"))
            self.assertEqual(second.context["pending_assignment_count"], 1)
            self.assertEqual(attention.call_count, 2)

    def test_attention_pages_get_requests_do_not_write_to_database(self):
        self.client.force_login(self.organizer)
        self.make_registration()
        urls = [
            reverse("home"),
            reverse("organizer_dashboard"),
            reverse("event_list"),
            reverse("event_detail", args=[self.event.pk]),
            reverse("registration_list", args=[self.event.pk]),
            reverse("registration_list", args=[self.event.pk]) + "?affectation=a_faire",
            reverse("assignment_board_event", args=[self.event.pk]),
        ]
        for url in urls:
            with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
            writes = [
                query["sql"] for query in queries
                if re.match(r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b", query["sql"], re.I)
            ]
            self.assertEqual(writes, [], msg=f"GET {url} unexpectedly writes to the database")

    def test_bulk_confirmation_button_uses_requested_label(self):
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("assignment_board_event", args=[self.event.pk]))
        self.assertContains(response, "Confirmer toutes les affectations")
        self.assertNotContains(response, "Confirmer tous les brouillons")
