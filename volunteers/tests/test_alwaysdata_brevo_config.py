import contextlib
import importlib.util
import io
import os
import shlex
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[2] / "deploy/alwaysdata/configure_brevo.py"
SPEC = importlib.util.spec_from_file_location("alwaysdata_brevo_config", SOURCE)
BREVO = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BREVO)
READY = {"key_present": True, "account_authenticated": True, "sender_validated": True,
         "transactional_relay_enabled": True, "ready": True, "reason": "ready"}
BLOCKED = {**READY, "account_authenticated": False, "sender_validated": False,
           "ready": False, "reason": "ip_not_authorized"}


class AlwaysdataBrevoConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "amelone.env"
        self.values = {
            "DJANGO_SETTINGS_MODULE": "amelone.settings_alwaysdata", "DJANGO_DEBUG": "0",
            "ALWAYSDATA_SITE_HOST": "amelone.alwaysdata.net", "DJANGO_SECRET_KEY": "PRIVATE-generated-key",
            "DATABASE_URL": "postgresql://amelone:PRIVATE-db@postgresql-amelone.alwaysdata.net/amelone_prod?sslmode=require",
            "ALWAYSDATA_HSTS_SECONDS": "3600", "ALWAYSDATA_ENABLE_EMAILS": "0", "EMAIL_ENABLED": "0",
            "EVENT_NOTIFICATIONS_ENABLED": "0", "SMS_ENABLED": "0", "EMAIL_PROVIDER": "brevo",
            "DEFAULT_FROM_EMAIL": "AMELONE <sender@example.test>", "BREVO_API_KEY": "PRIVATE-existing-key",
        }
        self.write_file()

    def write_file(self, overrides=None):
        values = {**self.values, **(overrides or {})}
        self.path.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))
        self.path.chmod(0o600)

    def run_main(self, arguments=()):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = BREVO.main(["--env-file", str(self.path), *arguments])
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_key_is_quoted_atomically_and_preserves_database_and_django_secret(self):
        self.write_file({key: "1" for key in BREVO.SENDING_FLAGS})
        key = "PRIVATE-key;quote'\\$x"
        replacement = os.replace
        published_modes = []
        def publish(source, destination):
            published_modes.append(stat.S_IMODE(Path(source).stat().st_mode))
            replacement(source, destination)
        with patch.object(BREVO.getpass, "getpass", return_value=key), \
                patch.object(BREVO, "check_connection", return_value=READY), \
                patch.object(BREVO.os, "replace", side_effect=publish), \
                patch.object(BREVO.os, "fsync", wraps=os.fsync) as sync:
            self.assertEqual(self.run_main()[0], 0)
        values, _ = BREVO.read_configuration(self.path)
        self.assertEqual(values["BREVO_API_KEY"], key)
        self.assertEqual(values["EMAIL_PROVIDER"], "brevo")
        for name in BREVO.SENDING_FLAGS:
            self.assertEqual(values[name], "0")
        for name in ("DATABASE_URL", "DJANGO_SECRET_KEY", "DEFAULT_FROM_EMAIL"):
            self.assertEqual(values[name], self.values[name])
        self.assertEqual(published_modes, [0o600])
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        sync.assert_called()
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_blocked_provider_still_persists_key_with_all_sending_disabled(self):
        with patch.object(BREVO.getpass, "getpass", return_value="PRIVATE-new-key"), \
                patch.object(BREVO, "check_connection", return_value=BLOCKED):
            code, output, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("ip_not_authorized", output + errors)
        values, _ = BREVO.read_configuration(self.path)
        self.assertEqual(values["BREVO_API_KEY"], "PRIVATE-new-key")
        self.assertTrue(all(values[name] == "0" for name in BREVO.SENDING_FLAGS))

    def test_check_never_prompts_or_rewrites_configuration(self):
        before = self.path.read_bytes()
        for diagnostic, expected_code in ((READY, 0), ({**READY, "reason": "PRIVATE provider body", "extra": "PRIVATE"}, 1)):
            with self.subTest(code=expected_code), patch.object(BREVO.getpass, "getpass") as prompt, \
                    patch.object(BREVO, "check_connection", return_value=diagnostic), \
                    patch.object(BREVO, "write_configuration") as write:
                self.assertEqual(self.run_main(["--check"])[0], expected_code)
            prompt.assert_not_called()
            write.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_enable_requires_ready_provider_and_keeps_event_and_sms_disabled(self):
        before = self.path.read_bytes()
        with patch.object(BREVO.getpass, "getpass") as prompt, \
                patch.object(BREVO, "check_connection", return_value=BLOCKED):
            self.assertEqual(self.run_main(["--enable-email"])[0], 1)
        prompt.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        for flag in ("EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED"):
            self.write_file({flag: "1"})
            before = self.path.read_bytes()
            with patch.object(BREVO, "check_connection", return_value=READY):
                self.assertEqual(self.run_main(["--enable-email"])[0], 1)
            self.assertEqual(self.path.read_bytes(), before)
        self.write_file()
        with patch.object(BREVO, "check_connection", return_value=READY):
            self.assertEqual(self.run_main(["--enable-email"])[0], 0)
        values, _ = BREVO.read_configuration(self.path)
        self.assertEqual(values["ALWAYSDATA_ENABLE_EMAILS"], "1")
        self.assertEqual(values["EMAIL_ENABLED"], "1")
        self.assertEqual(values["EVENT_NOTIFICATIONS_ENABLED"], "0")
        self.assertEqual(values["SMS_ENABLED"], "0")

    def test_invalid_keys_do_not_write_or_check_provider(self):
        for key in ("", "PRIVATE key", "PRIVATE\tkey", "PRIVATE\nkey", "PRIVATE\x00key", "x" * 4097):
            before = self.path.read_bytes()
            with self.subTest(key_length=len(key)), patch.object(BREVO.getpass, "getpass", return_value=key), \
                    patch.object(BREVO, "check_connection") as check:
                self.assertEqual(self.run_main()[0], 1)
            check.assert_not_called()
            self.assertEqual(self.path.read_bytes(), before)

    def test_input_failures_never_display_exception_values(self):
        for error in (EOFError("PRIVATE"), KeyboardInterrupt("PRIVATE"),
                      BREVO.getpass.GetPassWarning("PRIVATE"), PermissionError(13, "PRIVATE", "/PRIVATE/path")):
            before = self.path.read_bytes()
            with self.subTest(error_type=type(error).__name__), patch.object(BREVO.getpass, "getpass", side_effect=error):
                self.assertEqual(self.run_main()[0], 1)
            self.assertEqual(self.path.read_bytes(), before)

    def test_failed_atomic_publication_removes_partial_and_preserves_old_file(self):
        values, expected = BREVO.read_configuration(self.path)
        values["BREVO_API_KEY"] = "PRIVATE-new-key"
        before = self.path.read_bytes()
        with patch.object(BREVO.os, "replace", side_effect=PermissionError(13, "PRIVATE")):
            with self.assertRaises(BREVO.ConfigError):
                BREVO.write_configuration(self.path, values, expected)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_change_is_never_replaced(self):
        values, expected = BREVO.read_configuration(self.path)
        self.write_file({"BREVO_API_KEY": "PRIVATE-concurrent-key"})
        changed = self.path.read_bytes()
        with self.assertRaises(BREVO.ConfigError):
            BREVO.write_configuration(self.path, values, expected)
        self.assertEqual(self.path.read_bytes(), changed)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_connection_wrapper_requests_only_readonly_checks_without_sandbox(self):
        django = types.ModuleType("django")
        django.setup = Mock()
        connection = types.ModuleType("volunteers.brevo_connection")
        connection.check_brevo_connection = Mock(return_value=READY)
        with patch.dict(sys.modules, {"django": django, "volunteers.brevo_connection": connection}), \
                patch.dict(os.environ, {}, clear=True):
            diagnostic = BREVO.check_connection(dict(self.values))
        django.setup.assert_called_once_with()
        connection.check_brevo_connection.assert_called_once_with(sandbox=False)
        self.assertTrue(diagnostic["ready"])
