import os

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Crée de façon idempotente le premier organisateur depuis les variables d'environnement."

    @transaction.atomic
    def handle(self, *args, **options):
        email = os.environ.get("INITIAL_ADMIN_EMAIL", "").strip().lower()
        password = os.environ.get("INITIAL_ADMIN_PASSWORD", "")

        if not email and not password:
            self.stdout.write(
                self.style.WARNING(
                    "Création de l'organisateur ignorée : variables INITIAL_ADMIN_* absentes."
                )
            )
            return
        if not email or not password:
            raise CommandError(
                "INITIAL_ADMIN_EMAIL et INITIAL_ADMIN_PASSWORD doivent être définies ensemble."
            )

        User = get_user_model()
        existing = User.objects.select_for_update().filter(email=email).first()
        if existing:
            if not (existing.is_superuser and existing.is_staff and existing.is_organizer):
                raise CommandError(
                    "Cette adresse existe déjà sans être un compte organisateur complet."
                )
            self.stdout.write(self.style.SUCCESS("Le compte organisateur existe déjà."))
            return

        candidate = User(email=email)
        try:
            validate_password(password, user=candidate)
        except ValidationError as exc:
            raise CommandError("Mot de passe organisateur refusé : " + " ".join(exc.messages)) from exc

        User.objects.create_superuser(email=email, password=password)
        self.stdout.write(self.style.SUCCESS("Compte organisateur initial créé."))
