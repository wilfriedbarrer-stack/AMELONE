"""Choix d'un profil et de la date du nouvel événement."""

from django import forms

from .forms_organizer import OrganizerFormMixin
from .models import EventProfile


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
