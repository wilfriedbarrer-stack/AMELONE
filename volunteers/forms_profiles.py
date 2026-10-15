"""Choix d'un profil et de la date du nouvel événement."""

from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseFormSet, formset_factory

from .forms_organizer import OrganizerFormMixin, StationForm
from .models import EventProfile, StationSlot


class SaveEventProfileForm(OrganizerFormMixin, forms.ModelForm):
    class Meta:
        model = EventProfile
        fields = ("name", "description")
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}


class CreateFromProfileForm(OrganizerFormMixin, forms.Form):
    name = forms.CharField(label="Nom du nouvel événement", max_length=200)
    event_date = forms.DateField(
        label="Date de l'événement",
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )


class ProfileEventCreationForm(CreateFromProfileForm):
    profile = forms.ModelChoiceField(
        label="Profil d’événement", queryset=EventProfile.objects.all(),
        empty_label="Choisir un profil réutilisable",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.order_fields(("profile", "name", "event_date"))


class ProfileVersionMixin:
    def clean(self):
        cleaned = super().clean()
        if self.fields["expected_updated_at"].required and not cleaned.get("expected_updated_at"):
            raise ValidationError("La version de ce profil est manquante ou invalide. Actualisez la page avant d’enregistrer.")
        return cleaned


class ReusableProfileForm(ProfileVersionMixin, OrganizerFormMixin, forms.Form):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput, max_length=100)
    name = forms.CharField(label="Nom du profil réutilisable", max_length=200)
    description = forms.CharField(
        label="Description du profil", required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Une note pour retrouver ce modèle dans la liste des profils.",
    )
    location = forms.CharField(label="Lieu habituel", max_length=255)
    event_description = forms.CharField(
        label="Description proposée pour les événements", required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    general_instructions = forms.CharField(
        label="Consignes générales", required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    morning_hot_beverage_enabled = forms.BooleanField(
        label="Proposer une boisson chaude avec la collation du matin", required=False, initial=True,
    )
    afternoon_cold_beverage_enabled = forms.BooleanField(
        label="Proposer une boisson fraîche avec la collation de l’après-midi", required=False, initial=True,
    )

    def __init__(self, *args, profile=None, **kwargs):
        self.profile = profile
        if profile:
            snapshot = profile.snapshot if isinstance(profile.snapshot, dict) else {}
            event = snapshot.get("event", {})
            event = event if isinstance(event, dict) else {}
            initial = kwargs.setdefault("initial", {})
            initial.update({
                "name": profile.name,
                "description": profile.description,
                "location": event.get("location", ""),
                "event_description": event.get("description", ""),
                "general_instructions": event.get("general_instructions", ""),
                "morning_hot_beverage_enabled": event.get("morning_hot_beverage_enabled", True),
                "afternoon_cold_beverage_enabled": event.get("afternoon_cold_beverage_enabled", True),
                "expected_updated_at": profile.updated_at.isoformat(),
            })
        super().__init__(*args, **kwargs)
        self.fields["expected_updated_at"].required = profile is not None


class ProfileStationForm(ProfileVersionMixin, StationForm):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput, max_length=100)


class ProfileStationDeleteForm(ProfileVersionMixin, forms.Form):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput, max_length=100)


class ProfileSlotForm(OrganizerFormMixin, forms.Form):
    day_offset = forms.TypedChoiceField(label="Journée du profil", coerce=int)
    period = forms.ChoiceField(
        label="Période", choices=[("", "Choisir une période"), *StationSlot.Period.choices],
    )
    capacity = forms.IntegerField(label="Nombre de places", min_value=0, max_value=32767)
    start_time = forms.TimeField(
        label="Heure de début", required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )
    end_time = forms.TimeField(
        label="Heure de fin", required=False,
        widget=forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
    )

    def __init__(self, *args, day_offsets, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["day_offset"].choices = [("", "Choisir une journée"), *[
            (offset, "Jour de l’événement" if offset == 0 else f"Jour + {offset}")
            for offset in day_offsets
        ]]

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("start_time"), cleaned.get("end_time")
        if "start_time" not in self.errors and "end_time" not in self.errors:
            if bool(start) != bool(end):
                self.add_error("end_time", "Renseignez les deux horaires ou laissez-les tous les deux vides.")
            elif start and end and start >= end:
                self.add_error("end_time", "L’heure de fin doit suivre l’heure de début.")
        return cleaned


class BaseProfileSlotFormSet(BaseFormSet):
    def clean(self):
        super().clean()
        selected = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            if form.errors:
                continue
            key = (form.cleaned_data["day_offset"], form.cleaned_data["period"])
            if key in selected:
                raise ValidationError("Une même journée et une même période ne peuvent figurer qu’une fois pour ce poste.")
            selected.add(key)


ProfileSlotFormSet = formset_factory(
    ProfileSlotForm, formset=BaseProfileSlotFormSet,
    extra=1, can_delete=True, max_num=1000, validate_max=True, absolute_max=1000,
)
