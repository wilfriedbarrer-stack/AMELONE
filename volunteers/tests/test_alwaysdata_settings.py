"""Production configuration checks isolated from Django's active test settings."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SETTINGS_SCRIPT = """
import importlib
import json

settings = importlib.import_module("amelone.settings_alwaysdata")
names = (
    "DEBUG", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SITE_URL",
    "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT",
    "SECURE_PROXY_SSL_HEADER", "USE_X_FORWARDED_HOST", "SECURE_HSTS_SECONDS",
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", "SECURE_HSTS_PRELOAD",
    "EMAIL_ENABLED", "EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED", "EMAIL_BACKEND",
)
result = {name: getattr(settings, name) for name in names}
result["database_engine"] = settings.DATABASES["default"]["ENGINE"]
print(json.dumps(result))
"""


class AlwaysdataSettingsTests(unittest.TestCase):
    def run_settings(self, overrides=None, script=SETTINGS_SCRIPT):
        # Never inherit local credentials, a developer's settings module, or flags.
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "ALWAYSDATA_SITE_HOST": "preview.example.test",
            "DJANGO_SECRET_KEY": "synthetic-settings-test-key",
            "DJANGO_DEBUG": "1",
            "DATABASE_URL": "postgresql://test:test@database.example.test/test",
        }
        for name, value in (overrides or {}).items():
            if value is None:
                environment.pop(name, None)
            else:
                environment[name] = value
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=PROJECT_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )

    def settings(self, overrides=None):
        result = self.run_settings(overrides)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def assert_configuration_rejected(self, overrides, variable):
        result = self.run_settings(overrides)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn(variable, result.stderr)
        return result

    def test_requires_site_host_secret_and_postgresql_url_even_with_debug_default(self):
        for variable in ("ALWAYSDATA_SITE_HOST", "DJANGO_SECRET_KEY", "DATABASE_URL"):
            for value in (None, "", "   "):
                with self.subTest(variable=variable, value=value):
                    self.assert_configuration_rejected({variable: value}, variable)
        self.assert_configuration_rejected({"DJANGO_SECRET_KEY": None, "DJANGO_DEBUG": "0"}, "DJANGO_SECRET_KEY")

    def test_rejects_host_urls_wildcards_ips_and_multiple_hosts(self):
        for hostname in (
            "https://preview.example.test", "preview.example.test:443",
            "preview.example.test/path", "preview.example.test?query=1",
            "preview.example.test#fragment", "user@preview.example.test",
            "*.example.test", ".example.test", "localhost", "127.0.0.1", "[::1]",
            "first.example.test,second.example.test", "first.example.test second.example.test",
            " preview.example.test", "preview.example.test ", "preview.example.test.",
            "wrong_name.example.test", "-wrong.example.test", "wrong-.example.test",
            "préprod.example.test", f"{'a' * 64}.example.test",
            ".".join(["a" * 63] * 4),
        ):
            with self.subTest(hostname=hostname):
                self.assert_configuration_rejected({"ALWAYSDATA_SITE_HOST": hostname}, "ALWAYSDATA_SITE_HOST")

    def test_production_host_and_https_rules_override_render_and_development_flags(self):
        config = self.settings({
            "ALWAYSDATA_SITE_HOST": "Preview.Example.Test",
            "ALLOWED_HOSTS": "*,old.example.test",
            "CSRF_TRUSTED_ORIGINS": "http://old.example.test",
            "SITE_URL": "http://old.example.test",
            "RENDER_EXTERNAL_HOSTNAME": "old.onrender.com",
            "RENDER_EXTERNAL_URL": "https://old.onrender.com",
            "SESSION_COOKIE_SECURE": "0", "CSRF_COOKIE_SECURE": "0",
            "SECURE_SSL_REDIRECT": "0", "SECURE_HSTS_SECONDS": "31536000",
        })
        self.assertFalse(config["DEBUG"])
        self.assertEqual(config["ALLOWED_HOSTS"], ["preview.example.test"])
        self.assertEqual(config["CSRF_TRUSTED_ORIGINS"], ["https://preview.example.test"])
        self.assertEqual(config["SITE_URL"], "https://preview.example.test")
        for name in ("SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_SSL_REDIRECT"):
            self.assertTrue(config[name], name)
        self.assertEqual(config["SECURE_PROXY_SSL_HEADER"], ["HTTP_X_FORWARDED_PROTO", "https"])
        self.assertFalse(config["USE_X_FORWARDED_HOST"])
        self.assertEqual(config["SECURE_HSTS_SECONDS"], 3600)
        self.assertFalse(config["SECURE_HSTS_INCLUDE_SUBDOMAINS"])
        self.assertFalse(config["SECURE_HSTS_PRELOAD"])
        self.assertEqual(config["database_engine"], "django.db.backends.postgresql")

    def test_postgresql_aliases_are_supported_and_other_database_urls_fail_closed(self):
        for scheme in ("postgres", "postgresql", "pgsql"):
            with self.subTest(scheme=scheme):
                config = self.settings({"DATABASE_URL": f"{scheme}://user:password@db.example.test/app"})
                self.assertEqual(config["database_engine"], "django.db.backends.postgresql")
        for url in (
            "sqlite://:memory:", "mysql://user:password@db.example.test/app",
            "postgis://user:password@db.example.test/app", "postgresql://",
            "postgresql://db.example.test", "postgresql:///app",
            "postgresql://user:password@db.example.test:PRIVATE_PORT/app",
            "postgresql://user:PRIVATE_PASSWORD@[invalid/app",
            "postgresql://user:PRIVATE_PASSWORD@db.example.test/app#fragment",
        ):
            with self.subTest(url_kind=url.partition(":")[0]):
                result = self.assert_configuration_rejected({"DATABASE_URL": url}, "DATABASE_URL")
                self.assertNotIn("PRIVATE_PORT", result.stderr)
                self.assertNotIn("PRIVATE_PASSWORD", result.stderr)

    def test_shared_database_engine_is_checked_after_import(self):
        script = """
import sys
import types
base = types.ModuleType("amelone.settings")
base.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3"}}
sys.modules["amelone.settings"] = base
import amelone.settings_alwaysdata
"""
        result = self.run_settings(script=script)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ImproperlyConfigured", result.stderr)
        self.assertIn("PostgreSQL", result.stderr)

    def test_hsts_can_be_shortened_or_disabled_and_rejects_bad_values(self):
        for value in ("0", "600"):
            with self.subTest(value=value):
                self.assertEqual(self.settings({"ALWAYSDATA_HSTS_SECONDS": value})["SECURE_HSTS_SECONDS"], int(value))
        for value in ("-1", "1.5", "invalid", ""):
            with self.subTest(value=value):
                self.assert_configuration_rejected({"ALWAYSDATA_HSTS_SECONDS": value}, "ALWAYSDATA_HSTS_SECONDS")

    def test_restored_render_email_and_sms_flags_stay_silent_before_cutover(self):
        for cutover_flag in (None, "0", "true"):
            with self.subTest(cutover_flag=cutover_flag):
                config = self.settings({
                    "ALWAYSDATA_ENABLE_EMAILS": cutover_flag,
                    "EMAIL_ENABLED": "1", "EVENT_NOTIFICATIONS_ENABLED": "1", "SMS_ENABLED": "1",
                    "EMAIL_PROVIDER": "brevo", "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
                })
                self.assertFalse(config["EMAIL_ENABLED"])
                self.assertFalse(config["EVENT_NOTIFICATIONS_ENABLED"])
                self.assertFalse(config["SMS_ENABLED"])
                self.assertEqual(config["EMAIL_BACKEND"], "django.core.mail.backends.dummy.EmailBackend")

    def test_cutover_requires_both_email_flags_and_notifications_remain_opt_in(self):
        for email_flag in (None, "0", "true"):
            with self.subTest(email_flag=email_flag):
                config = self.settings({
                    "ALWAYSDATA_ENABLE_EMAILS": "1", "EMAIL_ENABLED": email_flag,
                    "EVENT_NOTIFICATIONS_ENABLED": "1",
                })
                self.assertFalse(config["EMAIL_ENABLED"])
                self.assertFalse(config["EVENT_NOTIFICATIONS_ENABLED"])
                self.assertEqual(config["EMAIL_BACKEND"], "django.core.mail.backends.dummy.EmailBackend")
        config = self.settings({"ALWAYSDATA_ENABLE_EMAILS": "1", "EMAIL_ENABLED": "1"})
        self.assertTrue(config["EMAIL_ENABLED"])
        self.assertFalse(config["EVENT_NOTIFICATIONS_ENABLED"])

    def test_explicit_cutover_enables_brevo_or_smtp_and_never_sms(self):
        for provider, backend in (
            ("brevo", "volunteers.notification_transports.BrevoEmailBackend"),
            ("django", "django.core.mail.backends.smtp.EmailBackend"),
        ):
            with self.subTest(provider=provider):
                config = self.settings({
                    "ALWAYSDATA_ENABLE_EMAILS": "1", "EMAIL_ENABLED": "1",
                    "EVENT_NOTIFICATIONS_ENABLED": "1", "SMS_ENABLED": "1",
                    "EMAIL_PROVIDER": provider, "EMAIL_BACKEND": "django.core.mail.backends.console.EmailBackend",
                })
                self.assertTrue(config["EMAIL_ENABLED"])
                self.assertTrue(config["EVENT_NOTIFICATIONS_ENABLED"])
                self.assertFalse(config["SMS_ENABLED"])
                self.assertEqual(config["EMAIL_BACKEND"], backend)

    def test_wsgi_forces_alwaysdata_settings_and_adds_project_root_without_database_access(self):
        script = """
import json
import os
from pathlib import Path
import runpy
import sys
from unittest.mock import patch

root = Path.cwd()
sys.path = [item for item in sys.path if item and Path(item).resolve() != root]
os.environ["DJANGO_SETTINGS_MODULE"] = "amelone.settings"
with patch("django.db.backends.base.base.BaseDatabaseWrapper.connect", side_effect=AssertionError("No database during WSGI setup")):
    namespace = runpy.run_path(str(root / "deploy/alwaysdata/amelone.wsgi.py"))
from django.conf import settings
print(json.dumps({
    "module": os.environ["DJANGO_SETTINGS_MODULE"],
    "application": callable(namespace["application"]),
    "project_path": sys.path[0] == str(root),
    "debug": settings.DEBUG,
}))
"""
        result = self.run_settings(script=script)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {
            "module": "amelone.settings_alwaysdata", "application": True,
            "project_path": True, "debug": False,
        })
