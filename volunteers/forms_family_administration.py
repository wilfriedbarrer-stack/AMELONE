"""Formulaires privés des fiches membres et des coordonnées du responsable."""

from django import forms

from .forms_family import FamilyAccountForm, MemberForm
from .models import User


class AdministratorMemberForm(MemberForm):
    class Meta(MemberForm.Meta):
        fields = ("first_name", "last_name", "birth_date", "is_competition_official")
        labels = {
            **MemberForm.Meta.labels,
            "is_competition_official": "Officiel de compétition",
        }
        widgets = {
            "birth_date": forms.DateInput(format="%Y-%m-%d", attrs={"type": "date"}),
        }
        help_texts = {
            "is_competition_official": (
                "Licence et formation à jour nécessaires. "
                "Cette fonction s’attribue à cette personne uniquement."
            ),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["birth_date"].input_formats = ["%Y-%m-%d"]

    def _post_clean(self):
        # Keep MemberForm's birth-date validation and error mapping, while allowing
        # this administrator-only form to validate the submitted official function.
        # MemberForm's own refresh would replace that submitted value from the DB.
        forms.ModelForm._post_clean(self)

    def save(self, commit=True):
        member = forms.ModelForm.save(self, commit=False)
        if commit:
            member.save(update_fields=[
                "first_name", "last_name", "birth_date", "is_competition_official", "updated_at",
            ])
        return member


class AdministratorFamilyAccountForm(FamilyAccountForm):
    email = forms.EmailField(
        label="Adresse e-mail", max_length=User._meta.get_field("email").max_length,
    )
