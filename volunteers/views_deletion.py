"""Suppression confirmée des configurations et conservation de l’historique."""

from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .deletion import (
    delete_event_or_archive,
    delete_reusable_event_profile,
    get_event_deletion_summary,
)
from .forms_deletion import DeletionConfirmationForm
from .models import Event, EventProfile
from .views_organizer import organizer_required


def _report_deletion_error(form, error):
    for message in error.messages:
        form.add_error(None, message)


@organizer_required
@require_http_methods(["GET", "POST"])
def event_delete(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    try:
        summary = get_event_deletion_summary(event=event)
    except Event.DoesNotExist as error:
        raise Http404("Cet événement a déjà été supprimé.") from error
    archive = summary["mode"] == "archive"
    form = DeletionConfirmationForm(
        request.POST if request.method == "POST" else None,
        expected_updated_at=event.updated_at.isoformat(),
        confirm_label=(
            "Je confirme le retrait de cet événement de la liste active, en conservant son historique."
            if archive else "Je confirme la suppression définitive de cet événement et de sa configuration."
        ),
    )
    if request.method == "POST" and form.is_valid():
        try:
            result = delete_event_or_archive(
                event=event, actor=request.user,
                expected_updated_at=form.cleaned_data["expected_updated_at"],
            )
        except ValidationError as error:
            _report_deletion_error(form, error)
        except Event.DoesNotExist as error:
            raise Http404("Cet événement a déjà été supprimé.") from error
        else:
            if result["mode"] == "archive":
                messages.success(
                    request,
                    f"L’événement « {result['name']} » a été retiré de la liste active. "
                    "Son historique est conservé.",
                )
            else:
                messages.success(request, f"L’événement « {result['name']} » a été supprimé.")
            return redirect("event_list")
    return render(request, "volunteers/organizer/event_confirm_delete.html", {
        "event": event, "form": form, "summary": summary,
    })


@organizer_required
@require_http_methods(["GET", "POST"])
def event_profile_delete(request, profile_id):
    profile = get_object_or_404(EventProfile, pk=profile_id)
    form = DeletionConfirmationForm(
        request.POST if request.method == "POST" else None,
        expected_updated_at=profile.updated_at.isoformat(),
        confirm_label="Je confirme la suppression définitive de ce modèle réutilisable.",
    )
    if request.method == "POST" and form.is_valid():
        try:
            result = delete_reusable_event_profile(
                profile=profile, actor=request.user,
                expected_updated_at=form.cleaned_data["expected_updated_at"],
            )
        except ValidationError as error:
            _report_deletion_error(form, error)
        except EventProfile.DoesNotExist as error:
            raise Http404("Ce modèle a déjà été supprimé.") from error
        else:
            messages.success(request, f"Le modèle réutilisable « {result['name']} » a été supprimé.")
            return redirect("event_profile_list")
    snapshot = profile.snapshot if isinstance(profile.snapshot, dict) else {}
    stations = snapshot.get("stations", [])
    stations = stations if isinstance(stations, list) else []
    slot_count = sum(
        len(station["slots"]) for station in stations
        if isinstance(station, dict) and isinstance(station.get("slots"), list)
    )
    return render(request, "volunteers/organizer/profiles/confirm_delete.html", {
        "profile": profile, "form": form,
        "station_count": len(stations), "slot_count": slot_count,
    })
