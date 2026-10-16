"""Les rappels présentent les périodes comme des choix dans les mêmes places."""

from copy import deepcopy
from datetime import date, time, timedelta
from unittest.mock import patch

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from volunteers.event_notifications import group_vacancies, notification_preview
from volunteers.models import Assignment, StationSlot
from volunteers.whatsapp_sharing import whatsapp_preview

from .factories import DataMixin


class VacancyGroupingTests(SimpleTestCase):
    def option(self, period, remaining, **changes):
        labels = {"morning": "Matin", "afternoon": "Après-midi", "full_day": "Journée"}
        return {
            "station_id": "station-a", "station_name": "Buvette",
            "date": date(2026, 11, 8), "period_code": period, "period": labels[period],
            "remaining": remaining, "start_time": None, "end_time": None,
            "morning_capacity": 4, "afternoon_capacity": 4,
            "is_official": False, "shared_places": True, **changes,
        }

    def test_shared_options_have_one_capacity_and_preserve_original_values(self):
        vacancies = [self.option("afternoon", 3), self.option("full_day", 3),
                     self.option("morning", 4)]
        before = deepcopy(vacancies)

        groups = group_vacancies(vacancies)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["capacity_label"], "4 places partagées")
        self.assertEqual([(option["period"], option["remaining"]) for option in groups[0]["options"]],
                         [("Matin", 4), ("Après-midi", 3), ("Journée", 3)])
        self.assertEqual(vacancies, before)

    def test_distinct_stations_with_same_name_and_distinct_dates_stay_separate(self):
        groups = group_vacancies([
            self.option("morning", 4),
            self.option("afternoon", 3, station_id="station-b"),
            self.option("morning", 4, date=date(2026, 11, 9)),
        ])

        self.assertEqual(len(groups), 3)
        self.assertEqual([len(group["options"]) for group in groups], [1, 1, 1])

    def test_legacy_options_without_identifiers_or_period_codes_use_name_and_label(self):
        vacancies = [self.option("full_day", 3), self.option("morning", 4)]
        for option in vacancies:
            del option["station_id"]
            del option["period_code"]

        groups = group_vacancies(vacancies)

        self.assertEqual(len(groups), 1)
        self.assertEqual([option["period"] for option in groups[0]["options"]],
                         ["Matin", "Journée"])
        self.assertEqual(group_vacancies([]), [])

    def test_different_capacities_are_explicit_and_nonshared_groups_have_no_shared_label(self):
        groups = group_vacancies([
            self.option("morning", 4, afternoon_capacity=3),
            self.option("full_day", 2, station_id="full-only", shared_places=False),
            self.option("morning", 1, station_id="one-place", morning_capacity=1,
                        afternoon_capacity=1),
        ])

        self.assertEqual(groups[0]["capacity_label"], "4 places le matin · 3 l’après-midi")
        self.assertEqual(groups[1]["capacity_label"], "")
        self.assertFalse(groups[1]["shared_places"])
        self.assertEqual(groups[2]["capacity_label"], "1 place partagée")

    def test_missing_capacity_metadata_does_not_turn_remaining_into_capacity(self):
        option = self.option("morning", 3)
        del option["morning_capacity"]
        del option["afternoon_capacity"]

        self.assertEqual(group_vacancies([option])[0]["capacity_label"], "")


@override_settings(EVENT_NOTIFICATIONS_ENABLED=False, EMAIL_ENABLED=False, SMS_ENABLED=False)
class VacancyGroupingPreviewTests(DataMixin, TestCase):
    def setUp(self):
        self.now = timezone.now()
        StationSlot.objects.filter(pk__in=(self.morning_slot.pk, self.afternoon_slot.pk)).update(
            capacity=4,
        )
        self.full_slot = StationSlot.objects.create(
            station=self.station, event_day=self.day,
            period=StationSlot.Period.FULL_DAY, capacity=4,
            start_time=time(6), end_time=time(18),
        )

    def test_one_afternoon_assignment_is_presented_as_four_shared_places_and_three_choices(self):
        Assignment.objects.create(
            registration=self.registration(), station_slot=self.afternoon_slot,
            status=Assignment.Status.CONFIRMED,
        )

        with patch("volunteers.event_notifications.send_notification_email") as email, patch(
            "volunteers.event_notifications.send_notification_sms"
        ) as sms, CaptureQueriesContext(connection) as queries:
            preview = notification_preview(self.event, "vacancy_reminder", now=self.now)
            whatsapp = whatsapp_preview(
                self.event, "vacancy_reminder", now=self.now, vacancies=preview["vacancies"],
            )

        self.assertEqual(len(preview["vacancy_groups"]), 1)
        group = preview["vacancy_groups"][0]
        self.assertEqual(group["capacity_label"], "4 places partagées")
        self.assertEqual([(option["period"], option["remaining"]) for option in group["options"]],
                         [("Matin", 4), ("Après-midi", 3), ("Journée", 3)])
        self.assertEqual({option["station_id"] for option in group["options"]}, {self.station.pk})
        self.assertEqual(len(preview["vacancies"]), 3)
        for body in (preview["body_text"], whatsapp["body_text"]):
            self.assertEqual(body.count(self.station.name), 1)
            self.assertIn("4 places partagées", body)
            self.assertIn("4 libres", body)
            self.assertIn("3 libres", body)
            self.assertIn("Journée · 06:00–18:00 : 3 possibles dans ces mêmes places", body)
            self.assertLess(body.index("Matin"), body.index("Après-midi"))
            self.assertLess(body.index("Après-midi"), body.index("Journée"))
            self.assertNotIn(self.family_user.email, body)
            self.assertNotIn(self.member.full_name, body)
        self.assertTrue(all(query["sql"].lstrip().upper().startswith("SELECT") for query in queries))
        email.assert_not_called()
        sms.assert_not_called()

    def test_full_day_assignment_reduces_both_half_day_options_without_changing_capacity(self):
        Assignment.objects.create(
            registration=self.registration(), station_slot=self.full_slot,
            status=Assignment.Status.CONFIRMED,
        )

        preview = notification_preview(self.event, "vacancy_reminder", now=self.now)

        group = preview["vacancy_groups"][0]
        self.assertEqual(group["capacity_label"], "4 places partagées")
        self.assertEqual([option["remaining"] for option in group["options"]], [3, 3, 3])

    def test_whatsapp_limits_slots_before_grouping_and_preserves_other_slot_notice(self):
        vacancies = []
        for index in range(3):
            for period in ("afternoon", "full_day", "morning"):
                vacancies.append(VacancyGroupingTests().option(
                    period, 3, station_id=f"station-{index}", station_name=f"Poste {index}",
                    date=self.day.date,
                ))
        vacancies[-1]["period"] = "Option hors limite"

        body = whatsapp_preview(
            self.event, "vacancy_reminder", now=self.now, vacancies=vacancies,
        )["body_text"]

        self.assertEqual(body.count("4 places partagées"), 3)
        self.assertNotIn("Option hors limite", body)
        self.assertEqual(body.count("3 libres") + body.count("3 possibles"), 8)
        self.assertIn("… et 1 autre(s) créneau(x), à consulter sur le site.", body)

    def test_whatsapp_date_limit_is_preserved(self):
        for offset in range(1, 6):
            self.event.days.create(date=self.day.date + timedelta(days=offset))

        body = whatsapp_preview(self.event, "vacancy_reminder", now=self.now)["body_text"]

        self.assertIn("et 2 autre(s) journée(s)", body)
