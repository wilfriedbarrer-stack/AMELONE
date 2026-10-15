from django import forms

from .models import EventDay


class CatalogueUploadForm(forms.Form):
    event_day = forms.ModelChoiceField(
        label="Date de l'événement", queryset=EventDay.objects.none(), empty_label=None
    )
    file = forms.FileField(
        label="Fichier à importer",
        help_text="CSV UTF-8 ou Excel (.xlsx), 2 Mo et 500 lignes maximum.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx"}),
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["event_day"].queryset = event.days.order_by("date")
        if event.days.count() == 1:
            self.fields["event_day"].initial = event.days.first()
            self.fields["event_day"].widget = forms.HiddenInput()
            # Ignore a missing field from older clients, while still validating
            # any explicit value against the event-scoped queryset.
            if self.is_bound and not self.data.get("event_day"):
                self.data = self.data.copy()
                self.data["event_day"] = str(self.fields["event_day"].initial.pk)


class CatalogueConfirmForm(forms.Form):
    preview_token = forms.CharField(widget=forms.HiddenInput)
    confirm_updates = forms.BooleanField(
        label="Je confirme la mise à jour des menus ou boissons existants indiqués dans l'aperçu.",
        required=False,
    )
