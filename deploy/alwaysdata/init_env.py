"""Create a private staging configuration without displaying credentials."""

import argparse
import getpass
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path
from urllib.parse import quote, urlsplit

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
    if any(output.resolve().is_relative_to(root) for root in (PROJECT_ROOT, Path("/home/amelone/www"))):
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


class TargetCheckError(Exception):
    """A fixed diagnostic that contains no credentials or child output."""


def check_target_database(host, name, user, password):
    fields = (host, "5432", name, user, password)
    if any(character in value for value in fields for character in ("\n", "\r", "\x00")):
        raise TargetCheckError("Les champs PostgreSQL contiennent des caractères incompatibles.")
    with tempfile.TemporaryDirectory(prefix="amelone-target-pgpass-") as directory:
        password_file = Path(directory) / "pgpass"
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(":".join(value.replace("\\", "\\\\").replace(":", "\\:") for value in fields) + "\n")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith("PG") and key != "DATABASE_URL"}
        environment.update(
            PGHOST=host, PGPORT="5432", PGDATABASE=name, PGUSER=user,
            PGPASSFILE=str(password_file), PGSSLMODE="require", PGCONNECT_TIMEOUT="20",
            PGOPTIONS="-c default_transaction_read_only=on",
        )
        try:
            result = subprocess.run(
                ["psql", "--no-password", "--no-psqlrc", "--tuples-only", "--no-align",
                 "--set=ON_ERROR_STOP=1", "-c", "SELECT current_setting('server_version_num')::integer;",
                 "-c", "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';"],
                env=environment, capture_output=True, timeout=60, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise TargetCheckError("Vérification PostgreSQL impossible ; vérifier le client et la connexion.") from None
        if result.returncode:
            raise TargetCheckError(f"Vérification PostgreSQL impossible (code {result.returncode}).")
        match = re.fullmatch(rb"\s*([0-9]+)\s+([0-9]+)\s*", result.stdout)
        if not match:
            raise TargetCheckError("La vérification PostgreSQL n’a pas renvoyé les deux nombres attendus.")
        major = int(match[1]) // 10000
        if major != 18:
            raise TargetCheckError(f"PostgreSQL 18 requis sur la cible (version majeure détectée : {major}).")
        if int(match[2]) != 0:
            raise TargetCheckError("La base cible doit être vide (aucune table publique).")


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Arguments invalides ; utiliser --help et fournir les trois champs PostgreSQL ensemble.\n")


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--db-host")
    parser.add_argument("--db-name")
    parser.add_argument("--db-user")
    parser.add_argument("--check-db", action="store_true", help="Vérifier PostgreSQL 18 et une base cible vide avant écriture")
    args = parser.parse_args(argv)
    fields = (args.db_host, args.db_name, args.db_user)
    fields_present = tuple(value is not None for value in fields)
    if (any(fields_present) and not all(fields_present)) or (args.check_db and not all(fields_present)):
        parser.error("Incomplete database fields")
    if all(fields_present):
        label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
        if (not re.fullmatch(rf"{label}(?:\.{label})+", args.db_host)
                or len(args.db_host) > 253 or not args.db_name.strip() or not args.db_user.strip()
                or any(ord(character) < 32 or ord(character) == 127 for value in fields for character in value)):
            parser.error("Invalid database fields")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            if all(fields_present):
                password = getpass.getpass("Mot de passe PostgreSQL cible (saisie masquée) : ")
                if not password or any(character in password for character in ("\n", "\r", "\x00")):
                    raise TargetCheckError("Le mot de passe PostgreSQL est vide ou incompatible.")
                connection = (
                    f"postgresql://{quote(args.db_user, safe='')}:{quote(password, safe='')}"
                    f"@{args.db_host}:5432/{quote(args.db_name, safe='')}?sslmode=require"
                )
            else:
                connection = getpass.getpass("Connexion PostgreSQL cible (saisie masquée) : ")
        if args.check_db:
            print("Vérification de la base Alwaysdata en cours ; patienter sans rien recoller.")
            check_target_database(args.db_host, args.db_name, args.db_user, password)
        destination = create_environment(args.host, args.output, connection)
    except TargetCheckError as error:
        print(f"Configuration non créée : {error}", file=sys.stderr)
        return 1
    except EOFError:
        print("Saisie terminée sans valeur (EOF) ; relancer dans un terminal interactif.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Saisie annulée.", file=sys.stderr)
        return 1
    except getpass.GetPassWarning:
        print("Saisie masquée indisponible ; utiliser un terminal interactif.", file=sys.stderr)
        return 1
    except Exception as error:
        code = type(error).__name__
        if isinstance(getattr(error, "errno", None), int):
            code += f", errno={error.errno}"
        print(f"Configuration non créée ({code}) ; aucun détail sensible affiché.", file=sys.stderr)
        return 1
    print(f"Configuration de test créée : {destination}. Aucune base modifiée, aucun email activé.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
