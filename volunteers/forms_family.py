"""Formulaires de l'espace famille.

Les listes de choix sensibles sont toujours reconstruites côté serveur à partir
de la journée concernée. Une valeur forgée dans le navigateur ne peut donc pas
référencer un poste, un menu ou une boisson d'un autre événement.
"""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import Beverage, Family, MealOrder, Member, Menu, Registration, Station

User = get_user_model()


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


class FamilyRegistrationForm(forms.Form):
    """Saisie d'une disponibilité ; l'enregistrement passe par le service métier."""

    availability = forms.ChoiceField(
        label="Disponibilité",
        choices=Registration.Availability.choices,
    )
    preferred_station = forms.ModelChoiceField(
        label="Poste souhaité (facultatif)",
        queryset=Station.objects.none(),
        required=False,
        empty_label="Sans préférence",
        help_text="Ce souhait ne réserve pas le poste et ne garantit pas l'affectation.",
    )
    note = forms.CharField(
        label="Remarque pour l'organisateur",
        required=False,
        max_length=1000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, event_day, registration=None, allow_closed_meals=False, **kwargs):
        self.event_day = event_day
        self.registration = registration
        if registration is not None and "initial" not in kwargs:
            kwargs["initial"] = {
                "availability": registration.availability,
                "preferred_station": registration.preferred_station_id,
                "note": registration.note,
            }
        super().__init__(*args, **kwargs)
        self.fields["preferred_station"].queryset = Station.objects.filter(
            event=event_day.event,
            is_active=True,
        ).order_by("name")
        self.meal_choices_open = allow_closed_meals or event_day.event.meal_orders_are_open()
        self.menu_options = []
        if self.meal_choices_open:
            order = MealOrder.objects.filter(registration=registration).first() if registration else None
            availability = self.data.get("availability") if self.is_bound else self.initial.get("availability", Registration.Availability.FULL_DAY)
            expected = Menu.MealType.FULL_MEAL if availability == Registration.Availability.FULL_DAY else Menu.MealType.SNACK
            menus = Menu.objects.filter(event_day=event_day).filter(Q(is_active=True) | Q(pk=order.menu_id if order else None))
            allowed = Q(is_active=True, meal_type=expected)
            if order:
                allowed |= Q(pk=order.menu_id, meal_type=expected)
                if order.legacy_choice_availability == availability:
                    allowed |= Q(pk=order.menu_id)
            self.fields["no_meal"] = forms.BooleanField(label="Je ne souhaite pas de repas ni de collation", required=False)
            self.fields["menu"] = forms.ModelChoiceField(label="Repas complet ou collation", queryset=menus.filter(allowed).order_by("name"), required=False, empty_label="Choisir selon la présence")
            self.fields["beverage"] = forms.ModelChoiceField(label="Boisson", queryset=Beverage.objects.filter(event_day=event_day).filter(Q(is_active=True) | Q(pk=order.beverage_id if order else None)).order_by("name"), required=False, empty_label="Sans boisson")
            self.fields["payment_method"] = forms.ChoiceField(label="Mode de paiement souhaité", choices=MealOrder.PaymentMethod.choices, initial=MealOrder.PaymentMethod.COUNTER, required=False)
            self.initial.update({"no_meal": not order.wants_meal if order else not menus.filter(allowed).exists(), "menu": order.menu_id if order else None, "beverage": order.beverage_id if order else None, "payment_method": order.payment_method if order else MealOrder.PaymentMethod.COUNTER})
            for menu in menus:
                self.menu_options.append({"id": str(menu.pk), "name": menu.name, "type": menu.meal_type, "surcharge": menu.surcharge_cents, "legacy": order.legacy_choice_availability if order and order.menu_id == menu.pk else ""})

    def clean(self):
        cleaned = super().clean()
        if not self.meal_choices_open:
            return cleaned
        no_meal = cleaned.get("no_meal", False)
        menu, beverage = cleaned.get("menu"), cleaned.get("beverage")
        if no_meal and (menu or beverage):
            raise ValidationError("Décochez « Je ne souhaite pas de repas ni de collation » avant de choisir un menu ou une boisson.")
        if not no_meal and menu is None and "menu" not in self.errors:
            if not self.fields["menu"].queryset.exists() and beverage is None:
                cleaned["no_meal"] = True
            else:
                self.add_error("menu", "Choisissez le repas ou la collation, ou indiquez que vous n’en souhaitez pas.")
        cleaned["payment_method"] = cleaned.get("payment_method") or MealOrder.PaymentMethod.COUNTER
        return cleaned



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
        self.fields["menu"].queryset = Menu.objects.filter(event_day=event_day).filter(allowed).order_by("name")
        self.fields["menu"].label = "Repas complet" if expected == Menu.MealType.FULL_MEAL else "Collation"
        self.fields["beverage"].queryset = Beverage.objects.filter(event_day=event_day).filter(Q(is_active=True) | Q(pk=meal_order.beverage_id if meal_order else None)).order_by("name")

    def clean(self):
        cleaned_data = super().clean()
        no_meal = cleaned_data.get("no_meal", False)
        menu = cleaned_data.get("menu")
        beverage = cleaned_data.get("beverage")
        if no_meal and (menu or beverage):
            raise ValidationError(
                "Décochez « Pas de repas » pour choisir un menu ou une boisson."
            )
        if not no_meal and menu is None:
            self.add_error("menu", "Choisissez un menu ou indiquez « Pas de repas ».")
        return cleaned_data

