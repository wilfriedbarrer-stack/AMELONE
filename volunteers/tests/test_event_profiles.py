from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import Beverage, Event, EventDay, EventProfile, MealOrder, Menu, StationSlot

from .factories import DataMixin


class ReusableEventProfileTests(DataMixin, TestCase):
    def configure_source(self):
        paris = ZoneInfo("Europe/Paris")
        self.event.registration_opens_at = datetime(2027, 2, 20, 9, 0, tzinfo=paris)
        self.event.registration_closes_at = datetime(2027, 2, 27, 18, 0, tzinfo=paris)
        self.event.meal_orders_close_at = datetime(2027, 2, 26, 12, 0, tzinfo=paris)
        self.event.sumup_payment_url = "https://pay.sumup.com/example"
        self.event.save()
        self.menu = Menu.objects.create(
            event_day=self.day,
            name="Végétarien",
            surcharge_cents=300,
            meal_type=Menu.MealType.FULL_MEAL,
        )
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=100)

    def test_profile_reuses_configuration_without_participants_or_orders(self):
        self.configure_source()
        registration = self.registration(note="Note personnelle à ne pas copier")
        MealOrder.objects.create(
            registration=registration, wants_meal=True, menu=self.menu, beverage=self.drink
        )
        profile = save_event_profile(event=self.event, name="Concours type")
        self.assertNotIn("Note personnelle", str(profile.snapshot))
        self.assertNotIn(str(self.member.pk), str(profile.snapshot))
        clone = create_event_from_profile(
            profile=profile, name="Concours avril", event_date=date(2027, 4, 10)
        )
        self.assertEqual(clone.status, Event.Status.DRAFT)
        self.assertEqual(clone.sumup_payment_url, self.event.sumup_payment_url)
        self.assertEqual(clone.location, self.event.location)
        self.assertEqual(clone.general_instructions, self.event.general_instructions)
        day = clone.days.get()
        self.assertEqual(day.date, date(2027, 4, 10))
        self.assertFalse(day.registrations.exists())
        self.assertEqual(day.menus.get().meal_type, Menu.MealType.FULL_MEAL)
        self.assertEqual(day.menus.get().surcharge_cents, 300)
        self.assertEqual(day.beverages.get().name, "Jus")
        station = clone.stations.get(name=self.station.name)
        slot = station.slots.get(period=StationSlot.Period.MORNING)
        self.assertEqual(slot.capacity, 1)
        self.assertEqual(slot.start_time, time(8))
        self.assertFalse(slot.assignments.exists())

    def test_deadlines_keep_local_time_across_daylight_saving_change(self):
        self.configure_source()
        profile = save_event_profile(event=self.event, name="Échéances")
        clone = create_event_from_profile(
            profile=profile, name="Avril", event_date=date(2027, 4, 10)
        )
        self.assertEqual(
            timezone.localtime(clone.registration_opens_at),
            datetime(2027, 4, 2, 9, 0, tzinfo=ZoneInfo("Europe/Paris")),
        )
        self.assertEqual(timezone.localtime(clone.registration_closes_at).hour, 18)
        self.assertEqual(timezone.localtime(clone.meal_orders_close_at).hour, 12)

    def test_profile_preserves_multiple_days_and_snack_menus(self):
        self.configure_source()
        second = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        Menu.objects.create(event_day=second, name="Collation", meal_type=Menu.MealType.SNACK)
        StationSlot.objects.create(
            station=self.station,
            event_day=second,
            period=StationSlot.Period.FULL_DAY,
            capacity=4,
        )
        profile = save_event_profile(event=self.event, name="Week-end")
        clone = create_event_from_profile(
            profile=profile, name="Week-end suivant", event_date=date(2027, 4, 10)
        )
        dates = list(clone.days.values_list("date", flat=True))
        self.assertEqual(dates, [date(2027, 4, 10), date(2027, 4, 11)])
        second_clone = clone.days.get(date=dates[1])
        self.assertEqual(second_clone.menus.get().meal_type, Menu.MealType.SNACK)
        self.assertEqual(second_clone.station_slots.get().period, StationSlot.Period.FULL_DAY)

    def test_invalid_profile_does_not_leave_a_partial_event(self):
        self.configure_source()
        profile = save_event_profile(event=self.event, name="Profil à vérifier")
        profile.snapshot["stations"][0]["slots"][0]["day_offset"] = 400
        event_count = Event.objects.count()
        with self.assertRaises(ValidationError):
            create_event_from_profile(
                profile=profile, name="Invalide", event_date=date(2027, 4, 10)
            )
        self.assertEqual(Event.objects.count(), event_count)

    def test_saving_profile_requires_an_event_date(self):
        self.event.days.all().delete()
        with self.assertRaises(ValidationError):
            save_event_profile(event=self.event, name="Sans date")
        self.assertFalse(EventProfile.objects.exists())


class EventDateAndProfileViewsTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)

    def event_data(self, **changes):
        data = {
            "name": self.event.name,
            "event_date": self.day.date.isoformat(),
            "description": self.event.description,
            "location": self.event.location,
            "general_instructions": self.event.general_instructions,
            "status": self.event.status,
            "registration_opens_at": timezone.localtime(self.event.registration_opens_at).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "registration_closes_at": timezone.localtime(
                self.event.registration_closes_at
            ).strftime("%Y-%m-%dT%H:%M"),
            "meal_orders_close_at": timezone.localtime(self.event.meal_orders_close_at).strftime(
                "%Y-%m-%dT%H:%M"
            ),
        }
        data.update(changes)
        return data

    def test_create_has_one_required_date_without_journees_panel(self):
        url = reverse("event_create")
        response = self.client.get(url)
        self.assertContains(response, "Date de l&#x27;événement")
        self.assertNotContains(response, "<h2>Journées</h2>")
        self.assertNotContains(response, 'name="sumup_payment_url"')
        response = self.client.post(url, self.event_data(name="Nouvel événement"))
        self.assertEqual(response.status_code, 302)
        created = Event.objects.get(name="Nouvel événement")
        self.assertEqual(created.days.get().date, self.day.date)
        data = self.event_data(name="Date manquante")
        data.pop("event_date")
        self.assertEqual(self.client.post(url, data).status_code, 200)
        self.assertFalse(Event.objects.filter(name="Date manquante").exists())

    def test_changing_unregistered_event_date_preserves_related_configuration(self):
        menu = Menu.objects.create(event_day=self.day, name="Menu existant")
        new_date = self.day.date + timedelta(days=2)
        response = self.client.post(
            reverse("event_update", args=[self.event.pk]),
            self.event_data(event_date=new_date.isoformat()),
        )
        self.assertEqual(response.status_code, 302)
        self.day.refresh_from_db()
        self.assertEqual(self.day.date, new_date)
        self.assertEqual(self.event.days.count(), 1)
        self.assertTrue(Menu.objects.filter(pk=menu.pk, event_day=self.day).exists())
        self.assertTrue(
            StationSlot.objects.filter(pk=self.morning_slot.pk, event_day=self.day).exists()
        )

    def test_registered_date_is_protected_and_event_changes_roll_back(self):
        registration = self.registration()
        response = self.client.post(
            reverse("event_update", args=[self.event.pk]),
            self.event_data(name="Ne pas enregistrer", event_date="2027-03-01"),
        )
        self.assertContains(response, "La date ne peut plus être modifiée")
        self.day.refresh_from_db()
        self.event.refresh_from_db()
        self.assertEqual(self.day.date, date(2027, 2, 28))
        self.assertEqual(self.event.name, "Festival test")
        self.assertTrue(self.day.registrations.filter(pk=registration.pk).exists())

    def test_legacy_extra_days_are_kept_even_with_forged_delete_fields(self):
        second = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        response = self.client.post(
            reverse("event_update", args=[self.event.pk]),
            self.event_data(
                name="Modifié",
                **{
                    "days-TOTAL_FORMS": "1",
                    "days-INITIAL_FORMS": "1",
                    "days-0-id": str(second.pk),
                    "days-0-DELETE": "on",
                },
            ),
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(EventDay.objects.filter(pk=second.pk).exists())
        self.assertEqual(self.event.days.count(), 2)

    def test_organizer_can_save_then_reuse_a_profile(self):
        response = self.client.post(
            reverse("event_profile_save", args=[self.event.pk]),
            {"name": "Configuration concours", "description": "Postes et buvette"},
        )
        self.assertRedirects(response, reverse("event_profile_list"))
        profile = EventProfile.objects.get(name="Configuration concours")
        response = self.client.post(
            reverse("event_profile_create", args=[profile.pk]),
            {"name": "Nouveau concours", "event_date": "2027-05-01"},
        )
        created = Event.objects.get(name="Nouveau concours")
        self.assertRedirects(response, reverse("event_update", args=[created.pk]))
        self.assertEqual(created.days.get().date, date(2027, 5, 1))
        self.assertEqual(created.stations.count(), self.event.stations.count())

    def test_family_cannot_read_save_or_reuse_profiles(self):
        profile = save_event_profile(event=self.event, name="Privé organisateur")
        self.client.force_login(self.family_user)
        for name, args in (
            ("event_profile_list", []),
            ("event_profile_save", [self.event.pk]),
            ("event_profile_create", [profile.pk]),
        ):
            self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
