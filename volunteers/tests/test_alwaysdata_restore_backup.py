import contextlib
import hashlib
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
from urllib.parse import quote

SOURCE = Path(__file__).resolve().parents[2] / "deploy/alwaysdata/restore_backup.py"
SPEC = importlib.util.spec_from_file_location("alwaysdata_restore_backup", SOURCE)
RESTORE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RESTORE)


class AlwaysdataRestoreBackupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.directory.name)
        self.dump = self.root / "source.dump"
        self.checksum = self.root / "source.dump.sha256"
        self.env_file = self.root / "amelone.env"
        self.password = "PRIVATE:pass\\word"
        self.connection = (
            f"postgresql://amelone:{quote(self.password, safe='')}"
            "@postgresql-amelone.alwaysdata.net:5432/amelone_prod?sslmode=require"
        )
        self.calls = []
        self.password_files = []
        self.preflight_output = b"180006\n0\n"
        self.restore_code = 0
        self.write_files()

    def write_files(self, overrides=None):
        for path in (self.dump, self.checksum):
            path.unlink(missing_ok=True)
        self.dump.write_bytes(b"synthetic custom-format backup")
        self.dump.chmod(0o600)
        digest = hashlib.sha256(self.dump.read_bytes()).hexdigest()
        self.checksum.write_text(f"{digest}  {self.dump.name}\n")
        self.checksum.chmod(0o600)
        values = {"DATABASE_URL": self.connection, "ALWAYSDATA_ENABLE_EMAILS": "0",
                  "EMAIL_ENABLED": "0", "EVENT_NOTIFICATIONS_ENABLED": "0", "SMS_ENABLED": "0"}
        values.update(overrides or {})
        self.env_file.write_text("".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items()))
        self.env_file.chmod(0o600)

    def tool(self, arguments, **kwargs):
        self.calls.append((arguments, kwargs))
        environment = kwargs["env"]
        self.assertFalse(any("PRIVATE" in argument for argument in arguments))
        self.assertFalse(any("PRIVATE" in value for value in environment.values()))
        for key in ("PGPASSWORD", "PGSERVICE", "PGSERVICEFILE", "DATABASE_URL"):
            self.assertNotIn(key, environment)
        if "--version" in arguments:
            return subprocess.CompletedProcess(arguments, 0, b"pg_restore (PostgreSQL) 18.6\n", b"PRIVATE")
        self.assertEqual(environment["PGHOST"], "postgresql-amelone.alwaysdata.net")
        self.assertEqual(environment["PGDATABASE"], "amelone_prod")
        self.assertEqual(environment["PGUSER"], "amelone")
        self.assertEqual(environment["PGPORT"], "5432")
        self.assertEqual(environment["PGSSLMODE"], "require")
        self.assertEqual(environment["PGCONNECT_TIMEOUT"], "20")
        password_file = Path(environment["PGPASSFILE"])
        self.password_files.append(password_file)
        self.assertEqual(stat.S_IMODE(password_file.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(password_file.parent.stat().st_mode), 0o700)
        self.assertEqual(password_file.read_text(), "postgresql-amelone.alwaysdata.net:5432:amelone_prod:amelone:PRIVATE\\:pass\\\\word\n")
        if arguments[0] == "pg_restore":
            self.assertEqual(kwargs["timeout"], 300)
            return subprocess.CompletedProcess(arguments, self.restore_code, b"PRIVATE raw output", b"PRIVATE raw error")
        output = self.preflight_output if any("server_version_num" in argument for argument in arguments) else b"40\n50\n"
        return subprocess.CompletedProcess(arguments, 0, output, b"PRIVATE raw error")

    def run_main(self):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = RESTORE.main(["--dump", str(self.dump), "--env-file", str(self.env_file)])
        self.assertNotIn("PRIVATE", output.getvalue() + errors.getvalue())
        return code, output.getvalue(), errors.getvalue()

    def test_private_verified_backup_restores_once_in_transaction_without_password_arguments(self):
        before = self.dump.read_bytes()
        inherited = {"PGPASSWORD": "PRIVATE inherited", "PGSERVICE": "bad", "PGSERVICEFILE": "bad", "DATABASE_URL": "PRIVATE inherited"}
        with patch.dict(os.environ, inherited), patch.object(RESTORE.subprocess, "run", side_effect=self.tool):
            code, output, errors = self.run_main()
        self.assertEqual(code, 0, errors)
        self.assertIn("40", output)
        self.assertIn("50", output)
        restore_calls = [arguments for arguments, _ in self.calls if arguments[0] == "pg_restore" and "--version" not in arguments]
        self.assertEqual(len(restore_calls), 1)
        for flag in ("--dbname=amelone_prod", "--no-owner", "--no-privileges", "--single-transaction", "--exit-on-error", "--no-password"):
            self.assertIn(flag, restore_calls[0])
        self.assertNotIn("--clean", restore_calls[0])
        self.assertNotIn("--create", restore_calls[0])
        self.assertEqual(self.dump.read_bytes(), before)
        self.assertTrue(self.checksum.exists())
        self.assertFalse(any(path.exists() for path in self.password_files))

    def test_other_targets_and_insecure_url_are_rejected_before_running_tools(self):
        for connection in (
            self.connection.replace("postgresql-amelone.alwaysdata.net", "source-postgres.render.com"),
            self.connection.replace("/amelone_prod", "/other_database"),
            self.connection.replace("//amelone:", "//other_user:"),
            self.connection.replace(":5432/", ":9999/"),
            self.connection.replace("sslmode=require", "sslmode=disable"),
        ):
            self.write_files({"DATABASE_URL": connection})
            with self.subTest(target_kind=connection.partition(":")[0]), patch.object(RESTORE.subprocess, "run") as tool:
                self.assertEqual(self.run_main()[0], 1)
            tool.assert_not_called()

    def test_enabled_sending_flags_are_rejected_before_restore(self):
        for key in ("ALWAYSDATA_ENABLE_EMAILS", "EMAIL_ENABLED", "EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED"):
            self.write_files({key: "1"})
            with self.subTest(flag=key), patch.object(RESTORE.subprocess, "run") as tool:
                self.assertEqual(self.run_main()[0], 1)
            tool.assert_not_called()

    def test_corrupt_checksum_or_wrong_checksum_filename_is_rejected(self):
        for checksum in (f"{'0' * 64}  {self.dump.name}\n", f"{hashlib.sha256(self.dump.read_bytes()).hexdigest()}  wrong.dump\n"):
            self.checksum.write_text(checksum)
            with patch.object(RESTORE.subprocess, "run") as tool:
                self.assertEqual(self.run_main()[0], 1)
            tool.assert_not_called()

    def test_missing_public_and_symlink_files_are_rejected(self):
        for kind in ("missing_dump", "public_dump", "public_checksum", "symlink_checksum", "public_env"):
            self.write_files()
            if kind == "missing_dump":
                self.dump.unlink()
            elif kind == "public_dump":
                self.dump.chmod(0o644)
            elif kind == "public_checksum":
                self.checksum.chmod(0o644)
            elif kind == "symlink_checksum":
                self.checksum.unlink()
                self.checksum.symlink_to(self.dump)
            else:
                self.env_file.chmod(0o644)
            with self.subTest(kind=kind), patch.object(RESTORE.subprocess, "run") as tool:
                self.assertEqual(self.run_main()[0], 1)
            tool.assert_not_called()

    def test_wrong_owner_is_rejected_without_database_access(self):
        owner = os.geteuid()
        with patch.object(RESTORE.os, "geteuid", return_value=owner + 1), patch.object(RESTORE.subprocess, "run") as tool:
            self.assertEqual(self.run_main()[0], 1)
        tool.assert_not_called()

    def test_nonempty_target_is_rejected_without_restore(self):
        for preflight_output in (b"180006\n1\n", b"170009\n0\n"):
            self.preflight_output = preflight_output
            self.calls.clear()
            with self.subTest(preflight=preflight_output), patch.object(RESTORE.subprocess, "run", side_effect=self.tool):
                self.assertEqual(self.run_main()[0], 1)
            self.assertFalse(any(arguments[0] == "pg_restore" and "--version" not in arguments for arguments, _ in self.calls))

    def test_older_restore_client_is_rejected_before_target_access(self):
        result = subprocess.CompletedProcess([], 0, b"pg_restore (PostgreSQL) 17.8\n", b"PRIVATE raw error")
        with patch.object(RESTORE.subprocess, "run", return_value=result) as tool:
            self.assertEqual(self.run_main()[0], 1)
        tool.assert_called_once()
        self.assertEqual(tool.call_args.args[0], ["pg_restore", "--version"])

    def test_dump_inside_repository_or_webroot_is_rejected_before_tools(self):
        for attribute in ("PROJECT_ROOT", "PUBLIC_ROOT"):
            with self.subTest(root=attribute), patch.object(RESTORE, attribute, self.root), \
                    patch.object(RESTORE.subprocess, "run") as tool:
                self.assertEqual(self.run_main()[0], 1)
            tool.assert_not_called()

    def test_restore_failure_hides_raw_output_and_skips_final_check(self):
        self.restore_code = 7
        before = self.dump.read_bytes()
        with patch.object(RESTORE.subprocess, "run", side_effect=self.tool):
            code, _, errors = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("7", errors)
        self.assertEqual(sum(arguments[0] == "psql" for arguments, _ in self.calls), 1)
        self.assertEqual(self.dump.read_bytes(), before)
        self.assertFalse(any(path.exists() for path in self.password_files))
