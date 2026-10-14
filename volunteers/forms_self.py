"""Profil personnel et participation des administrateurs et officiels."""

from django import forms
from django.db.models import Count, Q

from .forms_family import FamilyRegistrationForm, MemberForm
from .models import Assignment, Member, StationSlot


class PersonalProfileForm(forms.Form):
    existing_member = forms.ModelChoiceField(
        label="Mon profil existe déjà parmi les membres",
        queryset=Member.objects.none(), required=False,
        empty_label="Créer mon profil avec les informations ci-dessous",
    )
    first_name = forms.CharField(label="Prénom", max_length=150, required=False)
    last_name = forms.CharField(label="Nom", max_length=150, required=False)
    birth_date = forms.DateField(
        label="Date de naissance", required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, user, member=None, **kwargs):
        self.member = member
        super().__init__(*args, **kwargs)
        if member:
            self.fields.pop("existing_member")
            self.initial.update({"first_name": member.first_name, "last_name": member.last_name,
                                 "birth_date": member.birth_date})
        else:
            self.fields["existing_member"].queryset = Member.objects.filter(
                family__account=user, archived_at__isnull=True, user__isnull=True,
            )
            self.initial.update({"first_name": user.first_name, "last_name": user.last_name})

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("existing_member"):
            return cleaned
        identity = MemberForm({key: cleaned.get(key) for key in ("first_name", "last_name", "birth_date")})
        if not identity.is_valid():
            for field, errors in identity.errors.items():
                for error in errors:
                    self.add_error(field, error)
        return cleaned


class PersonalSlotChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, slot):
        remaining = max(0, slot.capacity - slot.assignment_count)
        hours = f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}" if slot.start_time and slot.end_time else "Horaires à préciser"
        inactive = " (poste inactif, affectation existante)" if not slot.station.is_active else ""
        return (f"{slot.station.name}{inactive} · {slot.station.get_category_display()} · "
                f"{slot.get_period_display()} · {hours} "
                f"· {remaining} place(s) restante(s)")


class PersonalRegistrationForm(FamilyRegistrationForm):
    def __init__(self, *args, user, event_day, registration=None, **kwargs):
        super().__init__(*args, event_day=event_day, registration=registration,
                         allow_closed_meals=user.is_superuser, **kwargs)
        if user.is_superuser:
            self.fields.pop("preferred_station")
            current_slot_ids = list(registration.assignments.exclude(
                status=Assignment.Status.CANCELLED,
            ).values_list("station_slot_id", flat=True)) if registration else []
            self.fields["station_slots"] = PersonalSlotChoiceField(
                label="Mes postes et périodes", required=False,
                queryset=StationSlot.objects.filter(event_day=event_day)
                .filter(Q(station__is_active=True) | Q(pk__in=current_slot_ids))
                .select_related("station")
                .annotate(assignment_count=Count("assignments", filter=Q(
                    assignments__status__in=[Assignment.Status.DRAFT, Assignment.Status.CONFIRMED],
                    assignments__registration__status="active",
                )))
                .order_by("station__position", "station__name", "start_time"),
                widget=forms.CheckboxSelectMultiple,
                help_text="Cochez vos postes pour confirmer vos affectations. Décochez un poste pour le retirer. Vous pouvez aussi vous inscrire sans poste.",
            )
            if registration:
                self.initial["station_slots"] = current_slot_ids

    def clean(self):
        cleaned = super().clean()
        availability = cleaned.get("availability")
        for slot in cleaned.get("station_slots", []):
            if availability != "full_day" and slot.period != availability:
                self.add_error("station_slots", "Choisissez des postes correspondant à votre présence.")
                break
        return cleaned
