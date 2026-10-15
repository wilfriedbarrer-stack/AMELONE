import csv
import io

from django.contrib import admin
from django.test import TestCase
from django.urls import reverse

from volunteers.admin import BeverageAdmin, EventAdmin, MealOrderAdmin
from volunteers.models import Beverage, Event, MealOrder, Menu, Payment, Registration
from volunteers.services import save_meal_order

from .factories import DataMixin


class SnackBeverageReportingTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Repas complet", surcharge_cents=100)
        self.snack = Menu.objects.create(
            name="Fruit et biscuit", meal_type=Menu.MealType.SNACK, surcharge_cents=50,
        )
        self.hot = Beverage.objects.create(
            name="Thé chaud", temperature=Beverage.Temperature.HOT, surcharge_cents=25,
        )
        self.cold = Beverage.objects.create(
            name="Jus frais", temperature=Beverage.Temperature.COLD, surcharge_cents=40,
        )
        self.client.force_login(self.organizer)

    def order(self, member=None):
        return save_meal_order(
            registration=self.registration(member=member), menu=self.menu,
            morning_snack=self.snack, afternoon_snack=self.snack,
            beverage=self.hot, morning_beverage=self.hot, afternoon_beverage=self.cold,
            actor=self.organizer,
        )

    def report(self, name, *, view="participants"):
        response = self.client.get(reverse(name, args=[self.event.pk]), {"view": view}, follow=True)
        self.assertEqual(response.status_code, 200)
        if name == "catering":
            self.assertEqual(response.redirect_chain, [(
                reverse("recap_meals", args=[self.event.pk]) + f"?view={view}", 302,
            )])
        return response

    def test_recap_and_catering_distinguish_three_drink_service_periods(self):
        self.order()
        for name in ("recap_meals", "catering"):
            with self.subTest(name=name):
                response = self.report(name, view="quantities")
                self.assertEqual(
                    {(item["type"], item["name"]): item["count"]
                     for item in response.context["beverage_quantities"]},
                    {
                        ("Boisson — collation matin", self.hot.name): 1,
                        ("Boisson — repas", self.hot.name): 1,
                        ("Boisson — collation après-midi", self.cold.name): 1,
                    },
                )
                self.assertContains(response, "Boisson — collation matin")
                self.assertContains(response, "Boisson — collation après-midi")
                self.assertContains(response, self.hot.name)
                self.assertContains(response, self.cold.name)
        row = self.client.get(reverse("recap_meals", args=[self.event.pk])).context["rows"][0]
        self.assertEqual(row["morning_beverage_name"], self.hot.name)
        self.assertEqual(row["afternoon_beverage_name"], self.cold.name)
        self.assertEqual(row["surcharge_cents"], 290)

    def test_disabled_offer_keeps_existing_drinks_and_saved_prices_in_reports_and_exports(self):
        order = self.order()
        Event.objects.filter(pk=self.event.pk).update(
            morning_hot_beverage_enabled=False, afternoon_cold_beverage_enabled=False,
        )
        Beverage.objects.all().update(surcharge_cents=9999, is_active=False)
        before = MealOrder.objects.filter(pk=order.pk).values().get()
        for name in ("recap_meals", "catering"):
            response = self.report(name)
            self.assertContains(response, self.hot.name)
            self.assertContains(response, self.cold.name)
        for name in ("recap_meals_csv", "catering_export"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.event.pk]))
                rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
                self.assertEqual(rows[1][rows[0].index("Boisson chaude matin")], self.hot.name)
                self.assertEqual(rows[1][rows[0].index("Boisson fraîche après-midi")], self.cold.name)
                self.assertEqual(rows[1][-1], "2,90")
        self.assertEqual(MealOrder.objects.filter(pk=order.pk).values().get(), before)
        self.assertFalse(Payment.objects.exists())

    def test_cancelled_orders_do_not_increase_drink_quantities(self):
        self.order()
        cancelled = self.order(member=self.other_member)
        cancelled.registration.status = Registration.Status.CANCELLED
        cancelled.registration.save(update_fields=["status"])
        for name in ("recap_meals", "catering"):
            response = self.report(name, view="quantities")
            self.assertEqual(sum(item["count"] for item in response.context["beverage_quantities"]), 3)

    def test_half_day_main_drink_is_counted_in_its_collation_period(self):
        save_meal_order(
            registration=self.registration(availability=Registration.Availability.MORNING),
            menu=self.snack, beverage=self.hot, actor=self.organizer,
        )
        save_meal_order(
            registration=self.registration(member=self.other_member, availability=Registration.Availability.AFTERNOON),
            menu=self.snack, beverage=self.cold, actor=self.organizer,
        )
        response = self.report("catering", view="quantities")
        self.assertEqual(
            {(item["type"], item["name"]): item["count"] for item in response.context["beverage_quantities"]},
            {("Boisson — collation matin", self.hot.name): 1,
             ("Boisson — collation après-midi", self.cold.name): 1},
        )

    def test_historical_main_drink_uses_saved_availability(self):
        order = self.order()
        # An administrator can retain a past meal after correcting presence.
        Registration.objects.filter(pk=order.registration_id).update(availability=Registration.Availability.MORNING)
        response = self.report("catering", view="quantities")
        self.assertIn(
            ("Boisson — repas", self.hot.name),
            {(item["type"], item["name"]) for item in response.context["beverage_quantities"]},
        )

    def test_organizer_create_and_update_forward_both_extra_drinks(self):
        registration = self.registration()
        data = {
            "wants_meal": "on", "menu": str(self.menu.pk), "beverage": str(self.hot.pk),
            "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.snack.pk),
            "morning_beverage": str(self.hot.pk), "afternoon_beverage": str(self.cold.pk),
            "payment_method": "counter",
        }
        response = self.client.post(reverse("meal_order_create", args=[registration.pk]), data)
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration=registration)
        self.assertEqual((order.morning_beverage_id, order.afternoon_beverage_id), (self.hot.pk, self.cold.pk))
        data.update(morning_beverage="", afternoon_beverage="")
        response = self.client.post(reverse("meal_order_update", args=[order.pk]), data)
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertIsNone(order.morning_beverage_id)
        self.assertIsNone(order.afternoon_beverage_id)
        self.assertEqual(order.total_surcharge_cents, 225)

    def test_event_duplicate_keeps_drink_settings_and_local_temperature(self):
        self.event.morning_hot_beverage_enabled = False
        self.event.afternoon_cold_beverage_enabled = False
        self.event.save(update_fields=["morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled"])
        local = Beverage.objects.create(
            event_day=self.day, name="Café local", temperature=Beverage.Temperature.HOT,
        )
        response = self.client.post(reverse("event_duplicate", args=[self.event.pk]), {
            "name": "Copie avec réglages boissons", "copy_catering": "on",
        })
        self.assertEqual(response.status_code, 302)
        clone = Event.objects.get(name="Copie avec réglages boissons")
        self.assertFalse(clone.morning_hot_beverage_enabled)
        self.assertFalse(clone.afternoon_cold_beverage_enabled)
        self.assertEqual(
            Beverage.objects.get(event_day__event=clone, name=local.name).temperature,
            Beverage.Temperature.HOT,
        )

    def test_admin_exposes_settings_temperature_and_readonly_price_snapshots(self):
        self.assertIn("morning_hot_beverage_enabled", EventAdmin.list_filter)
        self.assertIn("afternoon_cold_beverage_enabled", EventAdmin.list_filter)
        self.assertIn("temperature", BeverageAdmin.list_filter)
        model_admin = MealOrderAdmin(MealOrder, admin.site)
        for component in ("morning_beverage", "afternoon_beverage"):
            self.assertIn(component, model_admin.autocomplete_fields)
            self.assertIn(f"{component}_surcharge_cents", model_admin.readonly_fields)
            self.assertIn(f"{component}_standard_surcharge_cents", model_admin.readonly_fields)

    def test_csv_template_documents_drink_temperature(self):
        response = self.client.get(reverse("catalogue_template", kwargs={"kind": "boissons"}))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[0][-1], "temperature")
        self.assertEqual({row[-1] for row in rows[1:]}, {"chaude", "fraîche"})
