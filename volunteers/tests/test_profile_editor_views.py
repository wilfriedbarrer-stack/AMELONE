from copy import deepcopy
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from volunteers.event_profiles import save_event_profile
from volunteers.forms_profiles import ProfileEventCreationForm
from volunteers.models import Beverage, Event, EventDay, EventProfile, Menu, Station, StationSlot

from .factories import DataMixin


class ProfileEditorViewsTests(DataMixin, TestCase):
    def setUp(self):
        self.profile = save_event_profile(event=self.event, name="Profil de concours")
        self.client.force_login(self.organizer)

    def profile_url(self):
        return reverse("event_profile_update", args=[self.profile.pk])

    def station_url(self, index=0):
        return reverse("event_profile_station_update", args=[self.profile.pk, index])

    def metadata(self, **changes):
        event = self.profile.snapshot["event"]
        return {
            "name": self.profile.name,
            "description": self.profile.description,
            "location": event["location"],
            "event_description": event["description"],
            "general_instructions": event["general_instructions"],
            "expected_updated_at": self.profile.updated_at.isoformat(),
            **changes,
        }

    def station_data(self, *, index=0, slots=None, **changes):
        station = self.profile.snapshot["stations"][index] if index is not None else {
            "name": "Jury", "description": "", "instructions": "",
            "category": Station.Category.OFFICIAL, "age_policy": Station.AgePolicy.ADULTS_ONLY,
            "is_active": True, "position": 1, "slots": [],
        }
        initial_slots = station["slots"]
        slots = initial_slots if slots is None else slots
        data = {
            field: station[field] for field in
            ("name", "description", "instructions", "category", "age_policy", "position")
        }
        if station["is_active"]:
            data["is_active"] = "on"
        data.update({
            "expected_updated_at": self.profile.updated_at.isoformat(),
            "slots-TOTAL_FORMS": str(len(slots) + 1),
            "slots-INITIAL_FORMS": str(len(initial_slots)),
            "slots-MIN_NUM_FORMS": "0", "slots-MAX_NUM_FORMS": "1000",
        })
        for index, slot in enumerate([*slots, {}]):
            for field in ("day_offset", "period", "capacity", "start_time", "end_time"):
                value = slot.get(field)
                data[f"slots-{index}-{field}"] = str(value) if value is not None else ""
            if slot.get("DELETE"):
                data[f"slots-{index}-DELETE"] = "on"
        return {**data, **changes}

    def slot(self, **changes):
        return {
            "day_offset": 0, "period": StationSlot.Period.FULL_DAY, "capacity": 2,
            "start_time": "08:00", "end_time": "18:00", **changes,
        }

    def test_family_cannot_read_or_mutate_any_profile_editor_route(self):
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.client.force_login(self.family_user)
        routes = [
            ("event_profile_new", []),
            ("event_profile_update", [self.profile.pk]),
            ("event_profile_station_create", [self.profile.pk]),
            ("event_profile_station_update", [self.profile.pk, 0]),
            ("event_profile_station_delete", [self.profile.pk, 0]),
        ]
        for name, args in routes:
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
                self.assertEqual(self.client.post(reverse(name, args=args), {}).status_code, 403)

    def test_new_profile_requires_no_real_event_and_has_editable_configuration(self):
        event_count, station_count = Event.objects.count(), Station.objects.count()
        response = self.client.post(reverse("event_profile_new"), {
            "name": "Nouveau modèle", "description": "Profil annuel", "location": "Carrière",
            "event_description": "Concours", "general_instructions": "Accueil à 8 h",
            "status": Event.Status.PUBLISHED, "event_id": str(self.event.pk),
        })
        created = EventProfile.objects.get(name="Nouveau modèle")
        self.assertRedirects(response, reverse("event_profile_update", args=[created.pk]))
        self.assertEqual(created.snapshot["event"]["location"], "Carrière")
        self.assertEqual(created.snapshot["days"], [{"offset": 0, "menus": [], "beverages": []}])
        self.assertEqual(created.snapshot["stations"], [])
        self.assertEqual(Event.objects.count(), event_count)
        self.assertEqual(Station.objects.count(), station_count)

    def test_empty_location_and_duplicate_name_are_form_errors(self):
        for changes, field in (({"location": ""}, "location"),
                               ({"name": self.profile.name}, "name")):
            with self.subTest(changes=changes):
                response = self.client.post(reverse("event_profile_new"), self.metadata(
                    name="Autre modèle", **changes,
                )) if field == "location" else self.client.post(reverse("event_profile_new"), self.metadata())
                self.assertEqual(response.status_code, 200)
                self.assertIn(field, response.context["form"].errors)
        self.assertEqual(EventProfile.objects.count(), 1)

    def test_metadata_update_preserves_days_deadlines_catalogues_and_real_events(self):
        second = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=2))
        Menu.objects.create(event_day=second, name="Collation", meal_type=Menu.MealType.SNACK)
        Beverage.objects.create(event_day=second, name="Jus", surcharge_cents=100)
        self.profile = save_event_profile(event=self.event, name="Modèle plusieurs jours")
        original = deepcopy(self.profile.snapshot)
        response = self.client.post(self.profile_url(), self.metadata(
            name="Modèle modifié", description="Note", event_description="Description nouvelle",
            location="Grand manège", general_instructions="Instructions nouvelles",
        ))
        self.assertRedirects(response, self.profile_url())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, "Modèle modifié")
        self.assertEqual(self.profile.snapshot["event"]["location"], "Grand manège")
        for field in ("days", "windows", "stations"):
            self.assertEqual(self.profile.snapshot[field], original[field])
        self.event.refresh_from_db()
        self.assertEqual(self.event.location, "Gymnase")

    def test_all_existing_profile_mutations_require_a_version_token(self):
        original = deepcopy(self.profile.snapshot)
        metadata = self.metadata(name="Ne pas sauvegarder")
        metadata.pop("expected_updated_at")
        station_data = self.station_data(name="Ne pas sauvegarder")
        station_data.pop("expected_updated_at")
        for url, data in (
            (self.profile_url(), metadata), (self.station_url(), station_data),
            (reverse("event_profile_station_create", args=[self.profile.pk]), station_data),
            (reverse("event_profile_station_delete", args=[self.profile.pk, 0]), {}),
        ):
            with self.subTest(url=url):
                response = self.client.post(url, data)
                self.assertContains(response, "La version de ce profil est manquante ou invalide.")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, "Profil de concours")
        self.assertEqual(self.profile.snapshot, original)

    def test_stale_token_cannot_overwrite_metadata_or_station(self):
        stale_metadata = self.metadata(name="Édition ancienne")
        stale_station = self.station_data(name="Ancien poste")
        response = self.client.post(self.profile_url(), self.metadata(name="Dernière édition"))
        self.assertEqual(response.status_code, 302)
        for url, data in ((self.profile_url(), stale_metadata), (self.station_url(), stale_station)):
            with self.subTest(url=url):
                self.assertContains(self.client.post(url, data), "Ce profil a été modifié")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.name, "Dernière édition")
        self.assertEqual(self.profile.snapshot["stations"][0]["name"], "Accueil")

    def test_station_creation_with_full_day_and_capacity_zero_creates_no_real_station(self):
        original_stations = Station.objects.count()
        response = self.client.post(
            reverse("event_profile_station_create", args=[self.profile.pk]),
            self.station_data(index=None, slots=[self.slot(capacity=0)]),
        )
        self.assertRedirects(response, self.profile_url())
        self.profile.refresh_from_db()
        station = self.profile.snapshot["stations"][-1]
        self.assertEqual(station["name"], "Jury")
        self.assertEqual(station["category"], Station.Category.OFFICIAL)
        self.assertEqual(station["age_policy"], Station.AgePolicy.ADULTS_ONLY)
        self.assertEqual(station["slots"][0]["period"], StationSlot.Period.FULL_DAY)
        self.assertEqual(station["slots"][0]["capacity"], 0)
        self.assertEqual(Station.objects.count(), original_stations)

    def test_editing_existing_station_ignores_browser_blank_extra_row(self):
        response = self.client.get(self.station_url())
        formset = response.context["formset"]
        self.assertEqual(formset.extra, 1)
        self.assertEqual(list(formset.forms[-1].fields["day_offset"].choices)[0][0], "")
        self.assertEqual(list(formset.forms[-1].fields["period"].choices)[0][0], "")
        data = self.station_data(name="Accueil modifié")
        response = self.client.post(self.station_url(), data)
        self.assertRedirects(response, self.profile_url())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.snapshot["stations"][0]["name"], "Accueil modifié")
        self.assertEqual(len(self.profile.snapshot["stations"][0]["slots"]), 2)
        self.station.refresh_from_db()
        self.assertEqual(self.station.name, "Accueil")

    def test_new_station_can_be_saved_without_any_slots(self):
        response = self.client.post(
            reverse("event_profile_station_create", args=[self.profile.pk]),
            self.station_data(index=None, slots=[]),
        )
        self.assertRedirects(response, self.profile_url())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.snapshot["stations"][-1]["slots"], [])

    def test_invalid_slot_values_do_not_partially_update_station(self):
        original = deepcopy(self.profile.snapshot)
        invalid = [
            {"day_offset": 999}, {"period": "nuit"}, {"capacity": -1}, {"capacity": 32768},
            {"start_time": "08:00", "end_time": ""},
            {"start_time": "18:00", "end_time": "08:00"},
            {"start_time": "incorrect", "end_time": "18:00"},
        ]
        for values in invalid:
            with self.subTest(values=values):
                response = self.client.post(self.station_url(), self.station_data(
                    name="Refusé", slots=[self.slot(**values)],
                ))
                self.assertEqual(response.status_code, 200)
                self.assertFalse(response.context["formset"].is_valid())
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.snapshot, original)

    def test_duplicate_day_period_is_rejected_but_deleted_duplicate_is_ignored(self):
        duplicate = self.slot()
        response = self.client.post(self.station_url(), self.station_data(slots=[duplicate, duplicate]))
        self.assertContains(response, "Une même journée et une même période")
        response = self.client.post(self.station_url(), self.station_data(slots=[
            duplicate, {**duplicate, "DELETE": True},
        ]))
        self.assertRedirects(response, self.profile_url())
        self.profile.refresh_from_db()
        self.assertEqual(len(self.profile.snapshot["stations"][0]["slots"]), 1)

    def test_duplicate_station_name_is_a_visible_form_error(self):
        response = self.client.post(self.station_url(), self.station_data(name="Buvette"))
        self.assertContains(response, "Un poste porte déjà ce nom dans le profil.")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.snapshot["stations"][0]["name"], "Accueil")

    def test_delete_get_only_confirms_and_post_removes_only_model_station(self):
        url = reverse("event_profile_station_delete", args=[self.profile.pk, 0])
        original_count = len(self.profile.snapshot["stations"])
        response = self.client.get(url)
        self.assertContains(response, "Confirmez la suppression")
        self.profile.refresh_from_db()
        self.assertEqual(len(self.profile.snapshot["stations"]), original_count)
        self.assertRedirects(self.client.post(url, {
            "expected_updated_at": self.profile.updated_at.isoformat(),
        }), self.profile_url())
        self.profile.refresh_from_db()
        self.assertEqual(len(self.profile.snapshot["stations"]), original_count - 1)
        self.assertTrue(Station.objects.filter(pk=self.station.pk).exists())
        self.assertTrue(StationSlot.objects.filter(pk=self.morning_slot.pk).exists())

    def test_stale_delete_cannot_remove_shifted_station_index(self):
        url = reverse("event_profile_station_delete", args=[self.profile.pk, 0])
        token = self.profile.updated_at.isoformat()
        self.assertEqual(self.client.post(url, {"expected_updated_at": token}).status_code, 302)
        self.assertContains(self.client.post(url, {"expected_updated_at": token}), "Ce profil a été modifié")
        self.profile.refresh_from_db()
        self.assertEqual([station["name"] for station in self.profile.snapshot["stations"]], ["Buvette"])

    def test_out_of_range_indices_are_not_found_for_get_and_post(self):
        for name in ("event_profile_station_update", "event_profile_station_delete"):
            url = reverse(name, args=[self.profile.pk, 99])
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, {}).status_code, 404)
        self.assertEqual(self.client.get(self.station_url().replace("/0/", "/-1/")).status_code, 404)

    def test_missing_management_form_and_malformed_token_return_visible_errors(self):
        response = self.client.post(self.station_url(), {
            "name": "Incomplet", "category": "volunteer", "age_policy": "none", "position": 0,
            "expected_updated_at": self.profile.updated_at.isoformat(),
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["formset"].non_form_errors())
        self.assertContains(self.client.post(self.profile_url(), self.metadata(
            expected_updated_at="pas-une-date",
        )), "Ce profil a été modifié")

    def test_corrupt_profile_configuration_does_not_produce_server_error(self):
        self.profile.snapshot = []
        self.profile.save(update_fields=["snapshot", "updated_at"])
        self.assertContains(self.client.get(self.profile_url()), "Ce profil ne contient pas une configuration utilisable.")
        self.assertEqual(self.client.get(reverse("event_profile_station_create", args=[self.profile.pk])).status_code, 200)
        self.assertEqual(self.client.get(self.station_url()).status_code, 404)

    def test_creation_selector_has_profile_first_and_supports_root_prefix(self):
        form = ProfileEventCreationForm(prefix="profile")
        self.assertEqual(list(form.fields), ["profile", "name", "event_date"])
        self.assertEqual(form["profile"].html_name, "profile-profile")
        self.assertEqual(form["event_date"].html_name, "profile-event_date")
