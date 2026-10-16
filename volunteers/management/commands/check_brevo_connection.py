import json

from django.core.management.base import BaseCommand, CommandError

from volunteers.brevo_connection import check_brevo_connection


class Command(BaseCommand):
    help = "Vérifie Brevo et l’expéditeur configuré sans envoyer ni modifier de données."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-fail", action="store_true",
            help="Afficher le diagnostic sans faire échouer la commande si Brevo n’est pas prêt.",
        )

    def handle(self, *args, **options):
        result = check_brevo_connection()
        self.stdout.write("BREVO_CONNECTION_CHECK " + json.dumps(result, sort_keys=True))
        if not result["ready"] and not options["no_fail"]:
            raise CommandError("BREVO_CONNECTION_CHECK_NOT_READY")
