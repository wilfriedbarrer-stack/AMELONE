"""Confirmation explicite des suppressions dans l’espace organisateur."""

from django import forms
from django.core.exceptions import ValidationError

from .forms_organizer import OrganizerFormMixin


class DeletionConfirmationForm(OrganizerFormMixin, forms.Form):
    expected_updated_at = forms.CharField(widget=forms.HiddenInput, max_length=100)
    confirm = forms.BooleanField(
        label="Je confirme cette suppression.",
        error_messages={"required": "Confirmez cette action avant de continuer."},
    )

    def __init__(self, *args, expected_updated_at, confirm_label, **kwargs):
        initial = kwargs.setdefault("initial", {})
        initial["expected_updated_at"] = expected_updated_at
        super().__init__(*args, **kwargs)
        self.fields["confirm"].label = confirm_label

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("expected_updated_at"):
            raise ValidationError(
                "La version de cet élément est manquante ou invalide. "
                "Actualisez la page avant de confirmer."
            )
        return cleaned
