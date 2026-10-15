from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase
from django.utils import timezone

from volunteers.deletion import delete_event_or_archive, get_event_deletion_summary
from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import Event, EventNotificationCampaign, EventNotificationDelivery

from .factories import DataMixin


class NotificationIntegrationTests(DataMixin, TestCase):
    def event_values(self, **changes):
        return {
            "name": "Prochain concours", "location": "Carrière",
            "registration_opens_at": self.event.registration_opens_at,
            "registration_closes_at": self.event.registration_closes_at,
            "meal_orders_close_at": self.event.meal_orders_close_at,
            **changes,
        }

    def test_partial_publication_uses_stored_notification_flags(self):
        event = Event.objects.create(**self.event_values(status="draft"))
        event.notify_publication = False
        event.status = Event.Status.PUBLISHED
        event.save(update_fields=("status",))
        campaign = event.notification_campaigns.get(kind="publication")
        self.assertIsNone(campaign.completed_at)
        self.assertEqual(campaign.skipped_reason, "")
        event.refresh_from_db()
        self.assertTrue(event.notify_publication)

    def test_partial_publication_preserves_stored_opt_out(self):
        event = Event.objects.create(**self.event_values(status="draft", notify_publication=False))
        event.notify_publication = True
        event.status = Event.Status.PUBLISHED
        event.save(update_fields=("status",))
        campaign = event.notification_campaigns.get(kind="publication")
        self.assertIsNotNone(campaign.completed_at)
        self.assertIn("désactivée", campaign.skipped_reason)

    def test_published_event_without_message_history_can_still_be_deleted(self):
        self.assertTrue(self.event.notification_campaigns.exists())
        summary = get_event_deletion_summary(self.event)
        self.assertFalse(summary["has_history"])
        result = delete_event_or_archive(
            event=self.event, actor=self.organizer,
            expected_updated_at=self.event.updated_at.isoformat(),
        )
        self.assertEqual(result["mode"], "delete")
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertFalse(EventNotificationCampaign.objects.filter(event_id=self.event.pk).exists())

    def test_notification_history_forces_archiving_and_preserves_the_message(self):
        campaign = self.event.notification_campaigns.get(kind="publication")
        delivery = EventNotificationDelivery.objects.create(
            campaign=campaign, family=self.family, channel="sms", recipient="+33601020304",
            body_text="Message déjà accepté", status="accepted", accepted_at=timezone.now(),
        )
        before = EventNotificationDelivery.objects.values().get(pk=delivery.pk)
        self.assertEqual(get_event_deletion_summary(self.event)["counts"]["emails"], 1)
        result = delete_event_or_archive(
            event=self.event, actor=self.organizer,
            expected_updated_at=self.event.updated_at.isoformat(),
        )
        self.assertEqual(result["mode"], "archive")
        self.assertEqual(EventNotificationDelivery.objects.values().get(pk=delivery.pk), before)
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)

    def test_reusable_event_model_keeps_notification_settings_without_copying_campaigns(self):
        self.event.notify_sms = False
        self.event.notify_vacancies = False
        self.event.save(update_fields=("notify_sms", "notify_vacancies"))
        profile = save_event_profile(event=self.event, name="Concours sans SMS")
        clone = create_event_from_profile(
            profile=profile, name="Autre concours", event_date=self.day.date + timedelta(days=10),
        )
        self.assertFalse(clone.notify_sms)
        self.assertFalse(clone.notify_vacancies)
        self.assertTrue(clone.notify_email)
        self.assertFalse(clone.notification_campaigns.exists())

    def test_migration_baselines_existing_publications_without_changing_event_data(self):
        draft = Event.objects.create(**self.event_values(status="draft", name="Pas encore publié"))
        archived = Event.objects.create(**self.event_values(status="archived", name="Historique"))
        EventNotificationCampaign.objects.all().delete()
        before = list(Event.objects.order_by("pk").values())
        migration = import_module("volunteers.migrations.0012_event_notifications")
        apps = MigrationExecutor(connection).loader.project_state([
            ("volunteers", "0012_event_notifications"),
        ]).apps
        migration.baseline_existing_publications(apps, SimpleNamespace(connection=connection))
        self.assertEqual(list(Event.objects.order_by("pk").values()), before)
        self.assertFalse(draft.notification_campaigns.exists())
        self.assertSetEqual(set(EventNotificationCampaign.objects.values_list("event_id", flat=True)), {
            self.event.pk, archived.pk,
        })
        self.assertFalse(EventNotificationCampaign.objects.filter(completed_at__isnull=True).exists())
        self.assertFalse(EventNotificationDelivery.objects.exists())
        draft.status = Event.Status.PUBLISHED
        draft.save(update_fields=("status",))
        self.assertIsNone(draft.notification_campaigns.get(kind="publication").completed_at)
