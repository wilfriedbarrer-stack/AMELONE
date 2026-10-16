"""Campagnes d'événements préparées et envoyées uniquement par le traitement dédié.

L'acceptation par un prestataire n'est pas une preuve de livraison. Une tentative
dont le résultat est incertain reste en attente d'une vérification humaine.
"""

from collections import defaultdict
from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.validators import validate_email
from django.db import connection, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone, translation
from django.utils.formats import date_format

from .capacity import get_slot_capacities
from .models import (
    Event,
    EventNotificationCampaign,
    EventNotificationDelivery,
    Family,
    Registration,
    StationSlot,
)
from .notification_transports import (
    NotificationTransportError,
    get_channel_readiness,
    normalize_sms_recipient,
    send_notification_email,
    send_notification_sms,
)

PARIS = ZoneInfo("Europe/Paris")
PUBLICATION = "publication"
VACANCY_REMINDER = "vacancy_reminder"
CHANNELS = ("email", "sms")
SHARED_PLACES_NOTE = (
    "Pour un même poste et une même date, les places à la journée et à la demi-journée "
    "sont partagées : les disponibilités ne s’additionnent pas."
)


def _locked(queryset, *, skip_locked=False):
    if skip_locked and connection.features.has_select_for_update_skip_locked:
        return queryset.select_for_update(of=("self",), skip_locked=True)
    return queryset.select_for_update(of=("self",))


def _vacancies(event, now):
    slots = list(StationSlot.objects.filter(
        station__event=event, station__is_active=True,
        event_day__date__gte=timezone.localdate(now, PARIS),
    ).select_related("station", "event_day").order_by(
        "event_day__date", "station__position", "station__name", "period",
    ))
    capacities = get_slot_capacities(slots)
    periods = defaultdict(set)
    for slot in slots:
        if slot.capacity > 0:
            periods[(slot.station_id, slot.event_day_id)].add(slot.period)
    shared_groups = {
        group for group, choices in periods.items()
        if StationSlot.Period.FULL_DAY in choices and len(choices) > 1
    }
    return [{
        "slot_id": slot.pk, "station_id": slot.station_id,
        "station_name": slot.station.name,
        "date": slot.event_day.date, "period": slot.get_period_display(),
        "period_code": slot.period,
        "start_time": slot.start_time, "end_time": slot.end_time,
        "remaining": capacities[slot.pk].remaining,
        "morning_capacity": capacities[slot.pk].morning_capacity,
        "afternoon_capacity": capacities[slot.pk].afternoon_capacity,
        "is_official": slot.station.category == "official",
        "shared_places": (slot.station_id, slot.event_day_id) in shared_groups,
    } for slot in slots if capacities[slot.pk].remaining > 0]


def group_vacancies(vacancies):
    """Présente les options d'un poste et d'une date sans additionner ses places.

    Les dictionnaires d'origine restent intacts, notamment leurs disponibilités.
    Le nom sert de repli aux aperçus personnalisés sans identifiant de poste.
    """
    groups = {}
    order = {"morning": 0, "Matin": 0, "afternoon": 1, "Après-midi": 1,
             "full_day": 2, "Journée": 2}
    for vacancy in vacancies:
        identity = (
            ("id", vacancy["station_id"]) if vacancy.get("station_id") is not None
            else ("name", vacancy["station_name"])
        )
        key = (*identity, vacancy["date"])
        if key not in groups:
            groups[key] = {
                "station_name": vacancy["station_name"], "date": vacancy["date"],
                "is_official": False, "shared_places": False,
                "capacity_label": "", "options": [],
            }
        group = groups[key]
        group["is_official"] |= bool(vacancy.get("is_official"))
        group["shared_places"] |= bool(vacancy.get("shared_places"))
        group["options"].append(vacancy)
    for group in groups.values():
        group["options"].sort(key=lambda option: order.get(
            option.get("period_code") or option["period"], 99,
        ))
        if group["shared_places"]:
            capacities = next((option for option in group["options"] if (
                option.get("morning_capacity") is not None
                and option.get("afternoon_capacity") is not None
            )), None)
            if capacities is not None:
                morning = capacities["morning_capacity"]
                afternoon = capacities["afternoon_capacity"]
                places = "place" if morning == 1 else "places"
                if morning == afternoon:
                    shared = "partagée" if morning == 1 else "partagées"
                    group["capacity_label"] = f"{morning} {places} {shared}"
                else:
                    group["capacity_label"] = f"{morning} {places} le matin · {afternoon} l’après-midi"
    return list(groups.values())


def _audience(event, kind):
    families = Family.objects.filter(account__is_active=True).select_related("account")
    if kind == VACANCY_REMINDER:
        registered = Registration.objects.filter(
            event_day__event=event, status=Registration.Status.ACTIVE,
        ).values("member__family_id")
        families = families.exclude(pk__in=registered)
    return families.order_by("pk")


def _french_date(value):
    with translation.override("fr"):
        if hasattr(value, "hour"):
            return date_format(timezone.localtime(value, PARIS), "l j F Y à H:i")
        return date_format(value, "l j F Y")


def notification_preview(event, kind, *, now=None):
    """Aperçu sans écriture, sans appel au prestataire ni changement de campagne."""
    if kind not in (PUBLICATION, VACANCY_REMINDER):
        raise ValueError("Type de notification inconnu.")
    now = now or timezone.now()
    vacancies = _vacancies(event, now)
    vacancy_groups = group_vacancies(vacancies)
    has_shared_places = any(item["shared_places"] for item in vacancies)
    event_url = settings.SITE_URL.rstrip("/") + reverse("family_event_detail", args=[event.pk])
    dates = list(event.days.order_by("date").values_list("date", flat=True))
    date_text = ", ".join(_french_date(day) for day in dates)
    closing = _french_date(event.registration_closes_at)
    if kind == PUBLICATION:
        subject = f"AMELONE — {event.name} est en ligne"[:255]
        opening = (
            f"Les inscriptions ouvriront le {_french_date(event.registration_opens_at)}."
            if now < event.registration_opens_at else "Vous pouvez dès maintenant proposer votre présence."
        )
        introduction = f"Un nouvel événement vous attend : {event.name}.\n{opening}"
        sms_intro = (
            "Les inscriptions sont ouvertes"
            if event.registration_opens_at <= now < event.registration_closes_at
            else "Nouvel événement"
        )
    else:
        subject = f"AMELONE — encore des places pour {event.name}"[:255]
        introduction = (
            f"Il reste des postes à pourvoir pour {event.name}. "
            "Si vous êtes disponible, votre aide sera la bienvenue !"
        )
        sms_intro = "Un coup de main ?"
    lines = ["Bonjour,", "", introduction]
    if date_text:
        lines.extend(["", f"Date : {date_text}."])
    if event.location:
        lines.append(f"Lieu : {event.location}.")
    lines.append(f"Clôture des inscriptions : {closing} (heure de Paris).")
    if kind == VACANCY_REMINDER:
        lines.extend(["", "Les places disponibles au moment de cet envoi :"])
        if has_shared_places:
            lines.append(SHARED_PLACES_NOTE)
        for group in vacancy_groups:
            capacity_label = f" · {group['capacity_label']}" if group["capacity_label"] else ""
            lines.append(f"- {_french_date(group['date'])} : {group['station_name']}{capacity_label}")
            for vacancy in group["options"]:
                hours = (
                    f" · {vacancy['start_time']:%H:%M}–{vacancy['end_time']:%H:%M}"
                    if vacancy["start_time"] and vacancy["end_time"] else ""
                )
                full_day = (vacancy.get("period_code") or vacancy["period"]) in ("full_day", "Journée")
                if group["shared_places"] and full_day:
                    available = "possible" if vacancy["remaining"] == 1 else "possibles"
                    available += " dans ces mêmes places"
                else:
                    available = "libre" if vacancy["remaining"] == 1 else "libres"
                lines.append(f"  {vacancy['period']}{hours} : {vacancy['remaining']} {available}.")
        if any(item["is_official"] for item in vacancies):
            lines.append("Postes officiels : habilitation, licence et formation à jour nécessaires.")
    lines.extend(["", f"Consulter l'événement et s'inscrire : {event_url}", "", "Merci et à bientôt,", "L’équipe AMELONE Loisirs"])
    short_name = event.name if len(event.name) <= 70 else event.name[:67] + "..."
    sms_text = f"AMELONE : {sms_intro} - {short_name}. {event_url}"
    return {
        "subject": subject, "body_text": "\n".join(lines), "sms_text": sms_text,
        "vacancies": vacancies, "vacancy_groups": vacancy_groups,
        "has_shared_places": has_shared_places,
        "shared_places_note": SHARED_PLACES_NOTE,
        "recipient_count": _audience(event, kind).count(),
        "event_url": event_url, "scheduled_for": (
            event.registration_closes_at - timedelta(hours=48) if kind == VACANCY_REMINDER else now
        ),
    }


def record_event_publication(event):
    """Mémorise la première publication ; aucun message n'est envoyé ici."""
    if event.status != Event.Status.PUBLISHED:
        return None
    now = timezone.now()
    disabled = not event.notify_publication
    campaign, _ = EventNotificationCampaign.objects.get_or_create(
        event=event, kind=PUBLICATION,
        defaults={
            "scheduled_for": now, "completed_at": now if disabled else None,
            "skipped_reason": "Annonce désactivée lors de la première publication." if disabled else "",
        },
    )
    return campaign


def _campaign_guard(event, kind, now):
    """Renvoie (attendre, motif d'abandon) après lecture de l'état courant."""
    if event.status != Event.Status.PUBLISHED:
        return False, "Événement non publié ou archivé."
    flag = event.notify_publication if kind == PUBLICATION else event.notify_vacancies
    if not flag:
        return False, "Cette notification a été désactivée pour cet événement."
    if event.registration_closes_at <= now:
        return False, "Les inscriptions sont clôturées."
    if not event.days.filter(date__gte=timezone.localdate(now, PARIS)).exists():
        return False, "Cet événement n’a plus de journée à venir."
    if kind == VACANCY_REMINDER:
        if now < event.registration_closes_at - timedelta(hours=48):
            return True, "La relance n’est pas encore due."
        if not _vacancies(event, now):
            return False, "Tous les postes sont pourvus."
    return False, ""


def _skip_pending(campaign, reason, now):
    EventNotificationDelivery.objects.filter(campaign=campaign, status="pending").update(
        status="skipped", failure_reason=reason, updated_at=now,
    )
    campaign.completed_at = now
    campaign.skipped_reason = reason
    campaign.save(update_fields=("completed_at", "skipped_reason", "updated_at"))


def _recipient(family, channel):
    if channel == "email":
        value = family.account.email.strip()
        validate_email(value)
        return value
    return normalize_sms_recipient(family.account.phone)


def _prepare_campaign(campaign_id, now):
    with transaction.atomic(durable=True):
        campaign = _locked(EventNotificationCampaign.objects.filter(pk=campaign_id), skip_locked=True).first()
        if campaign is None or campaign.completed_at or campaign.prepared_at:
            return False
        now = now() if callable(now) else now
        event = Event.objects.get(pk=campaign.event_id)
        wait, reason = _campaign_guard(event, campaign.kind, now)
        if wait:
            return False
        if reason:
            _skip_pending(campaign, reason, now)
            return False
        preview = notification_preview(event, campaign.kind, now=now)
        deliveries = []
        for family in _audience(event, campaign.kind):
            for channel in CHANNELS:
                failure = ""
                recipient = family.account.email if channel == "email" else family.account.phone
                if not getattr(event, f"notify_{channel}"):
                    failure = "Canal désactivé pour cet événement."
                else:
                    try:
                        recipient = _recipient(family, channel)
                    except (ValidationError, NotificationTransportError, ValueError):
                        failure = "Adresse e-mail invalide." if channel == "email" else "Numéro mobile manquant ou invalide."
                deliveries.append(EventNotificationDelivery(
                    campaign=campaign, family=family, channel=channel, recipient=recipient[:255],
                    subject=preview["subject"] if channel == "email" else "",
                    body_text=preview["body_text"] if channel == "email" else preview["sms_text"],
                    status="skipped" if failure else "pending", failure_reason=failure,
                ))
        EventNotificationDelivery.objects.bulk_create(deliveries)
        campaign.prepared_at = now
        campaign.save(update_fields=("prepared_at", "updated_at"))
        return True


def _claim_delivery(delivery_id, now):
    # Cette transaction est validée avant tout appel réseau. Deux traitements
    # ne peuvent pas revendiquer la même notification.
    with transaction.atomic(durable=True):
        delivery = _locked(EventNotificationDelivery.objects.filter(pk=delivery_id), skip_locked=True).first()
        if delivery is None or delivery.status != "pending":
            return None
        now = now() if callable(now) else now
        campaign = EventNotificationCampaign.objects.get(pk=delivery.campaign_id)
        if campaign.completed_at:
            return None
        event = Event.objects.get(pk=campaign.event_id)
        wait, reason = _campaign_guard(event, campaign.kind, now)
        if wait:
            return None
        family = Family.objects.select_related("account").get(pk=delivery.family_id)
        if not reason and not family.account.is_active:
            reason = "Le compte de cette famille est inactif."
        if not reason and campaign.kind == VACANCY_REMINDER and Registration.objects.filter(
            member__family=family, event_day__event=event, status=Registration.Status.ACTIVE,
        ).exists():
            reason = "Cette famille est désormais inscrite à l’événement."
        if not reason and not getattr(event, f"notify_{delivery.channel}"):
            reason = "Canal désactivé pour cet événement."
        if not reason:
            try:
                if _recipient(family, delivery.channel) != delivery.recipient:
                    reason = "Les coordonnées ont changé depuis la préparation."
            except (ValidationError, NotificationTransportError, ValueError):
                reason = "Les coordonnées ne sont plus valides."
        if reason:
            delivery.status = "skipped"
            delivery.failure_reason = reason
            delivery.save(update_fields=("status", "failure_reason", "updated_at"))
            return "skipped"
        if not get_channel_readiness(delivery.channel)["ready"]:
            return "waiting"
        preview = notification_preview(event, campaign.kind, now=now)
        delivery.subject = preview["subject"] if delivery.channel == "email" else ""
        delivery.body_text = preview["body_text"] if delivery.channel == "email" else preview["sms_text"]
        delivery.status = "sending"
        delivery.last_attempt_at = now
        delivery.attempt_count += 1
        delivery.failure_reason = ""
        delivery.save(update_fields=("subject", "body_text", "status", "last_attempt_at", "attempt_count", "failure_reason", "updated_at"))
        return delivery


def _send_claimed(delivery, now):
    key = f"amelone-event-notification-{delivery.pk}"
    try:
        if delivery.channel == "email":
            provider_id = send_notification_email(
                to=delivery.recipient, subject=delivery.subject,
                body=delivery.body_text, idempotency_key=key,
            )
        else:
            provider_id = send_notification_sms(
                to=delivery.recipient, body=delivery.body_text, idempotency_key=key,
            )
    except NotificationTransportError as error:
        state = "uncertain" if error.uncertain else "failed"
        finished_at = now() if callable(now) else now
        # Les exceptions des transports ne doivent jamais contenir de secrets.
        EventNotificationDelivery.objects.filter(pk=delivery.pk, status="sending").update(
            status=state, failure_reason=str(error)[:2000], updated_at=finished_at,
        )
        return state
    except Exception:
        finished_at = now() if callable(now) else now
        EventNotificationDelivery.objects.filter(pk=delivery.pk, status="sending").update(
            status="uncertain", failure_reason="Résultat du prestataire inconnu : vérification manuelle nécessaire.",
            updated_at=finished_at,
        )
        return "uncertain"
    finished_at = now() if callable(now) else now
    EventNotificationDelivery.objects.filter(pk=delivery.pk, status__in=("sending", "uncertain")).update(
        status="accepted", provider_message_id=str(provider_id or "")[:255],
        accepted_at=finished_at, failure_reason="", updated_at=finished_at,
    )
    return "accepted"


def _finish_campaign(campaign_id, now):
    with transaction.atomic():
        campaign = _locked(EventNotificationCampaign.objects.filter(pk=campaign_id)).first()
        if not campaign or campaign.completed_at or not campaign.prepared_at:
            return
        if not EventNotificationDelivery.objects.filter(
            campaign=campaign, status__in=("pending", "sending", "failed", "uncertain"),
        ).exists():
            campaign.completed_at = now
            campaign.save(update_fields=("completed_at", "updated_at"))


def retry_event_notification_failures(event, actor):
    """Remet uniquement les échecs certains en attente, sur action explicite."""
    if not actor or not actor.is_active or not (
        actor.is_superuser or actor.is_staff or actor.is_organizer
    ):
        raise PermissionDenied("Ce suivi est réservé aux organisateurs.")
    with transaction.atomic():
        campaign_ids = list(EventNotificationCampaign.objects.filter(event=event).values_list("pk", flat=True))
        # Même ordre que la finalisation : campagne, puis notifications.
        list(_locked(EventNotificationCampaign.objects.filter(pk__in=campaign_ids)).order_by("pk"))
        count = EventNotificationDelivery.objects.filter(
            campaign_id__in=campaign_ids, campaign__skipped_reason="", status="failed",
        ).update(status="pending", failure_reason="", updated_at=timezone.now())
        if count:
            EventNotificationCampaign.objects.filter(
                pk__in=campaign_ids, skipped_reason="",
            ).update(completed_at=None, updated_at=timezone.now())
        return count


def _reminder_events(now):
    return Event.objects.filter(
        status=Event.Status.PUBLISHED, notify_vacancies=True,
        registration_closes_at__gt=now,
        registration_closes_at__lte=now + timedelta(hours=48),
        days__date__gte=timezone.localdate(now, PARIS),
    ).distinct().order_by("registration_closes_at", "pk")


def process_event_notifications(now=None, dry_run=False, limit=100):
    """Traite une file finie ; jamais appelé par un GET ou par Event.save."""
    clock = (lambda: now) if now is not None else timezone.now
    now = clock()
    if timezone.is_naive(now):
        raise ValueError("La date du traitement doit inclure un fuseau horaire.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
        raise ValueError("La limite doit être comprise entre 1 et 10000.")
    stats = dict.fromkeys(("campaigns_prepared", "accepted", "failed", "uncertain", "skipped", "waiting", "attempted"), 0)
    stats.update(enabled=bool(getattr(settings, "EVENT_NOTIFICATIONS_ENABLED", False)), dry_run=dry_run)
    if dry_run:
        stats["campaigns_due"] = EventNotificationCampaign.objects.filter(
            completed_at__isnull=True, scheduled_for__lte=now,
        ).count()
        stats["reminder_events_due"] = _reminder_events(now).count()
        return stats
    if not stats["enabled"]:
        return stats
    # Couvre une publication effectuée par l'ancienne instance pendant un
    # déploiement. La migration a déjà marqué les anciennes publications.
    for event in Event.objects.filter(status=Event.Status.PUBLISHED).exclude(
        pk__in=EventNotificationCampaign.objects.filter(kind=PUBLICATION).values("event_id"),
    ):
        record_event_publication(event)
    # Le changement de statut initial reste enregistré même quand les envois
    # sont désactivés ; il n'y a pas de rattrapage des événements historiques.
    for event in _reminder_events(now):
        EventNotificationCampaign.objects.get_or_create(
            event=event, kind=VACANCY_REMINDER,
            defaults={"scheduled_for": event.registration_closes_at - timedelta(hours=48)},
        )
    grace = max(60, int(getattr(settings, "EVENT_NOTIFICATIONS_SENDING_GRACE_SECONDS", 900)))
    stale = now - timedelta(seconds=grace)
    stats["uncertain"] = EventNotificationDelivery.objects.filter(status="sending").filter(
        Q(last_attempt_at__lte=stale) | Q(last_attempt_at__isnull=True, created_at__lte=stale),
    ).update(status="uncertain", failure_reason="Traitement interrompu : résultat inconnu, vérifier avant toute action.", updated_at=now)
    campaign_ids = list(EventNotificationCampaign.objects.filter(
        completed_at__isnull=True, scheduled_for__lte=now,
    ).order_by("scheduled_for", "pk").values_list("pk", flat=True))
    for campaign_id in campaign_ids:
        stats["campaigns_prepared"] += int(_prepare_campaign(campaign_id, clock))
        pending_ids = EventNotificationDelivery.objects.filter(
            campaign_id=campaign_id, status="pending",
        ).order_by("created_at", "pk").values_list("pk", flat=True)
        for delivery_id in pending_ids.iterator():
            if stats["attempted"] >= limit:
                break
            claimed = _claim_delivery(delivery_id, clock)
            if claimed is None:
                continue
            if isinstance(claimed, str):
                stats[claimed] += 1
                continue
            stats["attempted"] += 1
            stats[_send_claimed(claimed, clock)] += 1
        _finish_campaign(campaign_id, clock())
        if stats["attempted"] >= limit:
            break
    return stats
