import csv
import io

from django.test import TestCase
from django.urls import reverse

from volunteers.meal_catalogue import menu_options_for_day
from volunteers.models import Beverage, MealOrder, Menu, Registration
from volunteers.participation import save_participation
from volunteers.services import BusinessRuleError, save_meal_order

from .factories import DataMixin


class SnackReportingTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Repas commun", surcharge_cents=100)
        self.snack = Menu.objects.create(
            name="Collation commune", meal_type=Menu.MealType.SNACK, surcharge_cents=50,
        )
        self.drink = Beverage.objects.create(name="Jus commun", surcharge_cents=25)
        self.client.force_login(self.organizer)

    def order(self, member=None):
        return save_meal_order(
            registration=self.registration(member=member), menu=self.menu,
            morning_snack=self.snack, afternoon_snack=self.snack, beverage=self.drink,
            actor=self.organizer,
        )

    def test_recap_and_catering_count_snacks_separately_from_main_meal(self):
        order = self.order()
        response = self.client.get(reverse("recap_meals", args=[self.event.pk]))
        row = response.context["rows"][0]
        self.assertEqual(row["morning_snack_name"], self.snack.name)
        self.assertEqual(row["afternoon_snack_name"], self.snack.name)
        phases = {item["type"]: item["count"] for item in response.context["quantities"]}
        self.assertEqual(phases["Collation matin"], 1)
        self.assertEqual(phases["Collation après-midi"], 1)
        self.assertEqual(order.total_surcharge_cents, 225)
        catering_url = reverse("catering", args=[self.event.pk])
        quantities_url = reverse("recap_meals", args=[self.event.pk]) + "?view=quantities"
        self.assertRedirects(
            self.client.get(catering_url, {"view": "quantities"}), quantities_url,
            fetch_redirect_response=False,
        )
        response = self.client.get(catering_url, {"view": "quantities"}, follow=True)
        self.assertContains(response, self.menu.name)
        self.assertEqual(sum(item["count"] for item in response.context["quantities"]), 3)
        self.assertEqual(sum(item["count"] for item in response.context["beverage_quantities"]), 1)
        self.assertIn(self.menu, menu_options_for_day(self.day))
        self.assertContains(response, reverse("shared_catalogue"))

    def test_exports_include_both_snacks_and_preserve_total_column(self):
        self.order()
        for name in ("recap_meals_csv", "catering_export"):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=[self.event.pk]))
                rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
                self.assertIn("Collation matin", rows[0])
                self.assertIn("Collation après-midi", rows[0])
                self.assertEqual(rows[1][rows[0].index("Collation matin")], self.snack.name)
                self.assertEqual(rows[1][-1], "2,25")

    def test_cancelled_registrations_are_excluded_from_preparation_quantities(self):
        self.order()
        cancelled = self.order(member=self.other_member)
        cancelled.registration.status = Registration.Status.CANCELLED
        cancelled.registration.save(update_fields=["status"])
        response = self.client.get(reverse("catering", args=[self.event.pk]), follow=True)
        self.assertEqual(response.redirect_chain, [(reverse("recap_meals", args=[self.event.pk]), 302)])
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["order"].registration.member, self.member)
        self.assertEqual(sum(item["count"] for item in response.context["quantities"]), 3)

    def test_closed_meals_cannot_silently_drop_snacks_from_a_legacy_main_choice(self):
        order = self.order()
        # Représente un ancien choix dérogatoire : le repas principal est une
        # collation conservée, donc son type seul autoriserait la demi-journée.
        Menu.objects.filter(pk=self.menu.pk).update(meal_type=Menu.MealType.SNACK)
        MealOrder.objects.filter(pk=order.pk).update(legacy_availability="legacy")
        with self.assertRaisesMessage(BusinessRuleError, "les collations et les boissons ensemble"):
            save_participation(
                member=self.member, event_day=self.day,
                availability=Registration.Availability.MORNING, actor=self.family_user,
                meal_choices=None,
            )
        order.refresh_from_db()
        order.registration.refresh_from_db()
        self.assertEqual(order.registration.availability, Registration.Availability.FULL_DAY)
        self.assertEqual(order.morning_snack_id, self.snack.pk)
        self.assertEqual(order.afternoon_snack_id, self.snack.pk)
        self.assertEqual(order.total_surcharge_cents, 225)

    def test_presence_view_forwards_both_snack_choices(self):
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]), {
            "availability": "full_day", "menu": str(self.menu.pk),
            "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.snack.pk),
            "beverage": str(self.drink.pk), "payment_method": "counter",
        })
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration__member=self.member)
        self.assertEqual(order.morning_snack_id, self.snack.pk)
        self.assertEqual(order.afternoon_snack_id, self.snack.pk)
        self.assertEqual(order.total_surcharge_cents, 225)

    def test_legacy_option_edit_urls_redirect_global_items_to_central_editor(self):
        for name, kind, option in (("menu_update", "menus", self.menu), ("beverage_update", "boissons", self.drink)):
            with self.subTest(name=name):
                self.assertRedirects(
                    self.client.get(reverse(name, args=[option.pk])),
                    reverse("shared_catalogue_update", kwargs={"kind": kind, "option_id": option.pk}),
                )
