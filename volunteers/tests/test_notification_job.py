import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.test import Client, SimpleTestCase
from django.urls import reverse
from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError, PyJWKSetError

from volunteers.notification_job_auth import (
    GITHUB_ISSUER,
    GITHUB_JWKS_URL,
    NotificationJobUnauthorized,
    _github_jwks_client,
    verify_notification_job_token,
)


class NotificationJobAuthenticationTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public_key = cls.private_key.public_key()
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.client = Client(enforce_csrf_checks=True)
        self.url = reverse("process_notifications_job")
        self.keys = self.enterContext(patch("volunteers.notification_job_auth._github_jwks_client"))
        self.keys.return_value.get_signing_key_from_jwt.return_value = SimpleNamespace(key=self.public_key)
        self.process = self.enterContext(patch(
            "volunteers.views_notification_job.process_event_notifications",
            return_value={"enabled": True, "dry_run": False, "attempted": 1, "accepted": 1},
        ))

    def claims(self, **changes):
        now = int(time.time())
        return {
            "iss": GITHUB_ISSUER, "aud": "amelone-event-notifications",
            "iat": now - 5, "exp": now + 300,
            "repository": "wilfriedbarrer-stack/AMELONE", "repository_id": "1419056471",
            "repository_owner_id": "342539682", "ref": "refs/heads/main",
            "workflow_ref": "wilfriedbarrer-stack/AMELONE/.github/workflows/event-notifications.yml@refs/heads/main",
            "sub": "repo:wilfriedbarrer-stack/AMELONE:ref:refs/heads/main",
            "event_name": "schedule", **changes,
        }

    def token(self, *, claims=None, key=None, headers=None):
        return jwt.encode(
            claims if claims is not None else self.claims(), key or self.private_key,
            algorithm="RS256", headers={"kid": "github-test-key", **(headers or {})},
        )

    def post(self, token=None, *, body=b"{}", url=None):
        return self.client.post(
            url or self.url, data=body, content_type="application/json",
            HTTP_AUTHORIZATION="Bearer " + (token if token is not None else self.token()),
        )

    def assert_rejected(self, response):
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "Authentification refusée."})
        self.assertIn("no-store", response.headers["Cache-Control"])
        self.process.assert_not_called()

    def test_signed_schedule_and_dispatch_are_allowed_without_csrf_or_database_access(self):
        for event_name in ("schedule", "workflow_dispatch"):
            with self.subTest(event_name=event_name):
                response = self.post(self.token(claims=self.claims(event_name=event_name)))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["accepted"], 1)
                self.assertIn("no-store", response.headers["Cache-Control"])
        self.assertEqual(self.process.call_count, 2)
        self.process.assert_called_with(limit=1)

    def test_missing_malformed_or_unusable_authorization_is_generic_and_does_not_query_database(self):
        for header in (None, "", "Basic password", "Bearer", "Bearer a b", "Bearer not-a-jwt"):
            with self.subTest(header=header):
                kwargs = {"HTTP_AUTHORIZATION": header} if header is not None else {}
                self.assert_rejected(self.client.post(self.url, data=b"{}", content_type="application/json", **kwargs))
        self.keys.assert_not_called()

    def test_get_cannot_trigger_any_processing_or_key_fetch(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION="Bearer " + self.token())
        self.assertEqual(response.status_code, 405)
        self.process.assert_not_called()
        self.keys.assert_not_called()

    def test_wrong_signature_expiry_future_iat_or_nbf_are_rejected(self):
        now = int(time.time())
        tokens = (
            self.token(key=self.other_key), self.token(claims=self.claims(exp=now - 1)),
            self.token(claims=self.claims(iat=now + 300)), self.token(claims=self.claims(nbf=now + 300)),
        )
        for token in tokens:
            with self.subTest(token_type=tokens.index(token)):
                self.assert_rejected(self.post(token))

    def test_every_identity_restriction_is_checked_on_signed_claims(self):
        wrong_claims = (
            {"repository": "someone/AMELONE"}, {"repository_id": "999"},
            {"repository_owner_id": "999"}, {"ref": "refs/heads/feature"},
            {"workflow_ref": "wilfriedbarrer-stack/AMELONE/.github/workflows/another.yml@refs/heads/main"},
            {"sub": "repo:wilfriedbarrer-stack/AMELONE:pull_request"},
            {"event_name": "pull_request"}, {"event_name": "push"},
            {"iss": "https://example.test"}, {"aud": "another-audience"},
            {"aud": ["amelone-event-notifications", "extra"]},
        )
        for changes in wrong_claims:
            with self.subTest(changes=changes):
                self.assert_rejected(self.post(self.token(claims=self.claims(**changes))))

    def test_missing_required_claims_are_rejected(self):
        for name in ("exp", "iat", "iss", "aud", "sub", "repository", "repository_id",
                     "repository_owner_id", "ref", "workflow_ref", "event_name"):
            with self.subTest(name=name):
                claims = self.claims()
                del claims[name]
                self.assert_rejected(self.post(self.token(claims=claims)))

    def test_algorithm_confusion_or_header_supplied_keys_are_rejected_before_jwks_access(self):
        hs256 = jwt.encode(self.claims(), "test-only-hmac-key", algorithm="HS256", headers={"kid": "github-test-key"})
        unsecured = jwt.encode(self.claims(), key=None, algorithm="none", headers={"kid": "github-test-key"})
        tokens = (hs256, unsecured, self.token(headers={"jku": "https://attacker.test/keys"}),
                  self.token(headers={"jwk": {"kty": "RSA"}}), self.token(headers={"crit": ["custom"]}))
        for token in tokens:
            self.assert_rejected(self.post(token))
        self.keys.assert_not_called()

    def test_jwks_network_failure_returns_503_without_exposing_token_or_exception(self):
        self.keys.return_value.get_signing_key_from_jwt.side_effect = PyJWKClientConnectionError("private provider detail")
        token = self.token()
        response = self.post(token)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Authentification temporairement indisponible."})
        self.assertNotContains(response, token, status_code=503)
        self.assertNotContains(response, "private provider detail", status_code=503)
        self.process.assert_not_called()

    def test_unknown_key_id_is_rejected_generically(self):
        self.keys.return_value.get_signing_key_from_jwt.side_effect = PyJWKClientError("key unknown")
        self.assert_rejected(self.post())

    def test_malformed_provider_key_set_returns_generic_503(self):
        self.keys.return_value.get_signing_key_from_jwt.side_effect = PyJWKSetError("private raw response")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Authentification temporairement indisponible."})
        self.process.assert_not_called()

    def test_numeric_date_claims_cannot_be_encoded_as_strings_or_booleans(self):
        now = int(time.time())
        for changes in ({"exp": str(now + 300)}, {"iat": str(now - 5)}, {"iat": False}, {"exp": float("inf")}):
            self.assert_rejected(self.post(self.token(claims=self.claims(**changes))))

    def test_endpoint_accepts_only_empty_body_or_empty_object_and_never_accepts_limits_from_caller(self):
        response = self.post(body=b"")
        self.assertEqual(response.status_code, 200)
        self.process.assert_called_once_with(limit=1)
        self.process.reset_mock()
        for body in (b'{"limit":100}', b'{"retry":true}', b'[]', b'null', b'broken', b'""'):
            self.assertEqual(self.post(body=body).status_code, 400)
        self.assertEqual(self.post(url=self.url + "?limit=100").status_code, 400)
        self.process.assert_not_called()

    def test_disabled_global_result_is_returned_without_personal_data(self):
        self.process.return_value = {
            "enabled": False, "attempted": 0, "accepted": "person@example.test",
            "recipient": "person@example.test", "message": "Private message", "skipped": 0,
        }
        response = self.post()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"enabled": False, "attempted": 0, "skipped": 0})

    def test_processing_failure_never_exposes_trace_or_provider_details(self):
        self.process.side_effect = RuntimeError("Private provider key or recipient")
        response = self.post()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "Traitement temporairement indisponible."})

    def test_jwks_url_and_timeout_are_pinned_and_cached(self):
        # Examine la construction réelle du client sans interroger GitHub.
        _github_jwks_client.cache_clear()
        try:
            with patch("volunteers.notification_job_auth.PyJWKClient") as factory:
                _github_jwks_client()
                _github_jwks_client()
            factory.assert_called_once_with(GITHUB_JWKS_URL, timeout=5, cache_jwk_set=True, lifespan=300)
            self.assertEqual(GITHUB_ISSUER, "https://token.actions.githubusercontent.com")
        finally:
            _github_jwks_client.cache_clear()

    def test_direct_verification_never_accepts_oversized_or_nonstring_tokens(self):
        for token in (None, "", "A" * 16385):
            with self.assertRaises(NotificationJobUnauthorized):
                verify_notification_job_token(token)
        self.keys.assert_not_called()


class NotificationWorkflowTests(SimpleTestCase):
    def test_scheduled_workflow_inline_python_compiles_and_uses_no_checkout_or_shared_secret(self):
        workflow = (Path(settings.BASE_DIR) / ".github/workflows/event-notifications.yml").read_text()
        self.assertIn("7,22,37,52 * * * *", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("actions/checkout", workflow)
        lines = workflow.splitlines()
        start = lines.index("          python3 - <<'PY'") + 1
        end = lines.index("          PY", start)
        source = "\n".join(line[10:] for line in lines[start:end])
        compile(source, "event-notifications.yml inline Python", "exec")
        self.assertIn('result.get("attempted", 0) == 0', source)
        self.assertIn('time.monotonic() - refreshed_at >= 120', source)
