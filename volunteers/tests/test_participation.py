from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Beverage,
    MealOrder,
    Menu,
    Payment,
    Registration,
    SiteBranding,
)
from volunteers.services import create_assignment

from .factories import DataMixin


class ParticipationTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.family_user)
        self.full = Menu.objects.create(event_day=self.day, name="Repas complet", meal_type=Menu.MealType.FULL_MEAL, surcharge_cents=350)
        self.snack = Menu.objects.create(event_day=self.day, name="Collation", meal_type=Menu.MealType.SNACK, surcharge_cents=100)
        self.drink = Beverage.objects.create(event_day=self.day, name="Thé", temperature=Beverage.Temperature.HOT, surcharge_cents=50)
        self.url = reverse("family_registration_edit", args=[self.day.pk, self.member.pk])

    def post(self, availability="full_day", menu=None, **extra):
        return self.client.post(self.url, {"availability": availability, "menu": str((menu or self.full).pk), "beverage": str(self.drink.pk), "payment_method": "sumup", **extra})

    def test_presence_full_meal_and_preferred_payment_saved_together(self):
        response = self.post()
        self.assertEqual(response.status_code, 302)
        registration = Registration.objects.get(member=self.member, event_day=self.day)
        order = registration.meal_order
        self.assertEqual(order.menu, self.full)
        self.assertEqual(order.beverage, self.drink)
        self.assertEqual(order.total_surcharge_cents, 400)
        self.assertEqual(order.payment_method, "sumup")
        self.assertFalse(Payment.objects.exists())

    def test_half_day_accepts_snack_but_rejects_full_meal(self):
        response = self.post("morning")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Registration.objects.filter(member=self.member).exists())
        self.assertEqual(self.post("morning", self.snack).status_code, 302)
        self.assertEqual(Registration.objects.get(member=self.member).meal_order.menu, self.snack)

    def test_no_meal_is_explicit_and_creates_no_charge(self):
        response = self.client.post(self.url, {"availability": "full_day", "no_meal": "on", "payment_method": "counter"})
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration__member=self.member)
        self.assertFalse(order.wants_meal)
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_inactive_menu_and_other_family_member_are_rejected(self):
        other_menu = Menu.objects.create(event_day=self.day, name="Inactif", is_active=False)
        self.assertEqual(self.post(menu=other_menu).status_code, 200)
        other_url = reverse("family_registration_edit", args=[self.day.pk, self.other_member.pk])
        self.assertEqual(self.client.post(other_url, {"availability": "full_day", "no_meal": "on"}).status_code, 404)
        self.assertFalse(Registration.objects.filter(member=self.member).exists())

    def test_failure_after_presence_save_rolls_back_assignments_and_registration(self):
        registration = self.registration()
        assignment = create_assignment(registration=registration, station_slot=self.afternoon_slot, actor=self.organizer)
        # The form is valid at initialization, but the meal service itself must validate deadlines.
        from unittest.mock import patch

        from volunteers.services import BusinessRuleError
        with patch("volunteers.participation.save_meal_order", side_effect=BusinessRuleError("Repas fermé")):
            response = self.post("morning", self.snack)
        self.assertContains(response, "Repas fermé")
        registration.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(registration.availability, "full_day")
        self.assertEqual(assignment.status, Assignment.Status.DRAFT)
        self.assertFalse(MealOrder.objects.exists())

    def test_payment_choice_does_not_confirm_payment_and_global_link_is_shown(self):
        SiteBranding.objects.create(pk=1, sumup_payment_url="https://pay.sumup.com/test-association")
        self.post()
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, "https://pay.sumup.com/test-association")
        self.assertFalse(Payment.objects.exists())

    def test_closed_meals_preserve_current_choice_when_presence_unchanged(self):
        self.post()
        self.event.meal_orders_close_at = timezone.now() - timezone.timedelta(minutes=1)
        self.event.save()
        response = self.client.post(self.url, {"availability": "full_day", "note": "Remarque modifiée"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(MealOrder.objects.get().menu, self.full)
        response = self.client.post(self.url, {"availability": "morning"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Registration.objects.get(member=self.member).availability, "full_day")

    def test_legacy_half_day_choice_is_preserved_but_not_granted_to_new_bookings(self):
        reg = self.registration(availability="morning")
        order = MealOrder(registration=reg, wants_meal=True, menu=self.snack)
        order.save()
        # Represents a choice saved before the new entitlement rule was introduced.
        MealOrder.objects.filter(pk=order.pk).update(menu=self.full, legacy_availability="morning")
        response = self.post("morning", self.full)
        self.assertEqual(response.status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.menu, self.full)
        self.assertEqual(self.post("afternoon", self.full).status_code, 200)

    def test_reserved_menu_cannot_change_type_in_manual_form(self):
        self.post()
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("menu_update", args=[self.full.pk]), {
            "name": self.full.name, "description": "", "meal_type": "snack",
            "surcharge_euros": "3.50", "is_active": "on",
        })
        self.assertContains(response, "réservations incompatibles")
        self.full.refresh_from_db()
        self.assertEqual(self.full.meal_type, "full_meal")

    def test_stale_menu_object_is_revalidated_against_current_database_state(self):
        from volunteers.services import BusinessRuleError, save_meal_order
        registration = self.registration()
        Menu.objects.filter(pk=self.full.pk).update(meal_type="snack", is_active=False)
        with self.assertRaises(BusinessRuleError):
            save_meal_order(registration=registration, menu=self.full, actor=self.family_user)
        self.assertFalse(MealOrder.objects.exists())

    def test_inactive_current_menu_and_beverage_can_be_kept_without_new_booking(self):
        self.post()
        Menu.objects.filter(pk=self.full.pk).update(is_active=False, surcharge_cents=999)
        Beverage.objects.filter(pk=self.drink.pk).update(is_active=False, surcharge_cents=999)
        self.assertEqual(self.post(note="Nouvelle remarque").status_code, 302)
        self.assertEqual(MealOrder.objects.get().total_surcharge_cents, 400)
        registration = self.registration(member=self.other_member)
        self.client.force_login(self.other_user)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.other_member.pk]), {
            "availability": "full_day", "menu": str(self.full.pk), "beverage": str(self.drink.pk), "payment_method": "counter",
        })
        self.assertEqual(response.status_code, 200)
        self.assertFalse(MealOrder.objects.filter(registration=registration).exists())

    def test_organizer_can_fill_meal_for_participant_without_order(self):
        registration = self.registration()
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("meal_order_create", args=[registration.pk]), {
            "wants_meal": "on", "menu": str(self.full.pk), "beverage": str(self.drink.pk), "payment_method": "counter",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(registration.meal_order.menu, self.full)

    def test_choice_created_by_previous_version_during_rollout_is_preserved(self):
        registration = self.registration(availability="morning")
        order = MealOrder.objects.create(registration=registration, wants_meal=True, menu=self.snack)
        MealOrder.objects.filter(pk=order.pk).update(menu=self.full, legacy_availability="legacy")
        self.assertEqual(self.post("morning", self.full).status_code, 302)
        order.refresh_from_db()
        self.assertEqual(order.legacy_availability, "morning")
        self.assertEqual(order.menu, self.full)
