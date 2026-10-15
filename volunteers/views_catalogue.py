import csv
import secrets
from collections import Counter

from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.db import IntegrityError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render

from .catalogue_import import (
    CATALOGUE_KINDS,
    apply_catalogue_preview,
    parse_catalogue_upload,
    prepare_catalogue_preview,
)
from .forms_catalogue import CatalogueConfirmForm, CatalogueUploadForm
from .models import Assignment, Event, EventDay, Registration
from .views_organizer import organizer_required

PREVIEW_SESSION_KEY = "catalogue_import_preview"
PREVIEW_SALT = "volunteers.catalogue-import"


def _kind_label(kind):
    if kind not in CATALOGUE_KINDS:
        raise Http404
    return "menus" if kind == "menus" else "boissons"


@organizer_required
def catalogue_import(request, event_id, kind):
    label = _kind_label(kind)
    event = get_object_or_404(Event, pk=event_id)
    upload_form = CatalogueUploadForm(event=event)
    confirm_form = None
    preview = None
    day = None
    update_count = 0
    if request.method == "POST" and request.POST.get("action") == "confirm":
        confirm_form = CatalogueConfirmForm(request.POST)
        if confirm_form.is_valid():
            stored = request.session.get(PREVIEW_SESSION_KEY)
            try:
                token = signing.loads(
                    confirm_form.cleaned_data["preview_token"], salt=PREVIEW_SALT, max_age=15 * 60
                )
                if (
                    not stored
                    or stored["nonce"] != token.get("nonce")
                    or stored["event_id"] != str(event.pk)
                    or stored["user_id"] != str(request.user.pk)
                    or stored["kind"] != kind
                ):
                    raise ValidationError("Cet aperçu n'est plus disponible. Recommencez l'import.")
                day = get_object_or_404(EventDay, pk=stored["day_id"], event=event)
                count = apply_catalogue_preview(
                    day=day,
                    kind=kind,
                    rows=stored["rows"],
                    confirm_updates=confirm_form.cleaned_data["confirm_updates"],
                )
            except signing.BadSignature:
                preview = {"rows": [], "errors": ["L'aperçu a expiré. Recommencez l'import."]}
                confirm_form = None
            except (ValidationError, IntegrityError) as error:
                if isinstance(error, ValidationError):
                    confirm_form.add_error(None, error)
                else:
                    confirm_form.add_error(None, "Le catalogue a changé. Recommencez l'import.")
                if stored and day is not None:
                    preview = stored
                    update_count = sum(row["action"] == "update" for row in stored["rows"])
                else:
                    preview = {"rows": [], "errors": ["Cet aperçu n'est plus disponible. Recommencez l'import."]}
                    confirm_form = None
            else:
                del request.session[PREVIEW_SESSION_KEY]
                messages.success(request, f"{count} {label} importés pour le {day.date:%d/%m/%Y}.")
                return redirect("catering", event_id=event.pk)
    elif request.method == "POST":
        # A new upload invalidates the previous preview, including when invalid.
        request.session.pop(PREVIEW_SESSION_KEY, None)
        upload_form = CatalogueUploadForm(request.POST, request.FILES, event=event)
        if upload_form.is_valid():
            day = upload_form.cleaned_data["event_day"]
            parsed = prepare_catalogue_preview(
                parse_catalogue_upload(upload_form.cleaned_data["file"], kind), day, kind
            )
            preview = {"rows": parsed.rows, "errors": parsed.errors}
            if parsed.valid:
                nonce = secrets.token_urlsafe(24)
                preview.update(
                    nonce=nonce,
                    event_id=str(event.pk),
                    user_id=str(request.user.pk),
                    day_id=str(day.pk),
                    kind=kind,
                )
                request.session[PREVIEW_SESSION_KEY] = preview
                update_count = sum(row["action"] == "update" for row in parsed.rows)
                confirm_form = CatalogueConfirmForm(
                    initial={"preview_token": signing.dumps({"nonce": nonce}, salt=PREVIEW_SALT)}
                )
    return render(
        request,
        "organizer/catalogue_import.html",
        {
            "event": event,
            "kind": kind,
            "label": label,
            "upload_form": upload_form,
            "confirm_form": confirm_form,
            "preview": preview,
            "day": day,
            "update_count": update_count,
        },
    )


def _csv_response(filename):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response.write("\ufeff")
    return response


def _spreadsheet_safe(value):
    # CSV quoting does not prevent spreadsheet software from evaluating formulas.
    text = str(value if value is not None else "")
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text


@organizer_required
def catalogue_template(request, kind):
    _kind_label(kind)
    response = _csv_response(f"modele-{kind}.csv")
    writer = csv.writer(response, delimiter=";")
    headers = ["nom", "description", "supplement_euros"]
    if kind == "menus":
        headers.append("type")
        writer.writerow(headers)
        writer.writerow(["EXEMPLE - Repas végétarien", "Plat et dessert (à adapter)", "0,00", "repas"])
        writer.writerow(["EXEMPLE - Collation", "Fruit et biscuit (à adapter)", "0,00", "collation"])
    else:
        writer.writerow(headers)
        writer.writerow(["EXEMPLE - Eau", "Petite bouteille (à adapter)", "0,00"])
    return response


def _event_registrations(event):
    return Registration.objects.filter(event_day__event=event, status=Registration.Status.ACTIVE).select_related(
        "event_day", "member__family__account", "meal_order__menu", "meal_order__beverage",
        "meal_order__morning_snack", "meal_order__afternoon_snack",
    ).order_by("event_day__date", "member__last_name", "member__first_name")


def _assignment_rows(event):
    registrations = list(_event_registrations(event))
    assignment_map = {}
    assignments = Assignment.objects.filter(
        registration__event_day__event=event,
        registration__status=Registration.Status.ACTIVE,
        status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
    ).select_related("station_slot__station").order_by(
        "station_slot__station__position", "station_slot__station__name", "period"
    )
    for assignment in assignments:
        assignment_map.setdefault(assignment.registration_id, []).append(assignment)
    rows = []
    unassigned_count = 0
    for registration in registrations:
        member_assignments = assignment_map.get(registration.pk, [])
        if not member_assignments:
            unassigned_count += 1
        for assignment in member_assignments or [None]:
            rows.append({
                "registration": registration,
                "assignment": assignment,
                "station": assignment.station_slot.station.name if assignment else "Non affecté",
                "period": assignment.get_period_display() if assignment else "—",
                "status": assignment.get_status_display() if assignment else "En attente d'affectation",
                "start_time": assignment.station_slot.start_time if assignment else None,
                "end_time": assignment.station_slot.end_time if assignment else None,
            })
    return rows, len(registrations), assignments.count(), unassigned_count


@organizer_required
def recap_assignments(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    rows, participant_count, assignment_count, unassigned_count = _assignment_rows(event)
    return render(request, "organizer/recap_assignments.html", {
        "event": event,
        "rows": rows,
        "participant_count": participant_count,
        "assignment_count": assignment_count,
        "unassigned_count": unassigned_count,
        "print_view": request.GET.get("print") == "1",
    })


@organizer_required
def recap_assignments_csv(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    rows, _, _, _ = _assignment_rows(event)
    response = _csv_response("recapitulatif-affectations.csv")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Date", "Famille", "Participant", "Présence proposée", "Poste", "Période", "Début", "Fin", "Statut"])
    for row in rows:
        registration = row["registration"]
        writer.writerow([_spreadsheet_safe(value) for value in [
            registration.event_day.date.strftime("%d/%m/%Y"),
            registration.member.family.display_name,
            registration.member.full_name,
            registration.get_availability_display(),
            row["station"], row["period"],
            row["start_time"].strftime("%H:%M") if row["start_time"] else "",
            row["end_time"].strftime("%H:%M") if row["end_time"] else "",
            row["status"],
        ]])
    return response


def _meal_rows(event):
    rows = []
    quantities = Counter()
    beverage_quantities = Counter()
    for registration in _event_registrations(event):
        order = getattr(registration, "meal_order", None)
        has_meal = bool(order and order.wants_meal)
        menu = order.menu if has_meal else None
        beverage = order.beverage if has_meal else None
        morning_snack = order.morning_snack if has_meal else None
        afternoon_snack = order.afternoon_snack if has_meal else None
        formula = menu.get_meal_type_display() if menu else (
            "Repas complet" if registration.availability == Registration.Availability.FULL_DAY
            else "Collation"
        )
        choice = "Réservé" if has_meal else "Pas de repas" if order else "Non renseigné"
        payment = (
            "Officiel — sans supplément" if has_meal and order.official_exemption_applied
            else order.get_payment_method_display() if has_meal else "—"
        )
        rows.append({
            "registration": registration,
            "order": order,
            "formula": formula,
            "choice": choice,
            "menu": menu.name if menu else "—",
            "beverage": beverage.name if beverage else "—",
            "morning_snack_name": morning_snack.name if morning_snack else "—",
            "afternoon_snack_name": afternoon_snack.name if afternoon_snack else "—",
            "payment": payment,
            "surcharge_cents": order.total_surcharge_cents if has_meal else 0,
        })
        if menu:
            quantities[(registration.event_day.date, menu.get_meal_type_display(), menu.name)] += 1
        for snack, label in ((morning_snack, "Collation matin"), (afternoon_snack, "Collation après-midi")):
            if snack:
                quantities[(registration.event_day.date, label, snack.name)] += 1
        if beverage:
            beverage_quantities[(registration.event_day.date, beverage.name)] += 1
    return rows, quantities, beverage_quantities


@organizer_required
def recap_meals(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    rows, quantities, beverage_quantities = _meal_rows(event)
    return render(request, "organizer/recap_meals.html", {
        "event": event,
        "rows": rows,
        "participant_count": len(rows),
        "ordered_count": sum(row["choice"] == "Réservé" for row in rows),
        "no_meal_count": sum(row["choice"] == "Pas de repas" for row in rows),
        "missing_count": sum(row["choice"] == "Non renseigné" for row in rows),
        "quantities": [{"date": date, "type": meal_type, "name": name, "count": count} for (date, meal_type, name), count in sorted(quantities.items())],
        "beverage_quantities": [{"date": date, "name": name, "count": count} for (date, name), count in sorted(beverage_quantities.items())],
        "print_view": request.GET.get("print") == "1",
    })


@organizer_required
def recap_meals_csv(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    rows, _, _ = _meal_rows(event)
    response = _csv_response("recapitulatif-repas-participants.csv")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Date", "Famille", "Participant", "Présence proposée", "Formule", "Choix repas", "Menu", "Boisson", "Collation matin", "Collation après-midi", "Paiement souhaité", "Supplément (€)"])
    for row in rows:
        registration = row["registration"]
        writer.writerow([_spreadsheet_safe(value) for value in [
            registration.event_day.date.strftime("%d/%m/%Y"),
            registration.member.family.display_name,
            registration.member.full_name,
            registration.get_availability_display(),
            row["formula"], row["choice"], row["menu"], row["beverage"],
            row["morning_snack_name"], row["afternoon_snack_name"], row["payment"],
            f'{row["surcharge_cents"] / 100:.2f}'.replace(".", ","),
        ]])
    return response
