from datetime import date

from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse

from volunteers.models import Assignment, Member, Registration, Station, User
from volunteers.services import confirm_assignments, create_assignment

from .factories import DataMixin


class AdvancedAdminEligibilityTests(DataMixin, TestCase):
    def setUp(self):
        self.administrator = User.objects.create_superuser(
            "advanced-admin@example.test", "Strong-pass-123",
        )
        self.client.force_login(self.administrator)
        self.organizer.user_permissions.add(*Permission.objects.filter(
            content_type__app_label="volunteers",
            codename__in=("change_member", "change_family", "view_member", "view_family"),
        ))

    def confirmed_pair(self):
        minor = Member.objects.create(
            family=self.family, first_name="Mineur", last_name="Encadré",
            birth_date=date(2010, 1, 1),
        )
        self.station.age_policy = Station.AgePolicy.AT_LEAST_ONE_ADULT
        self.station.save()
        self.morning_slot.capacity = 3
        self.morning_slot.save()
        adult_registration = self.registration()
        adult = create_assignment(registration=adult_registration, station_slot=self.morning_slot)
        child = create_assignment(
            registration=self.registration(member=minor), station_slot=self.morning_slot,
        )
        confirm_assignments(assignments=[adult, child], confirmed_by=self.organizer)
        return adult_registration, adult, child

    def member_data(self, **changes):
        return {
            "family": str(self.family.pk), "first_name": self.member.first_name,
            "last_name": self.member.last_name,
            "birth_date": self.member.birth_date.isoformat(), **changes,
        }

    def test_assignment_and_registration_delete_views_preserve_adult_coverage(self):
        registration, adult, child = self.confirmed_pair()
        for name, object_id in (
            ("admin:volunteers_assignment_delete", adult.pk),
            ("admin:volunteers_registration_delete", registration.pk),
        ):
            with self.subTest(name=name):
                response = self.client.post(reverse(name, args=[object_id]), {"post": "yes"})
                self.assertEqual(response.status_code, 403)
                self.assertTrue(Assignment.objects.filter(pk=adult.pk).exists())
                self.assertTrue(Registration.objects.filter(pk=registration.pk).exists())
                child.refresh_from_db()
                self.assertEqual(child.status, Assignment.Status.CONFIRMED)

    def test_bulk_delete_cannot_remove_assignment_or_registration_history(self):
        registration, adult, _ = self.confirmed_pair()
        for name, model, object_id in (
            ("admin:volunteers_assignment_changelist", Assignment, adult.pk),
            ("admin:volunteers_registration_changelist", Registration, registration.pk),
        ):
            with self.subTest(name=name):
                self.client.post(reverse(name), {
                    "action": "delete_selected", "_selected_action": str(object_id),
                    "select_across": "0", "post": "yes",
                })
                self.assertTrue(model.objects.filter(pk=object_id).exists())

    def test_registration_change_ignores_forged_structural_fields(self):
        registration = self.registration()
        response = self.client.post(
            reverse("admin:volunteers_registration_change", args=[registration.pk]),
            {
                "member": str(self.other_member.pk), "event_day": str(self.day.pk),
                "availability": Registration.Availability.AFTERNOON,
                "preferred_station": str(self.other_station.pk),
                "status": Registration.Status.CANCELLED, "note": "Remarque autorisée",
                "assignments-TOTAL_FORMS": "0", "assignments-INITIAL_FORMS": "0",
            },
        )
        self.assertEqual(response.status_code, 302)
        registration.refresh_from_db()
        self.assertEqual(registration.member_id, self.member.pk)
        self.assertEqual(registration.availability, Registration.Availability.FULL_DAY)
        self.assertIsNone(registration.preferred_station_id)
        self.assertEqual(registration.status, Registration.Status.ACTIVE)
        self.assertEqual(registration.note, "Remarque autorisée")
        response = self.client.get(reverse("admin:volunteers_registration_change", args=[registration.pk]))
        self.assertContains(response, reverse("registration_update", args=[registration.pk]))

    def test_assignment_inline_does_not_offer_deletion(self):
        registration, adult, _ = self.confirmed_pair()
        response = self.client.get(reverse("admin:volunteers_registration_change", args=[registration.pk]))
        self.assertNotContains(response, 'name="assignments-0-DELETE"')
        self.assertContains(response, str(adult.pk))

    def test_member_official_and_account_fields_ignore_staff_forgery(self):
        self.client.force_login(self.organizer)
        response = self.client.post(
            reverse("admin:volunteers_member_change", args=[self.member.pk]),
            self.member_data(is_competition_official="on", user=str(self.family_user.pk)),
        )
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)
        self.assertIsNone(self.member.user_id)

    def test_family_inline_cannot_grant_official_status_or_link_an_account(self):
        self.client.force_login(self.organizer)
        response = self.client.post(reverse("admin:volunteers_family_change", args=[self.family.pk]), {
            "account": str(self.family_user.pk), "name": self.family.name,
            "members-TOTAL_FORMS": "1", "members-INITIAL_FORMS": "1",
            "members-0-id": str(self.member.pk), "members-0-family": str(self.family.pk),
            "members-0-first_name": "Prénom corrigé",
            "members-0-last_name": self.member.last_name,
            "members-0-birth_date": self.member.birth_date.isoformat(),
            "members-0-is_competition_official": "on", "members-0-user": str(self.family_user.pk),
        })
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.first_name, "Prénom corrigé")
        self.assertFalse(self.member.is_competition_official)
        self.assertIsNone(self.member.user_id)

    def test_superuser_member_edit_synchronizes_only_linked_account_official_flag(self):
        self.member.user = self.family_user
        self.member.save()
        response = self.client.post(
            reverse("admin:volunteers_member_change", args=[self.member.pk]),
            self.member_data(is_competition_official="on"),
        )
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertFalse(self.family_user.is_staff)
        self.assertFalse(self.family_user.is_superuser)

    def test_user_admin_cannot_override_linked_members_individual_permission(self):
        self.member.user = self.family_user
        self.member.is_competition_official = True
        self.member.save()
        # An old account-level flag is aligned with its authoritative participant.
        self.assertFalse(self.family_user.is_competition_official)
        url = reverse("admin:volunteers_user_change", args=[self.family_user.pk])
        response = self.client.post(url, {
            "email": self.family_user.email, "first_name": self.family_user.first_name,
            "last_name": self.family_user.last_name, "phone": self.family_user.phone,
            "is_active": "on", "is_competition_official": "",
        })
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertContains(self.client.get(url), reverse("administrator_family_detail", args=[self.family.pk]))

    def test_failed_member_official_revocation_preserves_linked_account_flag(self):
        self.member.user = self.family_user
        self.member.is_competition_official = True
        self.member.save()
        self.family_user.is_competition_official = True
        self.family_user.save()
        self.station.category = Station.Category.OFFICIAL
        self.station.save()
        assignment = create_assignment(
            registration=self.registration(), station_slot=self.morning_slot,
        )

        response = self.client.post(
            reverse("admin:volunteers_member_change", args=[self.member.pk]), self.member_data(),
        )
        self.assertContains(response, "Retirez les affectations officielles")
        self.member.refresh_from_db()
        self.family_user.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)
        self.assertTrue(self.family_user.is_competition_official)
        self.assertTrue(Assignment.objects.filter(pk=assignment.pk).exists())

    def test_registered_event_date_cannot_be_changed_in_admin(self):
        self.registration()
        response = self.client.post(reverse("admin:volunteers_eventday_change", args=[self.day.pk]), {
            "event": str(self.event.pk), "date": "2020-01-01",
        })
        self.assertEqual(response.status_code, 302)
        self.day.refresh_from_db()
        self.assertEqual(self.day.date, date(2027, 2, 28))

