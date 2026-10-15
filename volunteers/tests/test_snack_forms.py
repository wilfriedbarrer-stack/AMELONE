from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from volunteers.forms_family import FamilyMealForm, FamilyRegistrationForm
from volunteers.forms_organizer import MealOrderOrganizerForm
from volunteers.models import Beverage, EventDay, MealOrder, Menu

from .factories import DataMixin


class SharedMealAndSnackFormTests(DataMixin, TestCase):
    def setUp(self):
        self.full = Menu.objects.create(name="Menu commun", surcharge_cents=300)
        self.snack = Menu.objects.create(
            name="Collation commune", meal_type=Menu.MealType.SNACK, surcharge_cents=100,
        )
        self.local_snack = Menu.objects.create(
            event_day=self.day, name="Collation locale", meal_type=Menu.MealType.SNACK,
            surcharge_cents=150,
        )
        self.drink = Beverage.objects.create(name="Jus commun", surcharge_cents=50, temperature=Beverage.Temperature.HOT)

    def data(self, **extra):
        return {
            "availability": "full_day", "menu": str(self.full.pk),
            "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.local_snack.pk),
            "beverage": str(self.drink.pk), "payment_method": "counter", **extra,
        }

    def test_full_day_accepts_common_and_local_snacks_with_clear_origin_labels(self):
        form = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["morning_snack"], self.snack)
        self.assertEqual(form.cleaned_data["afternoon_snack"], self.local_snack)
        field = form.fields["morning_snack"]
        self.assertIn("Commun", field.label_from_instance(self.snack))
        self.assertIn("Spécifique à cet événement", field.label_from_instance(self.local_snack))

    def test_full_day_snacks_are_optional_and_no_meal_cleans_all_extra_keys(self):
        for data in (
            self.data(morning_snack="", afternoon_snack=""),
            {"availability": "full_day", "no_meal": "on"},
        ):
            form = FamilyRegistrationForm(data, event_day=self.day, member=self.member)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertIsNone(form.cleaned_data["morning_snack"])
            self.assertIsNone(form.cleaned_data["afternoon_snack"])

    def test_half_day_forged_extra_snacks_are_rejected(self):
        form = FamilyRegistrationForm(
            self.data(availability="morning", menu=str(self.snack.pk)),
            event_day=self.day, member=self.member,
        )
        self.assertFalse(form.is_valid())
        self.assertIn("réservées à une présence à la journée", str(form.non_field_errors()))
        plain = FamilyRegistrationForm(
            self.data(availability="morning", menu=str(self.snack.pk), morning_snack="", afternoon_snack=""),
            event_day=self.day, member=self.member,
        )
        self.assertTrue(plain.is_valid(), plain.errors)

    def test_foreign_day_inactive_and_full_meal_cannot_be_selected_as_snack(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Menu.objects.create(event_day=other_day, name="Autre jour", meal_type=Menu.MealType.SNACK)
        inactive = Menu.objects.create(name="Inactif", meal_type=Menu.MealType.SNACK, is_active=False)
        for option in (foreign, inactive, self.full):
            with self.subTest(option=option.name):
                form = FamilyRegistrationForm(self.data(morning_snack=str(option.pk)), event_day=self.day, member=self.member)
                self.assertFalse(form.is_valid())
                self.assertIn("morning_snack", form.errors)

    def test_official_prices_are_zero_for_all_four_components(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        form = FamilyRegistrationForm(self.data(payment_method="forged"), event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        for name, option in (("menu", self.full), ("morning_snack", self.snack),
                             ("afternoon_snack", self.local_snack), ("beverage", self.drink)):
            self.assertIn("0,00 € (officiel)", form.fields[name].label_from_instance(option))
        self.assertEqual({item["surcharge"] for item in form.menu_options}, {0})
        self.assertEqual(form.cleaned_data["payment_method"], "counter")

    def test_saved_inactive_snack_is_available_only_to_its_existing_order(self):
        order = MealOrder.objects.create(
            registration=self.registration(), wants_meal=True, menu=self.full,
            morning_snack=self.snack,
        )
        Menu.objects.filter(pk=self.snack.pk).update(is_active=False)
        existing = FamilyMealForm(registration=order.registration, meal_order=order)
        self.assertTrue(existing.fields["morning_snack"].queryset.filter(pk=self.snack.pk).exists())
        fresh = FamilyRegistrationForm(event_day=self.day, member=self.other_member)
        self.assertFalse(fresh.fields["morning_snack"].queryset.filter(pk=self.snack.pk).exists())

    def test_family_and_organizer_forms_accept_same_full_day_combination(self):
        registration = self.registration()
        family_form = FamilyMealForm(self.data(), registration=registration)
        self.assertTrue(family_form.is_valid(), family_form.errors)
        order = MealOrder(registration=registration)
        organizer_form = MealOrderOrganizerForm({"wants_meal": "on", **self.data()}, registration=registration, instance=order)
        self.assertTrue(organizer_form.is_valid(), organizer_form.errors)
        self.assertEqual(organizer_form.cleaned_data["afternoon_snack"], self.local_snack)

    def test_half_day_static_forms_hide_extras_and_report_forgery_without_value_error(self):
        registration = self.registration(availability="morning")
        for form in (
            FamilyMealForm(self.data(menu=str(self.snack.pk)), registration=registration),
            MealOrderOrganizerForm({"wants_meal": "on", **self.data(menu=str(self.snack.pk))}, registration=registration, instance=MealOrder(registration=registration)),
        ):
            self.assertTrue(form.fields["morning_snack"].widget.is_hidden)
            self.assertFalse(form.is_valid())
            self.assertIn("réservées à une présence à la journée", str(form.non_field_errors()))

    def test_family_summary_shows_all_reserved_choices(self):
        MealOrder.objects.create(
            registration=self.registration(), wants_meal=True, menu=self.full,
            morning_snack=self.snack, afternoon_snack=self.local_snack,
        )
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, self.full.name)
        self.assertContains(response, f"Collation du matin : {self.snack.name}")
        self.assertContains(response, f"Collation de l’après-midi : {self.local_snack.name}")
