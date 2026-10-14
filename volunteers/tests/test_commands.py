from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase


class BootstrapAdminCommandTests(TestCase):
    def test_creates_admin_once_without_printing_password(self):
        environment = {
            "INITIAL_ADMIN_EMAIL": "Direction@Example.org",
            "INITIAL_ADMIN_PASSWORD": "Une-phrase-secrete-2026!",
        }
        first_output = StringIO()
        with patch.dict("os.environ", environment, clear=False):
            call_command("bootstrap_admin", stdout=first_output)
            call_command("bootstrap_admin", stdout=StringIO())

        user = get_user_model().objects.get(email="direction@example.org")
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_organizer)
        self.assertTrue(user.check_password(environment["INITIAL_ADMIN_PASSWORD"]))
        self.assertNotIn(environment["INITIAL_ADMIN_PASSWORD"], first_output.getvalue())
        self.assertEqual(get_user_model().objects.count(), 1)

    def test_rejects_incomplete_configuration(self):
        with patch.dict(
            "os.environ",
            {"INITIAL_ADMIN_EMAIL": "direction@example.org", "INITIAL_ADMIN_PASSWORD": ""},
            clear=False,
        ):
            with self.assertRaises(CommandError):
                call_command("bootstrap_admin", stdout=StringIO())
