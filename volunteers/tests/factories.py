from datetime import date, time, timedelta
from zoneinfo import ZoneInfo

from django.utils import timezone

from volunteers.models import (
    Event,
    EventDay,
    Family,
    Member,
    Registration,
    Station,
    StationSlot,
    User,
)

PARIS = ZoneInfo("Europe/Paris")


class DataMixin:
    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.organizer = User.objects.create_user(
            email="orga@example.test", password="Strong-pass-123", is_organizer=True, is_staff=True
        )
        cls.family_user = User.objects.create_user(
            email="famille@example.test",
            password="Strong-pass-123",
            first_name="Alice",
            last_name="Durand",
            phone="0601020304",
        )
        cls.family = Family.objects.create(account=cls.family_user, name="Durand")
        cls.member = Member.objects.create(
            family=cls.family,
            first_name="Lina",
            last_name="Durand",
            birth_date=date(2000, 2, 29),
        )
        cls.other_user = User.objects.create_user(
            email="autre@example.test", password="Strong-pass-123", first_name="Autre"
        )
        cls.other_family = Family.objects.create(account=cls.other_user, name="Autre")
        cls.other_member = Member.objects.create(
            family=cls.other_family,
            first_name="Noé",
            last_name="Autre",
            birth_date=date(1990, 5, 1),
        )
        cls.event = Event.objects.create(
            name="Festival test",
            description="Description publique",
            location="Gymnase",
            registration_opens_at=now - timedelta(days=2),
            registration_closes_at=now + timedelta(days=2),
            meal_orders_close_at=now + timedelta(days=1),
            status=Event.Status.PUBLISHED,
            general_instructions="Consignes test",
        )
        cls.day = EventDay.objects.create(event=cls.event, date=date(2027, 2, 28))
        cls.station = Station.objects.create(event=cls.event, name="Accueil")
        cls.other_station = Station.objects.create(event=cls.event, name="Buvette")
        cls.morning_slot = StationSlot.objects.create(
            station=cls.station,
            event_day=cls.day,
            period=StationSlot.Period.MORNING,
            capacity=1,
            start_time=time(8, 0),
            end_time=time(12, 30),
        )
        cls.afternoon_slot = StationSlot.objects.create(
            station=cls.station,
            event_day=cls.day,
            period=StationSlot.Period.AFTERNOON,
            capacity=2,
            start_time=time(13, 30),
            end_time=time(18, 0),
        )

    def registration(self, member=None, availability=Registration.Availability.FULL_DAY, **kwargs):
        return Registration.objects.create(
            member=member or self.member,
            event_day=self.day,
            availability=availability,
            **kwargs,
        )

