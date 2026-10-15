"""Gestion des modèles d'événement dans l'espace organisateur."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from .event_profiles import create_event_from_profile, save_event_profile
from .forms_profiles import (
    CreateFromProfileForm,
    ProfileSlotFormSet,
    ProfileStationDeleteForm,
    ProfileStationForm,
    ReusableProfileForm,
    SaveEventProfileForm,
)
from .models import Event, EventProfile
from .profile_editor import (
    create_reusable_profile,
    delete_profile_station,
    save_profile_station,
    update_reusable_profile,
)
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
            form.add_error("name", "Un modèle porte déjà ce nom. Choisissez un autre nom.")
        except ValidationError as error:
            for message in _form_error_messages(error):
                form.add_error(None, message)
        else:
            messages.success(request, "Le modèle réutilisable a été enregistré.")
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
    if request.method == "GET":
        return redirect(f"{reverse('event_create')}?modele={profile.pk}")
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


def _profile_stations(profile):
    snapshot = profile.snapshot
    if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
        raise ValidationError("Ce modèle ne contient pas une configuration utilisable.")
    stations = snapshot.get("stations", [])
    if not isinstance(stations, list) or any(not isinstance(station, dict) for station in stations):
        raise ValidationError("Les postes enregistrés dans ce modèle sont invalides.")
    return stations


def _profile_days(profile):
    snapshot = profile.snapshot
    days = snapshot.get("days") if isinstance(snapshot, dict) else None
    if not isinstance(days, list) or not days:
        raise ValidationError("Ce modèle ne contient aucune journée utilisable.")
    offsets = []
    for day in days:
        offset = day.get("offset") if isinstance(day, dict) else None
        if type(offset) is not int or offset < 0 or offset in offsets:
            raise ValidationError("Les journées enregistrées dans ce modèle sont invalides.")
        offsets.append(offset)
    if 0 not in offsets:
        raise ValidationError("Le premier jour de l’événement est absent de ce modèle.")
    return sorted(offsets)


def _station_at(profile, station_index):
    try:
        stations = _profile_stations(profile)
    except ValidationError as error:
        raise Http404("Ce poste est introuvable dans le modèle.") from error
    if type(station_index) is not int or not 0 <= station_index < len(stations):
        raise Http404("Ce poste est introuvable dans le modèle.")
    return stations[station_index]


def _station_rows(profile):
    return [
        {"index": index, "name": config.get("name", "Poste sans nom"), "config": config,
         "slots": config.get("slots", []) if isinstance(config.get("slots", []), list) else []}
        for index, config in enumerate(_profile_stations(profile))
    ]


def _profile_form_errors(form, error):
    if hasattr(error, "message_dict"):
        for field, field_messages in error.message_dict.items():
            target = field if field in form.fields and not form.fields[field].widget.is_hidden else None
            for message in field_messages:
                form.add_error(target, message)
    else:
        for message in error.messages:
            form.add_error(None, message)


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_new(request):
    form = ReusableProfileForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            profile = create_reusable_profile(**{
                field: form.cleaned_data[field] for field in
                ("name", "description", "location", "event_description", "general_instructions",
                 "morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled")
            })
        except ValidationError as error:
            _profile_form_errors(form, error)
        except IntegrityError:
            form.add_error("name", "Un modèle porte déjà ce nom. Choisissez un autre nom.")
        else:
            messages.success(request, "Le modèle réutilisable a été créé. Vous pouvez ajouter ses postes.")
            return redirect("event_profile_update", profile_id=profile.pk)
    return render(request, "volunteers/organizer/profiles/edit.html", {
        "form": form, "profile": None, "stations": [], "mode_create": True,
    })


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_update(request, profile_id):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    form = ReusableProfileForm(
        request.POST if request.method == "POST" else None, profile=profile,
    )
    if request.method == "POST" and form.is_valid():
        try:
            update_reusable_profile(
                profile=profile, expected_updated_at=form.cleaned_data["expected_updated_at"],
                **{field: form.cleaned_data[field] for field in
                   ("name", "description", "location", "event_description", "general_instructions",
                    "morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled")},
            )
        except ValidationError as error:
            _profile_form_errors(form, error)
        except IntegrityError:
            form.add_error("name", "Un modèle porte déjà ce nom. Choisissez un autre nom.")
        else:
            messages.success(request, "Le modèle réutilisable a été modifié.")
            return redirect("event_profile_update", profile_id=profile.pk)
    profile_error = ""
    try:
        stations = _station_rows(profile)
    except ValidationError as error:
        stations = []
        profile_error = " ".join(error.messages)
    return render(request, "volunteers/organizer/profiles/edit.html", {
        "form": form, "profile": profile, "stations": stations, "mode_create": False,
        "profile_error": profile_error,
    })


def _profile_station_edit(request, profile, station_index=None):
    station = _station_at(profile, station_index) if station_index is not None else {}
    profile_error = ""
    try:
        day_offsets = _profile_days(profile)
    except ValidationError as error:
        day_offsets = []
        profile_error = " ".join(error.messages)
    initial = {**station, "expected_updated_at": profile.updated_at.isoformat()}
    form = ProfileStationForm(
        request.POST if request.method == "POST" else None, initial=initial,
    )
    initial_slots = station.get("slots", [])
    if not isinstance(initial_slots, list) or any(not isinstance(slot, dict) for slot in initial_slots):
        initial_slots = []
        profile_error = "Les créneaux enregistrés dans ce poste sont invalides."
    formset = ProfileSlotFormSet(
        request.POST if request.method == "POST" else None, initial=initial_slots,
        prefix="slots", form_kwargs={"day_offsets": day_offsets},
    )
    if request.method == "POST":
        valid_form = form.is_valid()
        valid_slots = formset.is_valid()
        if profile_error:
            form.add_error(None, profile_error)
        elif valid_form and valid_slots:
            try:
                save_profile_station(
                    profile=profile, expected_updated_at=form.cleaned_data["expected_updated_at"],
                    station_index=station_index,
                    station_data={field: form.cleaned_data[field] for field in ProfileStationForm.Meta.fields},
                    slots=[
                        {field: slot.cleaned_data[field] for field in
                         ("day_offset", "period", "capacity", "start_time", "end_time")}
                        for slot in formset.forms
                        if slot.cleaned_data and not slot.cleaned_data.get("DELETE")
                    ],
                )
            except ValidationError as error:
                _profile_form_errors(form, error)
            else:
                messages.success(request, "Le poste et ses créneaux ont été enregistrés dans le modèle.")
                return redirect("event_profile_update", profile_id=profile.pk)
    return render(request, "volunteers/organizer/profiles/station_form.html", {
        "form": form, "formset": formset, "profile": profile, "station_index": station_index,
        "profile_error": profile_error,
    })


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_station_create(request, profile_id):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    return _profile_station_edit(request, profile)


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_station_update(request, profile_id, station_index):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    return _profile_station_edit(request, profile, station_index)


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_station_delete(request, profile_id, station_index):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    station = _station_at(profile, station_index)
    form = ProfileStationDeleteForm(
        request.POST if request.method == "POST" else None,
        initial={"expected_updated_at": profile.updated_at.isoformat()},
    )
    if request.method == "POST" and form.is_valid():
        try:
            delete_profile_station(
                profile=profile, expected_updated_at=form.cleaned_data["expected_updated_at"],
                station_index=station_index,
            )
        except ValidationError as error:
            _profile_form_errors(form, error)
        else:
            messages.success(request, "Le poste a été supprimé du modèle réutilisable.")
            return redirect("event_profile_update", profile_id=profile.pk)
    return render(request, "volunteers/organizer/profiles/station_confirm_delete.html", {
        "profile": profile, "station": station, "station_index": station_index, "form": form,
    })
