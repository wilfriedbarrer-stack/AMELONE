from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from volunteers.forms_family import FamilyMealForm, FamilyRegistrationForm
from volunteers.forms_organizer import BeverageForm, MealOrderOrganizerForm
from volunteers.forms_self import PersonalRegistrationForm
from volunteers.models import Beverage, EventDay, MealOrder, Menu

from .factories import DataMixin


class SnackBeverageFormTests(DataMixin, TestCase):
    def setUp(self):
        self.meal = Menu.objects.create(name="Repas complet", surcharge_cents=350)
        self.snack = Menu.objects.create(name="Collation", meal_type="snack", surcharge_cents=100)
        self.hot = Beverage.objects.create(name="Thé", temperature="hot", surcharge_cents=50)
        self.cold = Beverage.objects.create(name="Jus frais", temperature="cold", surcharge_cents=80)
        self.unclassified = Beverage.objects.create(name="Boisson à classer", surcharge_cents=30)

    def data(self, **changes):
        return {
            "availability": "full_day", "menu": str(self.meal.pk),
            "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.snack.pk),
            "beverage": str(self.unclassified.pk), "morning_beverage": str(self.hot.pk),
            "afternoon_beverage": str(self.cold.pk), "payment_method": "sumup", **changes,
        }

    def disable_beverages(self):
        self.event.morning_hot_beverage_enabled = False
        self.event.afternoon_cold_beverage_enabled = False
        self.event.save(update_fields=["morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled"])

    def test_six_components_are_available_to_all_three_meal_forms(self):
        registration = self.registration()
        for form in (
            FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member),
            FamilyMealForm(self.data(), registration=registration),
            MealOrderOrganizerForm({"wants_meal": "on", **self.data()}, registration=registration,
                                   instance=MealOrder(registration=registration)),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.cleaned_data["morning_beverage"], self.hot)
                self.assertEqual(form.cleaned_data["afternoon_beverage"], self.cold)
                fields = list(form.fields)
                self.assertLess(fields.index("morning_snack"), fields.index("morning_beverage"))
                self.assertLess(fields.index("afternoon_snack"), fields.index("afternoon_beverage"))

    def test_beverages_remain_optional_and_temperature_is_scoped_to_period(self):
        form = FamilyRegistrationForm(self.data(morning_beverage="", afternoon_beverage=""),
                                      event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        for field, option in (("morning_beverage", self.cold), ("afternoon_beverage", self.hot),
                              ("morning_beverage", self.unclassified)):
            with self.subTest(field=field, option=option.name):
                form = FamilyRegistrationForm(self.data(**{field: str(option.pk)}),
                                              event_day=self.day, member=self.member)
                self.assertFalse(form.is_valid())
                self.assertIn(field, form.errors)

    def test_snack_beverage_requires_its_snack_and_rejects_half_day_or_no_meal(self):
        for changes in (
            {"morning_snack": ""}, {"afternoon_snack": ""}, {"no_meal": "on"},
            {"availability": "morning", "menu": str(self.snack.pk)},
        ):
            with self.subTest(changes=changes):
                form = FamilyRegistrationForm(self.data(**changes), event_day=self.day, member=self.member)
                self.assertFalse(form.is_valid())
        plain = FamilyRegistrationForm({"availability": "full_day", "no_meal": "on"},
                                       event_day=self.day, member=self.member)
        self.assertTrue(plain.is_valid(), plain.errors)
        self.assertIsNone(plain.cleaned_data["morning_beverage"])
        self.assertIsNone(plain.cleaned_data["afternoon_beverage"])

    def test_foreign_or_inactive_beverage_is_not_a_new_choice(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        foreign = Beverage.objects.create(event_day=other_day, name="Autre thé", temperature="hot")
        inactive = Beverage.objects.create(name="Ancien thé", temperature="hot", is_active=False)
        for option in (foreign, inactive):
            form = FamilyRegistrationForm(self.data(morning_beverage=str(option.pk)),
                                          event_day=self.day, member=self.member)
            self.assertFalse(form.is_valid())
            self.assertIn("morning_beverage", form.errors)

    def test_half_day_principal_beverage_matches_temperature_and_enabled_event(self):
        for period, accepted, refused in (("morning", self.hot, self.cold),
                                         ("afternoon", self.cold, self.hot)):
            data = {"availability": period, "menu": str(self.snack.pk), "beverage": str(accepted.pk)}
            form = FamilyRegistrationForm(data, event_day=self.day, member=self.member)
            self.assertTrue(form.is_valid(), form.errors)
            for option in (refused, self.unclassified):
                form = FamilyRegistrationForm({**data, "beverage": str(option.pk)},
                                              event_day=self.day, member=self.member)
                self.assertFalse(form.is_valid())
                self.assertIn("beverage", form.errors)
        self.disable_beverages()
        for period, option in (("morning", self.hot), ("afternoon", self.cold)):
            form = FamilyRegistrationForm({"availability": period, "menu": str(self.snack.pk),
                                           "beverage": str(option.pk)}, event_day=self.day, member=self.member)
            self.assertFalse(form.is_valid())
            self.assertIn("beverage", form.errors)

    def test_disabling_snack_beverages_leaves_full_day_meal_beverage_available(self):
        self.disable_beverages()
        for beverage in (self.hot, self.cold, self.unclassified):
            form = FamilyRegistrationForm(self.data(beverage=str(beverage.pk), morning_beverage="",
                                                   afternoon_beverage=""), event_day=self.day, member=self.member)
            self.assertTrue(form.is_valid(), form.errors)
        form = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member)
        self.assertFalse(form.is_valid())
        self.assertIn("morning_beverage", form.errors)
        self.assertIn("afternoon_beverage", form.errors)

    def test_saved_extras_survive_disabled_event_changed_type_and_catalogue_price(self):
        registration = self.registration()
        order = MealOrder.objects.create(
            registration=registration, wants_meal=True, menu=self.meal, beverage=self.unclassified,
            morning_snack=self.snack, afternoon_snack=self.snack,
            morning_beverage=self.hot, afternoon_beverage=self.cold,
        )
        self.disable_beverages()
        Beverage.objects.filter(pk=self.hot.pk).update(is_active=False, temperature="cold", surcharge_cents=50000)
        Beverage.objects.filter(pk=self.cold.pk).update(temperature="hot", surcharge_cents=60000)
        for form in (
            FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member, registration=registration),
            FamilyMealForm(self.data(), registration=registration, meal_order=order),
            MealOrderOrganizerForm({"wants_meal": "on", **self.data()}, registration=registration, instance=order),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.initial["morning_beverage"], self.hot.pk)
                self.assertEqual(form.initial["afternoon_beverage"], self.cold.pk)
                for field, option, price in (("morning_beverage", self.hot, "0,50"),
                                             ("afternoon_beverage", self.cold, "0,80")):
                    self.assertIn(price + " €", form.fields[field].label_from_instance(option))
                    self.assertIn("réservation est conservée", form.fields[field].help_text)

    def test_saved_half_day_beverage_is_preserved_only_in_its_original_phase(self):
        registration = self.registration(availability="morning")
        order = MealOrder.objects.create(registration=registration, wants_meal=True,
                                         menu=self.snack, beverage=self.hot)
        self.disable_beverages()
        Beverage.objects.filter(pk=self.hot.pk).update(is_active=False, temperature="unspecified")
        same = FamilyMealForm({"menu": str(self.snack.pk), "beverage": str(self.hot.pk),
                               "payment_method": "counter"}, registration=registration, meal_order=order)
        self.assertTrue(same.is_valid(), same.errors)
        moved = FamilyRegistrationForm({"availability": "afternoon", "menu": str(self.snack.pk),
                                        "beverage": str(self.hot.pk)}, event_day=self.day,
                                       member=self.member, registration=registration)
        self.assertFalse(moved.is_valid())
        self.assertIn("beverage", moved.errors)

    def test_official_preview_and_reserved_prices_include_both_snack_beverages(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        registration = self.registration()
        order = MealOrder.objects.create(
            registration=registration, wants_meal=True, menu=self.meal, beverage=self.unclassified,
            morning_snack=self.snack, afternoon_snack=self.snack,
            morning_beverage=self.hot, afternoon_beverage=self.cold,
        )
        Beverage.objects.filter(pk__in=[self.hot.pk, self.cold.pk]).update(surcharge_cents=50000)
        free = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member, registration=registration)
        self.assertTrue(free.is_valid(), free.errors)
        paid = FamilyRegistrationForm(self.data(preferred_station=str(self.station.pk)),
                                      event_day=self.day, member=self.member, registration=registration)
        self.assertTrue(paid.is_valid(), paid.errors)
        for field, option, price in (("morning_beverage", self.hot, 50), ("afternoon_beverage", self.cold, 80)):
            self.assertIn("0,00 € (officiel)", free.fields[field].label_from_instance(option))
            entry = next(item for item in paid.official_pricing_data["option_prices"][field]
                         if item["id"] == str(option.pk))
            self.assertEqual(entry["standard_surcharge"], price)
            self.assertEqual(entry["standard"], paid.fields[field].label_from_instance(option))
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_family_and_personal_posts_save_both_beverages_and_show_summary(self):
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]), self.data())
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration__member=self.member)
        self.assertEqual((order.morning_beverage_id, order.afternoon_beverage_id), (self.hot.pk, self.cold.pk))
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, "Boisson chaude du matin : " + self.hot.name)
        self.assertContains(response, "Boisson froide de l’après-midi : " + self.cold.name)
        self.member.user = self.family_user
        self.member.is_competition_official = True
        self.member.save(update_fields=["user", "is_competition_official"])
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=["is_competition_official"])
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("personal_registration", args=[self.day.pk]), self.data())
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual((order.morning_beverage_id, order.afternoon_beverage_id), (self.hot.pk, self.cold.pk))
        response = self.client.get(reverse("personal_participation"))
        self.assertContains(response, "Boisson chaude du matin : " + self.hot.name)
        self.assertContains(response, "Boisson froide de l’après-midi : " + self.cold.name)

    def test_beverage_form_missing_temperature_keeps_existing_classification(self):
        for instance, expected in ((self.hot, "hot"), (self.cold, "cold"), (Beverage(), "unspecified")):
            form = BeverageForm({"name": "Boisson formulaire", "surcharge_euros": "1.00", "is_active": "on"}, instance=instance)
            self.assertTrue(form.is_valid(), form.errors)
            self.assertEqual(form.cleaned_data["temperature"], expected)

    def test_administrator_personal_form_inherits_both_beverage_choices(self):
        self.family_user.is_superuser = True
        form = PersonalRegistrationForm(self.data(station_slots=[]), user=self.family_user,
                                        event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["morning_beverage"], self.hot)
        self.assertEqual(form.cleaned_data["afternoon_beverage"], self.cold)
