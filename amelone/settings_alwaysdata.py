"""Alwaysdata settings; set variables in the hosting panel, without a dotenv file."""

import os as _os
import re as _re
from urllib.parse import urlsplit as _urlsplit

from django.core.exceptions import ImproperlyConfigured as _ImproperlyConfigured

# Validate before importing the shared settings: they intentionally support local
# development defaults, and database parser errors must not disclose credentials.
_site_host = _os.environ.get("ALWAYSDATA_SITE_HOST", "").lower()
_hostname_label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
if (
    len(_site_host) > 253
    or not _re.fullmatch(rf"{_hostname_label}(?:\.{_hostname_label})+", _site_host)
    or not any(character.isalpha() for character in _site_host.rsplit(".", 1)[-1])
):
    raise _ImproperlyConfigured(
        "ALWAYSDATA_SITE_HOST doit être un nom d’hôte complet, sans URL ni port."
    )

_secret_key = _os.environ.get("DJANGO_SECRET_KEY", "")
if not _secret_key.strip():
    raise _ImproperlyConfigured("DJANGO_SECRET_KEY est obligatoire sur Alwaysdata.")

_database_url = _os.environ.get("DATABASE_URL", "")
try:
    _database_parts = _urlsplit(_database_url)
    _valid_database = (
        bool(_database_url)
        and not any(character.isspace() for character in _database_url)
        and _database_parts.scheme in {"postgres", "postgresql", "pgsql"}
        and bool(_database_parts.hostname)
        and bool(_database_parts.path.strip("/"))
        and not _database_parts.fragment
    )
    # Accessing .port validates it without echoing a bad value in an exception.
    _database_parts.port
except (ValueError, UnicodeError):
    _valid_database = False
if not _valid_database:
    raise _ImproperlyConfigured(
        "DATABASE_URL doit fournir une connexion PostgreSQL valide sur Alwaysdata."
    )

try:
    from .settings import *  # noqa: F403
except (ValueError, UnicodeError):
    raise _ImproperlyConfigured(
        "Configuration Alwaysdata invalide ; vérifier les variables du panneau."
    ) from None

if DATABASES.get("default", {}).get("ENGINE") != "django.db.backends.postgresql":  # noqa: F405
    raise _ImproperlyConfigured("Alwaysdata nécessite le moteur PostgreSQL de Django.")

DEBUG = False
SECRET_KEY = _secret_key
ALLOWED_HOSTS = [_site_host]
CSRF_TRUSTED_ORIGINS = [f"https://{_site_host}"]
SITE_URL = f"https://{_site_host}"

# Alwaysdata terminates TLS and supplies this header to the private WSGI process.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = False
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_SSL_REDIRECT = True
try:
    SECURE_HSTS_SECONDS = int(_os.environ.get("ALWAYSDATA_HSTS_SECONDS", "3600"))
except ValueError:
    raise _ImproperlyConfigured(
        "ALWAYSDATA_HSTS_SECONDS doit être un entier positif ou nul."
    ) from None
if SECURE_HSTS_SECONDS < 0:
    raise _ImproperlyConfigured(
        "ALWAYSDATA_HSTS_SECONDS doit être un entier positif ou nul."
    )
# The provider's domain may be shared by unrelated sites.
SECURE_HSTS_INCLUDE_SUBDOMAINS = False
SECURE_HSTS_PRELOAD = False

# A restored production environment must stay silent until the explicit cutover.
EMAIL_ENABLED = (
    _os.environ.get("ALWAYSDATA_ENABLE_EMAILS", "0") == "1"
    and _os.environ.get("EMAIL_ENABLED", "0") == "1"
)
EVENT_NOTIFICATIONS_ENABLED = (
    EMAIL_ENABLED and _os.environ.get("EVENT_NOTIFICATIONS_ENABLED", "0") == "1"
)
SMS_ENABLED = False
EMAIL_BACKEND = (
    "django.core.mail.backends.dummy.EmailBackend"
    if not EMAIL_ENABLED
    else "volunteers.notification_transports.BrevoEmailBackend"
    if EMAIL_PROVIDER == "brevo"  # noqa: F405
    else "django.core.mail.backends.smtp.EmailBackend"
)
