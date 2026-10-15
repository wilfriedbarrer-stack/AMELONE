from datetime import date, timedelta
from uuid import uuid4

from django.test import TestCase
from django.urls import reverse

from volunteers.models import (
    Beverage,
    Event,
    EventDay,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    User,
)

from .factories import DataMixin


class MealTicketTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(
            event_day=self.day, name="Déjeuner végétarien", description="Légumes et dessert",
            surcharge_cents=400,
        )
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus de pomme", surcharge_cents=100)
        self.reg = self.registration(note="REMARQUE STRICTEMENT PRIVÉE")
        self.order = MealOrder.objects.create(
            registration=self.reg, wants_meal=True, menu=self.menu, beverage=self.drink,
            payment_method=MealOrder.PaymentMethod.SUMUP,
        )
        self.client.force_login(self.organizer)

    def url(self):
        return reverse("meal_tickets", args=[self.event.pk])

    def add_member(self, first_name="Zoé", *, event_day=None, wants_meal=True, menu=None):
        member = Member.objects.create(
            family=self.family, first_name=first_name, last_name="Durand", birth_date=date(1995, 1, 1),
        )
        day = event_day or self.day
        registration = Registration.objects.create(
            member=member, event_day=day, availability=Registration.Availability.FULL_DAY,
        )
        order = MealOrder.objects.create(
            registration=registration, wants_meal=wants_meal,
            menu=(menu or self.menu) if wants_meal else None,
        )
        return member, registration, order

    def foreign_event(self):
        return Event.objects.create(
            name="Autre concours", description="Description autre", location="Autre lieu",
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
            status=Event.Status.PUBLISHED,
        )

    def test_public_and_family_accounts_cannot_access_tickets(self):
        self.client.logout()
        self.assertEqual(self.client.get(self.url()).status_code, 302)
        self.family_user.is_competition_official = True
        self.family_user.save()
        for user in (self.family_user, self.other_user):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                self.assertEqual(self.client.get(self.url()).status_code, 403)

    def test_organizer_and_administrator_can_read_tickets(self):
        self.assertEqual(self.client.get(self.url()).status_code, 200)
        admin = User.objects.create_superuser("ticket-admin@example.test", "Strong-pass-123")
        self.client.force_login(admin)
        self.assertEqual(self.client.get(self.url()).status_code, 200)

    def test_ticket_contains_person_day_choices_and_stable_human_reference(self):
        response = self.client.get(self.url())
        self.assertEqual(len(response.context["tickets"]), 1)
        ticket = response.context["tickets"][0]
        self.assertEqual(ticket["full_name"], self.member.full_name)
        self.assertEqual(ticket["family_name"], "Durand")
        self.assertEqual(ticket["event_name"], self.event.name)
        self.assertEqual(ticket["event_date"], self.day.date)
        self.assertEqual(ticket["meal_type_label"], "Repas complet")
        self.assertEqual(ticket["menu_name"], self.menu.name)
        self.assertEqual(ticket["menu_description"], self.menu.description)
        self.assertEqual(ticket["beverage_name"], self.drink.name)
        self.assertEqual(ticket["reference"], f"REP-{self.order.pk.hex[:12].upper()}")
        repeated = self.client.get(self.url(), {"print": "1"}).context["tickets"][0]
        self.assertEqual(repeated["reference"], ticket["reference"])
        self.assertContains(response, self.member.full_name)
        self.assertContains(response, self.menu.name)
        self.assertContains(response, self.drink.name)

    def test_no_meal_and_cancelled_registrations_are_excluded(self):
        self.add_member("Sans repas", wants_meal=False)
        _, cancelled, _ = self.add_member("Annulée")
        Registration.objects.filter(pk=cancelled.pk).update(status=Registration.Status.CANCELLED)
        response = self.client.get(self.url())
        self.assertEqual([ticket["member_id"] for ticket in response.context["tickets"]], [self.member.pk])
        self.assertNotContains(response, "Annulée Durand")

    def test_tickets_are_scoped_to_event_and_foreign_day_or_member_is_not_found(self):
        event = self.foreign_event()
        day = EventDay.objects.create(event=event, date=self.day.date)
        menu = Menu.objects.create(event_day=day, name="Menu autre événement")
        registration = Registration.objects.create(
            member=self.other_member, event_day=day, availability=Registration.Availability.FULL_DAY,
        )
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=menu)
        response = self.client.get(self.url())
        self.assertEqual(len(response.context["tickets"]), 1)
        self.assertNotContains(response, menu.name)
        for query in ({"day": str(day.pk)}, {"member": str(self.other_member.pk)},
                      {"day": "invalid"}, {"member": "invalid"}):
            with self.subTest(query=query):
                self.assertEqual(self.client.get(self.url(), query).status_code, 404)
        self.assertEqual(self.client.get(reverse("meal_tickets", args=[uuid4()])).status_code, 404)

    def test_each_person_has_one_ticket_per_day_and_day_filter_is_exact(self):
        day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        menu = Menu.objects.create(event_day=day, name="Repas du second jour")
        registration = Registration.objects.create(
            member=self.member, event_day=day, availability=Registration.Availability.FULL_DAY,
        )
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=menu)
        self.add_member("Alice")
        response = self.client.get(self.url())
        rows = response.context["tickets"]
        self.assertEqual([(row["event_date"], row["first_name"]) for row in rows], [
            (self.day.date, "Alice"), (self.day.date, "Lina"), (day.date, "Lina"),
        ])
        self.assertEqual(len({row["reference"] for row in rows}), 3)
        filtered = self.client.get(self.url(), {"day": str(day.pk)})
        self.assertEqual(filtered.context["selected_day"], day)
        self.assertEqual(len(filtered.context["tickets"]), 1)
        self.assertEqual(filtered.context["tickets"][0]["menu_name"], menu.name)
        participant = self.client.get(self.url(), {"member": str(self.member.pk)})
        self.assertEqual(len(participant.context["tickets"]), 2)
        self.assertEqual(participant.context["member_filter"], self.member)

    def test_active_member_without_order_can_be_filtered_without_printing_ticket(self):
        member, _, _ = self.add_member("Sans repas", wants_meal=False)
        response = self.client.get(self.url(), {"member": str(member.pk)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["tickets"], [])
        self.assertEqual(response.context["sheets"], [])

    def test_half_day_snack_and_no_beverage_are_printed_correctly(self):
        member = Member.objects.create(
            family=self.family, first_name="Demi", last_name="Durand", birth_date=date(1990, 5, 1),
        )
        registration = Registration.objects.create(
            member=member, event_day=self.day, availability=Registration.Availability.MORNING,
        )
        snack = Menu.objects.create(
            event_day=self.day, name="Collation demi-journée", meal_type=Menu.MealType.SNACK,
        )
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=snack)
        response = self.client.get(self.url(), {"member": str(member.pk)})
        ticket = response.context["tickets"][0]
        self.assertEqual(ticket["meal_type_label"], "Collation")
        self.assertEqual(ticket["menu_name"], snack.name)
        self.assertEqual(ticket["beverage_name"], "Sans boisson")
        self.assertContains(response, "Sans boisson")

    def test_full_day_morning_and_afternoon_snacks_remain_on_one_ticket(self):
        morning = Menu.objects.create(
            event_day=self.day, name="Petit-déjeuner", description="Café et brioche",
            meal_type=Menu.MealType.SNACK,
        )
        afternoon = Menu.objects.create(
            event_day=self.day, name="Goûter", description="Fruit et gâteau",
            meal_type=Menu.MealType.SNACK,
        )
        self.order.morning_snack = morning
        self.order.afternoon_snack = afternoon
        self.order.save()
        response = self.client.get(self.url())
        tickets = response.context["tickets"]
        self.assertEqual(len(tickets), 1)
        self.assertEqual(tickets[0]["menu_name"], self.menu.name)
        self.assertEqual(tickets[0]["morning_snack_name"], morning.name)
        self.assertEqual(tickets[0]["morning_snack_description"], morning.description)
        self.assertEqual(tickets[0]["afternoon_snack_name"], afternoon.name)
        self.assertEqual(tickets[0]["afternoon_snack_description"], afternoon.description)
        self.assertContains(response, morning.name)
        self.assertContains(response, afternoon.name)

    def test_sheets_hold_six_tickets_and_pad_the_last_sheet_without_extra_tickets(self):
        for index in range(6):
            self.add_member(f"Participant {index}")
        response = self.client.get(self.url())
        self.assertEqual(len(response.context["tickets"]), 7)
        sheets = response.context["sheets"]
        self.assertEqual(len(sheets), 2)
        self.assertEqual([len(sheet) for sheet in sheets], [6, 6])
        self.assertEqual(sum(ticket is not None for sheet in sheets for ticket in sheet), 7)
        self.assertEqual(sum(ticket is None for ticket in sheets[-1]), 5)

    def test_personal_contacts_birth_date_and_private_notes_are_not_exposed(self):
        self.family.name = ""
        self.family.save()
        self.family_user.first_name = self.family_user.last_name = ""
        self.family_user.save()
        response = self.client.get(self.url())
        for private in (self.family_user.email, self.family_user.phone,
                        "29/02/2000", "2000-02-29", "REMARQUE STRICTEMENT PRIVÉE"):
            self.assertNotContains(response, private)
        self.assertEqual(response.context["tickets"][0]["family_name"], "Famille Durand")
        self.assertNotIn("birth_date", response.context["tickets"][0])
        self.assertNotIn("note", response.context["tickets"][0])

    def test_official_note_uses_applied_exemption_not_actor_or_account_role(self):
        self.organizer.is_competition_official = True
        self.organizer.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.assertFalse(self.client.get(self.url()).context["tickets"][0]["official_exemption"])
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        response = self.client.get(self.url())
        self.assertTrue(response.context["tickets"][0]["official_exemption"])
        self.assertContains(response, "Officiel — sans supplément")

    def test_print_and_reprint_never_change_orders_registrations_or_payments(self):
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=100,
            method=Payment.Method.ON_SITE, note="PAIEMENT PRIVÉ",
        )
        before_orders = list(MealOrder.objects.order_by("pk").values())
        before_registrations = list(Registration.objects.order_by("pk").values())
        before_payments = list(Payment.objects.order_by("pk").values())
        for query in ({}, {"print": "1"}, {"print": "1", "paid": "1", "used": "1",
                                           "member": str(self.member.pk), "day": str(self.day.pk)}):
            response = self.client.get(self.url(), query)
            self.assertEqual(response.status_code, 200)
            self.assertNotContains(response, payment.note)
        self.assertEqual(list(MealOrder.objects.order_by("pk").values()), before_orders)
        self.assertEqual(list(Registration.objects.order_by("pk").values()), before_registrations)
        self.assertEqual(list(Payment.objects.order_by("pk").values()), before_payments)

    def test_mutating_http_methods_are_not_allowed(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(self.url(), {}).status_code, 405)
