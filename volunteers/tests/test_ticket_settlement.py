import base64
from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse

from volunteers.models import (
    Event,
    EventDay,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    SiteBranding,
)
from volunteers.services import create_assignment, remove_assignment

from .factories import DataMixin


class TicketSettlementTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Repas commun", surcharge_cents=500)
        self.order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=self.menu)
        self.url = reverse("meal_tickets", args=[self.event.pk])
        self.client.force_login(self.organizer)

    def payment(self, amount, **kwargs):
        return Payment.objects.create(
            family=kwargs.pop("family", self.family), event=kwargs.pop("event", self.event),
            amount_cents=amount, method=Payment.Method.ON_SITE, **kwargs,
        )

    def ticket(self, **filters):
        response = self.client.get(self.url, filters)
        self.assertEqual(response.status_code, 200)
        return response.context["tickets"][0]

    def sibling(self):
        return Member.objects.create(
            family=self.family, first_name="Zoé", last_name="Durand", birth_date=date(1990, 1, 1),
        )

    def test_unpaid_or_sumup_preference_is_not_a_settled_note(self):
        self.order.payment_method = MealOrder.PaymentMethod.SUMUP
        self.order.save()
        ticket = self.ticket(paid="1", settled="1")
        self.assertFalse(ticket["note_settled"])
        self.assertEqual(ticket["note_remaining_cents"], 500)
        self.assertEqual(ticket["note_status_label"], "Note famille non soldée")
        self.assertContains(self.client.get(self.url), "Note famille non soldée")
        self.assertContains(self.client.get(self.url), "5,00 €")
        self.assertFalse(Payment.objects.exists())

    def test_partial_payments_full_payment_and_overpayment_use_recorded_amounts(self):
        payment = self.payment(200)
        for amount, remaining, settled in ((200, 300, False), (500, 0, True), (700, 0, True)):
            Payment.objects.filter(pk=payment.pk).update(amount_cents=amount)
            with self.subTest(amount=amount):
                ticket = self.ticket()
                self.assertEqual(ticket["note_remaining_cents"], remaining)
                self.assertIs(ticket["note_settled"], settled)
                if settled:
                    self.assertEqual(ticket["note_status_label"], "Note famille soldée")

    def test_payments_from_other_family_or_event_do_not_settle_this_note(self):
        other_event = Event.objects.create(
            name="Autre concours", registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        self.payment(5000, family=self.other_family)
        self.payment(5000, event=other_event)
        self.assertFalse(self.ticket()["note_settled"])
        self.assertEqual(self.ticket()["note_remaining_cents"], 500)

    def test_day_and_participant_filters_keep_the_entire_family_event_balance(self):
        sibling = self.sibling()
        MealOrder.objects.create(registration=self.registration(member=sibling), wants_meal=True, menu=self.menu)
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        registration = Registration.objects.create(
            member=self.member, event_day=other_day, availability=Registration.Availability.FULL_DAY,
        )
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=self.menu)
        self.payment(500)
        for filters in ({}, {"day": self.day.pk}, {"member": self.member.pk},
                        {"day": self.day.pk, "member": self.member.pk}):
            with self.subTest(filters=filters):
                response = self.client.get(self.url, filters)
                self.assertTrue(response.context["tickets"])
                for ticket in response.context["tickets"]:
                    self.assertFalse(ticket["note_settled"])
                    self.assertEqual(ticket["note_remaining_cents"], 1000)

    def test_cancelled_registrations_are_excluded_from_the_family_note(self):
        sibling = self.sibling()
        registration = self.registration(member=sibling)
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=self.menu)
        Registration.objects.filter(pk=registration.pk).update(status=Registration.Status.CANCELLED)
        self.payment(500)
        self.assertTrue(self.ticket()["note_settled"])

    def test_reserved_prices_are_used_instead_of_current_catalogue_prices(self):
        self.payment(500)
        Menu.objects.filter(pk=self.menu.pk).update(surcharge_cents=50000)
        before = MealOrder.objects.filter(pk=self.order.pk).values().get()
        self.assertTrue(self.ticket()["note_settled"])
        self.assertEqual(MealOrder.objects.filter(pk=self.order.pk).values().get(), before)

    def test_free_official_note_is_settled_but_another_member_can_still_owe_money(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        self.assertTrue(self.ticket()["note_settled"])
        sibling = self.sibling()
        MealOrder.objects.create(registration=self.registration(member=sibling), wants_meal=True, menu=self.menu)
        official = self.ticket(member=self.member.pk)
        self.assertTrue(official["official_exemption"])
        self.assertFalse(official["note_settled"])
        self.assertEqual(official["note_remaining_cents"], 500)
        self.assertFalse(Payment.objects.exists())

    def test_status_is_recomputed_on_reprint_without_mutating_orders_or_payments(self):
        initial = self.client.get(self.url, {"day": self.day.pk, "member": self.member.pk, "paid": "1"})
        self.assertFalse(initial.context["tickets"][0]["note_settled"])
        print_url = initial.context["print_url"]
        self.assertEqual(parse_qs(urlsplit(print_url).query), {
            "print": ["1"], "day": [str(self.day.pk)], "member": [str(self.member.pk)],
        })
        self.payment(500)
        before_orders = list(MealOrder.objects.values().order_by("pk"))
        before_payments = list(Payment.objects.values().order_by("pk"))
        response = self.client.get(print_url)
        self.assertTrue(response.context["tickets"][0]["note_settled"])
        self.assertTrue(response.context["print_view"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertEqual(list(MealOrder.objects.values().order_by("pk")), before_orders)
        self.assertEqual(list(Payment.objects.values().order_by("pk")), before_payments)

    def test_official_volunteer_assignment_updates_ticket_debt_and_preserves_payment_on_removal(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        self.assertTrue(self.ticket()["note_settled"])
        assignment = create_assignment(
            registration=self.order.registration, station_slot=self.morning_slot, actor=self.organizer,
        )
        ticket = self.ticket()
        self.assertFalse(ticket["official_exemption"])
        self.assertFalse(ticket["note_settled"])
        self.assertEqual(ticket["note_remaining_cents"], 500)
        self.assertContains(self.client.get(self.url), "5,00 €")
        payment = self.payment(500)
        before = Payment.objects.filter(pk=payment.pk).values().get()
        self.assertTrue(self.ticket()["note_settled"])
        remove_assignment(assignment=assignment, actor=self.organizer)
        self.assertTrue(self.ticket()["official_exemption"])
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), before)


class TicketLogoTests(DataMixin, TestCase):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jF9sAAAAASUVORK5CYII="
    )

    def logo(self, value=None):
        value = value or "data:image/png;base64," + base64.b64encode(self.png).decode()
        SiteBranding.objects.update_or_create(pk=1, defaults={"logo_data": value})
        return value

    def test_logo_is_public_served_exactly_and_replaced_without_stale_cache(self):
        self.logo()
        url = reverse("association_logo_image")
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, self.png)
        self.assertEqual(response.headers["Content-Type"], "image/png")
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.client.head(url).status_code, 200)
        self.assertEqual(self.client.post(url, {}).status_code, 405)
        replacement = b"replacement-fixture"
        self.logo("data:image/webp;base64," + base64.b64encode(replacement).decode())
        updated = self.client.get(url)
        self.assertEqual(updated.content, replacement)
        self.assertEqual(updated.headers["Content-Type"], "image/webp")

    def test_missing_invalid_unsupported_or_oversized_logo_is_not_served(self):
        url = reverse("association_logo_image")
        self.assertEqual(self.client.get(url).status_code, 404)
        for value in ("", "data:image/svg+xml;base64,PHN2Zy8+", "data:image/png;base64,invalid!",
                      "data:image/png;base64,", "data:image/png;base64," + base64.b64encode(b"x" * (1024 * 1024 + 1)).decode()):
            with self.subTest(value_type=value[:28]):
                SiteBranding.objects.update_or_create(pk=1, defaults={"logo_data": value})
                self.assertEqual(self.client.get(url).status_code, 404)

    def test_every_ticket_reuses_the_logo_url_and_omitted_logo_keeps_text_branding(self):
        value = self.logo()
        menu = Menu.objects.create(name="Repas")
        for member in (self.member, self.other_member):
            MealOrder.objects.create(registration=self.registration(member=member), wants_meal=True, menu=menu)
        self.client.force_login(self.organizer)
        url = reverse("meal_tickets", args=[self.event.pk])
        response = self.client.get(url)
        self.assertContains(response, 'class="meal-ticket-logo"', count=2)
        self.assertContains(response, 'src="' + reverse("association_logo_image") + '"', count=2)
        # Le seul contenu base64 reste dans l'en-tête global déjà existant.
        self.assertEqual(response.content.count(value.encode()), 1)
        SiteBranding.objects.filter(pk=1).update(logo_data="")
        response = self.client.get(url)
        self.assertNotContains(response, 'class="meal-ticket-logo"')
        self.assertContains(response, "AMELONE LOISIRS")
