from django.test import TestCase
from django.urls import reverse

from volunteers.models import Beverage, MealOrder, Menu, Registration

from .factories import DataMixin


class MealTicketSnackDrinkTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Assiette végétarienne", surcharge_cents=400)
        self.snack = Menu.objects.create(
            name="Fruit et brioche", meal_type=Menu.MealType.SNACK, surcharge_cents=25,
        )
        self.hot = Beverage.objects.create(
            name="Chocolat chaud", temperature=Beverage.Temperature.HOT, surcharge_cents=75,
        )
        self.cold = Beverage.objects.create(
            name="Citronnade fraîche", temperature=Beverage.Temperature.COLD, surcharge_cents=100,
        )
        self.client.force_login(self.organizer)

    def response(self):
        return self.client.get(reverse("meal_tickets", args=[self.event.pk]))

    def full_day_order(self, **kwargs):
        return MealOrder.objects.create(
            registration=self.registration(), wants_meal=True, menu=self.menu,
            morning_snack=self.snack, afternoon_snack=self.snack, beverage=self.cold,
            **kwargs,
        )

    def test_three_drinks_print_on_one_full_day_ticket_with_family_remaining_amount(self):
        order = self.full_day_order(morning_beverage=self.hot, afternoon_beverage=self.cold)
        response = self.response()
        tickets = response.context["tickets"]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["beverage_label"], "Boisson du repas")
        self.assertEqual(tickets[0]["morning_beverage_name"], self.hot.name)
        self.assertEqual(tickets[0]["afternoon_beverage_name"], self.cold.name)
        self.assertEqual(tickets[0]["note_remaining_cents"], order.total_surcharge_cents)
        for text in ("Boisson du repas", "Boisson chaude du matin", "Boisson fraîche de l’après-midi"):
            self.assertContains(response, text)
        self.assertContains(response, "reste : 7,25 €")

    def test_optional_snack_drinks_are_omitted_when_not_selected(self):
        self.full_day_order()
        response = self.response()
        self.assertEqual(response.context["tickets"][0]["morning_beverage_name"], "")
        self.assertEqual(response.context["tickets"][0]["afternoon_beverage_name"], "")
        self.assertNotContains(response, "Boisson chaude du matin")
        self.assertNotContains(response, "Boisson fraîche de l’après-midi")

    def test_half_day_morning_drink_has_its_time_label(self):
        MealOrder.objects.create(
            registration=self.registration(availability=Registration.Availability.MORNING),
            wants_meal=True, menu=self.snack, beverage=self.hot,
        )
        response = self.response()
        ticket = response.context["tickets"][0]
        self.assertEqual(ticket["beverage_label"], "Boisson chaude du matin")
        self.assertEqual(ticket["beverage_name"], self.hot.name)
        self.assertContains(response, "Boisson chaude du matin")
        self.assertNotContains(response, "Boisson du repas")

    def test_half_day_afternoon_drink_has_its_time_label(self):
        MealOrder.objects.create(
            registration=self.registration(availability=Registration.Availability.AFTERNOON),
            wants_meal=True, menu=self.snack, beverage=self.cold,
        )
        response = self.response()
        self.assertEqual(
            response.context["tickets"][0]["beverage_label"], "Boisson fraîche de l’après-midi",
        )
        self.assertContains(response, self.cold.name)
        self.assertNotContains(response, "Boisson du repas")

    def test_disabled_catalogue_drink_stays_on_existing_ticket(self):
        self.full_day_order(morning_beverage=self.hot, afternoon_beverage=self.cold)
        self.hot.is_active = False
        self.hot.save(update_fields=["is_active"])
        response = self.response()
        self.assertEqual(response.context["tickets"][0]["morning_beverage_name"], self.hot.name)
        self.assertContains(response, self.hot.name)
