from datetime import time

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.models import Assignment, Event, EventDay, Registration, Station, StationSlot
from volunteers.services import confirm_assignment, create_assignment

from .factories import DataMixin


class StationScheduleViewsTests(DataMixin, TestCase):
    def setUp(self):
        self.registration_record = self.registration()
        self.morning_assignment = create_assignment(
            registration=self.registration_record, station_slot=self.morning_slot,
            actor=self.organizer,
        )
        self.client.force_login(self.organizer)
        self.url = reverse("station_update", args=[self.station.pk])

    def assign_afternoon(self):
        return create_assignment(
            registration=self.registration_record, station_slot=self.afternoon_slot,
            actor=self.organizer,
        )

    def station_post_data(self, changes, **station_changes):
        response = self.client.get(self.url)
        formset = response.context["formset"]
        data = {
            "name": self.station.name, "description": self.station.description,
            "instructions": self.station.instructions, "category": self.station.category,
            "age_policy": self.station.age_policy, "position": str(self.station.position),
            "is_active": "on", **station_changes,
        }
        for field in formset.management_form:
            data[field.html_name] = field.value()
        for slot_form in formset.forms:
            for field in slot_form:
                if field.name == "DELETE":
                    continue
                value = field.value()
                data[field.html_name] = value if value is not None else ""
            if not slot_form.instance._state.adding:
                for field, value in changes.get(slot_form.instance.period, {}).items():
                    data[slot_form[field].html_name] = value
        return data

    def other_event_assignment(self):
        event = Event.objects.create(
            name="Concours secondaire privé", location="Autre carrière", status=Event.Status.DRAFT,
            registration_opens_at=self.event.registration_opens_at,
            registration_closes_at=self.event.registration_closes_at,
            meal_orders_close_at=self.event.meal_orders_close_at,
        )
        day = EventDay.objects.create(event=event, date=self.day.date)
        station = Station.objects.create(event=event, name="Poste de l’autre concours")
        slot = StationSlot.objects.create(
            event_day=day, station=station, period=StationSlot.Period.MORNING,
            capacity=1, start_time=time(14), end_time=time(16),
        )
        registration = Registration.objects.create(
            event_day=day, member=self.member, availability=Registration.Availability.FULL_DAY,
        )
        assignment = create_assignment(
            registration=registration, station_slot=slot, actor=self.organizer,
        )
        return event, slot, assignment

    def test_conflicting_time_edit_is_shown_on_same_screen_without_saving_any_change(self):
        self.assign_afternoon()
        before_slots = list(StationSlot.objects.order_by("pk").values())
        before_assignments = list(Assignment.objects.order_by("pk").values())
        data = self.station_post_data({"morning": {"end_time": "14:00"}}, name="Nom à ne pas enregistrer")
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "volunteers/organizer/station_form.html")
        self.assertTrue(response.context["formset"].non_form_errors())
        self.assertContains(response, "se chevauchent")
        self.assertContains(response, self.member.full_name)
        self.assertContains(response, self.day.date.strftime("%d/%m/%Y"))
        self.assertContains(response, 'value="14:00"')
        self.assertEqual(list(StationSlot.objects.order_by("pk").values()), before_slots)
        self.assertEqual(list(Assignment.objects.order_by("pk").values()), before_assignments)
        self.station.refresh_from_db()
        self.assertEqual(self.station.name, "Accueil")

    def test_coordinated_time_changes_validate_final_schedule_instead_of_old_peer(self):
        self.assign_afternoon()
        before_assignments = list(Assignment.objects.order_by("pk").values())
        data = self.station_post_data({
            "morning": {"end_time": "14:00"},
            "afternoon": {"start_time": "14:00", "end_time": "19:00"},
        })
        response = self.client.post(self.url, data)
        self.assertRedirects(response, reverse("event_detail", args=[self.event.pk]))
        self.morning_slot.refresh_from_db()
        self.afternoon_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.end_time, time(14))
        self.assertEqual((self.afternoon_slot.start_time, self.afternoon_slot.end_time), (time(14), time(19)))
        self.assertEqual(list(Assignment.objects.order_by("pk").values()), before_assignments)

    def test_touching_times_are_accepted_without_removing_existing_assignments(self):
        self.assign_afternoon()
        response = self.client.post(self.url, self.station_post_data({
            "afternoon": {"start_time": "12:30"},
        }))
        self.assertRedirects(response, reverse("event_detail", args=[self.event.pk]))
        self.afternoon_slot.refresh_from_db()
        self.assertEqual(self.afternoon_slot.start_time, self.morning_slot.end_time)
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.DRAFT).count(), 2)

    def test_time_edit_also_detects_assignment_in_another_event_on_same_date(self):
        other_event, _, _ = self.other_event_assignment()
        response = self.client.post(self.url, self.station_post_data({
            "morning": {"end_time": "15:00"},
        }))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "se chevauchent")
        self.assertContains(response, other_event.name)
        self.morning_slot.refresh_from_db()
        self.assertEqual(self.morning_slot.end_time, time(12, 30))
        self.assertEqual(Assignment.objects.filter(status=Assignment.Status.DRAFT).count(), 2)

    def test_existing_conflicts_are_private_warnings_without_automatic_cancellation(self):
        other_event, other_slot, other_assignment = self.other_event_assignment()
        for assignment in (self.morning_assignment, other_assignment):
            confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        # Représente un ancien conflit, créé avant le contrôle des horaires.
        StationSlot.objects.filter(pk=other_slot.pk).update(start_time=time(12))
        before_assignments = list(Assignment.objects.order_by("pk").values())
        response = self.client.get(reverse("assignment_board_event", args=[self.event.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["schedule_conflicts"]), 1)
        conflict = response.context["schedule_conflicts"][0]
        self.assertEqual(conflict["member_name"], self.member.full_name)
        self.assertContains(response, "Conflits d’horaires à corriger")
        self.assertContains(response, other_event.name)
        self.assertContains(response, "Ces affectations existantes sont conservées.")
        for assignment in (self.morning_assignment, other_assignment):
            self.assertContains(response, reverse("assignment_update", args=[assignment.pk]))
        self.client.logout()
        public = self.client.get(reverse("public_planning", args=[self.event.pk]))
        self.assertEqual(public.status_code, 200)
        self.assertNotIn("schedule_conflicts", public.context)
        self.assertNotContains(public, "Conflits d’horaires à corriger")
        self.assertNotContains(public, other_event.name)
        self.assertNotContains(public, other_slot.station.name)
        self.assertContains(public, self.member.full_name)
        self.assertEqual(list(Assignment.objects.order_by("pk").values()), before_assignments)

    def test_reactivating_archived_event_warns_about_conflicts_and_preserves_assignments(self):
        _, other_slot, other_assignment = self.other_event_assignment()
        for assignment in (self.morning_assignment, other_assignment):
            confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        self.event.status = Event.Status.ARCHIVED
        self.event.save(update_fields=("status",))
        # Pendant l'archivage, les horaires d'un autre événement ont pu changer.
        StationSlot.objects.filter(pk=other_slot.pk).update(start_time=time(12))
        before_assignments = list(Assignment.objects.order_by("pk").values())
        data = {
            "name": self.event.name, "event_date": self.day.date.isoformat(),
            "location": self.event.location, "description": self.event.description,
            "general_instructions": self.event.general_instructions,
            "status": Event.Status.PUBLISHED,
            "morning_hot_beverage_enabled": "on", "afternoon_cold_beverage_enabled": "on",
        }
        for field in ("registration_opens_at", "registration_closes_at", "meal_orders_close_at"):
            data[field] = timezone.localtime(getattr(self.event, field)).strftime("%Y-%m-%dT%H:%M")
        response = self.client.post(reverse("event_update", args=[self.event.pk]), data, follow=True)
        self.assertRedirects(response, reverse("event_detail", args=[self.event.pk]))
        warnings = [str(message) for message in response.context["messages"] if message.level_tag == "warning"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("réactivé avec des conflits d’horaires existants", warnings[0])
        self.assertIn("Postes → Gérer les affectations", warnings[0])
        self.assertContains(response, "Gérer les affectations")
        self.event.refresh_from_db()
        self.assertEqual(self.event.status, Event.Status.PUBLISHED)
        self.assertEqual(list(Assignment.objects.order_by("pk").values()), before_assignments)
