from datetime import date

from django.test import TestCase
from django.urls import reverse

from volunteers.event_profiles import (
    build_event_snapshot,
    create_event_from_profile,
    save_event_profile,
)
from volunteers.forms_family import FamilyRegistrationForm, MemberForm
from volunteers.forms_self import PersonalRegistrationForm
from volunteers.models import EventProfile, Member, Station, StationSlot

from .factories import DataMixin


class EligibilityFormTests(DataMixin, TestCase):
    def setUp(self):
        self.official_station = Station.objects.create(
            event=self.event, name="Jury officiel", category=Station.Category.OFFICIAL,
        )
        self.adults_station = Station.objects.create(
            event=self.event, name="Adultes uniquement", age_policy=Station.AgePolicy.ADULTS_ONLY,
        )
        self.minor = Member.objects.create(
            family=self.family, first_name="Jeune", last_name="Durand", birth_date=date(2010, 3, 1),
        )
        self.official_slot = StationSlot.objects.create(
            station=self.official_station, event_day=self.day, period=StationSlot.Period.MORNING,
            capacity=3,
        )

    def choices(self, member):
        form = FamilyRegistrationForm(event_day=self.day, member=member)
        return form.fields["preferred_station"].queryset

    def test_member_age_and_individual_role_filter_family_post_choices(self):
        self.assertNotIn(self.official_station, self.choices(self.minor))
        self.assertNotIn(self.adults_station, self.choices(self.minor))
        self.assertIn(self.station, self.choices(self.minor))
        self.assertIn(self.adults_station, self.choices(self.member))
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        self.assertIn(self.official_station, self.choices(self.member))
        self.assertNotIn(self.official_station, self.choices(self.minor))

    def test_forged_official_preference_is_rejected_even_for_legacy_official_account(self):
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=["is_competition_official"])
        form = FamilyRegistrationForm({
            "availability": "full_day", "preferred_station": self.official_station.pk,
            "no_meal": "on", "payment_method": "counter",
        }, event_day=self.day, member=self.member)
        self.assertFalse(form.is_valid())
        self.assertIn("preferred_station", form.errors)

    def test_administrator_personal_choices_require_own_member_authorization(self):
        self.family_user.is_superuser = True
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=["is_superuser", "is_competition_official"])
        self.member.user = self.family_user
        self.member.save(update_fields=["user"])
        form = PersonalRegistrationForm(user=self.family_user, event_day=self.day, member=self.member)
        self.assertNotIn(self.official_slot, form.fields["station_slots"].queryset)
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        form = PersonalRegistrationForm(user=self.family_user, event_day=self.day, member=self.member)
        self.assertIn(self.official_slot, form.fields["station_slots"].queryset)
        self.assertIn("Licence et formation à jour nécessaires.",
                      form.fields["station_slots"].label_from_instance(
                          form.fields["station_slots"].queryset.get(pk=self.official_slot.pk)))

    def test_family_identity_edit_neither_grants_nor_overwrites_official_permission(self):
        form = MemberForm({"first_name": "Lina", "last_name": "Durand", "birth_date": "2000-02-29",
                           "is_competition_official": "on"}, instance=self.member)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)
        stale_form = MemberForm({"first_name": "Lina", "last_name": "Durand", "birth_date": "2000-02-29"}, instance=self.member)
        Member.objects.filter(pk=self.member.pk).update(is_competition_official=True)
        self.assertTrue(stale_form.is_valid(), stale_form.errors)
        stale_form.save()
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_competition_official)

    def test_new_personal_profile_does_not_self_grant_an_official_role(self):
        self.family_user.is_competition_official = True
        self.family_user.save(update_fields=["is_competition_official"])
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("personal_profile"), {
            "existing_member": self.member.pk, "is_competition_official": "on",
        })
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_competition_official)

    def test_reusable_profile_preserves_age_policy_and_old_profiles_remain_usable(self):
        self.station.age_policy = Station.AgePolicy.AT_LEAST_ONE_ADULT
        self.station.save(update_fields=["age_policy"])
        profile = save_event_profile(event=self.event, name="Avec encadrement")
        clone = create_event_from_profile(profile=profile, name="Épreuve avec encadrement", event_date=date(2028, 3, 1))
        self.assertEqual(clone.stations.get(name=self.station.name).age_policy,
                         Station.AgePolicy.AT_LEAST_ONE_ADULT)
        snapshot = build_event_snapshot(self.event)
        for station in snapshot["stations"]:
            station.pop("age_policy")
        old_profile = EventProfile.objects.create(name="Ancien profil", snapshot=snapshot)
        old_clone = create_event_from_profile(profile=old_profile, name="Ancienne épreuve", event_date=date(2028, 3, 2))
        self.assertEqual(old_clone.stations.get(name=self.station.name).age_policy, Station.AgePolicy.NONE)
