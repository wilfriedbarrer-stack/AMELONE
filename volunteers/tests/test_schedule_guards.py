from concurrent.futures import ThreadPoolExecutor
from datetime import time, timedelta
from threading import Barrier
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import close_old_connections, connection, connections, transaction
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse

from volunteers.models import (
    Assignment,
    Event,
    EventDay,
    MealOrder,
    Menu,
    Registration,
    Station,
    StationSlot,
    User,
)
from volunteers.scheduling import (
    event_assignment_conflicts,
    lock_schedule_members,
    station_schedule_context,
    validate_station_schedule,
)
from volunteers.self_participation import save_personal_participation
from volunteers.services import (
    BusinessRuleError,
    confirm_assignments,
    create_assignment,
    remove_assignment,
    save_meal_order,
    update_assignment,
)

from .factories import DataMixin


class ScheduleGuardTests(DataMixin, TestCase):
    def setUp(self):
        self.reg = self.registration()

    def another_event(self, *, on_date=None, start=time(11), end=time(15), period="afternoon"):
        event = Event.objects.create(
            name="Deuxième concours", registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at, status=Event.Status.PUBLISHED,
        )
        day = EventDay.objects.create(event=event, date=on_date or self.day.date)
        station = Station.objects.create(event=event, name="Second poste")
        slot = StationSlot.objects.create(
            event_day=day, station=station, period=period, capacity=2, start_time=start, end_time=end,
        )
        registration = Registration.objects.create(
            member=self.member, event_day=day, availability=Registration.Availability.FULL_DAY,
        )
        return event, registration, slot

    def legacy_conflict(self):
        first = create_assignment(registration=self.reg, station_slot=self.morning_slot)
        event, registration, slot = self.another_event()
        second = Assignment(registration=registration, station_slot=slot, period=slot.period)
        Assignment.objects.bulk_create([second])
        return first, second, event

    def test_cross_event_same_date_overlap_is_refused_for_same_member(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event()
        with self.assertRaisesMessage(BusinessRuleError, "se chevauchent"):
            create_assignment(registration=registration, station_slot=slot)
        self.assertEqual(Assignment.objects.count(), 1)

    def test_contiguous_cross_event_times_and_different_dates_are_accepted(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event(start=self.morning_slot.end_time)
        create_assignment(registration=registration, station_slot=slot)
        _, another_reg, another_slot = self.another_event(on_date=self.day.date + timedelta(days=1))
        create_assignment(registration=another_reg, station_slot=another_slot)
        self.assertEqual(Assignment.objects.count(), 3)

    def test_full_day_blocks_another_event_even_with_disjoint_indicative_hours(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event(start=time(19), end=time(20), period="full_day")
        with self.assertRaises(BusinessRuleError):
            create_assignment(registration=registration, station_slot=slot)

    def test_unknown_hours_use_period_but_known_disjoint_same_period_cross_event_is_allowed(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event(start=time(15), end=time(17), period="morning")
        assignment = create_assignment(registration=registration, station_slot=slot)
        remove_assignment(assignment=assignment)
        slot.start_time = slot.end_time = None
        slot.save()
        with self.assertRaises(BusinessRuleError):
            create_assignment(registration=registration, station_slot=slot)

    def test_other_members_cancelled_rows_and_archived_events_do_not_block(self):
        first = create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event()
        registration.member = self.other_member
        registration.save()
        second = create_assignment(registration=registration, station_slot=slot)
        remove_assignment(assignment=second)
        registration.member = self.member
        registration.save()
        self.event.status = Event.Status.ARCHIVED
        self.event.save()
        create_assignment(registration=registration, station_slot=slot)
        first.refresh_from_db()
        self.assertEqual(first.status, Assignment.Status.DRAFT)

    def test_inconsistent_cancelled_registration_is_ignored_without_deleting_history(self):
        first = create_assignment(registration=self.reg, station_slot=self.morning_slot)
        Registration.objects.filter(pk=self.reg.pk).update(status=Registration.Status.CANCELLED)
        _, registration, slot = self.another_event()
        create_assignment(registration=registration, station_slot=slot)
        first.refresh_from_db()
        self.assertEqual(first.status, Assignment.Status.DRAFT)

    def test_move_refuses_cross_event_conflict_and_preserves_original_assignment(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event(start=time(13), end=time(18))
        assignment = create_assignment(registration=registration, station_slot=slot)
        destination_station = Station.objects.create(event=slot.station.event, name="Autre emplacement")
        destination = StationSlot.objects.create(
            station=destination_station, event_day=slot.event_day, period="afternoon", capacity=2,
            start_time=time(11), end_time=time(18),
        )
        with self.assertRaises(BusinessRuleError):
            update_assignment(assignment=assignment, station_slot=destination)
        assignment.refresh_from_db()
        self.assertEqual(assignment.station_slot_id, slot.pk)

    def test_confirmation_refuses_legacy_conflicts_without_partial_confirmation(self):
        first, second, _ = self.legacy_conflict()
        with self.assertRaises(BusinessRuleError):
            confirm_assignments(assignments=[first, second], confirmed_by=self.organizer)
        for assignment in (first, second):
            assignment.refresh_from_db()
            self.assertEqual(assignment.status, Assignment.Status.DRAFT)
            self.assertIsNone(assignment.confirmed_at)

    def test_editing_assigned_slot_hours_cannot_create_local_or_cross_event_conflicts(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        self.afternoon_slot.start_time = time(11)
        with self.assertRaises(ValidationError):
            self.afternoon_slot.full_clean()
        with self.assertRaises(ValidationError):
            self.afternoon_slot.save(update_fields=("start_time",))
        self.afternoon_slot.refresh_from_db()
        self.assertEqual(self.afternoon_slot.start_time, time(13, 30))

    def test_editing_assigned_slot_checks_other_event_and_keeps_reserved_prices(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        _, registration, slot = self.another_event(start=time(13), end=time(18))
        create_assignment(registration=registration, station_slot=slot)
        menu = Menu.objects.create(name="Repas conservé", surcharge_cents=500)
        order = save_meal_order(registration=self.reg, menu=menu)
        before = MealOrder.objects.values().get(pk=order.pk)
        slot.start_time = time(11)
        with self.assertRaises(ValidationError):
            slot.save()
        self.assertEqual(MealOrder.objects.values().get(pk=order.pk), before)

    def test_final_batch_state_accepts_coordinated_morning_and_afternoon_changes(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        self.morning_slot.start_time, self.morning_slot.end_time = time(10), time(14)
        self.afternoon_slot.start_time, self.afternoon_slot.end_time = time(15), time(20)
        slots = [self.morning_slot, self.afternoon_slot]
        with transaction.atomic():
            members = lock_schedule_members(slots)
            list(EventDay.objects.select_for_update().filter(pk=self.day.pk))
            list(StationSlot.objects.select_for_update().filter(pk__in=[slot.pk for slot in slots]).order_by("pk"))
            with station_schedule_context(slots, locked_member_ids=members):
                for slot in slots:
                    slot.save()
        self.assertFalse(self.morning_slot.overlaps(self.afternoon_slot))
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.DRAFT).count(), 2)

    def test_final_batch_conflict_is_refused_before_any_hour_changes(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        self.morning_slot.end_time = time(16)
        self.afternoon_slot.start_time = time(15)
        with self.assertRaises(ValidationError), transaction.atomic():
            members = lock_schedule_members([self.morning_slot, self.afternoon_slot])
            with station_schedule_context([self.morning_slot, self.afternoon_slot], locked_member_ids=members):
                self.morning_slot.save()
                self.afternoon_slot.save()
        self.morning_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.end_time, time(12, 30))

    def test_appearance_of_unlocked_assignee_requires_reload_instead_of_late_member_lock(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        with self.assertRaisesMessage(ValidationError, "ont changé"):
            validate_station_schedule([self.morning_slot], locked_member_ids=set())

    def test_read_only_conflict_audit_includes_other_event_and_preserves_all_rows(self):
        first, second, other_event = self.legacy_conflict()
        before = list(Assignment.objects.values().order_by("pk"))
        warnings = event_assignment_conflicts(self.event)
        self.assertEqual(len(warnings), 1)
        warning = warnings[0]
        self.assertEqual(warning["member_name"], self.member.full_name)
        self.assertSetEqual({warning["first_event"], warning["second_event"]}, {self.event.name, other_event.name})
        self.assertSetEqual({warning["first_assignment_id"], warning["second_assignment_id"]}, {first.pk, second.pk})
        self.assertEqual(list(Assignment.objects.values().order_by("pk")), before)

    def test_django_admin_inline_uses_final_state_and_keeps_audit_log(self):
        admin = User.objects.create_superuser("schedule-admin@example.test", "Strong-pass-123")
        self.client.force_login(admin)
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        data = {
            "name": self.station.name, "event": str(self.event.pk), "category": "volunteer",
            "age_policy": "none", "is_active": "on", "position": 0,
            "slots-TOTAL_FORMS": 2, "slots-INITIAL_FORMS": 2, "slots-MIN_NUM_FORMS": 0,
            "slots-MAX_NUM_FORMS": 1000, "_save": "Enregistrer",
        }
        for index, (slot, start, end) in enumerate((
            (self.morning_slot, "10:00", "14:00"), (self.afternoon_slot, "15:00", "20:00"),
        )):
            data.update({
                f"slots-{index}-id": str(slot.pk), f"slots-{index}-station": str(self.station.pk),
                f"slots-{index}-event_day": str(self.day.pk), f"slots-{index}-period": slot.period,
                f"slots-{index}-capacity": slot.capacity,
                f"slots-{index}-start_time": start, f"slots-{index}-end_time": end,
            })
        response = self.client.post(reverse("admin:volunteers_station_change", args=[self.station.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.morning_slot.refresh_from_db()
        self.afternoon_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.end_time, time(14))
        self.assertEqual(self.afternoon_slot.start_time, time(15))

    def test_django_admin_single_slot_refuses_conflict_with_visible_form_error(self):
        admin = User.objects.create_superuser("single-slot-admin@example.test", "Strong-pass-123")
        self.client.force_login(admin)
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        create_assignment(registration=self.reg, station_slot=self.afternoon_slot)
        response = self.client.post(reverse("admin:volunteers_stationslot_change", args=[self.afternoon_slot.pk]), {
            "station": str(self.station.pk), "event_day": str(self.day.pk), "period": "afternoon",
            "capacity": self.afternoon_slot.capacity, "start_time": "11:00", "end_time": "18:00",
            "_save": "Enregistrer",
        })
        self.assertContains(response, "se chevauchent")
        self.afternoon_slot.refresh_from_db()
        self.assertEqual(self.afternoon_slot.start_time, time(13, 30))


@skipUnless(connection.vendor == "postgresql", "Verrous PostgreSQL nécessaires")
class ConcurrentScheduleGuards(DataMixin, TransactionTestCase):
    def setUp(self):
        self.setUpTestData()
        self.reg = self.registration()
        self.event2 = Event.objects.create(
            name="Concours concurrent", registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at, status=Event.Status.PUBLISHED,
        )
        self.day2 = EventDay.objects.create(event=self.event2, date=self.day.date)
        self.station2 = Station.objects.create(event=self.event2, name="Poste concurrent")
        self.slot2 = StationSlot.objects.create(
            station=self.station2, event_day=self.day2, period="afternoon", capacity=2,
            start_time=time(11), end_time=time(15),
        )
        self.reg2 = Registration.objects.create(member=self.member, event_day=self.day2, availability="full_day")

    def concurrent_actions(self, actions):
        barrier = Barrier(len(actions))

        def run(action):
            close_old_connections()
            try:
                with connections["default"].cursor() as cursor:
                    cursor.execute("SET lock_timeout = '5s'")
                barrier.wait(timeout=5)
                try:
                    action()
                    return "saved"
                except ValidationError:
                    return "refused"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=len(actions)) as pool:
            futures = [pool.submit(run, action) for action in actions]
            return [future.result(timeout=15) for future in futures]

    def test_cross_event_concurrent_assignments_serialize_by_member(self):
        actions = [
            lambda: create_assignment(registration=self.reg, station_slot=self.morning_slot),
            lambda: create_assignment(registration=self.reg2, station_slot=self.slot2),
        ]
        self.assertCountEqual(self.concurrent_actions(actions), ["saved", "refused"])
        self.assertEqual(Assignment.objects.count(), 1)

    def test_slot_hour_change_and_cross_event_assignment_serialize_by_member(self):
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        self.slot2.start_time = time(13)
        self.slot2.save(update_fields=("start_time",))

        def extend_morning():
            slot = StationSlot.objects.get(pk=self.morning_slot.pk)
            slot.end_time = time(14)
            slot.save(update_fields=("end_time",))

        self.assertCountEqual(self.concurrent_actions([
            extend_morning,
            lambda: create_assignment(registration=self.reg2, station_slot=self.slot2),
        ]), ["saved", "refused"])
        self.assertFalse(event_assignment_conflicts(self.event))

    def test_admin_audit_and_personal_participation_keep_actor_before_member_lock_order(self):
        user = self.family_user
        user.is_superuser = user.is_staff = user.is_organizer = True
        user.save()
        self.member.user = user
        self.member.save()
        create_assignment(registration=self.reg, station_slot=self.morning_slot)
        client = Client()
        client.force_login(user)
        session = client.cookies["sessionid"].value

        def admin_edit():
            local_client = Client()
            local_client.cookies["sessionid"] = session
            for index in range(2):
                response = local_client.post(reverse("admin:volunteers_stationslot_change", args=[self.morning_slot.pk]), {
                    "station": str(self.station.pk), "event_day": str(self.day.pk), "period": "morning",
                    "capacity": self.morning_slot.capacity, "start_time": "08:00",
                    "end_time": "12:15" if index == 0 else "12:30", "_save": "Enregistrer",
                })
                if response.status_code != 302:
                    raise AssertionError(f"Échec du formulaire admin : {response.status_code}")

        def personal_edit():
            for index in range(2):
                save_personal_participation(
                    actor=user, event_day=self.day, availability="full_day", note=f"Essai {index}",
                    meal_choices={"no_meal": True}, station_slots=[self.morning_slot],
                )

        self.assertEqual(self.concurrent_actions([admin_edit, personal_edit]), ["saved", "saved"])
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.CONFIRMED).count(), 1)
