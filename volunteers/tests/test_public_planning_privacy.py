from datetime import date

from django.test import TestCase
from django.urls import reverse

from volunteers.models import (
    Assignment,
    Beverage,
    MealOrder,
    Member,
    Menu,
    Registration,
    Station,
    User,
)
from volunteers.services import confirm_assignment, create_assignment

from .factories import DataMixin


class PublicPlanningPrivacyTests(DataMixin, TestCase):
    def test_confirmed_names_are_public_without_ages_or_other_private_information(self):
        registration = self.registration(note="Renseignements confidentiels 86413")
        assignment = create_assignment(
            registration=registration, station_slot=self.morning_slot, actor=self.organizer,
        )
        confirm_assignment(assignment=assignment, confirmed_by=self.organizer)
        menu = Menu.objects.create(event_day=self.day, name="Repas confidentiel 84629")
        beverage = Beverage.objects.create(event_day=self.day, name="Boisson confidentielle 75319")
        MealOrder.objects.create(
            registration=registration, wants_meal=True, menu=menu, beverage=beverage,
        )

        response = self.client.get(reverse("public_planning", args=[self.event.pk]))

        self.assertContains(response, self.member.full_name)
        for value in (
            self.family_user.email,
            self.family_user.phone,
            self.member.birth_date.isoformat(),
            self.member.birth_date.strftime("%d/%m/%Y"),
            "29 février 2000",
            f"{registration.age_on_event_day} ans",
            registration.note,
            menu.name,
            beverage.name,
        ):
            with self.subTest(private_value=value):
                self.assertNotContains(response, value)

        # Une liste explicite de clés évite qu'une future évolution transmette
        # un membre, une date de naissance ou un indicateur d'âge au gabarit public.
        public_keys = {
            "slot_id", "station_name", "station_category", "category", "category_label",
            "is_official", "name", "description", "period", "schedule", "time_label",
            "names", "people", "capacity", "taken", "remaining", "is_full", "fill_percent",
        }
        for row in response.context["rows"]:
            self.assertEqual(set(row), public_keys)
            self.assertTrue(all(isinstance(person, str) for person in row["people"]))
            self.assertTrue(all(isinstance(person, str) for person in row["names"]))
            self.assertIsInstance(row["is_official"], bool)

    def test_draft_cancelled_and_inactive_registration_names_are_not_public(self):
        self.morning_slot.capacity = 4
        self.morning_slot.save(update_fields=["capacity"])
        for number, (assignment_status, registration_status) in enumerate((
            (Assignment.Status.DRAFT, Registration.Status.ACTIVE),
            (Assignment.Status.CANCELLED, Registration.Status.ACTIVE),
            (Assignment.Status.CONFIRMED, Registration.Status.CANCELLED),
        )):
            member = Member.objects.create(
                family=self.family, first_name=f"Nonpubliable{number}",
                last_name="Confidentiel", birth_date=date(1990, 1, 1),
            )
            registration = Registration.objects.create(
                member=member, event_day=self.day,
                availability=Registration.Availability.FULL_DAY, status=registration_status,
            )
            # Ces états couvrent également les données historiques : la vue doit
            # filtrer les deux statuts même si aucun formulaire ne produit ce cas.
            Assignment.objects.bulk_create([Assignment(
                registration=registration, station_slot=self.morning_slot,
                period=self.morning_slot.period, status=assignment_status,
            )])

        response = self.client.get(reverse("public_planning", args=[self.event.pk]))

        for number in range(3):
            self.assertNotContains(response, f"Nonpubliable{number}")
        self.assertTrue(all(not row["people"] for row in response.context["rows"]))

    def test_only_official_posts_display_license_and_training_notice(self):
        self.station.category = Station.Category.OFFICIAL
        self.station.save(update_fields=["category"])

        response = self.client.get(reverse("public_planning", args=[self.event.pk]))

        self.assertContains(response, "Licence et formation à jour nécessaires.", count=2)
        self.assertTrue(all(row["is_official"] for row in response.context["rows"]))
        self.station.category = Station.Category.VOLUNTEER
        self.station.save(update_fields=["category"])
        response = self.client.get(reverse("public_planning", args=[self.event.pk]))
        self.assertNotContains(response, "Licence et formation à jour nécessaires.")
        self.assertTrue(all(not row["is_official"] for row in response.context["rows"]))


class PublicPlanningVisibilityTests(DataMixin, TestCase):
    def test_planning_is_a_prominent_navigation_and_home_link_for_every_role(self):
        admin = User.objects.create_superuser(email="planning-admin@example.test", password=None)
        official = User.objects.create_user(
            email="planning-official@example.test", is_competition_official=True,
        )
        combined = User.objects.create_superuser(
            email="planning-combined@example.test", password=None, is_competition_official=True,
        )
        for actor in (None, self.family_user, self.organizer, official, admin, combined):
            with self.subTest(role=actor.pk if actor else "visiteur"):
                self.client.logout()
                if actor:
                    self.client.force_login(actor)

                response = self.client.get(reverse("home"))

                self.assertContains(
                    response,
                    '<a class="nav-planning" href="/planning/">Planning public</a>',
                    html=True,
                )
                self.assertContains(
                    response,
                    '<a class="button button-primary" href="/planning/">'
                    'Consulter le planning public</a>',
                    html=True,
                )
                planning = self.client.get(reverse("public_planning"))
                self.assertEqual(planning.status_code, 200)
                self.assertContains(
                    planning,
                    '<a class="nav-planning" href="/planning/" aria-current="page">'
                    'Planning public</a>',
                    html=True,
                )
