from django.test import TestCase
from django.urls import reverse

from volunteers.models import Event

from .factories import DataMixin


class EventArchiveVisibilityTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)

    def archive_event(self):
        self.event.status = Event.Status.ARCHIVED
        self.event.save(update_fields=["status", "updated_at"])

    def test_event_list_keeps_active_events_and_offers_separate_archives(self):
        response = self.client.get(reverse("event_list"))
        self.assertIn(self.event, response.context["events"])
        self.archive_event()
        response = self.client.get(reverse("event_list"))
        self.assertNotIn(self.event, response.context["events"])
        self.assertFalse(response.context["show_archived"])
        response = self.client.get(reverse("event_list"), {"archives": "1"})
        self.assertIn(self.event, response.context["events"])
        self.assertTrue(response.context["show_archived"])

    def test_settings_links_to_event_list_without_duplicate_event_cards(self):
        self.archive_event()
        response = self.client.get(reverse("administration_dashboard"))
        self.assertContains(response, reverse("event_list"))
        self.assertNotContains(response, self.event.name)
        response = self.client.get(reverse("administration_dashboard"), {"archives": "1"})
        self.assertNotContains(response, self.event.name)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)

    def test_archived_days_do_not_remain_in_upcoming_dashboard(self):
        self.archive_event()
        response = self.client.get(reverse("organizer_dashboard"))
        self.assertNotIn(self.day, response.context["upcoming_days"])
        self.assertNotIn(self.event, response.context["events"])
