"""Pages publiques, composées exclusivement de données publiables."""

import base64
import binascii
from urllib.parse import urlencode

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import ValidationError
from django.db.models import Min, Prefetch, Q
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_safe

from .models import (
    Assignment,
    Event,
    EventDay,
    Family,
    Registration,
    SiteBranding,
    Station,
    StationSlot,
)


@never_cache
@require_safe
def association_logo_image(request):
    """Réutiliser le logo courant sans répéter son contenu dans chaque ticket."""
    logo = SiteBranding.objects.filter(pk=1).values_list("logo_data", flat=True).first() or ""
    metadata, separator, encoded = logo.partition(",")
    allowed = {f"data:{mime};base64": mime for mime in ("image/png", "image/jpeg", "image/webp")}
    if not separator or metadata not in allowed:
        raise Http404("Aucun logo disponible.")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as error:
        raise Http404("Aucun logo disponible.") from error
    if not data or len(data) > 1024 * 1024:
        raise Http404("Aucun logo disponible.")
    return HttpResponse(data, content_type=allowed[metadata])


def _published_events():
    return Event.objects.filter(status=Event.Status.PUBLISHED).prefetch_related("days")


def _event_from_public_id(value):
    try:
        return get_object_or_404(Event, pk=value, status=Event.Status.PUBLISHED)
    except (ValidationError, ValueError, TypeError) as exc:
        raise Http404("Cet événement public n'existe pas.") from exc


def home(request):
    upcoming_events = (
        _published_events()
        .filter(Q(days__date__gte=timezone.localdate()) | Q(days__isnull=True))
        .annotate(first_day=Min("days__date"))
        .order_by("first_day", "name")[:6]
    )
    return render(
        request,
        "volunteers/home.html",
        {"upcoming_events": upcoming_events},
    )


def public_events(request):
    events = _published_events().annotate(first_day=Min("days__date")).order_by("first_day", "name")
    return render(
        request,
        "volunteers/public/event_list.html",
        {"events": events},
    )


@never_cache
def public_planning(request, event_id=None, day_id=None):
    """Affiche uniquement les affectations confirmées, sans objet privé en contexte."""

    events = _published_events().annotate(first_day=Min("days__date")).order_by("first_day", "name")
    event = None
    if event_id is not None:
        event = _event_from_public_id(event_id)
    elif request.GET.get("event"):
        event = _event_from_public_id(request.GET["event"])
    else:
        event = events.filter(days__date__gte=timezone.localdate()).order_by("days__date").first()
        if event is None:
            event = events.first()

    days = EventDay.objects.none()
    selected_day = None
    rows = []
    family_scope_active = request.GET.get("scope") == "family"
    authenticated = request.user.is_authenticated and request.user.is_active
    family_id = (
        Family.objects.filter(account_id=request.user.pk).values_list("pk", flat=True).first()
        if authenticated else None
    )
    if event is not None:
        days = event.days.all().order_by("date")
        requested_day_id = day_id or request.GET.get("day")
        if requested_day_id:
            try:
                selected_day = get_object_or_404(days, pk=requested_day_id)
            except (ValidationError, ValueError, TypeError) as exc:
                raise Http404("Cette journée publique n'existe pas.") from exc
        else:
            selected_day = days.filter(date__gte=timezone.localdate()).first() or days.first()

    # Construire les liens uniquement à partir des sélections publiques validées.
    # Aucun identifiant de famille fourni dans l'URL ne détermine ce filtre.
    selection = {}
    if event is not None:
        selection["event"] = str(event.pk)
    if selected_day is not None:
        selection["day"] = str(selected_day.pk)
    planning_url = reverse("public_planning")
    all_scope_url = f"{planning_url}?{urlencode({**selection, 'scope': 'all'})}"
    family_target = f"{planning_url}?{urlencode({**selection, 'scope': 'family'})}"
    if family_scope_active and not authenticated:
        return redirect_to_login(family_target)
    family_scope_url = family_target if authenticated else redirect_to_login(family_target).url

    if selected_day is not None:
        confirmed = Assignment.objects.filter(
            status=Assignment.Status.CONFIRMED,
            registration__status=Registration.Status.ACTIVE,
        ).select_related("registration__member")
        slots = (
            StationSlot.objects.filter(
                event_day=selected_day,
                station__is_active=True,
            )
            .select_related("station")
            .prefetch_related(
                Prefetch("assignments", queryset=confirmed, to_attr="confirmed_assignments")
            )
            .order_by("period", "station__position", "station__name")
        )
        if family_scope_active:
            # Une même affectation doit satisfaire les trois conditions. Le
            # prefetch reste complet pour préserver l'occupation réelle du poste.
            slots = slots.filter(
                assignments__status=Assignment.Status.CONFIRMED,
                assignments__registration__status=Registration.Status.ACTIVE,
                assignments__registration__member__family_id=family_id,
            ).distinct() if family_id else slots.none()
        for slot in slots:
            names = sorted(
                assignment.registration.member.full_name
                for assignment in slot.confirmed_assignments
            )
            taken = len(names)
            if slot.start_time and slot.end_time:
                schedule = f"{slot.start_time:%H:%M}–{slot.end_time:%H:%M}"
            else:
                schedule = slot.get_period_display()
            # Ne jamais placer les objets Assignment/Registration/Member dans
            # ce contexte : seuls les noms explicitement publiables subsistent.
            rows.append(
                {
                    "slot_id": slot.pk,
                    "station_name": slot.station.name,
                    "station_category": slot.station.get_category_display(),
                    "category": slot.station.category,
                    "category_label": slot.station.get_category_display(),
                    "is_official": slot.station.category == Station.Category.OFFICIAL,
                    "name": slot.station.name,
                    "description": slot.station.description,
                    "period": slot.get_period_display(),
                    "schedule": schedule,
                    "time_label": schedule,
                    "names": names,
                    "people": names,
                    "capacity": slot.capacity,
                    "taken": taken,
                    "remaining": max(slot.capacity - taken, 0),
                    "is_full": taken >= slot.capacity,
                    "fill_percent": min(round(taken * 100 / slot.capacity), 100)
                    if slot.capacity
                    else 100,
                }
            )

    return render(
        request,
        "volunteers/public/planning.html",
        {
            "events": events,
            "event": event,
            "days": days,
            "show_day_filter": days.count() > 1,
            "selected_day": selected_day,
            "rows": rows,
            "planning_scope": "family" if family_scope_active else "all",
            "family_scope_active": family_scope_active,
            "family_filter_available": authenticated and family_id is not None,
            "has_family": family_id is not None,
            "all_scope_url": all_scope_url,
            "family_scope_url": family_scope_url,
        },
    )

