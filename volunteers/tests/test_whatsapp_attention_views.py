import re
from datetime import timedelta
from unittest.mock import patch

from django.db import connection
from django.test import Client, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from volunteers.models import (
    Event,
    EventNotificationCampaign,
    EventNotificationDelivery,
    EventWhatsAppShare,
    User,
)
from volunteers.whatsapp_attention import get_whatsapp_attention

from .factories import DataMixin


@override_settings(EVENT_NOTIFICATIONS_ENABLED=False, EMAIL_ENABLED=False, SMS_ENABLED=False)
class WhatsAppAttentionViewTests(DataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        now = timezone.now()
        cls.day.date = timezone.localdate() + timedelta(days=7)
        cls.day.save(update_fields=("date",))
        cls.event.registration_closes_at = now + timedelta(days=1)
        cls.event.notify_publication = cls.event.notify_vacancies = False
        cls.event.notify_email = cls.event.notify_sms = False
        cls.event.save(update_fields=(
            "registration_closes_at", "notify_publication", "notify_vacancies",
            "notify_email", "notify_sms",
        ))
        cls.other_organizer = User.objects.create_user(
            email="whatsapp-organizer@example.test", password=None, is_organizer=True,
        )
        cls.staff = User.objects.create_user(
            email="whatsapp-staff@example.test", password=None, is_staff=True,
        )
        cls.administrator = User.objects.create_superuser(
            email="whatsapp-admin@example.test", password=None,
        )

    def setUp(self):
        self.notifications_url = reverse("event_notifications", args=[self.event.pk])
        self.status_url = reverse("whatsapp_reminder_status", args=[self.event.pk])

    def status_data(self, action="mark_shared", **changes):
        return {
            "action": action,
            "registration_closes_at": self.event.registration_closes_at.isoformat(),
            **changes,
        }

    def assert_pending(self, count):
        response = self.client.get(reverse("organizer_dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["pending_whatsapp_count"], count)
        self.assertEqual(response.context["whatsapp_attention"]["count"], count)
        if count:
            self.assertContains(response, "whatsapp-attention-link")
        else:
            self.assertNotContains(response, "whatsapp-attention-link")
        return response

    def test_anonymous_and_family_never_see_badge_or_load_whatsapp_attention(self):
        for actor in (None, self.family_user):
            with self.subTest(actor=actor):
                if actor is None:
                    self.client.logout()
                else:
                    self.client.force_login(actor)
                with patch(
                    "volunteers.context_processors.get_whatsapp_attention",
                    wraps=get_whatsapp_attention,
                ) as attention:
                    response = self.client.get(reverse("home"))
                self.assertEqual(response.status_code, 200)
                self.assertNotContains(response, "whatsapp-attention-link")
                self.assertEqual(response.context["pending_whatsapp_count"], 0)
                attention.assert_not_called()

    def test_organizer_staff_and_administrator_see_due_badge_with_channels_disabled(self):
        for actor in (self.other_organizer, self.staff, self.administrator):
            with self.subTest(actor=actor):
                self.client.force_login(actor)
                response = self.client.get(reverse("home"))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["pending_whatsapp_count"], 1)
                self.assertContains(response, "whatsapp-attention-link")
                self.assertContains(
                    response, f'href="{reverse("organizer_dashboard")}#whatsapp-a-envoyer"',
                )

    def test_dashboard_lists_the_due_reminder_and_links_directly_to_its_message(self):
        self.client.force_login(self.organizer)
        response = self.assert_pending(1)
        self.assertContains(response, 'id="whatsapp-a-envoyer"')
        self.assertContains(
            response, f'href="{self.notifications_url}#whatsapp-vacancy_reminder-title"',
        )
        tasks = response.context["whatsapp_attention"]["tasks"]
        self.assertEqual([task["event_id"] for task in tasks], [self.event.pk])
        self.assertEqual(tasks[0]["kind"], "vacancy_reminder")
        self.assertContains(response, self.event.name)

    def test_dashboard_and_navigation_reuse_one_summary_per_request(self):
        self.client.force_login(self.organizer)
        with patch(
            "volunteers.context_processors.get_whatsapp_attention",
            wraps=get_whatsapp_attention,
        ) as attention:
            self.assert_pending(1)
            self.assertEqual(attention.call_count, 1)
            self.assert_pending(1)
            self.assertEqual(attention.call_count, 2)

    def test_get_pages_do_not_acknowledge_reminders_write_or_send(self):
        self.client.force_login(self.organizer)
        urls = [
            reverse("home"),
            reverse("organizer_dashboard"),
            reverse("event_list"),
            reverse("event_detail", args=[self.event.pk]),
            self.notifications_url,
        ]
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            for url in urls:
                with self.subTest(url=url), CaptureQueriesContext(connection) as queries:
                    response = self.client.get(url)
                    self.assertEqual(response.status_code, 200)
                writes = [
                    query["sql"] for query in queries
                    if re.match(
                        r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|ALTER|DROP)\b",
                        query["sql"], re.I,
                    )
                ]
                self.assertEqual(writes, [], msg=f"GET {url} unexpectedly writes to the database")
            email.assert_not_called()
            sms.assert_not_called()
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_copying_opening_and_query_string_actions_do_not_declare_a_share(self):
        self.client.force_login(self.organizer)
        response = self.client.get(self.notifications_url, self.status_data())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-whatsapp-copy="whatsapp-vacancy_reminder"')
        self.assertContains(response, 'data-whatsapp-share="whatsapp-vacancy_reminder"')
        self.assertContains(response, "https://wa.me/")
        self.assertFalse(EventWhatsAppShare.objects.exists())
        self.assert_pending(1)

    def test_event_card_has_an_explicit_post_form_and_current_deadline_snapshot(self):
        self.client.force_login(self.organizer)
        response = self.client.get(self.notifications_url)
        self.assertContains(response, f'action="{self.status_url}"')
        self.assertContains(response, 'method="post"')
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'value="mark_shared"')
        self.assertContains(response, 'name="registration_closes_at"')
        snapshot = re.search(
            r'name="registration_closes_at"\s+value="([^"]+)"',
            response.content.decode(),
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(parse_datetime(snapshot[1]), self.event.registration_closes_at)
        self.assertContains(response, "Rappel WhatsApp à envoyer")
        self.assertContains(response, "J’ai envoyé le message dans le groupe")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_status_endpoint_is_private_and_post_only(self):
        self.assertEqual(self.client.get(self.status_url).status_code, 302)
        response = self.client.post(self.status_url, self.status_data())
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        self.client.force_login(self.family_user)
        self.assertEqual(self.client.get(self.status_url).status_code, 403)
        self.assertEqual(self.client.post(self.status_url, self.status_data()).status_code, 403)
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(self.status_url).status_code, 405)
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_post_without_csrf_does_not_acknowledge_the_reminder(self):
        protected = Client(enforce_csrf_checks=True)
        protected.force_login(self.organizer)
        response = protected.post(self.status_url, self.status_data())
        self.assertEqual(response.status_code, 403)
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_mark_shared_hides_badge_for_every_organizer_on_the_next_request(self):
        self.client.force_login(self.organizer)
        self.assert_pending(1)
        automatic_notifications_before = (
            list(EventNotificationCampaign.objects.order_by("pk").values()),
            list(EventNotificationDelivery.objects.order_by("pk").values()),
        )
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            response = self.client.post(self.status_url, self.status_data())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.notifications_url + "#whatsapp-vacancy_reminder-title")
        self.assertIn("no-store", response.headers["Cache-Control"])
        email.assert_not_called()
        sms.assert_not_called()
        share = EventWhatsAppShare.objects.get(event=self.event, kind="vacancy_reminder")
        self.assertEqual(share.shared_by, self.organizer)
        self.assertEqual(share.registration_closes_at, self.event.registration_closes_at)
        self.assertEqual(automatic_notifications_before, (
            list(EventNotificationCampaign.objects.order_by("pk").values()),
            list(EventNotificationDelivery.objects.order_by("pk").values()),
        ))
        for actor in (self.organizer, self.other_organizer, self.staff, self.administrator):
            with self.subTest(actor=actor):
                self.client.force_login(actor)
                self.assert_pending(0)
        card = self.client.get(self.notifications_url)
        self.assertContains(card, 'value="reset_shared"')
        self.assertNotContains(card, 'value="mark_shared"')
        self.assertContains(card, "Partage déclaré le")
        self.assertContains(card, "Remettre l’alerte")

    def test_other_organizer_can_reset_a_share_and_restore_the_alert(self):
        self.client.force_login(self.organizer)
        self.client.post(self.status_url, self.status_data())
        self.client.force_login(self.other_organizer)
        self.assert_pending(0)
        response = self.client.post(self.status_url, self.status_data("reset_shared"))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(EventWhatsAppShare.objects.exists())
        self.assert_pending(1)
        self.client.force_login(self.organizer)
        self.assert_pending(1)
        card = self.client.get(self.notifications_url)
        self.assertContains(card, 'value="mark_shared"')

    def test_missing_invalid_snapshot_and_unknown_action_cannot_acknowledge(self):
        self.client.force_login(self.organizer)
        missing_snapshot = {"action": "mark_shared"}
        for data in (
            missing_snapshot,
            self.status_data(registration_closes_at="invalid-date"),
            self.status_data(action="send_all"),
        ):
            with self.subTest(data=data):
                response = self.client.post(self.status_url, data)
                self.assertEqual(response.status_code, 302)
                self.assertFalse(EventWhatsAppShare.objects.exists())
                self.assert_pending(1)

    def test_stale_confirmation_cannot_acknowledge_a_changed_deadline(self):
        self.client.force_login(self.organizer)
        stale_data = self.status_data()
        Event.objects.filter(pk=self.event.pk).update(
            registration_closes_at=self.event.registration_closes_at + timedelta(hours=2),
        )
        response = self.client.post(self.status_url, stale_data, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La clôture des inscriptions a changé.")
        self.assertFalse(EventWhatsAppShare.objects.exists())
        self.assert_pending(1)

    def test_stale_reset_cannot_remove_a_previous_declaration(self):
        self.client.force_login(self.organizer)
        self.client.post(self.status_url, self.status_data())
        share = EventWhatsAppShare.objects.get(event=self.event, kind="vacancy_reminder")
        Event.objects.filter(pk=self.event.pk).update(
            registration_closes_at=self.event.registration_closes_at + timedelta(hours=2),
        )
        response = self.client.post(
            self.status_url, self.status_data("reset_shared"), follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "La clôture des inscriptions a changé.")
        self.assertTrue(EventWhatsAppShare.objects.filter(pk=share.pk).exists())
        self.assert_pending(1)

    def test_changed_deadline_reopens_the_alert_after_a_previous_declaration(self):
        self.client.force_login(self.organizer)
        self.client.post(self.status_url, self.status_data())
        self.assert_pending(0)
        self.event.registration_closes_at += timedelta(hours=2)
        self.event.save(update_fields=("registration_closes_at",))
        self.assert_pending(1)
        response = self.client.post(self.status_url, self.status_data())
        self.assertEqual(response.status_code, 302)
        self.assert_pending(0)
        self.assertEqual(EventWhatsAppShare.objects.count(), 1)

    def test_completed_automatic_email_campaign_does_not_clear_manual_alert(self):
        campaign = EventNotificationCampaign.objects.create(
            event=self.event, kind="vacancy_reminder", completed_at=timezone.now(),
        )
        EventNotificationDelivery.objects.create(
            campaign=campaign, family=self.family, channel="email", status="accepted",
            body_text="Rappel automatique", accepted_at=timezone.now(),
        )
        self.client.force_login(self.organizer)
        self.assert_pending(1)

    def test_draft_archived_closed_and_past_events_do_not_show_an_alert(self):
        self.client.force_login(self.organizer)
        now = timezone.now()
        for changes, day in (
            ({"status": Event.Status.DRAFT}, self.day.date),
            ({"status": Event.Status.ARCHIVED}, self.day.date),
            ({"registration_closes_at": now - timedelta(seconds=1)}, self.day.date),
            ({}, timezone.localdate() - timedelta(days=1)),
        ):
            with self.subTest(changes=changes, day=day):
                Event.objects.filter(pk=self.event.pk).update(**{
                    "status": Event.Status.PUBLISHED,
                    "registration_closes_at": self.event.registration_closes_at,
                    **changes,
                })
                self.event.days.update(date=day)
                self.assert_pending(0)

    def test_draft_closed_and_past_events_cannot_be_marked_shared_by_post(self):
        self.client.force_login(self.organizer)
        now = timezone.now()
        for changes, day in (
            ({"status": Event.Status.DRAFT}, self.day.date),
            ({"registration_closes_at": now - timedelta(seconds=1)}, self.day.date),
            ({}, timezone.localdate() - timedelta(days=1)),
        ):
            with self.subTest(changes=changes, day=day):
                Event.objects.filter(pk=self.event.pk).update(**{
                    "status": Event.Status.PUBLISHED,
                    "registration_closes_at": self.event.registration_closes_at,
                    **changes,
                })
                self.event.days.update(date=day)
                current_event = Event.objects.get(pk=self.event.pk)
                data = self.status_data(
                    registration_closes_at=current_event.registration_closes_at.isoformat(),
                )
                response = self.client.post(self.status_url, data)
                self.assertEqual(response.status_code, 302)
                self.assertFalse(EventWhatsAppShare.objects.exists())
