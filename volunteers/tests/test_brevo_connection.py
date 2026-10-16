import io
import json
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from volunteers.brevo_connection import check_brevo_connection
from volunteers.notification_transports import _NoRedirect


@override_settings(
    BREVO_API_KEY="fake-brevo-key-not-a-secret",
    DEFAULT_FROM_EMAIL="AMELONE <sender@example.test>",
    EMAIL_PROVIDER="brevo", EMAIL_ENABLED=False,
    EVENT_NOTIFICATIONS_ENABLED=False,
)
class BrevoConnectionTests(SimpleTestCase):
    def setUp(self):
        self.http_patch = patch("volunteers.brevo_connection.build_opener")
        self.opener_builder = self.http_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.opener = self.opener_builder.return_value
        self.opener.open.side_effect = AssertionError("Real provider HTTP forbidden")
        self.mail_patch = patch("django.core.mail.send_mail", side_effect=AssertionError("Email forbidden"))
        self.mail_mock = self.mail_patch.start()
        self.addCleanup(self.mail_patch.stop)

    def response(self, payload, *, raw=None, status=200):
        response = Mock()
        response.status = status
        response.read.return_value = json.dumps(payload).encode() if raw is None else raw
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        return context

    def responses(self, account=None, senders=None):
        self.opener.open.side_effect = [
            self.response({} if account is None else account),
            self.response({"senders": [{"email": "sender@example.test", "active": True}]} if senders is None else senders),
        ]

    def assert_safe(self, result):
        rendered = json.dumps(result)
        for sensitive in (
            "fake-brevo-key-not-a-secret", "sender@example.test", "private@example.test",
            "PRIVATE-RAW-BODY", "PRIVATE-ERROR", "SECRET-REDIRECT",
        ):
            self.assertNotIn(sensitive, rendered)
        self.assertEqual(set(result), {
            "key_present", "account_authenticated", "sender_validated",
            "transactional_relay_enabled", "ready", "reason",
        })
        for field in ("key_present", "account_authenticated", "sender_validated", "ready"):
            self.assertIsInstance(result[field], bool)
        self.mail_mock.assert_not_called()

    def test_ready_while_sending_disabled_uses_only_fixed_gets_and_no_redirects(self):
        self.responses(account={"email": "private@example.test", "relay": {"enabled": True}})
        result = check_brevo_connection()
        self.assertTrue(result["ready"])
        self.assertTrue(result["account_authenticated"])
        self.assertTrue(result["sender_validated"])
        self.assertIs(result["transactional_relay_enabled"], True)
        self.assertEqual(result["reason"], "ready")
        self.assertEqual(self.opener.open.call_count, 2)
        for call, expected in zip(self.opener.open.call_args_list, ("account", "senders")):
            request = call.args[0]
            self.assertEqual(request.full_url, "https://api.brevo.com/v3/" + expected)
            self.assertEqual(request.get_method(), "GET")
            self.assertIsNone(request.data)
            self.assertEqual(request.get_header("Api-key"), "fake-brevo-key-not-a-secret")
            self.assertEqual(call.kwargs, {"timeout": 10})
        for call in self.opener_builder.call_args_list:
            handler = call.args[0]
            self.assertIsInstance(handler, _NoRedirect)
            self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://example.test"))
        self.assert_safe(result)

    def test_missing_or_unsafe_key_has_no_network_call(self):
        for key in ("", "  ", None, "key\nPRIVATE-ERROR", "key\x00PRIVATE-ERROR"):
            with self.subTest(key_type=type(key).__name__), override_settings(BREVO_API_KEY=key):
                result = check_brevo_connection()
                self.assertFalse(result["key_present"])
                self.assertEqual(result["reason"], "key_not_configured")
                self.assert_safe(result)
        self.opener.open.assert_not_called()

    def test_provider_and_sender_are_required_without_network(self):
        for config, reason in (
            ({"EMAIL_PROVIDER": "django"}, "provider_not_brevo"),
            ({"DEFAULT_FROM_EMAIL": "invalid"}, "sender_not_configured"),
        ):
            with self.subTest(reason=reason), override_settings(**config):
                result = check_brevo_connection()
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["ready"])
                self.assert_safe(result)
        self.opener.open.assert_not_called()

    def test_sender_match_ignores_case_but_not_whitespace_or_substrings(self):
        for email, ready in (
            ("SENDER@EXAMPLE.TEST", True), (" sender@example.test", False),
            ("other-sender@example.test", False),
        ):
            with self.subTest(ready=ready):
                self.responses(senders={"senders": [{"email": email, "active": True}]})
                result = check_brevo_connection()
                self.assertIs(result["ready"], ready)
                self.assert_safe(result)

    def test_sender_active_requires_boolean_true(self):
        for active in (False, None, 1, "true"):
            with self.subTest(active_type=type(active).__name__):
                self.responses(senders={"senders": [{"email": "sender@example.test", "active": active}]})
                result = check_brevo_connection()
                self.assertFalse(result["ready"])
                self.assertTrue(result["account_authenticated"])
                self.assertEqual(result["reason"], "sender_not_validated")
                self.assert_safe(result)

    def test_optional_relay_flag_is_only_a_boolean_diagnostic(self):
        for relay, diagnostic in ((None, None), ({"enabled": False}, False), ({"enabled": "false"}, None)):
            with self.subTest(diagnostic=diagnostic):
                self.responses(account={"relay": relay})
                result = check_brevo_connection()
                self.assertTrue(result["ready"])
                self.assertIs(result["transactional_relay_enabled"], diagnostic)
                self.assert_safe(result)

    def test_http_errors_are_distinct_and_never_expose_provider_details(self):
        for status, reason in (
            (401, "invalid_api_key"), (403, "access_denied"),
            (302, "redirect_refused"), (429, "provider_unavailable"),
            (503, "provider_unavailable"), (400, "unexpected_http_status"),
        ):
            for endpoint in ("account", "senders"):
                with self.subTest(status=status, endpoint=endpoint):
                    body = io.BytesIO(b"PRIVATE-RAW-BODY")
                    error = HTTPError("https://SECRET-REDIRECT", status, "PRIVATE-ERROR", {}, body)
                    self.opener.open.side_effect = ([self.response({})] if endpoint == "senders" else []) + [error]
                    result = check_brevo_connection()
                    self.assertFalse(result["ready"])
                    self.assertEqual(result["account_authenticated"], endpoint == "senders")
                    self.assertEqual(result["reason"], reason)
                    self.assertTrue(body.closed)
                    self.assert_safe(result)

    def test_network_failures_are_safe_and_timeouts_are_distinct(self):
        for error, reason in (
            (TimeoutError("PRIVATE-ERROR"), "network_timeout"),
            (URLError(TimeoutError("PRIVATE-ERROR")), "network_timeout"),
            (URLError(ConnectionRefusedError("PRIVATE-ERROR")), "connection_failed"),
            (OSError("PRIVATE-ERROR"), "connection_failed"),
            (ValueError("PRIVATE-ERROR"), "connection_failed"),
        ):
            with self.subTest(reason=reason):
                self.opener.open.side_effect = error
                result = check_brevo_connection()
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["ready"])
                self.assert_safe(result)

    def test_non_200_response_cannot_authenticate(self):
        self.opener.open.side_effect = [self.response({}, status=201)]
        result = check_brevo_connection()
        self.assertFalse(result["account_authenticated"])
        self.assertEqual(result["reason"], "unexpected_http_status")
        self.assert_safe(result)

    def test_malformed_and_oversized_responses_are_bounded(self):
        for raw, reason in (
            (b"PRIVATE-RAW-BODY", "malformed_response"),
            (b"\xff", "malformed_response"), (b"[]", "malformed_response"),
            (b"null", "malformed_response"),
            (b"[" * 2000 + b"]" * 2000, "malformed_response"),
            (b"x" * 65537, "response_too_large"),
        ):
            with self.subTest(reason=reason):
                context = self.response(None, raw=raw)
                self.opener.open.side_effect = [context]
                result = check_brevo_connection()
                self.assertEqual(result["reason"], reason)
                self.assertFalse(result["account_authenticated"])
                context.__enter__.return_value.read.assert_called_once_with(65537)
                self.assert_safe(result)

    def test_senders_payload_requires_a_list_of_objects(self):
        for payload in ({}, {"senders": None}, {"senders": {}}, {"senders": ["PRIVATE-RAW-BODY"]}):
            with self.subTest(payload_type=type(payload.get("senders")).__name__):
                self.responses(senders=payload)
                result = check_brevo_connection()
                self.assertEqual(result["reason"], "malformed_response")
                self.assertTrue(result["account_authenticated"])
                self.assertFalse(result["ready"])
                self.assert_safe(result)

    def test_command_prints_one_safe_json_line_and_fails_unless_no_fail(self):
        with override_settings(BREVO_API_KEY=""):
            for no_fail in (False, True):
                output = io.StringIO()
                if no_fail:
                    call_command("check_brevo_connection", no_fail=True, stdout=output)
                else:
                    with self.assertRaisesMessage(CommandError, "BREVO_CONNECTION_CHECK_NOT_READY"):
                        call_command("check_brevo_connection", stdout=output)
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("BREVO_CONNECTION_CHECK "))
                result = json.loads(lines[0].partition(" ")[2])
                self.assertFalse(result["ready"])
                self.assert_safe(result)
        self.opener.open.assert_not_called()

    def test_command_ready_exits_successfully(self):
        self.responses()
        output = io.StringIO()
        call_command("check_brevo_connection", stdout=output)
        result = json.loads(output.getvalue().partition(" ")[2])
        self.assertTrue(result["ready"])
        self.assert_safe(result)
