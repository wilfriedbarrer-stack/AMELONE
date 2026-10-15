import csv
import io
from datetime import date, timedelta

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from volunteers.forms_catalogue import CatalogueUploadForm
from volunteers.models import Beverage, EventDay, MealOrder, Member, Menu
from volunteers.views_tickets import MealTicketFilterForm

from .factories import DataMixin


class RestorationWorkspaceTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)
        self.menu = Menu.objects.create(name="Repas commun actif", surcharge_cents=300)
        self.snack = Menu.objects.create(name="Fruit du matin", meal_type=Menu.MealType.SNACK)
        self.hot = Beverage.objects.create(name="Thé chaud", temperature=Beverage.Temperature.HOT)
        self.cold = Beverage.objects.create(name="Eau fraîche", temperature=Beverage.Temperature.COLD)

    def order(self):
        return MealOrder.objects.create(
            registration=self.registration(), wants_meal=True, menu=self.menu,
            morning_snack=self.snack, morning_beverage=self.hot, beverage=self.cold,
        )

    def test_canonical_participants_distinguishes_missing_from_declined_without_duplicate_columns(self):
        self.order()
        MealOrder.objects.create(registration=self.registration(member=self.other_member), wants_meal=False)
        member = Member.objects.create(
            family=self.family, first_name="Sans", last_name="Choix", birth_date=date(1990, 1, 1),
        )
        self.registration(member=member)
        response = self.client.get(reverse("recap_meals", args=[self.event.pk]))
        self.assertEqual(response.context["participant_count"], 3)
        self.assertEqual(response.context["ordered_count"], 1)
        self.assertEqual(response.context["no_meal_count"], 1)
        self.assertEqual(response.context["missing_count"], 1)
        for text in ("Sans repas", "À renseigner", self.menu.name, self.hot.name, "Reste famille : 3,00 €"):
            self.assertContains(response, text)
        for old_column in ("<th>Formule</th>", "<th>Choix</th>", "<th>Famille</th>", "<th>Date</th>"):
            self.assertNotContains(response, old_column, html=True)

    def test_quantities_are_a_separate_view_of_the_same_reservations(self):
        self.order()
        response = self.client.get(reverse("recap_meals", args=[self.event.pk]), {"view": "quantities"})
        self.assertEqual(response.context["selected_view"], "quantities")
        self.assertEqual(sum(item["count"] for item in response.context["quantities"]), 2)
        self.assertEqual(sum(item["count"] for item in response.context["beverage_quantities"]), 2)
        self.assertContains(response, "Quantités à préparer")
        self.assertNotContains(response, "Choix des participants")

    def test_legacy_local_options_remain_only_in_collapsible_exceptions(self):
        local = Menu.objects.create(name="Ancienne exception conservée", event_day=self.day)
        response = self.client.get(reverse("recap_meals", args=[self.event.pk]))
        self.assertContains(response, local.name)
        self.assertContains(response, "Réglages et anciennes exceptions de cet événement")
        self.assertNotContains(response, reverse("menu_create", args=[self.day.pk]))
        self.assertNotContains(response, reverse("beverage_create", args=[self.day.pk]))
        self.assertContains(response, reverse("shared_catalogue"))

    def test_complete_csv_keeps_all_individual_choices(self):
        self.order()
        response = self.client.get(reverse("recap_meals_csv", args=[self.event.pk]))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        for header in ("Présence proposée", "Formule", "Choix repas", "Boisson chaude matin", "Boisson fraîche après-midi"):
            self.assertIn(header, rows[0])
        self.assertEqual(rows[1][rows[0].index("Boisson chaude matin")], self.hot.name)
        self.assertEqual(rows[1][-1], "3,00")

    def test_catalogue_defaults_to_active_and_filters_temperature_and_search(self):
        inactive = Beverage.objects.create(name="Ancienne tisane", is_active=False, temperature="hot")
        url = reverse("shared_catalogue")
        response = self.client.get(url)
        self.assertNotContains(response, inactive.name)
        response = self.client.get(url, {"temperature": "hot", "q": "Thé"})
        self.assertQuerySetEqual(response.context["beverages"], [self.hot])
        self.assertNotContains(response, self.cold.name)
        response = self.client.get(url, {"status": "inactive"})
        self.assertQuerySetEqual(response.context["beverages"], [inactive])
        self.assertNotContains(response, self.menu.name)

    def test_invalid_catalogue_filter_keeps_active_default_without_broadening_scope(self):
        inactive = Menu.objects.create(name="Menu retiré", is_active=False)
        response = self.client.get(reverse("shared_catalogue"), {"status": "invalid"})
        self.assertFalse(response.context["filter_form"].is_valid())
        self.assertContains(response, self.menu.name)
        self.assertNotContains(response, inactive.name)

    def test_single_day_fields_are_hidden_but_day_filters_remain_scoped(self):
        self.order()
        self.assertTrue(MealTicketFilterForm(event=self.event).fields["day"].widget.is_hidden)
        upload = SimpleUploadedFile("menus.csv", b"nom\nAncien import\n")
        form = CatalogueUploadForm({}, {"file": upload}, event=self.event)
        self.assertTrue(form.fields["event_day"].widget.is_hidden)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["event_day"], self.day)
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        self.assertFalse(MealTicketFilterForm(event=self.event).fields["day"].widget.is_hidden)
        response = self.client.get(reverse("meal_tickets", args=[self.event.pk]), {"day": other_day.pk})
        self.assertEqual(response.context["selected_day"], other_day)
        self.assertEqual(response.context["tickets"], [])

    def test_single_day_legacy_import_still_rejects_a_foreign_explicit_day(self):
        upload = SimpleUploadedFile("menus.csv", b"nom\nAncien import\n")
        form = CatalogueUploadForm({"event_day": "00000000-0000-0000-0000-000000000000"}, {"file": upload}, event=self.event)
        self.assertFalse(form.is_valid())
        self.assertIn("event_day", form.errors)
