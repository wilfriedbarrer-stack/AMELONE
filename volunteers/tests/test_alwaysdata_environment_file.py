import contextlib
import importlib.util
import io
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

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
        with self.assertRaises(ValueError):
            INIT.create_environment("amelone.alwaysdata.net", "/home/amelone/www/private-test.env", self.connection)
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

    def database_arguments(self):
        return ["--host", "amelone.alwaysdata.net", "--output", str(self.path),
                "--db-host", "postgresql-amelone.alwaysdata.net", "--db-name", "amelone_prod", "--db-user", "amelone"]

    def test_database_fields_prompt_only_password_and_encode_credentials(self):
        password = "PRIVATE:/\\@ ?%é"
        with patch.object(INIT.getpass, "getpass", return_value=password) as prompt, \
                patch.object(INIT, "create_environment", return_value=self.path) as create, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(INIT.main(self.database_arguments()), 0)
        self.assertIn("Mot de passe", prompt.call_args.args[0])
        connection = urlsplit(create.call_args.args[2])
        self.assertEqual(unquote(connection.password), password)
        self.assertEqual(connection.username, "amelone")
        self.assertEqual(connection.hostname, "postgresql-amelone.alwaysdata.net")
        self.assertEqual(connection.path, "/amelone_prod")
        self.assertEqual(connection.port, 5432)
        self.assertEqual(parse_qs(connection.query), {"sslmode": ["require"]})

    def test_partial_database_fields_and_check_without_fields_fail_before_prompt(self):
        base = ["--host", "amelone.alwaysdata.net", "--output", str(self.path)]
        for fields in (["--db-host", "postgresql-amelone.alwaysdata.net"], ["--check-db"]):
            with self.subTest(fields=fields), patch.object(INIT.getpass, "getpass") as prompt, \
                    contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as result:
                INIT.main(base + fields)
            self.assertEqual(result.exception.code, 2)
            prompt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_readonly_target_check_accepts_only_empty_postgresql18_before_writing(self):
        password = "PRIVATE:pass\\word"
        password_files = []
        def psql(arguments, **kwargs):
            self.assertEqual(arguments[0], "psql")
            self.assertEqual(arguments.count("-c"), 2)
            self.assertTrue(all(arguments[index + 1].startswith("SELECT ") for index, argument in enumerate(arguments) if argument == "-c"))
            self.assertFalse(any("PRIVATE" in argument for argument in arguments))
            environment = kwargs["env"]
            self.assertFalse(any("PRIVATE" in value for value in environment.values()))
            for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "DATABASE_URL"):
                self.assertNotIn(key, environment)
            self.assertEqual(environment["PGSSLMODE"], "require")
            self.assertEqual(environment["PGCONNECT_TIMEOUT"], "20")
            self.assertEqual(kwargs["timeout"], 60)
            password_file = Path(environment["PGPASSFILE"])
            password_files.append(password_file)
            self.assertEqual(stat.S_IMODE(password_file.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(password_file.parent.stat().st_mode), 0o700)
            self.assertEqual(password_file.read_text(), "postgresql-amelone.alwaysdata.net:5432:amelone_prod:amelone:PRIVATE\\:pass\\\\word\n")
            self.assertFalse(self.path.exists())
            return subprocess.CompletedProcess(arguments, 0, b"180006\n0\n", b"PRIVATE raw output")
        output, errors = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, {"PGPASSWORD": "PRIVATE inherited", "DATABASE_URL": "PRIVATE inherited"}, clear=True), \
                patch.object(INIT.getpass, "getpass", return_value=password), \
                patch.object(INIT.subprocess, "run", side_effect=psql), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(INIT.main(self.database_arguments() + ["--check-db"]), 0)
        self.assertTrue(self.path.exists())
        self.assertFalse(any(path.exists() for path in password_files))
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())

    def test_target17_nonempty_and_invalid_output_never_write_configuration(self):
        for stdout, diagnostic in ((b"170009\n0\n", "PostgreSQL 18 requis"),
                                   (b"180006\n1\n", "doit être vide"), (b"PRIVATE\n", "deux nombres")):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(kind=diagnostic), patch.object(INIT.getpass, "getpass", return_value="PRIVATE"), \
                    patch.object(INIT.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout, b"PRIVATE")), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(INIT.main(self.database_arguments() + ["--check-db"]), 1)
            self.assertFalse(self.path.exists())
            self.assertIn(diagnostic, errors.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())

    def test_safe_input_errors_never_display_exception_text(self):
        for exception, diagnostic in ((EOFError("PRIVATE"), "EOF"),
                                      (KeyboardInterrupt("PRIVATE"), "Saisie annulée"),
                                      (INIT.getpass.GetPassWarning("PRIVATE"), "Saisie masquée indisponible"),
                                      (PermissionError(13, "PRIVATE"), "PermissionError, errno=13")):
            output, errors = io.StringIO(), io.StringIO()
            with self.subTest(error_type=type(exception).__name__), \
                    patch.object(INIT.getpass, "getpass", side_effect=exception), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(INIT.main(self.database_arguments()), 1)
            self.assertIn(diagnostic, errors.getvalue())
            self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())


if __name__ == "__main__":
    unittest.main()
