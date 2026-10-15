"""Authentification du workflow GitHub par son identité OIDC signée et limitée."""

from functools import lru_cache

import jwt
from django.conf import settings
from jwt import PyJWKClient
from jwt.exceptions import (
    PyJWKClientConnectionError,
    PyJWKClientError,
    PyJWKError,
    PyJWKSetError,
)

GITHUB_ISSUER = "https://token.actions.githubusercontent.com"
GITHUB_JWKS_URL = GITHUB_ISSUER + "/.well-known/jwks"
REQUIRED_CLAIMS = (
    "exp", "iat", "iss", "aud", "sub", "repository", "repository_id",
    "repository_owner_id", "ref", "workflow_ref", "event_name",
)


class NotificationJobUnauthorized(Exception):
    """Identité absente, invalide ou non autorisée."""


class NotificationJobUnavailable(Exception):
    """Les clés publiques GitHub ne peuvent pas être vérifiées actuellement."""


@lru_cache(maxsize=1)
def _github_jwks_client():
    # L'URL ne vient jamais du JWT (jku, iss) ou d'une requête entrante.
    return PyJWKClient(
        GITHUB_JWKS_URL, timeout=5, cache_jwk_set=True, lifespan=300,
    )


def _expected_claims():
    repository = settings.EVENT_NOTIFICATIONS_JOB_REPOSITORY
    ref = settings.EVENT_NOTIFICATIONS_JOB_REF
    return {
        "repository": repository,
        "repository_id": settings.EVENT_NOTIFICATIONS_JOB_REPOSITORY_ID,
        "repository_owner_id": settings.EVENT_NOTIFICATIONS_JOB_OWNER_ID,
        "ref": ref,
        "workflow_ref": settings.EVENT_NOTIFICATIONS_JOB_WORKFLOW_REF,
        "sub": f"repo:{repository}:ref:{ref}",
    }


def verify_notification_job_token(token):
    """Valide la signature RS256 puis chaque restriction de l'identité GitHub."""
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise NotificationJobUnauthorized()
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError as error:
        raise NotificationJobUnauthorized() from error
    if (
        header.get("alg") != "RS256"
        or not isinstance(header.get("kid"), str)
        or not 1 <= len(header["kid"]) <= 256
        or header.get("crit")
        or "jku" in header or "jwk" in header or "x5u" in header
    ):
        raise NotificationJobUnauthorized()
    try:
        signing_key = _github_jwks_client().get_signing_key_from_jwt(token).key
    except PyJWKClientConnectionError as error:
        raise NotificationJobUnavailable() from error
    except (PyJWKError, PyJWKSetError) as error:
        raise NotificationJobUnavailable() from error
    except PyJWKClientError as error:
        raise NotificationJobUnauthorized() from error
    except (OSError, ValueError) as error:
        raise NotificationJobUnavailable() from error
    try:
        claims = jwt.decode(
            token, signing_key, algorithms=["RS256"],
            audience=settings.EVENT_NOTIFICATIONS_JOB_AUDIENCE,
            issuer=GITHUB_ISSUER, options={"require": list(REQUIRED_CLAIMS), "strict_aud": True},
        )
    except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError) as error:
        raise NotificationJobUnauthorized() from error
    if any(type(claims[name]) not in (int, float) for name in ("exp", "iat", "nbf") if name in claims):
        raise NotificationJobUnauthorized()
    if any(claims.get(key) != value for key, value in _expected_claims().items()):
        raise NotificationJobUnauthorized()
    if claims.get("event_name") not in ("schedule", "workflow_dispatch"):
        raise NotificationJobUnauthorized()
    return claims
