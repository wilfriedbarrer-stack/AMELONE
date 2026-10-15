from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.forms_family import FamilyMealForm, FamilyRegistrationForm
from volunteers.forms_self import PersonalRegistrationForm
from volunteers.models import Beverage, MealOrder, Menu

from .factories import DataMixin


class ParticipationErgonomicsTests(DataMixin, TestCase):
    def setUp(self):
        self.menu = Menu.objects.create(name="Repas du midi", surcharge_cents=101)
        self.snack = Menu.objects.create(name="Collation", meal_type="snack", surcharge_cents=7)
        self.drink = Beverage.objects.create(name="Eau", surcharge_cents=13)
        self.hot = Beverage.objects.create(name="Thé", temperature="hot", surcharge_cents=17)
        self.cold = Beverage.objects.create(name="Jus frais", temperature="cold", surcharge_cents=19)
        self.client.force_login(self.family_user)

    def data(self, **changes):
        return {"availability": "full_day", "menu": str(self.menu.pk), "beverage": str(self.drink.pk),
                "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.snack.pk),
                "morning_beverage": str(self.hot.pk), "afternoon_beverage": str(self.cold.pk), **changes}

    def order(self):
        return MealOrder.objects.create(registration=self.registration(), wants_meal=True,
                                        menu=self.menu, beverage=self.drink, morning_snack=self.snack,
                                        afternoon_snack=self.snack, morning_beverage=self.hot,
                                        afternoon_beverage=self.cold)

    def test_admin_posts_precede_meal_choices_in_form_and_page(self):
        self.family_user.is_superuser = True
        form = PersonalRegistrationForm(self.data(station_slots=[]), user=self.family_user,
                                        event_day=self.day, member=self.member)
        fields = list(form.fields)
        self.assertLess(fields.index("availability"), fields.index("station_slots"))
        self.assertLess(fields.index("station_slots"), fields.index("menu"))
        self.member.user = self.family_user
        self.member.save(update_fields=["user"])
        self.family_user.save(update_fields=["is_superuser"])
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("personal_registration", args=[self.day.pk]))
        html = response.content.decode()
        self.assertLess(html.index('name="station_slots"'), html.index('name="menu"'))
        self.assertLess(html.index('id="meal-period-morning"'), html.index('id="meal-period-main"'))
        self.assertLess(html.index('id="meal-period-main"'), html.index('id="meal-period-afternoon"'))
        self.assertContains(response, "Montant de cette réservation")

    def test_preview_uses_six_integer_cent_components_and_reserved_prices(self):
        order = self.order()
        Menu.objects.filter(pk__in=[self.menu.pk, self.snack.pk]).update(surcharge_cents=99999)
        Beverage.objects.filter(pk__in=[self.drink.pk, self.hot.pk, self.cold.pk]).update(surcharge_cents=99999)
        form = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member, registration=order.registration)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.preview_total_cents, 164)
        self.assertEqual(len(form.preview_summary), 6)
        self.assertEqual(sum(item["cents"] for item in form.preview_summary), 164)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]),
                                    self.data(note="x" * 1001))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '<strong id="meal-summary-total">1,64 €</strong>', html=True)
        self.assertEqual(response.context["form"].data["menu"], str(self.menu.pk))
        self.assertEqual(response.context["form"].data["morning_beverage"], str(self.hot.pk))
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 164)

    def test_explicit_no_meal_clears_only_meal_components_on_submission(self):
        order = self.order()
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]),
                                    self.data(no_meal="on", payment_method="forged"))
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertFalse(order.wants_meal)
        self.assertEqual(order.total_surcharge_cents, 0)
        for component in MealOrder.PRICE_COMPONENTS:
            self.assertIsNone(getattr(order, f"{component}_id"))
        invalid = FamilyRegistrationForm(self.data(no_meal="on", preferred_station="invalid", note="x" * 1001),
                                         event_day=self.day, member=self.member, registration=order.registration)
        self.assertFalse(invalid.is_valid())
        self.assertIn("preferred_station", invalid.errors)
        self.assertIn("note", invalid.errors)

    def test_zero_price_booking_does_not_require_payment_and_can_still_change_menu(self):
        free = Menu.objects.create(name="Repas sans supplément", surcharge_cents=0)
        registration = self.registration()
        form = FamilyMealForm({"menu": str(free.pk), "payment_method": "forged"}, registration=registration)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.preview_total_cents, 0)
        self.assertFalse(form.fields["payment_method"].required)
        self.assertTrue(form.fields["payment_method"].disabled)
        self.assertFalse(form.fields["payment_method"].widget.is_hidden)
        self.assertEqual(form.cleaned_data["payment_method"], "counter")
        paid = FamilyMealForm({"menu": str(self.menu.pk), "payment_method": "forged"}, registration=registration)
        self.assertFalse(paid.is_valid())
        self.assertIn("payment_method", paid.errors)

    def test_official_and_volunteer_previews_apply_to_total(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        free = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member)
        self.assertEqual(free.preview_total_cents, 0)
        paid = FamilyRegistrationForm(self.data(preferred_station=str(self.station.pk)),
                                      event_day=self.day, member=self.member)
        self.assertEqual(paid.preview_total_cents, 164)

    def test_family_has_one_participation_action_and_explicit_card_labels(self):
        order = self.order()
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        edit = reverse("family_registration_edit", args=[self.day.pk, self.member.pk])
        meals = reverse("family_meal_edit", args=[order.registration_id])
        self.assertContains(response, f'href="{edit}"', count=1)
        self.assertNotContains(response, f'href="{meals}"')
        self.assertContains(response, "Modifier la participation")
        self.assertContains(response, "<dt>Présence</dt>", html=True)
        self.assertContains(response, "<dt>Affectation confirmée</dt>", html=True)
        self.assertContains(response, "<dt>Restauration</dt>", html=True)
        self.event.registration_closes_at = timezone.now() - timedelta(minutes=1)
        self.event.save(update_fields=["registration_closes_at"])
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertNotContains(response, f'href="{edit}"')
        self.assertContains(response, f'href="{meals}"', count=1)
        self.assertContains(response, "Modifier les repas")

    def test_personal_profile_offers_existing_member_without_duplicate_identity_write(self):
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=["is_competition_official"])
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("personal_profile"))
        self.assertContains(response, 'id="participant-identity"')
        self.assertContains(response, 'id="id_existing_member"')
        response = self.client.post(reverse("personal_profile"), {"existing_member": str(self.member.pk),
                                                                  "first_name": "Ignoré", "last_name": "Ignoré"})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Lina")
        self.assertEqual(self.member.user_id, self.family_user.pk)

    def test_only_homonyms_keep_origin_in_choice_labels_and_javascript_metadata(self):
        local_menu = Menu.objects.create(event_day=self.day, name=self.menu.name, surcharge_cents=101)
        local_hot = Beverage.objects.create(event_day=self.day, name=self.hot.name, temperature="hot", surcharge_cents=17)
        form = FamilyRegistrationForm(self.data(), event_day=self.day, member=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        self.assertIn(" — Commun — ", form.fields["menu"].label_from_instance(self.menu))
        self.assertIn(" — Spécifique à cet événement — ", form.fields["menu"].label_from_instance(local_menu))
        self.assertIn(" — Commun — ", form.fields["morning_beverage"].label_from_instance(self.hot))
        self.assertIn(" — Spécifique à cet événement — ", form.fields["morning_beverage"].label_from_instance(local_hot))
        self.assertEqual(form.fields["morning_snack"].label_from_instance(self.snack), "Collation — 0,07 €")
        for name in ("menu", "morning_beverage"):
            records = form.official_pricing_data["option_prices"][name]
            self.assertEqual({record["origin"] for record in records}, {"Commun", "Spécifique à cet événement"})
