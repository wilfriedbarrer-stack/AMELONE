from copy import deepcopy
from datetime import date

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from volunteers.catalogue_import import (
    apply_catalogue_preview,
    parse_catalogue_upload,
    prepare_catalogue_preview,
)
from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.forms_profiles import ReusableProfileForm
from volunteers.models import Beverage, EventProfile
from volunteers.profile_editor import create_reusable_profile, update_reusable_profile
from volunteers.shared_catalogue import save_shared_option, shared_option_version

from .factories import DataMixin


def upload(content):
    return SimpleUploadedFile("boissons.csv", content.encode("utf-8-sig"), content_type="text/csv")


class SnackDrinkCatalogueTests(DataMixin, TestCase):
    def preview(self, content):
        return prepare_catalogue_preview(parse_catalogue_upload(upload(content), "boissons"), None, "boissons")

    def apply(self, preview, **kwargs):
        return apply_catalogue_preview(day=None, kind="boissons", rows=preview.rows,
                                       actor=self.organizer, **kwargs)

    def test_import_accepts_french_temperatures_and_applies_them_to_shared_options(self):
        preview = self.preview("nom;temperature;supplement_euros\nCafé;chaude;1\nEau;fraîche;0\nJus;à classer;2\n")
        self.assertTrue(preview.valid, preview.errors)
        self.assertEqual(self.apply(preview), 3)
        self.assertEqual(dict(Beverage.objects.values_list("name", "temperature")),
                         {"Café": "hot", "Eau": "cold", "Jus": "unspecified"})
        self.assertFalse(Beverage.objects.exclude(event_day=None).exists())

    def test_import_without_temperature_preserves_existing_classification(self):
        drink = Beverage.objects.create(name="Café", temperature="hot", surcharge_cents=100)
        preview = self.preview("nom;supplement_euros\nCafé;2\n")
        self.assertTrue(preview.valid)
        self.apply(preview, confirm_updates=True)
        drink.refresh_from_db()
        self.assertEqual(drink.temperature, "hot")
        self.assertEqual(drink.surcharge_cents, 200)

    def test_blank_temperature_preserves_existing_classification(self):
        drink = Beverage.objects.create(name="Eau", temperature="cold")
        preview = self.preview("nom;temperature\nEau;\n")
        self.assertTrue(preview.valid)
        self.apply(preview, confirm_updates=True)
        drink.refresh_from_db()
        self.assertEqual(drink.temperature, "cold")

    def test_unknown_temperature_prevents_the_entire_import(self):
        preview = self.preview("nom;temperature\nEau;fraîche\nCafé;tiède\n")
        self.assertFalse(preview.valid)
        with self.assertRaises(ValidationError):
            self.apply(preview)
        self.assertFalse(Beverage.objects.exists())

    def test_classification_change_invalidates_an_open_import_preview(self):
        drink = Beverage.objects.create(name="Café", temperature="unspecified")
        preview = self.preview("nom;temperature\nCafé;chaude\n")
        # Simulate a concurrent administrator with an unchanged timestamp.
        Beverage.objects.filter(pk=drink.pk).update(temperature="cold")
        with self.assertRaises(ValidationError):
            self.apply(preview, confirm_updates=True)
        drink.refresh_from_db()
        self.assertEqual(drink.temperature, "cold")

    def test_shared_editor_saves_category_and_legacy_posts_keep_it(self):
        drink = save_shared_option(kind="boissons", actor=self.organizer, data={
            "name": "Thé", "temperature": "hot", "description": "Tasse", "is_active": True,
            "surcharge_euros": "1.50",
        })
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("shared_catalogue_update", args=["boissons", drink.pk]), {
            "name": drink.name, "description": "Tasse", "is_active": "on",
            "surcharge_euros": "1.50", "version": shared_option_version(drink),
        })
        self.assertEqual(response.status_code, 302)
        drink.refresh_from_db()
        self.assertEqual(drink.temperature, "hot")

    def test_profile_copies_drink_categories_and_independent_event_settings(self):
        self.event.morning_hot_beverage_enabled = False
        self.event.save()
        Beverage.objects.create(event_day=self.day, name="Café local", temperature="hot")
        profile = save_event_profile(event=self.event, name="Profil boissons")
        clone = create_event_from_profile(profile=profile, name="Nouveau concours", event_date=date(2027, 5, 1))
        self.assertFalse(clone.morning_hot_beverage_enabled)
        self.assertTrue(clone.afternoon_cold_beverage_enabled)
        self.assertEqual(clone.days.get().beverages.get().temperature, "hot")
        clone.afternoon_cold_beverage_enabled = False
        clone.save()
        self.event.refresh_from_db()
        self.assertTrue(self.event.afternoon_cold_beverage_enabled)

    def test_old_profiles_default_to_enabled_without_guessing_drink_categories(self):
        Beverage.objects.create(event_day=self.day, name="Ancienne boisson")
        profile = save_event_profile(event=self.event, name="Ancien profil")
        snapshot = deepcopy(profile.snapshot)
        for name in ("morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled"):
            snapshot["event"].pop(name)
        snapshot["days"][0]["beverages"][0].pop("temperature")
        profile.snapshot = snapshot
        profile.save()
        clone = create_event_from_profile(profile=profile, name="Nouveau", event_date=date(2027, 5, 1))
        self.assertTrue(clone.morning_hot_beverage_enabled)
        self.assertTrue(clone.afternoon_cold_beverage_enabled)
        self.assertEqual(clone.days.get().beverages.get().temperature, "unspecified")

    def test_profile_editor_updates_settings_and_preserves_them_when_not_supplied(self):
        profile = create_reusable_profile(name="Matin seulement", location="Club",
                                          afternoon_cold_beverage_enabled=False)
        form = ReusableProfileForm(profile=profile)
        self.assertTrue(form.initial["morning_hot_beverage_enabled"])
        self.assertFalse(form.initial["afternoon_cold_beverage_enabled"])
        metadata = {"name": profile.name, "description": "", "location": "Club",
                    "event_description": "", "general_instructions": ""}
        profile = update_reusable_profile(profile=profile, expected_updated_at=profile.updated_at.isoformat(),
                                           **metadata, morning_hot_beverage_enabled=False)
        self.assertFalse(profile.snapshot["event"]["morning_hot_beverage_enabled"])
        self.assertFalse(profile.snapshot["event"]["afternoon_cold_beverage_enabled"])
        clone = create_event_from_profile(profile=profile, name="Sans boissons collation", event_date=date(2027, 6, 1))
        self.assertFalse(clone.morning_hot_beverage_enabled)
        self.assertFalse(clone.afternoon_cold_beverage_enabled)

    def test_invalid_profile_setting_is_rejected_without_partial_event(self):
        profile = create_reusable_profile(name="Boissons", location="Club")
        profile.snapshot["event"]["morning_hot_beverage_enabled"] = "false"
        profile.save()
        with self.assertRaises(ValidationError):
            create_event_from_profile(profile=profile, name="Invalide", event_date=date(2027, 6, 1))
        self.assertEqual(EventProfile.objects.count(), 1)

    def test_event_update_changes_each_toggle_independently_and_is_private(self):
        self.client.force_login(self.organizer)
        url = reverse("event_update", args=[self.event.pk])
        data = {"name": self.event.name, "location": self.event.location,
                "event_date": self.day.date.isoformat(), "status": self.event.status,
                "registration_opens_at": self.event.registration_opens_at.strftime("%Y-%m-%d %H:%M:%S"),
                "registration_closes_at": self.event.registration_closes_at.strftime("%Y-%m-%d %H:%M:%S"),
                "meal_orders_close_at": self.event.meal_orders_close_at.strftime("%Y-%m-%d %H:%M:%S"),
                "afternoon_cold_beverage_enabled": "on"}
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, 302)
        self.event.refresh_from_db()
        self.assertFalse(self.event.morning_hot_beverage_enabled)
        self.assertTrue(self.event.afternoon_cold_beverage_enabled)
        self.client.force_login(self.family_user)
        self.assertEqual(self.client.post(url, data).status_code, 403)
