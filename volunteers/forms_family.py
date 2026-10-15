"""Formulaires de l'espace famille.

Les listes de choix sensibles sont toujours reconstruites côté serveur à partir
de la journée concernée. Une valeur forgée dans le navigateur ne peut donc pas
référencer un poste, un menu ou une boisson d'un autre événement.
"""

from collections import Counter

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .forms_eligibility import eligible_stations, station_requirements_label
from .meal_catalogue import beverage_options_for_day, menu_options_for_day
from .models import Beverage, Event, Family, MealOrder, Member, Menu, Registration, Station
from .official_pricing import registration_has_official_meal_exemption

User = get_user_model()
SNACK_FIELDS = ("morning_snack", "afternoon_snack")
SNACK_BEVERAGE_FIELDS = ("morning_beverage", "afternoon_beverage")
SNACK_CHOICE_FIELDS = (*SNACK_FIELDS, *SNACK_BEVERAGE_FIELDS)
MEAL_COMPONENT_FIELDS = ("menu", *SNACK_FIELDS, "beverage", *SNACK_BEVERAGE_FIELDS)
MEAL_FIELD_ORDER = ("menu", "beverage", "morning_snack", "morning_beverage", "afternoon_snack", "afternoon_beverage")
MEAL_SUMMARY_LABELS = {
    "menu": "Repas ou collation", "beverage": "Boisson",
    "morning_snack": "Collation du matin", "morning_beverage": "Boisson du matin",
    "afternoon_snack": "Collation de l’après-midi", "afternoon_beverage": "Boisson de l’après-midi",
}


def meal_option_label(option, *, official=False, surcharge_cents=None, distinguish_origin=False):
    standard = option.surcharge_cents if surcharge_cents is None else surcharge_cents
    cents = 0 if official else standard
    price = f"{cents / 100:.2f}".replace(".", ",")
    origin = ("Commun" if option.event_day_id is None else "Spécifique à cet événement")
    return f"{option.name}" + (f" — {origin}" if distinguish_origin else "") + f" — {price} €" + (" (officiel)" if official else "")


def _form_value(form, name):
    value = form.data.get(form.add_prefix(name)) if form.is_bound else form.initial.get(name)
    return str(getattr(value, "pk", value) or "")


def _meal_is_wanted(form):
    if "no_meal" in form.fields:
        return not forms.BooleanField(required=False).to_python(_form_value(form, "no_meal"))
    if "wants_meal" in form.fields:
        return forms.BooleanField(required=False).to_python(_form_value(form, "wants_meal"))
    return True


def normalize_meal_payment(form, cleaned):
    cleaned["payment_method"] = cleaned.get("payment_method") or MealOrder.PaymentMethod.COUNTER
    return cleaned


def _meal_component_surcharge(option, component, meal_order=None):
    if meal_order and option.pk == getattr(meal_order, f"{component}_id"):
        standard = getattr(meal_order, f"{component}_standard_surcharge_cents")
        return getattr(meal_order, f"{component}_surcharge_cents") if standard is None else standard
    return option.surcharge_cents


def configure_snack_fields(form, event_day, availability, order=None):
    form.meal_availability = availability
    if "menu" in form.fields:
        form.fields["menu"].label = {
            "full_day": "Repas du midi", "morning": "Collation du matin",
            "afternoon": "Collation de l’après-midi",
        }.get(availability, "Repas ou collation")
    for name, label in (("morning_snack", "Collation du matin (facultatif)"),
                        ("afternoon_snack", "Collation de l’après-midi (facultatif)")):
        old_id = getattr(order, f"{name}_id", None) if order else None
        queryset = menu_options_for_day(event_day, active_only=False).filter(
            Q(is_active=True, meal_type=Menu.MealType.SNACK)
            | Q(pk=old_id, meal_type=Menu.MealType.SNACK)
        ).order_by("name", "event_day_id")
        form.fields[name] = forms.ModelChoiceField(
            label=label, queryset=queryset, required=False, empty_label="Sans collation",
        )
        if availability != Registration.Availability.FULL_DAY and "availability" not in form.fields:
            form.fields[name].widget = forms.HiddenInput()
        elif order:
            form.initial[name] = old_id


def configure_beverage_fields(form, event_day, availability, order=None):
    """Boisson du repas et boissons facultatives des deux collations."""
    event = event_day.event
    beverages = beverage_options_for_day(event_day, active_only=False)
    old_main_id = order.beverage_id if order else None
    old_availability = order.beverage_choice_availability if order else ""
    enabled = {
        "morning": event.morning_hot_beverage_enabled,
        "afternoon": event.afternoon_cold_beverage_enabled,
    }
    temperatures = {"morning": Beverage.Temperature.HOT, "afternoon": Beverage.Temperature.COLD}
    allowed = Q(is_active=True)
    if availability != Registration.Availability.FULL_DAY:
        allowed = Q(is_active=True, temperature=temperatures.get(availability)) if enabled.get(availability) else Q(pk__in=[])
    if old_main_id and old_availability == availability:
        allowed |= Q(pk=old_main_id)
    form.fields["beverage"] = forms.ModelChoiceField(
        label={"full_day": "Boisson du repas", "morning": "Boisson chaude", "afternoon": "Boisson fraîche"}.get(availability, "Boisson"),
        queryset=beverages.filter(allowed).order_by("name"),
        required=False, empty_label="Sans boisson",
    )
    if order:
        form.initial["beverage"] = old_main_id
    form.beverage_options = []
    for option in beverages.filter(Q(is_active=True) | Q(pk=old_main_id)).order_by("name"):
        standard = _meal_component_surcharge(option, "beverage", order)
        form.beverage_options.append({
            "id": str(option.pk), "name": option.name, "temperature": option.temperature, "active": option.is_active,
            "origin": "Commun" if option.event_day_id is None else "Spécifique à cet événement",
            "standard_surcharge": standard,
            "standard_label": meal_option_label(option, surcharge_cents=standard),
            "official_label": meal_option_label(option, official=True),
            "legacy_availability": old_availability if option.pk == old_main_id else "",
        })
    form.snack_beverage_settings = {
        "morning_enabled": enabled["morning"], "afternoon_enabled": enabled["afternoon"],
    }
    for period, label in (("morning", "Boisson chaude avec la collation du matin (facultatif)"),
                          ("afternoon", "Boisson froide avec la collation de l’après-midi (facultatif)")):
        name = f"{period}_beverage"
        old_id = getattr(order, f"{name}_id", None) if order else None
        allowed = Q(is_active=True, temperature=temperatures[period]) if enabled[period] else Q(pk__in=[])
        allowed |= Q(pk=old_id)
        form.fields[name] = forms.ModelChoiceField(
            label=label, queryset=beverages.filter(allowed).order_by("name"),
            required=False, empty_label="Sans boisson",
        )
        if availability != Registration.Availability.FULL_DAY and "availability" not in form.fields:
            form.fields[name].widget = forms.HiddenInput()
        if order:
            form.initial[name] = old_id
        if not enabled[period]:
            form.fields[name].help_text = (
                "Cette option n’est plus proposée sur cet événement. Votre réservation est conservée ; vous pouvez la retirer."
                if old_id else "Cette option n’est pas proposée sur cet événement."
            )


def clean_snack_choices(form, cleaned, *, availability, no_meal):
    for name in SNACK_CHOICE_FIELDS:
        cleaned.setdefault(name, None)
    if availability != Registration.Availability.FULL_DAY and any(
        form.data.get(form.add_prefix(name)) for name in SNACK_CHOICE_FIELDS
    ):
        form.add_error(None, "Les collations du matin et de l’après-midi sont réservées à une présence à la journée.")
    if no_meal and any(cleaned.get(name) for name in SNACK_CHOICE_FIELDS):
        form.add_error(None, "Retirez les collations et boissons associées ou décochez le choix sans repas ni collation.")
    if no_meal:
        for name in SNACK_CHOICE_FIELDS:
            cleaned[name] = None
    for period in ("morning", "afternoon"):
        if cleaned.get(f"{period}_beverage") and not cleaned.get(f"{period}_snack"):
            form.add_error(f"{period}_beverage", "Choisissez la collation correspondante pour ajouter sa boisson.")


def _pricing_registration(form, member, meal_order=None):
    return (
        getattr(form, "registration", None)
        or (meal_order.registration if meal_order else None)
        or Registration(member=member, event_day=form.event_day)
    )


def official_meal_exemption(member, meal_order=None, *, reactivating=False, form=None):
    if meal_order:
        registration = meal_order.registration
        if (
            registration.event_day.date < timezone.localdate()
            or registration.event_day.event.status == Event.Status.ARCHIVED
            or (registration.status != Registration.Status.ACTIVE and not reactivating)
        ):
            return meal_order.official_exemption_applied
    if not member:
        return False
    if form is None:
        return registration_has_official_meal_exemption(
            meal_order.registration, member=member,
        ) if meal_order else bool(member.is_competition_official)
    registration = _pricing_registration(form, member, meal_order)
    overrides = {}
    if "station_slots" in form.fields:
        # L'administrateur remplace ses affectations par les cases cochées.
        field = form.fields["station_slots"]
        if form.is_bound:
            key = form.add_prefix("station_slots")
            selected = form.data.getlist(key) if hasattr(form.data, "getlist") else form.data.get(key, [])
        else:
            selected = form.initial.get("station_slots", [])
        if not isinstance(selected, (list, tuple)):
            selected = [selected]
        try:
            selected_slots = field.queryset.filter(pk__in=selected)
        except (ValidationError, ValueError):
            selected_slots = field.queryset.none()
        overrides.update(station_slots=selected_slots, preferred_station=None)
    elif "preferred_station" in form.fields:
        selected = form.data.get(form.add_prefix("preferred_station")) if form.is_bound else form.initial.get("preferred_station")
        try:
            preferred = form.fields["preferred_station"].queryset.filter(pk=selected).first() if selected else None
        except (ValidationError, ValueError):
            preferred = None  # Le ModelChoiceField signalera la valeur forgée.
        overrides["preferred_station"] = preferred
    return registration_has_official_meal_exemption(registration, member=member, **overrides)


def configure_official_meal_pricing(form, member, meal_order=None, *, reactivating=False):
    """Présente le tarif du rôle tenu, et les données du changement de poste."""
    form.official_exemption = official_meal_exemption(member, meal_order, reactivating=reactivating, form=form)
    if form.is_bound and not _meal_is_wanted(form):
        # Le choix explicite « sans repas » est appliqué à l'enregistrement.
        # Les valeurs conservées dans le navigateur ne doivent pas le bloquer.
        form.data = form.data.copy()
        for name in MEAL_COMPONENT_FIELDS:
            if name in form.fields:
                form.data[form.add_prefix(name)] = ""
    dynamic = "preferred_station" in form.fields or "station_slots" in form.fields
    form.official_pricing_show_notice = bool(member and member.is_competition_official) or form.official_exemption
    option_prices = {}
    for name in MEAL_COMPONENT_FIELDS:
        if name in form.fields:
            options = list(form.fields[name].queryset)
            prices = {option.pk: _meal_component_surcharge(option, name, meal_order) for option in options}
            names = Counter(option.name.strip().casefold() for option in options)
            form.fields[name].label_from_instance = (
                lambda option, prices=prices, names=names: meal_option_label(
                    option, official=form.official_exemption, surcharge_cents=prices.get(option.pk),
                    distinguish_origin=names[option.name.strip().casefold()] > 1,
                )
            )
            option_prices[name] = [
                {"id": str(option.pk), "name": option.name,
                 "origin": "Commun" if option.event_day_id is None else "Spécifique à cet événement",
                 "standard": meal_option_label(option, surcharge_cents=prices[option.pk], distinguish_origin=names[option.name.strip().casefold()] > 1),
                 "standard_surcharge": prices[option.pk], "official": meal_option_label(option, official=True, distinguish_origin=names[option.name.strip().casefold()] > 1)}
                for option in options
            ]
    for option in getattr(form, "menu_options", []):
        option["label"] = option["official_label"] if form.official_exemption else option["standard_label"]
        option["surcharge"] = 0 if form.official_exemption else option["standard_surcharge"]
    selected_prices = []
    form.preview_summary = []
    availability = getattr(form, "meal_availability", Registration.Availability.FULL_DAY)
    for name, choices in option_prices.items():
        if name in SNACK_CHOICE_FIELDS and availability != Registration.Availability.FULL_DAY:
            continue
        if name in SNACK_BEVERAGE_FIELDS and not _form_value(form, name.replace("beverage", "snack")):
            continue
        for option in choices:
            if option["id"] == _form_value(form, name):
                selected_prices.append(option["standard_surcharge"])
                if _meal_is_wanted(form):
                    form.preview_summary.append({
                        "label": MEAL_SUMMARY_LABELS[name], "name": option["name"],
                        "cents": 0 if form.official_exemption else option["standard_surcharge"],
                    })
    form.preview_total_cents = sum(selected_prices) if _meal_is_wanted(form) and not form.official_exemption else 0
    if "payment_method" in form.fields:
        field = form.fields["payment_method"]
        field.disabled = form.preview_total_cents == 0
        field.required = False
        if form.preview_total_cents == 0:
            if not dynamic and form.official_exemption:
                field.widget = forms.HiddenInput()
            field.initial = MealOrder.PaymentMethod.COUNTER
            form.initial["payment_method"] = MealOrder.PaymentMethod.COUNTER
    registration = _pricing_registration(form, member, meal_order) if member else None
    categories = set(registration.assignments.exclude(status="cancelled").values_list(
        "station_slot__station__category", flat=True,
    )) if registration else set()
    historical = bool(meal_order and (
        registration.event_day.date < timezone.localdate()
        or registration.event_day.event.status == Event.Status.ARCHIVED
        or (registration.status != Registration.Status.ACTIVE and not reactivating)
    ))
    form.official_pricing_data = {
        "eligible": bool(member and member.is_competition_official),
        "fixed_exemption": form.official_exemption if historical or not dynamic else None,
        "has_assignments": bool(categories), "has_volunteer_assignment": "volunteer" in categories,
        "stations": {str(station.pk): station.category for station in form.fields["preferred_station"].queryset} if "preferred_station" in form.fields else {},
        "slots": {str(slot.pk): slot.station.category for slot in form.fields["station_slots"].queryset} if "station_slots" in form.fields else None,
        "option_prices": option_prices,
    }


class SignUpForm(UserCreationForm):
    """Crée atomiquement le responsable et son compte famille."""

    family_name = forms.CharField(label="Nom de la famille", max_length=150)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ("first_name", "last_name", "email", "phone")
        labels = {
            "first_name": "Prénom du responsable",
            "last_name": "Nom du responsable",
            "email": "Adresse e-mail",
            "phone": "Téléphone",
        }

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("Un compte existe déjà avec cette adresse e-mail.")
        return email

    @transaction.atomic
    def save(self, commit=True):
        user = super().save(commit=False)
        user.email = self.cleaned_data["email"]
        if not commit:
            return user
        user.save()
        Family.objects.create(account=user, name=self.cleaned_data["family_name"].strip())
        return user


class FamilyAccountForm(forms.Form):
    """Modifie les données du responsable et de la famille en une transaction."""

    family_name = forms.CharField(label="Nom de la famille", max_length=150)
    first_name = forms.CharField(label="Prénom du responsable", max_length=150)
    last_name = forms.CharField(label="Nom du responsable", max_length=150)
    email = forms.EmailField(label="Adresse e-mail")
    phone = forms.CharField(label="Téléphone", max_length=30, required=False)

    def __init__(self, *args, family, **kwargs):
        self.family = family
        initial = kwargs.setdefault("initial", {})
        initial.update(
            {
                "family_name": family.name,
                "first_name": family.account.first_name,
                "last_name": family.account.last_name,
                "email": family.account.email,
                "phone": family.account.phone,
            }
        )
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.family.account_id).exists():
            raise ValidationError("Cette adresse e-mail est déjà utilisée.")
        return email

    @transaction.atomic
    def save(self):
        account = self.family.account
        account.first_name = self.cleaned_data["first_name"].strip()
        account.last_name = self.cleaned_data["last_name"].strip()
        account.email = self.cleaned_data["email"]
        account.phone = self.cleaned_data["phone"].strip()
        account.save(update_fields=["first_name", "last_name", "email", "phone"])
        self.family.name = self.cleaned_data["family_name"].strip()
        self.family.save(update_fields=["name", "updated_at"])
        return self.family


class MemberForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ("first_name", "last_name", "birth_date")
        labels = {
            "first_name": "Prénom",
            "last_name": "Nom",
            "birth_date": "Date de naissance",
        }
        widgets = {"birth_date": forms.DateInput(attrs={"type": "date"})}

    def _post_clean(self):
        if self.instance.pk and not self.instance._state.adding:
            # Les droits ne sont pas exposés dans ce formulaire familial.
            # Recharger leur valeur évite de valider une révocation involontaire.
            current = Member.objects.filter(pk=self.instance.pk).values(
                "is_competition_official", "user_id"
            ).first()
            if current:
                self.instance.is_competition_official = current["is_competition_official"]
                self.instance.user_id = current["user_id"]
        super()._post_clean()

    def _update_errors(self, errors):
        if hasattr(errors, "error_dict"):
            mapped = {}
            for field, messages in errors.error_dict.items():
                target = field if field in self.fields else "__all__"
                mapped.setdefault(target, []).extend(messages)
            errors = ValidationError(mapped)
        super()._update_errors(errors)

    def save(self, commit=True):
        member = super().save(commit=False)
        if commit:
            if member._state.adding:
                member.save()
            else:
                member.save(update_fields=["first_name", "last_name", "birth_date", "updated_at"])
        return member

    def clean_birth_date(self):
        birth_date = self.cleaned_data["birth_date"]
        today = timezone.localdate()
        if birth_date > today:
            raise ValidationError("La date de naissance ne peut pas être dans le futur.")
        try:
            oldest = today.replace(year=today.year - 120)
        except ValueError:  # 29 février d'une année bissextile
            oldest = today.replace(year=today.year - 120, day=28)
        if birth_date < oldest:
            raise ValidationError("Vérifiez la date de naissance saisie.")
        return birth_date


class PreferredStationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, station):
        requirements = station_requirements_label(station)
        return station.name + (f" — {requirements}" if requirements else "")


class FamilyRegistrationForm(forms.Form):
    """Saisie d'une disponibilité ; l'enregistrement passe par le service métier."""

    availability = forms.ChoiceField(
        label="Disponibilité",
        choices=Registration.Availability.choices,
    )
    preferred_station = PreferredStationChoiceField(
        label="Poste souhaité (facultatif)",
        queryset=Station.objects.none(),
        required=False,
        empty_label="Sans préférence",
        help_text="Ce souhait ne réserve pas le poste. Les postes d’officiel sont réservés aux membres habilités par un administrateur. Licence et formation à jour nécessaires.",
    )
    note = forms.CharField(
        label="Remarque pour l'organisateur",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, event_day, registration=None, member=None, allow_closed_meals=False, **kwargs):
        self.event_day = event_day
        self.registration = registration
        self.member = member or (registration.member if registration else None)
        if registration is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "availability": registration.availability,
                "preferred_station": registration.preferred_station_id,
                "note": registration.note,
            }
        super().__init__(*args, **kwargs)
        self.fields["preferred_station"].queryset = eligible_stations(Station.objects.filter(
            event=event_day.event,
            is_active=True,
        ).order_by("name"), self.member, event_day)
        self.meal_choices_open = allow_closed_meals or event_day.event.meal_orders_are_open()
        self.menu_options = []
        self.beverage_options = []
        self.snack_beverage_settings = {}
        order = MealOrder.objects.filter(registration=registration).first() if registration else None
        self.official_exemption = official_meal_exemption(self.member, order, reactivating=True, form=self)
        if self.meal_choices_open:
            availability = self.data.get("availability") if self.is_bound else self.initial.get("availability", Registration.Availability.FULL_DAY)
            expected = Menu.MealType.FULL_MEAL if availability == Registration.Availability.FULL_DAY else Menu.MealType.SNACK
            menus = menu_options_for_day(event_day, active_only=False).filter(Q(is_active=True) | Q(pk=order.menu_id if order else None))
            allowed = Q(is_active=True, meal_type=expected)
            if order:
                allowed |= Q(pk=order.menu_id, meal_type=expected)
                if order.legacy_choice_availability == availability:
                    allowed |= Q(pk=order.menu_id)
            self.fields["no_meal"] = forms.BooleanField(label="Je ne souhaite pas de repas ni de collation", required=False)
            self.fields["menu"] = forms.ModelChoiceField(label="Repas complet ou collation", queryset=menus.filter(allowed).order_by("name"), required=False, empty_label="Choisir selon la présence")
            configure_snack_fields(self, event_day, availability, order)
            configure_beverage_fields(self, event_day, availability, order)
            self.fields["payment_method"] = forms.ChoiceField(label="Mode de paiement souhaité", choices=MealOrder.PaymentMethod.choices, initial=MealOrder.PaymentMethod.COUNTER, required=False)
            self.initial.update({"no_meal": not order.wants_meal if order else not menus.filter(allowed).exists(), "menu": order.menu_id if order else None, "beverage": order.beverage_id if order else None, "payment_method": order.payment_method if order else MealOrder.PaymentMethod.COUNTER})
            for menu in menus:
                standard = _meal_component_surcharge(menu, "menu", order)
                self.menu_options.append({"id": str(menu.pk), "name": menu.name, "origin": "Commun" if menu.event_day_id is None else "Spécifique à cet événement", "label": meal_option_label(menu, official=self.official_exemption, surcharge_cents=standard), "standard_label": meal_option_label(menu, surcharge_cents=standard), "official_label": meal_option_label(menu, official=True), "type": menu.meal_type, "surcharge": 0 if self.official_exemption else standard, "standard_surcharge": standard, "legacy": order.legacy_choice_availability if order and order.menu_id == menu.pk else ""})
            self.order_fields(("availability", "preferred_station", "note", "no_meal", *MEAL_FIELD_ORDER, "payment_method"))
        configure_official_meal_pricing(self, self.member, order, reactivating=True)

    def clean(self):
        cleaned = super().clean()
        if not self.meal_choices_open:
            return cleaned
        no_meal = cleaned.get("no_meal", False)
        clean_snack_choices(self, cleaned, availability=cleaned.get("availability"), no_meal=no_meal)
        menu, beverage = cleaned.get("menu"), cleaned.get("beverage")
        if no_meal and (menu or beverage):
            raise ValidationError("Décochez « Je ne souhaite pas de repas ni de collation » avant de choisir un menu ou une boisson.")
        if not no_meal and menu is None and "menu" not in self.errors:
            if not self.fields["menu"].queryset.exists() and beverage is None and not any(cleaned.get(name) for name in SNACK_CHOICE_FIELDS):
                cleaned["no_meal"] = True
            else:
                self.add_error("menu", "Choisissez le repas ou la collation, ou indiquez que vous n’en souhaitez pas.")
        return normalize_meal_payment(self, cleaned)



class FamilyMealForm(forms.Form):
    """Réservation d'un repas liée à une inscription active."""

    no_meal = forms.BooleanField(label="Pas de repas ni de collation", required=False)
    payment_method = forms.ChoiceField(label="Mode de paiement souhaité", choices=MealOrder.PaymentMethod.choices, initial=MealOrder.PaymentMethod.COUNTER)
    menu = forms.ModelChoiceField(
        label="Menu",
        queryset=Menu.objects.none(),
        required=False,
        empty_label="Aucun menu sélectionné",
    )
    beverage = forms.ModelChoiceField(
        label="Boisson",
        queryset=Beverage.objects.none(),
        required=False,
        empty_label="Aucune boisson sélectionnée",
    )

    def __init__(self, *args, registration, meal_order=None, **kwargs):
        self.registration = registration
        self.meal_order = meal_order
        if meal_order is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "no_meal": not meal_order.wants_meal,
                "menu": meal_order.menu_id,
                "beverage": meal_order.beverage_id,
                "payment_method": meal_order.payment_method,
            }
        super().__init__(*args, **kwargs)
        event_day = registration.event_day
        expected = Menu.MealType.FULL_MEAL if registration.availability == Registration.Availability.FULL_DAY else Menu.MealType.SNACK
        allowed = Q(is_active=True, meal_type=expected)
        if meal_order:
            allowed |= Q(pk=meal_order.menu_id, meal_type=expected)
            if meal_order.legacy_choice_availability == registration.availability:
                allowed |= Q(pk=meal_order.menu_id)
        self.fields["menu"].queryset = menu_options_for_day(event_day, active_only=False).filter(allowed).order_by("name")
        self.fields["menu"].label = "Repas complet" if expected == Menu.MealType.FULL_MEAL else "Collation"
        configure_snack_fields(self, event_day, registration.availability, meal_order)
        configure_beverage_fields(self, event_day, registration.availability, meal_order)
        self.order_fields(("no_meal", *MEAL_FIELD_ORDER, "payment_method"))
        self.menu_options = []
        configure_official_meal_pricing(self, registration.member, meal_order)

    def clean(self):
        cleaned_data = super().clean()
        no_meal = cleaned_data.get("no_meal", False)
        clean_snack_choices(self, cleaned_data, availability=self.registration.availability, no_meal=no_meal)
        menu = cleaned_data.get("menu")
        beverage = cleaned_data.get("beverage")
        if no_meal and (menu or beverage):
            raise ValidationError(
                "Décochez « Pas de repas » pour choisir un menu ou une boisson."
            )
        if not no_meal and menu is None:
            self.add_error("menu", "Choisissez un menu ou indiquez « Pas de repas ».")
        return normalize_meal_payment(self, cleaned_data)

