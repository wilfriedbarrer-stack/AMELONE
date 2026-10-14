"""Formulaires réservés à l'espace organisateur.

Les clés étrangères proposées par les formulaires sont systématiquement
limitées à l'événement ou à la journée affichée.  Ces restrictions améliorent
l'interface mais ne remplacent pas les contrôles transactionnels des services
métier utilisés par les vues.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (
    Assignment,
    Attendance,
    Beverage,
    Event,
    EventDay,
    Family,
    MealOrder,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
)


class OrganizerFormMixin:
    """Ajoute des attributs HTML cohérents sans imposer une bibliothèque CSS."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            css_class = "form-check-input" if isinstance(field.widget, forms.CheckboxInput) else "form-control"
            if isinstance(field.widget, (forms.Select, forms.SelectMultiple)):
                css_class = "form-select"
            field.widget.attrs.setdefault("class", css_class)


class EventForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = Event
        fields = (
            "name",
            "description",
            "location",
            "registration_opens_at",
            "registration_closes_at",
            "meal_orders_close_at",
            "sumup_payment_url",
            "general_instructions",
            "status",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "general_instructions": forms.Textarea(attrs={"rows": 4}),
            "registration_opens_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "registration_closes_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
            "meal_orders_close_at": forms.DateTimeInput(
                attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in (
            "registration_opens_at",
            "registration_closes_at",
            "meal_orders_close_at",
        ):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"]


class EventDayForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = EventDay
        fields = ("date",)
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class DuplicateEventForm(OrganizerFormMixin, forms.Form):
    name = forms.CharField(label="Nom du nouvel événement", max_length=200)
    copy_catering = forms.BooleanField(
        label="Copier aussi les menus et boissons",
        required=False,
        initial=True,
    )


EventDayFormSet = inlineformset_factory(
    Event,
    EventDay,
    form=EventDayForm,
    fields=("date",),
    extra=1,
    can_delete=True,
)


class StationForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = Station
        fields = (
            "name",
            "description",
            "instructions",
            "category",
            "is_active",
            "position",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "instructions": forms.Textarea(attrs={"rows": 3}),
        }


class StationSlotForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = StationSlot
        fields = ("event_day", "period", "capacity", "start_time", "end_time")
        widgets = {
            "start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
            "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
        }

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is None and self.instance and self.instance.pk:
            event = self.instance.station.event
        self.fields["event_day"].queryset = (
            EventDay.objects.filter(event=event).order_by("date")
            if event is not None
            else EventDay.objects.none()
        )


StationSlotFormSet = inlineformset_factory(
    Station,
    StationSlot,
    form=StationSlotForm,
    fields=("event_day", "period", "capacity", "start_time", "end_time"),
    extra=1,
    can_delete=True,
)


class RegistrationOrganizerForm(OrganizerFormMixin, forms.ModelForm):
    """Correction d'une disponibilité par un organisateur, même après clôture."""

    class Meta:
        model = Registration
        fields = ("availability", "preferred_station", "note", "status")
        widgets = {"note": forms.Textarea(attrs={"rows": 3})}

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event is None and self.instance and self.instance.pk:
            event = self.instance.event_day.event
        self.fields["preferred_station"].queryset = (
            Station.objects.filter(event=event, is_active=True).order_by("position", "name")
            if event is not None
            else Station.objects.none()
        )
        self.fields["preferred_station"].empty_label = "Sans préférence"


# Nom court conservé pour les imports de templates/tests.
RegistrationForm = RegistrationOrganizerForm


class AssignmentForm(OrganizerFormMixin, forms.Form):
    registration = forms.ModelChoiceField(
        label="Bénévole",
        queryset=Registration.objects.none(),
    )
    station_slot = forms.ModelChoiceField(
        label="Poste et créneau",
        queryset=StationSlot.objects.none(),
    )

    def __init__(
        self,
        *args,
        event: Event | None = None,
        event_day: EventDay | None = None,
        registration: Registration | None = None,
        assignment: Assignment | None = None,
        **kwargs,
    ):
        self.event = event
        self.event_day = event_day
        self.assignment = assignment
        if assignment is not None:
            registration = assignment.registration
            event_day = registration.event_day
            event = event_day.event
            kwargs.setdefault(
                "initial",
                {"registration": registration.pk, "station_slot": assignment.station_slot_id},
            )
        elif registration is not None:
            event_day = registration.event_day
            event = event_day.event
            kwargs.setdefault("initial", {"registration": registration.pk})
        self.event = event
        self.event_day = event_day
        super().__init__(*args, **kwargs)

        registrations = Registration.objects.filter(status=Registration.Status.ACTIVE)
        slots = StationSlot.objects.filter(station__is_active=True)
        if event_day is not None:
            registrations = registrations.filter(event_day=event_day)
            slots = slots.filter(event_day=event_day)
        elif event is not None:
            registrations = registrations.filter(event_day__event=event)
            slots = slots.filter(event_day__event=event)
        else:
            registrations = registrations.none()
            slots = slots.none()
        if registration is not None:
            registrations = registrations.filter(pk=registration.pk)
        self.fields["registration"].queryset = registrations.select_related(
            "member", "event_day"
        ).order_by("event_day__date", "member__last_name", "member__first_name")
        self.fields["station_slot"].queryset = slots.select_related(
            "station", "event_day"
        ).order_by("event_day__date", "period", "station__position", "station__name")
        if registration is not None:
            self.fields["registration"].widget = forms.HiddenInput()

    def clean(self):
        cleaned = super().clean()
        registration = cleaned.get("registration")
        slot = cleaned.get("station_slot")
        if not registration or not slot:
            return cleaned
        if registration.event_day_id != slot.event_day_id:
            self.add_error("station_slot", "Le créneau ne correspond pas à la journée d'inscription.")
        elif not registration.covers_period(slot.period):
            self.add_error(
                "station_slot",
                "Ce créneau se trouve hors de la disponibilité déclarée.",
            )
        return cleaned


class ConfirmationEmailForm(OrganizerFormMixin, forms.Form):
    families = forms.ModelMultipleChoiceField(
        label="Familles destinataires",
        queryset=Family.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
    )
    send_to_all = forms.BooleanField(
        label="Toutes les familles concernées",
        required=False,
        initial=True,
    )
    extra_instructions = forms.CharField(
        label="Consignes complémentaires",
        required=False,
        widget=forms.Textarea(attrs={"rows": 4}),
    )

    def __init__(self, *args, event: Event, failed_only=False, **kwargs):
        self.event = event
        self.failed_only = failed_only
        super().__init__(*args, **kwargs)
        families = Family.objects.filter(
            members__registrations__event_day__event=event,
            members__registrations__assignments__isnull=False,
        )
        if failed_only:
            families = families.filter(
                email_deliveries__event=event,
                email_deliveries__status="failed",
            )
        self.fields["families"].queryset = families.select_related("account").distinct()

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("send_to_all") and not cleaned.get("families"):
            self.add_error("families", "Sélectionnez au moins une famille.")
        return cleaned


EmailConfirmationForm = ConfirmationEmailForm


class PricedOptionFormMixin(OrganizerFormMixin, forms.ModelForm):
    surcharge_euros = forms.DecimalField(
        label="Supplément (€)",
        min_value=Decimal("0.00"),
        max_digits=8,
        decimal_places=2,
        initial=Decimal("0.00"),
    )

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        initial = kwargs.setdefault("initial", {})
        if instance is not None and instance.pk:
            initial.setdefault("surcharge_euros", Decimal(instance.surcharge_cents) / 100)
        super().__init__(*args, **kwargs)

    def save(self, commit=True):
        instance = super().save(commit=False)
        amount = self.cleaned_data["surcharge_euros"]
        instance.surcharge_cents = int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))
        if commit:
            instance.save()
            self.save_m2m()
        return instance


class MenuForm(PricedOptionFormMixin):
    class Meta:
        model = Menu
        fields = ("name", "description", "is_active")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class BeverageForm(PricedOptionFormMixin):
    class Meta:
        model = Beverage
        fields = ("name", "description", "is_active")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class MealOrderOrganizerForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = MealOrder
        fields = ("wants_meal", "menu", "beverage")

    def __init__(self, *args, registration=None, **kwargs):
        super().__init__(*args, **kwargs)
        if registration is None and self.instance and self.instance.pk:
            registration = self.instance.registration
        self.registration = registration
        day = registration.event_day if registration is not None else None
        self.fields["menu"].queryset = (
            Menu.objects.filter(event_day=day, is_active=True).order_by("name")
            if day is not None
            else Menu.objects.none()
        )
        self.fields["beverage"].queryset = (
            Beverage.objects.filter(event_day=day, is_active=True).order_by("name")
            if day is not None
            else Beverage.objects.none()
        )

    def clean(self):
        cleaned = super().clean()
        wants_meal = cleaned.get("wants_meal")
        if wants_meal and cleaned.get("menu") is None:
            self.add_error("menu", "Choisissez un menu.")
        if not wants_meal and (cleaned.get("menu") or cleaned.get("beverage")):
            raise ValidationError("Un repas non demandé ne peut contenir ni menu ni boisson.")
        return cleaned


MealOrderForm = MealOrderOrganizerForm


class PaymentForm(OrganizerFormMixin, forms.Form):
    family = forms.ModelChoiceField(label="Famille", queryset=Family.objects.none())
    amount_euros = forms.DecimalField(
        label="Montant (€)",
        min_value=Decimal("0.01"),
        max_digits=10,
        decimal_places=2,
    )
    paid_at = forms.DateTimeField(
        label="Date et heure du règlement",
        initial=timezone.now,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"],
    )
    method = forms.ChoiceField(label="Mode", choices=Payment.Method.choices)
    note = forms.CharField(
        label="Remarque",
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )

    def __init__(self, *args, event: Event, family: Family | None = None, **kwargs):
        self.event = event
        self.family = family
        super().__init__(*args, **kwargs)
        families = Family.objects.filter(
            Q(members__registrations__event_day__event=event) | Q(payments__event=event)
        ).distinct()
        if family is not None:
            families = families.filter(pk=family.pk)
            self.fields["family"].initial = family
            self.fields["family"].widget = forms.HiddenInput()
        self.fields["family"].queryset = families.select_related("account").order_by("name")

    @property
    def amount_cents(self):
        amount = self.cleaned_data["amount_euros"]
        return int((amount * 100).quantize(Decimal("1"), ROUND_HALF_UP))


class AttendanceForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = Attendance
        fields = ("status", "note")
        widgets = {"note": forms.Textarea(attrs={"rows": 2})}


class AnnualTrackingFilterForm(OrganizerFormMixin, forms.Form):
    year = forms.ChoiceField(label="Année", required=False)
    event = forms.ModelChoiceField(
        label="Événement",
        queryset=Event.objects.none(),
        required=False,
        empty_label="Tous les événements",
    )
    query = forms.CharField(label="Famille ou membre", required=False, max_length=150)
    attendance = forms.ChoiceField(
        label="Présence",
        required=False,
        choices=(("", "Toutes"), *Attendance.Status.choices),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        years = list(
            EventDay.objects.order_by("-date")
            .values_list("date__year", flat=True)
            .distinct()
        )
        current_year = timezone.localdate().year
        if current_year not in years:
            years.insert(0, current_year)
        self.fields["year"].choices = [("", "Toutes"), *((str(year), str(year)) for year in years)]
        self.fields["event"].queryset = Event.objects.exclude(
            status=Event.Status.ARCHIVED
        ).order_by("-registration_opens_at", "name")


TrackingFilterForm = AnnualTrackingFilterForm
