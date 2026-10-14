import csv
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from volunteers.models import (
    Event,
    EventDay,
    Family,
    LegacyImportRecord,
    Member,
    Registration,
    Station,
    StationSlot,
    User,
)

PARIS = ZoneInfo("Europe/Paris")


def _read_rows(path):
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        yield from csv.DictReader(handle)


def _fingerprint(row):
    return hashlib.sha256(
        json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _required(row, *names):
    missing = [name for name in names if not (row.get(name) or "").strip()]
    if missing:
        raise ValueError("Champs manquants : " + ", ".join(missing))


def _date(value, field):
    result = parse_date((value or "").strip())
    if result is None:
        raise ValueError(f"{field} doit être une date ISO AAAA-MM-JJ")
    return result


def _datetime(value, field):
    result = parse_datetime((value or "").strip())
    if result is None:
        raise ValueError(f"{field} doit être une date/heure ISO")
    if timezone.is_naive(result):
        result = timezone.make_aware(result, PARIS)
    return result


class Command(BaseCommand):
    help = "Importe des exports CSV de l'ancien Google Sheets avec journal et rapport."

    def add_arguments(self, parser):
        parser.add_argument("--events", type=Path)
        parser.add_argument("--stations", type=Path)
        parser.add_argument("--registrations", type=Path)
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--report", type=Path, default=Path("rapport-import.json"))

    def handle(self, *args, **options):
        if not any(options[name] for name in ("events", "stations", "registrations")):
            raise CommandError("Indiquez au moins un fichier CSV à importer.")
        report = {
            "dry_run": options["dry_run"],
            "generated_at": timezone.now().isoformat(),
            "summary": {"imported": 0, "skipped": 0, "needs_review": 0, "failed": 0},
            "rows": [],
        }
        with transaction.atomic():
            if options["events"]:
                self._process_file(
                    options["events"], LegacyImportRecord.SourceType.EVENT, self._import_event, report
                )
            if options["stations"]:
                self._process_file(
                    options["stations"], LegacyImportRecord.SourceType.STATION, self._import_station, report
                )
            if options["registrations"]:
                self._process_file(
                    options["registrations"],
                    LegacyImportRecord.SourceType.REGISTRATION,
                    self._import_registration,
                    report,
                )
            if options["dry_run"]:
                transaction.set_rollback(True)

        options["report"].parent.mkdir(parents=True, exist_ok=True)
        options["report"].write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        summary = report["summary"]
        self.stdout.write(
            self.style.SUCCESS(
                f"Rapport écrit dans {options['report']} — {summary['imported']} importée(s), "
                f"{summary['needs_review']} à revoir, {summary['failed']} en échec, "
                f"{summary['skipped']} ignorée(s)."
            )
        )

    def _process_file(self, path, source_type, importer, report):
        if not path.exists():
            raise CommandError(f"Fichier introuvable : {path}")
        for line_number, row in enumerate(_read_rows(path), start=2):
            legacy_id = (row.get("legacy_id") or "").strip()
            item = {
                "source": source_type,
                "file": str(path),
                "line": line_number,
                "legacy_id": legacy_id,
            }
            if not legacy_id:
                item.update(status="failed", messages=["legacy_id est obligatoire"])
                report["summary"]["failed"] += 1
                report["rows"].append(item)
                continue
            fingerprint = _fingerprint(row)
            record = LegacyImportRecord.objects.filter(
                source_type=source_type, legacy_id=legacy_id
            ).first()
            if record and record.status == LegacyImportRecord.Status.IMPORTED:
                if record.content_fingerprint == fingerprint:
                    item.update(status="skipped", messages=["Ligne déjà importée à l'identique."])
                else:
                    item.update(
                        status="needs_review",
                        messages=["La source a changé après import ; vérification humaine requise."],
                    )
                report["summary"][item["status"]] += 1
                report["rows"].append(item)
                continue
            if record is None:
                record = LegacyImportRecord(
                    source_type=source_type,
                    legacy_id=legacy_id,
                    content_fingerprint=fingerprint,
                    payload=row,
                )
            else:
                record.content_fingerprint = fingerprint
                record.payload = row
            try:
                # Un point de sauvegarde par ligne évite de conserver un objet
                # partiellement créé quand une validation tardive échoue.
                with transaction.atomic():
                    target, messages = importer(row)
            except ValueError as exc:
                record.status = LegacyImportRecord.Status.NEEDS_REVIEW
                record.report = {"messages": [str(exc)]}
                record.target_model = ""
                record.target_uuid = None
                record.save()
                item.update(status="needs_review", messages=[str(exc)])
            except Exception as exc:  # une ligne invalide ne bloque pas le rapport complet
                record.status = LegacyImportRecord.Status.FAILED
                record.report = {"messages": [str(exc)]}
                record.target_model = ""
                record.target_uuid = None
                record.save()
                item.update(status="failed", messages=[str(exc)])
            else:
                record.status = LegacyImportRecord.Status.IMPORTED
                record.target_model = target._meta.label
                record.target_uuid = target.pk
                record.imported_at = timezone.now()
                record.report = {"messages": messages}
                record.save()
                item.update(status="imported", target=str(target.pk), messages=messages)
            report["summary"][item["status"]] += 1
            report["rows"].append(item)

    def _import_event(self, row):
        _required(
            row,
            "legacy_id",
            "nom",
            "lieu",
            "dates",
            "ouverture_inscriptions",
            "cloture_inscriptions",
            "cloture_repas",
        )
        status_map = {
            "brouillon": Event.Status.DRAFT,
            "draft": Event.Status.DRAFT,
            "publié": Event.Status.PUBLISHED,
            "publie": Event.Status.PUBLISHED,
            "published": Event.Status.PUBLISHED,
            "archivé": Event.Status.ARCHIVED,
            "archive": Event.Status.ARCHIVED,
            "archived": Event.Status.ARCHIVED,
        }
        raw_status = (row.get("statut") or "brouillon").strip().lower()
        if raw_status not in status_map:
            raise ValueError(f"Statut d'événement inconnu : {raw_status}")
        event = Event.objects.create(
            name=row["nom"].strip(),
            description=(row.get("description") or "").strip(),
            location=row["lieu"].strip(),
            registration_opens_at=_datetime(row["ouverture_inscriptions"], "ouverture_inscriptions"),
            registration_closes_at=_datetime(row["cloture_inscriptions"], "cloture_inscriptions"),
            meal_orders_close_at=_datetime(row["cloture_repas"], "cloture_repas"),
            sumup_payment_url=(row.get("lien_sumup") or "").strip(),
            general_instructions=(row.get("consignes") or "").strip(),
            status=status_map[raw_status],
        )
        event.full_clean()
        event.save()
        raw_dates = [part.strip() for part in row["dates"].split(";") if part.strip()]
        if not raw_dates:
            raise ValueError("Au moins une date d'événement est obligatoire.")
        for raw_date in raw_dates:
            EventDay.objects.create(event=event, date=_date(raw_date, "dates"))
        return event, []

    def _event_from_legacy(self, legacy_id):
        record = LegacyImportRecord.objects.filter(
            source_type=LegacyImportRecord.SourceType.EVENT,
            legacy_id=legacy_id,
            status=LegacyImportRecord.Status.IMPORTED,
            target_model=Event._meta.label,
        ).first()
        if not record:
            raise ValueError(f"Événement legacy {legacy_id!r} non importé ou absent.")
        return Event.objects.get(pk=record.target_uuid)

    def _import_station(self, row):
        _required(
            row,
            "legacy_id",
            "event_legacy_id",
            "nom",
            "date",
            "periode",
            "capacite",
        )
        event = self._event_from_legacy(row["event_legacy_id"].strip())
        event_day = EventDay.objects.get(event=event, date=_date(row["date"], "date"))
        category_map = {
            "bénévole": Station.Category.VOLUNTEER,
            "benevole": Station.Category.VOLUNTEER,
            "volunteer": Station.Category.VOLUNTEER,
            "officiel": Station.Category.OFFICIAL,
            "official": Station.Category.OFFICIAL,
        }
        period_map = {
            "matin": StationSlot.Period.MORNING,
            "morning": StationSlot.Period.MORNING,
            "après-midi": StationSlot.Period.AFTERNOON,
            "apres-midi": StationSlot.Period.AFTERNOON,
            "afternoon": StationSlot.Period.AFTERNOON,
        }
        category = category_map.get((row.get("categorie") or "bénévole").strip().lower())
        period = period_map.get(row["periode"].strip().lower())
        if category is None or period is None:
            raise ValueError("Catégorie ou période inconnue.")
        try:
            capacity = int(row["capacite"])
        except ValueError as exc:
            raise ValueError("capacite doit être un entier positif ou nul.") from exc
        station, _ = Station.objects.get_or_create(
            event=event,
            name=row["nom"].strip(),
            defaults={
                "description": (row.get("description") or "").strip(),
                "instructions": (row.get("consignes") or "").strip(),
                "category": category,
            },
        )
        start = parse_time((row.get("debut") or "").strip())
        end = parse_time((row.get("fin") or "").strip())
        if bool(start) != bool(end):
            raise ValueError("Les horaires debut et fin doivent être fournis ensemble.")
        slot = StationSlot.objects.create(
            station=station,
            event_day=event_day,
            period=period,
            capacity=capacity,
            start_time=start,
            end_time=end,
        )
        slot.full_clean()
        return slot, []

    def _import_registration(self, row):
        _required(
            row,
            "legacy_id",
            "event_legacy_id",
            "date",
            "prenom",
            "nom",
            "periode",
            "email_responsable",
            "date_naissance",
        )
        event = self._event_from_legacy(row["event_legacy_id"].strip())
        event_day = EventDay.objects.get(event=event, date=_date(row["date"], "date"))
        email = row["email_responsable"].strip().lower()
        account = User.objects.filter(email__iexact=email).first()
        messages = []
        if account is None:
            account = User.objects.create_user(email=email, password=None)
            account.set_unusable_password()
            account.save(update_fields=["password"])
            messages.append("Compte créé sans mot de passe : envoyer une réinitialisation au responsable.")
        family, _ = Family.objects.get_or_create(account=account, defaults={"name": row["nom"].strip()})
        birth_date = _date(row["date_naissance"], "date_naissance")
        member, _ = Member.objects.get_or_create(
            family=family,
            first_name=row["prenom"].strip(),
            last_name=row["nom"].strip(),
            birth_date=birth_date,
        )
        availability_map = {
            "journée entière": Registration.Availability.FULL_DAY,
            "journee entiere": Registration.Availability.FULL_DAY,
            "full_day": Registration.Availability.FULL_DAY,
            "matin": Registration.Availability.MORNING,
            "morning": Registration.Availability.MORNING,
            "après-midi": Registration.Availability.AFTERNOON,
            "apres-midi": Registration.Availability.AFTERNOON,
            "afternoon": Registration.Availability.AFTERNOON,
        }
        availability = availability_map.get(row["periode"].strip().lower())
        if availability is None:
            raise ValueError(f"Période inconnue : {row['periode']}")
        preferred = None
        preferred_name = (row.get("poste_souhaite") or "").strip()
        if preferred_name:
            preferred = Station.objects.filter(event=event, name__iexact=preferred_name).first()
            if preferred is None:
                messages.append(f"Poste souhaité introuvable : {preferred_name!r}; laissé sans préférence.")
        registration, _ = Registration.objects.get_or_create(
            member=member,
            event_day=event_day,
            defaults={
                "availability": availability,
                "preferred_station": preferred,
                "note": (row.get("remarque") or "").strip(),
            },
        )
        return registration, messages

