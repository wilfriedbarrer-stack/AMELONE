"""Rappels WhatsApp à préparer et déclarations de partage entièrement manuelles."""

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Exists, OuterRef
from django.urls import reverse
from django.utils import timezone

from .capacity import get_slot_capacities
from .event_notifications import PARIS, VACANCY_REMINDER, _vacancies
from .models import Event, EventDay, EventWhatsAppShare, StationSlot
from .whatsapp_sharing import whatsapp_preview

REMINDER_TITLE = "Rappel des postes à pourvoir"


def _now(value):
    value = value or timezone.now()
    if timezone.is_naive(value):
        raise ValueError("La date doit inclure un fuseau horaire.")
    return value


def _require_organizer(actor):
    if not (
        actor is not None and actor.is_authenticated and actor.is_active
        and (actor.is_staff or actor.is_organizer)
    ):
        raise PermissionDenied("Cette action est réservée aux organisateurs.")


def _require_reminder(kind):
    if kind != VACANCY_REMINDER:
        raise ValidationError("Type de rappel WhatsApp inconnu.")


def _reminder_url(event_id):
    return reverse("event_notifications", args=[event_id]) + "#whatsapp-vacancy_reminder-title"


def get_whatsapp_attention(*, now=None):
    """Liste les rappels dus sans écriture ni chargement de familles.

    Les places sont calculées en lot, y compris leurs quotas partagés entre
    journée et demi-journées. Le nombre de requêtes ne dépend pas des événements.
    """
    now = _now(now)
    today = timezone.localdate(now, PARIS)
    upcoming_days = EventDay.objects.filter(event_id=OuterRef("pk"), date__gte=today)
    shared = EventWhatsAppShare.objects.filter(
        event_id=OuterRef("pk"), kind=VACANCY_REMINDER,
        registration_closes_at=OuterRef("registration_closes_at"),
    )
    candidates = list(Event.objects.filter(
        status=Event.Status.PUBLISHED,
        registration_opens_at__lte=now,
        registration_closes_at__gt=now,
        registration_closes_at__lte=now + timedelta(hours=48),
    ).alias(
        _whatsapp_has_day=Exists(upcoming_days), _whatsapp_shared=Exists(shared),
    ).filter(
        _whatsapp_has_day=True, _whatsapp_shared=False,
    ).order_by("registration_closes_at", "name", "pk"))
    if not candidates:
        return {"count": 0, "events": [], "tasks": []}

    slots = list(StationSlot.objects.filter(
        station__event_id__in=[event.pk for event in candidates],
        station__is_active=True, event_day__date__gte=today,
    ).select_related("station", "event_day"))
    capacities = get_slot_capacities(slots)
    vacant_event_ids = {
        slot.station.event_id for slot in slots if capacities[slot.pk].remaining > 0
    }
    events = []
    tasks = []
    for event in candidates:
        if event.pk not in vacant_event_ids:
            continue
        task = {
            "event_id": event.pk, "event_name": event.name,
            "kind": VACANCY_REMINDER, "title": REMINDER_TITLE,
            "due_at": event.registration_closes_at - timedelta(hours=48),
            "registration_closes_at": event.registration_closes_at,
            "url": _reminder_url(event.pk),
        }
        tasks.append(task)
        events.append({
            "event_id": event.pk, "event_name": event.name, "url": task["url"],
            "task_count": 1, "tasks": [task],
        })
    return {"count": len(tasks), "events": events, "tasks": tasks}


def get_event_whatsapp_status(event, *, now=None, vacancies=None):
    """État déclaratif du rappel courant ; l'aperçu déjà chargé peut être réutilisé."""
    now = _now(now)
    share = EventWhatsAppShare.objects.filter(
        event=event, kind=VACANCY_REMINDER,
    ).select_related("shared_by").first()
    is_shared = share is not None and share.registration_closes_at == event.registration_closes_at
    due_at = event.registration_closes_at - timedelta(hours=48)
    is_due = False
    if not is_shared and due_at <= now < event.registration_closes_at and (
        event.status == Event.Status.PUBLISHED and event.registration_opens_at <= now
        and event.days.filter(date__gte=timezone.localdate(now, PARIS)).exists()
    ):
        is_due = bool(_vacancies(event, now) if vacancies is None else vacancies)
    return {VACANCY_REMINDER: {
        "shared": is_shared,
        "shared_at": share.shared_at if is_shared else None,
        "shared_by": share.shared_by if is_shared else None,
        "due_at": due_at, "is_due": is_due,
    }}


@transaction.atomic
def mark_whatsapp_shared(
    event, kind, actor, *, now=None, expected_registration_closes_at=None,
):
    """Acquitte un rappel uniquement après une déclaration explicite de l'équipe.

    Le site ne sait pas vérifier un envoi WhatsApp. Une répétition du même POST
    conserve la première déclaration ; une nouvelle clôture ouvre un autre cycle.
    """
    _require_organizer(actor)
    _require_reminder(kind)
    now = _now(now)
    current_event = Event.objects.select_for_update(of=("self",)).get(pk=event.pk)
    if expected_registration_closes_at is not None and (
        expected_registration_closes_at != current_event.registration_closes_at
    ):
        raise ValidationError("La clôture des inscriptions a changé. Rechargez la page avant de confirmer.")
    preview = whatsapp_preview(current_event, kind, now=now)
    if not preview["can_share"]:
        raise ValidationError(preview["unavailable_reason"])
    share, created = EventWhatsAppShare.objects.get_or_create(
        event=current_event, kind=kind,
        defaults={
            "shared_at": now, "shared_by": actor,
            "registration_closes_at": current_event.registration_closes_at,
        },
    )
    if created or share.registration_closes_at == current_event.registration_closes_at:
        return share, created
    share.shared_at = now
    share.shared_by = actor
    share.registration_closes_at = current_event.registration_closes_at
    share.save(update_fields=("shared_at", "shared_by", "registration_closes_at", "updated_at"))
    return share, True


@transaction.atomic
def reset_whatsapp_shared(event, kind, actor, *, expected_registration_closes_at=None):
    """Annule une déclaration erronée sans modifier les campagnes automatiques."""
    _require_organizer(actor)
    _require_reminder(kind)
    current_event = Event.objects.select_for_update(of=("self",)).get(pk=event.pk)
    if expected_registration_closes_at is not None and (
        expected_registration_closes_at != current_event.registration_closes_at
    ):
        raise ValidationError("La clôture des inscriptions a changé. Rechargez la page avant de confirmer.")
    deleted, _ = EventWhatsAppShare.objects.filter(event=current_event, kind=kind).delete()
    return bool(deleted)
