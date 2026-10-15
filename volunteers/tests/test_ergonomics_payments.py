from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.models import EventDay, MealOrder, Menu, Payment, Registration

from .factories import DataMixin


class PaymentWorkspaceTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Repas du concours", surcharge_cents=500)
        self.order = MealOrder.objects.create(
            registration=self.registration(), wants_meal=True, menu=self.menu,
        )
        self.url = reverse("payments", args=[self.event.pk])
        self.client.force_login(self.organizer)

    def payment(self, amount):
        return Payment.objects.create(
            family=self.family, event=self.event, amount_cents=amount,
            method=Payment.Method.ON_SITE,
        )

    def post_data(self, **overrides):
        return {
            "family": str(self.family.pk), "amount_euros": "1.25",
            "method": Payment.Method.ON_SITE,
            "paid_at": timezone.localtime().strftime("%Y-%m-%dT%H:%M"),
            "note": "Règlement partiel au comptoir", **overrides,
        }

    def test_balances_precede_payment_form_and_link_to_family_collection(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertLess(content.index('id="soldes-title"'), content.index('id="encaissement"'))
        self.assertContains(response, f'{self.url}?family={self.family.pk}#encaissement')
        self.assertContains(response, 'data-label="Reste à régler"')
        self.assertContains(response, 'class="mobile-card-table"', count=2)
        self.assertContains(response, "Détails facultatifs")
        self.assertContains(response, "Enregistrer le paiement reçu")
        self.assertFalse(Payment.objects.exists())

    def test_statuses_are_translated_and_settled_families_have_no_collection_button(self):
        self.assertContains(self.client.get(self.url), ">À régler</span>")
        payment = self.payment(200)
        self.assertContains(self.client.get(self.url), ">Partiellement réglée</span>")
        for amount, label in ((500, "Soldée"), (700, "Trop-perçu")):
            Payment.objects.filter(pk=payment.pk).update(amount_cents=amount)
            response = self.client.get(self.url)
            self.assertContains(response, f">{label}</span>")
            self.assertNotContains(response, f"?family={self.family.pk}#encaissement")
        self.assertContains(response, "Trop-perçu : 2,00 €")

    def test_family_collection_prefills_remaining_event_total_from_reserved_prices(self):
        next_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        registration = Registration.objects.create(
            member=self.member, event_day=next_day,
            availability=Registration.Availability.FULL_DAY,
        )
        snack = Menu.objects.create(name="Second repas", surcharge_cents=250)
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=snack)
        self.payment(200)
        Menu.objects.filter(pk__in=[self.menu.pk, snack.pk]).update(surcharge_cents=50000)
        before_orders = list(MealOrder.objects.order_by("pk").values())
        before_payments = list(Payment.objects.order_by("pk").values())
        response = self.client.get(self.url, {"family": self.family.pk})
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(str(form["family"].value()), str(self.family.pk))
        self.assertEqual(form["amount_euros"].value(), Decimal("5.50"))
        self.assertIsNotNone(form["paid_at"].value())
        self.assertFalse(form.fields["family"].widget.is_hidden)
        self.assertContains(response, "Encaisser — Durand")
        self.assertEqual(response.context["selected_payment_family"], self.family)
        self.assertEqual(list(MealOrder.objects.order_by("pk").values()), before_orders)
        self.assertEqual(list(Payment.objects.order_by("pk").values()), before_payments)

    def test_prefill_rejects_unknown_or_unrelated_families(self):
        for family in ("invalide", self.other_family.pk, "00000000-0000-0000-0000-000000000000"):
            with self.subTest(family=family):
                self.assertEqual(self.client.get(self.url, {"family": family}).status_code, 404)
        self.assertFalse(Payment.objects.exists())

    def test_prefill_allows_family_with_payment_history_even_without_registration(self):
        Payment.objects.create(
            family=self.other_family, event=self.event, amount_cents=100,
            method=Payment.Method.SUMUP,
        )
        response = self.client.get(self.url, {"family": self.other_family.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(str(response.context["form"]["family"].value()), str(self.other_family.pk))
        self.assertEqual(response.context["form"]["amount_euros"].value(), Decimal("0"))
        self.assertEqual(Payment.objects.count(), 1)

    def test_collection_amount_is_editable_and_post_records_actual_partial_payment(self):
        response = self.client.post(f"{self.url}?family={self.other_family.pk}", self.post_data())
        self.assertRedirects(response, self.url)
        payment = Payment.objects.get()
        self.assertEqual(payment.family, self.family)
        self.assertEqual(payment.amount_cents, 125)
        self.assertEqual(payment.note, "Règlement partiel au comptoir")
        self.assertEqual(payment.recorded_by, self.organizer)
        self.assertContains(self.client.get(self.url), "3,75 €")

    def test_invalid_post_preserves_entered_amount_and_opens_date_errors(self):
        response = self.client.post(
            f"{self.url}?family={self.family.pk}", self.post_data(amount_euros="2.15", paid_at="invalide"),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["amount_euros"].value(), "2.15")
        self.assertIn("paid_at", response.context["form"].errors)
        self.assertContains(response, '<details class="optional-details" open>')
        self.assertFalse(Payment.objects.exists())

    def test_post_cannot_collect_for_family_outside_event(self):
        response = self.client.post(self.url, self.post_data(family=str(self.other_family.pk)))
        self.assertEqual(response.status_code, 200)
        self.assertIn("family", response.context["form"].errors)
        self.assertFalse(Payment.objects.exists())

    def test_family_account_cannot_see_balances_or_collect(self):
        self.client.force_login(self.family_user)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.post(self.url, self.post_data()).status_code, 403)
        self.assertFalse(Payment.objects.exists())
