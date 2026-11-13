import contextlib
import hashlib
import importlib.util
import io
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

MODULE_PATH = Path(__file__).resolve().parents[2] / "deploy/alwaysdata/backup_render.py"
SPEC = importlib.util.spec_from_file_location("alwaysdata_render_backup", MODULE_PATH)
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)
SOURCE_URL = f"postgresql://account:PRIVATE%3Apass%5Cword@{backup.SOURCE_HOST}/source_db"


class RenderBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment_patch = patch.dict(os.environ, {}, clear=True)
        environment_patch.start()
        self.addCleanup(environment_patch.stop)
        self.output = Path(self.directory.name) / "backups"
        self.calls = []
        self.password_files = []

    def tool(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "DATABASE_URL"):
            self.assertNotIn(key, kwargs["env"])
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, b"pg_dump (PostgreSQL) 18.6\n", b"")
        environment = kwargs["env"]
        password_file = Path(environment["PGPASSFILE"])
        self.password_files.append(password_file)
        self.assertEqual(stat.S_IMODE(password_file.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(password_file.stat().st_mode), 0o600)
        self.assertEqual(password_file.read_text(), f"{backup.SOURCE_HOST}:5432:source_db:account:PRIVATE\\:pass\\\\word\n")
        self.assertEqual(environment["PGSSLMODE"], "require")
        self.assertNotIn("PGPASSWORD", environment)
        self.assertNotIn("PGSERVICE", environment)
        self.assertNotIn("PGSERVICEFILE", environment)
        self.assertNotIn("DATABASE_URL", environment)
        self.assertFalse(any("PRIVATE" in argument for argument in arguments))
        self.assertFalse(any("PRIVATE" in value for value in environment.values()))
        if arguments[0] == "pg_dump":
            path = Path(arguments[-1])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            path.write_bytes(b"synthetic custom dump")
        return subprocess.CompletedProcess(arguments, 0, b"PRIVATE raw tool output", b"PRIVATE raw error")

    def test_masked_prompt_private_dump_checksum_and_sanitized_environment(self):
        output, errors = io.StringIO(), io.StringIO()
        inherited = {"PGPASSWORD": "PRIVATE inherited", "PGSERVICE": "bad", "PGSERVICEFILE": "bad", "DATABASE_URL": "PRIVATE inherited"}
        with patch.dict(os.environ, inherited), patch.object(backup.subprocess, "run", side_effect=self.tool), \
                patch.object(backup.getpass, "getpass", return_value=SOURCE_URL) as prompt, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(backup.main(["--output-dir", str(self.output)]), 0)
        prompt.assert_called_once()
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), 0o700)
        dump = next(self.output.glob("*.dump"))
        checksum = next(self.output.glob("*.sha256"))
        for path in (dump, checksum):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(checksum.read_text(), f"{hashlib.sha256(dump.read_bytes()).hexdigest()}  {dump.name}\n")
        self.assertFalse(any(path.exists() for path in self.password_files))
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        self.assertEqual([call[0][0] for call in self.calls], ["pg_dump", "pg_restore", "pg_dump", "pg_restore"])

    def test_rejects_other_sources_incomplete_credentials_and_newline_injection(self):
        for url in (
            SOURCE_URL.replace(backup.SOURCE_HOST, "attacker.example.test"),
            SOURCE_URL.replace("postgresql:", "mysql:"),
            f"postgresql://account@{backup.SOURCE_HOST}/source_db",
            f"postgresql://account:password@{backup.SOURCE_HOST}/",
            SOURCE_URL.replace("/source_db", "/source_db%0Ainjection"),
            SOURCE_URL.replace("PRIVATE%3Apass%5Cword", "secret%0D%0Aline"),
            SOURCE_URL.replace("PRIVATE%3Apass%5Cword", "secret\nline"),
            SOURCE_URL.replace(backup.SOURCE_HOST, backup.SOURCE_HOST + ":1234"),
            SOURCE_URL.replace(backup.SOURCE_HOST, backup.SOURCE_HOST + ":0"),
        ):
            with self.subTest(url_kind=url.partition(":")[0]), self.assertRaises(backup.BackupError):
                backup.parse_source(url)

    def test_failed_dump_removes_partials_and_never_displays_raw_errors(self):
        def fail_dump(arguments, **kwargs):
            result = self.tool(arguments, **kwargs)
            if arguments[0] == "pg_dump" and "--version" not in arguments:
                result.returncode = 7
            return result
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(backup.subprocess, "run", side_effect=fail_dump), \
                patch.object(backup.getpass, "getpass", return_value=SOURCE_URL), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(backup.main(["--output-dir", str(self.output)]), 1)
        self.assertIn("code 7", errors.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(path.exists() for path in self.password_files))

    def test_existing_destination_is_never_overwritten(self):
        with patch.object(backup.subprocess, "run", side_effect=self.tool), \
                patch.object(backup.secrets, "token_hex", return_value="fixed"), \
                patch.object(backup, "datetime") as clock:
            clock.now.return_value.strftime.return_value = "fixed"
            first, checksum = backup.create_backup(SOURCE_URL, self.output)
            first.write_bytes(b"existing backup preserved")
            with self.assertRaises(backup.BackupError):
                backup.create_backup(SOURCE_URL, self.output)
        self.assertEqual(first.read_bytes(), b"existing backup preserved")
        self.assertTrue(checksum.exists())

    def test_failed_archive_verification_removes_dump_and_password_file(self):
        def fail_list(arguments, **kwargs):
            result = self.tool(arguments, **kwargs)
            if "--list" in arguments:
                result.returncode = 9
            return result
        with patch.object(backup.subprocess, "run", side_effect=fail_list):
            with self.assertRaisesRegex(backup.BackupError, "code 9"):
                backup.create_backup(SOURCE_URL, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        self.assertFalse(any(path.exists() for path in self.password_files))

    def test_publication_race_preserves_existing_checksum_and_removes_new_dump(self):
        real_link = os.link
        def competing_writer(source, destination):
            if destination.suffix == ".sha256":
                destination.write_bytes(b"existing checksum preserved")
            real_link(source, destination)
        with patch.object(backup.subprocess, "run", side_effect=self.tool), \
                patch.object(backup.os, "link", side_effect=competing_writer):
            with self.assertRaises(FileExistsError):
                backup.create_backup(SOURCE_URL, self.output)
        remaining = list(self.output.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].read_bytes(), b"existing checksum preserved")

    def test_rejects_repository_relative_and_public_output_directories(self):
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        for path in ("relative", backup.PROJECT_ROOT / "private-backup", self.output,
                     "/home/amelone/www", "/home/amelone/www/backups"):
            with self.subTest(path=str(path)), self.assertRaises(backup.BackupError):
                backup.private_output_dir(path)

    def test_wrong_client_major_fails_before_dump(self):
        with patch.object(backup.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"pg_dump (PostgreSQL) 17.8", b"PRIVATE")):
            with self.assertRaisesRegex(backup.BackupError, "majeure 18"):
                backup.create_backup(SOURCE_URL, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
