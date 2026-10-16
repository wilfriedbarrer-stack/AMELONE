"""Read-only Brevo authentication and sender check, without database access.

Only the two fixed GET endpoints below are permitted. Returned diagnostics are
deliberately limited to booleans and fixed reason codes: account information,
sender addresses, credentials, response bodies and exceptions are never exposed.
"""

import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from django.conf import settings

from .notification_transports import (
    NotificationTransportError,
    _brevo_key_present,
    _NoRedirect,
    _sender,
)

_ENDPOINTS = {
    "account": "https://api.brevo.com/v3/account",
    "senders": "https://api.brevo.com/v3/senders",
}
_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 65536


class _CheckFailure(Exception):
    """Carries only an internal, fixed diagnostic code."""


def _http_reason(status):
    if status == 401:
        return "invalid_api_key"
    if status == 403:
        return "access_denied"
    if 300 <= status < 400:
        return "redirect_refused"
    if status == 429 or status >= 500:
        return "provider_unavailable"
    return "unexpected_http_status"


def _get(endpoint):
    try:
        request = Request(
            _ENDPOINTS[endpoint],
            headers={"accept": "application/json", "api-key": settings.BREVO_API_KEY},
            method="GET",
        )
        with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise _CheckFailure(_http_reason(response.status))
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise _CheckFailure("response_too_large")
    except _CheckFailure:
        raise
    except HTTPError as error:
        reason = _http_reason(error.code)
        error.close()
        raise _CheckFailure(reason) from None
    except URLError as error:
        reason = "network_timeout" if isinstance(error.reason, TimeoutError) else "connection_failed"
        raise _CheckFailure(reason) from None
    except TimeoutError:
        raise _CheckFailure("network_timeout") from None
    except Exception:
        raise _CheckFailure("connection_failed") from None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise _CheckFailure("malformed_response") from None
    if not isinstance(payload, dict):
        raise _CheckFailure("malformed_response")
    return payload


def check_brevo_connection():
    """Check credentials and the configured sender, without sending anything.

    The email and global notification switches may remain disabled during this
    preparation check. ``ready`` confirms authentication and sender validation;
    it cannot guarantee eventual delivery, credit availability or account limits.
    Brevo's optional relay flag is diagnostic only, not a readiness requirement.
    """
    result = {
        "key_present": _brevo_key_present(),
        "account_authenticated": False,
        "sender_validated": False,
        "transactional_relay_enabled": None,
        "ready": False,
        "reason": "key_not_configured",
    }
    if not result["key_present"]:
        return result
    if getattr(settings, "EMAIL_PROVIDER", "") != "brevo":
        result["reason"] = "provider_not_brevo"
        return result
    try:
        sender_email = _sender()["email"].casefold()
    except NotificationTransportError:
        result["reason"] = "sender_not_configured"
        return result
    try:
        account = _get("account")
        result["account_authenticated"] = True
        relay = account.get("relay")
        if isinstance(relay, dict) and isinstance(relay.get("enabled"), bool):
            result["transactional_relay_enabled"] = relay["enabled"]
        senders = _get("senders").get("senders")
        if not isinstance(senders, list):
            raise _CheckFailure("malformed_response")
        matched = False
        for sender in senders:
            if not isinstance(sender, dict):
                raise _CheckFailure("malformed_response")
            email = sender.get("email")
            if isinstance(email, str) and email.casefold() == sender_email:
                matched = True
                if sender.get("active") is True:
                    result.update(sender_validated=True, ready=True, reason="ready")
                    return result
        result["reason"] = "sender_not_validated" if matched else "sender_not_found"
    except _CheckFailure as error:
        result["reason"] = str(error)
    return result
