"""Profil personnel et participation des administrateurs et officiels."""

from django import forms
from django.db.models import Count, Q

from .forms_eligibility import eligible_slots, station_requirements_label
from .forms_family import FamilyRegistrationForm, MemberForm, configure_official_meal_pricing
from .models import Assignment, MealOrder, Member, StationSlot


class PersonalProfileForm(forms.Form):
    existing_member = forms.ModelChoiceField(
        label="Mon profil de participant",
        queryset=Member.objects.none(), required=False,
        empty_label="Créer un nouveau participant",
    )
    first_name = forms.CharField(label="Prénom", max_length=150, required=False)
    last_name = forms.CharField(label="Nom", max_length=150, required=False)
    birth_date = forms.DateField(
        label="Date de naissance", required=False,
        widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
    )

    def __init__(self, *args, user, member=None, **kwargs):
        self.member = member
        self.has_existing_member_choices = False
        super().__init__(*args, **kwargs)
        if member:
            self.fields.pop("existing_member")
            self.initial.update({"first_name": member.first_name, "last_name": member.last_name,
                                 "birth_date": member.birth_date})
        else:
            self.fields["existing_member"].queryset = Member.objects.filter(
                family__account=user, archived_at__isnull=True, user__isnull=True,
            )
            self.has_existing_member_choices = self.fields["existing_member"].queryset.exists()
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
        requirements = station_requirements_label(slot.station)
        return (f"{slot.station.name}{inactive} · {slot.station.get_category_display()} · "
                f"{slot.get_period_display()} · {hours} "
                f"· {remaining} place(s) restante(s)" + (f" · {requirements}" if requirements else ""))


class PersonalRegistrationForm(FamilyRegistrationForm):
    def __init__(self, *args, user, event_day, registration=None, member=None, **kwargs):
        member = member or Member.objects.filter(user=user, family__account=user).first()
        super().__init__(*args, event_day=event_day, registration=registration,
                         member=member, allow_closed_meals=user.is_superuser, **kwargs)
        if user.is_superuser:
            self.fields.pop("preferred_station")
            current_slot_ids = list(registration.assignments.exclude(
                status=Assignment.Status.CANCELLED,
            ).values_list("station_slot_id", flat=True)) if registration else []
            available_slots = StationSlot.objects.filter(event_day=event_day)
            available_slots = available_slots.filter(
                Q(station__is_active=True) | Q(pk__in=current_slot_ids),
            ).select_related("station", "event_day")
            eligible_ids = list(eligible_slots(available_slots, member, event_day).values_list("pk", flat=True))
            self.fields["station_slots"] = PersonalSlotChoiceField(
                label="Mes postes et périodes", required=False,
                queryset=available_slots.filter(Q(pk__in=eligible_ids) | Q(pk__in=current_slot_ids))
                .select_related("station")
                .annotate(assignment_count=Count("assignments", filter=Q(
                    assignments__status__in=[Assignment.Status.DRAFT, Assignment.Status.CONFIRMED],
                    assignments__registration__status="active",
                )))
                .order_by("station__position", "station__name", "start_time"),
                widget=forms.CheckboxSelectMultiple,
                help_text="Cochez vos postes pour confirmer vos affectations et décochez-les pour les retirer. Un poste d’officiel nécessite une habilitation attribuée à votre profil personnel par un administrateur. Licence et formation à jour nécessaires.",
            )
            if registration:
                self.initial["station_slots"] = current_slot_ids
            self.order_fields(("availability", "station_slots", "no_meal", "morning_snack", "morning_beverage",
                               "menu", "beverage", "afternoon_snack", "afternoon_beverage", "payment_method", "note"))
            order = MealOrder.objects.filter(registration=registration).first() if registration else None
            configure_official_meal_pricing(self, member, order, reactivating=True)

    def clean(self):
        cleaned = super().clean()
        availability = cleaned.get("availability")
        for slot in cleaned.get("station_slots", []):
            if availability != "full_day" and slot.period != availability:
                self.add_error("station_slots", "Choisissez des postes correspondant à votre présence.")
                break
        return cleaned
