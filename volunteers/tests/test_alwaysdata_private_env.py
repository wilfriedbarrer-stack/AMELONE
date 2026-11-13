import importlib.util
import os
import runpy
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from deploy.alwaysdata import private_env

ROOT = Path(__file__).resolve().parents[2]


class AlwaysdataPrivateEnvTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "amelone.env"
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def write(self, content):
        self.path.write_text(content, encoding="utf-8")
        self.path.chmod(0o600)

    def test_generator_roundtrip_and_authoritative_file_values(self):
        spec = importlib.util.spec_from_file_location("private_env_test_generator", ROOT / "deploy/alwaysdata/init_env.py")
        generator = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(generator)
        connection = "postgresql://amelone:synthetic@localhost/amelone_prod"
        generator.create_environment("amelone.alwaysdata.net", self.path, connection)
        os.environ["DATABASE_URL"] = "postgresql://old@localhost/old"
        private_env.load_private_environment(self.path)
        self.assertEqual(os.environ["DATABASE_URL"], connection)
        self.assertEqual(os.environ["EMAIL_ENABLED"], "0")

    def test_quotes_and_shell_characters_are_literal_and_never_executed(self):
        sentinel = Path(self.directory.name) / "must-not-exist"
        value = f"a'b $HOME $(touch {sentinel}) `touch {sentinel}` ; café"
        self.write(f"# Comment\n\nexport DJANGO_SECRET_KEY={shlex.quote(value)}\n")
        private_env.load_private_environment(self.path)
        self.assertEqual(os.environ["DJANGO_SECRET_KEY"], value)
        self.assertFalse(sentinel.exists())

    def test_group_and_other_permissions_are_rejected(self):
        self.write("export EMAIL_ENABLED=0\n")
        for mode in (0o640, 0o604, 0o666):
            with self.subTest(mode=mode):
                self.path.chmod(mode)
                with self.assertRaisesRegex(ValueError, private_env.ERROR_MESSAGE):
                    private_env.load_private_environment(self.path)

    def test_manual_single_quoted_flags_match_documented_operations(self):
        self.write("export EMAIL_ENABLED='1'\nexport EVENT_NOTIFICATIONS_ENABLED='0'\n")
        private_env.load_private_environment(self.path)
        self.assertEqual(os.environ["EMAIL_ENABLED"], "1")
        self.assertEqual(os.environ["EVENT_NOTIFICATIONS_ENABLED"], "0")

    def test_wrong_owner_is_rejected(self):
        self.write("export EMAIL_ENABLED=0\n")
        with patch.object(private_env.os, "geteuid", return_value=self.path.stat().st_uid + 1):
            with self.assertRaises(ValueError):
                private_env.load_private_environment(self.path)

    def test_symlinks_regular_files_and_relative_paths(self):
        self.write("export EMAIL_ENABLED=0\n")
        link = Path(self.directory.name) / "link.env"
        link.symlink_to(self.path)
        for path in (link, Path(self.directory.name), Path("relative.env")):
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    private_env.load_private_environment(path)

    def test_repository_and_public_web_paths_are_rejected(self):
        self.write("export EMAIL_ENABLED=0\n")
        for root_name in ("PROJECT_ROOT", "PUBLIC_ROOT"):
            with self.subTest(root_name=root_name), patch.object(private_env, root_name, self.path.parent):
                with self.assertRaises(ValueError):
                    private_env.load_private_environment(self.path)

    def test_malformed_duplicate_unknown_and_shell_syntax_fail_atomically(self):
        bad_lines = (
            "export PATH=private-secret", "export EMAIL_ENABLED=1", "EMAIL_PROVIDER=brevo",
            "export EMAIL_PROVIDER='unclosed", "export EMAIL_PROVIDER=brevo extra",
            "export EMAIL_PROVIDER=$(private-secret)", "export EMAIL_PROVIDER=brevo;private-secret",
            "export EMAIL_PROVIDER='nul\x00private-secret'",
        )
        for bad in bad_lines:
            with self.subTest(bad=bad):
                self.write(f"export EMAIL_ENABLED=0\n{bad}\n")
                with self.assertRaises(ValueError) as raised:
                    private_env.load_private_environment(self.path)
                self.assertEqual(str(raised.exception), private_env.ERROR_MESSAGE)
                self.assertNotIn("EMAIL_ENABLED", os.environ)

    def test_size_limit_and_invalid_utf8(self):
        for payload in (b"#" * (private_env.MAX_SIZE + 1), b"#\xff"):
            self.path.write_bytes(payload)
            self.path.chmod(0o600)
            with self.assertRaises(ValueError):
                private_env.load_private_environment(self.path)

    def test_absent_file_setting_preserves_environment(self):
        os.environ["EMAIL_ENABLED"] = "1"
        private_env.load_private_environment()
        self.assertEqual(dict(os.environ), {"EMAIL_ENABLED": "1"})

    def test_wsgi_loads_private_file_before_django_and_forces_settings_module(self):
        self.write("export DJANGO_SETTINGS_MODULE=unexpected.module\nexport EMAIL_ENABLED=0\n")
        os.environ["ALWAYSDATA_ENV_FILE"] = str(self.path)

        def get_application():
            self.assertEqual(os.environ["DJANGO_SETTINGS_MODULE"], "amelone.settings_alwaysdata")
            self.assertEqual(os.environ["EMAIL_ENABLED"], "0")
            return "synthetic-application"

        with patch("django.core.wsgi.get_wsgi_application", side_effect=get_application):
            result = runpy.run_path(str(ROOT / "deploy/alwaysdata/amelone.wsgi.py"))
        self.assertEqual(result["application"], "synthetic-application")
