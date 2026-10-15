import io
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier
from unittest.mock import patch

from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, override_settings, skipUnlessDBFeature
from django.utils import timezone

from volunteers.event_notifications import (
    notification_preview,
    process_event_notifications,
    record_event_publication,
    retry_event_notification_failures,
)
from volunteers.models import (
    Event,
    EventDay,
    EventNotificationCampaign,
    EventNotificationDelivery,
    Family,
    Registration,
    StationSlot,
    User,
)
from volunteers.notification_transports import NotificationTransportError
from volunteers.services import create_assignment, remove_assignment

from .factories import DataMixin


@override_settings(EVENT_NOTIFICATIONS_ENABLED=True, SITE_URL="https://amelone.example")
class EventNotificationTests(DataMixin, TestCase):
    def setUp(self):
        self.now = timezone.now()
        Event.objects.filter(pk=self.event.pk).update(
            registration_closes_at=self.now + timedelta(days=10),
        )
        self.event.refresh_from_db()
        self.email = self.enterContext(patch(
            "volunteers.event_notifications.send_notification_email", return_value="email-accepted",
        ))
        self.sms = self.enterContext(patch(
            "volunteers.event_notifications.send_notification_sms", return_value="sms-accepted",
        ))
        self.readiness = self.enterContext(patch(
            "volunteers.event_notifications.get_channel_readiness",
            return_value={"ready": True, "reason": "Prêt"},
        ))

    def campaign(self, kind="publication"):
        return EventNotificationCampaign.objects.get(event=self.event, kind=kind)

    def suppress_publication(self):
        EventNotificationCampaign.objects.filter(event=self.event, kind="publication").update(
            completed_at=self.now, skipped_reason="Annonce historique déjà traitée.",
        )

    def reminder_due(self, hours=47):
        self.suppress_publication()
        Event.objects.filter(pk=self.event.pk).update(
            registration_closes_at=self.now + timedelta(hours=hours),
        )
        self.event.refresh_from_db()

    def deliveries(self, kind="publication"):
        return EventNotificationDelivery.objects.filter(campaign__event=self.event, campaign__kind=kind)

    def test_publication_hook_records_once_without_sending_and_does_not_repeat_on_republication(self):
        self.assertEqual(EventNotificationCampaign.objects.filter(event=self.event).count(), 1)
        self.assertFalse(EventNotificationDelivery.objects.exists())
        self.email.assert_not_called()
        self.sms.assert_not_called()
        process_event_notifications(now=self.now)
        self.assertEqual(self.email.call_count, 2)
        self.assertEqual(self.sms.call_count, 1)
        self.event.name = "Nom corrigé"
        self.event.save(update_fields=("name",))
        self.event.status = Event.Status.ARCHIVED
        self.event.save(update_fields=("status",))
        self.event.status = Event.Status.PUBLISHED
        self.event.save(update_fields=("status",))
        record_event_publication(self.event)
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertEqual(self.email.call_count, 2)
        self.assertEqual(self.sms.call_count, 1)
        self.assertEqual(EventNotificationCampaign.objects.filter(event=self.event, kind="publication").count(), 1)

    def test_publication_reaches_each_active_family_once_per_channel_and_never_exposes_other_contacts(self):
        self.registration()
        stats = process_event_notifications(now=self.now)
        self.assertEqual(stats["accepted"], 3)
        self.assertEqual(set(self.deliveries().values_list("family_id", flat=True)), {self.family.pk, self.other_family.pk})
        self.assertEqual(self.deliveries().count(), 4)
        for call in self.email.call_args_list:
            self.assertIn(call.kwargs["to"], {self.family_user.email, self.other_user.email})
            for contact in (self.family_user.email, self.other_user.email, self.family_user.phone):
                self.assertNotIn(contact, call.kwargs["body"])
            self.assertIn(f"https://amelone.example/famille/evenements/{self.event.pk}/", call.kwargs["body"])
        missing = self.deliveries().get(family=self.other_family, channel="sms")
        self.assertEqual(missing.status, "skipped")
        self.assertIsNone(missing.accepted_at)
        self.assertEqual(missing.attempt_count, 0)

    @override_settings(EVENT_NOTIFICATIONS_ENABLED=False)
    def test_global_switch_prevents_preparation_and_transport_calls(self):
        stats = process_event_notifications(now=self.now)
        self.assertFalse(stats["enabled"])
        self.assertFalse(EventNotificationDelivery.objects.exists())
        self.assertIsNone(self.campaign().prepared_at)
        self.email.assert_not_called()
        self.sms.assert_not_called()

    def test_dry_run_and_preview_are_read_only_even_when_reminder_is_due(self):
        self.reminder_due()
        before = list(EventNotificationCampaign.objects.values())
        preview = notification_preview(self.event, "vacancy_reminder", now=self.now)
        self.assertEqual(preview["recipient_count"], 2)
        self.assertEqual(sum(item["remaining"] for item in preview["vacancies"]), 3)
        self.assertIn("heure de Paris", preview["body_text"])
        self.assertIn("dimanche 28 février 2027", preview["body_text"])
        result = process_event_notifications(now=self.now, dry_run=True)
        self.assertEqual(result["reminder_events_due"], 1)
        self.assertEqual(list(EventNotificationCampaign.objects.values()), before)
        self.assertFalse(EventNotificationDelivery.objects.exists())
        output = io.StringIO()
        call_command("process_event_notifications", "--dry-run", "--limit=3", stdout=output)
        self.assertIn('"dry_run": true', output.getvalue())
        self.email.assert_not_called()
        self.sms.assert_not_called()

    def test_sms_copy_is_short_without_forcing_unicode_or_claiming_early_opening(self):
        self.event.name = "Concours " + "A" * 100
        self.event.registration_opens_at = self.now + timedelta(days=1)
        early = notification_preview(self.event, "publication", now=self.now)["sms_text"]
        self.assertTrue(early.startswith("AMELONE : Nouvel événement"))
        self.assertIn("...", early)
        self.assertNotIn("—", early)
        self.assertNotIn("…", early)
        self.assertNotIn("Les inscriptions sont ouvertes", early)
        self.event.registration_opens_at = self.now - timedelta(days=1)
        opened = notification_preview(self.event, "publication", now=self.now)["sms_text"]
        self.assertIn("Les inscriptions sont ouvertes", opened)
        reminder = notification_preview(self.event, "vacancy_reminder", now=self.now)["sms_text"]
        self.assertIn("Un coup de main ?", reminder)
        self.event.name = "CSO — été"
        self.assertIn(self.event.name, notification_preview(self.event, "publication", now=self.now)["sms_text"])

    def test_unconfigured_channel_stays_pending_without_claim_or_acceptance(self):
        self.readiness.return_value = {"ready": False, "reason": "Prestataire non configuré"}
        first = process_event_notifications(now=self.now)
        self.assertEqual(first["waiting"], 3)
        self.assertEqual(self.deliveries().filter(status="pending", attempt_count=0).count(), 3)
        self.assertFalse(self.deliveries().exclude(accepted_at=None).exists())
        self.email.assert_not_called()
        self.sms.assert_not_called()
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        second = process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertEqual(second["accepted"], 3)
        self.assertEqual(self.deliveries().filter(status="accepted", attempt_count=1).count(), 3)

    def test_recipient_list_is_frozen_after_first_preparation(self):
        self.readiness.return_value = {"ready": False, "reason": "À configurer"}
        process_event_notifications(now=self.now)
        newcomer = User.objects.create_user(email="nouveau@example.test", phone="0701020304")
        new_family = Family.objects.create(account=newcomer, name="Nouvelle famille")
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertFalse(self.deliveries().filter(family=new_family).exists())
        self.assertEqual(self.email.call_count, 2)

    def test_invalid_contact_and_inactive_account_are_never_accepted(self):
        User.objects.filter(pk=self.family_user.pk).update(email="invalide", phone="01 02 03 04 05")
        User.objects.filter(pk=self.other_user.pk).update(is_active=False)
        process_event_notifications(now=self.now)
        self.assertEqual(self.deliveries().count(), 2)
        self.assertEqual(self.deliveries().filter(status="skipped", attempt_count=0).count(), 2)
        self.email.assert_not_called()
        self.sms.assert_not_called()

    def test_disabling_a_publication_at_first_publication_permanently_suppresses_announcement(self):
        event = Event.objects.create(
            name="Sans annonce", location="Carrière", status=Event.Status.PUBLISHED,
            notify_publication=False, registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        campaign = EventNotificationCampaign.objects.get(event=event, kind="publication")
        self.assertIsNotNone(campaign.completed_at)
        self.assertTrue(campaign.skipped_reason)
        event.notify_publication = True
        event.save(update_fields=("notify_publication",))
        self.assertEqual(record_event_publication(event).pk, campaign.pk)
        self.assertFalse(EventNotificationDelivery.objects.filter(campaign=campaign).exists())

    def test_email_and_sms_choices_are_independent(self):
        Event.objects.filter(pk=self.event.pk).update(notify_email=False)
        stats = process_event_notifications(now=self.now)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(self.deliveries().filter(channel="email", status="skipped").count(), 2)
        self.email.assert_not_called()
        self.sms.assert_called_once()

    def test_reminder_is_sent_only_inside_48_hour_window_and_to_nonregistered_families(self):
        self.reminder_due(hours=49)
        self.registration()
        process_event_notifications(now=self.now)
        self.assertFalse(EventNotificationCampaign.objects.filter(kind="vacancy_reminder").exists())
        at_boundary = self.now + timedelta(hours=1)
        stats = process_event_notifications(now=at_boundary)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(set(self.deliveries("vacancy_reminder").values_list("family_id", flat=True)), {self.other_family.pk})
        self.assertIn("votre aide sera la bienvenue", self.email.call_args.kwargs["body"])
        process_event_notifications(now=at_boundary + timedelta(minutes=5))
        self.email.assert_called_once()

    def test_cancelled_registration_does_not_exclude_family_from_reminder(self):
        self.reminder_due()
        registration = self.registration()
        Registration.objects.filter(pk=registration.pk).update(status=Registration.Status.CANCELLED)
        process_event_notifications(now=self.now)
        self.assertEqual(set(self.deliveries("vacancy_reminder").values_list("family_id", flat=True)), {self.family.pk, self.other_family.pk})

    def test_fully_occupied_slots_including_drafts_suppress_reminder(self):
        self.reminder_due()
        registration = self.registration()
        create_assignment(registration=registration, station_slot=self.morning_slot, actor=self.organizer)
        StationSlot.objects.filter(pk=self.afternoon_slot.pk).update(capacity=0)
        process_event_notifications(now=self.now)
        self.assertIn("pourvus", self.campaign("vacancy_reminder").skipped_reason)
        self.assertFalse(self.deliveries("vacancy_reminder").exists())
        self.email.assert_not_called()

    def test_cancelled_assignment_and_inactive_station_do_not_create_false_occupancy_or_vacancies(self):
        self.reminder_due()
        assignment = create_assignment(
            registration=self.registration(), station_slot=self.morning_slot, actor=self.organizer,
        )
        remove_assignment(assignment=assignment, actor=self.organizer)
        self.station.is_active = False
        self.station.save(update_fields=("is_active",))
        preview = notification_preview(self.event, "vacancy_reminder", now=self.now)
        self.assertFalse(preview["vacancies"])
        self.station.is_active = True
        self.station.save(update_fields=("is_active",))
        preview = notification_preview(self.event, "vacancy_reminder", now=self.now)
        self.assertEqual(sum(item["remaining"] for item in preview["vacancies"]), 3)

    def test_account_deactivation_or_contact_change_after_preparation_prevents_delivery(self):
        self.readiness.return_value = {"ready": False, "reason": "À configurer"}
        process_event_notifications(now=self.now)
        User.objects.filter(pk=self.family_user.pk).update(is_active=False)
        User.objects.filter(pk=self.other_user.pk).update(email="nouvelle@example.test")
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        stats = process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertEqual(stats["accepted"], 0)
        self.assertEqual(self.deliveries().filter(status="skipped").count(), 4)
        self.email.assert_not_called()
        self.sms.assert_not_called()

    def test_family_registering_while_waiting_does_not_receive_reminder(self):
        self.reminder_due()
        self.readiness.return_value = {"ready": False, "reason": "À configurer"}
        process_event_notifications(now=self.now)
        self.registration()
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertEqual(self.email.call_count, 1)
        self.assertEqual(self.email.call_args.kwargs["to"], self.other_user.email)
        self.assertFalse(self.deliveries("vacancy_reminder").filter(family=self.family, status="accepted").exists())

    def test_archive_prevents_waiting_announcement(self):
        self.readiness.return_value = {"ready": False, "reason": "À configurer"}
        process_event_notifications(now=self.now)
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.ARCHIVED)
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertFalse(self.deliveries().filter(status="accepted").exists())
        self.email.assert_not_called()

    def test_closed_or_past_event_is_skipped_without_preparing_recipients(self):
        for state in ("closed", "past"):
            with self.subTest(state=state):
                Event.objects.filter(pk=self.event.pk).update(
                    registration_closes_at=self.now - timedelta(seconds=1)
                    if state == "closed" else self.now + timedelta(days=10),
                )
                if state == "past":
                    EventDay.objects.filter(pk=self.day.pk).update(date=self.now.date() - timedelta(days=1))
                EventNotificationCampaign.objects.filter(event=self.event).update(
                    completed_at=None, skipped_reason="",
                )
                process_event_notifications(now=self.now)
                self.assertIsNotNone(self.campaign().completed_at)
                self.assertTrue(self.campaign().skipped_reason)
                self.assertFalse(EventNotificationDelivery.objects.exists())
        self.email.assert_not_called()
        self.sms.assert_not_called()

    def test_transport_only_runs_after_durable_sending_claim(self):
        def inspect_claim(**kwargs):
            delivery_id = kwargs["idempotency_key"].removeprefix("amelone-event-notification-")
            delivery = EventNotificationDelivery.objects.get(pk=delivery_id)
            self.assertEqual(delivery.status, "sending")
            self.assertEqual(delivery.attempt_count, 1)
            self.assertEqual(delivery.last_attempt_at, self.now)
            self.assertFalse(any(
                not getattr(block, "_from_testcase", False)
                for block in transaction.get_connection().atomic_blocks
            ))
            return "accepted-after-claim"

        self.email.side_effect = inspect_claim
        self.sms.side_effect = inspect_claim
        self.assertEqual(process_event_notifications(now=self.now)["accepted"], 3)

    def test_places_and_closing_are_rechecked_before_each_delivery(self):
        self.reminder_due()
        User.objects.filter(pk=self.other_user.pk).update(phone="0701020304")

        def accept_and_fill(**kwargs):
            StationSlot.objects.filter(station__event=self.event).update(capacity=0)
            return "accepted-once"

        self.email.side_effect = accept_and_fill
        self.sms.side_effect = accept_and_fill
        stats = process_event_notifications(now=self.now)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(self.deliveries("vacancy_reminder").filter(status="skipped").count(), 3)

    def test_failed_delivery_requires_explicit_authorized_retry_and_never_replays_accepted(self):
        self.sms.side_effect = NotificationTransportError("Envoi refusé.")
        first = process_event_notifications(now=self.now)
        self.assertEqual(first["failed"], 1)
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.sms.assert_called_once()
        with self.assertRaises(PermissionDenied):
            retry_event_notification_failures(self.event, self.family_user)
        self.assertEqual(retry_event_notification_failures(self.event, self.organizer), 1)
        self.sms.side_effect = None
        process_event_notifications(now=self.now + timedelta(minutes=2))
        self.assertEqual(self.email.call_count, 2)
        self.assertEqual(self.sms.call_count, 2)
        delivery = self.deliveries().get(family=self.family, channel="sms")
        self.assertEqual(delivery.status, "accepted")
        self.assertEqual(delivery.attempt_count, 2)
        self.assertEqual(self.sms.call_args_list[0].kwargs["idempotency_key"], self.sms.call_args_list[1].kwargs["idempotency_key"])

    def test_uncertain_transport_result_is_never_automatically_or_manually_retried(self):
        self.sms.side_effect = NotificationTransportError("Réponse inconnue.", uncertain=True)
        process_event_notifications(now=self.now)
        self.assertEqual(self.deliveries().get(channel="sms", family=self.family).status, "uncertain")
        self.assertEqual(retry_event_notification_failures(self.event, self.organizer), 0)
        process_event_notifications(now=self.now + timedelta(hours=1))
        self.sms.assert_called_once()

    def test_interrupted_sending_becomes_uncertain_after_grace_without_resending(self):
        self.readiness.return_value = {"ready": False, "reason": "À configurer"}
        process_event_notifications(now=self.now)
        delivery = self.deliveries().get(channel="sms", family=self.family)
        EventNotificationDelivery.objects.filter(pk=delivery.pk).update(
            status="sending", last_attempt_at=self.now, attempt_count=1,
        )
        self.readiness.return_value = {"ready": True, "reason": "Prêt"}
        process_event_notifications(now=self.now + timedelta(minutes=10))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "sending")
        process_event_notifications(now=self.now + timedelta(minutes=16))
        delivery.refresh_from_db()
        self.assertEqual(delivery.status, "uncertain")
        self.sms.assert_not_called()

    def test_limit_caps_transport_attempts_and_next_run_completes_without_duplicates(self):
        first = process_event_notifications(now=self.now, limit=1)
        self.assertEqual(first["attempted"], 1)
        process_event_notifications(now=self.now + timedelta(minutes=1), limit=1)
        process_event_notifications(now=self.now + timedelta(minutes=2), limit=1)
        self.assertEqual(self.email.call_count + self.sms.call_count, 3)
        self.assertEqual(self.deliveries().filter(status="accepted", attempt_count=1).count(), 3)
        self.assertIsNotNone(self.campaign().completed_at)

    def test_failed_old_campaign_does_not_starve_next_campaign_when_limit_is_one(self):
        self.email.side_effect = NotificationTransportError("Refus certain.")
        self.sms.side_effect = NotificationTransportError("Refus certain.")
        process_event_notifications(now=self.now)
        second = Event.objects.create(
            name="Nouvel événement à annoncer", location="Carrière", status=Event.Status.PUBLISHED,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        EventDay.objects.create(event=second, date=self.day.date)
        self.email.side_effect = None
        self.sms.side_effect = None
        stats = process_event_notifications(now=self.now + timedelta(minutes=1), limit=1)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(EventNotificationDelivery.objects.filter(campaign__event=second, status="accepted").count(), 1)
        self.assertEqual(self.deliveries().filter(status="failed").count(), 3)

    def test_real_clock_is_refreshed_between_sends_to_stop_at_closing(self):
        closing = self.now + timedelta(minutes=1)
        Event.objects.filter(pk=self.event.pk).update(registration_closes_at=closing, notify_vacancies=False)
        current = [self.now]

        def accept_and_advance_clock(**kwargs):
            current[0] = closing + timedelta(minutes=1)
            return "accepted-before-closing"

        self.email.side_effect = accept_and_advance_clock
        self.sms.side_effect = accept_and_advance_clock
        with patch("volunteers.event_notifications.timezone.now", side_effect=lambda: current[0]):
            result = process_event_notifications()
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(self.email.call_count + self.sms.call_count, 1)
        self.assertEqual(self.deliveries().filter(status="skipped").count(), 3)

    def test_publication_from_old_application_instance_is_recovered_once(self):
        EventNotificationCampaign.objects.filter(event=self.event).delete()
        process_event_notifications(now=self.now + timedelta(seconds=1))
        process_event_notifications(now=self.now + timedelta(minutes=1))
        self.assertEqual(self.email.call_count, 2)
        self.assertEqual(EventNotificationCampaign.objects.filter(event=self.event, kind="publication").count(), 1)

    def test_enclosing_application_transaction_cannot_send_before_commit(self):
        with self.assertRaises(RuntimeError), transaction.atomic():
            process_event_notifications(now=self.now)
        self.email.assert_not_called()
        self.sms.assert_not_called()


@override_settings(EVENT_NOTIFICATIONS_ENABLED=True)
@skipUnlessDBFeature("has_select_for_update_skip_locked")
class ConcurrentEventNotificationTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.now = timezone.now()
        Event.objects.filter(pk=self.event.pk).update(
            notify_sms=False, registration_closes_at=self.now + timedelta(days=10),
        )

    def test_two_processors_cannot_send_same_delivery_twice(self):
        barrier = Barrier(2)

        def run():
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                return process_event_notifications(now=self.now)
            finally:
                connections.close_all()

        with patch("volunteers.event_notifications.get_channel_readiness", return_value={"ready": True}), patch(
            "volunteers.event_notifications.send_notification_email", return_value="accepted",
        ) as send, patch("volunteers.event_notifications.send_notification_sms") as sms:
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: run(), range(2)))
        self.assertEqual(sum(result["accepted"] for result in results), 2)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(len({call.kwargs["idempotency_key"] for call in send.call_args_list}), 2)
        self.assertFalse(EventNotificationDelivery.objects.filter(attempt_count__gt=1).exists())
        sms.assert_not_called()
