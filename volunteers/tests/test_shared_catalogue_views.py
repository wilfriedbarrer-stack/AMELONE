import io
from datetime import timedelta

from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from openpyxl import Workbook

from volunteers.catalogue_import import (
    apply_catalogue_preview,
    parse_catalogue_upload,
    prepare_catalogue_preview,
)
from volunteers.forms_family import FamilyRegistrationForm
from volunteers.meal_catalogue import beverage_options_for_day, menu_options_for_day
from volunteers.models import Beverage, Event, EventDay, MealOrder, Menu, Registration
from volunteers.shared_catalogue import (
    promote_local_option,
    save_shared_option,
    shared_option_version,
)
from volunteers.views_shared_catalogue import PREVIEW_SESSION_KEY

from .factories import DataMixin


def csv_upload(content):
    return SimpleUploadedFile("catalogue.csv", content.encode("utf-8-sig"), content_type="text/csv")


class SharedCatalogueViewsTests(DataMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.second_event = Event.objects.create(
            name="Autre rencontre", location="Club",
            registration_opens_at=cls.event.registration_opens_at,
            registration_closes_at=cls.event.registration_closes_at,
            meal_orders_close_at=cls.event.meal_orders_close_at,
        )
        cls.second_day = EventDay.objects.create(event=cls.second_event, date=cls.day.date + timedelta(days=2))

    def setUp(self):
        self.client.force_login(self.organizer)

    def menu_data(self, **extra):
        return {
            "name": "Repas commun", "description": "Plat et dessert", "meal_type": "full_meal",
            "surcharge_euros": "7.50", "is_active": "on", **extra,
        }

    def test_global_creation_applies_to_existing_and_future_event_days_without_copies(self):
        url = reverse("shared_catalogue_create", args=["menus"])
        response = self.client.post(url, self.menu_data(event_day=str(self.day.pk)))
        self.assertRedirects(response, reverse("shared_catalogue"))
        menu = Menu.objects.get(name="Repas commun")
        self.assertIsNone(menu.event_day_id)
        self.assertEqual(menu.surcharge_cents, 750)
        future_day = EventDay.objects.create(event=self.event, date=self.day.date + timedelta(days=100))
        for day in (self.day, self.second_day, future_day):
            self.assertIn(menu, menu_options_for_day(day))
        self.assertEqual(Menu.objects.filter(name=menu.name).count(), 1)

    def test_beverage_create_edit_and_disable_keep_same_record_across_events(self):
        response = self.client.post(reverse("shared_catalogue_create", args=["boissons"]), {
            "name": "Eau commune", "description": "Eau plate", "surcharge_euros": "1.50", "is_active": "on",
        })
        self.assertEqual(response.status_code, 302)
        beverage = Beverage.objects.get(name="Eau commune")
        self.assertIn(beverage, beverage_options_for_day(self.second_day))
        response = self.client.post(reverse("shared_catalogue_update", args=["boissons", beverage.pk]), {
            "name": "Eau mise à jour", "description": "Nouvelle description", "surcharge_euros": "2.25",
            "version": shared_option_version(beverage),
        })
        self.assertEqual(response.status_code, 302)
        beverage.refresh_from_db()
        self.assertEqual(beverage.surcharge_cents, 225)
        self.assertFalse(beverage.is_active)
        self.assertNotIn(beverage, beverage_options_for_day(self.day))
        self.assertNotIn(beverage, beverage_options_for_day(self.second_day))
        self.assertTrue(Beverage.objects.filter(pk=beverage.pk).exists())

    def test_price_edit_preserves_existing_orders_and_changes_only_future_price(self):
        menu = Menu.objects.create(name="Repas commun", event_day=None, surcharge_cents=750)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=menu)
        self.assertEqual(order.menu_surcharge_cents, 750)
        response = self.client.post(reverse("shared_catalogue_update", args=["menus", menu.pk]),
                                    self.menu_data(surcharge_euros="9.50", version=shared_option_version(menu)))
        self.assertEqual(response.status_code, 302)
        menu.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(menu.surcharge_cents, 950)
        self.assertEqual(order.menu_id, menu.pk)
        self.assertEqual(order.menu_surcharge_cents, 750)
        self.assertEqual(order.total_surcharge_cents, 750)
        next_registration = Registration.objects.create(member=self.other_member, event_day=self.second_day, availability="full_day")
        next_order = MealOrder.objects.create(registration=next_registration, wants_meal=True, menu=menu)
        self.assertEqual(next_order.total_surcharge_cents, 950)

    def test_disabling_a_global_menu_preserves_order_and_hides_future_choice(self):
        menu = Menu.objects.create(name="Repas commun", event_day=None, surcharge_cents=750)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=menu)
        data = self.menu_data(version=shared_option_version(menu))
        data.pop("is_active")
        response = self.client.post(reverse("shared_catalogue_update", args=["menus", menu.pk]), data)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn(menu, menu_options_for_day(self.day))
        order.refresh_from_db()
        self.assertEqual(order.menu_id, menu.pk)
        self.assertEqual(order.total_surcharge_cents, 750)

    def test_same_name_is_allowed_for_two_global_menu_types_and_conflicts_are_explicit(self):
        self.client.post(reverse("shared_catalogue_create", args=["menus"]), self.menu_data())
        response = self.client.post(reverse("shared_catalogue_create", args=["menus"]), self.menu_data(meal_type="snack"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Menu.objects.filter(name="Repas commun", event_day__isnull=True).count(), 2)
        response = self.client.post(reverse("shared_catalogue_create", args=["menus"]), self.menu_data(name="REPAS COMMUN"))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        self.assertEqual(Menu.objects.count(), 2)

    def test_global_update_requires_current_signed_version(self):
        menu = Menu.objects.create(name="Repas commun", event_day=None, surcharge_cents=100)
        old_version = shared_option_version(menu)
        menu.description = "Modification effectuée ailleurs"
        menu.save()
        for version in (old_version, old_version + "tamper", ""):
            response = self.client.post(reverse("shared_catalogue_update", args=["menus", menu.pk]),
                                        self.menu_data(version=version))
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.context["form"].errors)
            menu.refresh_from_db()
            self.assertEqual(menu.surcharge_cents, 100)

    def test_changing_snack_type_is_refused_when_used_as_extra_collation(self):
        menu = Menu.objects.create(name="Repas", event_day=None)
        snack = Menu.objects.create(name="Fruit", event_day=None, meal_type="snack", surcharge_cents=125)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=menu, morning_snack=snack)
        response = self.client.post(reverse("shared_catalogue_update", args=["menus", snack.pk]),
                                    self.menu_data(name="Fruit", meal_type="full_meal", version=shared_option_version(snack)))
        self.assertEqual(response.status_code, 200)
        self.assertIn("meal_type", response.context["form"].errors)
        snack.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(snack.meal_type, "snack")
        self.assertEqual(order.morning_snack_id, snack.pk)

    def test_promotion_is_explicit_and_keeps_existing_record_choices_and_snapshots(self):
        menu = Menu.objects.create(name="Ancien menu", event_day=self.day, surcharge_cents=875)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=menu)
        url = reverse("shared_catalogue_promote", args=["menus", menu.pk])
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        menu.refresh_from_db()
        self.assertEqual(menu.event_day_id, self.day.pk)
        token = response.context["form"]["version"].value()
        response = self.client.post(url, {"version": token})
        self.assertEqual(response.status_code, 200)
        menu.refresh_from_db()
        self.assertEqual(menu.event_day_id, self.day.pk)
        response = self.client.post(url, {"version": token, "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        menu.refresh_from_db()
        order.refresh_from_db()
        self.assertIsNone(menu.event_day_id)
        self.assertEqual(order.menu_id, menu.pk)
        self.assertEqual(order.menu_surcharge_cents, 875)
        self.assertEqual(order.total_surcharge_cents, 875)
        self.assertIn(menu, menu_options_for_day(self.second_day))

    def test_promotion_conflict_never_merges_or_deletes_local_option(self):
        global_menu = Menu.objects.create(name="Menu identique", event_day=None, surcharge_cents=900)
        local_menu = Menu.objects.create(name="Menu identique", event_day=self.day, surcharge_cents=500)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=local_menu)
        response = self.client.post(reverse("shared_catalogue_promote", args=["menus", local_menu.pk]), {
            "version": shared_option_version(local_menu, action="promote"), "confirm": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].errors)
        local_menu.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(local_menu.event_day_id, self.day.pk)
        self.assertEqual(order.menu_id, local_menu.pk)
        self.assertTrue(Menu.objects.filter(pk=global_menu.pk).exists())

    def test_promotion_rejects_stale_version_and_handles_beverage(self):
        beverage = Beverage.objects.create(name="Ancienne eau", event_day=self.day)
        old_version = shared_option_version(beverage, action="promote")
        beverage.description = "Modifiée ailleurs"
        beverage.save()
        url = reverse("shared_catalogue_promote", args=["boissons", beverage.pk])
        response = self.client.post(url, {"version": old_version, "confirm": "on"})
        self.assertEqual(response.status_code, 200)
        beverage.refresh_from_db()
        self.assertEqual(beverage.event_day_id, self.day.pk)
        response = self.client.post(url, {"version": shared_option_version(beverage, action="promote"), "confirm": "on"})
        self.assertEqual(response.status_code, 302)
        beverage.refresh_from_db()
        self.assertIsNone(beverage.event_day_id)

    def test_promoted_options_survive_removal_of_their_original_event(self):
        menu = Menu.objects.create(name="Ancien repas", event_day=self.second_day)
        beverage = Beverage.objects.create(name="Ancien jus", event_day=self.second_day)
        for kind, option in (("menus", menu), ("boissons", beverage)):
            response = self.client.post(reverse("shared_catalogue_promote", args=[kind, option.pk]), {
                "version": shared_option_version(option, action="promote"), "confirm": "on",
            })
            self.assertEqual(response.status_code, 302)
        self.second_event.delete()
        menu.refresh_from_db()
        beverage.refresh_from_db()
        self.assertIsNone(menu.event_day_id)
        self.assertIsNone(beverage.event_day_id)
        self.assertIn(menu, menu_options_for_day(self.day))
        self.assertIn(beverage, beverage_options_for_day(self.day))

    def test_central_page_shows_globals_and_legacy_sources_without_family_data(self):
        Menu.objects.create(name="Repas commun", event_day=None)
        Menu.objects.create(name="Collation commune", event_day=None, meal_type="snack")
        Beverage.objects.create(name="Eau commune", event_day=None)
        Menu.objects.create(name="Ancien menu local", event_day=self.day)
        self.registration(note="Secret de la famille")
        response = self.client.get(reverse("shared_catalogue"))
        for text in ("Repas commun", "Collation commune", "Eau commune", "Ancien menu local", self.event.name):
            self.assertContains(response, text)
        for private in (self.member.full_name, self.family_user.email, "Secret de la famille"):
            self.assertNotContains(response, private)
        self.assertEqual(response["Cache-Control"], "max-age=0, no-cache, no-store, must-revalidate, private")

    def test_legacy_filter_does_not_hide_other_global_options(self):
        Menu.objects.create(name="Ancien premier", event_day=self.day)
        Menu.objects.create(name="Ancien second", event_day=self.second_day)
        Menu.objects.create(name="Toujours commun", event_day=None)
        response = self.client.get(reverse("shared_catalogue"), {"event": self.second_event.pk})
        self.assertContains(response, "Ancien second")
        self.assertContains(response, "Toujours commun")
        self.assertNotContains(response, "Ancien premier")

    def test_local_and_global_edit_routes_cannot_be_swapped(self):
        local = Menu.objects.create(name="Spécifique", event_day=self.day)
        global_menu = Menu.objects.create(name="Commun", event_day=None)
        self.assertEqual(self.client.get(reverse("shared_catalogue_update", args=["menus", local.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("shared_catalogue_promote", args=["menus", global_menu.pk])).status_code, 404)

    def test_global_and_local_same_name_are_distinct_choices(self):
        global_menu = Menu.objects.create(name="Végétarien", event_day=None)
        local = Menu.objects.create(name="Végétarien", event_day=self.day, surcharge_cents=250)
        form = FamilyRegistrationForm(event_day=self.day, member=self.member)
        labels = {str(value): str(label) for value, label in form.fields["menu"].choices}
        self.assertIn("Commun", labels[str(global_menu.pk)])
        self.assertIn("Spécifique", labels[str(local.pk)])

    def test_shared_pages_are_organizer_only_and_get_never_creates_data(self):
        local = Menu.objects.create(name="Ancien", event_day=self.day)
        global_menu = Menu.objects.create(name="Commun", event_day=None)
        urls = [
            reverse("shared_catalogue"), reverse("shared_catalogue_create", args=["menus"]),
            reverse("shared_catalogue_update", args=["menus", global_menu.pk]),
            reverse("shared_catalogue_import", args=["menus"]),
            reverse("shared_catalogue_promote", args=["menus", local.pk]),
        ]
        for user in (None, self.family_user):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for url in urls:
                for method in (self.client.get, self.client.post):
                    self.assertEqual(method(url).status_code, 403 if user else 302)
        self.assertEqual(Menu.objects.count(), 2)

    def test_services_recheck_current_active_rights_even_for_a_cached_organizer(self):
        local = Menu.objects.create(name="Ancien", event_day=self.day)
        preview = prepare_catalogue_preview(
            parse_catalogue_upload(csv_upload("nom\nNouveau repas\n"), "menus", identity_by_type=True),
            None, "menus",
        )
        for changes in ({"is_staff": False, "is_organizer": False}, {"is_active": False}):
            with self.subTest(changes=changes):
                type(self.organizer).objects.filter(pk=self.organizer.pk).update(**changes)
                with self.assertRaises(PermissionDenied):
                    save_shared_option(kind="menus", data=self.menu_data(), actor=self.organizer)
                with self.assertRaises(PermissionDenied):
                    promote_local_option(
                        kind="menus", option_id=local.pk, actor=self.organizer,
                        version=shared_option_version(local, action="promote"),
                    )
                with self.assertRaises(PermissionDenied):
                    apply_catalogue_preview(day=None, kind="menus", rows=preview.rows, actor=self.organizer)
                type(self.organizer).objects.filter(pk=self.organizer.pk).update(
                    is_staff=True, is_organizer=True, is_active=True,
                )
        self.assertEqual(Menu.objects.count(), 1)
        local.refresh_from_db()
        self.assertEqual(local.event_day_id, self.day.pk)


class SharedCatalogueImportTests(DataMixin, TestCase):
    def setUp(self):
        self.client.force_login(self.organizer)

    def upload(self, content, kind="menus"):
        return self.client.post(reverse("shared_catalogue_import", args=[kind]), {
            "action": "preview", "file": csv_upload(content),
        })

    def confirm(self, response, kind="menus", **extra):
        return self.client.post(reverse("shared_catalogue_import", args=[kind]), {
            "action": "confirm", "preview_token": response.context["confirm_form"]["preview_token"].value(), **extra,
        })

    def test_global_import_accepts_same_name_for_meal_and_snack_only_after_confirmation(self):
        content = "nom;description;supplement_euros;type\nFormule;Plat;8;repas\nFormule;Fruit;2;collation\n"
        response = self.upload(content)
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.context["confirm_form"])
        self.assertEqual(Menu.objects.count(), 0)
        response = self.confirm(response, rows='[{"name":"Non autorisé"}]')
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Menu.objects.filter(event_day__isnull=True).count(), 2)
        self.assertFalse(Menu.objects.filter(name="Non autorisé").exists())
        self.assertNotIn(PREVIEW_SESSION_KEY, self.client.session)

    def test_import_updates_matching_type_and_keeps_other_type_and_booked_amount(self):
        menu = Menu.objects.create(name="Formule", event_day=None, surcharge_cents=800)
        snack = Menu.objects.create(name="Formule", event_day=None, meal_type="snack", surcharge_cents=200)
        order = MealOrder.objects.create(registration=self.registration(), wants_meal=True, menu=menu, afternoon_snack=snack)
        response = self.upload("nom;supplement_euros;type\nFormule;9;repas\n")
        blocked = self.confirm(response)
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, "Confirmez explicitement")
        response = self.confirm(response, confirm_updates="on")
        self.assertEqual(response.status_code, 302)
        menu.refresh_from_db()
        snack.refresh_from_db()
        order.refresh_from_db()
        self.assertEqual(menu.surcharge_cents, 900)
        self.assertEqual(snack.surcharge_cents, 200)
        self.assertEqual(order.total_surcharge_cents, 1000)

    def test_existing_local_menu_with_same_name_is_not_silently_promoted_or_updated(self):
        local = Menu.objects.create(name="Formule", event_day=self.day, surcharge_cents=100)
        response = self.upload("nom;supplement_euros;type\nFormule;8;repas\n")
        self.assertEqual(response.context["update_count"], 0)
        self.assertEqual(self.confirm(response).status_code, 302)
        local.refresh_from_db()
        self.assertEqual(local.event_day_id, self.day.pk)
        self.assertEqual(local.surcharge_cents, 100)
        self.assertEqual(Menu.objects.filter(name="Formule").count(), 2)

    def test_invalid_duplicate_type_blocks_whole_import_and_local_parser_stays_name_unique(self):
        response = self.upload("nom;type\nFormule;repas\nFormule;repas\n")
        self.assertIsNone(response.context["confirm_form"])
        self.assertEqual(Menu.objects.count(), 0)
        local_preview = parse_catalogue_upload(csv_upload("nom;type\nFormule;repas\nFormule;collation\n"), "menus")
        self.assertFalse(local_preview.valid)

    def test_spreadsheet_import_of_global_beverages_uses_existing_parser(self):
        workbook = Workbook()
        workbook.active.append(["nom", "description", "supplement_euros"])
        workbook.active.append(["Jus commun", "Pomme", 1.25])
        output = io.BytesIO()
        workbook.save(output)
        workbook.close()
        response = self.client.post(reverse("shared_catalogue_import", args=["boissons"]), {
            "action": "preview", "file": SimpleUploadedFile("boissons.xlsx", output.getvalue()),
        })
        self.assertEqual(Beverage.objects.count(), 0)
        self.assertEqual(self.confirm(response, kind="boissons").status_code, 302)
        beverage = Beverage.objects.get(name="Jus commun")
        self.assertIsNone(beverage.event_day_id)
        self.assertEqual(beverage.surcharge_cents, 125)

    def test_global_import_rejects_tampered_token_without_changes(self):
        response = self.upload("nom\nRepas\n")
        token = response.context["confirm_form"]["preview_token"].value()
        response = self.client.post(reverse("shared_catalogue_import", args=["menus"]), {
            "action": "confirm", "preview_token": token + "tampered",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(Menu.objects.count(), 0)
