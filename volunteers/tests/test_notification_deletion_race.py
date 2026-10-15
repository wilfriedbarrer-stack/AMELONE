from concurrent.futures import ThreadPoolExecutor
from threading import Event as ThreadEvent
from time import monotonic, sleep
from unittest import skipUnless
from unittest.mock import patch

from django.db import close_old_connections, connection, connections
from django.test import TransactionTestCase
from django.utils import timezone

from volunteers import event_notifications
from volunteers.deletion import delete_event_or_archive
from volunteers.models import Event, EventNotificationCampaign, EventNotificationDelivery, User

from .factories import DataMixin


@skipUnless(connection.vendor == "postgresql", "Nécessite les verrous PostgreSQL.")
class ConcurrentNotificationDeletionTests(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()

    def test_preparation_finishing_during_deletion_preserves_messages_and_archives_event(self):
        campaign = EventNotificationCampaign.objects.get(event=self.event, kind="publication")
        preparation_locked = ThreadEvent()
        release_preparation = ThreadEvent()
        deletion_started = ThreadEvent()
        backend_ids = {}
        now = timezone.now()
        original_guard = event_notifications._campaign_guard

        def pause_under_campaign_lock(event, kind, at):
            preparation_locked.set()
            if not release_preparation.wait(timeout=5):
                raise AssertionError("La préparation n'a pas été libérée.")
            return original_guard(event, kind, at)

        def initialize_connection(name):
            close_old_connections()
            with connections["default"].cursor() as cursor:
                cursor.execute("SET lock_timeout = '5s'")
                cursor.execute("SELECT pg_backend_pid()")
                backend_ids[name] = cursor.fetchone()[0]

        def prepare():
            initialize_connection("preparation")
            try:
                return event_notifications._prepare_campaign(campaign.pk, now)
            finally:
                connections.close_all()

        def delete():
            initialize_connection("deletion")
            try:
                event = Event.objects.get(pk=self.event.pk)
                actor = User.objects.get(pk=self.organizer.pk)
                deletion_started.set()
                return delete_event_or_archive(
                    event=event, actor=actor,
                    expected_updated_at=event.updated_at.isoformat(),
                )
            finally:
                connections.close_all()

        with (
            patch("volunteers.event_notifications._campaign_guard", pause_under_campaign_lock),
            patch("volunteers.event_notifications.send_notification_email") as email,
            patch("volunteers.event_notifications.send_notification_sms") as sms,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            preparation = pool.submit(prepare)
            try:
                self.assertTrue(preparation_locked.wait(timeout=5))
                deletion = pool.submit(delete)
                self.assertTrue(deletion_started.wait(timeout=5))
                # La suppression doit être réellement en attente du verrou de
                # préparation avant son commit, sans dépendre d'une temporisation.
                deadline = monotonic() + 3
                blocked = False
                while monotonic() < deadline:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT %s = ANY(pg_blocking_pids(%s))",
                            [backend_ids["preparation"], backend_ids["deletion"]],
                        )
                        blocked = cursor.fetchone()[0]
                    if blocked:
                        break
                    sleep(0.01)
                self.assertTrue(blocked, "La suppression n'a pas attendu la campagne en cours.")
            finally:
                release_preparation.set()
            self.assertTrue(preparation.result(timeout=10))
            result = deletion.result(timeout=10)

        self.assertEqual(result["mode"], "archive")
        self.event.refresh_from_db()
        campaign.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertIsNotNone(campaign.prepared_at)
        self.assertEqual(EventNotificationDelivery.objects.filter(campaign=campaign).count(), 4)
        self.assertFalse(EventNotificationDelivery.objects.filter(campaign=campaign).exclude(
            status__in=("pending", "skipped"), attempt_count=0,
        ).exists())
        email.assert_not_called()
        sms.assert_not_called()
