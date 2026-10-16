"""Messages de groupe prêts à partager, sans envoi ni suivi automatique."""

from urllib.parse import quote

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from .event_notifications import (
    PARIS,
    PUBLICATION,
    SHARED_PLACES_NOTE,
    VACANCY_REMINDER,
    _french_date,
    _vacancies,
)
from .models import Event

MAX_SHARED_SLOTS = 8
MAX_SHARED_DATES = 4


def _single_line(value, limit):
    value = " ".join(str(value).split())
    return value if len(value) <= limit else value[:limit - 1].rstrip() + "…"


def whatsapp_preview(event, kind, *, now=None, vacancies=None, event_url=None):
    """Prépare un partage WhatsApp générique ; ne choisit aucun destinataire.

    Les réglages des e-mails et de l'automatisation n'interviennent pas dans un
    partage manuel. Les places affichées sont une photographie de la page.
    """
    if kind not in (PUBLICATION, VACANCY_REMINDER):
        raise ValueError("Type de message WhatsApp inconnu.")
    now = now or timezone.now()
    dates = list(event.days.order_by("date").values_list("date", flat=True))
    if vacancies is None:
        vacancies = _vacancies(event, now) if kind == VACANCY_REMINDER else []
    event_url = event_url or settings.SITE_URL.rstrip("/") + reverse("family_event_detail", args=[event.pk])

    unavailable_reason = ""
    if event.status != Event.Status.PUBLISHED:
        unavailable_reason = "Publiez l’événement pour partager son lien dans le groupe."
    elif event.registration_closes_at <= now:
        unavailable_reason = "Les inscriptions sont clôturées."
    elif not any(day >= timezone.localdate(now, PARIS) for day in dates):
        unavailable_reason = "Cet événement n’a plus de journée à venir."
    elif kind == VACANCY_REMINDER and now < event.registration_opens_at:
        unavailable_reason = "Le rappel sera disponible à l’ouverture des inscriptions."
    elif kind == VACANCY_REMINDER and not vacancies:
        unavailable_reason = "Tous les postes sont pourvus : aucun rappel à partager."

    name = _single_line(event.name, 200)
    lines = ["Bonjour à toute l’équipe des bénévoles 👋", ""]
    if kind == PUBLICATION:
        lines.append(f"Un nouvel événement AMELONE est en ligne : {name} !")
    else:
        lines.append(f"Un petit coup de main pour {name} ? 🙌")
        lines.append(
            "Si vous n’êtes pas encore inscrit et que vous êtes disponible, "
            "il reste des postes à pourvoir. Même une demi-journée compte !"
        )

    if dates:
        date_text = ", ".join(_french_date(day) for day in dates[:MAX_SHARED_DATES])
        if len(dates) > MAX_SHARED_DATES:
            date_text += f" et {len(dates) - MAX_SHARED_DATES} autre(s) journée(s)"
        lines.extend(["", f"Date : {date_text}."])
    if event.location:
        lines.append(f"Lieu : {_single_line(event.location, 255)}.")
    if kind == PUBLICATION:
        lines.extend(["", (
            f"Les inscriptions ouvriront le {_french_date(event.registration_opens_at)} (heure de Paris)."
            if now < event.registration_opens_at
            else "Les inscriptions sont ouvertes : venez proposer votre présence !"
        )])
    lines.append(f"Clôture des inscriptions : {_french_date(event.registration_closes_at)} (heure de Paris).")

    if kind == VACANCY_REMINDER and vacancies:
        lines.extend(["", "Places disponibles au moment de préparer ce message :"])
        if any(item.get("shared_places") for item in vacancies[:MAX_SHARED_SLOTS]):
            lines.append(SHARED_PLACES_NOTE)
        for vacancy in vacancies[:MAX_SHARED_SLOTS]:
            hours = (
                f" · {vacancy['start_time']:%H:%M}–{vacancy['end_time']:%H:%M}"
                if vacancy["start_time"] and vacancy["end_time"] else ""
            )
            category = " · Officiel" if vacancy["is_official"] else ""
            lines.append(
                f"• {vacancy['date']:%d/%m} · {_single_line(vacancy['station_name'], 70)} · "
                f"{vacancy['period']}{hours}{category} : {vacancy['remaining']} place(s)"
            )
        if len(vacancies) > MAX_SHARED_SLOTS:
            lines.append(f"… et {len(vacancies) - MAX_SHARED_SLOTS} autre(s) créneau(x), à consulter sur le site.")
        if any(item["is_official"] for item in vacancies):
            lines.append("Postes officiels : habilitation, licence et formation à jour nécessaires.")
    lines.extend([
        "", f"Voir l’événement et s’inscrire : {event_url}",
        "", "Merci pour votre aide et votre bonne humeur 💚", "L’équipe AMELONE Loisirs",
    ])
    body_text = "\n".join(lines)
    return {
        "body_text": body_text,
        "share_url": "https://wa.me/?text=" + quote(body_text, safe="") if not unavailable_reason else "",
        "can_share": not unavailable_reason,
        "unavailable_reason": unavailable_reason,
    }
