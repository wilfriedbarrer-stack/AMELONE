from copy import deepcopy
from datetime import timedelta
from html.parser import HTMLParser

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.event_profiles import save_event_profile
from volunteers.models import Event, EventDay, Payment, StationSlot

from .factories import DataMixin


class EditorParser(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.in_editor = False
        self.field_names = []
        self.editor_count = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "event-editor":
            self.in_editor = True
            self.editor_count += 1
        if self.in_editor and tag in {"input", "select", "textarea"} and attrs.get("name"):
            self.field_names.append(attrs["name"])

    def handle_endtag(self, tag):
        if tag == "form":
            self.in_editor = False


class EventWorkspaceErgonomicsTests(DataMixin, TestCase):
    def setUp(self):
        self.profile = save_event_profile(event=self.event, name="Concours réutilisable")
        self.client.force_login(self.organizer)

    def blank_event_data(self, **changes):
        data = {
            "profile": "", "name": "Événement sans modèle", "event_date": "2027-06-13",
            "description": "Présentation", "location": "Carrière", "status": "draft",
            "registration_opens_at": timezone.localtime(self.event.registration_opens_at).strftime("%Y-%m-%dT%H:%M"),
            "registration_closes_at": timezone.localtime(self.event.registration_closes_at).strftime("%Y-%m-%dT%H:%M"),
            "meal_orders_close_at": timezone.localtime(self.event.meal_orders_close_at).strftime("%Y-%m-%dT%H:%M"),
        }
        return {**data, **changes}

    def station_data(self, **changes):
        data = {
            "name": "Jury modèle", "category": "official", "age_policy": "adults_only",
            "position": "1", "is_active": "on", "description": "", "instructions": "",
            "expected_updated_at": self.profile.updated_at.isoformat(),
            "slots-TOTAL_FORMS": "2", "slots-INITIAL_FORMS": "0",
            "slots-MIN_NUM_FORMS": "0", "slots-MAX_NUM_FORMS": "1000",
            "slots-0-day_offset": "0", "slots-0-period": "full_day", "slots-0-capacity": "3",
            "slots-0-start_time": "08:00", "slots-0-end_time": "18:00",
            "slots-1-day_offset": "0", "slots-1-period": "", "slots-1-capacity": "",
            "slots-1-start_time": "", "slots-1-end_time": "",
        }
        return {**data, **changes}

    def test_creation_uses_one_editor_with_one_name_date_and_optional_model(self):
        response = self.client.get(reverse("event_create"))
        parser = EditorParser(response.content.decode())
        self.assertEqual(parser.editor_count, 1)
        for field in ("profile", "name", "event_date"):
            self.assertEqual(parser.field_names.count(field), 1)
        self.assertNotIn("profile-name", parser.field_names)
        self.assertNotIn("profile-event_date", parser.field_names)
        self.assertFalse(response.context["profile_creation_form"].fields["profile"].required)
        self.assertContains(response, "Événement vierge")
        self.assertContains(response, "Inscriptions et échéances")

    def test_model_shortcut_opens_same_editor_with_model_preselected(self):
        url = reverse("event_profile_create", args=[self.profile.pk])
        target = f"{reverse('event_create')}?modele={self.profile.pk}"
        self.assertRedirects(self.client.get(url), target)
        response = self.client.get(target)
        self.assertEqual(str(response.context["profile_creation_form"]["profile"].value()), str(self.profile.pk))
        self.assertEqual(EditorParser(response.content.decode()).editor_count, 1)

    def test_invalid_or_deleted_model_shortcut_returns_not_found(self):
        for value in ("modèle-inexistant", "00000000-0000-0000-0000-000000000001"):
            response = self.client.get(reverse("event_create"), {"modele": value})
            self.assertEqual(response.status_code, 404)

    def test_shared_editor_creates_independent_draft_from_model(self):
        self.registration()
        response = self.client.post(reverse("event_create"), {
            "profile": str(self.profile.pk), "name": "Concours depuis le modèle",
            "event_date": "2027-11-08", "status": "published",
        })
        event = Event.objects.get(name="Concours depuis le modèle")
        self.assertRedirects(response, reverse("event_update", args=[event.pk]))
        self.assertEqual(event.status, Event.Status.DRAFT)
        self.assertEqual(event.days.get().date.isoformat(), "2027-11-08")
        self.assertEqual(event.stations.count(), self.event.stations.count())
        self.assertFalse(event.days.get().registrations.exists())
        self.assertFalse(Payment.objects.filter(event=event).exists())

    def test_shared_editor_creates_blank_event_and_keeps_errors_in_one_form(self):
        response = self.client.post(reverse("event_create"), self.blank_event_data())
        self.assertEqual(response.status_code, 302)
        event = Event.objects.get(name="Événement sans modèle")
        self.assertEqual(event.days.get().date.isoformat(), "2027-06-13")
        self.assertFalse(event.stations.exists())
        response = self.client.post(reverse("event_create"), self.blank_event_data(
            name="Erreur conservée", location="",
        ))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"].errors.keys(), {"location"})
        self.assertContains(response, 'value="Erreur conservée"')
        self.assertEqual(EditorParser(response.content.decode()).field_names.count("name"), 1)
        self.assertFalse(Event.objects.filter(name="Erreur conservée").exists())

    def test_invalid_shared_name_or_date_keeps_the_other_blank_event_fields(self):
        for changes in ({"name": ""}, {"event_date": "date invalide"}):
            with self.subTest(changes=changes):
                data = self.blank_event_data(
                    description="Présentation à conserver", general_instructions="Consigne à conserver",
                    morning_hot_beverage_enabled="on", **changes,
                )
                response = self.client.post(reverse("event_create"), data)
                self.assertEqual(response.status_code, 200)
                form = response.context["form"]
                self.assertTrue(form.is_bound)
                for field in ("location", "description", "general_instructions", "registration_opens_at",
                              "registration_closes_at", "meal_orders_close_at"):
                    self.assertEqual(form[field].value(), data[field])
                self.assertTrue(form["morning_hot_beverage_enabled"].value())
                self.assertFalse(Event.objects.filter(name="Événement sans modèle").exists())

    def test_event_workspace_keeps_four_sections_and_secondary_actions(self):
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertContains(response, 'aria-label="Gestion de l’événement"')
        for route in ("registration_list", "recap_meals", "payments", "event_emails", "event_profile_save", "event_delete"):
            self.assertContains(response, reverse(route, args=[self.event.pk]))
        self.assertContains(response, 'href="' + reverse("event_detail", args=[self.event.pk]) + '#postes" aria-current="page"')
        html = response.content.decode()
        self.assertLess(html.index("<h1>"), html.index("event-workspace-nav"))
        self.assertContains(response, "Autres actions")
        self.assertContains(response, "Gérer les affectations")

    def test_management_home_keeps_an_entry_to_annual_tracking(self):
        response = self.client.get(reverse("organizer_dashboard"))
        self.assertContains(response, reverse("annual_tracking"))
        self.assertContains(response, "Suivi annuel")

    def test_model_editor_uses_clear_model_and_internal_note_names(self):
        for route, args in (("event_profile_list", []), ("event_profile_update", [self.profile.pk]), ("event_profile_new", [])):
            response = self.client.get(reverse(route, args=args))
            self.assertContains(response, "Modèles d’événements")
            self.assertNotContains(response, "Profils d’événements")
        response = self.client.get(reverse("event_profile_update", args=[self.profile.pk]))
        self.assertContains(response, "Note interne (facultative)")
        self.assertContains(response, "Présentation publique proposée")

    def test_single_day_model_hides_day_without_removing_server_validation(self):
        url = reverse("event_profile_station_create", args=[self.profile.pk])
        response = self.client.get(url)
        day = response.context["formset"].forms[0]["day_offset"]
        self.assertTrue(day.is_hidden)
        self.assertEqual(day.value(), 0)
        original = deepcopy(self.profile.snapshot)
        response = self.client.post(url, self.station_data(**{"slots-0-day_offset": "99"}))
        self.assertEqual(response.status_code, 200)
        self.assertIn("day_offset", response.context["formset"].forms[0].errors)
        self.assertContains(response, "Journée du modèle")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.snapshot, original)

    def test_automatic_day_does_not_create_an_unfilled_extra_slot(self):
        url = reverse("event_profile_station_create", args=[self.profile.pk])
        response = self.client.post(url, self.station_data())
        self.assertEqual(response.status_code, 302)
        self.profile.refresh_from_db()
        self.assertEqual(len(self.profile.snapshot["stations"][-1]["slots"]), 1)
        self.assertEqual(self.profile.snapshot["stations"][-1]["slots"][0]["day_offset"], 0)
        self.assertEqual(self.profile.snapshot["stations"][-1]["slots"][0]["period"], StationSlot.Period.FULL_DAY)

    def test_multi_day_model_keeps_selectable_day_offsets(self):
        EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        profile = save_event_profile(event=self.event, name="Modèle sur deux jours")
        response = self.client.get(reverse("event_profile_station_create", args=[profile.pk]))
        day = response.context["formset"].forms[0]["day_offset"]
        self.assertFalse(day.is_hidden)
        self.assertEqual({choice[0] for choice in day.field.choices}, {"", 0, 1})

    def test_family_cannot_use_simplified_event_or_model_editors(self):
        self.client.force_login(self.family_user)
        before = Event.objects.count()
        for route, args in (("event_create", []), ("event_detail", [self.event.pk]), ("event_profile_new", []), ("event_profile_update", [self.profile.pk])):
            url = reverse(route, args=args)
            self.assertEqual(self.client.get(url).status_code, 403)
            self.assertEqual(self.client.post(url, {}).status_code, 403)
        self.assertEqual(Event.objects.count(), before)
