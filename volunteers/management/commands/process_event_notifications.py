import json

from django.core.management.base import BaseCommand, CommandError

from volunteers.event_notifications import process_event_notifications


class Command(BaseCommand):
    help = "Traite les annonces et relances d’événements sans rejouer les envois acceptés."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Afficher les volumes sans écrire ni envoyer.")
        parser.add_argument("--limit", type=int, default=100, help="Nombre maximal de tentatives (1 à 10000).")

    def handle(self, *args, **options):
        try:
            result = process_event_notifications(dry_run=options["dry_run"], limit=options["limit"])
        except ValueError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(json.dumps(result, ensure_ascii=False, sort_keys=True))
