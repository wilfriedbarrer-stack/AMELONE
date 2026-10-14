from django.test import TestCase
from django.urls import reverse

from volunteers.models import Event, MealOrder, Menu, Registration
from volunteers.services import confirm_assignment, create_assignment

from .factories import DataMixin


class AuthorizationAndPublicPrivacyTests(DataMixin, TestCase):
    def test_family_cannot_open_another_family_member(self):
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("member_update", args=[self.other_member.pk]))
        self.assertEqual(response.status_code, 404)

    def test_family_cannot_open_organizer_space(self):
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("organizer_dashboard"))
        self.assertEqual(response.status_code, 403)

    def test_public_planning_only_contains_confirmed_names_and_no_private_data(self):
        registration = self.registration(note="Remarque très privée")
        assignment = create_assignment(
            registration=registration, station_slot=self.morning_slot, actor=self.organizer
        )
        response = self.client.get(reverse("public_planning", args=[self.event.pk]))
        self.assertNotContains(response, self.member.full_name)
        confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        menu = Menu.objects.create(event_day=self.day, name="Menu secret", surcharge_cents=500)
        MealOrder.objects.create(registration=registration, wants_meal=True, menu=menu)
        response = self.client.get(reverse("public_planning", args=[self.event.pk]))
        self.assertContains(response, self.member.full_name)
        for private_value in (
            self.family_user.email,
            self.family_user.phone,
            self.member.birth_date.isoformat(),
            registration.note,
            menu.name,
        ):
            self.assertNotContains(response, private_value)

    def test_draft_event_is_not_public(self):
        self.event.status = Event.Status.DRAFT
        self.event.save(update_fields=["status"])
        response = self.client.get(reverse("public_events"))
        self.assertNotContains(response, self.event.name)
        response = self.client.get(reverse("public_planning", args=[self.event.pk]))
        self.assertEqual(response.status_code, 404)

    def test_family_event_endpoint_does_not_leak_other_family_registration(self):
        own = self.registration(note="Note famille Durand")
        Registration.objects.create(
            member=self.other_member,
            event_day=self.day,
            availability=Registration.Availability.MORNING,
            note="Secret autre famille",
        )
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, own.member.full_name)
        self.assertNotContains(response, "Secret autre famille")
        self.assertNotContains(response, self.other_member.full_name)

