from datetime import date, timedelta
from unittest.mock import patch
from uuid import uuid4

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.forms_family_administration import AdministratorFamilyAccountForm
from volunteers.models import Assignment, Beverage, Family, Menu, Payment, Station, User
from volunteers.services import save_meal_order
from volunteers.tests.factories import DataMixin
from volunteers.views_family_administration import (
    administrator_family_update,
    administrator_member_update,
)


class AdministratorProfileEditingTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("profiles-admin@example.test", "Strong-pass-123")
        self.client.force_login(self.admin)

    def member_url(self, member=None, family=None):
        return reverse("administrator_member_update", args=[
            (family or self.family).pk, (member or self.member).pk,
        ])

    def family_url(self, family=None):
        return reverse("administrator_family_update", args=[(family or self.family).pk])

    def member_data(self, **changes):
        return {
            "first_name": self.member.first_name,
            "last_name": self.member.last_name,
            "birth_date": self.member.birth_date.isoformat(),
            **changes,
        }

    def family_data(self, **changes):
        return {
            "family_name": self.family.name,
            "first_name": self.family_user.first_name,
            "last_name": self.family_user.last_name,
            "email": self.family_user.email,
            "phone": self.family_user.phone,
            **changes,
        }

    def linked_participant(self):
        self.member.user = self.family_user
        self.member.save()

    def meal_order(self):
        registration = self.registration()
        menu = Menu.objects.create(event_day=self.day, name="Menu payant", surcharge_cents=500)
        drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=150)
        return save_meal_order(registration=registration, menu=menu, beverage=drink)

    def assignment(self):
        return Assignment.objects.create(
            registration=self.registration(), station_slot=self.morning_slot,
        )

    def test_family_and_organizer_cannot_read_or_edit_admin_profiles(self):
        self.family_user.is_competition_official = True
        self.family_user.save()
        for user in (self.family_user, self.organizer):
            self.client.force_login(user)
            for url, data in ((self.family_url(), self.family_data()),
                              (self.member_url(), self.member_data())):
                with self.subTest(user=user.email, url=url):
                    self.assertEqual(self.client.get(url).status_code, 403)
                    self.assertEqual(self.client.post(url, data).status_code, 403)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Lina")

    def test_anonymous_users_must_sign_in(self):
        self.client.logout()
        for url in (self.family_url(), self.member_url()):
            self.assertEqual(self.client.get(url).status_code, 302)

    def test_member_edit_requires_exact_family_for_get_and_post(self):
        for url in (self.member_url(member=self.other_member),
                    self.member_url(family=self.other_family)):
            self.assertEqual(self.client.get(url).status_code, 404)
            self.assertEqual(self.client.post(url, self.member_data(first_name="Intrus")).status_code, 404)
        self.other_member.refresh_from_db()
        self.assertEqual(self.other_member.first_name, "Noé")
        self.assertEqual(self.client.get(reverse("administrator_family_update", args=[uuid4()])).status_code, 404)

    def test_member_get_uses_iso_date_and_editable_official_function(self):
        response = self.client.get(self.member_url())
        self.assertContains(response, 'type="date"')
        self.assertContains(response, 'value="2000-02-29"')
        self.assertContains(response, 'name="is_competition_official"')

    def test_identity_edit_preserves_archive_family_link_and_account_rights(self):
        self.linked_participant()
        archived_at = timezone.now()
        self.member.archived_at = archived_at
        self.member.save()
        self.assertContains(self.client.get(self.member_url()), "Membre archivé")
        old_password = self.family_user.password
        response = self.client.post(self.member_url(), self.member_data(
            first_name="  Céline  ", last_name="Nouvel", birth_date="1999-03-14",
            family=str(self.other_family.pk), user=str(self.admin.pk), archived_at="",
            is_superuser="on", is_staff="on", is_organizer="on", role="administrator",
            password="remplacez", is_active="",
        ))
        self.assertRedirects(response, reverse("administrator_family_detail", args=[self.family.pk]))
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertEqual((self.member.first_name, self.member.last_name, self.member.birth_date),
                         ("Céline", "Nouvel", date(1999, 3, 14)))
        self.assertEqual(self.member.family_id, self.family.pk)
        self.assertEqual(self.member.user_id, self.family_user.pk)
        self.assertEqual(self.member.archived_at, archived_at)
        self.assertFalse(self.family_user.is_superuser)
        self.assertFalse(self.family_user.is_staff)
        self.assertFalse(self.family_user.is_organizer)
        self.assertTrue(self.family_user.is_active)
        self.assertEqual(self.family_user.password, old_password)
        self.assertEqual(self.family_user.first_name, "Alice")

    def test_editing_official_function_syncs_linked_user_and_reprices_only_participant(self):
        self.linked_participant()
        order = self.meal_order()
        payment = Payment.objects.create(
            family=self.family, event=self.event, amount_cents=650, method=Payment.Method.SUMUP,
        )
        payment_snapshot = Payment.objects.filter(pk=payment.pk).values().get()
        for official, expected_price in ((True, 0), (False, 650)):
            with self.subTest(official=official):
                data = self.member_data(first_name="Céline")
                if official:
                    data["is_competition_official"] = "on"
                self.assertEqual(self.client.post(self.member_url(), data).status_code, 302)
                self.member.refresh_from_db()
                self.family_user.refresh_from_db()
                order.refresh_from_db()
                self.assertEqual(self.member.is_competition_official, official)
                self.assertEqual(self.family_user.is_competition_official, official)
                self.assertEqual(order.total_surcharge_cents, expected_price)
                self.assertEqual(order.menu_standard_surcharge_cents, 500)
                self.assertEqual(order.beverage_standard_surcharge_cents, 150)
                self.assertFalse(self.family_user.is_superuser)
        self.assertEqual(Payment.objects.filter(pk=payment.pk).values().get(), payment_snapshot)
        self.other_member.refresh_from_db()
        self.assertFalse(self.other_member.is_competition_official)

    def test_member_date_validation_reuses_future_and_120_year_limits(self):
        for birth_date in ((timezone.localdate() + timedelta(days=1)).isoformat(), "1800-01-01", "29/02/2000"):
            with self.subTest(birth_date=birth_date):
                response = self.client.post(self.member_url(), self.member_data(
                    first_name="Nouveau", birth_date=birth_date, is_competition_official="on",
                ))
                self.assertEqual(response.status_code, 200)
                self.assertIn("birth_date", response.context["form"].errors)
        self.member.refresh_from_db()
        self.assertEqual(self.member.birth_date, date(2000, 2, 29))
        self.assertEqual(self.member.first_name, "Lina")
        self.assertFalse(self.member.is_competition_official)

    def test_incompatible_birth_date_rolls_back_identity_function_and_pricing(self):
        self.linked_participant()
        self.station.age_policy = Station.AgePolicy.ADULTS_ONLY
        self.station.save()
        assignment = self.assignment()
        menu = Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=500)
        order = save_meal_order(registration=assignment.registration, menu=menu)
        response = self.client.post(self.member_url(), self.member_data(
            first_name="Refusé", birth_date="2010-03-14", is_competition_official="on",
        ))
        self.assertContains(response, "Cette date rend une affectation à venir incompatible.")
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.member.first_name, "Lina")
        self.assertEqual(self.member.birth_date, date(2000, 2, 29))
        self.assertFalse(self.member.is_competition_official)
        self.assertFalse(self.family_user.is_competition_official)
        self.assertEqual(order.total_surcharge_cents, 500)

    def test_official_assignment_prevents_revocation_and_partial_identity_save(self):
        self.linked_participant()
        self.member.is_competition_official = True
        self.member.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        assignment = self.assignment()
        menu = Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=500)
        order = save_meal_order(registration=assignment.registration, menu=menu)
        response = self.client.post(self.member_url(), self.member_data(first_name="Refusé"))
        self.assertContains(response, "Retirez les affectations officielles à venir")
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.member.first_name, "Lina")
        self.assertTrue(self.member.is_competition_official)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertEqual(order.total_surcharge_cents, 0)

    def test_late_linked_account_validation_rolls_back_identity_and_repricing(self):
        self.linked_participant()
        order = self.meal_order()
        original_save = User.save

        def reject_linked_save(user, *args, **kwargs):
            if user.pk == self.family_user.pk and kwargs.get("update_fields") == ["is_competition_official"]:
                raise ValidationError("Le compte lié a changé. Actualisez cette page.")
            return original_save(user, *args, **kwargs)

        with patch.object(User, "save", reject_linked_save):
            response = self.client.post(self.member_url(), self.member_data(
                first_name="Refusé", is_competition_official="on",
            ))
        self.assertContains(response, "Le compte lié a changé.")
        self.member.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(self.member.first_name, "Lina")
        self.assertFalse(self.member.is_competition_official)
        self.assertEqual(order.total_surcharge_cents, 650)
        self.assertFalse(order.official_exemption_applied)

    def test_family_coordinates_edit_preserves_account_link_password_and_rights(self):
        old_password = self.family_user.password
        response = self.client.post(self.family_url(), self.family_data(
            family_name="Nouvelle famille", first_name="Céline", last_name="Nouveau",
            email="  CELINE@EXAMPLE.TEST  ", phone="0600112233",
            account=str(self.admin.pk), is_superuser="on", is_staff="on", is_organizer="on",
            is_competition_official="on", is_active="", password="Nouveau-secret",
        ))
        self.assertRedirects(response, reverse("administrator_family_detail", args=[self.family.pk]))
        self.family.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertEqual(self.family.name, "Nouvelle famille")
        self.assertEqual(self.family.account_id, self.family_user.pk)
        self.assertEqual((self.family_user.first_name, self.family_user.last_name,
                          self.family_user.email, self.family_user.phone),
                         ("Céline", "Nouveau", "celine@example.test", "0600112233"))
        self.assertEqual(self.family_user.password, old_password)
        self.assertFalse(self.family_user.is_superuser)
        self.assertFalse(self.family_user.is_staff)
        self.assertFalse(self.family_user.is_organizer)
        self.assertFalse(self.family_user.is_competition_official)
        self.assertTrue(self.family_user.is_active)

    def test_duplicate_family_email_returns_field_error_without_partial_edit(self):
        response = self.client.post(self.family_url(), self.family_data(
            family_name="Refusée", first_name="Refusé", email=self.other_user.email.upper(),
        ))
        self.assertContains(response, "Cette adresse e-mail est déjà utilisée.")
        self.assertIn("email", response.context["form"].errors)
        self.family.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertEqual(self.family.name, "Durand")
        self.assertEqual(self.family_user.first_name, "Alice")
        self.assertEqual(self.family_user.email, "famille@example.test")

    def test_concurrent_email_collision_rolls_back_account_and_family(self):
        def concurrent_collision(form):
            User.objects.filter(pk=form.family.account_id).update(first_name="Écriture partielle")
            Family.objects.filter(pk=form.family.pk).update(name="Écriture partielle")
            raise IntegrityError("duplicate email")

        with patch.object(AdministratorFamilyAccountForm, "save", concurrent_collision):
            response = self.client.post(self.family_url(), self.family_data(
                family_name="Refusée", first_name="Refusé", email="libre@example.test",
            ))
        self.assertContains(response, "Cette adresse e-mail est déjà utilisée.")
        self.family.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertEqual(self.family.name, "Durand")
        self.assertEqual(self.family_user.first_name, "Alice")
        self.assertEqual(self.family_user.email, "famille@example.test")

    def test_rights_are_rechecked_under_lock_for_both_edit_routes(self):
        for view, url, args, data in (
            (administrator_family_update, self.family_url(), [self.family.pk], self.family_data()),
            (administrator_member_update, self.member_url(), [self.family.pk, self.member.pk], self.member_data()),
        ):
            for changed in ({"is_active": False}, {"is_superuser": False}):
                with self.subTest(view=view.__name__, changed=changed):
                    User.objects.filter(pk=self.admin.pk).update(is_active=True, is_superuser=True)
                    self.admin.refresh_from_db()
                    request = RequestFactory().post(url, data)
                    request.user = self.admin
                    User.objects.filter(pk=self.admin.pk).update(**changed)
                    with self.assertRaises(PermissionDenied):
                        view(request, *args)
