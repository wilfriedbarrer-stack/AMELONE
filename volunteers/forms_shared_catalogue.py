from django import forms

from .forms_catalogue import CatalogueConfirmForm
from .forms_organizer import BeverageForm, MenuForm
from .shared_catalogue import shared_option_version


class SharedCatalogueFormMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.instance.event_day = None
        self.fields["surcharge_euros"].label = "Prix (€)"
        self.fields["surcharge_euros"].help_text = "Les nouveaux choix utiliseront ce prix. Les montants des réservations existantes sont conservés."
        if not self.instance._state.adding:
            self.fields["version"].required = True
            self.fields["version"].initial = shared_option_version(self.instance)


class SharedMenuForm(SharedCatalogueFormMixin, MenuForm):
    version = forms.CharField(widget=forms.HiddenInput, required=False)
    description = forms.CharField(
        label="Description", required=False, max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class SharedBeverageForm(SharedCatalogueFormMixin, BeverageForm):
    version = forms.CharField(widget=forms.HiddenInput, required=False)
    description = forms.CharField(
        label="Description", required=False, max_length=4000,
        widget=forms.Textarea(attrs={"rows": 3}),
    )


class SharedCatalogueUploadForm(forms.Form):
    file = forms.FileField(
        label="Fichier à importer",
        help_text="CSV UTF-8 ou Excel (.xlsx), 2 Mo et 500 lignes maximum.",
        widget=forms.ClearableFileInput(attrs={"accept": ".csv,.xlsx"}),
    )


class SharedCatalogueConfirmForm(CatalogueConfirmForm):
    confirm_updates = forms.BooleanField(
        label="Je confirme la mise à jour des options communes indiquées dans l'aperçu, pour tous les événements.",
        required=False,
    )


class SharedCataloguePromotionForm(forms.Form):
    version = forms.CharField(widget=forms.HiddenInput)
    confirm = forms.BooleanField(
        label="Je confirme que ce menu ou cette boisson deviendra disponible pour tous les événements présents et futurs.",
    )
