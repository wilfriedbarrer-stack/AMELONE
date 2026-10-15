from django.test import TestCase
from django.urls import reverse

from volunteers.event_profiles import save_event_profile
from volunteers.models import Event

from .factories import DataMixin


class EventProfileSelectionTests(DataMixin, TestCase):
    def setUp(self):
        self.profile = save_event_profile(event=self.event, name="CSO avec postes")
        self.client.force_login(self.organizer)
        self.url = reverse("event_create")

    def data(self, **changes):
        data = {
            "action": "create_from_profile", "profile-profile": str(self.profile.pk),
            "profile-name": "CSO de novembre", "profile-event_date": "2027-11-08",
        }
        data.update(changes)
        return data

    def test_creation_page_offers_profile_selection_alongside_blank_event(self):
        response = self.client.get(self.url)
        self.assertContains(response, 'name="profile-profile"')
        self.assertContains(response, self.profile.name)
        self.assertContains(response, 'name="profile-event_date"')
        self.assertContains(response, 'name="registration_opens_at"')
        self.assertIsNone(response.context["event"])

    def test_selected_profile_copies_posts_into_a_new_independent_draft(self):
        self.registration()
        response = self.client.post(self.url, self.data(status="published"))
        event = Event.objects.get(name="CSO de novembre")
        self.assertRedirects(response, reverse("event_update", args=[event.pk]))
        self.assertEqual(event.status, Event.Status.DRAFT)
        self.assertEqual(event.days.get().date.isoformat(), "2027-11-08")
        station = event.stations.get(name=self.station.name)
        slot = station.slots.get(period=self.morning_slot.period)
        self.assertEqual(slot.capacity, self.morning_slot.capacity)
        self.assertEqual(slot.start_time, self.morning_slot.start_time)
        self.assertNotEqual(station.pk, self.station.pk)
        self.assertFalse(event.days.get().registrations.exists())
        self.assertFalse(slot.assignments.exists())
        self.assertNotContains(self.client.get(reverse("event_update", args=[event.pk])), 'name="profile-profile"')

    def test_invalid_profile_or_date_never_creates_partial_event(self):
        count = Event.objects.count()
        for changes in ({"profile-profile": "invalid"}, {"profile-event_date": "invalid"}):
            with self.subTest(changes=changes):
                response = self.client.post(self.url, self.data(**changes))
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.context["profile_creation_form"].errors)
                self.assertFalse(response.context["form"].is_bound)
                self.assertEqual(Event.objects.count(), count)

    def test_family_cannot_create_event_with_profile(self):
        self.client.force_login(self.family_user)
        count = Event.objects.count()
        self.assertEqual(self.client.post(self.url, self.data()).status_code, 403)
        self.assertEqual(Event.objects.count(), count)
