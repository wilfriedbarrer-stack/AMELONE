"""WSGI entry point selected by the Alwaysdata Python site configuration."""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deploy.alwaysdata.private_env import load_private_environment  # noqa: E402

load_private_environment()
os.environ["DJANGO_SETTINGS_MODULE"] = "amelone.settings_alwaysdata"

from django.core.wsgi import get_wsgi_application  # noqa: E402

application = get_wsgi_application()
