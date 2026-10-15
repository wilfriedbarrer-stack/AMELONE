from datetime import date, timedelta
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse

from volunteers.models import Assignment, Event, EventDay, Family, Member, Registration
from volunteers.services import confirm_assignment, create_assignment

from .factories import DataMixin


class PublicPlanningFamilyFilterTests(DataMixin, TestCase):
    def setUp(self):
        self.url = reverse("public_planning", args=[self.event.pk])
        self.client.force_login(self.family_user)

    def confirmed(self, member=None, slot=None):
        member = member or self.member
        slot = slot or self.morning_slot
        registration, _ = Registration.objects.get_or_create(
            member=member, event_day=slot.event_day,
            defaults={"availability": Registration.Availability.FULL_DAY},
        )
        assignment = create_assignment(
            registration=registration, station_slot=slot, actor=self.organizer,
        )
        return confirm_assignment(assignment=assignment, confirmed_by=self.organizer)

    def family_view(self, **params):
        return self.client.get(self.url, {"scope": "family", **params})

    def test_toggle_shows_only_family_confirmed_slots_and_keeps_all_view_available(self):
        self.confirmed()
        self.confirmed(self.other_member, self.afternoon_slot)
        public = self.client.get(self.url)
        family = self.family_view()
        self.assertEqual({row["slot_id"] for row in public.context["rows"]}, {
            self.morning_slot.pk, self.afternoon_slot.pk,
        })
        self.assertEqual([row["slot_id"] for row in family.context["rows"]], [self.morning_slot.pk])
        self.assertContains(family, self.member.full_name)
        self.assertNotContains(family, self.other_member.full_name)
        self.assertContains(family, "Tous les postes")
        self.assertContains(family, "Uniquement les postes de ma famille")
        self.assertTrue(family.context["family_scope_active"])
        self.assertTrue(family.context["has_family"])
        self.assertContains(self.client.get(family.context["all_scope_url"]), self.other_member.full_name)

    def test_family_filter_keeps_the_whole_team_and_actual_capacity_of_visible_slot(self):
        self.morning_slot.capacity = 4
        self.morning_slot.save(update_fields=["capacity"])
        self.confirmed()
        self.confirmed(self.other_member)
        family = self.family_view()
        row = family.context["rows"][0]
        self.assertEqual(row["taken"], 2)
        self.assertEqual(row["remaining"], 2)
        self.assertEqual(set(row["people"]), {self.member.full_name, self.other_member.full_name})
        self.assertEqual(row, next(row for row in self.client.get(self.url).context["rows"]
                                   if row["slot_id"] == self.morning_slot.pk))

    def test_two_confirmed_family_members_do_not_duplicate_the_slot(self):
        self.morning_slot.capacity = 4
        self.morning_slot.save(update_fields=["capacity"])
        sibling = Member.objects.create(
            family=self.family, first_name="Maël", last_name="Durand", birth_date=date(1999, 1, 1),
        )
        self.confirmed()
        self.confirmed(sibling)
        response = self.family_view()
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["taken"], 2)

    def test_family_draft_does_not_match_an_unrelated_confirmed_assignment(self):
        self.morning_slot.capacity = 3
        self.morning_slot.save(update_fields=["capacity"])
        create_assignment(
            registration=self.registration(), station_slot=self.morning_slot, actor=self.organizer,
        )
        self.confirmed(self.other_member)
        response = self.family_view()
        self.assertEqual(response.context["rows"], [])
        self.assertNotContains(response, self.other_member.full_name)
        self.assertNotContains(response, self.member.full_name)

    def test_cancelled_assignment_or_registration_cannot_make_a_family_slot_visible(self):
        assignment = self.confirmed()
        for cancelled_registration in (False, True):
            with self.subTest(cancelled_registration=cancelled_registration):
                Assignment.objects.filter(pk=assignment.pk).update(
                    status=Assignment.Status.CONFIRMED if cancelled_registration else Assignment.Status.CANCELLED,
                )
                Registration.objects.filter(pk=assignment.registration_id).update(
                    status=Registration.Status.CANCELLED if cancelled_registration else Registration.Status.ACTIVE,
                )
                self.assertEqual(self.family_view().context["rows"], [])

    def test_requested_family_or_member_ids_cannot_switch_to_another_family(self):
        self.confirmed()
        self.confirmed(self.other_member, self.afternoon_slot)
        response = self.family_view(family=self.other_family.pk, member=self.other_member.pk,
                                    account=self.other_user.pk)
        self.assertEqual([row["slot_id"] for row in response.context["rows"]], [self.morning_slot.pk])
        self.assertNotContains(response, self.other_member.full_name)
        self.assertNotIn(str(self.other_family.pk), response.context["family_scope_url"])

    def test_guest_can_see_all_posts_and_family_button_connects_then_returns_to_same_selection(self):
        self.confirmed()
        self.client.logout()
        response = self.client.get(self.url, {"day": self.day.pk})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.member.full_name)
        login_url = urlsplit(response.context["family_scope_url"])
        self.assertEqual(login_url.path, reverse("login"))
        target = parse_qs(login_url.query)["next"][0]
        self.assertEqual(parse_qs(urlsplit(target).query), {
            "event": [str(self.event.pk)], "day": [str(self.day.pk)], "scope": ["family"],
        })
        direct = self.family_view(day=self.day.pk)
        self.assertRedirects(direct, response.context["family_scope_url"], fetch_redirect_response=False)
        self.client.force_login(self.family_user)
        self.assertTrue(self.client.get(target).context["family_scope_active"])

    def test_no_family_account_has_an_empty_state_without_creating_a_family(self):
        before = Family.objects.count()
        self.client.force_login(self.organizer)
        response = self.family_view()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["has_family"])
        self.assertEqual(response.context["rows"], [])
        self.assertEqual(Family.objects.count(), before)
        self.assertContains(response, "famille")
        self.assertEqual(len(self.client.get(response.context["all_scope_url"]).context["rows"]), 2)

    def test_administrator_with_own_family_uses_the_same_filter(self):
        self.family_user.is_staff = self.family_user.is_superuser = True
        self.family_user.save(update_fields=["is_staff", "is_superuser"])
        self.confirmed()
        self.confirmed(self.other_member, self.afternoon_slot)
        response = self.family_view()
        self.assertEqual([row["slot_id"] for row in response.context["rows"]], [self.morning_slot.pk])

    def test_event_and_day_selections_preserve_scope_but_event_change_resets_the_day(self):
        self.confirmed()
        next_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        response = self.family_view(day=next_day.pk)
        for key, scope in (("all_scope_url", "all"), ("family_scope_url", "family")):
            self.assertEqual(parse_qs(urlsplit(response.context[key]).query), {
                "event": [str(self.event.pk)], "day": [str(next_day.pk)], "scope": [scope],
            })
        forms = [part.split("</form>", 1)[0] for part in response.content.decode().split('<form method="get"')[1:]]
        self.assertEqual(len(forms), 2)
        for form in forms:
            self.assertIn('name="scope" value="family"', form)
        self.assertNotIn('name="day"', forms[0])
        another_event = Event.objects.create(
            name="Autre concours", status=Event.Status.PUBLISHED,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        other_day = EventDay.objects.create(event=another_event, date=next_day.date)
        changed = self.client.get(reverse("public_planning"), {"event": another_event.pk, "scope": "family"})
        self.assertEqual(changed.context["selected_day"], other_day)
        self.assertTrue(changed.context["family_scope_active"])
        self.assertEqual(self.family_view(day=other_day.pk).status_code, 404)

    def test_unpublished_or_inactive_posts_remain_hidden_in_family_scope(self):
        self.confirmed()
        self.station.is_active = False
        self.station.save(update_fields=["is_active"])
        self.assertEqual(self.family_view().context["rows"], [])
        self.event.status = Event.Status.DRAFT
        self.event.save(update_fields=["status"])
        self.assertEqual(self.family_view().status_code, 404)

    def test_family_response_is_not_shared_cached_and_private_information_stays_absent(self):
        registration = self.registration(note="Remarque privée 97423")
        assignment = create_assignment(
            registration=registration, station_slot=self.morning_slot, actor=self.organizer,
        )
        confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        response = self.family_view()
        self.assertIn("private", response.headers["Cache-Control"])
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertIn("Cookie", response.headers["Vary"])
        for private_value in (self.family_user.email, self.family_user.phone,
                              self.member.birth_date.isoformat(), registration.note):
            self.assertNotContains(response, private_value)
        row = response.context["rows"][0]
        self.assertTrue(all(isinstance(person, str) for person in row["people"]))
        self.assertNotIn("registration", row)
        self.assertNotIn("member", row)
        self.client.force_login(self.other_user)
        self.assertEqual(self.family_view().context["rows"], [])

    def test_unknown_scope_falls_back_to_the_public_all_posts_view(self):
        self.confirmed(self.other_member)
        response = self.client.get(self.url, {"scope": "someone-else"})
        self.assertFalse(response.context["family_scope_active"])
        self.assertEqual(len(response.context["rows"]), 2)
        self.assertContains(response, self.other_member.full_name)
