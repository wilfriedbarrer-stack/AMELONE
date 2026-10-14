"""Vues de l'espace famille.

Chaque objet privé est récupéré depuis la famille de l'utilisateur connecté.
Cette restriction est volontairement appliquée dans les requêtes (et pas
seulement dans les gabarits) afin qu'un UUID deviné ne permette aucun accès
inter-familles.
"""

from collections import defaultdict

from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.db.models import Min, Prefetch
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .forms_family import (
    FamilyAccountForm,
    FamilyMealForm,
    FamilyRegistrationForm,
    MemberForm,
    SignUpForm,
)
from .models import Assignment, Attendance, Event, EventDay, Family, Member, Registration
from .services import (
    cancel_registration,
    family_event_balance,
    save_meal_reservation,
    save_registration,
)


def _family_for(request):
    """Retourne exclusivement la famille rattachée au compte courant."""

    try:
        return Family.objects.select_related("account").get(account=request.user)
    except Family.DoesNotExist as exc:
        raise Http404("Aucun compte famille n'est rattaché à cet utilisateur.") from exc


def _report_validation_error(form, error):
    """Transforme proprement une erreur métier en erreurs de formulaire."""

    if hasattr(error, "message_dict"):
        for field, field_messages in error.message_dict.items():
            target = field if field in form.fields else None
            for message in field_messages:
                form.add_error(target, message)
        return
    for message in getattr(error, "messages", [str(error)]):
        form.add_error(None, message)


def _members_with_current_age(queryset):
    members = list(queryset)
    today = timezone.localdate()
    for member in members:
        member.age = member.age_on(today)
    return members


def _balance_for_display(balance):
    result = dict(balance)
    for source, target in (
        ("due_cents", "total_due"),
        ("paid_cents", "paid"),
        ("amount_remaining_cents", "remaining"),
        ("overpayment_cents", "overpayment"),
    ):
        result[target] = f"{result[source] / 100:.2f}".replace(".", ",")
    return result


@require_http_methods(["GET", "POST"])
def signup(request):
    if request.user.is_authenticated:
        return redirect("dashboard")
    form = SignUpForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user, backend="volunteers.auth_backend.EmailBackend")
        messages.success(request, "Votre compte famille a bien été créé.")
        return redirect("dashboard")
    return render(request, "volunteers/signup.html", {"form": form})


@login_required
def dashboard(request):
    family = _family_for(request)
    now = timezone.now()
    members = _members_with_current_age(family.members.filter(archived_at__isnull=True))
    open_events = (
        Event.objects.filter(
            status=Event.Status.PUBLISHED,
            registration_opens_at__lte=now,
            registration_closes_at__gte=now,
        )
        .annotate(first_day=Min("days__date"))
        .prefetch_related("days")
        .order_by("first_day", "name")
    )
    next_assignments = (
        Assignment.objects.filter(
            registration__member__family=family,
            registration__status=Registration.Status.ACTIVE,
            station_slot__event_day__date__gte=timezone.localdate(),
            status=Assignment.Status.CONFIRMED,
        )
        .select_related(
            "registration__member",
            "station_slot__station",
            "station_slot__event_day__event",
        )
        .order_by("station_slot__event_day__date", "period")[:8]
    )
    return render(
        request,
        "volunteers/family/dashboard.html",
        {
            "family": family,
            "members": members,
            "open_events": open_events,
            "next_assignments": next_assignments,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def family_account(request):
    family = _family_for(request)
    form = FamilyAccountForm(request.POST or None, family=family)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Les informations de votre compte ont été enregistrées.")
        return redirect("family_account")
    return render(
        request,
        "volunteers/family/account.html",
        {"family": family, "form": form},
    )


@login_required
def member_list(request):
    family = _family_for(request)
    members = _members_with_current_age(
        family.members.all().order_by("archived_at", "last_name", "first_name")
    )
    return render(
        request,
        "volunteers/family/member_list.html",
        {"family": family, "members": members},
    )


@login_required
@require_http_methods(["GET", "POST"])
def member_create(request):
    family = _family_for(request)
    form = MemberForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        member = form.save(commit=False)
        member.family = family
        member.save()
        messages.success(request, f"{member.full_name} a été ajouté(e) à votre famille.")
        return redirect("member_list")
    return render(
        request,
        "volunteers/family/member_form.html",
        {"form": form, "member": None, "object": None},
    )


@login_required
@require_http_methods(["GET", "POST"])
def member_update(request, member_id):
    family = _family_for(request)
    member = get_object_or_404(
        Member,
        pk=member_id,
        family=family,
        archived_at__isnull=True,
    )
    form = MemberForm(request.POST or None, instance=member)
    if request.method == "POST" and form.is_valid():
        member = form.save()
        messages.success(request, f"Le profil de {member.full_name} a été enregistré.")
        return redirect("member_list")
    return render(
        request,
        "volunteers/family/member_form.html",
        {"form": form, "member": member, "object": member},
    )


@login_required
@require_http_methods(["GET", "POST"])
def member_archive(request, member_id):
    family = _family_for(request)
    member = get_object_or_404(
        Member,
        pk=member_id,
        family=family,
        archived_at__isnull=True,
    )
    if request.method == "POST":
        member.archived_at = timezone.now()
        member.save(update_fields=["archived_at", "updated_at"])
        messages.success(
            request,
            f"{member.full_name} a été archivé(e). Son historique est conservé.",
        )
        return redirect("member_list")
    return render(
        request,
        "volunteers/family/member_confirm_archive.html",
        {"member": member, "object": member},
    )


@login_required
def open_events(request):
    family = _family_for(request)
    now = timezone.now()
    events = (
        Event.objects.filter(status=Event.Status.PUBLISHED)
        .annotate(first_day=Min("days__date"))
        .prefetch_related("days")
        .order_by("first_day", "name")
    )
    return render(
        request,
        "volunteers/family/event_list.html",
        {"family": family, "events": events, "now": now},
    )


@login_required
def family_event_detail(request, event_id):
    family = _family_for(request)
    event = get_object_or_404(
        Event.objects.prefetch_related("days"),
        pk=event_id,
        status=Event.Status.PUBLISHED,
    )
    members = list(family.members.filter(archived_at__isnull=True))
    first_day = event.days.order_by("date").first()
    for member in members:
        member.age = member.age_on(first_day.date if first_day else timezone.localdate())
    registrations = list(
        Registration.objects.filter(
            member__family=family,
            event_day__event=event,
            status=Registration.Status.ACTIVE,
        )
        .select_related("member", "event_day", "preferred_station", "meal_order")
        .order_by("event_day__date", "member__last_name", "member__first_name")
    )
    registrations_by_member_and_day = {
        (registration.member_id, registration.event_day_id): registration
        for registration in registrations
    }
    meal_orders = {
        registration.pk: getattr(registration, "meal_order", None)
        for registration in registrations
    }
    balance = _balance_for_display(family_event_balance(family=family, event=event))
    return render(
        request,
        "volunteers/family/event_detail.html",
        {
            "family": family,
            "event": event,
            "days": event.days.all(),
            "members": members,
            "registrations": registrations,
            "registrations_by_member_and_day": registrations_by_member_and_day,
            "meal_orders": meal_orders,
            "balance": balance,
            "registration_is_open": event.registration_is_open(),
            "registration_not_started": timezone.now() < event.registration_opens_at,
            "meal_orders_are_open": event.meal_orders_are_open(),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def family_registration_edit(request, event_day_id, member_id):
    family = _family_for(request)
    member = get_object_or_404(
        Member,
        pk=member_id,
        family=family,
        archived_at__isnull=True,
    )
    event_day = get_object_or_404(
        EventDay.objects.select_related("event"),
        pk=event_day_id,
        event__status=Event.Status.PUBLISHED,
    )
    registration = Registration.objects.filter(member=member, event_day=event_day).first()
    form = FamilyRegistrationForm(
        request.POST or None,
        event_day=event_day,
        registration=registration,
    )
    if request.method == "POST" and form.is_valid():
        try:
            registration = save_registration(
                member=member,
                event_day=event_day,
                availability=form.cleaned_data["availability"],
                preferred_station=form.cleaned_data["preferred_station"],
                note=form.cleaned_data["note"],
                actor=request.user,
                actor_is_organizer=False,
            )
        except ValidationError as error:
            _report_validation_error(form, error)
        else:
            messages.success(
                request,
                "La disponibilité a été enregistrée. Le poste indiqué reste un souhait, "
                "pas une affectation confirmée.",
            )
            return redirect("family_event_detail", event_id=event_day.event_id)
    return render(
        request,
        "volunteers/family/registration_form.html",
        {
            "form": form,
            "member": member,
            "event_day": event_day,
            "event": event_day.event,
            "registration": registration,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def family_registration_cancel(request, registration_id):
    family = _family_for(request)
    registration = get_object_or_404(
        Registration.objects.select_related("member", "event_day__event"),
        pk=registration_id,
        member__family=family,
        status=Registration.Status.ACTIVE,
    )
    if request.method == "POST":
        try:
            cancel_registration(
                registration=registration,
                actor=request.user,
                actor_is_organizer=False,
            )
        except ValidationError as error:
            for message in getattr(error, "messages", [str(error)]):
                messages.error(request, message)
        else:
            messages.success(
                request,
                "L'inscription a été annulée. Toute affectation devenue incompatible "
                "a été retirée.",
            )
        return redirect("family_event_detail", event_id=registration.event_day.event_id)
    return render(
        request,
        "volunteers/family/registration_confirm_cancel.html",
        {
            "registration": registration,
            "object": registration,
            "has_assignments": registration.assignments.exclude(
                status=Assignment.Status.CANCELLED
            ).exists(),
            "event": registration.event_day.event,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def family_meal_edit(request, registration_id):
    family = _family_for(request)
    registration = get_object_or_404(
        Registration.objects.select_related("member", "event_day__event"),
        pk=registration_id,
        member__family=family,
        status=Registration.Status.ACTIVE,
        event_day__event__status=Event.Status.PUBLISHED,
    )
    try:
        meal_order = registration.meal_order
    except Registration.meal_order.RelatedObjectDoesNotExist:
        meal_order = None
    form = FamilyMealForm(
        request.POST or None,
        registration=registration,
        meal_order=meal_order,
    )
    if request.method == "POST" and form.is_valid():
        try:
            save_meal_reservation(
                registration=registration,
                menu=form.cleaned_data["menu"],
                drink=form.cleaned_data["beverage"],
                no_meal=form.cleaned_data["no_meal"],
                actor=request.user,
                actor_is_organizer=False,
            )
        except ValidationError as error:
            _report_validation_error(form, error)
        else:
            messages.success(request, "Le choix de repas a été enregistré.")
            return redirect("family_event_detail", event_id=registration.event_day.event_id)
    return render(
        request,
        "volunteers/family/meal_form.html",
        {
            "form": form,
            "registration": registration,
            "meal_order": meal_order,
            "event": registration.event_day.event,
            "locked": not registration.event_day.event.meal_orders_are_open(),
            "total_display": (
                f"{meal_order.total_surcharge_cents / 100:.2f}".replace(".", ",")
                if meal_order
                else "0,00"
            ),
        },
    )


@login_required
def family_assignments(request):
    family = _family_for(request)
    assignments = (
        Assignment.objects.filter(
            registration__member__family=family,
            registration__status=Registration.Status.ACTIVE,
            status=Assignment.Status.CONFIRMED,
        )
        .select_related(
            "registration__member",
            "station_slot__station",
            "station_slot__event_day__event",
        )
        .order_by("station_slot__event_day__date", "period", "registration__member__last_name")
    )
    return render(
        request,
        "volunteers/family/assignments.html",
        {"family": family, "assignments": assignments},
    )


@login_required
def family_history(request):
    family = _family_for(request)
    members = _members_with_current_age(family.members.all())
    attendances = list(
        Attendance.objects.filter(member__family=family)
        .select_related("member", "event_day__event")
        .order_by("-event_day__date", "member__last_name", "member__first_name")
    )
    confirmed_assignments = Prefetch(
        "assignments",
        queryset=Assignment.objects.filter(status=Assignment.Status.CONFIRMED).select_related(
            "station_slot__station"
        ),
        to_attr="confirmed_assignments",
    )
    registrations_query = Registration.objects.filter(
        member__family=family,
        event_day__date__lte=timezone.localdate(),
    )
    years = sorted(
        {date.year for date in registrations_query.values_list("event_day__date", flat=True)},
        reverse=True,
    )
    selected_member = request.GET.get("member", "")
    if selected_member in {str(member.pk) for member in members}:
        registrations_query = registrations_query.filter(member_id=selected_member)
    selected_year = request.GET.get("year", "")
    if selected_year.isdigit() and int(selected_year) in years:
        registrations_query = registrations_query.filter(event_day__date__year=int(selected_year))
    registrations = list(
        registrations_query
        .select_related("member", "event_day__event", "preferred_station")
        .prefetch_related(confirmed_assignments)
        .order_by("-event_day__date", "member__last_name", "member__first_name")
    )
    attendance_by_member_and_day = {
        (attendance.member_id, attendance.event_day_id): attendance for attendance in attendances
    }
    effective_participations = defaultdict(int)
    for attendance in attendances:
        if attendance.status == Attendance.Status.PRESENT:
            effective_participations[attendance.member_id] += 1
    for registration in registrations:
        registration.recorded_attendance = attendance_by_member_and_day.get(
            (registration.member_id, registration.event_day_id)
        )
        registration.attendance_status = (
            registration.recorded_attendance.status
            if registration.recorded_attendance
            else Attendance.Status.UNKNOWN
        )
        registration.event = registration.event_day.event
        registration.assignment = (
            registration.confirmed_assignments[0]
            if registration.confirmed_assignments
            else None
        )
        registration.role = (
            registration.assignment.station_slot.station if registration.assignment else None
        )
    return render(
        request,
        "volunteers/family/history.html",
        {
            "family": family,
            "members": members,
            "registrations": registrations,
            "history": registrations,
            "attendances": attendances,
            "effective_participations": dict(effective_participations),
            "years": years,
        },
    )

