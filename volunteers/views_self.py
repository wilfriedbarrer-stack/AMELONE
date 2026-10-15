"""Espace personnel pour participer sans perdre ses droits de gestion."""

from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Min
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms_self import PersonalProfileForm, PersonalRegistrationForm
from .models import Assignment, Event, EventDay, Family, Member, Registration, SiteBranding, User
from .self_participation import save_personal_participation
from .views_family import _report_validation_error


def participant_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not (
            request.user.is_superuser or request.user.is_competition_official
        ):
            raise PermissionDenied("Un profil administrateur ou officiel de compétition est nécessaire.")
        return view(request, *args, **kwargs)
    return wrapped


def _participant_for(user):
    member = Member.objects.select_related("family").filter(user=user).first()
    if member and member.family.account_id != user.pk:
        raise PermissionDenied("Votre profil personnel n’est plus rattaché à votre compte.")
    return member


@participant_required
def personal_participation(request):
    member = _participant_for(request.user)
    statuses = [Event.Status.PUBLISHED]
    if request.user.is_superuser:
        statuses.append(Event.Status.DRAFT)
    events = Event.objects.filter(status__in=statuses).annotate(first_day=Min("days__date"))
    events = events.prefetch_related("days").order_by("first_day", "name")
    registrations = Registration.objects.none()
    if member:
        shared_sumup_url = SiteBranding.objects.filter(pk=1).values_list("sumup_payment_url", flat=True).first() or ""
        registrations = Registration.objects.filter(member=member, status=Registration.Status.ACTIVE)
        registrations = registrations.select_related("event_day__event", "meal_order__menu").prefetch_related(
            "assignments__station_slot__station",
        ).order_by("event_day__date")
        for registration in registrations:
            registration.sumup_url = shared_sumup_url or registration.event_day.event.sumup_payment_url
            registration.current_assignments = [item for item in registration.assignments.all()
                                                if item.status != Assignment.Status.CANCELLED]
    return render(request, "participation/dashboard.html", {
        "member": member, "events": events, "registrations": registrations,
    })


@participant_required
@require_http_methods(["GET", "POST"])
def personal_profile(request):
    member = _participant_for(request.user)
    form = PersonalProfileForm(request.POST or None, user=request.user, member=member)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                user = User.objects.select_for_update().get(pk=request.user.pk)
                if not user.is_active or not (user.is_superuser or user.is_competition_official):
                    raise PermissionDenied("Vos droits de participation ont changé.")
                member = _participant_for(user)
                if member:
                    member = Member.objects.select_for_update().get(pk=member.pk)
                else:
                    selected = form.cleaned_data.get("existing_member")
                    if selected:
                        member = Member.objects.select_for_update().get(pk=selected.pk)
                        if member.family.account_id != user.pk or member.user_id or member.is_archived:
                            raise ValidationError("Ce membre ne peut plus être associé à votre profil personnel.")
                    else:
                        family, _ = Family.objects.get_or_create(account=user)
                        member = Member(family=family)
                if not form.cleaned_data.get("existing_member"):
                    member.first_name = form.cleaned_data["first_name"].strip()
                    member.last_name = form.cleaned_data["last_name"].strip()
                    member.birth_date = form.cleaned_data["birth_date"]
                member.user = user
                member.archived_at = None
                member.full_clean()
                if member._state.adding:
                    member.save()
                else:
                    member.save(update_fields=["first_name", "last_name", "birth_date", "user", "archived_at", "updated_at"])
                user.first_name, user.last_name = member.first_name, member.last_name
                user.save(update_fields=["first_name", "last_name"])
            messages.success(request, "Votre profil personnel est enregistré. Vous pouvez participer aux événements.")
            return redirect("personal_participation")
        except ValidationError as error:
            _report_validation_error(form, error)
    return render(request, "participation/profile.html", {"form": form, "member": member})


@participant_required
@require_http_methods(["GET", "POST"])
def personal_registration(request, event_day_id):
    member = _participant_for(request.user)
    if not member or member.is_archived:
        messages.info(request, "Complétez votre profil personnel pour vous inscrire à un événement.")
        return redirect("personal_profile")
    statuses = [Event.Status.PUBLISHED]
    if request.user.is_superuser:
        statuses.append(Event.Status.DRAFT)
    day = get_object_or_404(EventDay.objects.select_related("event"), pk=event_day_id,
                            event__status__in=statuses)
    registration = Registration.objects.filter(member=member, event_day=day).first()
    form = PersonalRegistrationForm(request.POST or None, user=request.user, event_day=day,
                                    registration=registration, member=member)
    if request.method == "POST" and form.is_valid():
        try:
            choices = None
            if form.meal_choices_open:
                choices = {key: form.cleaned_data[key] for key in
                           ("menu", "beverage", "no_meal", "payment_method")}
            saved = save_personal_participation(
                actor=request.user, event_day=day, availability=form.cleaned_data["availability"],
                preferred_station=form.cleaned_data.get("preferred_station"),
                note=form.cleaned_data["note"], meal_choices=choices,
                station_slots=form.cleaned_data.get("station_slots"),
            )
            if getattr(saved, "coverage_invalidated_assignment_ids", []):
                messages.warning(request, "Des affectations de mineurs sont repassées en brouillon : un adulte doit être confirmé sur leur créneau.")
            messages.success(request, "Votre participation a été enregistrée." + (
                " Vos postes sélectionnés sont confirmés." if request.user.is_superuser else ""
            ))
            return redirect("personal_participation")
        except ValidationError as error:
            _report_validation_error(form, error)
    return render(request, "participation/registration.html", {
        "form": form, "member": member, "event_day": day,
    })
