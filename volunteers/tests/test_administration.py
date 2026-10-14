from io import BytesIO

from django.contrib.auth.models import Permission
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from PIL import Image

from volunteers.models import Member, Menu, SiteBranding, User
from volunteers.tests.factories import DataMixin


class AdministrationTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin@example.test", "Strong-pass-123")

    def test_family_cannot_access_administration_or_change_profiles(self):
        self.client.force_login(self.family_user)
        for name in ("administration_dashboard", "profile_list", "branding_update"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.post(reverse("profile_update", args=[self.family_user.pk]), {"role": "administrator"}).status_code, 403)
        self.family_user.refresh_from_db()
        self.assertFalse(self.family_user.is_superuser)

    def test_organizer_can_manage_options_but_not_rights(self):
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("administration_dashboard"))
        self.assertContains(response, "Créer un poste")
        self.assertContains(response, "Créer un menu")
        self.assertNotContains(response, "Gérer les profils et les droits")
        for name in ("profile_list", "branding_update"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)

    def test_admin_can_change_role_and_clear_old_permissions(self):
        self.client.force_login(self.admin)
        self.organizer.user_permissions.add(Permission.objects.get(codename="change_user"))
        response = self.client.post(reverse("profile_update", args=[self.organizer.pk]), {
            "email": self.organizer.email, "role": "family", "is_active": "on",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.organizer.refresh_from_db()
        self.assertFalse(self.organizer.is_organizer)
        self.assertFalse(self.organizer.is_staff)
        self.assertFalse(self.organizer.user_permissions.exists())
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(reverse("family_dashboard")).status_code, 200)

    def test_admin_cannot_remove_own_access(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("profile_update", args=[self.admin.pk]), {
            "email": self.admin.email, "role": "family",
        })
        self.assertContains(response, "Vous ne pouvez pas retirer")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)
        self.assertTrue(self.admin.is_active)

    def test_official_profile_alone_does_not_grant_management_rights(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("profile_update", args=[self.family_user.pk]), {
            "email": self.family_user.email, "role": "family", "is_active": "on",
            "is_competition_official": "on",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.family_user.refresh_from_db()
        self.assertTrue(self.family_user.is_competition_official)
        self.assertFalse(self.family_user.is_superuser)
        self.assertFalse(self.family_user.is_staff)
        self.assertFalse(self.family_user.is_organizer)
        self.assertContains(self.client.get(reverse("profile_list")), "Famille + Officiel de compétition")
        self.client.force_login(self.family_user)
        for name in ("administration_dashboard", "profile_list", "branding_update"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 403)
        self.assertEqual(self.client.post(reverse("profile_update", args=[self.family_user.pk]), {
            "role": "administrator", "is_competition_official": "on",
        }).status_code, 403)

    def test_administrator_can_add_and_remove_own_official_profile(self):
        self.client.force_login(self.admin)
        for official in (True, False):
            with self.subTest(official=official):
                data = {
                    "email": self.admin.email, "role": "administrator", "is_active": "on",
                }
                if official:
                    data["is_competition_official"] = "on"
                response = self.client.post(reverse("profile_update", args=[self.admin.pk]), data)
                self.assertRedirects(response, reverse("profile_list"))
                self.admin.refresh_from_db()
                self.assertEqual(self.admin.is_competition_official, official)
                self.assertTrue(self.admin.is_superuser)
                self.assertTrue(self.admin.is_staff)
                self.assertTrue(self.admin.is_organizer)
                self.assertTrue(self.admin.is_active)
                if official:
                    self.assertContains(
                        self.client.get(reverse("profile_list")),
                        "Administrateur + Officiel de compétition",
                    )

    def test_official_flag_does_not_bypass_own_administrator_protection(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("profile_update", args=[self.admin.pk]), {
            "email": self.admin.email, "role": "family", "is_active": "on",
            "is_competition_official": "on",
        })
        self.assertContains(response, "Vous ne pouvez pas retirer")
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)
        self.assertFalse(self.admin.is_competition_official)

    def test_personal_member_must_belong_to_accounts_own_family(self):
        self.member.user = self.family_user
        self.member.full_clean()
        self.member.save()
        self.assertEqual(self.family_user.participant_profile.pk, self.member.pk)
        self.member.user = self.other_user
        with self.assertRaisesMessage(ValidationError, "Le compte personnel doit être le compte responsable"):
            self.member.full_clean()

    def test_account_can_have_only_one_personal_member(self):
        self.member.user = self.family_user
        self.member.save()
        duplicate = Member(
            family=self.family, user=self.family_user,
            first_name="Alice", last_name="Durand", birth_date=self.member.birth_date,
        )
        with self.assertRaises(ValidationError) as caught:
            duplicate.full_clean()
        self.assertIn("user", caught.exception.message_dict)

    def test_staff_with_user_permission_cannot_escalate_via_django_admin(self):
        self.organizer.user_permissions.add(Permission.objects.get(codename="change_user"))
        self.client.force_login(self.organizer)
        self.assertEqual(self.client.get(reverse("admin:volunteers_user_change", args=[self.family_user.pk])).status_code, 403)

    def test_logo_upload_is_persisted_and_shown_publicly(self):
        stream = BytesIO()
        Image.new("RGB", (12, 12), "green").save(stream, "PNG")
        self.client.force_login(self.admin)
        response = self.client.post(reverse("branding_update"), {
            "logo": SimpleUploadedFile("logo.png", stream.getvalue(), content_type="image/png"),
        })
        self.assertRedirects(response, reverse("administration_dashboard"))
        data = SiteBranding.objects.get(pk=1).logo_data
        self.assertTrue(data.startswith("data:image/png;base64,"))
        self.client.logout()
        self.assertContains(self.client.get(reverse("home")), data)

    def test_fake_image_is_rejected(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("branding_update"), {
            "logo": SimpleUploadedFile("logo.png", b"<script>alert(1)</script>"),
        })
        self.assertContains(response, "Le fichier n’est pas une image valide.")
        self.assertFalse(SiteBranding.objects.exists())

    def test_menu_update_and_deactivation(self):
        menu = Menu.objects.create(event_day=self.day, name="Ancien menu", surcharge_cents=0)
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("menu_update", args=[menu.pk]), {
            "name": "Menu végétarien", "description": "Légumes", "surcharge_euros": "2.50", "meal_type": "full_meal",
        })
        self.assertEqual(response.status_code, 302)
        menu.refresh_from_db()
        self.assertEqual(menu.name, "Menu végétarien")
        self.assertFalse(menu.is_active)

    def test_admin_created_family_account_has_usable_space(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:volunteers_user_add"), {
            "email": "nouveau@example.test", "first_name": "Nouveau", "last_name": "Membre",
            "phone": "", "password1": "Unique-admin-created-123!",
            "password2": "Unique-admin-created-123!", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        account = User.objects.get(email="nouveau@example.test")
        self.client.force_login(account)
        self.assertEqual(self.client.get(reverse("family_dashboard")).status_code, 200)

    def test_admin_email_case_duplicate_returns_form_error(self):
        self.client.force_login(self.admin)
        response = self.client.post(reverse("admin:volunteers_user_add"), {
            "email": self.family_user.email.upper(), "password1": "Unique-admin-created-123!",
            "password2": "Unique-admin-created-123!", "is_active": "on",
        })
        self.assertContains(response, "Cette adresse e-mail est déjà utilisée.")
        self.assertEqual(User.objects.filter(email__iexact=self.family_user.email).count(), 1)
