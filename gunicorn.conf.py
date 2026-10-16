"""Contrôle ponctuel Brevo dans le contexte réseau du service web."""

import os
import subprocess
import sys
from pathlib import Path


def when_ready(server):
    sandbox = os.environ.get("BREVO_VERIFY_SANDBOX_ON_START", "0") == "1"
    if os.environ.get("BREVO_VERIFY_ON_START", "0") != "1" and not sandbox:
        return
    root = Path(__file__).resolve().parent
    command = [sys.executable, str(root / "manage.py"), "check_brevo_connection", "--no-fail"]
    if sandbox:
        command.append("--sandbox")
    try:
        result = subprocess.run(
            command, cwd=root, timeout=40 if sandbox else 25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        server.log.warning("BREVO_CONNECTION_CHECK runtime_probe_unavailable")
        return
    if result.returncode:
        server.log.warning("BREVO_CONNECTION_CHECK runtime_probe_failed")
