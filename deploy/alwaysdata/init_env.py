"""Create a private staging configuration without displaying credentials."""

import argparse
import getpass
import os
import re
import secrets
import shlex
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def create_environment(host, output, database_url):
    host = host.lower()
    label = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    if (
        len(host) > 253 or not re.fullmatch(rf"{label}(?:\.{label})+", host)
        or not any(character.isalpha() for character in host.rsplit(".", 1)[-1])
    ):
        raise ValueError("Le nom d’hôte doit être complet, sans URL ni port.")
    output = Path(output)
    if not output.is_absolute():
        raise ValueError("Le fichier privé doit avoir un chemin absolu.")
    if output.is_symlink():
        raise FileExistsError("Le chemin de configuration existe déjà.")
    if output.resolve().is_relative_to(PROJECT_ROOT):
        raise ValueError("Le fichier privé doit rester en dehors du dépôt et du site web.")
    try:
        parts = urlsplit(database_url)
        valid = (
            parts.scheme in {"postgres", "postgresql", "pgsql"}
            and parts.hostname and parts.path.strip("/") and not parts.fragment
            and not any(character.isspace() for character in database_url)
        )
        parts.port
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise ValueError("La connexion PostgreSQL n’est pas valide ; sa valeur n’est pas affichée.")
    values = {
        "DJANGO_SETTINGS_MODULE": "amelone.settings_alwaysdata",
        "DJANGO_DEBUG": "0",
        "ALWAYSDATA_SITE_HOST": host,
        "DJANGO_SECRET_KEY": secrets.token_urlsafe(64),
        "DATABASE_URL": database_url,
        "ALWAYSDATA_HSTS_SECONDS": "3600",
        "ALWAYSDATA_ENABLE_EMAILS": "0",
        "EMAIL_ENABLED": "0",
        "EVENT_NOTIFICATIONS_ENABLED": "0",
        "EMAIL_PROVIDER": "brevo",
        "DEFAULT_FROM_EMAIL": "Amelone Loisirs <amelonebenevoles@gmail.com>",
        "BREVO_API_KEY": "",
        "SMS_ENABLED": "0",
    }
    content = "# Configuration privée AMELONE : ne pas publier ni coller dans le chat.\n"
    content += "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never replace an existing configuration, including an existing symlink.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(output, flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as target:
        target.write(content)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        connection = getpass.getpass("Connexion PostgreSQL cible (saisie masquée) : ")
        destination = create_environment(args.host, args.output, connection)
    except (ValueError, OSError, EOFError):
        parser.exit(1, "Configuration non créée : vérifier le chemin privé, la connexion et l’absence de fichier existant.\n")
    print(f"Configuration de test créée : {destination}. Aucune base modifiée, aucun email activé.")


if __name__ == "__main__":
    main()
