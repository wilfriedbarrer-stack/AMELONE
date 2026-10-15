import csv
import io
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.forms_family import FamilyMealForm, FamilyRegistrationForm
from volunteers.forms_organizer import MealOrderOrganizerForm
from volunteers.forms_self import PersonalRegistrationForm
from volunteers.models import Beverage, MealOrder, Member, Menu, Registration, SiteBranding, User

from .factories import DataMixin


class OfficialPricingInterfaceTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(
            event_day=self.day, name="Menu du jour", surcharge_cents=350,
        )
        self.snack = Menu.objects.create(
            event_day=self.day, name="Collation", meal_type=Menu.MealType.SNACK,
            surcharge_cents=150,
        )
        self.drink = Beverage.objects.create(
            event_day=self.day, name="Jus", surcharge_cents=100,
        )
        self.client.force_login(self.family_user)

    def make_official(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])

    def meal_order(self, member=None):
        return MealOrder.objects.create(
            registration=self.registration(member=member), menu=self.menu,
            beverage=self.drink, wants_meal=True, payment_method="sumup",
        )

    def test_official_options_and_dynamic_menu_prices_are_zero(self):
        self.make_official()
        response = self.client.get(reverse(
            "family_registration_edit", args=[self.day.pk, self.member.pk],
        ))
        self.assertContains(response, "Officiel de compétition : aucun supplément pour le repas et la boisson.")
        form = response.context["form"]
        self.assertTrue(form.official_exemption)
        self.assertEqual({item["surcharge"] for item in form.menu_options}, {0})
        self.assertIn("0,00 € (officiel)", form.fields["menu"].label_from_instance(self.menu))
        self.assertIn("0,00 € (officiel)", form.fields["beverage"].label_from_instance(self.drink))
        # Le choix reste présent pour s'afficher immédiatement si l'officiel
        # sélectionne ensuite un poste bénévole dans le navigateur.
        self.assertFalse(form.fields["payment_method"].widget.is_hidden)
        self.assertTrue(form.fields["payment_method"].disabled)
        self.assertTrue(form.official_pricing_data["eligible"])

    def test_official_hidden_payment_preserves_valid_cleaned_key_and_ignores_forgery(self):
        self.make_official()
        form = FamilyRegistrationForm({
            "availability": "full_day", "menu": str(self.menu.pk),
            "beverage": str(self.drink.pk), "payment_method": "forged",
        }, event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["payment_method"], "counter")
        response = self.client.post(reverse(
            "family_registration_edit", args=[self.day.pk, self.member.pk],
        ), form.data)
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration__member=self.member)
        self.assertEqual(order.total_surcharge_cents, 0)
        self.assertTrue(order.official_exemption_applied)

    def test_user_roles_and_posted_exemption_do_not_exempt_unhabilitated_member(self):
        User.objects.filter(pk=self.family_user.pk).update(
            is_competition_official=True, is_superuser=True,
        )
        self.family_user.refresh_from_db()
        form = PersonalRegistrationForm({
            "availability": "full_day", "menu": str(self.menu.pk),
            "beverage": str(self.drink.pk), "payment_method": "sumup",
            "official_exemption": "on", "official_exemption_applied": "on",
        }, user=self.family_user, event_day=self.day, member=self.member)
        self.assertFalse(form.official_exemption)
        self.assertEqual({item["surcharge"] for item in form.menu_options}, {150, 350})
        self.assertFalse(form.fields["payment_method"].widget.is_hidden)

    def test_family_and_organizer_edit_forms_show_official_prices(self):
        self.make_official()
        order = self.meal_order()
        for form in (
            FamilyMealForm(registration=order.registration, meal_order=order),
            MealOrderOrganizerForm(registration=order.registration, instance=order),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.official_exemption)
                self.assertIn("0,00 €", form.fields["beverage"].label_from_instance(self.drink))
                self.assertTrue(form.fields["payment_method"].widget.is_hidden)
        response = self.client.get(reverse("family_meal_edit", args=[order.registration.pk]))
        self.assertContains(response, "aucun supplément pour le repas et la boisson")
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("meal_order_update", args=[order.pk]))
        self.assertContains(response, "aucun supplément pour le repas et la boisson")

    def test_zero_official_order_hides_payment_requests_and_sumup_link(self):
        self.make_official()
        self.meal_order()
        SiteBranding.objects.create(pk=1, sumup_payment_url="https://pay.sumup.com/test")
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertFalse(response.context["sumup_requested"])
        self.assertFalse(response.context["has_payment_due"])
        self.assertContains(response, "Aucun règlement attendu")
        self.assertNotContains(response, "Paiement au comptoir")
        self.assertNotContains(response, "https://pay.sumup.com/test")

    def test_official_exemption_keeps_other_family_members_payment_available(self):
        self.make_official()
        self.meal_order()
        relative = Member.objects.create(
            family=self.family, first_name="Paul", last_name="Durand",
            birth_date=self.member.birth_date,
        )
        paid_order = self.meal_order(member=relative)
        SiteBranding.objects.create(pk=1, sumup_payment_url="https://pay.sumup.com/test")
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertTrue(response.context["sumup_requested"])
        self.assertTrue(response.context["has_payment_due"])
        self.assertEqual(response.context["balance"]["due_cents"], 450)
        self.assertEqual(paid_order.total_surcharge_cents, 450)
        self.assertContains(response, "https://pay.sumup.com/test")

    def test_meal_recap_export_and_catering_show_zero_without_changing_catalogue(self):
        self.make_official()
        self.meal_order()
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("recap_meals", args=[self.event.pk]))
        self.assertContains(response, "Officiel — sans supplément")
        self.assertEqual(response.context["rows"][0]["surcharge_cents"], 0)
        response = self.client.get(reverse("recap_meals_csv", args=[self.event.pk]))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[1][-2:], ["Officiel — sans supplément", "0,00"])
        response = self.client.get(reverse("catering_export", args=[self.event.pk]))
        rows = list(csv.reader(io.StringIO(response.content.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[1][-1], "0,00")
        response = self.client.get(reverse("catering", args=[self.event.pk]))
        self.assertRedirects(response, reverse("recap_meals", args=[self.event.pk]), fetch_redirect_response=False)
        response = self.client.get(reverse("catering", args=[self.event.pk]), follow=True)
        self.assertContains(response, "Officiel — sans supplément")
        self.assertEqual(response.context["rows"][0]["surcharge_cents"], 0)
        self.menu.refresh_from_db()
        self.drink.refresh_from_db()
        self.assertEqual((self.menu.surcharge_cents, self.drink.surcharge_cents), (350, 100))

    def test_personal_dashboard_hides_sumup_for_official_order(self):
        self.make_official()
        self.member.user = self.family_user
        self.member.save(update_fields=["user"])
        User.objects.filter(pk=self.family_user.pk).update(is_competition_official=True)
        self.meal_order()
        SiteBranding.objects.create(pk=1, sumup_payment_url="https://pay.sumup.com/test")
        response = self.client.get(reverse("personal_participation"))
        self.assertContains(response, "Officiel — sans supplément")
        self.assertNotContains(response, "https://pay.sumup.com/test")

    def test_past_order_form_preserves_its_historical_exemption(self):
        order = self.meal_order()
        self.day.date = timezone.localdate() - timedelta(days=1)
        self.day.save(update_fields=["date"])
        self.make_official()
        order.refresh_from_db()
        for form in (
            FamilyRegistrationForm(event_day=self.day, registration=order.registration, member=self.member),
            FamilyMealForm(registration=order.registration, meal_order=order),
            MealOrderOrganizerForm(registration=order.registration, instance=order),
        ):
            with self.subTest(form=type(form).__name__):
                self.assertFalse(form.official_exemption)
                self.assertNotIn("0,00 € (officiel)", form.fields["menu"].label_from_instance(self.menu))
                self.assertFalse(form.fields["payment_method"].widget.is_hidden)
        # Un tarif historique déjà exonéré reste aussi lisible après une révocation.
        self.other_member.is_competition_official = True
        self.other_member.save(update_fields=["is_competition_official"])
        historical_order = self.meal_order(member=self.other_member)
        self.other_member.is_competition_official = False
        self.other_member.save(update_fields=["is_competition_official"])
        historical_order.refresh_from_db()
        form = FamilyMealForm(registration=historical_order.registration, meal_order=historical_order)
        self.assertTrue(form.official_exemption)
        self.assertTrue(form.fields["payment_method"].widget.is_hidden)

    def test_future_presence_reactivation_uses_current_member_habilitation(self):
        order = self.meal_order()
        order.registration.status = Registration.Status.CANCELLED
        order.registration.save(update_fields=["status"])
        self.make_official()
        order.refresh_from_db()
        self.assertFalse(order.official_exemption_applied)
        correction = MealOrderOrganizerForm(registration=order.registration, instance=order)
        self.assertFalse(correction.official_exemption)
        presence = FamilyRegistrationForm(
            event_day=self.day, registration=order.registration, member=self.member,
        )
        self.assertTrue(presence.official_exemption)
        self.assertEqual({item["surcharge"] for item in presence.menu_options}, {0})
