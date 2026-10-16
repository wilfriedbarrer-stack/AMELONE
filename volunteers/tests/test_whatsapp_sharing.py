from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from volunteers.forms_organizer import EventForm
from volunteers.models import (
    Assignment,
    Event,
    EventNotificationCampaign,
    EventNotificationDelivery,
    Station,
    StationSlot,
)
from volunteers.whatsapp_sharing import whatsapp_preview

from .factories import DataMixin


@override_settings(
    EVENT_NOTIFICATIONS_ENABLED=False, EMAIL_ENABLED=False, SMS_ENABLED=False,
    SITE_URL="https://amelone.onrender.com",
)
class WhatsAppSharingTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)
        self.url = reverse("event_notifications", args=[self.event.pk])

    def test_group_share_works_with_all_automatic_channels_disabled_without_sends_or_writes(self):
        self.event.notify_publication = self.event.notify_vacancies = False
        self.event.notify_email = self.event.notify_sms = False
        self.event.save(update_fields=("notify_publication", "notify_vacancies", "notify_email", "notify_sms"))
        before = (
            list(Event.objects.values()), list(EventNotificationCampaign.objects.values()),
            list(EventNotificationDelivery.objects.values()),
        )
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        email.assert_not_called()
        sms.assert_not_called()
        for card in response.context["cards"]:
            preview = card["whatsapp"]
            self.assertTrue(preview["can_share"])
            parsed = urlsplit(preview["share_url"])
            self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ("https", "wa.me", "/"))
            self.assertEqual(parse_qs(parsed.query), {"text": [preview["body_text"]]})
            self.assertIn(self.event.name, preview["body_text"])
            self.assertIn("https://amelone.onrender.com" + reverse("family_event_detail", args=[self.event.pk]), preview["body_text"])
            self.assertNotIn(self.family_user.email, preview["share_url"])
            self.assertNotIn(self.family_user.phone, preview["share_url"])
            self.assertNotIn(self.member.first_name, preview["body_text"])
        self.assertEqual(before, (
            list(Event.objects.values()), list(EventNotificationCampaign.objects.values()),
            list(EventNotificationDelivery.objects.values()),
        ))
        self.assertContains(response, 'rel="noopener noreferrer"')
        self.assertContains(response, "WhatsApp")
        self.assertNotContains(response, "Voir l’aperçu du SMS")

    def test_group_reminder_invites_people_not_yet_registered_without_identifying_families(self):
        self.registration()
        response = self.client.get(self.url)
        card = next(card for card in response.context["cards"] if card["kind"] == "vacancy_reminder")
        body = card["whatsapp"]["body_text"]
        self.assertIn("pas encore inscrit", body.lower())
        self.assertIn("Accueil", body)
        self.assertNotIn(self.family.name, body)
        self.assertNotIn(self.other_family.name, body)
        self.assertNotIn(self.other_user.email, body)

    def test_draft_archive_and_closed_events_cannot_be_shared(self):
        for status in (Event.Status.DRAFT, Event.Status.ARCHIVED):
            self.event.status = status
            for kind in ("publication", "vacancy_reminder"):
                with self.subTest(status=status, kind=kind):
                    result = whatsapp_preview(self.event, kind)
                    self.assertFalse(result["can_share"])
                    self.assertEqual(result["share_url"], "")
                    self.assertTrue(result["unavailable_reason"])
        self.event.status = Event.Status.PUBLISHED
        self.event.registration_closes_at = timezone.now() - timedelta(seconds=1)
        for kind in ("publication", "vacancy_reminder"):
            self.assertFalse(whatsapp_preview(self.event, kind)["can_share"])

    def test_before_registration_opening_only_announcement_is_shareable(self):
        self.event.registration_opens_at = timezone.now() + timedelta(hours=1)
        announcement = whatsapp_preview(self.event, "publication")
        self.assertTrue(announcement["can_share"])
        self.assertIn("ouvrir", announcement["body_text"].lower())
        self.assertFalse(whatsapp_preview(self.event, "vacancy_reminder")["can_share"])

    def test_past_event_cannot_be_shared_even_if_registration_deadline_is_future(self):
        self.day.date = timezone.localdate() - timedelta(days=1)
        self.day.save(update_fields=("date",))
        for kind in ("publication", "vacancy_reminder"):
            self.assertFalse(whatsapp_preview(self.event, kind)["can_share"])

    def test_fully_staffed_event_does_not_offer_vacancy_reminder(self):
        registration = self.registration()
        Assignment.objects.create(registration=registration, station_slot=self.morning_slot, status=Assignment.Status.DRAFT)
        self.afternoon_slot.delete()
        result = whatsapp_preview(self.event, "vacancy_reminder")
        self.assertFalse(result["can_share"])
        self.assertEqual(result["share_url"], "")
        self.assertTrue(whatsapp_preview(self.event, "publication")["can_share"])

    def test_shared_full_day_and_half_days_display_real_vacancies_with_an_explanation(self):
        StationSlot.objects.filter(pk__in=(self.morning_slot.pk, self.afternoon_slot.pk)).update(capacity=2)
        full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        Assignment.objects.create(registration=self.registration(), station_slot=full_slot)
        result = whatsapp_preview(self.event, "vacancy_reminder")
        self.assertTrue(result["can_share"])
        self.assertIn("Journée", result["body_text"])
        self.assertIn("Matin", result["body_text"])
        self.assertIn("Après-midi", result["body_text"])
        self.assertEqual(result["body_text"].count(": 1 libre"), 2)
        self.assertIn(": 1 possible dans ces mêmes places", result["body_text"])
        self.assertEqual(result["body_text"].count(self.station.name), 1)
        self.assertIn("les disponibilités ne s’additionnent pas", result["body_text"])
        self.assertIn("Merci pour votre aide et votre bonne humeur 💚", result["body_text"])

    def test_full_day_assignments_suppress_half_day_reminder_when_shared_station_is_full(self):
        full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        for member in (self.member, self.other_member):
            Assignment.objects.create(registration=self.registration(member=member), station_slot=full_slot)
        result = whatsapp_preview(self.event, "vacancy_reminder")
        self.assertFalse(result["can_share"])
        self.assertEqual(result["share_url"], "")
        self.assertIn("Tous les postes sont pourvus", result["unavailable_reason"])

    def test_long_vacancy_list_is_bounded_and_points_to_complete_event(self):
        for index in range(12):
            station = Station.objects.create(event=self.event, name=f"Renfort numéro {index:02}", position=index + 10)
            StationSlot.objects.create(station=station, event_day=self.day, period="morning", capacity=1)
        result = whatsapp_preview(self.event, "vacancy_reminder")
        self.assertTrue(result["can_share"])
        self.assertIn("Renfort numéro 00", result["body_text"])
        self.assertNotIn("Renfort numéro 11", result["body_text"])
        self.assertIn(reverse("family_event_detail", args=[self.event.pk]), result["body_text"])

    def test_event_editor_has_only_email_options_and_disables_legacy_sms_on_save(self):
        response = self.client.get(reverse("event_update", args=[self.event.pk]))
        self.assertNotContains(response, 'name="notify_sms"')
        self.assertContains(response, 'name="notify_email"')
        form = EventForm(instance=self.event, data={
            "name": self.event.name, "location": self.event.location,
            "event_date": self.day.date.isoformat(),
            "registration_opens_at": self.event.registration_opens_at.isoformat(),
            "registration_closes_at": self.event.registration_closes_at.isoformat(),
            "meal_orders_close_at": self.event.meal_orders_close_at.isoformat(),
            "status": self.event.status, "notify_email": "on", "notify_sms": "on",
        })
        self.assertTrue(form.is_valid(), form.errors.as_json())
        form.save()
        self.event.refresh_from_db()
        self.assertFalse(self.event.notify_sms)
        self.assertTrue(self.event.notify_email)
