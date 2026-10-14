"""Gestion des profils d'événement dans l'espace organisateur."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.shortcuts import get_object_or_404, redirect, render

from .event_profiles import create_event_from_profile, save_event_profile
from .forms_profiles import CreateFromProfileForm, SaveEventProfileForm
from .models import Event, EventProfile
from .views_organizer import _form_error_messages, organizer_required


@organizer_required
def event_profile_list(request):
    return render(
        request,
        "volunteers/organizer/profiles/list.html",
        {
            "profiles": EventProfile.objects.all(),
        },
    )


@organizer_required
def event_profile_save(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = SaveEventProfileForm(request.POST or None, initial={"name": event.name})
    if request.method == "POST" and form.is_valid():
        try:
            save_event_profile(
                event=event,
                name=form.cleaned_data["name"],
                description=form.cleaned_data["description"],
            )
        except IntegrityError:
            form.add_error("name", "Un profil porte déjà ce nom. Choisissez un autre nom.")
        except ValidationError as error:
            for message in _form_error_messages(error):
                form.add_error(None, message)
        else:
            messages.success(request, "Le profil réutilisable a été enregistré.")
            return redirect("event_profile_list")
    return render(
        request,
        "volunteers/organizer/profiles/save.html",
        {
            "form": form,
            "event": event,
        },
    )


@organizer_required
def event_profile_create(request, profile_id):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    suggested_name = (
        profile.snapshot.get("event_name", profile.name)
        if isinstance(profile.snapshot, dict) else profile.name
    )
    form = CreateFromProfileForm(
        request.POST or None,
        initial={
            "name": suggested_name,
        },
    )
    if request.method == "POST" and form.is_valid():
        try:
            event = create_event_from_profile(
                profile=profile,
                name=form.cleaned_data["name"],
                event_date=form.cleaned_data["event_date"],
            )
        except ValidationError as error:
            for message in _form_error_messages(error):
                form.add_error(None, message)
        else:
            messages.success(
                request,
                "L'événement a été créé en brouillon. Les échéances ont été décalées "
                "selon la nouvelle date ; vous pouvez les ajuster avant publication.",
            )
            return redirect("event_update", event_id=event.pk)
    return render(
        request,
        "volunteers/organizer/profiles/create.html",
        {
            "form": form,
            "profile": profile,
        },
    )
