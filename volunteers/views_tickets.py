"""Tickets nominatifs de restauration, réservés à l’organisation."""

from django import forms
from django.http import Http404
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_GET

from .forms_organizer import OrganizerFormMixin
from .models import Event, EventDay, MealOrder, Member, Menu, Registration
from .views_organizer import organizer_required


def _ticket_family_name(member):
    # Family.display_name can fall back to the responsible account's email.
    # Tickets deliberately use an identity-only fallback instead.
    return member.family.name.strip() or f"Famille {member.last_name}".strip()


class TicketMemberChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, member):
        return f"{member.full_name} — {_ticket_family_name(member)}"


class MealTicketFilterForm(OrganizerFormMixin, forms.Form):
    day = forms.ModelChoiceField(
        label="Journée", queryset=EventDay.objects.none(), required=False,
        empty_label="Toutes les journées",
    )
    member = TicketMemberChoiceField(
        label="Participant", queryset=Member.objects.none(), required=False,
        empty_label="Tous les participants",
    )

    def __init__(self, *args, event, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["day"].queryset = event.days.order_by("date")
        self.fields["member"].queryset = Member.objects.filter(
            registrations__event_day__event=event,
            registrations__status=Registration.Status.ACTIVE,
        ).select_related("family").distinct().order_by("last_name", "first_name", "pk")


def _ticket_row(order, event):
    registration = order.registration
    member = registration.member
    morning = order.morning_snack
    afternoon = order.afternoon_snack
    return {
        "member_id": member.pk,
        "first_name": member.first_name,
        "last_name": member.last_name,
        "full_name": member.full_name,
        "family_name": _ticket_family_name(member),
        "event_name": event.name,
        "event_date": registration.event_day.date,
        "meal_type_label": (
            "Collation" if order.menu.meal_type == Menu.MealType.SNACK else "Repas complet"
        ),
        "menu_name": order.menu.name,
        "menu_description": order.menu.description,
        "morning_snack_name": morning.name if morning else "",
        "morning_snack_description": morning.description if morning else "",
        "afternoon_snack_name": afternoon.name if afternoon else "",
        "afternoon_snack_description": afternoon.description if afternoon else "",
        "beverage_name": order.beverage.name if order.beverage else "Sans boisson",
        "reference": f"REP-{order.pk.hex[:12].upper()}",
        "official_exemption": order.official_exemption_applied,
        "is_official": order.official_exemption_applied,
    }


@organizer_required
@require_GET
def meal_tickets(request, event_id):
    event = get_object_or_404(Event, pk=event_id)
    form = MealTicketFilterForm(request.GET or None, event=event)
    selected_day = member_filter = None
    if form.is_bound:
        if not form.is_valid():
            raise Http404("La journée ou le participant n’appartient pas à cet événement.")
        selected_day = form.cleaned_data["day"]
        member_filter = form.cleaned_data["member"]
    orders = MealOrder.objects.filter(
        registration__event_day__event=event,
        registration__status=Registration.Status.ACTIVE,
        wants_meal=True,
        menu__isnull=False,
    )
    if selected_day:
        orders = orders.filter(registration__event_day=selected_day)
    if member_filter:
        orders = orders.filter(registration__member=member_filter)
    orders = orders.select_related(
        "registration__member__family", "registration__event_day", "menu", "beverage",
        "morning_snack", "afternoon_snack",
    ).order_by(
        "registration__event_day__date", "registration__member__last_name",
        "registration__member__first_name", "registration__member_id", "pk",
    )
    tickets = [_ticket_row(order, event) for order in orders]
    sheets = [tickets[index:index + 6] for index in range(0, len(tickets), 6)]
    for sheet in sheets:
        sheet.extend([None] * (6 - len(sheet)))
    return render(request, "volunteers/organizer/meal_tickets.html", {
        "event": event, "days": form.fields["day"].queryset, "selected_day": selected_day,
        "tickets": tickets, "member_filter": member_filter,
        "members": form.fields["member"].queryset, "form": form,
        "sheets": sheets,
        "print_view": request.GET.get("print") == "1",
    })
