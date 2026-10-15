from datetime import date
from uuid import uuid4

from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.models import Assignment, Family, Member, Station, User
from volunteers.tests.factories import DataMixin
from volunteers.views_family_administration import administrator_family_detail


class FamilyAdministrationTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("admin-familles@example.test", "Strong-pass-123")
        self.client.force_login(self.admin)

    def family_url(self, family=None):
        return reverse("administrator_family_detail", args=[(family or self.family).pk])

    def set_official(self, member=None, enabled=True, family=None, **data):
        data["member_id"] = str((member or self.member).pk)
        if enabled:
            data["is_competition_official"] = "on"
        return self.client.post(self.family_url(family), data)

    def official_assignment(self, member):
        member.is_competition_official = True
        member.save()
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        return Assignment.objects.create(
            registration=self.registration(member=member), station_slot=self.morning_slot,
        )

    def test_only_administrators_can_read_or_modify_families(self):
        for user in (self.family_user, self.other_user, self.organizer):
            with self.subTest(user=user.email):
                self.client.force_login(user)
                self.assertEqual(self.client.get(reverse("administrator_families")).status_code, 403)
                self.assertEqual(self.client.get(self.family_url()).status_code, 403)
                self.assertEqual(self.set_official().status_code, 403)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_anonymous_visitors_must_sign_in(self):
        self.client.logout()
        for url in (reverse("administrator_families"), self.family_url()):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertIn(reverse("login"), response.url)

    def test_list_and_detail_show_private_family_members_including_archived(self):
        self.member.archived_at = timezone.now()
        self.member.save()
        response = self.client.get(reverse("administrator_families"))
        self.assertContains(response, self.family_user.email)
        self.assertContains(response, "0 actifs / 1 au total")
        response = self.client.get(self.family_url())
        self.assertContains(response, self.member.full_name)
        self.assertContains(response, "29/02/2000")
        self.assertContains(response, f"{self.member.age_on()} ans")
        self.assertContains(response, "Archivé")
        self.assertContains(response, "Licence et formation à jour nécessaires.")
        self.assertNotContains(response, self.other_member.full_name)

    def test_official_function_is_individual_and_does_not_grant_management(self):
        sibling = Member.objects.create(
            family=self.family, first_name="Paul", last_name="Durand", birth_date=date(1995, 1, 1),
        )
        response = self.set_official(role="administrator", is_superuser="on")
        self.assertRedirects(response, self.family_url())
        for member in (self.member, sibling, self.other_member):
            member.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertFalse(sibling.is_competition_official)
        self.assertFalse(self.other_member.is_competition_official)
        self.family_user.refresh_from_db()
        self.assertFalse(self.family_user.is_competition_official)
        self.assertFalse(self.family_user.is_superuser)
        self.assertFalse(self.family_user.is_staff)
        self.assertFalse(self.family_user.is_organizer)
        self.assertRedirects(self.set_official(enabled=False), self.family_url())
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_linked_account_profile_is_synchronized_without_changing_rights(self):
        self.member.user = self.family_user
        self.member.save()
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.assertRedirects(self.set_official(enabled=enabled), self.family_url())
                self.family_user.refresh_from_db()
                self.member.refresh_from_db()
                self.assertEqual(self.family_user.is_competition_official, enabled)
                self.assertEqual(self.member.is_competition_official, enabled)
                self.assertFalse(self.family_user.is_superuser)
                self.assertFalse(self.family_user.is_staff)
                self.assertFalse(self.family_user.is_organizer)

    def test_post_cannot_target_member_outside_selected_family(self):
        self.assertEqual(self.set_official(member=self.other_member).status_code, 404)
        self.other_member.refresh_from_db()
        self.assertFalse(self.other_member.is_competition_official)

    def test_missing_or_malformed_identifiers_cannot_modify_members(self):
        for member_id in ("not-a-uuid", str(uuid4()), ""):
            with self.subTest(member_id=member_id):
                response = self.client.post(self.family_url(), {
                    "member_id": member_id, "is_competition_official": "on",
                })
                self.assertEqual(response.status_code, 404)
        self.assertEqual(self.client.get(reverse("administrator_family_detail", args=[uuid4()])).status_code, 404)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_privileges_are_rechecked_under_lock_before_writing(self):
        for changed in ({"is_superuser": False}, {"is_active": False}):
            with self.subTest(changed=changed):
                User.objects.filter(pk=self.admin.pk).update(is_superuser=True, is_active=True)
                self.admin.refresh_from_db()
                request = RequestFactory().post(self.family_url(), {
                    "member_id": str(self.member.pk), "is_competition_official": "on",
                })
                request.user = self.admin
                User.objects.filter(pk=self.admin.pk).update(**changed)
                with self.assertRaises(PermissionDenied):
                    administrator_family_detail(request, self.family.pk)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_administrator_can_enable_official_for_own_participant(self):
        family = Family.objects.create(account=self.admin, name="Administration")
        participant = Member.objects.create(
            family=family, user=self.admin, first_name="Admin", last_name="Officiel",
            birth_date=date(1980, 6, 2),
        )
        self.assertRedirects(self.set_official(member=participant, family=family), self.family_url(family))
        participant.refresh_from_db()
        self.admin.refresh_from_db()
        self.assertTrue(participant.is_competition_official)
        self.assertTrue(self.admin.is_competition_official)
        self.assertTrue(self.admin.is_superuser)
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_organizer)

    def test_revocation_with_future_official_assignment_is_rejected_without_partial_save(self):
        self.member.user = self.family_user
        self.member.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        assignment = self.official_assignment(self.member)
        response = self.set_official(enabled=False)
        self.assertContains(response, "Retirez les affectations officielles à venir")
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        assignment.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertEqual(assignment.status, Assignment.Status.DRAFT)

    def test_account_editor_cannot_change_personally_linked_member_function(self):
        self.member.user = self.family_user
        self.member.save()
        sibling = Member.objects.create(
            family=self.family, first_name="Paul", last_name="Durand", birth_date=date(1995, 1, 1),
        )
        response = self.client.post(reverse("profile_update", args=[self.family_user.pk]), {
            "email": self.family_user.email, "role": "family", "is_active": "on",
            "is_competition_official": "on",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.member.refresh_from_db()
        sibling.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)
        self.assertFalse(sibling.is_competition_official)

    def test_account_rights_edit_preserves_official_member_and_assignment(self):
        family = Family.objects.create(account=self.organizer)
        member = Member.objects.create(
            family=family, user=self.organizer, first_name="Organisation", last_name="Officiel",
            birth_date=date(1980, 6, 2),
        )
        self.organizer.is_competition_official = True
        self.organizer.save()
        permission = Permission.objects.get(codename="change_user")
        self.organizer.user_permissions.add(permission)
        self.official_assignment(member)
        response = self.client.post(reverse("profile_update", args=[self.organizer.pk]), {
            "email": self.organizer.email, "role": "family", "is_active": "on",
        })
        self.assertRedirects(response, reverse("profile_list"))
        self.organizer.refresh_from_db()
        member.refresh_from_db()
        self.assertTrue(self.organizer.is_competition_official)
        self.assertTrue(member.is_competition_official)
        self.assertFalse(self.organizer.is_staff)
        self.assertFalse(self.organizer.is_organizer)
        self.assertFalse(self.organizer.user_permissions.filter(pk=permission.pk).exists())
