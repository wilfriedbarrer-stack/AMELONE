import csv
from functools import wraps

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, F, Prefetch, Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms_organizer import (
    AssignmentForm,
    AttendanceForm,
    BeverageForm,
    ConfirmationEmailForm,
    DuplicateEventForm,
    EventForm,
    MealOrderOrganizerForm,
    MenuForm,
    PaymentForm,
    RegistrationOrganizerForm,
    StationForm,
    StationSlotFormSet,
    TrackingFilterForm,
)
from .models import (
    Assignment,
    Attendance,
    Beverage,
    EmailDelivery,
    Event,
    EventDay,
    Family,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
)
from .services import (
    birthday_alerts_for_event,
    build_assignment_email_preview,
    cancel_registration,
    confirm_assignment,
    confirm_assignments,
    create_assignment,
    family_event_balance,
    record_payment,
    remove_assignment,
    retry_failed_deliveries,
    save_meal_order,
    save_registration,
    send_assignment_confirmations,
    update_assignment,
)


def organizer_required(view_func):
    @wraps(view_func)
    @login_required
    def wrapped(request, *args, **kwargs):
        if not (request.user.is_staff or request.user.is_organizer):
            raise PermissionDenied("Cette page est réservée aux organisateurs.")
        return view_func(request, *args, **kwargs)

    return wrapped


def _form_error_messages(error):
    if hasattr(error, "message_dict"):
        return [message for values in error.message_dict.values() for message in values]
    return getattr(error, "messages", [str(error)])


@organizer_required
def organizer_dashboard(request):
    now = timezone.now()
    events = Event.objects.exclude(status=Event.Status.ARCHIVED).prefetch_related("days")[:8]
    upcoming_days = EventDay.objects.filter(date__gte=timezone.localdate()).order_by("date")[:6]
    context = {
        "events": events,
        "upcoming_days": upcoming_days,
        "family_count": Family.objects.count(),
        "member_count": Member.objects.filter(archived_at__isnull=True).count(),
        "open_event_count": Event.objects.filter(
            status=Event.Status.PUBLISHED,
            registration_opens_at__lte=now,
            registration_closes_at__gte=now,
        ).count(),
        "unassigned_count": Registration.objects.filter(status=Registration.Status.ACTIVE)
        .exclude(assignments__status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED))
        .count(),
        "failed_email_count": EmailDelivery.objects.filter(
            status=EmailDelivery.Status.FAILED
        ).count(),
    }
    return render(request, "volunteers/organizer/dashboard.html", context)


@organizer_required
def event_list(request):
    events = Event.objects.annotate(
        day_count=Count("days", distinct=True),
        registration_count=Count("days__registrations", distinct=True),
    ).order_by("status", "-registration_opens_at")
    return render(request, "volunteers/organizer/event_list.html", {"events": events})


def _event_form(request, event=None):
    form = EventForm(request.POST or None, instance=event)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                if event is not None:
                    Event.objects.select_for_update().get(pk=event.pk)
                saved = form.save()
                if form.event_day:
                    day = EventDay.objects.select_for_update().get(pk=form.event_day.pk)
                    if day.date != form.cleaned_data["event_date"]:
                        if day.registrations.exists():
                            raise ValidationError(
                                "La date ne peut plus être modifiée après une inscription."
                            )
                        day.date = form.cleaned_data["event_date"]
                        day.full_clean()
                        day.save(update_fields=("date", "updated_at"))
                else:
                    EventDay.objects.get_or_create(
                        event=saved, date=form.cleaned_data["event_date"]
                    )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error("event_date", text)
        else:
            messages.success(request, "L'événement a été enregistré.")
            return redirect("event_detail", event_id=saved.pk)
    return render(
        request,
        "volunteers/organizer/event_form.html",
        {
            "form": form,
            "event": event,
            "additional_days": event.days.order_by("date")[1:] if event else (),
        },
    )


@organizer_required
def event_create(request):
    return _event_form(request)


@organizer_required
def event_update(request, event_id):
    return _event_form(request, get_object_or_404(Event, pk=event_id))


@organizer_required
def event_detail(request, event_id):
    event = get_object_or_404(Event.objects.prefetch_related("days", "stations__slots"), pk=event_id)
    registrations = Registration.objects.filter(event_day__event=event).select_related(
        "member__family", "event_day", "preferred_station"
    )
    balances = []
    families = Family.objects.filter(members__registrations__event_day__event=event).distinct()
    for family in families:
        balances.append((family, family_event_balance(family=family, event=event)))
    return render(
        request,
        "volunteers/organizer/event_detail.html",
        {
            "event": event,
            "registrations": registrations,
            "stations": event.stations.all(),
            "balances": balances,
            "birthday_alerts": birthday_alerts_for_event(event),
            "email_enabled": settings.EMAIL_ENABLED,
        },
    )


@organizer_required
def event_duplicate(request, event_id):
    source = get_object_or_404(Event.objects.prefetch_related("days", "stations__slots"), pk=event_id)
    form = DuplicateEventForm(request.POST or None, initial={"name": f"{source.name} — copie"})
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            clone = Event.objects.create(
                name=form.cleaned_data["name"],
                description=source.description,
                location=source.location,
                registration_opens_at=source.registration_opens_at,
                registration_closes_at=source.registration_closes_at,
                meal_orders_close_at=source.meal_orders_close_at,
                sumup_payment_url=source.sumup_payment_url,
                general_instructions=source.general_instructions,
                status=Event.Status.DRAFT,
            )
            day_map = {
                day.pk: EventDay.objects.create(event=clone, date=day.date)
                for day in source.days.all()
            }
            for station in source.stations.all():
                new_station = Station.objects.create(
                    event=clone,
                    name=station.name,
                    description=station.description,
                    instructions=station.instructions,
                    category=station.category,
                    age_policy=station.age_policy,
                    is_active=station.is_active,
                    position=station.position,
                )
                for slot in station.slots.all():
                    StationSlot.objects.create(
                        station=new_station,
                        event_day=day_map[slot.event_day_id],
                        period=slot.period,
                        capacity=slot.capacity,
                        start_time=slot.start_time,
                        end_time=slot.end_time,
                    )
            if form.cleaned_data["copy_catering"]:
                for old_day_id, new_day in day_map.items():
                    for menu in Menu.objects.filter(event_day_id=old_day_id):
                        Menu.objects.create(
                            event_day=new_day,
                            name=menu.name,
                            description=menu.description,
                            surcharge_cents=menu.surcharge_cents,
                            is_active=menu.is_active,
                            meal_type=menu.meal_type,
                        )
                    for drink in Beverage.objects.filter(event_day_id=old_day_id):
                        Beverage.objects.create(
                            event_day=new_day,
                            name=drink.name,
                            description=drink.description,
                            surcharge_cents=drink.surcharge_cents,
                            is_active=drink.is_active,
                        )
        messages.success(request, "L'événement a été dupliqué en brouillon.")
        return redirect("event_update", event_id=clone.pk)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": "Dupliquer l'événement", "event": source},
    )


@organizer_required
@require_POST
def event_archive(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    event.status = Event.Status.ARCHIVED
    event.save(update_fields=("status", "updated_at"))
    messages.success(request, "L'événement a été archivé sans supprimer son historique.")
    return redirect("event_list")


def _station_form(request, event, station=None):
    form = StationForm(request.POST or None, instance=station)
    formset = StationSlotFormSet(
        request.POST or None,
        instance=station,
        prefix="slots",
        form_kwargs={"event": event},
    )
    if request.method == "POST" and form.is_valid() and formset.is_valid():
        try:
            with transaction.atomic():
                Event.objects.select_for_update().get(pk=event.pk)
                list(EventDay.objects.select_for_update().filter(event=event).order_by("pk"))
                existing_ids = [item.instance.pk for item in formset.forms if item.instance.pk]
                list(StationSlot.objects.select_for_update().filter(pk__in=existing_ids).order_by("pk"))
                for item in formset.forms:
                    if item.cleaned_data.get("DELETE"):
                        if item.instance.pk and item.instance.assignments.exists():
                            raise ValidationError("Un créneau avec un historique d’affectations ne peut pas être supprimé. Retirez les affectations et désactivez le poste.")
                    elif item.cleaned_data and item.instance.pk:
                        item.instance.full_clean()
                saved = form.save(commit=False)
                saved.event = event
                saved.save()
                formset.instance = saved
                formset.save()
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "Le poste et ses capacités ont été enregistrés.")
            return redirect("event_detail", event_id=event.pk)
    return render(
        request,
        "volunteers/organizer/station_form.html",
        {"event": event, "station": station, "form": form, "formset": formset},
    )


@organizer_required
def station_create(request, event_id):
    return _station_form(request, get_object_or_404(Event, pk=event_id))


@organizer_required
def station_update(request, station_id):
    station = get_object_or_404(Station.objects.select_related("event"), pk=station_id)
    return _station_form(request, station.event, station)


@organizer_required
def registration_list(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    registrations = (
        Registration.objects.filter(event_day__event=event)
        .select_related("member__family", "event_day", "preferred_station")
        .prefetch_related("assignments__station_slot__station")
    )
    return render(
        request,
        "volunteers/organizer/registration_list.html",
        {"event": event, "registrations": registrations},
    )


@organizer_required
def registration_update(request, registration_id):
    registration = get_object_or_404(
        Registration.objects.select_related("member", "event_day__event"), pk=registration_id
    )
    form = RegistrationOrganizerForm(
        request.POST or None, instance=registration, event=registration.event_day.event
    )
    if request.method == "POST" and form.is_valid():
        try:
            if form.cleaned_data["status"] == Registration.Status.CANCELLED:
                saved = cancel_registration(registration=registration, actor=request.user)
            else:
                saved = save_registration(
                    member=registration.member,
                    event_day=registration.event_day,
                    availability=form.cleaned_data["availability"],
                    preferred_station=form.cleaned_data["preferred_station"],
                    note=form.cleaned_data["note"],
                    actor=request.user,
                )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            if getattr(saved, "reassignment_required", False):
                messages.warning(request, "Des affectations incompatibles ont été retirées : réaffectation nécessaire.")
            if getattr(saved, "coverage_invalidated_assignment_ids", []):
                messages.warning(request, "Des affectations de mineurs sont repassées en brouillon : un adulte doit être confirmé sur leur créneau.")
            messages.success(request, "L'inscription a été corrigée.")
            return redirect("registration_list", event_id=registration.event_day.event_id)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": "Corriger une inscription", "event": registration.event_day.event},
    )


@organizer_required
def assignment_board(request, event_id=None):
    events = Event.objects.exclude(status=Event.Status.ARCHIVED).order_by("-registration_opens_at")
    event = get_object_or_404(Event, pk=event_id) if event_id else events.first()
    selected_day = None
    days = EventDay.objects.none()
    registrations = Registration.objects.none()
    slots = StationSlot.objects.none()
    if event:
        days = event.days.order_by("date")
        day_id = request.GET.get("day")
        selected_day = get_object_or_404(days, pk=day_id) if day_id else days.first()
    if selected_day:
        registrations = (
            Registration.objects.filter(event_day=selected_day, status=Registration.Status.ACTIVE)
            .select_related("member__family", "preferred_station")
            .prefetch_related("assignments__station_slot__station")
        )
        slots = (
            StationSlot.objects.filter(event_day=selected_day, station__is_active=True)
            .select_related("station")
            .prefetch_related(
                Prefetch(
                    "assignments",
                    queryset=Assignment.objects.exclude(status=Assignment.Status.CANCELLED).select_related(
                        "registration__member"
                    ),
                    to_attr="active_assignments",
                )
            )
        )
        for registration in registrations:
            registration.age = registration.age_on_event_day
            registration.birthday = registration.is_birthday
        for slot in slots:
            slot.taken = len(slot.active_assignments)
            slot.remaining = max(slot.capacity - slot.taken, 0)
    return render(
        request,
        "volunteers/organizer/assignment_board.html",
        {
            "events": events,
            "event": event,
            "days": days,
            "selected_day": selected_day,
            "registrations": registrations,
            "slots": slots,
            "birthday_alerts": birthday_alerts_for_event(event) if event else [],
        },
    )


@organizer_required
def assignment_create(request, registration_id):
    registration = get_object_or_404(
        Registration.objects.select_related("event_day__event", "member"), pk=registration_id
    )
    form = AssignmentForm(request.POST or None, registration=registration)
    if request.method == "POST" and form.is_valid():
        try:
            create_assignment(
                registration=registration,
                station_slot=form.cleaned_data["station_slot"],
                actor=request.user,
            )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "L'affectation brouillon a été créée.")
            return redirect("assignment_board_event", event_id=registration.event_day.event_id)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": f"Affecter {registration.member.full_name}", "event": registration.event_day.event},
    )


@organizer_required
def assignment_update_view(request, assignment_id):
    assignment = get_object_or_404(
        Assignment.objects.select_related("registration__event_day__event"), pk=assignment_id
    )
    form = AssignmentForm(request.POST or None, assignment=assignment)
    if request.method == "POST" and form.is_valid():
        try:
            saved = update_assignment(
                assignment=assignment,
                station_slot=form.cleaned_data["station_slot"],
                actor=request.user,
            )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            if getattr(saved, "coverage_invalidated_assignment_ids", []):
                messages.warning(request, "Des affectations de mineurs sont repassées en brouillon : un adulte doit être confirmé sur leur créneau.")
            messages.success(request, "L'affectation a été modifiée.")
            return redirect("assignment_board_event", event_id=assignment.registration.event_day.event_id)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": "Modifier l'affectation", "event": assignment.registration.event_day.event},
    )


@organizer_required
@require_POST
def assignment_remove(request, assignment_id):
    assignment = get_object_or_404(
        Assignment.objects.select_related("registration__event_day"), pk=assignment_id
    )
    saved = remove_assignment(assignment=assignment, actor=request.user)
    if getattr(saved, "coverage_invalidated_assignment_ids", []):
        messages.warning(request, "Des affectations de mineurs sont repassées en brouillon : un adulte doit être confirmé sur leur créneau.")
    messages.success(request, "L'affectation a été retirée et son historique conservé.")
    return redirect("assignment_board_event", event_id=assignment.registration.event_day.event_id)


@organizer_required
@require_POST
def assignment_confirm(request, assignment_id):
    assignment = get_object_or_404(
        Assignment.objects.select_related("registration__event_day"), pk=assignment_id
    )
    confirm_assignment(assignment=assignment, confirmed_by=request.user)
    messages.success(request, "L'affectation est confirmée ; elle apparaît sur le planning public.")
    return redirect("assignment_board_event", event_id=assignment.registration.event_day.event_id)


@organizer_required
@require_POST
def event_assignments_confirm(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    assignments = Assignment.objects.filter(
        registration__event_day__event=event, status=Assignment.Status.DRAFT
    )
    confirmed = confirm_assignments(assignments=assignments, confirmed_by=request.user)
    messages.success(request, f"{len(confirmed)} affectation(s) confirmée(s).")
    return redirect("assignment_board_event", event_id=event.pk)


def _families_with_confirmed_assignments(event):
    return Family.objects.filter(
        members__registrations__event_day__event=event,
        members__registrations__assignments__status=Assignment.Status.CONFIRMED,
    ).select_related("account").distinct()


@organizer_required
def event_emails(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = ConfirmationEmailForm(request.POST or None, event=event)
    previews = []
    if request.method == "POST" and form.is_valid():
        families = (
            _families_with_confirmed_assignments(event)
            if form.cleaned_data["send_to_all"]
            else form.cleaned_data["families"]
        )
        action = request.POST.get("action", "preview")
        if action == "retry":
            if not settings.EMAIL_ENABLED:
                messages.error(request, "Le SMTP n'est pas configuré : aucune relance n'a été envoyée.")
            else:
                retried = retry_failed_deliveries(event=event, families=families, actor=request.user)
                messages.success(request, f"{len(retried)} envoi(s) en échec relancé(s).")
                return redirect("event_emails", event_id=event.pk)
        elif action == "send":
            if not settings.EMAIL_ENABLED:
                messages.error(request, "Le SMTP n'est pas configuré : aucun message n'a été marqué envoyé.")
            else:
                deliveries = send_assignment_confirmations(
                    event=event,
                    families=families,
                    extra_instructions=form.cleaned_data["extra_instructions"],
                    actor=request.user,
                )
                accepted = sum(item.status == EmailDelivery.Status.ACCEPTED for item in deliveries)
                failed = sum(item.status == EmailDelivery.Status.FAILED for item in deliveries)
                messages.success(request, f"{accepted} message(s) accepté(s) par le service d'envoi.")
                if failed:
                    messages.error(request, f"{failed} envoi(s) ont échoué et peuvent être relancés.")
                return redirect("event_emails", event_id=event.pk)
        for family in families:
            preview = build_assignment_email_preview(
                event=event,
                family=family,
                extra_instructions=form.cleaned_data["extra_instructions"],
            )
            if preview["assignments"]:
                previews.append(preview)
    history = EmailDelivery.objects.filter(event=event).select_related("family__account")
    return render(
        request,
        "volunteers/organizer/emails.html",
        {
            "event": event,
            "form": form,
            "previews": previews,
            "history": history,
            "email_enabled": settings.EMAIL_ENABLED,
        },
    )


@organizer_required
def catering(request, event_id):
    event = get_object_or_404(Event.objects.prefetch_related("days__menus", "days__beverages"), pk=event_id)
    orders = MealOrder.objects.filter(registration__event_day__event=event).select_related(
        "registration__member__family", "registration__event_day", "menu", "beverage"
    )
    menu_totals = orders.values("registration__event_day__date", "menu__name").annotate(
        quantity=Count("id")
    )
    beverage_totals = orders.exclude(beverage=None).values(
        "registration__event_day__date", "beverage__name"
    ).annotate(quantity=Count("id"))
    return render(
        request,
        "volunteers/organizer/catering.html",
        {
            "event": event,
            "orders": orders,
            "menu_totals": menu_totals,
            "beverage_totals": beverage_totals,
        },
    )


def _priced_option_form(request, event_day, model, form_class, instance=None):
    form = form_class(request.POST or None, instance=instance)
    if request.method == "POST" and form.is_valid():
        try:
            with transaction.atomic():
                EventDay.objects.select_for_update().get(pk=event_day.pk)
                if instance is not None:
                    model.objects.select_for_update().get(pk=instance.pk)
                option = form.save(commit=False)
                option.event_day = event_day
                option.full_clean()
                option.save()
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "L'option a été enregistrée.")
            return redirect("catering", event_id=event_day.event_id)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": model._meta.verbose_name.capitalize(), "event": event_day.event},
    )


@organizer_required
def menu_create(request, day_id):
    day = get_object_or_404(EventDay.objects.select_related("event"), pk=day_id)
    return _priced_option_form(request, day, Menu, MenuForm)


@organizer_required
def beverage_create(request, day_id):
    day = get_object_or_404(EventDay.objects.select_related("event"), pk=day_id)
    return _priced_option_form(request, day, Beverage, BeverageForm)


@organizer_required
def meal_order_update(request, order_id):
    order = get_object_or_404(
        MealOrder.objects.select_related("registration__event_day__event"), pk=order_id
    )
    form = MealOrderOrganizerForm(request.POST or None, instance=order, registration=order.registration)
    if request.method == "POST" and form.is_valid():
        try:
            save_meal_order(
                registration=order.registration,
                menu=form.cleaned_data["menu"],
                beverage=form.cleaned_data["beverage"],
                no_meal=not form.cleaned_data["wants_meal"],
                payment_method=form.cleaned_data["payment_method"],
                actor=request.user,
            )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "Le repas a été corrigé et le solde recalculé.")
            return redirect("catering", event_id=order.registration.event_day.event_id)
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": "Corriger un repas", "event": order.registration.event_day.event},
    )


@organizer_required
def catering_export(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="repas-{event.pk}.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Date", "Famille", "Membre", "Menu", "Boisson", "Supplément (€)"])
    orders = MealOrder.objects.filter(registration__event_day__event=event).select_related(
        "registration__event_day", "registration__member__family", "menu", "beverage"
    )
    for order in orders:
        writer.writerow(
            [
                order.registration.event_day.date.strftime("%d/%m/%Y"),
                order.registration.member.family.display_name,
                order.registration.member.full_name,
                order.menu.name if order.menu else "Pas de repas",
                order.beverage.name if order.beverage else "",
                f"{order.total_surcharge_cents / 100:.2f}".replace(".", ","),
            ]
        )
    return response


@organizer_required
def payments(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = PaymentForm(request.POST or None, event=event)
    if request.method == "POST" and form.is_valid():
        try:
            record_payment(
                family=form.cleaned_data["family"],
                event=event,
                amount_cents=form.amount_cents,
                method=form.cleaned_data["method"],
                paid_at=form.cleaned_data["paid_at"],
                note=form.cleaned_data["note"],
                recorded_by=request.user,
            )
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "Le paiement manuel a été enregistré.")
            return redirect("payments", event_id=event.pk)
    families = Family.objects.filter(
        Q(members__registrations__event_day__event=event) | Q(payments__event=event)
    ).select_related("account").distinct()
    balances = [(family, family_event_balance(family=family, event=event)) for family in families]
    history = Payment.objects.filter(event=event).select_related("family", "recorded_by")
    return render(
        request,
        "volunteers/organizer/payments.html",
        {"event": event, "form": form, "balances": balances, "history": history},
    )


def _tracking_queryset(form):
    registrations = Registration.objects.select_related(
        "member__family", "event_day__event"
    ).prefetch_related("assignments__station_slot__station")
    if not form.is_valid():
        return registrations.none()
    if form.cleaned_data.get("year"):
        registrations = registrations.filter(event_day__date__year=form.cleaned_data["year"])
    if form.cleaned_data.get("event"):
        registrations = registrations.filter(event_day__event=form.cleaned_data["event"])
    query = form.cleaned_data.get("query")
    if query:
        registrations = registrations.filter(
            Q(member__first_name__icontains=query)
            | Q(member__last_name__icontains=query)
            | Q(member__family__name__icontains=query)
        )
    attendance = form.cleaned_data.get("attendance")
    if attendance:
        registrations = registrations.filter(
            event_day__attendances__member=F("member"),
            event_day__attendances__status=attendance,
        ).distinct()
    return registrations.order_by("-event_day__date", "member__last_name", "member__first_name")


@organizer_required
def annual_tracking(request):
    form = TrackingFilterForm(request.GET or None)
    registrations = _tracking_queryset(form)
    attendance_map = {
        (item.member_id, item.event_day_id): item
        for item in Attendance.objects.filter(
            member_id__in=registrations.values("member_id"),
            event_day_id__in=registrations.values("event_day_id"),
        )
    }
    rows = []
    for registration in registrations:
        attendance = attendance_map.get((registration.member_id, registration.event_day_id))
        rows.append({"registration": registration, "attendance": attendance})
    present_counts = dict(
        Attendance.objects.filter(status=Attendance.Status.PRESENT)
        .values_list("member_id")
        .annotate(total=Count("id"))
    )
    return render(
        request,
        "volunteers/organizer/tracking.html",
        {"form": form, "rows": rows, "present_counts": present_counts},
    )


@organizer_required
def attendance_update(request, registration_id):
    registration = get_object_or_404(
        Registration.objects.select_related("member", "event_day__event"), pk=registration_id
    )
    attendance, _ = Attendance.objects.get_or_create(
        member=registration.member,
        event_day=registration.event_day,
    )
    form = AttendanceForm(request.POST or None, instance=attendance)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.recorded_by = request.user
        saved.save()
        messages.success(request, "La présence réelle a été enregistrée.")
        return redirect("annual_tracking")
    return render(
        request,
        "volunteers/organizer/generic_form.html",
        {"form": form, "title": f"Présence de {registration.member.full_name}", "event": registration.event_day.event},
    )


@organizer_required
def tracking_export(request):
    form = TrackingFilterForm(request.GET or None)
    registrations = _tracking_queryset(form)
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="suivi-annuel.csv"'
    response.write("\ufeff")
    writer = csv.writer(response, delimiter=";")
    writer.writerow(["Date", "Événement", "Famille", "Membre", "Disponibilité", "Présence"])
    attendance_map = {
        (a.member_id, a.event_day_id): a
        for a in Attendance.objects.filter(
            member_id__in=registrations.values("member_id"),
            event_day_id__in=registrations.values("event_day_id"),
        )
    }
    for registration in registrations:
        attendance = attendance_map.get((registration.member_id, registration.event_day_id))
        writer.writerow(
            [
                registration.event_day.date.strftime("%d/%m/%Y"),
                registration.event_day.event.name,
                registration.member.family.display_name,
                registration.member.full_name,
                registration.get_availability_display(),
                attendance.get_status_display() if attendance else "Non renseignée",
            ]
        )
    return response



@organizer_required
def menu_update(request, menu_id):
    menu = get_object_or_404(Menu.objects.select_related("event_day__event"), pk=menu_id)
    return _priced_option_form(request, menu.event_day, Menu, MenuForm, instance=menu)


@organizer_required
def beverage_update(request, beverage_id):
    beverage = get_object_or_404(Beverage.objects.select_related("event_day__event"), pk=beverage_id)
    return _priced_option_form(request, beverage.event_day, Beverage, BeverageForm, instance=beverage)


@organizer_required
def meal_order_create(request, registration_id):
    registration = get_object_or_404(Registration.objects.select_related("event_day__event"), pk=registration_id, status=Registration.Status.ACTIVE)
    existing = MealOrder.objects.filter(registration=registration).first()
    if existing:
        return redirect("meal_order_update", order_id=existing.pk)
    form = MealOrderOrganizerForm(request.POST or None, instance=MealOrder(registration=registration), registration=registration)
    if request.method == "POST" and form.is_valid():
        try:
            save_meal_order(registration=registration, menu=form.cleaned_data["menu"], beverage=form.cleaned_data["beverage"], no_meal=not form.cleaned_data["wants_meal"], payment_method=form.cleaned_data["payment_method"], actor=request.user)
        except ValidationError as error:
            for text in _form_error_messages(error):
                form.add_error(None, text)
        else:
            messages.success(request, "La restauration du participant a été enregistrée.")
            return redirect("recap_meals", event_id=registration.event_day.event_id)
    return render(request, "volunteers/organizer/generic_form.html", {"form": form, "title": "Renseigner la restauration", "event": registration.event_day.event})
