from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from volunteers.models import User
from volunteers.views_administration import ProfileForm

from .factories import DataMixin


class AccountProfileEditorTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin-edit@example.test", "Strong-pass-123")
        self.client.force_login(self.admin)
        self.url = reverse("profile_update", args=[self.family_user.pk])

    def data(self, **overrides):
        data = {
            "email": "nouveau-contact@example.test", "first_name": "Anne",
            "last_name": "Martin", "phone": "0611223344",
            "role": "family", "is_active": "on",
        }
        data.update(overrides)
        return data

    def test_edit_account_contacts_preserves_password_family_and_member(self):
        password_hash = self.family_user.password
        self.assertRedirects(self.client.post(self.url, self.data()), reverse("profile_list"))
        self.family_user.refresh_from_db()
        self.family.refresh_from_db()
        self.member.refresh_from_db()
        self.assertEqual(self.family_user.email, "nouveau-contact@example.test")
        self.assertEqual(self.family_user.get_full_name(), "Anne Martin")
        self.assertEqual(self.family_user.phone, "0611223344")
        self.assertEqual(self.family_user.password, password_hash)
        self.assertEqual(self.family.account_id, self.family_user.pk)
        self.assertEqual(self.member.family_id, self.family.pk)
        self.assertFalse(self.family_user.is_superuser)

    def test_duplicate_email_does_not_partially_change_identity_or_rights(self):
        response = self.client.post(self.url, self.data(
            email=self.other_user.email.upper(), role="administrator",
        ))
        self.assertContains(response, "Cette adresse e-mail est déjà utilisée.")
        self.family_user.refresh_from_db()
        self.assertEqual(self.family_user.email, "famille@example.test")
        self.assertEqual(self.family_user.first_name, "Alice")
        self.assertFalse(self.family_user.is_superuser)

    def test_email_claimed_after_validation_returns_error_and_rolls_back_rights(self):
        original_is_valid = ProfileForm.is_valid

        def claim_email(form):
            valid = original_is_valid(form)
            if valid:
                User.objects.create_user(email=form.cleaned_data["email"])
            return valid

        with patch.object(ProfileForm, "is_valid", claim_email):
            response = self.client.post(self.url, self.data(role="administrator"))
        self.assertContains(response, "Cette adresse e-mail est déjà utilisée.")
        self.family_user.refresh_from_db()
        self.assertEqual(self.family_user.email, "famille@example.test")
        self.assertEqual(self.family_user.first_name, "Alice")
        self.assertFalse(self.family_user.is_superuser)
