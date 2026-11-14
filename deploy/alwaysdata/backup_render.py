#!/usr/bin/env python3
"""Create a private, verified Render backup. Never restores or changes SQL data."""

import argparse
import getpass
import hashlib
import os
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import warnings
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

SOURCE_HOST = "dpg-db7ppf5g1s2s73bdoc20-a.oregon-postgres.render.com"
SOURCE_USER = "amelone"
SOURCE_DATABASE = "amelone"
DEFAULT_OUTPUT_DIR = Path("/home/amelone/private/backups")
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class BackupError(Exception):
    """An error whose message is safe to display."""


def parse_source(url):
    if any(ord(character) <= 32 or ord(character) == 127 for character in url):
        raise BackupError("URL source invalide : les espaces et contrôles doivent être encodés.")
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port if parts.port is not None else 5432
        user = unquote(parts.username or "", errors="strict")
        password = unquote(parts.password or "", errors="strict")
        database = unquote(parts.path[1:], errors="strict")
        valid = (
            parts.scheme in {"postgres", "postgresql"}
            and host == SOURCE_HOST and port == 5432
            and parts.path.startswith("/") and "/" not in parts.path[1:]
            and not parts.fragment and user and password and database
            and not any(character in value for value in (user, password, database)
                        for character in ("\n", "\r", "\x00"))
        )
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise BackupError("URL source invalide : utiliser l’URL externe de la base Render attendue.")
    return host, str(port), database, user, password


def pgpass_line(source):
    return ":".join(value.replace("\\", "\\\\").replace(":", "\\:") for value in source) + "\n"


def child_environment(source=None, pgpass_file=None):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PG") and key != "DATABASE_URL"}
    environment["PGSSLMODE"] = "require"
    environment["PGCONNECT_TIMEOUT"] = "20"
    if source is not None:
        environment.update(zip(("PGHOST", "PGPORT", "PGDATABASE", "PGUSER"), source[:4]))
        environment["PGPASSFILE"] = str(pgpass_file)
    return environment


def run_tool(arguments, environment=None):
    try:
        result = subprocess.run(arguments, env=environment, capture_output=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError):
        raise BackupError("Impossible d’exécuter les outils PostgreSQL.") from None
    if result.returncode:
        raise BackupError(f"Échec de {Path(arguments[0]).name} (code {result.returncode}).")
    return result.stdout


def private_output_dir(value):
    path = Path(value)
    if not path.is_absolute():
        raise BackupError("Le répertoire de sauvegarde doit être un chemin absolu hors du dépôt.")
    path = path.resolve()
    web_root = Path("/home/amelone/www").resolve()
    if path == web_root or web_root in path.parents:
        raise BackupError("Les sauvegardes doivent rester hors du répertoire web public.")
    if path == PROJECT_ROOT or PROJECT_ROOT in path.parents:
        raise BackupError("Les sauvegardes doivent rester hors du dépôt.")
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
           for parent in (path, *path.parents)):
        raise BackupError("Les sauvegardes doivent rester hors de tout dépôt Git.")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise BackupError("Le répertoire de sauvegarde doit être privé (permissions 700).")
    return path


def create_backup(source_url, output_dir):
    source = parse_source(source_url)
    output_dir = private_output_dir(output_dir)
    for tool in ("pg_dump", "pg_restore"):
        version = run_tool([tool, "--version"], child_environment())
        if not re.search(rb"\(PostgreSQL\) 18(?:\.|\b)", version):
            raise BackupError("Les outils pg_dump et pg_restore de version majeure 18 sont requis.")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final_dump = output_dir / f"render-{stamp}-{secrets.token_hex(6)}.dump"
    final_checksum = final_dump.with_suffix(".dump.sha256")
    if os.path.lexists(final_dump) or os.path.lexists(final_checksum):
        raise BackupError("Un fichier de destination existe déjà ; aucune sauvegarde écrasée.")
    temporary_paths = []
    published_paths = []
    success = False
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".render-", suffix=".partial", dir=output_dir)
        os.close(descriptor)
        partial_dump = Path(filename)
        temporary_paths.append(partial_dump)
        with tempfile.TemporaryDirectory(prefix="amelone-pgpass-") as password_dir:
            password_file = Path(password_dir) / "pgpass"
            descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as password_handle:
                password_handle.write(pgpass_line(source))
            environment = child_environment(source, password_file)
            run_tool(["pg_dump", "--no-password", "--format=custom", "--no-owner", "--no-privileges",
                      "-f", str(partial_dump)], environment)
            run_tool(["pg_restore", "--list", str(partial_dump)], environment)
        digest = hashlib.sha256()
        with partial_dump.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        descriptor, filename = tempfile.mkstemp(prefix=".render-", suffix=".sha256.partial", dir=output_dir)
        partial_checksum = Path(filename)
        temporary_paths.append(partial_checksum)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(f"{digest.hexdigest()}  {final_dump.name}\n")
        # Hard links publish complete files atomically and fail if a name exists.
        for temporary, destination in ((partial_dump, final_dump), (partial_checksum, final_checksum)):
            os.link(temporary, destination)
            published_paths.append(destination)
        success = True
        return final_dump, final_checksum
    finally:
        for path in temporary_paths + ([] if success else published_paths):
            path.unlink(missing_ok=True)


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Arguments invalides ; utiliser --help.\n")


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Répertoire privé absolu hors du dépôt")
    parser.add_argument("--password-only", action="store_true", help="Demander seulement le mot de passe de la base Render connue")
    arguments = parser.parse_args(argv)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            if arguments.password_only:
                password = getpass.getpass("Mot de passe PostgreSQL Render (saisie masquée) : ")
                if not password:
                    raise BackupError("Mot de passe vide : relancer puis saisir la valeur à l’invite masquée.")
                source_url = (
                    f"postgresql://{quote(SOURCE_USER, safe='')}:{quote(password, safe='')}"
                    f"@{SOURCE_HOST}/{quote(SOURCE_DATABASE, safe='')}"
                )
            else:
                source_url = getpass.getpass("URL externe Render (saisie masquée) : ")
        dump, checksum = create_backup(source_url, arguments.output_dir)
    except BackupError as error:
        print(f"Erreur : {error}", file=sys.stderr)
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
        # Only the type and a numeric errno are safe; never print exception text.
        code = type(error).__name__
        if isinstance(getattr(error, "errno", None), int):
            code += f", errno={error.errno}"
        print(f"Erreur technique ({code}) ; aucun détail sensible affiché.", file=sys.stderr)
        return 1
    print(f"Export créé (table des matières lisible) : {dump}\nSHA-256 : {checksum}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
