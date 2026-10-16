"""Aperçus et historique privés ; l'envoi appartient au traitement automatique."""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST

from .event_notifications import notification_preview, retry_event_notification_failures
from .forms_notifications import RetryNotificationFailuresForm, WhatsAppReminderStatusForm
from .models import Event, EventNotificationCampaign, EventNotificationDelivery
from .notification_transports import get_channel_readiness
from .views_organizer import organizer_required
from .whatsapp_attention import (
    get_event_whatsapp_status,
    mark_whatsapp_shared,
    reset_whatsapp_shared,
)
from .whatsapp_sharing import whatsapp_preview


@organizer_required
@require_http_methods(["GET", "POST"])
@never_cache
def event_notifications(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = RetryNotificationFailuresForm(request.POST if request.method == "POST" else None)
    if request.method == "POST" and form.is_valid():
        try:
            retried_count = retry_event_notification_failures(event, request.user)
        except ValidationError as error:
            form.add_error(None, error)
        else:
            if retried_count:
                messages.success(
                    request,
                    f"{retried_count} notification(s) en échec remise(s) en attente. "
                    "Le traitement automatique les reprendra lorsque les envois seront actifs.",
                )
            else:
                messages.info(request, "Aucun échec à remettre en attente.")
            return redirect("event_notifications", event_id=event.pk)
    campaigns = {campaign.kind: campaign for campaign in EventNotificationCampaign.objects.filter(event=event)}
    now = timezone.now()
    cards = []
    for kind, title, description, enabled in (
        (EventNotificationCampaign.Kind.PUBLICATION, "Annonce du nouvel événement",
         "À la première publication, toutes les familles dont le compte est actif sont concernées.",
         event.notify_publication),
        (EventNotificationCampaign.Kind.VACANCY_REMINDER, "Rappel des postes à pourvoir",
         "48 heures avant la clôture des inscriptions, si des places restent : seules les familles sans aucune inscription active à cet événement sont concernées.",
         event.notify_vacancies),
    ):
        preview = notification_preview(event, kind, now=now)
        campaign = campaigns.get(kind)
        cards.append({
            "kind": kind, "title": title, "description": description,
            "enabled": enabled and event.notify_email,
            "preview": preview, "campaign": campaign,
            "whatsapp": whatsapp_preview(
                event, kind, now=now, vacancies=preview["vacancies"], event_url=preview["event_url"],
            ),
            "whatsapp_status": get_event_whatsapp_status(
                event, now=now, vacancies=preview["vacancies"],
            ).get(kind) if kind == EventNotificationCampaign.Kind.VACANCY_REMINDER else None,
            "scheduled_for": campaign.scheduled_for if campaign else (
                event.registration_closes_at - timedelta(hours=48)
                if kind == EventNotificationCampaign.Kind.VACANCY_REMINDER else None
            ),
            "vacant_post_count": len(preview["vacancy_groups"]),
        })
    history = EventNotificationDelivery.objects.filter(campaign__event=event).select_related(
        "campaign", "family__account",
    ).order_by("-created_at", "-pk")
    channels = [{"label": "Email via Brevo", "enabled": event.notify_email, **get_channel_readiness("email")}]
    return render(request, "notifications/event.html", {
        "event": event, "cards": cards, "channels": channels, "retry_form": form,
        "automation_enabled": bool(getattr(settings, "EVENT_NOTIFICATIONS_ENABLED", False)),
        "failed_count": history.filter(status=EventNotificationDelivery.Status.FAILED, campaign__skipped_reason="").count(),
        "uncertain_count": history.filter(status=EventNotificationDelivery.Status.UNCERTAIN).count(),
        "page_obj": Paginator(history, 20).get_page(request.GET.get("page")),
    })


@organizer_required
@require_POST
@never_cache
def whatsapp_reminder_status(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = WhatsAppReminderStatusForm(request.POST)
    if form.is_valid():
        try:
            operation = mark_whatsapp_shared if form.cleaned_data["action"] == "mark_shared" else reset_whatsapp_shared
            operation(
                event, EventNotificationCampaign.Kind.VACANCY_REMINDER, request.user,
                expected_registration_closes_at=form.cleaned_data["registration_closes_at"],
            )
        except ValidationError as error:
            messages.error(request, " ".join(error.messages))
        else:
            if form.cleaned_data["action"] == "mark_shared":
                messages.success(request, "Partage WhatsApp déclaré. L’alerte est retirée pour toute l’équipe de gestion.")
            else:
                messages.success(request, "Le partage n’est plus déclaré. L’alerte réapparaîtra si le rappel est à envoyer.")
    else:
        messages.error(request, "Action non valide. Rechargez la page avant de modifier le suivi WhatsApp.")
    return redirect(reverse("event_notifications", args=[event.pk]) + "#whatsapp-vacancy_reminder-title")
