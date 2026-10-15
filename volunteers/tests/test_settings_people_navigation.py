from django.test import TestCase
from django.urls import reverse

from volunteers.models import Family, Member, User

from .factories import DataMixin


class SettingsPeopleNavigationTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("settings-admin@example.test", "Strong-pass-123")
        self.client.force_login(self.admin)

    def account_url(self, account=None):
        return reverse("profile_update", args=[(account or self.family_user).pk])

    def test_settings_link_to_canonical_tools_without_repeating_event_contents(self):
        response = self.client.get(reverse("administration_dashboard"))
        self.assertContains(response, "<h1>Réglages</h1>", html=True)
        for name in ("event_list", "shared_catalogue", "event_profile_list", "branding_update",
                     "payment_settings", "profile_list", "administrator_families"):
            self.assertContains(response, reverse(name))
        self.assertNotContains(response, self.event.name)
        self.assertNotContains(response, "Créer un poste")

    def test_family_record_has_one_contact_editor_and_member_links_without_quick_official_form(self):
        response = self.client.get(reverse("administrator_family_detail", args=[self.family.pk]))
        for section in ("responsable", "membres", "droits"):
            self.assertContains(response, f'id="{section}"')
        self.assertContains(response, reverse("administrator_family_update", args=[self.family.pk]), count=1)
        self.assertContains(response, reverse("administrator_member_update", args=[self.family.pk, self.member.pk]))
        self.assertNotContains(response, 'name="is_competition_official"')
        self.assertNotIn('<form method="post"', response.content.decode().partition('<main id="contenu"')[2])

    def test_account_with_family_has_rights_only_and_a_link_to_responsible_person(self):
        response = self.client.get(self.account_url())
        self.assertSetEqual(set(response.context["form"].fields), {"role", "is_active"})
        self.assertContains(response, reverse("administrator_family_update", args=[self.family.pk]))
        for field in ("email", "first_name", "last_name", "phone", "is_competition_official"):
            self.assertNotContains(response, f'name="{field}"')

    def test_rights_post_ignores_forged_contacts_and_preserves_legacy_official_flag(self):
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=("is_competition_official",))
        before_contacts = (self.family_user.email, self.family_user.first_name,
                           self.family_user.last_name, self.family_user.phone)
        response = self.client.post(self.account_url(), {
            "role": "organizer", "is_active": "on", "email": "forged@example.test",
            "first_name": "Forged", "last_name": "Other", "phone": "000",
            "is_competition_official": "",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.family_user.refresh_from_db()
        self.assertEqual((self.family_user.email, self.family_user.first_name,
                          self.family_user.last_name, self.family_user.phone), before_contacts)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertTrue(self.family_user.is_organizer)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_linked_member_is_authoritative_for_read_only_official_status(self):
        self.member.user = self.family_user
        self.member.save()
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=("is_competition_official",))
        response = self.client.get(self.account_url())
        self.assertContains(response, "Fonction non attribuée")
        self.assertNotContains(response, '<span class="badge badge-success">Officiel de compétition</span>', html=True)
        self.assertContains(response, reverse("administrator_member_update", args=[self.family.pk, self.member.pk]))

    def test_account_without_family_keeps_contact_editor_and_cannot_change_official_flag(self):
        response = self.client.get(self.account_url(self.organizer))
        self.assertSetEqual(set(response.context["form"].fields), {
            "email", "first_name", "last_name", "phone", "is_active", "role",
        })
        response = self.client.post(self.account_url(self.organizer), {
            "email": "independent@example.test", "first_name": "Compte", "last_name": "Libre",
            "phone": "0612345678", "role": "organizer", "is_active": "on",
            "is_competition_official": "on",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.organizer.refresh_from_db()
        self.assertEqual(self.organizer.email, "independent@example.test")
        self.assertFalse(self.organizer.is_competition_official)
        self.assertFalse(Family.objects.filter(account=self.organizer).exists())

    def test_member_editor_is_the_only_visible_official_function_editor(self):
        response = self.client.get(reverse("administrator_member_update", args=[self.family.pk, self.member.pk]))
        self.assertContains(response, 'name="is_competition_official"')
        self.assertContains(response, "Licence et formation à jour nécessaires.")
        self.assertNotContains(response, 'name="role"')
        self.assertNotContains(response, 'name="email"')

    def test_account_rights_preserve_administrator_and_official_cumulation(self):
        family = Family.objects.create(account=self.admin, name="Administration")
        member = Member.objects.create(
            family=family, user=self.admin, first_name="Admin", last_name="Officiel",
            birth_date=self.member.birth_date, is_competition_official=True,
        )
        self.admin.is_competition_official = True
        self.admin.save(update_fields=("is_competition_official",))
        response = self.client.post(self.account_url(self.admin), {"role": "administrator", "is_active": "on"})
        self.assertRedirects(response, reverse("profile_list"))
        self.admin.refresh_from_db()
        member.refresh_from_db()
        self.assertTrue(self.admin.is_superuser)
        self.assertTrue(self.admin.is_competition_official)
        self.assertTrue(member.is_competition_official)

    def test_legacy_quick_official_post_remains_guarded_and_functional(self):
        response = self.client.post(reverse("administrator_family_detail", args=[self.family.pk]), {
            "member_id": str(self.member.pk), "is_competition_official": "on",
        })
        self.assertRedirects(response, reverse("administrator_family_detail", args=[self.family.pk]))
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
