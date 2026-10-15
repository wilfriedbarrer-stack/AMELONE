from datetime import date
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client, RequestFactory, TestCase
from django.urls import reverse

from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import (
    Beverage,
    Event,
    EventDay,
    EventProfile,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
    User,
)
from volunteers.views_deletion import event_delete, event_profile_delete

from .factories import DataMixin


class DeletionViewsTests(DataMixin, TestCase):
    def setUp(self):
        self.profile = save_event_profile(event=self.event, name="Profil à supprimer")
        self.client.force_login(self.organizer)

    def event_url(self):
        return reverse("event_delete", args=[self.event.pk])

    def profile_url(self):
        return reverse("event_profile_delete", args=[self.profile.pk])

    def event_data(self, **changes):
        return {"confirm": "on", "expected_updated_at": self.event.updated_at.isoformat(), **changes}

    def profile_data(self, **changes):
        return {"confirm": "on", "expected_updated_at": self.profile.updated_at.isoformat(), **changes}

    def test_anonymous_and_family_cannot_delete_events_or_profiles(self):
        self.client.logout()
        for url in (self.event_url(), self.profile_url()):
            self.assertEqual(self.client.get(url).status_code, 302)
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.client.force_login(self.family_user)
        for url, data in ((self.event_url(), self.event_data()), (self.profile_url(), self.profile_data())):
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_get_only_displays_confirmation_and_configuration_summary(self):
        menu = Menu.objects.create(event_day=self.day, name="Menu")
        drink = Beverage.objects.create(event_day=self.day, name="Jus")
        response = self.client.get(self.event_url())
        self.assertEqual(response.status_code, 200)
        summary = response.context["summary"]
        self.assertEqual(summary["mode"], "delete")
        self.assertFalse(summary["has_history"])
        self.assertEqual(summary["counts"]["days"], 1)
        self.assertEqual(summary["counts"]["stations"], 2)
        self.assertEqual(summary["counts"]["slots"], 2)
        self.assertEqual(summary["counts"]["menus"], 1)
        self.assertEqual(summary["counts"]["drinks"], 1)
        self.assertContains(response, "suppression définitive")
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        response = self.client.get(self.profile_url())
        self.assertEqual(response.context["station_count"], 2)
        self.assertEqual(response.context["slot_count"], 2)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertTrue(Menu.objects.filter(pk=menu.pk).exists())
        self.assertTrue(Beverage.objects.filter(pk=drink.pk).exists())

    def test_confirmation_and_version_are_required_for_both_mutations(self):
        for url, valid_data in ((self.event_url(), self.event_data()), (self.profile_url(), self.profile_data())):
            with self.subTest(url=url):
                data = {**valid_data, "confirm": ""}
                response = self.client.post(url, data)
                self.assertContains(response, "Confirmez cette action avant de continuer.")
                for token in ("", "x" * 101):
                    response = self.client.post(url, {**valid_data, "expected_updated_at": token})
                    self.assertContains(response, "La version de cet élément est manquante ou invalide.")
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_empty_event_is_deleted_with_configuration_but_reusable_profile_remains(self):
        menu = Menu.objects.create(event_day=self.day, name="Menu")
        drink = Beverage.objects.create(event_day=self.day, name="Jus")
        response = self.client.post(self.event_url(), self.event_data())
        self.assertRedirects(response, reverse("event_list"))
        self.assertFalse(Event.objects.filter(pk=self.event.pk).exists())
        self.assertFalse(EventDay.objects.filter(pk=self.day.pk).exists())
        self.assertFalse(Station.objects.filter(pk=self.station.pk).exists())
        self.assertFalse(StationSlot.objects.filter(pk=self.morning_slot.pk).exists())
        self.assertFalse(Menu.objects.filter(pk=menu.pk).exists())
        self.assertFalse(Beverage.objects.filter(pk=drink.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_event_with_cancelled_registration_is_archived_even_with_forged_delete_mode(self):
        registration = self.registration(status=Registration.Status.CANCELLED)
        menu = Menu.objects.create(event_day=self.day, name="Menu")
        response = self.client.get(self.event_url())
        self.assertEqual(response.context["summary"]["mode"], "archive")
        self.assertContains(response, "en conservant son historique")
        response = self.client.post(self.event_url(), self.event_data(mode="delete", has_history="0"))
        self.assertRedirects(response, reverse("event_list"))
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertTrue(Registration.objects.filter(pk=registration.pk).exists())
        self.assertTrue(Station.objects.filter(pk=self.station.pk).exists())
        self.assertTrue(Menu.objects.filter(pk=menu.pk).exists())

    def test_payment_history_is_kept_and_success_message_describes_archive(self):
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=500, method=Payment.Method.SUMUP,
        )
        before = Payment.objects.filter(pk=payment.pk).values().get()
        response = self.client.post(self.event_url(), self.event_data(), follow=True)
        self.assertContains(response, "Son historique est conservé.")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), before)

    def test_new_registration_since_confirmation_uses_actual_archive_mode(self):
        response = self.client.get(self.event_url())
        self.assertEqual(response.context["summary"]["mode"], "delete")
        data = self.event_data()
        registration = self.registration()
        response = self.client.post(self.event_url(), data, follow=True)
        self.assertContains(response, "Son historique est conservé.")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.ARCHIVED)
        self.assertTrue(Registration.objects.filter(pk=registration.pk).exists())

    def test_profile_deletion_does_not_change_events_already_created_from_it(self):
        clone = create_event_from_profile(
            profile=self.profile, name="Copie indépendante", event_date=date(2028, 5, 1),
        )
        original_station_ids = list(Station.objects.values_list("pk", flat=True))
        response = self.client.post(self.profile_url(), self.profile_data())
        self.assertRedirects(response, reverse("event_profile_list"))
        self.assertFalse(EventProfile.objects.filter(pk=self.profile.pk).exists())
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(Event.objects.filter(pk=clone.pk).exists())
        self.assertCountEqual(Station.objects.values_list("pk", flat=True), original_station_ids)

    def test_changed_event_or_profile_cannot_be_deleted_with_stale_confirmation(self):
        old_event_data, old_profile_data = self.event_data(), self.profile_data()
        self.event.name = "Événement modifié"
        self.event.save(update_fields=["name", "updated_at"])
        self.profile.name = "Profil modifié"
        self.profile.save(update_fields=["name", "updated_at"])
        for url, data in ((self.event_url(), old_event_data), (self.profile_url(), old_profile_data)):
            self.assertContains(self.client.post(url, data), "Cet élément a été modifié")
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_invalid_or_naive_version_tokens_are_visible_errors(self):
        for token in ("pas-une-date", "2026-10-15T09:00:00"):
            for url, data in ((self.event_url(), self.event_data()), (self.profile_url(), self.profile_data())):
                with self.subTest(url=url, token=token):
                    self.assertContains(self.client.post(url, {
                        **data, "expected_updated_at": token,
                    }), "Cet élément a été modifié")

    def test_missing_objects_and_concurrent_removal_are_handled_without_server_error(self):
        for name in ("event_delete", "event_profile_delete"):
            url = reverse(name, args=[uuid4()])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {"confirm": "on"}).status_code, 404)
        for tool, url, data in (
            ("delete_event_or_archive", self.event_url(), self.event_data()),
            ("delete_reusable_event_profile", self.profile_url(), self.profile_data()),
        ):
            with self.subTest(tool=tool):
                with patch(f"volunteers.views_deletion.{tool}", side_effect=ValidationError("Cet élément a déjà été supprimé.")):
                    self.assertContains(self.client.post(url, data), "Cet élément a déjà été supprimé.")

    def test_unsupported_methods_and_missing_csrf_cannot_mutate(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.organizer)
        for url, data in ((self.event_url(), self.event_data()), (self.profile_url(), self.profile_data())):
            for method in ("put", "patch", "delete"):
                self.assertEqual(getattr(self.client, method)(url, data).status_code, 405)
            self.assertEqual(csrf_client.post(url, data).status_code, 403)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_actor_permissions_are_rechecked_under_lock_before_either_mutation(self):
        for view, url, args, data in (
            (event_delete, self.event_url(), [self.event.pk], self.event_data()),
            (event_profile_delete, self.profile_url(), [self.profile.pk], self.profile_data()),
        ):
            for changed in ({"is_staff": False, "is_organizer": False}, {"is_active": False}):
                with self.subTest(view=view.__name__, changed=changed):
                    User.objects.filter(pk=self.organizer.pk).update(
                        is_active=True, is_staff=True, is_organizer=True,
                    )
                    self.organizer.refresh_from_db()
                    request = RequestFactory().post(url, data)
                    request.user = self.organizer
                    User.objects.filter(pk=self.organizer.pk).update(**changed)
                    with self.assertRaises(PermissionDenied):
                        view(request, *args)
        self.assertTrue(Event.objects.filter(pk=self.event.pk).exists())
        self.assertTrue(EventProfile.objects.filter(pk=self.profile.pk).exists())

    def test_administrator_can_delete_profile_and_corrupt_snapshot_confirmation_is_safe(self):
        admin = User.objects.create_superuser("deletion-admin@example.test", "Strong-pass-123")
        self.client.force_login(admin)
        self.profile.snapshot = ["invalid"]
        self.profile.save(update_fields=["snapshot", "updated_at"])
        response = self.client.get(self.profile_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["station_count"], 0)
        self.assertEqual(response.context["slot_count"], 0)
        self.assertRedirects(self.client.post(self.profile_url(), self.profile_data()), reverse("event_profile_list"))
        self.assertFalse(EventProfile.objects.filter(pk=self.profile.pk).exists())
