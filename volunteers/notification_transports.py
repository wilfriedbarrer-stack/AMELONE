"""Transports de notification, sans simulation de réussite ni relance implicite.

La clé fournie sert à la corrélation. La déduplication durable appartient au
registre de notifications ; un résultat ambigu ne doit pas être relancé sans
vérification auprès du prestataire.
"""

import base64
import hashlib
import json
import re
import smtplib
import socket
import ssl
import uuid
from email.mime.base import MIMEBase
from email.utils import parseaddr
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import BadHeaderError, EmailMessage, get_connection
from django.core.mail.backends.base import BaseEmailBackend
from django.core.validators import validate_email

_TIMEOUT_SECONDS = 10
_MAX_RESPONSE_BYTES = 65536
_SMTP_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
_BREVO_API = "https://api.brevo.com/v3/"


class NotificationTransportError(Exception):
    """Erreur sûre à afficher : ni secrets, ni numéro, ni réponse brute."""

    def __init__(self, message, *, uncertain=False):
        self.uncertain = bool(uncertain)
        if self.uncertain:
            message = f"{message} Vérifiez auprès du prestataire avant toute relance."
        super().__init__(message)


def normalize_sms_phone(value):
    """Normalise les mobiles français et les numéros internationaux E.164."""
    if not isinstance(value, str):
        raise NotificationTransportError("Un numéro mobile valide est nécessaire pour le SMS.")
    number = re.sub(r"[\s().-]", "", value.strip())
    if re.fullmatch(r"0[67][0-9]{8}", number):
        number = "+33" + number[1:]
    elif number.startswith("00"):
        number = "+" + number[2:]
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", number):
        raise NotificationTransportError("Un numéro mobile valide est nécessaire pour le SMS.")
    if number.startswith("+33") and not re.fullmatch(r"\+33[67][0-9]{8}", number):
        raise NotificationTransportError("Un numéro mobile français est nécessaire pour le SMS.")
    return number


normalize_sms_recipient = normalize_sms_phone


def _email_address(value):
    if not isinstance(value, str) or any(character in value for character in "\r\n\x00"):
        raise NotificationTransportError("L’adresse e-mail du destinataire est invalide.")
    address = value.strip()
    try:
        validate_email(address)
    except ValidationError:
        raise NotificationTransportError("L’adresse e-mail du destinataire est invalide.") from None
    return address


def _sender(value=None):
    value = getattr(settings, "DEFAULT_FROM_EMAIL", "") if value is None else value
    if not isinstance(value, str) or any(character in value for character in "\r\n\x00"):
        raise NotificationTransportError("L’adresse e-mail de l’association doit être configurée.")
    name, email = parseaddr(value)
    try:
        validate_email(email)
    except ValidationError:
        raise NotificationTransportError("L’adresse e-mail de l’association doit être configurée.") from None
    return {"name": name or "AMELONE", "email": email}


def _brevo_key_present():
    key = getattr(settings, "BREVO_API_KEY", "")
    return isinstance(key, str) and bool(key.strip()) and not any(character in key for character in "\r\n\x00")


def get_channel_readiness(channel):
    """Vérifie la configuration locale, indépendamment du commutateur global.

    Ne contacte jamais le prestataire et ne prétend pas vérifier ses crédits ou
    l'acceptation des identifiants.
    """
    if channel == "email":
        if not getattr(settings, "EMAIL_ENABLED", False):
            return {"ready": False, "reason": "L’envoi d’e-mails est désactivé."}
        try:
            _sender()
        except NotificationTransportError as error:
            return {"ready": False, "reason": str(error)}
        provider = getattr(settings, "EMAIL_PROVIDER", "django")
        if provider == "brevo":
            if not _brevo_key_present():
                return {"ready": False, "reason": "La clé API Brevo pour les e-mails n’est pas configurée."}
            return {"ready": True, "reason": "L’envoi d’e-mails via Brevo est configuré."}
        if provider != "django":
            return {"ready": False, "reason": "Le prestataire d’e-mails n’est pas pris en charge."}
        if getattr(settings, "EMAIL_BACKEND", "") != _SMTP_BACKEND:
            return {"ready": False, "reason": "Un serveur SMTP réel doit être configuré pour les e-mails."}
        host = getattr(settings, "EMAIL_HOST", "")
        if not isinstance(host, str) or not host.strip() or any(character in host for character in "\r\n\x00"):
            return {"ready": False, "reason": "Le serveur SMTP de l’association n’est pas configuré."}
        if getattr(settings, "EMAIL_USE_TLS", False) and getattr(settings, "EMAIL_USE_SSL", False):
            return {"ready": False, "reason": "Choisissez une seule méthode de sécurisation SMTP."}
        return {"ready": True, "reason": "L’envoi d’e-mails via SMTP est configuré."}
    if channel == "sms":
        if not getattr(settings, "SMS_ENABLED", False):
            return {"ready": False, "reason": "L’envoi de SMS est désactivé."}
        if getattr(settings, "SMS_PROVIDER", "") != "brevo":
            return {"ready": False, "reason": "Le prestataire de SMS n’est pas configuré ou pris en charge."}
        if not _brevo_key_present():
            return {"ready": False, "reason": "La clé API Brevo pour les SMS n’est pas configurée."}
        sender = getattr(settings, "SMS_SENDER", "")
        if not isinstance(sender, str) or not re.fullmatch(r"[A-Za-z0-9]{3,11}", sender) or not re.search(r"[A-Za-z]", sender):
            return {"ready": False, "reason": "Configurez un nom d’expéditeur SMS de 3 à 11 lettres ou chiffres."}
        return {"ready": True, "reason": "L’envoi de SMS via Brevo est configuré."}
    return {"ready": False, "reason": "Ce canal de notification n’est pas pris en charge."}


def _require_ready(channel):
    readiness = get_channel_readiness(channel)
    if not readiness["ready"]:
        raise NotificationTransportError(readiness["reason"])


def _correlation_key(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024 or any(character in value for character in "\r\n\x00"):
        raise NotificationTransportError("La référence de notification est invalide.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _body(value):
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise NotificationTransportError("Le contenu de la notification est vide ou invalide.")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Never forward the API key to a redirected host.
        return None


def _network_result_uncertain(error):
    return not isinstance(error, (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError))


def _brevo_request(path, payload, correlation_key):
    request = Request(
        _BREVO_API + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "accept": "application/json", "content-type": "application/json",
            "api-key": settings.BREVO_API_KEY,
            "X-AMELONE-Idempotency-Key": correlation_key,
        }, method="POST",
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=_TIMEOUT_SECONDS) as response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            if len(raw) > _MAX_RESPONSE_BYTES:
                raise NotificationTransportError("La réponse du prestataire ne permet pas de confirmer l’envoi.", uncertain=True)
    except NotificationTransportError:
        raise
    except HTTPError as error:
        status = error.code
        error.close()
        raise NotificationTransportError(
            f"Le prestataire a refusé l’envoi de la notification (HTTP {status}).",
            uncertain=status >= 500 or status in (408, 409),
        ) from None
    except URLError as error:
        raise NotificationTransportError(
            "La connexion au prestataire n’a pas permis de confirmer l’envoi.",
            uncertain=_network_result_uncertain(error.reason),
        ) from None
    except (TimeoutError, OSError) as error:
        raise NotificationTransportError(
            "La connexion au prestataire n’a pas permis de confirmer l’envoi.",
            uncertain=_network_result_uncertain(error),
        ) from None
    except Exception:
        raise NotificationTransportError("L’envoi au prestataire n’a pas pu être confirmé.", uncertain=True) from None
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise NotificationTransportError("La réponse du prestataire ne permet pas de confirmer l’envoi.", uncertain=True) from None
    if not isinstance(result, dict):
        raise NotificationTransportError("La réponse du prestataire ne permet pas de confirmer l’envoi.", uncertain=True)
    return result


def _provider_identifier(value):
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return f"brevo:{value}"
    if isinstance(value, str) and value.strip() and len(value.strip()) <= 240 and not any(ord(character) < 32 for character in value):
        return f"brevo:{value.strip()}"
    raise NotificationTransportError("Le prestataire n’a pas confirmé la référence d’envoi.", uncertain=True)


def _message_identifier(correlation, sender):
    domain = sender["email"].rsplit("@", 1)[1].encode("idna").decode("ascii")
    return f"<amelone.{correlation}@{domain}>"


def _subject(value):
    if not isinstance(value, str) or not value.strip() or any(character in value for character in "\r\n\x00"):
        raise NotificationTransportError("L’objet de l’e-mail est invalide.")
    return value


def _recipient_record(value):
    if not isinstance(value, str) or any(character in value for character in "\r\n\x00"):
        raise NotificationTransportError("L’adresse e-mail du destinataire est invalide.")
    name, address = parseaddr(value)
    record = {"email": _email_address(address)}
    if name:
        record["name"] = name
    return record


def _send_brevo_message(message, correlation):
    """Envoie un message Django via HTTPS, sans rappeler le backend Django."""
    _require_ready("email")
    if settings.EMAIL_PROVIDER != "brevo":
        raise NotificationTransportError("Configurez le prestataire Brevo pour ce backend e-mail.")
    sender = _sender(message.from_email)
    headers = dict(message.extra_headers)
    for name, value in headers.items():
        if not isinstance(name, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
            raise NotificationTransportError("Un en-tête de l’e-mail est invalide.")
        if not isinstance(value, str) or any(character in value for character in "\r\n\x00"):
            raise NotificationTransportError("Un en-tête de l’e-mail est invalide.")
    headers.setdefault("Message-ID", _message_identifier(correlation, sender))
    headers["X-AMELONE-Idempotency-Key"] = correlation
    message.extra_headers.update(headers)
    payload = {"sender": sender, "subject": _subject(message.subject), "headers": headers}
    for field in ("to", "cc", "bcc"):
        recipients = getattr(message, field, ())
        if recipients:
            payload[field] = [_recipient_record(value) for value in recipients]
    if not any(field in payload for field in ("to", "cc", "bcc")):
        raise NotificationTransportError("L’e-mail n’a aucun destinataire.")
    if message.body:
        content_key = "htmlContent" if message.content_subtype == "html" else "textContent"
        payload[content_key] = _body(message.body)
    for alternative in getattr(message, "alternatives", ()):
        content, mimetype = alternative
        if mimetype == "text/html":
            payload["htmlContent"] = _body(content)
    if not any(field in payload for field in ("textContent", "htmlContent")):
        raise NotificationTransportError("Le contenu de la notification est vide ou invalide.")
    if message.reply_to:
        if len(message.reply_to) != 1:
            raise NotificationTransportError("Configurez une seule adresse de réponse pour cet e-mail.")
        payload["replyTo"] = _recipient_record(message.reply_to[0])
    attachments = []
    for attachment in message.attachments:
        if isinstance(attachment, MIMEBase):
            filename, content = attachment.get_filename(), attachment.get_payload(decode=True)
        else:
            filename, content, _ = attachment
        if not isinstance(filename, str) or not filename or any(character in filename for character in "\r\n\x00"):
            raise NotificationTransportError("Le nom d’une pièce jointe est invalide.")
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not isinstance(content, bytes):
            raise NotificationTransportError("Une pièce jointe ne peut pas être envoyée.")
        attachments.append({"name": filename, "content": base64.b64encode(content).decode("ascii")})
    if attachments:
        payload["attachment"] = attachments
    return _provider_identifier(_brevo_request("smtp/email", payload, correlation).get("messageId"))


class BrevoEmailBackend(BaseEmailBackend):
    """Compatibilité EmailMessage, confirmations et réinitialisations via HTTPS."""

    def send_messages(self, email_messages):
        accepted = 0
        for message in email_messages or ():
            if not message.recipients():
                continue
            try:
                correlation = getattr(message, "_amelone_correlation", None)
                if correlation is None:
                    reference = message.extra_headers.get("Message-ID") or str(uuid.uuid4())
                    correlation = _correlation_key(reference)
                    message._amelone_correlation = correlation
                message.provider_id = _send_brevo_message(message, correlation)
            except NotificationTransportError:
                if not self.fail_silently:
                    raise
            else:
                accepted += 1
        return accepted


def send_notification_email(to, subject, body, idempotency_key):
    _require_ready("email")
    recipient = _email_address(to)
    subject = _subject(subject)
    body = _body(body)
    correlation = _correlation_key(idempotency_key)
    sender = _sender()
    message_id = _message_identifier(correlation, sender)
    if settings.EMAIL_PROVIDER == "brevo":
        result = _brevo_request("smtp/email", {
            "sender": sender, "to": [{"email": recipient}],
            "subject": subject, "textContent": body,
            "headers": {"Message-ID": message_id, "X-AMELONE-Idempotency-Key": correlation},
        }, correlation)
        return _provider_identifier(result.get("messageId"))
    try:
        message = EmailMessage(
            subject=subject, body=body, from_email=settings.DEFAULT_FROM_EMAIL, to=[recipient],
            headers={"Message-ID": message_id, "X-AMELONE-Idempotency-Key": correlation},
            connection=get_connection(timeout=_TIMEOUT_SECONDS),
        )
        accepted = message.send(fail_silently=False)
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPAuthenticationError,
            smtplib.SMTPConnectError, smtplib.SMTPHeloError, smtplib.SMTPDataError, BadHeaderError):
        raise NotificationTransportError("Le serveur SMTP a refusé l’envoi de l’e-mail.") from None
    except (ConnectionRefusedError, socket.gaierror, ssl.SSLCertVerificationError):
        raise NotificationTransportError("La connexion au serveur SMTP n’a pas pu être établie.") from None
    except Exception:
        raise NotificationTransportError("Le résultat de l’envoi SMTP n’a pas pu être confirmé.", uncertain=True) from None
    if accepted == 0:
        raise NotificationTransportError("Le serveur SMTP n’a accepté aucun e-mail.")
    if accepted != 1:
        raise NotificationTransportError("Le résultat de l’envoi SMTP n’a pas pu être confirmé.", uncertain=True)
    return message_id


def send_notification_sms(to, body, idempotency_key):
    _require_ready("sms")
    recipient = normalize_sms_phone(to)
    body = _body(body)
    correlation = _correlation_key(idempotency_key)
    result = _brevo_request("transactionalSMS/send", {
        "sender": settings.SMS_SENDER, "recipient": recipient[1:],
        "content": body, "type": "transactional", "unicodeEnabled": True,
        "tag": "amelone-" + correlation[:32],
    }, correlation)
    return _provider_identifier(result.get("messageId"))
