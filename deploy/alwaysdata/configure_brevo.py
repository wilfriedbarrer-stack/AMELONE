#!/usr/bin/env python3
"""Store a private Brevo key and check its connection without sending email."""

import argparse
import getpass
import json
import os
import shlex
import stat
import sys
import tempfile
import warnings
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from deploy.alwaysdata.private_env import ALLOWED_KEYS, _read_values  # noqa: E402

DEFAULT_ENV_FILE = Path("/home/amelone/private/amelone.env")
PUBLIC_ROOT = Path("/home/amelone/www")
SENDING_FLAGS = ("ALWAYSDATA_ENABLE_EMAILS", "EMAIL_ENABLED", "EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED")
REASONS = frozenset({
    "ready", "key_not_configured", "provider_not_brevo", "sender_not_configured",
    "invalid_api_key", "access_denied", "redirect_refused", "provider_unavailable",
    "unexpected_http_status", "response_too_large", "network_timeout", "connection_failed",
    "malformed_response", "sender_not_validated", "sender_not_found", "ip_not_authorized",
    "account_suspended", "activation_required", "permissions_missing",
})


class ConfigError(Exception):
    """A fixed message that can safely be displayed."""


def configuration_signature(path):
    metadata = path.lstat()
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077):
        raise ConfigError("La configuration doit être un fichier privé appartenant au compte.")
    return (metadata.st_dev, metadata.st_ino, metadata.st_uid, metadata.st_mode,
            metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def read_configuration(path):
    path = Path(path)
    if not path.is_absolute():
        raise ConfigError("Le fichier privé doit avoir un chemin absolu.")
    resolved = path.resolve(strict=True)
    if any(path.is_relative_to(root) or resolved.is_relative_to(root.resolve())
           for root in (PROJECT_ROOT, PUBLIC_ROOT)):
        raise ConfigError("La configuration doit rester hors du dépôt et du répertoire web.")
    if any((parent / ".git").is_file() or (parent / ".git" / "HEAD").is_file()
           for parent in (resolved.parent, *resolved.parents)):
        raise ConfigError("La configuration doit rester hors de tout dépôt Git.")
    before = configuration_signature(path)
    values = _read_values(path)
    if configuration_signature(path) != before:
        raise ConfigError("La configuration a changé pendant la lecture ; relancer l’opération.")
    return values, before


def write_configuration(path, values, expected):
    path = Path(path)
    if not set(values).issubset(ALLOWED_KEYS) or any(not isinstance(value, str) for value in values.values()):
        raise ConfigError("La configuration contient des champs incompatibles.")
    if read_configuration(path)[1] != expected:
        raise ConfigError("La configuration a changé ; aucun remplacement effectué.")
    content = "# Configuration privée AMELONE : ne pas publier ni coller dans le chat.\n"
    content += "".join(f"export {key}={shlex.quote(value)}\n" for key, value in values.items())
    temporary = None
    try:
        descriptor, filename = tempfile.mkstemp(prefix=".amelone-brevo-", suffix=".tmp", dir=path.parent)
        temporary = Path(filename)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if _read_values(temporary) != values:
            raise ConfigError("La nouvelle configuration privée n’a pas pu être vérifiée.")
        if read_configuration(path)[1] != expected:
            raise ConfigError("La configuration a changé ; aucun remplacement effectué.")
        os.replace(temporary, path)
        temporary = None
        configuration_signature(path)
    except OSError:
        raise ConfigError("Écriture privée impossible ; aucun détail sensible affiché.") from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def validate_key(key):
    if (not key or len(key) > 4096
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in key)):
        raise ConfigError("La clé doit être non vide, sans espace ni caractère de contrôle, et limitée à 4096 caractères.")


def check_connection(values):
    # The existing checker uses fixed GET endpoints; no sandbox or send is run.
    os.environ.update(values)
    os.environ.update({name: "0" for name in SENDING_FLAGS})
    os.environ["DJANGO_SETTINGS_MODULE"] = "amelone.settings_alwaysdata"
    import django

    django.setup()
    from volunteers.brevo_connection import check_brevo_connection

    return check_brevo_connection(sandbox=False)


def safe_diagnostic(result):
    if not isinstance(result, dict):
        result = {}
    fields = ("key_present", "account_authenticated", "sender_validated", "ready")
    diagnostic = {name: result.get(name) is True for name in fields}
    relay = result.get("transactional_relay_enabled")
    diagnostic["transactional_relay_enabled"] = relay if isinstance(relay, bool) else None
    reason = result.get("reason")
    diagnostic["reason"] = reason if isinstance(reason, str) and reason in REASONS else "diagnostic_unavailable"
    diagnostic["ready"] = all(diagnostic[name] for name in fields) and reason == "ready"
    return diagnostic


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "Arguments invalides ; utiliser --help.\n")


def main(argv=None):
    parser = SafeArgumentParser(description=__doc__)
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_true", help="Vérifier la connexion existante sans modifier le fichier")
    modes.add_argument("--enable-email", action="store_true", help="Activer seulement les emails transactionnels après vérification")
    args = parser.parse_args(argv)
    try:
        path = Path(args.env_file)
        values, expected = read_configuration(path)
        if args.enable_email and any(values.get(name) != "0" for name in ("EVENT_NOTIFICATIONS_ENABLED", "SMS_ENABLED")):
            raise ConfigError("Les notifications automatiques et les SMS doivent rester désactivés.")
        if not args.check and not args.enable_email:
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("Clé API Brevo (saisie masquée) : ")
            validate_key(key)
            print("Clé reçue ; ne pas la recoller. Enregistrement privé en cours.", flush=True)
            values.update(BREVO_API_KEY=key, EMAIL_PROVIDER="brevo")
            values.update({name: "0" for name in SENDING_FLAGS})
            write_configuration(path, values, expected)
            print("Clé enregistrée ; vérification GET en cours, sans envoi.", flush=True)
        elif args.enable_email:
            validate_key(values.get("BREVO_API_KEY", ""))
        diagnostic = safe_diagnostic(check_connection(values))
        print(json.dumps(diagnostic, ensure_ascii=True, sort_keys=True), flush=True)
        if not diagnostic["ready"]:
            return 1
        if args.enable_email:
            values.update(ALWAYSDATA_ENABLE_EMAILS="1", EMAIL_ENABLED="1")
            write_configuration(path, values, expected)
            print("Emails transactionnels activés dans le fichier privé ; notifications et SMS restent désactivés.")
        return 0
    except ConfigError as error:
        print(f"Configuration Brevo impossible : {error}", file=sys.stderr)
    except EOFError:
        print("Saisie terminée sans valeur (EOF) ; relancer dans un terminal interactif.", file=sys.stderr)
    except KeyboardInterrupt:
        print("Opération annulée.", file=sys.stderr)
    except getpass.GetPassWarning:
        print("Saisie masquée indisponible ; utiliser un terminal interactif.", file=sys.stderr)
    except Exception as error:
        print(f"Configuration Brevo impossible ({type(error).__name__}) ; aucun détail sensible affiché.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
