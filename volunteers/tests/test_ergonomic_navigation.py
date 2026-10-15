from datetime import timedelta

from django.test import TestCase
from django.urls import reverse

from volunteers.models import EventDay, User

from .factories import DataMixin


class ErgonomicNavigationTests(DataMixin, TestCase):
    def primary_navigation(self, response):
        return response.content.decode().split('<nav class="primary-nav"', 1)[1].split('</nav>', 1)[0]

    def test_management_navigation_has_one_entry_per_section_and_private_links_follow_rights(self):
        admin = User.objects.create_superuser(email="navigation-admin@example.test", password=None)
        for actor, admin_links in ((self.organizer, False), (admin, True)):
            with self.subTest(actor=actor.email):
                self.client.force_login(actor)
                response = self.client.get(reverse("home"))
                navigation = self.primary_navigation(response)
                for label in ("Accueil gestion", "Événements", "Catalogue"):
                    self.assertEqual(navigation.count(f">{label}</a>"), 1)
                self.assertNotIn(">Administration</a>", navigation)
                self.assertNotIn(">Tableau de bord</a>", navigation)
                self.assertEqual(">Familles</a>" in navigation, admin_links)
                self.assertEqual(">Réglages</a>" in navigation, admin_links)
                self.assertContains(response, '<a class="nav-planning" href="/planning/">Planning public</a>', html=True)

    def test_nested_management_page_marks_events_section_active(self):
        self.client.force_login(self.organizer)
        response = self.client.get(reverse("event_detail", args=[self.event.pk]))
        self.assertEqual(response.context["navigation_section"], "events")
        self.assertIn('href="/organisateur/evenements/" aria-current="page"', self.primary_navigation(response))

    def test_settings_and_families_are_separate_sections(self):
        admin = User.objects.create_superuser(email="section-admin@example.test", password=None)
        self.client.force_login(admin)
        for route, section in (("administration_dashboard", "settings"), ("administrator_families", "families")):
            with self.subTest(route=route):
                response = self.client.get(reverse(route))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.context["navigation_section"], section)
                self.assertIn(f'href="{reverse(route)}" aria-current="page"', self.primary_navigation(response))

    def test_personal_participation_remains_in_account_menu_without_adding_a_primary_entry(self):
        admin = User.objects.create_superuser(email="personal-nav@example.test", password=None)
        self.client.force_login(admin)
        response = self.client.get(reverse("personal_participation"))
        self.assertEqual(response.context["navigation_section"], "participation")
        self.assertNotIn("Ma participation", self.primary_navigation(response))
        self.assertContains(response, '<a href="/participation/" aria-current="page">Ma participation</a>', html=True)
        self.assertContains(response, 'class="account-menu"')

    def test_menu_uses_accessible_controls_and_keeps_links_visible_in_source_without_javascript(self):
        response = self.client.get(reverse("home"))
        self.assertContains(response, 'aria-expanded="false" aria-controls="site-navigation" hidden')
        self.assertContains(response, '<div class="site-navigation" id="site-navigation">')
        self.assertNotContains(response, 'id="site-navigation" hidden')
        self.assertContains(response, 'js/navigation.js')

    def test_family_navigation_keeps_family_events_and_assignments_separate(self):
        self.client.force_login(self.family_user)
        response = self.client.get(reverse("member_list"))
        navigation = self.primary_navigation(response)
        self.assertIn('href="/famille/membres/" aria-current="page">Ma famille</a>', navigation)
        self.assertIn('href="/famille/evenements/">Événements</a>', navigation)
        self.assertIn('href="/famille/affectations/">Mes postes</a>', navigation)
        self.assertNotIn("Réglages", navigation)


class SingleDayPublicPlanningTests(DataMixin, TestCase):
    def url(self):
        return reverse("public_planning", args=[self.event.pk])

    def test_single_date_is_shown_without_redundant_day_selector(self):
        response = self.client.get(self.url())
        self.assertFalse(response.context["show_day_filter"])
        self.assertNotContains(response, 'name="day"')
        self.assertContains(response, "Date de l’événement")
        self.assertContains(response, 'name="event"')
        self.assertContains(response, "Tous les postes")

    def test_multiple_days_keep_the_day_selector(self):
        EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=1))
        response = self.client.get(self.url())
        self.assertTrue(response.context["show_day_filter"])
        self.assertContains(response, 'name="day"')
        self.assertNotContains(response, "Date de l’événement")

    def test_event_date_keeps_french_labels_with_an_english_browser(self):
        response = self.client.get(self.url(), HTTP_ACCEPT_LANGUAGE="en-US")
        self.assertContains(response, "Dimanche 28 février 2027")

    def test_single_day_family_scope_still_preserves_event_and_selected_date(self):
        self.client.force_login(self.family_user)
        response = self.client.get(self.url(), {"scope": "family"})
        self.assertFalse(response.context["show_day_filter"])
        self.assertTrue(response.context["family_scope_active"])
        self.assertEqual(response.context["selected_day"], self.day)
        self.assertContains(response, 'name="scope" value="family"')
        self.assertIn(f"day={self.day.pk}", response.context["all_scope_url"])
