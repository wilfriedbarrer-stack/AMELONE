import secrets

from django.contrib import messages
from django.core import signing
from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache

from .catalogue_import import (
    CATALOGUE_KINDS,
    apply_catalogue_preview,
    parse_catalogue_upload,
    prepare_catalogue_preview,
)
from .forms_shared_catalogue import (
    SharedBeverageForm,
    SharedCatalogueConfirmForm,
    SharedCataloguePromotionForm,
    SharedCatalogueUploadForm,
    SharedMenuForm,
)
from .models import Beverage, Event, EventDay, Menu
from .shared_catalogue import promote_local_option, save_shared_option, shared_option_version
from .views_organizer import organizer_required

TEMPLATE_ROOT = "volunteers/organizer/shared_catalogue/"
PREVIEW_SESSION_KEY = "shared_catalogue_import_preview"
PREVIEW_SALT = "volunteers.shared-catalogue-import"


def _kind(kind):
    if kind not in CATALOGUE_KINDS:
        raise Http404
    return CATALOGUE_KINDS[kind], "menu" if kind == "menus" else "boisson"


def _form_errors(form, error):
    if hasattr(error, "error_dict"):
        for field, errors in error.error_dict.items():
            form.add_error(field if field in form.fields else None, errors)
    elif isinstance(error, ValidationError):
        form.add_error(None, error)
    else:
        form.add_error(None, "Une option commune de ce nom existe déjà ou a changé. Rechargez la page.")


@organizer_required
@never_cache
def shared_catalogue(request):
    menus = Menu.objects.filter(event_day__isnull=True).order_by("name")
    beverages = Beverage.objects.filter(event_day__isnull=True).order_by("name")
    local_days = EventDay.objects.filter(
        Q(menus__isnull=False) | Q(beverages__isnull=False)
    ).select_related("event").prefetch_related("menus", "beverages").distinct().order_by("-date", "event__name")
    local_events = Event.objects.filter(
        Q(days__menus__isnull=False) | Q(days__beverages__isnull=False)
    ).distinct().order_by("name")
    selected_event = None
    if request.GET.get("event"):
        try:
            selected_event = get_object_or_404(Event, pk=request.GET["event"])
        except ValidationError as error:
            raise Http404 from error
        local_days = local_days.filter(event=selected_event)
    return render(request, TEMPLATE_ROOT + "list.html", {
        "full_meal_menus": menus.filter(meal_type=Menu.MealType.FULL_MEAL),
        "snack_menus": menus.filter(meal_type=Menu.MealType.SNACK),
        "beverages": beverages,
        "local_days": Paginator(local_days, 15).get_page(request.GET.get("page")),
        "local_events": local_events,
        "selected_event": selected_event,
    })


def _shared_form(request, kind, option_id=None):
    model, label = _kind(kind)
    option = get_object_or_404(model, pk=option_id, event_day__isnull=True) if option_id else None
    form_class = SharedMenuForm if kind == "menus" else SharedBeverageForm
    form = form_class(request.POST if request.method == "POST" else None, instance=option)
    if request.method == "POST" and form.is_valid():
        try:
            save_shared_option(
                kind=kind, data=form.cleaned_data, actor=request.user,
                option_id=option_id, version=form.cleaned_data.get("version"),
            )
        except (ValidationError, IntegrityError) as error:
            _form_errors(form, error)
        else:
            messages.success(request, f"Le {label} commun a été enregistré pour tous les événements.")
            return redirect("shared_catalogue")
    return render(request, TEMPLATE_ROOT + "form.html", {
        "form": form, "kind": kind, "label": label, "option": option, "editing": option_id is not None,
    })


@organizer_required
@never_cache
def shared_catalogue_create(request, kind):
    return _shared_form(request, kind)


@organizer_required
@never_cache
def shared_catalogue_update(request, kind, option_id):
    return _shared_form(request, kind, option_id)


@organizer_required
@never_cache
def shared_catalogue_promote(request, kind, option_id):
    model, label = _kind(kind)
    option = get_object_or_404(
        model.objects.select_related("event_day__event"), pk=option_id, event_day__isnull=False
    )
    source_day = option.event_day
    form = SharedCataloguePromotionForm(
        request.POST if request.method == "POST" else None,
        initial={"version": shared_option_version(option, action="promote")},
    )
    if request.method == "POST" and form.is_valid():
        try:
            promote_local_option(
                kind=kind, option_id=option.pk, version=form.cleaned_data["version"], actor=request.user,
            )
        except (ValidationError, IntegrityError) as error:
            _form_errors(form, error)
        except model.DoesNotExist:
            form.add_error(None, "Cette option a déjà été rendue commune. Rechargez la page.")
        else:
            messages.success(request, f"Le {label} est maintenant commun à tous les événements. Les réservations existantes sont conservées.")
            return redirect("shared_catalogue")
    return render(request, TEMPLATE_ROOT + "promotion.html", {
        "form": form, "option": option, "kind": kind, "label": label,
        "source_day": source_day, "event": source_day.event,
    })


@organizer_required
@never_cache
def shared_catalogue_import(request, kind):
    _, label = _kind(kind)
    label += "s"
    upload_form = SharedCatalogueUploadForm()
    confirm_form = None
    preview = None
    update_count = 0
    if request.method == "POST" and request.POST.get("action") == "confirm":
        confirm_form = SharedCatalogueConfirmForm(request.POST)
        if confirm_form.is_valid():
            stored = request.session.get(PREVIEW_SESSION_KEY)
            valid_scope = False
            try:
                token = signing.loads(confirm_form.cleaned_data["preview_token"], salt=PREVIEW_SALT, max_age=15 * 60)
                valid_scope = bool(
                    stored and stored["nonce"] == token.get("nonce")
                    and stored["user_id"] == str(request.user.pk) and stored["kind"] == kind
                )
                if not valid_scope:
                    raise ValidationError("Cet aperçu n'est plus disponible. Recommencez l'import.")
                count = apply_catalogue_preview(
                    day=None, kind=kind, rows=stored["rows"],
                    confirm_updates=confirm_form.cleaned_data["confirm_updates"], actor=request.user,
                )
            except signing.BadSignature:
                preview = {"rows": [], "errors": ["L'aperçu a expiré ou est invalide. Recommencez l'import."]}
                confirm_form = None
            except (ValidationError, IntegrityError) as error:
                if valid_scope:
                    _form_errors(confirm_form, error)
                    preview = stored
                    update_count = sum(row["action"] == "update" for row in stored["rows"])
                else:
                    preview = {"rows": [], "errors": ["Cet aperçu n'est plus disponible. Recommencez l'import."]}
                    confirm_form = None
            else:
                del request.session[PREVIEW_SESSION_KEY]
                messages.success(request, f"{count} {label} importés dans le catalogue commun à tous les événements.")
                return redirect("shared_catalogue")
    elif request.method == "POST":
        request.session.pop(PREVIEW_SESSION_KEY, None)
        upload_form = SharedCatalogueUploadForm(request.POST, request.FILES)
        if upload_form.is_valid():
            parsed = prepare_catalogue_preview(
                parse_catalogue_upload(upload_form.cleaned_data["file"], kind, identity_by_type=True), None, kind,
            )
            preview = {"rows": parsed.rows, "errors": parsed.errors}
            if parsed.valid:
                nonce = secrets.token_urlsafe(24)
                preview.update(nonce=nonce, user_id=str(request.user.pk), kind=kind)
                request.session[PREVIEW_SESSION_KEY] = preview
                update_count = sum(row["action"] == "update" for row in parsed.rows)
                confirm_form = SharedCatalogueConfirmForm(initial={
                    "preview_token": signing.dumps({"nonce": nonce}, salt=PREVIEW_SALT),
                })
    return render(request, TEMPLATE_ROOT + "import.html", {
        "upload_form": upload_form, "confirm_form": confirm_form,
        "preview": preview, "kind": kind, "label": label, "update_count": update_count,
    })
