from datetime import datetime, timedelta
from unittest.mock import patch

from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from volunteers.event_notifications import PARIS, VACANCY_REMINDER
from volunteers.models import (
    Assignment,
    Event,
    EventDay,
    EventNotificationCampaign,
    EventNotificationDelivery,
    EventWhatsAppShare,
    Registration,
    Station,
    StationSlot,
    User,
)
from volunteers.whatsapp_attention import (
    get_event_whatsapp_status,
    get_whatsapp_attention,
    mark_whatsapp_shared,
    reset_whatsapp_shared,
)

from .factories import DataMixin


@override_settings(EVENT_NOTIFICATIONS_ENABLED=False, EMAIL_ENABLED=False, SMS_ENABLED=False)
class WhatsAppAttentionTests(DataMixin, TestCase):
    def setUp(self):
        self.now = timezone.make_aware(datetime(2027, 2, 27, 10), PARIS)
        self.set_event(
            registration_opens_at=self.now - timedelta(days=2),
            registration_closes_at=self.now + timedelta(hours=47),
        )

    def set_event(self, **changes):
        Event.objects.filter(pk=self.event.pk).update(**changes)
        self.event.refresh_from_db()

    def attention(self, **kwargs):
        return get_whatsapp_attention(now=kwargs.pop("now", self.now), **kwargs)

    def mark(self, **kwargs):
        return mark_whatsapp_shared(
            self.event, VACANCY_REMINDER, self.organizer,
            now=kwargs.pop("now", self.now), **kwargs,
        )

    def test_due_reminder_links_directly_to_preparation_independently_of_email_flags(self):
        self.set_event(notify_publication=False, notify_vacancies=False, notify_email=False, notify_sms=False)
        summary = self.attention()
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["events"][0]["event_id"], self.event.pk)
        self.assertEqual(summary["events"][0]["task_count"], 1)
        task = summary["tasks"][0]
        self.assertEqual(task["kind"], VACANCY_REMINDER)
        self.assertEqual(task["due_at"], self.event.registration_closes_at - timedelta(hours=48))
        self.assertEqual(task["url"], reverse("event_notifications", args=[self.event.pk]) + "#whatsapp-vacancy_reminder-title")
        self.assertNotIn(self.family_user.email, repr(summary))
        self.assertNotIn(self.family_user.phone, repr(summary))

    def test_exact_48_hours_is_due_but_closing_instant_is_expired(self):
        closes = self.now + timedelta(hours=48)
        self.set_event(registration_closes_at=closes)
        self.assertEqual(self.attention(now=self.now - timedelta(microseconds=1))["count"], 0)
        self.assertEqual(self.attention()["count"], 1)
        self.assertEqual(self.attention(now=closes)["count"], 0)

    def test_draft_archived_not_yet_open_or_without_future_days_are_excluded(self):
        for status in (Event.Status.DRAFT, Event.Status.ARCHIVED):
            with self.subTest(status=status):
                self.set_event(status=status)
                self.assertEqual(self.attention()["count"], 0)
        self.set_event(status=Event.Status.PUBLISHED, registration_opens_at=self.now + timedelta(hours=1))
        self.assertEqual(self.attention()["count"], 0)
        self.set_event(registration_opens_at=self.now - timedelta(days=2))
        self.day.date = timezone.localdate(self.now, PARIS) - timedelta(days=1)
        self.day.save(update_fields=("date",))
        self.assertEqual(self.attention()["count"], 0)

    def test_day_boundary_uses_paris_date_even_when_utc_is_still_previous_day(self):
        now = timezone.make_aware(datetime(2027, 2, 27, 23, 30), timezone.get_fixed_timezone(0))
        self.day.date = timezone.localdate(now, PARIS) - timedelta(days=1)
        self.day.save(update_fields=("date",))
        self.assertEqual(self.attention(now=now)["count"], 0)
        self.day.date = timezone.localdate(now, PARIS)
        self.day.save(update_fields=("date",))
        self.assertEqual(self.attention(now=now)["count"], 1)

    def test_inactive_stations_and_zero_capacity_never_create_an_alert(self):
        self.station.is_active = False
        self.station.save(update_fields=("is_active",))
        self.assertEqual(self.attention()["count"], 0)
        self.station.is_active = True
        self.station.save(update_fields=("is_active",))
        StationSlot.objects.filter(station=self.station).update(capacity=0)
        self.assertEqual(self.attention()["count"], 0)

    def test_shared_full_day_and_half_days_count_real_draft_and_confirmed_occupancy(self):
        StationSlot.objects.filter(station=self.station).update(capacity=2)
        full_day = StationSlot.objects.create(
            station=self.station, event_day=self.day, period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        first = Assignment.objects.create(
            registration=self.registration(), station_slot=full_day, status=Assignment.Status.DRAFT,
        )
        second_registration = self.registration(member=self.other_member)
        Assignment.objects.create(
            registration=second_registration, station_slot=full_day, status=Assignment.Status.CONFIRMED,
        )
        self.assertEqual(self.attention()["count"], 0)
        first.status = Assignment.Status.CANCELLED
        first.save(update_fields=("status",))
        self.assertEqual(self.attention()["count"], 1)
        first.status = Assignment.Status.DRAFT
        first.save(update_fields=("status",))
        Registration.objects.filter(pk=second_registration.pk).update(status=Registration.Status.CANCELLED)
        self.assertEqual(self.attention()["count"], 1)

    def test_global_query_count_stays_constant_across_events(self):
        for index in range(7):
            event = Event.objects.create(
                name=f"Autre événement {index}", location="Gymnase",
                registration_opens_at=self.now - timedelta(days=1),
                registration_closes_at=self.now + timedelta(hours=48),
                meal_orders_close_at=self.now + timedelta(hours=24),
                status=Event.Status.PUBLISHED,
            )
            day = EventDay.objects.create(event=event, date=self.day.date)
            station = Station.objects.create(event=event, name="Accueil")
            StationSlot.objects.create(station=station, event_day=day, period="morning", capacity=1)
        with self.assertNumQueries(3):
            result = self.attention()
        self.assertEqual(result["count"], 8)

    def test_summaries_and_status_are_read_only_and_do_not_contact_transports(self):
        before = (
            list(Event.objects.values()), list(EventNotificationCampaign.objects.values()),
            list(EventNotificationDelivery.objects.values()), list(EventWhatsAppShare.objects.values()),
        )
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            self.attention()
            state = get_event_whatsapp_status(self.event, now=self.now)[VACANCY_REMINDER]
        self.assertTrue(state["is_due"])
        self.assertFalse(state["shared"])
        email.assert_not_called()
        sms.assert_not_called()
        self.assertEqual(before, (
            list(Event.objects.values()), list(EventNotificationCampaign.objects.values()),
            list(EventNotificationDelivery.objects.values()), list(EventWhatsAppShare.objects.values()),
        ))

    def test_mark_is_explicit_global_and_idempotent_and_reset_restores_alert(self):
        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms:
            share, changed = self.mark()
            again, changed_again = self.mark(now=self.now + timedelta(minutes=1))
        email.assert_not_called()
        sms.assert_not_called()
        self.assertTrue(changed)
        self.assertFalse(changed_again)
        self.assertEqual(share.pk, again.pk)
        self.assertEqual(again.shared_at, self.now)
        self.assertEqual(again.shared_by, self.organizer)
        self.assertEqual(again.registration_closes_at, self.event.registration_closes_at)
        self.assertEqual(self.attention()["count"], 0)
        state = get_event_whatsapp_status(self.event, now=self.now)[VACANCY_REMINDER]
        self.assertTrue(state["shared"])
        self.assertFalse(state["is_due"])
        self.assertEqual(state["shared_at"], self.now)
        self.assertTrue(reset_whatsapp_shared(self.event, VACANCY_REMINDER, self.organizer))
        self.assertFalse(reset_whatsapp_shared(self.event, VACANCY_REMINDER, self.organizer))
        self.assertEqual(self.attention()["count"], 1)

    def test_message_declared_before_48_hours_stays_shared_when_reminder_becomes_due(self):
        self.set_event(registration_closes_at=self.now + timedelta(days=4))
        self.mark()
        self.assertEqual(self.attention(now=self.now + timedelta(hours=60))["count"], 0)

    def test_deadline_change_reopens_reminder_and_stale_page_cannot_mark_or_reset_it(self):
        share, _ = self.mark()
        old_closing = self.event.registration_closes_at
        self.set_event(registration_closes_at=old_closing + timedelta(hours=1))
        self.assertEqual(self.attention()["count"], 1)
        state = get_event_whatsapp_status(self.event, now=self.now)[VACANCY_REMINDER]
        self.assertFalse(state["shared"])
        self.assertIsNone(state["shared_at"])
        with self.assertRaises(ValidationError):
            self.mark(expected_registration_closes_at=old_closing)
        with self.assertRaises(ValidationError):
            reset_whatsapp_shared(
                self.event, VACANCY_REMINDER, self.organizer,
                expected_registration_closes_at=old_closing,
            )
        share.refresh_from_db()
        self.assertEqual(share.registration_closes_at, old_closing)
        new_share, changed = self.mark(expected_registration_closes_at=self.event.registration_closes_at)
        self.assertTrue(changed)
        self.assertEqual(new_share.pk, share.pk)
        self.assertEqual(new_share.registration_closes_at, self.event.registration_closes_at)
        self.assertEqual(self.attention()["count"], 0)

    def test_server_checks_current_event_even_when_caller_has_stale_published_instance(self):
        Event.objects.filter(pk=self.event.pk).update(status=Event.Status.ARCHIVED)
        with self.assertRaises(ValidationError):
            self.mark()
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_current_closure_opening_and_remaining_places_are_required_to_mark(self):
        for changes in (
            {"registration_closes_at": self.now},
            {"registration_closes_at": self.now + timedelta(hours=47), "registration_opens_at": self.now + timedelta(hours=1)},
        ):
            self.set_event(**changes)
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.mark()
        self.set_event(registration_opens_at=self.now - timedelta(days=2))
        StationSlot.objects.filter(station=self.station).update(capacity=0)
        with self.assertRaises(ValidationError):
            self.mark()
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_only_active_organizers_and_staff_can_mark_and_reset(self):
        for actor in (AnonymousUser(), self.family_user):
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                mark_whatsapp_shared(self.event, VACANCY_REMINDER, actor, now=self.now)
            with self.subTest(actor=actor), self.assertRaises(PermissionDenied):
                reset_whatsapp_shared(self.event, VACANCY_REMINDER, actor)
        self.organizer.is_active = False
        with self.assertRaises(PermissionDenied):
            self.mark()
        with self.assertRaises(PermissionDenied):
            reset_whatsapp_shared(self.event, VACANCY_REMINDER, self.organizer)
        self.organizer.is_active = True
        self.organizer.is_staff = False
        self.mark()
        administrator = User.objects.create_user(
            email="admin-whatsapp@example.test", is_staff=True, is_superuser=True,
        )
        self.assertTrue(reset_whatsapp_shared(self.event, VACANCY_REMINDER, administrator))
        share, changed = mark_whatsapp_shared(self.event, VACANCY_REMINDER, administrator, now=self.now)
        self.assertTrue(changed)
        self.assertEqual(share.shared_by, administrator)

    def test_unrecognized_or_publication_kind_cannot_change_reminder(self):
        for kind in ("publication", "unknown"):
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                mark_whatsapp_shared(self.event, kind, self.organizer, now=self.now)
            with self.subTest(kind=kind), self.assertRaises(ValidationError):
                reset_whatsapp_shared(self.event, kind, self.organizer)
        self.assertFalse(EventWhatsAppShare.objects.exists())

    def test_status_reuses_supplied_vacancies(self):
        with self.assertNumQueries(2):
            state = get_event_whatsapp_status(self.event, now=self.now, vacancies=[])[VACANCY_REMINDER]
        self.assertFalse(state["is_due"])

    def test_deleting_declarant_keeps_declaration(self):
        share, _ = self.mark()
        self.organizer.delete()
        share.refresh_from_db()
        self.assertIsNone(share.shared_by)
        self.assertEqual(self.attention()["count"], 0)
