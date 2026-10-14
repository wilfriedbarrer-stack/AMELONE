from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.models import Event, EventDay, MealOrder, Menu, Registration
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



class EventAccessibilityTests(DataMixin, TestCase):
    def test_family_can_consult_a_published_event_before_registration_opens(self):
        now = timezone.now()
        self.event.registration_opens_at = now + timedelta(days=3)
        self.event.registration_closes_at = now + timedelta(days=4)
        self.event.save()
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("open_events"))
        self.assertContains(response, self.event.name)
        self.assertContains(response, "Inscriptions à venir")
        self.assertContains(response, reverse("family_event_detail", args=[self.event.pk]))
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, "Inscriptions à partir du")
        self.assertContains(response, timezone.localtime(self.event.registration_opens_at).strftime("%d/%m/%Y à %H:%M"))
        self.assertNotContains(response, reverse("family_registration_edit", args=[self.day.pk, self.member.pk]))

    def test_consultable_event_still_rejects_premature_registration(self):
        now = timezone.now()
        self.event.registration_opens_at = now + timedelta(days=3)
        self.event.registration_closes_at = now + timedelta(days=4)
        self.event.save()
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]), {
            "availability": "morning", "actor_is_organizer": "true", "is_organizer": "true",
        })
        self.assertContains(response, "Les inscriptions ne sont pas ouvertes")
        self.assertFalse(Registration.objects.filter(member=self.member, event_day=self.day).exists())

    def test_closed_event_remains_consultable(self):
        now = timezone.now()
        self.event.registration_opens_at = now - timedelta(days=3)
        self.event.registration_closes_at = now - timedelta(days=1)
        self.event.save()
        registration = self.registration()
        self.client.force_login(self.family_user)
        self.assertContains(self.client.get(reverse("open_events")), self.event.name)
        response = self.client.get(reverse("family_event_detail", args=[self.event.pk]))
        self.assertContains(response, "Inscriptions clôturées")
        self.assertNotContains(response, reverse("family_registration_edit", args=[self.day.pk, self.member.pk]))
        self.assertNotContains(response, reverse("family_registration_cancel", args=[registration.pk]))

    def test_unpublished_event_is_hidden_from_family(self):
        self.client.force_login(self.family_user)
        for status in (Event.Status.DRAFT, Event.Status.ARCHIVED):
            self.event.status = status
            self.event.save()
            self.assertNotContains(self.client.get(reverse("open_events")), self.event.name)
            self.assertEqual(self.client.get(reverse("family_event_detail", args=[self.event.pk])).status_code, 404)

    def test_public_event_filter_uses_generic_route_and_resets_day(self):
        second = Event.objects.create(
            name="Autre rencontre", location="Centre équestre", status=Event.Status.PUBLISHED,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        second_day = EventDay.objects.create(event=second, date=self.day.date + timedelta(days=1))
        response = self.client.get(reverse("public_planning", args=[self.event.pk]))
        # Changing events submits only the new event, so an old event/day cannot stay selected.
        self.assertContains(response, 'action="' + reverse("public_planning") + '"')
        event_form = response.content.decode().split('</form>', 1)[0]
        self.assertNotIn('name="day"', event_form)
        response = self.client.get(reverse("public_planning"), {"event": second.pk})
        self.assertEqual(response.context["event"].pk, second.pk)
        self.assertEqual(response.context["selected_day"].pk, second_day.pk)
