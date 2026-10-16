from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from threading import Barrier

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connections, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature

from volunteers.capacity import get_slot_capacities, get_slot_capacity
from volunteers.forms_organizer import StationSlotFormSet
from volunteers.models import Assignment, EventDay, Member, Registration, StationSlot
from volunteers.scheduling import lock_schedule_members, station_schedule_context
from volunteers.services import (
    BusinessRuleError,
    cancel_registration,
    confirm_assignment,
    create_assignment,
    remove_assignment,
    save_registration,
    update_assignment,
)

from .factories import DataMixin


class SharedStationCapacityTests(DataMixin, TestCase):
    def setUp(self):
        self.morning_slot.capacity = 2
        self.morning_slot.save(update_fields=("capacity",))
        self.full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day, period=StationSlot.Period.FULL_DAY,
            capacity=2, start_time=time(19), end_time=time(20),
        )
        self.sequence = 0

    def new_registration(self, *, day=None):
        self.sequence += 1
        member = Member.objects.create(
            family=self.family, first_name=f"Participant {self.sequence}", last_name="Test",
            birth_date=date(1990, 1, 1),
        )
        return Registration.objects.create(
            member=member, event_day=day or self.day,
            availability=Registration.Availability.FULL_DAY,
        )

    def assign(self, slot):
        return create_assignment(
            registration=self.new_registration(), station_slot=slot, actor=self.organizer,
        )

    def test_two_places_accept_full_day_and_one_person_for_each_half_day(self):
        for slot in (self.full_slot, self.morning_slot, self.afternoon_slot):
            assignment = self.assign(slot)
            confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        snapshots = get_slot_capacities([self.full_slot, self.morning_slot, self.afternoon_slot])
        self.assertEqual(Assignment.objects.count(), 3)
        for snapshot in snapshots.values():
            self.assertEqual(snapshot.remaining, 0)
            self.assertEqual(snapshot.morning_occupied, 2)
            self.assertEqual(snapshot.afternoon_occupied, 2)
        with self.assertRaises(BusinessRuleError):
            self.assign(self.morning_slot)
        with self.assertRaises(BusinessRuleError):
            self.assign(self.afternoon_slot)

    def test_full_day_uses_both_halves_despite_indicative_disjoint_hours(self):
        self.assign(self.full_slot)
        self.assign(self.morning_slot)
        self.assertEqual(self.full_slot.remaining_capacity, 0)
        self.assertEqual(self.morning_slot.remaining_capacity, 0)
        self.assertEqual(self.afternoon_slot.remaining_capacity, 1)
        with self.assertRaises(BusinessRuleError):
            self.assign(self.full_slot)
        self.assign(self.afternoon_slot)

    def test_full_morning_does_not_block_afternoon(self):
        self.assign(self.morning_slot)
        self.assign(self.morning_slot)
        with self.assertRaises(BusinessRuleError):
            self.assign(self.full_slot)
        self.assertEqual(self.afternoon_slot.remaining_capacity, 2)
        self.assign(self.afternoon_slot)

    def test_different_quotas_keep_local_limit_and_share_maximum_per_half(self):
        self.morning_slot.capacity = 4
        self.morning_slot.save(update_fields=("capacity",))
        self.afternoon_slot.capacity = 3
        self.afternoon_slot.save(update_fields=("capacity",))
        self.assign(self.full_slot)
        self.assign(self.full_slot)
        snapshot = get_slot_capacity(self.full_slot)
        self.assertEqual((snapshot.morning_capacity, snapshot.afternoon_capacity), (4, 3))
        self.assertEqual(snapshot.remaining, 0)
        self.assertEqual(self.morning_slot.remaining_capacity, 2)
        self.assertEqual(self.afternoon_slot.remaining_capacity, 1)
        self.assign(self.morning_slot)
        self.assign(self.morning_slot)
        self.assign(self.afternoon_slot)
        with self.assertRaises(BusinessRuleError):
            self.assign(self.afternoon_slot)

    def test_selection_loads_hidden_siblings_in_one_query_and_empty_selection_in_none(self):
        self.assign(self.full_slot)
        self.assign(self.afternoon_slot)
        with self.assertNumQueries(1):
            snapshot = get_slot_capacities([self.morning_slot])[self.morning_slot.pk]
        self.assertEqual(snapshot.occupied, 0)
        self.assertEqual(snapshot.remaining, 1)
        self.assertEqual(snapshot.afternoon_occupied, 2)
        with self.assertNumQueries(0):
            self.assertEqual(get_slot_capacities([]), {})

    def test_custom_statuses_and_cancelled_registration_are_respected(self):
        draft = self.assign(self.full_slot)
        confirmed = self.assign(self.morning_slot)
        confirm_assignment(assignment=confirmed, confirmed_by=self.organizer)
        self.assertEqual(self.morning_slot.remaining_capacity, 0)
        public = get_slot_capacity(self.morning_slot, statuses=(Assignment.Status.CONFIRMED,))
        self.assertEqual(public.remaining, 1)
        Registration.objects.filter(pk=draft.registration_id).update(status=Registration.Status.CANCELLED)
        self.assertEqual(self.morning_slot.remaining_capacity, 1)
        self.assertEqual(get_slot_capacity(self.full_slot).occupied, 0)

    def test_cancellation_releases_both_budgets(self):
        full = self.assign(self.full_slot)
        self.assign(self.morning_slot)
        remove_assignment(assignment=full, actor=self.organizer)
        self.assertEqual(self.morning_slot.remaining_capacity, 1)
        self.assertEqual(self.afternoon_slot.remaining_capacity, 2)
        self.assertEqual(self.full_slot.remaining_capacity, 1)

    def test_update_excludes_current_assignment_before_checking_destination(self):
        self.assign(self.full_slot)
        morning = self.assign(self.morning_slot)
        moved = update_assignment(
            assignment=morning, station_slot=self.afternoon_slot, actor=self.organizer,
        )
        confirm_assignment(assignment=moved, confirmed_by=self.organizer)
        self.assertEqual(self.morning_slot.remaining_capacity, 1)
        self.assertEqual(self.afternoon_slot.remaining_capacity, 0)
        self.assign(self.morning_slot)
        with self.assertRaises(BusinessRuleError):
            update_assignment(
                assignment=moved, station_slot=self.full_slot, actor=self.organizer,
            )
        moved.refresh_from_db()
        self.assertEqual(moved.station_slot_id, self.afternoon_slot.pk)

    def test_other_post_and_other_day_have_independent_budgets(self):
        self.assign(self.full_slot)
        self.assign(self.full_slot)
        other_post = StationSlot.objects.create(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        next_day = StationSlot.objects.create(
            station=self.station, event_day=other_day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
        )
        snapshots = get_slot_capacities([other_post, next_day])
        self.assertEqual(snapshots[other_post.pk].remaining, 2)
        self.assertEqual(snapshots[next_day.pk].remaining, 2)

    def test_unsaved_and_virtual_capacity_are_supported_without_changing_data(self):
        self.full_slot.capacity = 4
        snapshot = get_slot_capacity(self.full_slot)
        self.assertEqual((snapshot.remaining, snapshot.morning_capacity), (4, 4))
        self.full_slot.refresh_from_db()
        self.assertEqual(self.full_slot.capacity, 2)
        unsaved = StationSlot(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=3,
        )
        self.assertEqual(get_slot_capacity(unsaved).remaining, 3)

    def test_capacity_reduction_cannot_create_local_overbooking(self):
        self.assign(self.morning_slot)
        self.assign(self.morning_slot)
        self.morning_slot.capacity = 1
        with self.assertRaises(ValidationError):
            self.morning_slot.save(update_fields=("capacity",))
        self.morning_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.capacity, 2)

    def test_coordinated_capacity_reduction_checks_final_shared_budget(self):
        self.assign(self.full_slot)
        self.assign(self.morning_slot)
        self.full_slot.capacity = 1
        self.morning_slot.capacity = 1
        with self.assertRaises(ValidationError), transaction.atomic():
            slots = [self.full_slot, self.morning_slot]
            member_ids = lock_schedule_members(slots)
            with station_schedule_context(slots, locked_member_ids=member_ids):
                self.full_slot.save(update_fields=("capacity",))
                self.morning_slot.save(update_fields=("capacity",))
        self.full_slot.refresh_from_db()
        self.morning_slot.refresh_from_db()
        self.assertEqual((self.full_slot.capacity, self.morning_slot.capacity), (2, 2))

    def test_coordinated_capacity_redistribution_uses_final_peer_values(self):
        self.morning_slot.capacity = 1
        self.morning_slot.save(update_fields=("capacity",))
        self.assign(self.full_slot)
        self.assign(self.morning_slot)
        self.full_slot.capacity = 1
        self.morning_slot.capacity = 2
        slots = [self.full_slot, self.morning_slot]
        with transaction.atomic():
            member_ids = lock_schedule_members(slots)
            with station_schedule_context(slots, locked_member_ids=member_ids):
                self.full_slot.save(update_fields=("capacity",))
                self.morning_slot.save(update_fields=("capacity",))
        self.assertEqual(self.morning_slot.remaining_capacity, 0)

    def test_bound_formset_defers_individual_checks_until_final_peer_values_are_known(self):
        self.morning_slot.capacity = 1
        self.morning_slot.save(update_fields=("capacity",))
        self.assign(self.full_slot)
        self.assign(self.morning_slot)
        slots = list(self.station.slots.all())
        data = {
            "slots-TOTAL_FORMS": str(len(slots)), "slots-INITIAL_FORMS": str(len(slots)),
            "slots-MIN_NUM_FORMS": "0", "slots-MAX_NUM_FORMS": "1000",
        }
        for index, slot in enumerate(slots):
            data.update({
                f"slots-{index}-id": str(slot.pk),
                f"slots-{index}-event_day": str(self.day.pk),
                f"slots-{index}-period": slot.period,
                f"slots-{index}-capacity": "1" if slot.period == StationSlot.Period.FULL_DAY else "2",
                f"slots-{index}-start_time": slot.start_time.strftime("%H:%M"),
                f"slots-{index}-end_time": slot.end_time.strftime("%H:%M"),
            })
        formset = StationSlotFormSet(
            data, instance=self.station, prefix="slots", form_kwargs={"event": self.event},
        )
        self.assertTrue(formset.is_valid(), formset.errors)
        final_slots = [form.instance for form in formset.forms]
        with transaction.atomic():
            member_ids = lock_schedule_members(final_slots)
            with station_schedule_context(final_slots, locked_member_ids=member_ids):
                formset.save()
        self.full_slot.refresh_from_db()
        self.assertEqual(self.full_slot.capacity, 1)
        self.assertEqual(self.morning_slot.remaining_capacity, 0)

    def test_legacy_overbooking_is_kept_but_cannot_be_aggravated(self):
        registrations = [self.new_registration() for _ in range(3)]
        assignments = [Assignment(
            registration=registration, station_slot=self.morning_slot,
            period=StationSlot.Period.MORNING,
        ) for registration in registrations]
        Assignment.objects.bulk_create(assignments)
        StationSlot.objects.filter(pk=self.morning_slot.pk).update(capacity=1)
        self.morning_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.remaining_capacity, 0)
        confirm_assignment(assignment=assignments[0], confirmed_by=self.organizer)
        self.morning_slot.capacity = 2
        self.morning_slot.save(update_fields=("capacity",))
        self.morning_slot.capacity = 1
        with self.assertRaises(ValidationError):
            self.morning_slot.save(update_fields=("capacity",))
        with self.assertRaises(BusinessRuleError):
            self.assign(self.morning_slot)
        self.assign(self.afternoon_slot)
        self.assertEqual(Assignment.objects.filter(station_slot=self.morning_slot).count(), 3)

    def test_reactivating_registration_cannot_restore_historical_overbooking(self):
        self.assign(self.full_slot)
        morning = self.assign(self.morning_slot)
        registration = morning.registration
        # An old direct admin cancellation may have left its assignment active.
        registration.status = Registration.Status.CANCELLED
        registration.save(update_fields=("status",))
        self.assign(self.full_slot)
        registration.status = Registration.Status.ACTIVE
        with self.assertRaises(ValidationError):
            registration.save(update_fields=("status",))
        registration.refresh_from_db()
        self.assertEqual(registration.status, Registration.Status.CANCELLED)
        self.assertEqual(get_slot_capacity(self.morning_slot).morning_occupied, 2)

    def test_normal_registration_cancellation_and_reactivation_preserve_released_places(self):
        self.assign(self.full_slot)
        morning = self.assign(self.morning_slot)
        cancel_registration(registration=morning.registration, actor=self.organizer)
        self.assign(self.full_slot)
        registration = save_registration(
            member=morning.registration.member, event_day=self.day,
            availability=Registration.Availability.FULL_DAY, actor=self.organizer,
        )
        self.assertEqual(registration.status, Registration.Status.ACTIVE)
        morning.refresh_from_db()
        self.assertEqual(morning.status, Assignment.Status.CANCELLED)
        self.assertEqual(get_slot_capacity(self.morning_slot).morning_occupied, 2)


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentSharedStationCapacityTests(DataMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day, period=StationSlot.Period.FULL_DAY,
            capacity=1,
        )
        self.registrations = [self.registration(), self.registration(member=self.other_member)]

    def concurrent_assignments(self, *, direct=False):
        barrier = Barrier(2)

        def assign(values):
            registration_id, slot_id = values
            close_old_connections()
            try:
                registration = Registration.objects.get(pk=registration_id)
                slot = StationSlot.objects.get(pk=slot_id)
                barrier.wait(timeout=10)
                if direct:
                    Assignment.objects.create(registration=registration, station_slot=slot)
                else:
                    create_assignment(registration=registration, station_slot=slot, actor=self.organizer)
                return "created"
            except (BusinessRuleError, ValidationError):
                return "rejected"
            finally:
                connections.close_all()

        values = list(zip(
            [registration.pk for registration in self.registrations],
            [self.full_slot.pk, self.morning_slot.pk],
        ))
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(assign, values))
        self.assertCountEqual(results, ["created", "rejected"])
        self.assertEqual(Assignment.objects.count(), 1)

    def test_distinct_people_and_distinct_slots_are_serialized(self):
        self.concurrent_assignments()

    def test_direct_model_writes_also_serialize_shared_capacity(self):
        self.concurrent_assignments(direct=True)

    def test_capacity_reduction_and_assignment_cannot_overbook_together(self):
        self.full_slot.capacity = 2
        self.full_slot.save(update_fields=("capacity",))
        create_assignment(
            registration=self.registrations[0], station_slot=self.full_slot, actor=self.organizer,
        )
        barrier = Barrier(2)

        def change(action):
            close_old_connections()
            try:
                barrier.wait(timeout=10)
                if action == "reduce":
                    slot = StationSlot.objects.get(pk=self.full_slot.pk)
                    slot.capacity = 1
                    slot.save(update_fields=("capacity",))
                else:
                    create_assignment(
                        registration=Registration.objects.get(pk=self.registrations[1].pk),
                        station_slot=StationSlot.objects.get(pk=self.morning_slot.pk),
                        actor=self.organizer,
                    )
                return "accepted"
            except (BusinessRuleError, ValidationError):
                return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(change, ("reduce", "assign")))
        self.assertCountEqual(results, ["accepted", "rejected"])
        self.full_slot.refresh_from_db()
        snapshot = get_slot_capacity(self.full_slot)
        self.assertLessEqual(snapshot.morning_occupied, snapshot.morning_capacity)
