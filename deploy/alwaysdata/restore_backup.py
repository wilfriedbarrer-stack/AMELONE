#!/usr/bin/env python3
"""Restore a verified private backup into the empty, fixed Alwaysdata target."""

import argparse
import hashlib
import hmac
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deploy.alwaysdata.init_env import TargetCheckError, check_target_database  # noqa: E402
from deploy.alwaysdata.private_env import _read_values  # noqa: E402

TARGET_HOST = "postgresql-amelone.alwaysdata.net"
TARGET_DATABASE = "amelone_prod"
TARGET_USER = "amelone"
PUBLIC_ROOT = Path("/home/amelone/www")
SENDING_FLAGS = ("ALWAYSDATA_ENABLE_EMAILS", "EMAIL_ENABLED", "EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED")


class RestoreError(Exception):
    """A safe diagnostic that never contains credentials or database output."""


def target_credentials(values):
    if any(values.get(name) != "0" for name in SENDING_FLAGS):
        raise RestoreError("Les quatre interrupteurs d’envoi doivent être désactivés dans le fichier privé.")
    url = values.get("DATABASE_URL", "")
    try:
        if any(ord(character) <= 32 or ord(character) == 127 for character in url):
            raise ValueError
        parts = urlsplit(url)
        password = unquote(parts.password or "", errors="strict")
        valid = (
            parts.scheme in {"postgres", "postgresql"}
            and parts.hostname == TARGET_HOST
            and (parts.port if parts.port is not None else 5432) == 5432
            and unquote(parts.username or "", errors="strict") == TARGET_USER
            and unquote(parts.path, errors="strict") == f"/{TARGET_DATABASE}"
            and parse_qs(parts.query, strict_parsing=True) == {"sslmode": ["require"]}
            and not parts.fragment and bool(password)
            and not any(character in password for character in ("\n", "\r", "\x00"))
        )
    except (ValueError, UnicodeError):
        valid = False
    if not valid:
        raise RestoreError("La connexion doit viser uniquement la cible Alwaysdata autorisée avec TLS obligatoire.")
    return password


def open_private_file(value):
    path = Path(value)
    if not path.is_absolute():
        raise RestoreError("Les fichiers de sauvegarde doivent avoir des chemins privés absolus.")
    resolved = path.resolve(strict=True)
    if any(path.is_relative_to(root) or resolved.is_relative_to(root.resolve())
           for root in (PROJECT_ROOT, PUBLIC_ROOT)):
        raise RestoreError("Les fichiers de sauvegarde doivent rester hors du dépôt et du répertoire web.")
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise RestoreError("Les liens symboliques ne sont pas admis pour les fichiers de sauvegarde.")
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
           for parent in (path.parent, *path.parents)):
        raise RestoreError("Les fichiers de sauvegarde doivent rester hors de tout dépôt Git.")
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise RestoreError("La sauvegarde et son checksum doivent être des fichiers privés ordinaires.")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
                or metadata.st_mode & 0o077
                or (metadata.st_dev, metadata.st_ino) != (before.st_dev, before.st_ino)):
            raise RestoreError("La sauvegarde et son checksum doivent appartenir au compte et rester privés.")
        return os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise


def verify_dump(dump):
    with open_private_file(dump) as archive, open_private_file(Path(f"{dump}.sha256")) as checksum:
        raw_checksum = checksum.read(4097)
        match = re.fullmatch(rb"([0-9a-fA-F]{64})  ([^\r\n]+)\n?", raw_checksum)
        if len(raw_checksum) > 4096 or not match or match[2] != dump.name.encode("utf-8"):
            raise RestoreError("Le fichier SHA-256 n’a pas le format attendu pour cette sauvegarde.")
        digest = hashlib.sha256()
        for block in iter(lambda: archive.read(1024 * 1024), b""):
            digest.update(block)
        if not hmac.compare_digest(digest.hexdigest(), match[1].decode("ascii").lower()):
            raise RestoreError("Le checksum SHA-256 de la sauvegarde ne correspond pas.")


def child_environment(password_file=None):
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("PG") and key != "DATABASE_URL"}
    environment.update(PGSSLMODE="require", PGCONNECT_TIMEOUT="20")
    if password_file is not None:
        environment.update(PGHOST=TARGET_HOST, PGPORT="5432", PGDATABASE=TARGET_DATABASE,
                           PGUSER=TARGET_USER, PGPASSFILE=str(password_file))
    return environment


def run_tool(arguments, environment, operation, timeout=300):
    try:
        result = subprocess.run(arguments, env=environment, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        raise RestoreError(f"Impossible de terminer {operation} ; aucun détail sensible affiché.") from None
    if result.returncode:
        raise RestoreError(f"Échec de {operation} (code {result.returncode}).")
    return result.stdout


def restore_backup(dump, env_file):
    dump = Path(dump)
    try:
        values = _read_values(Path(env_file))
    except (OSError, ValueError, UnicodeError, RuntimeError):
        raise RestoreError("Le fichier de configuration privé est inaccessible ou invalide.") from None
    password = target_credentials(values)
    verify_dump(dump)
    version = run_tool(["pg_restore", "--version"], child_environment(), "la vérification du client")
    if not re.search(rb"\(PostgreSQL\) 18(?:\.|\b)", version):
        raise RestoreError("Le client pg_restore de version majeure 18 est requis.")
    try:
        check_target_database(TARGET_HOST, TARGET_DATABASE, TARGET_USER, password)
    except TargetCheckError as error:
        raise RestoreError(str(error)) from None
    with tempfile.TemporaryDirectory(prefix="amelone-restore-pgpass-") as directory:
        password_file = Path(directory) / "pgpass"
        descriptor = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        fields = (TARGET_HOST, "5432", TARGET_DATABASE, TARGET_USER, password)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(":".join(value.replace("\\", "\\\\").replace(":", "\\:") for value in fields) + "\n")
        environment = child_environment(password_file)
        print("Archive et cible vérifiées. Restauration Alwaysdata en cours ; patienter.")
        run_tool(["pg_restore", f"--dbname={TARGET_DATABASE}", "--no-owner", "--no-privileges",
                  "--single-transaction", "--exit-on-error", "--no-password", str(dump)],
                 environment, "la restauration transactionnelle")
        environment["PGOPTIONS"] = "-c default_transaction_read_only=on"
        counts = run_tool(
            ["psql", "--no-password", "--no-psqlrc", "--tuples-only", "--no-align", "--set=ON_ERROR_STOP=1",
             "-c", "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname = 'public';",
             "-c", "SELECT count(*) FROM public.django_migrations;"],
            environment, "la vérification finale après restauration", timeout=60,
        )
    match = re.fullmatch(rb"\s*([0-9]+)\s+([0-9]+)\s*", counts)
    if not match or int(match[1]) == 0:
        raise RestoreError("Restauration exécutée ; la vérification finale des tables doit être examinée.")
    return int(match[1]), int(match[2])


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Arguments invalides ; utiliser --help.\n")


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True)
    parser.add_argument("--env-file", required=True)
    args = parser.parse_args(argv)
    try:
        tables, migrations = restore_backup(args.dump, args.env_file)
    except RestoreError as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Opération interrompue ; vérifier l’état de la cible avant de poursuivre.", file=sys.stderr)
        return 1
    except Exception as error:
        code = type(error).__name__
        if isinstance(getattr(error, "errno", None), int):
            code += f", errno={error.errno}"
        print(f"Restauration impossible ({code}) ; aucun détail sensible affiché.", file=sys.stderr)
        return 1
    print(f"Restauration terminée : {tables} tables publiques, {migrations} migrations Django. Envois désactivés.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
