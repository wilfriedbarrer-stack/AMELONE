from datetime import timedelta
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Event,
    EventNotificationCampaign,
    EventNotificationDelivery,
    Family,
    Registration,
    StationSlot,
    User,
)

from .factories import DataMixin


@override_settings(EVENT_NOTIFICATIONS_ENABLED=False, SITE_URL="https://amelone.onrender.com")
class EventNotificationViewTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)
        self.url = reverse("event_notifications", args=[self.event.pk])

    def campaign(self, kind="publication", event=None):
        return EventNotificationCampaign.objects.get_or_create(event=event or self.event, kind=kind)[0]

    def delivery(self, *, campaign=None, family=None, channel="email", status="failed", **changes):
        return EventNotificationDelivery.objects.create(
            campaign=campaign or self.campaign(), family=family or self.family, channel=channel,
            status=status, recipient="private-recipient@example.test", subject="Objet privé",
            body_text="Message enregistré", **changes,
        )

    def other_event(self):
        return Event.objects.create(
            name="Autre événement privé", location="Autre lieu",
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )

    def test_get_has_previews_counts_real_link_and_no_writes_or_sends(self):
        self.registration()
        self.registration(member=self.other_member, status=Registration.Status.CANCELLED)
        campaign_count = EventNotificationCampaign.objects.count()
        delivery_count = EventNotificationDelivery.objects.count()
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms, patch("volunteers.views_notifications.retry_event_notification_failures") as retry:
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        email.assert_not_called()
        sms.assert_not_called()
        retry.assert_not_called()
        self.assertEqual(EventNotificationCampaign.objects.count(), campaign_count)
        self.assertEqual(EventNotificationDelivery.objects.count(), delivery_count)
        cards = {card["kind"]: card for card in response.context["cards"]}
        self.assertEqual(cards["publication"]["preview"]["recipient_count"], 2)
        self.assertEqual(cards["vacancy_reminder"]["preview"]["recipient_count"], 1)
        self.assertEqual(cards["vacancy_reminder"]["vacant_slot_count"], 2)
        self.assertEqual(cards["vacancy_reminder"]["scheduled_for"], self.event.registration_closes_at - timedelta(hours=48))
        self.assertContains(response, "48 heures avant la clôture")
        self.assertContains(response, "Les e-mails automatiques sont désactivés.")
        self.assertContains(response, "Le partage manuel sur WhatsApp reste disponible")
        self.assertContains(response, "https://amelone.onrender.com" + reverse("family_event_detail", args=[self.event.pk]))
        self.assertNotContains(response, "Réessayer les échecs")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_shared_alternatives_show_vacant_slot_count_without_adding_places(self):
        StationSlot.objects.filter(pk__in=(self.morning_slot.pk, self.afternoon_slot.pk)).update(capacity=2)
        full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        Assignment.objects.create(registration=self.registration(), station_slot=full_slot)
        response = self.client.get(self.url)
        card = next(card for card in response.context["cards"] if card["kind"] == "vacancy_reminder")
        self.assertEqual(card["vacant_slot_count"], 3)
        self.assertEqual({item["remaining"] for item in card["preview"]["vacancies"]}, {1})
        self.assertNotIn("remaining_places", card)
        self.assertContains(response, "Créneaux avec des places")
        self.assertContains(response, "les disponibilités ne s’additionnent pas")
        self.assertNotContains(response, "Places encore libres")
        self.assertContains(response, 'data-whatsapp-share="whatsapp-vacancy_reminder"')
        self.assertContains(response, 'data-whatsapp-copy="whatsapp-vacancy_reminder"')
        self.assertContains(response, 'js/whatsapp-sharing.js?v=2')

    def test_family_and_anonymous_cannot_read_private_history_or_retry(self):
        self.delivery(failure_reason="Motif réservé à l’organisation")
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)
        self.client.force_login(self.family_user)
        with patch("volunteers.views_notifications.retry_event_notification_failures") as retry:
            self.assertEqual(self.client.get(self.url).status_code, 403)
            self.assertEqual(self.client.post(self.url, {"action": "retry_failures"}).status_code, 403)
        retry.assert_not_called()

    def test_preview_available_for_draft_and_settings_are_linked_not_duplicated(self):
        self.event.status = Event.Status.DRAFT
        self.event.notify_publication = False
        self.event.notify_vacancies = False
        self.event.notify_email = False
        self.event.notify_sms = False
        self.event.save(update_fields=["status", "notify_publication", "notify_vacancies", "notify_email", "notify_sms"])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, reverse("event_update", args=[self.event.pk]) + "#notifications")
        self.assertContains(response, "après sa publication")
        for field in ("notify_publication", "notify_vacancies", "notify_email", "notify_sms"):
            self.assertNotContains(response, f'name="{field}"')
        self.assertEqual(len(response.context["cards"]), 2)
        self.assertFalse(any(card["enabled"] for card in response.context["cards"]))

    @override_settings(EVENT_NOTIFICATIONS_ENABLED=True, EMAIL_ENABLED=True, EMAIL_PROVIDER="brevo",
                       DEFAULT_FROM_EMAIL="AMELONE <association@example.test>",
                       BREVO_API_KEY="SENSITIVE_KEY_MUST_NEVER_BE_RENDERED", SMS_ENABLED=False)
    def test_channel_configuration_is_shown_without_secret_values(self):
        response = self.client.get(self.url)
        self.assertTrue(response.context["automation_enabled"])
        self.assertTrue(response.context["channels"][0]["ready"])
        self.assertEqual(len(response.context["channels"]), 1)
        self.assertContains(response, "via Brevo est configuré")
        self.assertContains(response, "WhatsApp manuel")
        self.assertNotContains(response, "L’envoi de SMS")
        self.assertNotContains(response, "SENSITIVE_KEY_MUST_NEVER_BE_RENDERED")

    def test_post_retries_only_failed_for_this_event_without_sending(self):
        failed = self.delivery(attempt_count=1, failure_reason="Erreur de configuration")
        uncertain = self.delivery(channel="sms", status="uncertain", attempt_count=1, failure_reason="À vérifier")
        sending = self.delivery(family=self.other_family, status="sending", attempt_count=1)
        accepted = self.delivery(family=self.other_family, channel="sms", status="accepted", accepted_at=timezone.now())
        pending = self.delivery(campaign=self.campaign("vacancy_reminder"), status="pending")
        skipped = self.delivery(campaign=self.campaign("vacancy_reminder"), channel="sms", status="skipped")
        foreign = self.delivery(campaign=self.campaign(event=self.other_event()), failure_reason="Autre échec")
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            response = self.client.post(self.url, {"action": "retry_failures"})
        self.assertRedirects(response, self.url)
        email.assert_not_called()
        sms.assert_not_called()
        failed.refresh_from_db()
        self.assertEqual(failed.status, "pending")
        self.assertEqual(failed.attempt_count, 1)
        self.assertEqual(failed.failure_reason, "")
        for delivery, expected in ((uncertain, "uncertain"), (sending, "sending"), (accepted, "accepted"),
                                   (pending, "pending"), (skipped, "skipped"), (foreign, "failed")):
            delivery.refresh_from_db()
            self.assertEqual(delivery.status, expected)

    def test_unknown_action_and_missing_csrf_cannot_trigger_retry(self):
        self.delivery()
        with patch("volunteers.views_notifications.retry_event_notification_failures") as retry:
            response = self.client.post(self.url, {"action": "send_all"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["retry_form"].errors)
            protected = Client(enforce_csrf_checks=True)
            protected.force_login(self.organizer)
            self.assertEqual(protected.post(self.url, {"action": "retry_failures"}).status_code, 403)
            retry.assert_not_called()

    def test_uncertain_only_history_offers_no_retry_button(self):
        self.delivery(status="uncertain", attempt_count=1, failure_reason="Résultat à vérifier")
        response = self.client.get(self.url)
        self.assertContains(response, "Résultat incertain")
        self.assertContains(response, "vérifiés auprès du prestataire")
        self.assertNotContains(response, "Réessayer les échecs")

    def test_skipped_campaign_failures_offer_no_retry_button(self):
        campaign = self.campaign()
        campaign.skipped_reason = "Événement déjà publié avant activation."
        campaign.save(update_fields=["skipped_reason"])
        self.delivery(campaign=campaign, failure_reason="Ancien échec conservé")
        response = self.client.get(self.url)
        self.assertContains(response, "Ancien échec conservé")
        self.assertEqual(response.context["failed_count"], 0)
        self.assertNotContains(response, "Réessayer les échecs")

    def test_history_is_scoped_paged_twenty_and_hides_recipient_and_provider_id(self):
        campaign = self.campaign()
        for index in range(21):
            user = User.objects.create_user(email=f"notification-{index}@example.test")
            family = Family.objects.create(account=user, name=f"Famille notification {index:02}")
            self.delivery(campaign=campaign, family=family, status="accepted", provider_message_id="PRIVATE_PROVIDER_REFERENCE")
        self.delivery(campaign=self.campaign(event=self.other_event()), failure_reason="FOREIGN_EVENT_ERROR")
        response = self.client.get(self.url)
        self.assertEqual(response.context["page_obj"].paginator.count, 21)
        self.assertEqual(len(response.context["page_obj"]), 20)
        self.assertContains(response, "Suivant")
        self.assertNotContains(response, "FOREIGN_EVENT_ERROR")
        self.assertNotContains(response, "private-recipient@example.test")
        self.assertNotContains(response, "PRIVATE_PROVIDER_REFERENCE")
        response = self.client.get(self.url, {"page": "2"})
        self.assertEqual(len(response.context["page_obj"]), 1)
        self.assertEqual(response.context["page_obj"].number, 2)
        self.assertEqual(self.client.get(self.url, {"page": "invalid"}).context["page_obj"].number, 1)

    def test_preview_and_failure_text_are_escaped(self):
        self.event.name = '<script>alert("event")</script>'
        self.event.save(update_fields=["name"])
        self.delivery(failure_reason='<script>alert("failure")</script>')
        response = self.client.get(self.url)
        self.assertNotContains(response, '<script>alert("event")</script>')
        self.assertNotContains(response, '<script>alert("failure")</script>')
        self.assertContains(response, "&lt;script&gt;")

    def test_retry_validation_error_is_visible_without_automatic_send(self):
        self.delivery()
        with patch("volunteers.views_notifications.retry_event_notification_failures", side_effect=ValidationError("Actualisez cette page.")):
            response = self.client.post(self.url, {"action": "retry_failures"})
        self.assertContains(response, "Actualisez cette page.")
        self.assertEqual(EventNotificationDelivery.objects.get().status, "failed")
