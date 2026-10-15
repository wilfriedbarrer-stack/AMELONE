"""Point d'entrée du planificateur gratuit GitHub, sans session administrateur."""

import json

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .event_notifications import process_event_notifications
from .notification_job_auth import (
    NotificationJobUnauthorized,
    NotificationJobUnavailable,
    verify_notification_job_token,
)


@never_cache
@csrf_exempt
@require_POST
def process_notifications_job(request):
    authorization = request.headers.get("Authorization", "")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme != "Bearer" or not token or " " in token:
        return JsonResponse({"error": "Authentification refusée."}, status=401)
    try:
        verify_notification_job_token(token)
    except NotificationJobUnauthorized:
        return JsonResponse({"error": "Authentification refusée."}, status=401)
    except NotificationJobUnavailable:
        return JsonResponse({"error": "Authentification temporairement indisponible."}, status=503)
    # Les limites viennent du serveur : aucun paramètre de l'appelant ne peut
    # agrandir une exécution ou provoquer une reprise des résultats incertains.
    if request.GET:
        return JsonResponse({"error": "Aucun paramètre n’est accepté."}, status=400)
    try:
        if request.body and json.loads(request.body) != {}:
            return JsonResponse({"error": "Corps de requête invalide."}, status=400)
    except (ValueError, UnicodeError):
        return JsonResponse({"error": "Corps de requête invalide."}, status=400)
    try:
        result = process_event_notifications(limit=1)
    except Exception:
        # Ne pas exposer de trace, de coordonnées ou de jeton au planificateur.
        return JsonResponse({"error": "Traitement temporairement indisponible."}, status=503)
    allowed = {
        "enabled", "dry_run", "campaigns_prepared", "accepted", "failed",
        "uncertain", "skipped", "waiting", "attempted",
    }
    return JsonResponse({
        key: value for key, value in result.items()
        if key in allowed and (
            isinstance(value, bool) if key in ("enabled", "dry_run")
            else isinstance(value, int) and not isinstance(value, bool) and value >= 0
        )
    })
