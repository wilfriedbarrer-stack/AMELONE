from datetime import date, timedelta

from django.test import TestCase

from volunteers.capacity import get_slot_capacities
from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import Member, Registration, StationSlot
from volunteers.services import BusinessRuleError, create_assignment

from .factories import DataMixin


class SharedCapacityProfileTests(DataMixin, TestCase):
    def test_reusable_profile_keeps_shared_places_in_its_new_event(self):
        self.morning_slot.capacity = 2
        self.morning_slot.save(update_fields=("capacity",))
        StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        profile = save_event_profile(event=self.event, name="Deux places partagées")
        clone = create_event_from_profile(
            profile=profile, name="Événement issu du modèle",
            event_date=self.day.date + timedelta(days=7),
        )
        day = clone.days.get()
        slots = {slot.period: slot for slot in clone.stations.get(name=self.station.name).slots.all()}
        for index, period in enumerate(("full_day", "morning", "afternoon", "morning")):
            member = Member.objects.create(
                family=self.family, first_name=f"Participant {index}", last_name="Test",
                birth_date=date(1990, 1, 1),
            )
            registration = Registration.objects.create(
                member=member, event_day=day, availability=Registration.Availability.FULL_DAY,
            )
            if index == 3:
                with self.assertRaises(BusinessRuleError):
                    create_assignment(registration=registration, station_slot=slots[period], actor=self.organizer)
            else:
                create_assignment(registration=registration, station_slot=slots[period], actor=self.organizer)
        for snapshot in get_slot_capacities(slots.values()).values():
            self.assertEqual(snapshot.remaining, 0)
            self.assertEqual((snapshot.morning_occupied, snapshot.afternoon_occupied), (2, 2))
        self.assertFalse(self.day.registrations.exists())
        self.assertEqual(self.morning_slot.remaining_capacity, 2)
