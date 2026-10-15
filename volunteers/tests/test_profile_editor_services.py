from copy import deepcopy
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from volunteers.event_profiles import create_event_from_profile, save_event_profile
from volunteers.models import (
    Beverage,
    Event,
    EventDay,
    EventProfile,
    MealOrder,
    Menu,
    Registration,
    Station,
    StationSlot,
)
from volunteers.profile_editor import (
    create_reusable_profile,
    delete_profile_station,
    save_profile_station,
    update_reusable_profile,
)

from .factories import DataMixin


class ProfileEditorServicesTests(DataMixin, TestCase):
    def profile(self, **changes):
        data = {"name": "Concours réutilisable", "location": "Centre équestre"}
        data.update(changes)
        return create_reusable_profile(**data)

    def save_station(self, profile, **changes):
        data = {
            "profile": profile,
            "expected_updated_at": profile.updated_at.isoformat(),
            "station_data": {
                "name": "Piste", "category": Station.Category.OFFICIAL,
                "age_policy": Station.AgePolicy.ADULTS_ONLY,
            },
            "slots": [{
                "day_offset": 0, "period": StationSlot.Period.FULL_DAY, "capacity": 2,
                "start_time": time(8, 30), "end_time": "18:00",
            }],
        }
        data.update(changes)
        return save_profile_station(**data)

    def metadata(self, profile, **changes):
        data = {
            "profile": profile, "expected_updated_at": profile.updated_at.isoformat(),
            "name": "Concours renommé", "description": "Modèle de compétition",
            "event_description": "Description du concours", "location": "Nouveau lieu",
            "general_instructions": "Se présenter trente minutes avant.",
        }
        data.update(changes)
        return update_reusable_profile(**data)

    def test_create_independent_profile_does_not_create_an_event_or_stations(self):
        counts = (Event.objects.count(), EventDay.objects.count(), Station.objects.count())
        profile = create_reusable_profile(name=" Nouveau profil ")
        self.assertEqual(profile.name, "Nouveau profil")
        self.assertEqual(profile.snapshot["version"], 1)
        self.assertEqual(profile.snapshot["days"], [{"offset": 0, "menus": [], "beverages": []}])
        self.assertEqual(profile.snapshot["stations"], [])
        self.assertEqual(profile.snapshot["windows"], {
            "registration_opens_at": -30 * 86400 + 9 * 3600,
            "registration_closes_at": -86400 + 18 * 3600,
            "meal_orders_close_at": -86400 + 12 * 3600,
        })
        self.assertEqual(counts, (Event.objects.count(), EventDay.objects.count(), Station.objects.count()))

    def test_create_metadata_validates_names_and_limits_without_partial_rows(self):
        for changes in ({"name": " "}, {"name": "n" * 201}, {"location": "l" * 256}, {"name": 12}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.profile(**changes)
        self.assertFalse(EventProfile.objects.exists())
        self.profile()
        with self.assertRaises(ValidationError):
            self.profile()
        self.assertEqual(EventProfile.objects.count(), 1)

    def test_station_configuration_can_be_reused_as_a_draft_without_participants(self):
        profile = self.save_station(self.profile())
        config = profile.snapshot["stations"][0]
        self.assertEqual(config["slots"][0]["start_time"], "08:30:00")
        self.assertEqual(config["slots"][0]["end_time"], "18:00:00")
        self.assertEqual(config["age_policy"], Station.AgePolicy.ADULTS_ONLY)
        event = create_event_from_profile(profile=profile, name="Concours du printemps", event_date=date(2027, 4, 1))
        self.assertEqual(event.status, Event.Status.DRAFT)
        self.assertEqual(event.location, "Centre équestre")
        self.assertEqual(event.days.get().date, date(2027, 4, 1))
        slot = event.stations.get().slots.get()
        self.assertEqual(slot.capacity, 2)
        self.assertEqual(slot.period, StationSlot.Period.FULL_DAY)
        self.assertEqual(slot.start_time, time(8, 30))
        self.assertEqual(slot.station.category, Station.Category.OFFICIAL)
        self.assertFalse(Registration.objects.filter(event_day__event=event).exists())
        self.assertFalse(MealOrder.objects.filter(registration__event_day__event=event).exists())
        paris = ZoneInfo("Europe/Paris")
        self.assertEqual(timezone.localtime(event.registration_opens_at, paris), datetime(2027, 3, 2, 9, tzinfo=paris))
        self.assertEqual(timezone.localtime(event.registration_closes_at, paris), datetime(2027, 3, 31, 18, tzinfo=paris))
        self.assertEqual(timezone.localtime(event.meal_orders_close_at, paris), datetime(2027, 3, 31, 12, tzinfo=paris))

    def test_metadata_edit_preserves_configuration_and_does_not_modify_existing_events(self):
        profile = self.save_station(self.profile())
        existing = create_event_from_profile(profile=profile, name="Déjà créé", event_date=date(2027, 4, 1))
        original = deepcopy(profile.snapshot)
        updated = self.metadata(profile)
        self.assertEqual(updated.snapshot["stations"], original["stations"])
        self.assertEqual(updated.snapshot["days"], original["days"])
        self.assertEqual(updated.snapshot["windows"], original["windows"])
        self.assertEqual(updated.snapshot["event_name"], original["event_name"])
        self.assertEqual(updated.name, "Concours renommé")
        self.assertEqual(updated.snapshot["event"]["location"], "Nouveau lieu")
        existing.refresh_from_db()
        self.assertEqual(existing.location, "Centre équestre")
        self.assertEqual(existing.description, "")
        self.assertEqual(existing.stations.get().name, "Piste")
        self.assertEqual(Event.objects.filter(name="Déjà créé").count(), 1)

    def test_station_replace_and_delete_only_change_the_profile(self):
        profile = self.save_station(self.profile())
        existing = create_event_from_profile(profile=profile, name="Déjà créé", event_date=date(2027, 4, 1))
        updated = self.save_station(
            profile, station_index=0,
            station_data={"name": "Buvette", "position": 3, "is_active": False}, slots=[],
        )
        self.assertEqual(len(updated.snapshot["stations"]), 1)
        self.assertEqual(updated.snapshot["stations"][0]["name"], "Buvette")
        self.assertFalse(updated.snapshot["stations"][0]["is_active"])
        self.assertEqual(updated.snapshot["stations"][0]["slots"], [])
        deleted = delete_profile_station(profile=updated, expected_updated_at=updated.updated_at.isoformat(), station_index=0)
        self.assertEqual(deleted.snapshot["stations"], [])
        self.assertEqual(existing.stations.get().name, "Piste")
        self.assertEqual(existing.stations.get().slots.get().capacity, 2)

    def test_duplicate_station_name_is_rejected_and_edit_keeps_its_own_name(self):
        profile = self.save_station(self.profile())
        original = deepcopy(profile.snapshot)
        with self.assertRaises(ValidationError):
            self.save_station(profile, station_data={"name": " piste "})
        profile.refresh_from_db()
        self.assertEqual(profile.snapshot, original)
        updated = self.save_station(profile, station_index=0)
        self.assertEqual(len(updated.snapshot["stations"]), 1)

    def test_invalid_station_fields_are_rejected_before_writing(self):
        profile = self.profile()
        original = deepcopy(profile.snapshot)
        for changes in (
            {"name": " "}, {"name": "n" * 161}, {"category": "bad"},
            {"age_policy": "bad"}, {"position": -1}, {"position": 32768},
            {"position": True}, {"is_active": "yes"}, {"event_id": str(self.event.pk)},
        ):
            data = {"name": "Accueil", **changes}
            with self.subTest(data=data), self.assertRaises(ValidationError):
                self.save_station(profile, station_data=data)
            profile.refresh_from_db()
            self.assertEqual(profile.snapshot, original)

    def test_invalid_slot_data_is_rejected_without_partial_profile_changes(self):
        profile = self.profile()
        original = deepcopy(profile.snapshot)
        valid = {"day_offset": 0, "period": "morning", "capacity": 2}
        cases = [
            [{**valid, "day_offset": 5}], [{**valid, "day_offset": False}],
            [{**valid, "period": "night"}], [{**valid, "capacity": -1}],
            [{**valid, "capacity": 32768}], [{**valid, "capacity": True}],
            [{**valid, "start_time": "bad", "end_time": "12:00"}],
            [{**valid, "start_time": "08:00+01:00", "end_time": "12:00"}],
            [{**valid, "start_time": "08:00"}],
            [{**valid, "start_time": "12:00", "end_time": "08:00"}],
            [{**valid, "start_time": "08:00", "end_time": "08:00"}],
            [valid, valid], [{**valid, "event_day_id": str(self.day.pk)}],
            [None], None,
        ]
        for slots in cases:
            with self.subTest(slots=slots), self.assertRaises(ValidationError):
                self.save_station(profile, slots=slots)
            profile.refresh_from_db()
            self.assertEqual(profile.snapshot, original)

    def test_invalid_or_stale_index_is_never_applied_to_another_station(self):
        profile = self.save_station(self.profile())
        for index in (-1, 1, True, "0", None):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                delete_profile_station(profile=profile, expected_updated_at=profile.updated_at.isoformat(), station_index=index)
        for index in (-1, 1, True, "0"):
            with self.subTest(index=index), self.assertRaises(ValidationError):
                self.save_station(profile, station_index=index)
        profile.refresh_from_db()
        self.assertEqual(profile.snapshot["stations"][0]["name"], "Piste")

    def test_each_mutation_refuses_missing_or_stale_concurrency_token(self):
        profile = self.save_station(self.profile())
        stale_token = profile.updated_at.isoformat()
        current = self.metadata(profile)
        current_snapshot = deepcopy(current.snapshot)
        for token in (stale_token, "", None, "not-a-date", "2027-01-01T12:00:00"):
            actions = (
                lambda: self.metadata(profile, expected_updated_at=token),
                lambda: self.save_station(profile, expected_updated_at=token),
                lambda: delete_profile_station(profile=profile, expected_updated_at=token, station_index=0),
            )
            for action in actions:
                with self.subTest(token=token), self.assertRaises(ValidationError):
                    action()
        current.refresh_from_db()
        self.assertEqual(current.snapshot, current_snapshot)
        self.assertEqual(current.name, "Concours renommé")

    def test_deleted_index_cannot_be_reused_with_an_old_token(self):
        profile = self.save_station(self.profile())
        profile = self.save_station(profile, station_data={"name": "Buvette"})
        old_token = profile.updated_at.isoformat()
        current = delete_profile_station(profile=profile, expected_updated_at=old_token, station_index=0)
        with self.assertRaises(ValidationError):
            delete_profile_station(profile=profile, expected_updated_at=old_token, station_index=0)
        current.refresh_from_db()
        self.assertEqual(current.snapshot["stations"][0]["name"], "Buvette")

    def test_multiday_catalogues_windows_and_old_age_policy_are_preserved(self):
        second = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=2))
        Menu.objects.create(event_day=self.day, name="Menu", surcharge_cents=400)
        Menu.objects.create(event_day=second, name="Collation", meal_type=Menu.MealType.SNACK, surcharge_cents=100)
        Beverage.objects.create(event_day=second, name="Jus", surcharge_cents=50)
        StationSlot.objects.create(station=self.station, event_day=second, period=StationSlot.Period.FULL_DAY, capacity=3)
        profile = save_event_profile(event=self.event, name="Week-end enregistré")
        del profile.snapshot["stations"][0]["age_policy"]
        profile.snapshot["event"]["sumup_payment_url"] = "https://pay.sumup.com/example"
        profile.save()
        original = deepcopy(profile.snapshot)
        profile = self.metadata(profile)
        profile = self.save_station(profile, station_data={"name": "Nouveau poste"}, slots=[
            {"day_offset": 2, "period": "afternoon", "capacity": 5},
        ])
        self.assertEqual(profile.snapshot["days"], original["days"])
        self.assertEqual(profile.snapshot["windows"], original["windows"])
        self.assertEqual(profile.snapshot["stations"][:-1], original["stations"])
        self.assertEqual(profile.snapshot["event"]["sumup_payment_url"], "https://pay.sumup.com/example")
        clone = create_event_from_profile(profile=profile, name="Nouveau week-end", event_date=date(2027, 6, 1))
        cloned_second = clone.days.get(date=date(2027, 6, 3))
        self.assertEqual(cloned_second.menus.get().meal_type, Menu.MealType.SNACK)
        self.assertEqual(cloned_second.beverages.get().surcharge_cents, 50)
        self.assertEqual(clone.stations.get(name=self.station.name).age_policy, Station.AgePolicy.NONE)
        self.assertEqual(clone.stations.get(name="Nouveau poste").slots.get().capacity, 5)

    def test_reuse_reads_persisted_configuration_instead_of_stale_or_forged_object(self):
        profile = self.save_station(self.profile())
        stale = EventProfile.objects.get(pk=profile.pk)
        self.save_station(profile, station_index=0, station_data={"name": "Poste actuel"}, slots=[])
        stale.snapshot["event"]["location"] = "Valeur forgée"
        clone = create_event_from_profile(profile=stale, name="Depuis la base", event_date=date(2027, 6, 1))
        self.assertEqual(clone.location, "Centre équestre")
        self.assertEqual(clone.stations.get().name, "Poste actuel")

    def test_invalid_snapshot_is_rejected_before_metadata_or_post_mutation(self):
        profile = self.profile()
        for broken in ({}, {"version": 1}, {**profile.snapshot, "days": [{"offset": True}]}):
            EventProfile.objects.filter(pk=profile.pk).update(snapshot=broken)
            profile.refresh_from_db()
            with self.assertRaises(ValidationError):
                self.metadata(profile)
            with self.assertRaises(ValidationError):
                self.save_station(profile)
        self.assertEqual(EventProfile.objects.count(), 1)
