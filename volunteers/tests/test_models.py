from datetime import date

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from volunteers.models import Assignment, Registration, StationSlot

from .factories import DataMixin


class MemberAndConstraintTests(DataMixin, TestCase):
    def test_february_29_rule_and_age(self):
        self.assertEqual(self.member.birthday_in_year(2027), date(2027, 2, 28))
        self.assertFalse(self.member.has_birthday_on(date(2027, 2, 27)))
        self.assertTrue(self.member.has_birthday_on(date(2027, 2, 28)))
        self.assertEqual(self.member.age_on(date(2027, 2, 27)), 26)
        self.assertEqual(self.member.age_on(date(2027, 2, 28)), 27)

    def test_registration_unique_per_member_and_day(self):
        self.registration()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.registration()

    def test_assignment_model_rejects_period_outside_availability(self):
        registration = self.registration(availability=Registration.Availability.MORNING)
        assignment = Assignment(registration=registration, station_slot=self.afternoon_slot)
        with self.assertRaises(ValidationError):
            assignment.save()

    def test_assignment_model_rejects_time_overlap_across_periods(self):
        registration = self.registration()
        Assignment.objects.create(registration=registration, station_slot=self.morning_slot)
        overlapping = StationSlot.objects.create(
            station=self.other_station,
            event_day=self.day,
            period=StationSlot.Period.AFTERNOON,
            capacity=2,
            start_time=self.morning_slot.end_time.replace(hour=11),
            end_time=self.afternoon_slot.end_time.replace(hour=14),
        )
        with self.assertRaises(ValidationError):
            Assignment.objects.create(registration=registration, station_slot=overlapping)

