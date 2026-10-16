"""Contrôle ponctuel Brevo dans le contexte réseau du service web."""

import os
import subprocess
import sys
from pathlib import Path


def when_ready(server):
    if os.environ.get("BREVO_VERIFY_ON_START", "0") != "1":
        return
    root = Path(__file__).resolve().parent
    try:
        result = subprocess.run(
            [sys.executable, str(root / "manage.py"), "check_brevo_connection", "--no-fail"],
            cwd=root, timeout=25, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        server.log.warning("BREVO_CONNECTION_CHECK runtime_probe_unavailable")
        return
    if result.returncode:
        server.log.warning("BREVO_CONNECTION_CHECK runtime_probe_failed")
