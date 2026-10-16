"""Authentification du workflow GitHub par son identité OIDC signée et limitée."""

import logging
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
logger = logging.getLogger(__name__)


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
    owner, name = repository.split("/", 1)
    # AMELONE was created on 2026-10-14. GitHub repositories created after
    # 2026-07-15 use immutable subjects; the previous name-only subject is invalid.
    # https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims
    subject_repository = (
        f"{owner}@{settings.EVENT_NOTIFICATIONS_JOB_OWNER_ID}/"
        f"{name}@{settings.EVENT_NOTIFICATIONS_JOB_REPOSITORY_ID}"
    )
    return {
        "repository": repository,
        "repository_id": settings.EVENT_NOTIFICATIONS_JOB_REPOSITORY_ID,
        "repository_owner_id": settings.EVENT_NOTIFICATIONS_JOB_OWNER_ID,
        "ref": ref,
        "workflow_ref": settings.EVENT_NOTIFICATIONS_JOB_WORKFLOW_REF,
        "sub": f"repo:{subject_repository}:ref:{ref}",
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
        logger.warning("Notification scheduler identity rejected: signing_key_unavailable.")
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
        # The exception type is useful for operations; its message can contain
        # claims from the token and must never be logged.
        logger.warning("Notification scheduler identity rejected: %s.", type(error).__name__)
        raise NotificationJobUnauthorized() from error
    if any(type(claims[name]) not in (int, float) for name in ("exp", "iat", "nbf") if name in claims):
        logger.warning("Notification scheduler identity rejected: timestamp_type.")
        raise NotificationJobUnauthorized()
    mismatched_claims = [key for key, value in _expected_claims().items() if claims.get(key) != value]
    if mismatched_claims:
        # These are fixed, public claim names, never their received values.
        logger.warning("Notification scheduler identity rejected: claims %s.", ", ".join(mismatched_claims))
        raise NotificationJobUnauthorized()
    if claims.get("event_name") not in ("schedule", "workflow_dispatch"):
        logger.warning("Notification scheduler identity rejected: event_name.")
        raise NotificationJobUnauthorized()
    return claims
