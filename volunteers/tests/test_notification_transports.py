import hashlib
import io
import json
import os
import runpy
import smtplib
import socket
import traceback
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from django.core import mail
from django.core.mail import EmailMessage as DjangoEmailMessage
from django.core.mail import EmailMultiAlternatives
from django.test import SimpleTestCase, override_settings

from volunteers.notification_transports import (
    BrevoEmailBackend,
    NotificationTransportError,
    _NoRedirect,
    get_channel_readiness,
    normalize_sms_phone,
    normalize_sms_recipient,
    send_notification_email,
    send_notification_sms,
)


@override_settings(
    EVENT_NOTIFICATIONS_ENABLED=False,
    EMAIL_ENABLED=True, EMAIL_PROVIDER="django",
    EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend",
    EMAIL_HOST="smtp.example.test", EMAIL_USE_TLS=True, EMAIL_USE_SSL=False,
    DEFAULT_FROM_EMAIL="AMELONE <association@example.test>",
    BREVO_API_KEY="fake-test-key-not-a-secret",
    SMS_ENABLED=True, SMS_PROVIDER="brevo", SMS_SENDER="AMELONE",
)
class NotificationTransportTests(SimpleTestCase):
    def setUp(self):
        self.http_patch = patch("volunteers.notification_transports.build_opener")
        self.smtp_patch = patch("volunteers.notification_transports.get_connection")
        self.message_patch = patch("volunteers.notification_transports.EmailMessage")
        self.opener = self.http_patch.start().return_value
        self.connection = self.smtp_patch.start()
        self.message = self.message_patch.start()
        self.addCleanup(self.http_patch.stop)
        self.addCleanup(self.smtp_patch.stop)
        self.addCleanup(self.message_patch.stop)
        self.opener.open.side_effect = AssertionError("A real HTTP call is forbidden in tests")
        self.message.return_value.send.side_effect = AssertionError("A real SMTP call is forbidden in tests")

    def response(self, payload):
        self.opener.open.side_effect = None
        self.opener.open.return_value.__enter__.return_value = io.BytesIO(
            json.dumps(payload).encode("utf-8"),
        )

    def sms(self, **changes):
        return send_notification_sms(**{
            "to": "06 12 34 56 78", "body": "Le concours est ouvert.",
            "idempotency_key": "event-1-family-1-sms-publication", **changes,
        })

    def email(self, **changes):
        return send_notification_email(**{
            "to": "participant@example.test", "subject": "Inscriptions ouvertes",
            "body": "Le concours est ouvert.", "idempotency_key": "event-1-family-1-email-publication",
            **changes,
        })

    def assert_sanitized(self, error):
        rendered = "".join(traceback.format_exception(error))
        for sensitive in (
            "fake-test-key-not-a-secret", "participant@example.test", "0612345678",
            "RAW-PROVIDER-RESPONSE", "PRIVATE-CREDENTIAL",
        ):
            self.assertNotIn(sensitive, str(error))
            self.assertNotIn(sensitive, rendered)

    def test_french_mobile_formats_and_international_e164_are_normalized(self):
        for value in ("0612345678", "06 12 34 56 78", "06.12.34.56.78", "0033612345678", "+33 (6) 12-34-56-78"):
            self.assertEqual(normalize_sms_phone(value), "+33612345678")
        self.assertEqual(normalize_sms_recipient("0712345678"), "+33712345678")
        self.assertEqual(normalize_sms_phone("+447700900123"), "+447700900123")

    def test_landlines_invalid_formats_and_injection_are_rejected_without_echoing_phone(self):
        for value in ("0112345678", "+33112345678", "00330112345678", "06", "06abc45678", None, "not-a-phone", "+0000000000", "٠٦١٢٣٤٥٦٧٨"):
            with self.subTest(value=value), self.assertRaises(NotificationTransportError) as caught:
                normalize_sms_phone(value)
            self.assertFalse(caught.exception.uncertain)
            self.assertNotIn(str(value), str(caught.exception))

    def test_readiness_is_local_and_independent_of_global_campaign_switch(self):
        self.assertTrue(get_channel_readiness("email")["ready"])
        self.assertTrue(get_channel_readiness("sms")["ready"])
        self.assertFalse(get_channel_readiness("unknown")["ready"])
        self.opener.open.assert_not_called()
        self.connection.assert_not_called()

    def test_disabled_email_and_sms_are_not_reported_as_accepted(self):
        for channel, configuration, call in (
            ("email", {"EMAIL_ENABLED": False}, self.email),
            ("sms", {"SMS_ENABLED": False}, self.sms),
        ):
            with self.subTest(channel=channel), override_settings(**configuration):
                self.assertFalse(get_channel_readiness(channel)["ready"])
                with self.assertRaises(NotificationTransportError) as caught:
                    call()
                self.assertFalse(caught.exception.uncertain)
        self.opener.open.assert_not_called()
        self.connection.assert_not_called()

    def test_console_dummy_and_memory_email_backends_cannot_fake_delivery(self):
        for backend in ("console", "dummy", "locmem"):
            with self.subTest(backend=backend), override_settings(EMAIL_BACKEND=f"django.core.mail.backends.{backend}.EmailBackend"):
                self.assertFalse(get_channel_readiness("email")["ready"])
                with self.assertRaises(NotificationTransportError):
                    self.email()
        self.message.assert_not_called()

    def test_missing_provider_configuration_and_unsafe_headers_are_not_ready(self):
        cases = (
            ("email", {"EMAIL_HOST": ""}), ("email", {"EMAIL_PROVIDER": "unsupported"}),
            ("email", {"EMAIL_USE_SSL": True}), ("email", {"DEFAULT_FROM_EMAIL": "invalid"}),
            ("email", {"DEFAULT_FROM_EMAIL": "mail@example.test\r\nPRIVATE-CREDENTIAL"}),
            ("email", {"EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": ""}),
            ("sms", {"SMS_PROVIDER": ""}), ("sms", {"SMS_PROVIDER": "twilio"}),
            ("sms", {"BREVO_API_KEY": ""}), ("sms", {"SMS_SENDER": ""}),
            ("sms", {"SMS_SENDER": "Name\r\nPRIVATE-CREDENTIAL"}),
            ("sms", {"BREVO_API_KEY": "key\r\nPRIVATE-CREDENTIAL"}),
        )
        for channel, configuration in cases:
            with self.subTest(channel=channel, keys=list(configuration)), override_settings(**configuration):
                readiness = get_channel_readiness(channel)
                self.assertFalse(readiness["ready"])
                self.assertNotIn("PRIVATE-CREDENTIAL", readiness["reason"])
        self.opener.open.assert_not_called()

    def test_smtp_success_uses_stable_message_id_and_bounded_timeout(self):
        self.message.return_value.send.side_effect = None
        self.message.return_value.send.return_value = 1
        first = self.email()
        second = self.email()
        self.assertEqual(first, second)
        self.assertTrue(first.startswith("<amelone."))
        self.assertTrue(first.endswith("@example.test>"))
        self.connection.assert_called_with(timeout=10)
        kwargs = self.message.call_args.kwargs
        self.assertEqual(kwargs["to"], ["participant@example.test"])
        self.assertEqual(kwargs["headers"]["Message-ID"], first)
        self.assertNotIn("event-1-family-1-email-publication", kwargs["headers"]["X-AMELONE-Idempotency-Key"])

    def test_smtp_zero_acceptance_is_a_failure(self):
        self.message.return_value.send.side_effect = None
        self.message.return_value.send.return_value = 0
        with self.assertRaises(NotificationTransportError) as caught:
            self.email()
        self.assertFalse(caught.exception.uncertain)

    def test_smtp_explicit_refusal_is_safe_and_not_ambiguous(self):
        cases = (
            smtplib.SMTPAuthenticationError(535, b"PRIVATE-CREDENTIAL RAW-PROVIDER-RESPONSE"),
            smtplib.SMTPDataError(550, b"RAW-PROVIDER-RESPONSE"),
            smtplib.SMTPRecipientsRefused({"participant@example.test": (550, b"RAW-PROVIDER-RESPONSE")}),
            ConnectionRefusedError("PRIVATE-CREDENTIAL"),
        )
        for error in cases:
            with self.subTest(error_type=type(error).__name__):
                self.message.return_value.send.side_effect = error
                with self.assertRaises(NotificationTransportError) as caught:
                    self.email()
                self.assertFalse(caught.exception.uncertain)
                self.assert_sanitized(caught.exception)

    def test_smtp_timeout_or_disconnect_is_ambiguous_and_never_retried(self):
        for error in (TimeoutError("PRIVATE-CREDENTIAL"), smtplib.SMTPServerDisconnected("RAW-PROVIDER-RESPONSE")):
            with self.subTest(error_type=type(error).__name__):
                self.message.return_value.send.reset_mock()
                self.message.return_value.send.side_effect = error
                with self.assertRaises(NotificationTransportError) as caught:
                    self.email()
                self.assertTrue(caught.exception.uncertain)
                self.message.return_value.send.assert_called_once()
                self.assert_sanitized(caught.exception)

    def test_invalid_recipient_subject_content_or_reference_never_reaches_network(self):
        cases = (
            {"to": "bad-email"}, {"to": "participant@example.test\r\nBcc: other@example.test"},
            {"subject": "Subject\nPRIVATE-CREDENTIAL"}, {"body": ""}, {"idempotency_key": ""},
        )
        for changes in cases:
            with self.subTest(changes=list(changes)), self.assertRaises(NotificationTransportError) as caught:
                self.email(**changes)
            self.assertFalse(caught.exception.uncertain)
        self.message.assert_not_called()
        self.opener.open.assert_not_called()

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_HOST="")
    def test_brevo_email_uses_https_and_real_provider_reference_without_smtp(self):
        self.assertTrue(get_channel_readiness("email")["ready"])
        self.response({"messageId": "<provider-123@smtp-relay.mailin.fr>"})
        provider_id = self.email()
        self.assertEqual(provider_id, "brevo:<provider-123@smtp-relay.mailin.fr>")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 10)
        payload = json.loads(request.data)
        self.assertEqual(payload["to"], [{"email": "participant@example.test"}])
        self.assertEqual(payload["sender"]["email"], "association@example.test")
        self.assertEqual(payload["subject"], "Inscriptions ouvertes")
        self.assertEqual(payload["textContent"], "Le concours est ouvert.")
        self.connection.assert_not_called()

    def test_brevo_sms_sends_normalized_recipient_and_stable_correlation_tag(self):
        self.response({"messageId": 123, "reference": "provider-reference"})
        self.assertEqual(self.sms(), "brevo:123")
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/transactionalSMS/send")
        payload = json.loads(request.data)
        self.assertEqual(payload["recipient"], "33612345678")
        self.assertEqual(payload["sender"], "AMELONE")
        self.assertEqual(payload["type"], "transactional")
        digest = hashlib.sha256(b"event-1-family-1-sms-publication").hexdigest()
        self.assertEqual(payload["tag"], "amelone-" + digest[:32])
        self.assertEqual(request.get_header("X-amelone-idempotency-key"), digest)
        self.opener.open.assert_called_once()

    def test_sms_invalid_phone_is_skipped_before_any_network_call(self):
        with self.assertRaises(NotificationTransportError) as caught:
            self.sms(to="01 23 45 67 89")
        self.assertFalse(caught.exception.uncertain)
        self.opener.open.assert_not_called()

    def test_http_explicit_refusal_and_ambiguous_provider_failure_are_sanitized(self):
        for status, uncertain in ((400, False), (401, False), (402, False), (429, False), (408, True), (409, True), (500, True), (503, True)):
            with self.subTest(status=status):
                self.opener.open.reset_mock()
                error = HTTPError("https://api.brevo.com/private", status, "PRIVATE-CREDENTIAL", {}, io.BytesIO(b"RAW-PROVIDER-RESPONSE 0612345678"))
                self.opener.open.side_effect = error
                with self.assertRaises(NotificationTransportError) as caught:
                    self.sms()
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assert_sanitized(caught.exception)
                self.opener.open.assert_called_once()

    def test_timeout_is_ambiguous_and_dns_or_connection_refusal_is_definite(self):
        for error, uncertain in (
            (TimeoutError("PRIVATE-CREDENTIAL"), True),
            (URLError(TimeoutError("PRIVATE-CREDENTIAL")), True),
            (URLError(socket.gaierror("PRIVATE-CREDENTIAL")), False),
            (ConnectionRefusedError("PRIVATE-CREDENTIAL"), False),
        ):
            with self.subTest(error_type=type(error).__name__):
                self.opener.open.reset_mock()
                self.opener.open.side_effect = error
                with self.assertRaises(NotificationTransportError) as caught:
                    self.sms()
                self.assertEqual(caught.exception.uncertain, uncertain)
                self.assert_sanitized(caught.exception)
                self.opener.open.assert_called_once()

    def test_success_without_valid_reference_is_ambiguous(self):
        for payload in ({}, {"messageId": None}, {"messageId": False}, {"messageId": 0}, {"messageId": ""}, ["not-a-dict"]):
            with self.subTest(payload=payload):
                self.response(payload)
                with self.assertRaises(NotificationTransportError) as caught:
                    self.sms()
                self.assertTrue(caught.exception.uncertain)

    def test_malformed_or_oversized_success_response_is_ambiguous(self):
        self.opener.open.side_effect = None
        for raw in (b"RAW-PROVIDER-RESPONSE", b"x" * 65537):
            self.opener.open.return_value.__enter__.return_value = io.BytesIO(raw)
            with self.assertRaises(NotificationTransportError) as caught:
                self.sms()
            self.assertTrue(caught.exception.uncertain)
            self.assert_sanitized(caught.exception)

    def test_redirects_are_never_followed_with_api_credentials(self):
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example.test/"))

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_BACKEND="volunteers.notification_transports.BrevoEmailBackend")
    def test_existing_django_emailmessage_is_delivered_through_https_backend(self):
        self.response({"messageId": "<legacy-confirmation@brevo.test>"})
        message = DjangoEmailMessage(
            "Confirmation des postes", "Vos affectations confirmées.", to=["participant@example.test"],
        )
        self.assertEqual(message.send(fail_silently=False), 1)
        self.assertEqual(message.provider_id, "brevo:<legacy-confirmation@brevo.test>")
        self.connection.assert_not_called()
        self.message.assert_not_called()
        request = self.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(json.loads(request.data)["textContent"], "Vos affectations confirmées.")

    @override_settings(EMAIL_PROVIDER="brevo")
    def test_brevo_backend_preserves_html_reply_address_recipients_and_attachments(self):
        self.response({"messageId": "<reset-message@brevo.test>"})
        message = EmailMultiAlternatives(
            "Réinitialisation", "Texte du message.", to=["Participant <participant@example.test>"],
            cc=["copy@example.test"], bcc=["private-copy@example.test"],
            reply_to=["Association <reply@example.test>"], connection=BrevoEmailBackend(),
        )
        message.attach_alternative("<p>Texte du message.</p>", "text/html")
        message.attach("postes.csv", "Poste;Heure\nAccueil;08:00", "text/csv")
        self.assertEqual(message.send(), 1)
        payload = json.loads(self.opener.open.call_args.args[0].data)
        self.assertEqual(payload["textContent"], "Texte du message.")
        self.assertEqual(payload["htmlContent"], "<p>Texte du message.</p>")
        self.assertEqual(payload["to"], [{"email": "participant@example.test", "name": "Participant"}])
        self.assertEqual(payload["bcc"], [{"email": "private-copy@example.test"}])
        self.assertEqual(payload["replyTo"], {"email": "reply@example.test", "name": "Association"})
        self.assertEqual(payload["attachment"][0]["name"], "postes.csv")
        self.assertEqual(payload["attachment"][0]["content"], "UG9zdGU7SGV1cmUKQWNjdWVpbDswODowMA==")

    @override_settings(EMAIL_PROVIDER="brevo")
    def test_brevo_backend_failure_silent_mode_returns_zero_instead_of_fake_acceptance(self):
        self.opener.open.side_effect = TimeoutError("PRIVATE-CREDENTIAL RAW-PROVIDER-RESPONSE")
        message = DjangoEmailMessage(
            "Annonce", "Texte", to=["participant@example.test"],
            connection=BrevoEmailBackend(fail_silently=True),
        )
        self.assertEqual(message.send(), 0)
        self.assertFalse(hasattr(message, "provider_id"))
        self.opener.open.assert_called_once()

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_ENABLED=False)
    def test_disabled_brevo_backend_never_sends_legacy_messages(self):
        message = DjangoEmailMessage(
            "Annonce", "Texte", to=["participant@example.test"], connection=BrevoEmailBackend(),
        )
        with self.assertRaises(NotificationTransportError) as caught:
            message.send()
        self.assertFalse(caught.exception.uncertain)
        self.opener.open.assert_not_called()

    @override_settings(EMAIL_PROVIDER="brevo", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_explicit_memory_backend_for_existing_django_tests_stays_supported(self):
        message = DjangoEmailMessage("Test Django", "En mémoire.", to=["participant@example.test"])
        self.assertEqual(message.send(), 1)
        self.assertEqual(mail.outbox[-1].body, "En mémoire.")
        self.opener.open.assert_not_called()


class NotificationTransportSettingsTests(SimpleTestCase):
    def test_provider_selects_compatible_django_backend_without_overriding_explicit_backend(self):
        cases = (
            ({"EMAIL_PROVIDER": "brevo", "EMAIL_ENABLED": "1"}, "volunteers.notification_transports.BrevoEmailBackend"),
            ({"EMAIL_PROVIDER": "django", "EMAIL_ENABLED": "1"}, "django.core.mail.backends.smtp.EmailBackend"),
            ({"EMAIL_PROVIDER": "brevo", "EMAIL_ENABLED": "1", "EMAIL_BACKEND": "django.core.mail.backends.locmem.EmailBackend"}, "django.core.mail.backends.locmem.EmailBackend"),
            ({"EMAIL_PROVIDER": "brevo", "EMAIL_ENABLED": "0"}, "django.core.mail.backends.console.EmailBackend"),
        )
        for changes, expected_backend in cases:
            with self.subTest(keys=changes), patch.dict(os.environ, {"DJANGO_DEBUG": "1", **changes}, clear=True):
                config = runpy.run_path("amelone/settings.py")
            self.assertEqual(config["EMAIL_BACKEND"], expected_backend)
            self.assertFalse(config["EVENT_NOTIFICATIONS_ENABLED"])
            self.assertFalse(config["SMS_ENABLED"])
            self.assertEqual(config["SMS_PROVIDER"], "")
