from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from volunteers.forms_family import FamilyMealForm, FamilyRegistrationForm
from volunteers.forms_organizer import MealOrderOrganizerForm
from volunteers.forms_self import PersonalRegistrationForm
from volunteers.models import Assignment, Beverage, Event, MealOrder, Menu, Station

from .factories import DataMixin


class OfficialVolunteerPricingFormsTests(DataMixin, TestCase):
    def setUp(self):
        self.member.is_competition_official = True
        self.member.save(update_fields=["is_competition_official"])
        self.menu = Menu.objects.create(name="Repas", surcharge_cents=350)
        self.snack = Menu.objects.create(
            name="Collation", meal_type=Menu.MealType.SNACK, surcharge_cents=100,
        )
        self.drink = Beverage.objects.create(name="Jus", surcharge_cents=50)

    def data(self, **changes):
        return {
            "availability": "full_day", "menu": str(self.menu.pk),
            "morning_snack": str(self.snack.pk), "afternoon_snack": str(self.snack.pk),
            "beverage": str(self.drink.pk), "payment_method": "sumup", **changes,
        }

    def assign(self, registration, slot=None, *, official=False, status="draft"):
        slot = slot or self.morning_slot
        if official:
            slot.station.category = Station.Category.OFFICIAL
            slot.station.save(update_fields=["category"])
        return Assignment.objects.create(
            registration=registration, station_slot=slot, period=slot.period, status=status,
        )

    def assert_volunteer_prices(self, form):
        self.assertFalse(form.official_exemption)
        for name, option, price in (
            ("menu", self.menu, "3,50"), ("morning_snack", self.snack, "1,00"),
            ("afternoon_snack", self.snack, "1,00"), ("beverage", self.drink, "0,50"),
        ):
            self.assertIn(f"{price} €", form.fields[name].label_from_instance(option))
            self.assertNotIn("(officiel)", form.fields[name].label_from_instance(option))
        self.assertFalse(form.fields["payment_method"].widget.is_hidden)
        self.assertEqual(form.fields["payment_method"].disabled, form.preview_total_cents == 0)

    def test_new_volunteer_preference_shows_four_prices_and_keeps_payment_choice(self):
        form = FamilyRegistrationForm(
            self.data(preferred_station=str(self.station.pk)), event_day=self.day, member=self.member,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assert_volunteer_prices(form)
        self.assertEqual(form.cleaned_data["payment_method"], "sumup")
        menu_option = next(option for option in form.menu_options if option["id"] == str(self.menu.pk))
        self.assertEqual(menu_option["standard_surcharge"], 350)
        self.assertEqual(form.official_pricing_data["stations"][str(self.station.pk)], "volunteer")

    def test_saved_volunteer_preference_applies_to_family_and_organizer_meal_forms(self):
        registration = self.registration(preferred_station=self.station)
        order = MealOrder.objects.create(registration=registration, menu=self.menu, beverage=self.drink, wants_meal=True)
        for form in (
            FamilyRegistrationForm(event_day=self.day, registration=registration, member=self.member),
            FamilyMealForm(registration=registration, meal_order=order),
            MealOrderOrganizerForm(registration=registration, instance=order),
        ):
            with self.subTest(form=type(form).__name__):
                self.assert_volunteer_prices(form)

    def test_volunteer_assignment_overrides_new_official_preference(self):
        registration = self.registration()
        self.assign(registration)
        self.other_station.category = Station.Category.OFFICIAL
        self.other_station.save(update_fields=["category"])
        form = FamilyRegistrationForm(
            self.data(preferred_station=str(self.other_station.pk)),
            event_day=self.day, member=self.member, registration=registration,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assert_volunteer_prices(form)
        self.assertTrue(form.official_pricing_data["has_volunteer_assignment"])

    def test_official_assignment_overrides_obsolete_volunteer_preference(self):
        registration = self.registration(preferred_station=self.other_station)
        self.assign(registration, official=True)
        form = FamilyRegistrationForm(
            self.data(preferred_station=str(self.other_station.pk)),
            event_day=self.day, member=self.member, registration=registration,
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertTrue(form.official_exemption)
        self.assertEqual(form.cleaned_data["payment_method"], "counter")

    def test_cancelled_volunteer_assignment_does_not_remove_exemption(self):
        registration = self.registration()
        self.assign(registration, status=Assignment.Status.CANCELLED)
        form = FamilyRegistrationForm(event_day=self.day, member=self.member, registration=registration)
        self.assertTrue(form.official_exemption)
        self.assertFalse(form.official_pricing_data["has_assignments"])

    def test_mixed_assignments_show_volunteer_prices(self):
        registration = self.registration()
        self.assign(registration, official=True)
        # Les deux créneaux initiaux partagent le même poste : séparer celui de l'après-midi.
        self.afternoon_slot.station = self.other_station
        self.afternoon_slot.save(update_fields=["station"])
        self.assign(registration, self.afternoon_slot)
        form = FamilyRegistrationForm(event_day=self.day, member=self.member, registration=registration)
        self.assert_volunteer_prices(form)

    def test_administrator_new_checkbox_selection_replaces_existing_role_preview(self):
        self.family_user.is_superuser = True
        registration = self.registration()
        self.assign(registration)
        self.afternoon_slot.station = self.other_station
        self.afternoon_slot.save(update_fields=["station"])
        self.other_station.category = Station.Category.OFFICIAL
        self.other_station.save(update_fields=["category"])
        for choices, expected in (([str(self.morning_slot.pk)], False),
                                  ([str(self.afternoon_slot.pk)], True), ([], True)):
            with self.subTest(choices=choices):
                form = PersonalRegistrationForm(
                    self.data(station_slots=choices), user=self.family_user, event_day=self.day,
                    registration=registration, member=self.member,
                )
                self.assertTrue(form.is_valid(), form.errors)
                self.assertEqual(form.official_exemption, expected)
                self.assertIsNotNone(form.official_pricing_data["slots"])
                if not expected:
                    self.assert_volunteer_prices(form)

    def test_forged_station_or_slot_is_a_form_error_and_does_not_crash_pricing(self):
        for form in (
            FamilyRegistrationForm(self.data(preferred_station="invalid"), event_day=self.day, member=self.member),
            PersonalRegistrationForm(
                self.data(station_slots=["invalid"]),
                user=self._administrator(), event_day=self.day, member=self.member,
            ),
        ):
            self.assertFalse(form.is_valid())

    def _administrator(self):
        self.family_user.is_superuser = True
        return self.family_user

    def test_historical_exemption_remains_fixed_when_current_post_changes(self):
        registration = self.registration()
        order = MealOrder.objects.create(registration=registration, menu=self.menu, wants_meal=True)
        self.day.date = timezone.localdate() - timedelta(days=1)
        self.day.save(update_fields=["date"])
        self.assign(registration)
        form = FamilyRegistrationForm(
            self.data(preferred_station=str(self.station.pk)),
            event_day=self.day, member=self.member, registration=registration,
        )
        self.assertTrue(form.official_exemption)
        self.assertTrue(form.official_pricing_data["fixed_exemption"])
        order.refresh_from_db()
        self.assertTrue(order.official_exemption_applied)

    def test_family_post_saves_volunteer_price_and_shows_notice(self):
        self.client.force_login(self.family_user)
        response = self.client.post(reverse("family_registration_edit", args=[self.day.pk, self.member.pk]),
                                    self.data(preferred_station=str(self.station.pk)))
        self.assertEqual(response.status_code, 302)
        order = MealOrder.objects.get(registration__member=self.member)
        self.assertEqual(order.total_surcharge_cents, 600)
        self.assertFalse(order.official_exemption_applied)
        self.assertEqual(order.payment_method, "sumup")
        response = self.client.get(reverse("family_meal_edit", args=[order.registration_id]))
        self.assertContains(response, "Poste bénévole : restauration au tarif bénévole.")
        self.assertFalse(response.context["form"].official_exemption)

    def test_future_archived_event_keeps_both_historical_pricing_snapshots_in_forms(self):
        exempt_registration = self.registration()
        exempt_order = MealOrder.objects.create(registration=exempt_registration, menu=self.menu, wants_meal=True)
        paid_registration = self.registration(member=self.other_member)
        paid_order = MealOrder.objects.create(registration=paid_registration, menu=self.menu, wants_meal=True)
        self.event.status = Event.Status.ARCHIVED
        self.event.save(update_fields=["status"])
        exempt_registration.preferred_station = self.station
        exempt_registration.save(update_fields=["preferred_station"])
        self.other_member.is_competition_official = True
        self.other_member.save(update_fields=["is_competition_official"])
        for order, member, expected in ((exempt_order, self.member, True),
                                        (paid_order, self.other_member, False)):
            order.refresh_from_db()
            registration = order.registration
            for form in (
                FamilyRegistrationForm(
                    self.data(preferred_station=str(self.station.pk)), event_day=self.day,
                    registration=registration, member=member, allow_closed_meals=True,
                ),
                FamilyMealForm(registration=registration, meal_order=order),
                MealOrderOrganizerForm(registration=registration, instance=order),
            ):
                with self.subTest(form=type(form).__name__, expected=expected):
                    self.assertEqual(form.official_exemption, expected)
                    self.assertEqual(form.official_pricing_data["fixed_exemption"], expected)
                    self.assertEqual("(officiel)" in form.fields["menu"].label_from_instance(self.menu), expected)

    def test_volunteer_forms_and_javascript_use_reserved_component_prices_after_catalogue_change(self):
        registration = self.registration()
        order = MealOrder.objects.create(
            registration=registration, menu=self.menu, morning_snack=self.snack, beverage=self.drink, wants_meal=True,
        )
        # Une même collation est réservée à deux moments, avec deux tarifs figés.
        Menu.objects.filter(pk=self.snack.pk).update(surcharge_cents=150)
        order.afternoon_snack = self.snack
        order.save()
        Menu.objects.filter(pk__in=[self.menu.pk, self.snack.pk]).update(surcharge_cents=50000)
        Beverage.objects.filter(pk=self.drink.pk).update(surcharge_cents=60000)
        self.menu.refresh_from_db()
        self.snack.refresh_from_db()
        self.drink.refresh_from_db()
        new_menu = Menu.objects.create(name="Nouveau repas", surcharge_cents=70000)
        new_snack = Menu.objects.create(name="Nouvelle collation", meal_type="snack", surcharge_cents=80000)
        new_drink = Beverage.objects.create(name="Nouvelle boisson", surcharge_cents=90000)
        registration.preferred_station = self.station
        registration.save(update_fields=["preferred_station"])
        order.refresh_from_db()
        self.assertEqual(order.total_surcharge_cents, 650)
        data = self.data(preferred_station=str(self.station.pk))
        forms = (
            FamilyRegistrationForm(data, event_day=self.day, member=self.member, registration=registration),
            FamilyMealForm(data, registration=registration, meal_order=order),
            MealOrderOrganizerForm({"wants_meal": "on", **data}, registration=registration, instance=order),
        )
        expected = {"menu": (self.menu, 350), "morning_snack": (self.snack, 100),
                    "afternoon_snack": (self.snack, 150), "beverage": (self.drink, 50)}
        for form in forms:
            with self.subTest(form=type(form).__name__):
                self.assertTrue(form.is_valid(), form.errors)
                self.assertFalse(form.official_exemption)
                for component, (option, cents) in expected.items():
                    label = form.fields[component].label_from_instance(option)
                    self.assertIn(f"{cents / 100:.2f}".replace(".", ",") + " €", label)
                    entry = next(item for item in form.official_pricing_data["option_prices"][component]
                                 if item["id"] == str(option.pk))
                    self.assertEqual(entry["standard_surcharge"], cents)
                    self.assertEqual(entry["standard"], label)
                for component, option in (("menu", new_menu), ("morning_snack", new_snack),
                                          ("afternoon_snack", new_snack), ("beverage", new_drink)):
                    entry = next(item for item in form.official_pricing_data["option_prices"][component]
                                 if item["id"] == str(option.pk))
                    self.assertEqual(entry["standard_surcharge"], option.surcharge_cents)
        entry = next(item for item in forms[0].menu_options if item["id"] == str(self.menu.pk))
        self.assertEqual((entry["surcharge"], entry["standard_surcharge"]), (350, 350))
        self.assertEqual(entry["label"], entry["standard_label"])
        self.assertIn("3,50 €", entry["standard_label"])

    def test_legacy_missing_standard_snapshot_uses_applied_price_in_labels_and_javascript(self):
        registration = self.registration(preferred_station=self.station)
        order = MealOrder.objects.create(
            registration=registration, menu=self.menu, morning_snack=self.snack,
            afternoon_snack=self.snack, beverage=self.drink, wants_meal=True,
        )
        MealOrder.objects.filter(pk=order.pk).update(**{
            f"{component}_standard_surcharge_cents": None
            for component in MealOrder.PRICE_COMPONENTS
        })
        Menu.objects.filter(pk__in=[self.menu.pk, self.snack.pk]).update(surcharge_cents=99999)
        Beverage.objects.filter(pk=self.drink.pk).update(surcharge_cents=99999)
        order.refresh_from_db()
        form = FamilyRegistrationForm(event_day=self.day, member=self.member, registration=registration)
        for component in MealOrder.PRICE_COMPONENTS:
            option = getattr(order, component)
            if option is None:
                continue
            expected = getattr(order, f"{component}_surcharge_cents")
            entry = next(item for item in form.official_pricing_data["option_prices"][component]
                         if item["id"] == str(option.pk))
            self.assertEqual(entry["standard_surcharge"], expected)
            self.assertEqual(entry["standard"], form.fields[component].label_from_instance(option))
