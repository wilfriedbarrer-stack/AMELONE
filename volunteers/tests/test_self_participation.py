from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from threading import Barrier

from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, connections
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.urls import reverse
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Beverage,
    Event,
    EventDay,
    Family,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    StationSlot,
    User,
)
from volunteers.services import BusinessRuleError, confirm_assignment, create_assignment

from .factories import DataMixin


class PersonalProfileTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="personal-admin@example.test", password="Strong-pass-123",
            first_name="Alice", last_name="Durand",
        )
        self.client.force_login(self.admin)
        self.profile_url = reverse("personal_profile")

    def profile_data(self, **extra):
        return {
            "first_name": "Alice", "last_name": "Durand",
            "birth_date": "1985-05-10", **extra,
        }

    def test_get_and_invalid_profile_do_not_create_family_or_member(self):
        for response in (
            self.client.get(reverse("personal_participation")),
            self.client.get(self.profile_url),
            self.client.post(self.profile_url, self.profile_data(birth_date="")),
        ):
            self.assertEqual(response.status_code, 200)
        self.assertFalse(Family.objects.filter(account=self.admin).exists())
        self.assertFalse(Member.objects.filter(user=self.admin).exists())

    def test_admin_without_family_creates_own_personal_profile_once(self):
        response = self.client.post(self.profile_url, self.profile_data())
        self.assertEqual(response.status_code, 302)
        member = Member.objects.get(user=self.admin)
        self.assertEqual(member.family.account_id, self.admin.pk)
        self.assertEqual(member.first_name, "Alice")
        self.assertEqual(member.birth_date, date(1985, 5, 10))
        response = self.client.post(self.profile_url, self.profile_data(first_name="Alicia"))
        self.assertEqual(response.status_code, 302)
        member.refresh_from_db()
        self.assertEqual(member.first_name, "Alicia")
        self.assertEqual(Member.objects.filter(family__account=self.admin).count(), 1)
        self.assertTrue(self.admin.is_superuser)

    def test_official_can_link_existing_own_member_without_creating_duplicate(self):
        User.objects.filter(pk=self.family_user.pk).update(is_competition_official=True)
        self.family_user.refresh_from_db()
        self.client.force_login(self.family_user)
        response = self.client.post(self.profile_url, {"existing_member": str(self.member.pk)})
        self.assertEqual(response.status_code, 302)
        self.member.refresh_from_db()
        self.assertEqual(self.member.user_id, self.family_user.pk)
        self.assertEqual(self.family.members.count(), 1)
        self.family_user.refresh_from_db()
        self.assertFalse(self.family_user.is_superuser)
        self.assertFalse(self.family_user.is_staff)

    def test_foreign_and_archived_members_cannot_be_linked(self):
        User.objects.filter(pk=self.family_user.pk).update(is_competition_official=True)
        self.family_user.refresh_from_db()
        self.client.force_login(self.family_user)
        self.member.archived_at = timezone.now()
        self.member.save(update_fields=("archived_at",))
        for member in (self.other_member, self.member):
            with self.subTest(member=member.pk):
                response = self.client.post(
                    self.profile_url, self.profile_data(existing_member=str(member.pk))
                )
                self.assertEqual(response.status_code, 200)
        self.assertFalse(Member.objects.filter(user=self.family_user).exists())
        self.other_member.refresh_from_db()
        self.assertIsNone(self.other_member.user_id)

    def test_regular_family_and_organizer_without_official_profile_are_denied(self):
        for actor in (self.family_user, self.organizer):
            self.client.force_login(actor)
            for name, args in (
                ("personal_participation", []),
                ("personal_profile", []),
                ("personal_registration", [self.day.pk]),
            ):
                with self.subTest(actor=actor.pk, name=name):
                    self.assertEqual(self.client.get(reverse(name, args=args)).status_code, 403)
                    self.assertEqual(self.client.post(reverse(name, args=args), {}).status_code, 403)
        self.assertFalse(Member.objects.filter(user__isnull=False).exists())

    def test_anonymous_user_is_redirected_to_login(self):
        self.client.logout()
        for name, args in (
            ("personal_participation", []),
            ("personal_profile", []),
            ("personal_registration", [self.day.pk]),
        ):
            with self.subTest(name=name):
                response = self.client.get(reverse(name, args=args))
                self.assertEqual(response.status_code, 302)
                self.assertIn(reverse("login"), response.url)

    def test_admin_must_complete_profile_before_registering(self):
        url = reverse("personal_registration", args=[self.day.pk])
        response = self.client.post(url, {
            "availability": "full_day", "no_meal": "on",
            "station_slots": [str(self.morning_slot.pk)],
        })
        self.assertRedirects(response, self.profile_url)
        self.assertFalse(Family.objects.filter(account=self.admin).exists())
        self.assertFalse(Registration.objects.filter(member__user=self.admin).exists())


class PersonalParticipationTests(DataMixin, TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(
            email="participant-admin@example.test", password="Strong-pass-123",
        )
        self.personal_family = Family.objects.create(account=self.admin, name="Administrateur")
        self.personal_member = Member.objects.create(
            family=self.personal_family, user=self.admin,
            first_name="Alice", last_name="Administrateur", birth_date=date(1985, 5, 10),
        )
        self.full_meal = Menu.objects.create(
            event_day=self.day, name="Repas complet",
            meal_type=Menu.MealType.FULL_MEAL, surcharge_cents=350,
        )
        self.snack = Menu.objects.create(
            event_day=self.day, name="Collation", meal_type=Menu.MealType.SNACK,
        )
        self.drink = Beverage.objects.create(event_day=self.day, name="Jus", surcharge_cents=50)
        self.client.force_login(self.admin)
        self.url = reverse("personal_registration", args=[self.day.pk])

    def post_presence(self, **extra):
        data = {
            "availability": Registration.Availability.FULL_DAY,
            "menu": str(self.full_meal.pk), "beverage": str(self.drink.pk),
            "payment_method": MealOrder.PaymentMethod.SUMUP,
            "station_slots": [str(self.morning_slot.pk)], **extra,
        }
        return self.client.post(self.url, data)

    def as_official(self, *, organizer=False):
        User.objects.filter(pk=self.family_user.pk).update(
            is_competition_official=True, is_organizer=organizer, is_staff=organizer,
        )
        self.family_user.refresh_from_db()
        self.member.user = self.family_user
        self.member.save(update_fields=("user",))
        self.client.force_login(self.family_user)
        return self.family_user

    def test_admin_joins_event_and_two_posts_with_confirmed_assignments(self):
        response = self.post_presence(station_slots=[str(self.morning_slot.pk), str(self.afternoon_slot.pk)])
        self.assertEqual(response.status_code, 302)
        registration = Registration.objects.get(member=self.personal_member, event_day=self.day)
        self.assertEqual(registration.status, Registration.Status.ACTIVE)
        assignments = list(registration.assignments.all())
        self.assertEqual(len(assignments), 2)
        for assignment in assignments:
            self.assertEqual(assignment.status, Assignment.Status.CONFIRMED)
            self.assertEqual(assignment.confirmed_by_id, self.admin.pk)
            self.assertIsNotNone(assignment.confirmed_at)
        self.assertEqual(registration.meal_order.total_surcharge_cents, 400)
        self.assertEqual(registration.meal_order.payment_method, MealOrder.PaymentMethod.SUMUP)
        self.assertFalse(Payment.objects.exists())
        # Admin rights do not require also having the official profile enabled.
        self.assertFalse(self.admin.is_competition_official)
        self.assertContains(self.client.get(reverse("personal_participation")), self.event.name)

    def test_admin_can_join_without_a_post_and_choose_no_meal(self):
        response = self.post_presence(station_slots=[], no_meal="on", menu="", beverage="")
        self.assertEqual(response.status_code, 302)
        registration = Registration.objects.get(member=self.personal_member, event_day=self.day)
        self.assertFalse(registration.assignments.exists())
        self.assertFalse(registration.meal_order.wants_meal)
        self.assertEqual(registration.meal_order.total_surcharge_cents, 0)
        self.assertFalse(Payment.objects.exists())

    def test_identical_submission_preserves_confirmation_and_creates_no_duplicates(self):
        self.assertEqual(self.post_presence().status_code, 302)
        before = Assignment.objects.get(registration__member=self.personal_member)
        self.assertEqual(self.post_presence(note="Remarque mise à jour").status_code, 302)
        after = Assignment.objects.get(registration__member=self.personal_member)
        self.assertEqual(after.pk, before.pk)
        self.assertEqual(after.confirmed_at, before.confirmed_at)
        self.assertEqual(after.confirmed_by_id, before.confirmed_by_id)
        self.assertEqual(Registration.objects.filter(member=self.personal_member).count(), 1)
        self.assertEqual(MealOrder.objects.filter(registration__member=self.personal_member).count(), 1)

    def test_inactive_confirmed_post_stays_visible_and_is_kept_when_note_changes(self):
        self.assertEqual(self.post_presence().status_code, 302)
        assignment = Assignment.objects.get(registration__member=self.personal_member)
        confirmed_at = assignment.confirmed_at
        self.station.is_active = False
        self.station.save(update_fields=("is_active",))

        response = self.client.get(self.url)
        self.assertContains(response, f'value="{self.morning_slot.pk}"')
        self.assertIn(
            str(self.morning_slot.pk),
            [str(value) for value in response.context["form"]["station_slots"].value()],
        )
        self.assertEqual(self.post_presence(note="Remarque uniquement").status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Assignment.Status.CONFIRMED)
        self.assertEqual(assignment.confirmed_at, confirmed_at)
        self.assertIsNone(assignment.cancelled_at)
        self.assertEqual(Assignment.objects.filter(registration__member=self.personal_member).count(), 1)
        self.assertEqual(Registration.objects.get(member=self.personal_member).note, "Remarque uniquement")

    def test_unchecking_inactive_confirmed_post_cancels_it_with_history(self):
        self.assertEqual(self.post_presence().status_code, 302)
        assignment = Assignment.objects.get(registration__member=self.personal_member)
        confirmed_at = assignment.confirmed_at
        self.station.is_active = False
        self.station.save(update_fields=("is_active",))

        self.assertEqual(self.post_presence(station_slots=[]).status_code, 302)
        assignment.refresh_from_db()
        self.assertEqual(assignment.status, Assignment.Status.CANCELLED)
        self.assertEqual(assignment.confirmed_at, confirmed_at)
        self.assertIsNotNone(assignment.cancelled_at)
        self.assertEqual(assignment.cancelled_by_id, self.admin.pk)
        self.assertEqual(Assignment.objects.filter(registration__member=self.personal_member).count(), 1)
        self.assertEqual(self.morning_slot.remaining_capacity, 1)

    def test_inactive_post_cannot_receive_a_new_personal_assignment(self):
        from volunteers.self_participation import save_personal_participation

        self.station.is_active = False
        self.station.save(update_fields=("is_active",))
        response = self.client.get(self.url)
        self.assertNotContains(response, f'value="{self.morning_slot.pk}"')
        self.assertEqual(self.post_presence().status_code, 200)
        with self.assertRaisesMessage(BusinessRuleError, "Ce poste est inactif"):
            save_personal_participation(
                actor=self.admin, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                station_slots=[self.morning_slot], meal_choices={"no_meal": True},
            )
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())
        self.assertFalse(MealOrder.objects.filter(registration__member=self.personal_member).exists())
        self.assertFalse(Assignment.objects.filter(registration__member=self.personal_member).exists())

    def test_replacing_and_unchecking_post_preserves_cancellation_history(self):
        self.assertEqual(self.post_presence().status_code, 302)
        first = Assignment.objects.get(registration__member=self.personal_member)
        self.assertEqual(
            self.post_presence(station_slots=[str(self.afternoon_slot.pk)]).status_code, 302
        )
        first.refresh_from_db()
        self.assertEqual(first.status, Assignment.Status.CANCELLED)
        self.assertEqual(first.cancelled_by_id, self.admin.pk)
        self.assertIsNotNone(first.cancelled_at)
        self.assertIsNotNone(first.confirmed_at)
        replacement = Assignment.objects.get(
            registration__member=self.personal_member, status=Assignment.Status.CONFIRMED
        )
        self.assertEqual(replacement.station_slot_id, self.afternoon_slot.pk)
        self.assertEqual(self.post_presence(station_slots=[]).status_code, 302)
        replacement.refresh_from_db()
        self.assertEqual(replacement.status, Assignment.Status.CANCELLED)
        self.assertEqual(Assignment.objects.filter(registration__member=self.personal_member).count(), 2)
        self.assertEqual(Registration.objects.get(member=self.personal_member).status, Registration.Status.ACTIVE)

    def test_full_slot_rolls_back_new_registration_meal_and_assignments(self):
        occupied = create_assignment(
            registration=self.registration(), station_slot=self.morning_slot, actor=self.organizer,
        )
        confirm_assignment(assignment=occupied, confirmed_by=self.organizer)
        self.assertEqual(self.post_presence().status_code, 200)
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())
        self.assertFalse(MealOrder.objects.filter(registration__member=self.personal_member).exists())
        occupied.refresh_from_db()
        self.assertEqual(occupied.status, Assignment.Status.CONFIRMED)

    def test_failed_replacement_preserves_existing_confirmation_and_meal(self):
        self.assertEqual(
            self.post_presence(station_slots=[str(self.afternoon_slot.pk)]).status_code, 302
        )
        previous = Assignment.objects.get(registration__member=self.personal_member)
        order = MealOrder.objects.get(registration__member=self.personal_member)
        create_assignment(
            registration=self.registration(), station_slot=self.morning_slot, actor=self.organizer,
        )
        self.assertEqual(self.post_presence(note="Ne doit pas être enregistré").status_code, 200)
        previous.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(previous.status, Assignment.Status.CONFIRMED)
        self.assertIsNone(previous.cancelled_at)
        self.assertEqual(order.menu_id, self.full_meal.pk)
        self.assertEqual(Registration.objects.get(member=self.personal_member).note, "")
        self.assertEqual(Assignment.objects.filter(registration__member=self.personal_member).count(), 1)

    def test_bad_meal_service_call_rolls_back_presence_and_posts(self):
        from volunteers.self_participation import save_personal_participation

        with self.assertRaises(BusinessRuleError):
            save_personal_participation(
                actor=self.admin, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                station_slots=[self.morning_slot],
                meal_choices={"menu": self.snack, "payment_method": "counter"},
            )
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())
        self.assertFalse(Assignment.objects.filter(registration__member=self.personal_member).exists())
        self.assertFalse(MealOrder.objects.filter(registration__member=self.personal_member).exists())

    def test_foreign_day_and_inactive_posts_are_rejected(self):
        other_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        other_slot = StationSlot.objects.create(
            station=self.station, event_day=other_day,
            period=StationSlot.Period.MORNING, capacity=1,
        )
        self.other_station.is_active = False
        self.other_station.save(update_fields=("is_active",))
        inactive_slot = StationSlot.objects.create(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.MORNING, capacity=1,
        )
        for slot in (other_slot, inactive_slot):
            with self.subTest(slot=slot.pk):
                self.assertEqual(self.post_presence(station_slots=[str(slot.pk)]).status_code, 200)
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())

    def test_post_without_clock_times_can_be_displayed_and_selected(self):
        slot = StationSlot.objects.create(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.MORNING, capacity=1,
        )
        self.assertContains(self.client.get(self.url), self.other_station.name)
        self.assertEqual(self.post_presence(station_slots=[str(slot.pk)]).status_code, 302)
        self.assertEqual(
            Assignment.objects.get(registration__member=self.personal_member).station_slot_id,
            slot.pk,
        )

    def test_half_day_cannot_be_assigned_day_post_and_day_cannot_overlap_half(self):
        full_day_slot = StationSlot.objects.create(
            station=self.other_station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=1,
            start_time=time(19, 0), end_time=time(20, 0),
        )
        response = self.post_presence(
            availability=Registration.Availability.MORNING,
            menu=str(self.snack.pk), station_slots=[str(full_day_slot.pk)],
        )
        self.assertEqual(response.status_code, 200)
        response = self.post_presence(
            station_slots=[str(full_day_slot.pk), str(self.morning_slot.pk)],
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())

    def test_admin_can_register_before_opening_and_after_meal_deadline(self):
        self.event.registration_opens_at = timezone.now() + timedelta(days=1)
        self.event.registration_closes_at = timezone.now() + timedelta(days=2)
        self.event.meal_orders_close_at = timezone.now() - timedelta(minutes=1)
        self.event.save()
        self.assertEqual(self.post_presence().status_code, 302)
        registration = Registration.objects.get(member=self.personal_member)
        self.assertEqual(registration.meal_order.menu_id, self.full_meal.pk)
        self.assertEqual(registration.assignments.get().status, Assignment.Status.CONFIRMED)

    def test_official_without_admin_rights_can_register_but_not_assign_self(self):
        official = self.as_official()
        response = self.post_presence(station_slots=[])
        self.assertEqual(response.status_code, 302)
        registration = Registration.objects.get(member=self.member, event_day=self.day)
        self.assertEqual(registration.meal_order.menu_id, self.full_meal.pk)
        response = self.post_presence(station_slots=[str(self.morning_slot.pk)])
        self.assertIn(response.status_code, (200, 302, 403))
        self.assertFalse(Assignment.objects.filter(registration__member=self.member).exists())
        official.refresh_from_db()
        self.assertFalse(official.is_superuser)
        self.assertFalse(official.is_staff)
        self.assertFalse(Payment.objects.exists())

    def test_official_cannot_bypass_registration_or_meal_deadlines(self):
        self.as_official()
        self.event.registration_opens_at = timezone.now() + timedelta(days=1)
        self.event.registration_closes_at = timezone.now() + timedelta(days=2)
        self.event.save()
        self.assertEqual(self.post_presence(station_slots=[]).status_code, 200)
        self.assertFalse(Registration.objects.filter(member=self.member).exists())
        self.event.registration_opens_at = timezone.now() - timedelta(days=2)
        self.event.meal_orders_close_at = timezone.now() - timedelta(minutes=1)
        self.event.save()
        response = self.post_presence(station_slots=[])
        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(MealOrder.objects.filter(registration__member=self.member).exists())

    def test_direct_service_rejects_forged_official_self_assignment(self):
        from volunteers.self_participation import save_personal_participation

        official = self.as_official()
        with self.assertRaises((PermissionDenied, BusinessRuleError)):
            save_personal_participation(
                actor=official, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                station_slots=[self.morning_slot],
            )
        self.assertFalse(Assignment.objects.filter(registration__member=self.member).exists())

    def test_organizer_official_without_administrator_role_cannot_self_confirm_posts(self):
        from volunteers.self_participation import save_personal_participation

        official = self.as_official(organizer=True)
        with self.assertRaises(PermissionDenied):
            save_personal_participation(
                actor=official, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                station_slots=[self.morning_slot],
            )
        self.assertFalse(Registration.objects.filter(member=self.member).exists())

    def test_service_rechecks_inactive_account_and_revoked_roles_from_database(self):
        from volunteers.self_participation import save_personal_participation

        for update in (
            {"is_active": False},
            {"is_superuser": False, "is_competition_official": False},
        ):
            with self.subTest(update=update):
                User.objects.filter(pk=self.admin.pk).update(**update)
                with self.assertRaises(PermissionDenied):
                    save_personal_participation(
                        actor=self.admin, event_day=self.day,
                        availability=Registration.Availability.FULL_DAY,
                        station_slots=[self.morning_slot],
                    )
                User.objects.filter(pk=self.admin.pk).update(
                    is_active=True, is_superuser=True, is_competition_official=False,
                )
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())

    def test_service_rejects_personal_member_after_family_account_transfer(self):
        from volunteers.self_participation import save_personal_participation

        # A stale relation cached on the actor must never grant access after transfer.
        self.assertEqual(self.admin.participant_profile.pk, self.personal_member.pk)
        recipient = User.objects.create_user(email="recipient@example.test", password="Strong-pass-123")
        Family.objects.filter(pk=self.personal_family.pk).update(account=recipient)
        with self.assertRaises(PermissionDenied):
            save_personal_participation(
                actor=self.admin, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
                station_slots=[self.morning_slot],
            )
        self.assertFalse(Registration.objects.filter(member=self.personal_member).exists())

    def test_direct_service_rejects_unpublished_event_for_official(self):
        from volunteers.self_participation import save_personal_participation

        official = self.as_official()
        self.event.status = Event.Status.DRAFT
        self.event.save(update_fields=("status",))
        with self.assertRaises((PermissionDenied, BusinessRuleError)):
            save_personal_participation(
                actor=official, event_day=self.day,
                availability=Registration.Availability.FULL_DAY,
            )
        self.assertFalse(Registration.objects.filter(member=self.member).exists())


@skipUnlessDBFeature("has_select_for_update")
class ConcurrentPersonalParticipationTests(DataMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.setUpTestData()
        self.admin_ids = []
        for number in range(2):
            admin = User.objects.create_superuser(
                email=f"competing-admin-{number}@example.test", password="Strong-pass-123",
            )
            family = Family.objects.create(account=admin)
            Member.objects.create(
                family=family, user=admin, first_name=f"Admin {number}",
                last_name="Test", birth_date=date(1985, 5, 10),
            )
            self.admin_ids.append(admin.pk)

    def test_concurrent_admin_self_assignment_never_overbooks_capacity(self):
        from volunteers.self_participation import save_personal_participation

        barrier = Barrier(2)

        def register(admin_id):
            close_old_connections()
            try:
                actor = User.objects.get(pk=admin_id)
                day = EventDay.objects.get(pk=self.day.pk)
                slot = StationSlot.objects.get(pk=self.morning_slot.pk)
                barrier.wait(timeout=10)
                save_personal_participation(
                    actor=actor, event_day=day,
                    availability=Registration.Availability.FULL_DAY,
                    station_slots=[slot], meal_choices={"no_meal": True},
                )
                return "registered"
            except BusinessRuleError:
                return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(register, self.admin_ids))
        self.assertCountEqual(results, ["registered", "rejected"])
        self.assertEqual(
            Assignment.objects.filter(station_slot=self.morning_slot, status=Assignment.Status.CONFIRMED).count(),
            1,
        )
        self.assertEqual(Registration.objects.filter(member__user_id__in=self.admin_ids).count(), 1)
        self.assertEqual(MealOrder.objects.filter(registration__member__user_id__in=self.admin_ids).count(), 1)
        self.assertFalse(Payment.objects.exists())
