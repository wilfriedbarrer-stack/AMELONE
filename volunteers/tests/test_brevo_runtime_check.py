import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.test import SimpleTestCase


class BrevoRuntimeCheckTests(SimpleTestCase):
    def setUp(self):
        self.path = Path(settings.BASE_DIR) / "gunicorn.conf.py"
        spec = importlib.util.spec_from_file_location("brevo_runtime_config", self.path)
        self.config = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.config)
        self.server = SimpleNamespace(log=Mock())

    def test_default_start_never_calls_brevo(self):
        with patch.dict("os.environ", {"BREVO_VERIFY_ON_START": "0"}), patch.object(self.config.subprocess, "run") as run:
            self.config.when_ready(self.server)
        run.assert_not_called()
        self.server.log.warning.assert_not_called()

    def test_requested_check_runs_once_with_fixed_command_and_no_secret_arguments(self):
        with patch.dict("os.environ", {"BREVO_VERIFY_ON_START": "1"}), patch.object(
            self.config.subprocess, "run", return_value=SimpleNamespace(returncode=0),
        ) as run:
            self.config.when_ready(self.server)
        run.assert_called_once_with(
            [sys.executable, str(self.path.parent / "manage.py"), "check_brevo_connection", "--no-fail"],
            cwd=self.path.parent, timeout=25, check=False,
        )
        self.server.log.warning.assert_not_called()

    def test_check_timeout_and_execution_errors_leave_web_server_available_without_error_details(self):
        for error in (OSError("PRIVATE-ERROR"), subprocess.TimeoutExpired("PRIVATE-COMMAND", 25)):
            with self.subTest(kind=type(error).__name__), patch.dict("os.environ", {"BREVO_VERIFY_ON_START": "1"}), patch.object(
                self.config.subprocess, "run", side_effect=error,
            ):
                self.server.log.reset_mock()
                self.config.when_ready(self.server)
                self.server.log.warning.assert_called_once_with("BREVO_CONNECTION_CHECK runtime_probe_unavailable")

    def test_unexpected_command_failure_does_not_stop_web_server(self):
        with patch.dict("os.environ", {"BREVO_VERIFY_ON_START": "1"}), patch.object(
            self.config.subprocess, "run", return_value=SimpleNamespace(returncode=1),
        ):
            self.config.when_ready(self.server)
        self.server.log.warning.assert_called_once_with("BREVO_CONNECTION_CHECK runtime_probe_failed")
