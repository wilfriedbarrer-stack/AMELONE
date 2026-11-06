import importlib.util
import shlex
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / "deploy" / "alwaysdata" / "init_env.py"
SPEC = importlib.util.spec_from_file_location("alwaysdata_init_env", SOURCE)
INIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INIT)


class AlwaysdataEnvironmentFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private" / "amelone.env"
        self.connection = "postgresql://test:synthetic-secret@postgresql-amelone.alwaysdata.net/amelone_test"

    def test_configuration_is_private_and_all_sending_is_disabled(self):
        INIT.create_environment("amelone.alwaysdata.net", self.path, self.connection)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        values = {}
        for line in self.path.read_text().splitlines():
            if line.startswith("export "):
                tokens = shlex.split(line)
                key, value = tokens[1].split("=", 1)
                values[key] = value
        self.assertEqual(values["DATABASE_URL"], self.connection)
        self.assertGreaterEqual(len(values["DJANGO_SECRET_KEY"]), 64)
        for key in ("ALWAYSDATA_ENABLE_EMAILS", "EMAIL_ENABLED", "EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED"):
            self.assertEqual(values[key], "0")

    def test_existing_configuration_is_never_replaced(self):
        INIT.create_environment("amelone.alwaysdata.net", self.path, self.connection)
        before = self.path.read_bytes()
        with self.assertRaises(FileExistsError):
            INIT.create_environment("amelone.alwaysdata.net", self.path, self.connection)
        self.assertEqual(self.path.read_bytes(), before)

    def test_existing_dangling_symlink_cannot_redirect_private_configuration(self):
        self.path.parent.mkdir(mode=0o700)
        target = Path(self.directory.name) / "redirected.env"
        self.path.symlink_to(target)
        with self.assertRaises(FileExistsError):
            INIT.create_environment("amelone.alwaysdata.net", self.path, self.connection)
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(target.exists())

    def test_refuses_configuration_in_repository_or_with_invalid_database_url(self):
        inside = INIT.PROJECT_ROOT / "private-test.env"
        with self.assertRaises(ValueError):
            INIT.create_environment("amelone.alwaysdata.net", inside, self.connection)
        with self.assertRaises(ValueError) as error:
            INIT.create_environment("amelone.alwaysdata.net", self.path, "postgresql://PRIVATE:secret@host:BAD/db")
        self.assertNotIn("PRIVATE", str(error.exception))
        self.assertFalse(self.path.exists())

    def test_cli_never_displays_connection_or_generated_secret(self):
        with patch.object(INIT.getpass, "getpass", return_value=self.connection), patch.object(
            INIT.secrets, "token_urlsafe", return_value="PRIVATE-GENERATED-KEY",
        ), patch("sys.argv", ["init_env.py", "--host", "amelone.alwaysdata.net", "--output", str(self.path)]), patch(
            "builtins.print",
        ) as output:
            INIT.main()
        rendered = str(output.call_args_list)
        self.assertNotIn("synthetic-secret", rendered)
        self.assertNotIn("PRIVATE-GENERATED-KEY", rendered)


if __name__ == "__main__":
    unittest.main()
