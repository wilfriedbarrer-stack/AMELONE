"""La disponibilité affichée tient compte des périodes du même poste."""

from datetime import date, time

from django.contrib import admin
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from volunteers.models import Assignment, Family, Member, Registration, Station, StationSlot, User

from .factories import DataMixin


class SharedCapacityUIFixture(DataMixin):
    def setUp(self):
        super().setUp()
        StationSlot.objects.filter(pk__in=[self.morning_slot.pk, self.afternoon_slot.pk]).update(
            capacity=2,
        )
        self.morning_slot.refresh_from_db()
        self.afternoon_slot.refresh_from_db()
        self.full_day_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=2,
            start_time=time(8), end_time=time(18),
        )
        self.slots = (self.full_day_slot, self.morning_slot, self.afternoon_slot)
        self.member_number = 0

    def assignment(self, slot, *, member=None, status=Assignment.Status.CONFIRMED,
                   registration_status=Registration.Status.ACTIVE):
        if member is None:
            self.member_number += 1
            member = Member.objects.create(
                family=self.other_family, first_name=f"Participant{self.member_number}",
                last_name="Partage", birth_date=date(1990, 1, 1),
            )
        registration, _ = Registration.objects.get_or_create(
            member=member, event_day=self.day,
            defaults={"availability": Registration.Availability.FULL_DAY,
                      "status": registration_status},
        )
        # États historiques et brouillons compris : ces tests exercent la lecture
        # du planning sans dépendre de la validation de création d'affectation.
        assignment = Assignment(
            registration=registration, station_slot=slot, period=slot.period, status=status,
        )
        Assignment.objects.bulk_create([assignment])
        return assignment

    def public_rows(self, **params):
        response = self.client.get(reverse("public_planning", args=[self.event.pk]), params)
        self.assertEqual(response.status_code, 200)
        return response, {row["slot_id"]: row for row in response.context["rows"]}

    def board_slots(self):
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("assignment_board_event", args=[self.event.pk]))
        self.assertEqual(response.status_code, 200)
        return {slot.pk: slot for slot in response.context["slots"]}


class SharedCapacityPlanningTests(SharedCapacityUIFixture, TestCase):
    def test_full_day_and_both_half_days_fill_all_three_rows(self):
        for slot in self.slots:
            self.assignment(slot)

        response, rows = self.public_rows()
        board = self.board_slots()

        self.assertContains(response, "0 places disponibles", count=3)
        self.assertContains(response, "1 place indisponible sur une autre période", count=3)
        for slot in self.slots:
            with self.subTest(period=slot.period):
                row = rows[slot.pk]
                self.assertEqual(row["taken"], 1)
                self.assertEqual(row["remaining"], 0)
                self.assertEqual(row["blocked_elsewhere"], 1)
                self.assertTrue(row["is_full"])
                self.assertEqual(row["fill_percent"], 100)
                self.assertEqual(board[slot.pk].taken, 1)
                self.assertEqual(board[slot.pk].remaining, 0)
                self.assertEqual(board[slot.pk].blocked_elsewhere, 1)

    def test_distinct_half_day_people_share_one_full_day_place(self):
        self.assignment(self.morning_slot)
        self.assignment(self.afternoon_slot)

        _, rows = self.public_rows()

        row = rows[self.full_day_slot.pk]
        self.assertEqual(row["taken"], 0)
        self.assertEqual(row["remaining"], 1)
        self.assertEqual(row["blocked_elsewhere"], 1)
        self.assertFalse(row["is_full"])
        self.assertEqual(row["fill_percent"], 50)
        self.assertEqual(self.board_slots()[self.full_day_slot.pk].remaining, 1)

    def test_different_period_capacities_keep_local_quotas_and_use_the_larger_pool(self):
        StationSlot.objects.filter(pk=self.full_day_slot.pk).update(capacity=3)
        StationSlot.objects.filter(pk=self.morning_slot.pk).update(capacity=1)
        self.assignment(self.full_day_slot)
        self.assignment(self.morning_slot)

        _, rows = self.public_rows()
        board = self.board_slots()

        # Le matin partage un pool de trois avec la journée, mais son quota
        # propre d'une personne est déjà atteint. L'après-midi reste à deux.
        for slot, remaining in ((self.full_day_slot, 1), (self.morning_slot, 0),
                                (self.afternoon_slot, 2)):
            with self.subTest(period=slot.period):
                self.assertEqual(rows[slot.pk]["remaining"], remaining)
                self.assertEqual(board[slot.pk].remaining, remaining)

    def test_full_day_local_quota_can_be_full_while_half_days_still_have_places(self):
        StationSlot.objects.filter(pk=self.full_day_slot.pk).update(capacity=1)
        self.assignment(self.full_day_slot)

        _, rows = self.public_rows()

        self.assertEqual(rows[self.full_day_slot.pk]["remaining"], 0)
        self.assertTrue(rows[self.full_day_slot.pk]["is_full"])
        self.assertEqual(rows[self.morning_slot.pk]["remaining"], 1)
        self.assertEqual(rows[self.afternoon_slot.pk]["remaining"], 1)

    def test_public_uses_only_confirmed_active_people_but_board_reserves_drafts(self):
        confirmed = self.assignment(self.full_day_slot)
        draft = self.assignment(self.morning_slot, status=Assignment.Status.DRAFT)
        cancelled = self.assignment(self.afternoon_slot, status=Assignment.Status.CANCELLED)
        inactive = self.assignment(
            self.afternoon_slot, registration_status=Registration.Status.CANCELLED,
        )

        response, rows = self.public_rows()

        self.assertContains(response, confirmed.registration.member.full_name)
        for hidden in (draft, cancelled, inactive):
            self.assertNotContains(response, hidden.registration.member.full_name)
        for slot in self.slots:
            row = rows[slot.pk]
            self.assertEqual(row["remaining"], 1)
            self.assertFalse(row["is_full"])
        self.assertEqual(rows[self.full_day_slot.pk]["taken"], 1)
        self.assertEqual(rows[self.morning_slot.pk]["taken"], 0)
        self.assertEqual(rows[self.afternoon_slot.pk]["taken"], 0)

        board = self.board_slots()
        self.assertEqual(board[self.full_day_slot.pk].remaining, 0)
        self.assertEqual(board[self.morning_slot.pk].remaining, 0)
        self.assertEqual(board[self.afternoon_slot.pk].remaining, 1)
        self.assertEqual([item.pk for item in board[self.morning_slot.pk].active_assignments],
                         [draft.pk])
        self.assertEqual(board[self.afternoon_slot.pk].active_assignments, [])

    def test_family_scope_keeps_only_family_slots_and_counts_hidden_sibling_periods(self):
        self.assignment(self.morning_slot, member=self.member)
        self.assignment(self.full_day_slot, member=self.other_member)
        self.client.force_login(self.family_user)

        _, all_rows = self.public_rows()
        response, family_rows = self.public_rows(scope="family")

        self.assertEqual(set(family_rows), {self.morning_slot.pk})
        row = family_rows[self.morning_slot.pk]
        self.assertEqual(row, all_rows[self.morning_slot.pk])
        self.assertEqual(row["people"], [self.member.full_name])
        self.assertEqual(row["remaining"], 0)
        self.assertEqual(row["blocked_elsewhere"], 1)
        self.assertNotContains(response, self.other_member.full_name)

    def test_admin_remaining_column_uses_the_same_shared_capacity(self):
        for slot in self.slots:
            self.assignment(slot)
        administrator = User.objects.create_superuser(email="shared-admin@example.test")
        self.client.force_login(administrator)

        response = self.client.get(reverse("admin:volunteers_stationslot_changelist"))

        self.assertEqual(response.status_code, 200)
        model_admin = admin.site._registry[StationSlot]
        self.assertIn("shared_remaining_capacity", response.context["cl"].list_display)
        for slot in response.context["cl"].result_list:
            with self.subTest(period=slot.period):
                self.assertEqual(model_admin.shared_remaining_capacity(slot), 0)
                self.assertEqual(slot.remaining_capacity, 0)


class SharedCapacityPersonalFixture(SharedCapacityUIFixture):
    def setUp(self):
        super().setUp()
        self.administrator = User.objects.create_superuser(email="shared-personal@example.test")
        self.personal_family = Family.objects.create(account=self.administrator, name="Personnel")
        self.personal_member = Member.objects.create(
            family=self.personal_family, user=self.administrator,
            first_name="Admin", last_name="Personnel", birth_date=date(1985, 1, 1),
        )
        self.client.force_login(self.administrator)
        self.url = reverse("personal_registration", args=[self.day.pk])


class SharedCapacityPersonalChoicesTests(SharedCapacityPersonalFixture, TestCase):
    def test_full_shared_slots_are_absent_from_new_self_assignment_choices(self):
        self.assignment(self.full_day_slot)
        self.assignment(self.full_day_slot, status=Assignment.Status.DRAFT)
        free_slot = StationSlot.objects.create(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.MORNING, capacity=1,
        )

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        choices = set(response.context["form"].fields["station_slots"].queryset.values_list(
            "pk", flat=True,
        ))
        self.assertEqual(choices, {free_slot.pk})
        for slot in self.slots:
            self.assertNotContains(response, f'value="{slot.pk}"')
        self.assertContains(response, f'value="{free_slot.pk}"')

    def test_current_full_assignment_stays_editable_and_labels_show_actual_remaining(self):
        self.assignment(self.full_day_slot)
        self.assignment(self.morning_slot, member=self.personal_member)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, 200)
        field = response.context["form"].fields["station_slots"]
        choices = {slot.pk: slot for slot in field.queryset}
        self.assertEqual(set(choices), {self.morning_slot.pk, self.afternoon_slot.pk})
        self.assertEqual([str(value) for value in response.context["form"]["station_slots"].value()],
                         [str(self.morning_slot.pk)])
        self.assertIn("0 place(s) restante(s)", field.label_from_instance(choices[self.morning_slot.pk]))
        self.assertIn("1 place(s) restante(s)", field.label_from_instance(choices[self.afternoon_slot.pk]))
        self.assertRegex(response.content.decode(),
                         rf'<input[^>]+value="{self.morning_slot.pk}"[^>]+checked')


class SharedCapacityQueryTests(SharedCapacityPersonalFixture, TestCase):
    """Une augmentation des postes ne doit pas ajouter une requête par ligne."""

    def setUp(self):
        super().setUp()
        self.assignment(self.morning_slot)

    def add_station_groups(self, count):
        for index in range(count):
            station = Station.objects.create(event=self.event, name=f"Poste supplémentaire {index}")
            slots = [StationSlot(
                station=station, event_day=self.day, period=period, capacity=2,
            ) for period in StationSlot.Period.values]
            StationSlot.objects.bulk_create(slots)
            self.assignment(next(slot for slot in slots if slot.period == StationSlot.Period.FULL_DAY))

    def assert_query_growth_is_bounded(self, url, **params):
        self.assertEqual(self.client.get(url, params).status_code, 200)
        with CaptureQueriesContext(connection) as baseline:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        self.add_station_groups(7)
        with CaptureQueriesContext(connection) as expanded:
            response = self.client.get(url, params)
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(
            len(expanded), len(baseline) + 2,
            f"Les requêtes passent de {len(baseline)} à {len(expanded)} pour 21 créneaux de plus.",
        )

    def test_public_planning_query_count_does_not_grow_with_station_groups(self):
        self.client.logout()
        self.assert_query_growth_is_bounded(reverse("public_planning", args=[self.event.pk]))

    def test_family_scope_query_count_does_not_grow_with_hidden_station_groups(self):
        self.assignment(self.morning_slot, member=self.member)
        self.client.force_login(self.family_user)
        self.assert_query_growth_is_bounded(
            reverse("public_planning", args=[self.event.pk]), scope="family",
        )

    def test_organizer_board_query_count_does_not_grow_with_station_groups(self):
        self.client.force_login(self.organizer)
        self.assert_query_growth_is_bounded(reverse("assignment_board_event", args=[self.event.pk]))

    def test_personal_choice_labels_do_not_query_each_station_group(self):
        self.assert_query_growth_is_bounded(self.url)

    def test_admin_shared_capacity_columns_do_not_query_each_station_group(self):
        self.assert_query_growth_is_bounded(reverse("admin:volunteers_stationslot_changelist"))
