"""Read-only registration coverage indicators for organizers."""

from django.db.models import BooleanField, Case, Count, Exists, F, OuterRef, Q, Value, When
from django.utils import timezone

from .models import Assignment, Event, Registration, StationSlot


def _actionable_registrations(today):
    return Q(
        status=Registration.Status.ACTIVE,
        event_day__date__gte=today,
    ) & ~Q(event_day__event__status=Event.Status.ARCHIVED)


def annotate_registration_attention(registrations, *, today=None):
    """Add coverage flags without joining or duplicating registration rows.

    A draft already reserves a period, so it covers the availability just as a
    confirmed assignment does. A full-day presence still needs attention when
    only one half-day is assigned. Historical or cancelled registrations never
    require a new assignment in these indicators.
    """
    today = today if today is not None else timezone.localdate()
    assignments = Assignment.objects.filter(
        registration_id=OuterRef("pk"),
        status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
    )
    registrations = registrations.annotate(
        has_active_assignment=Exists(assignments),
        has_draft_assignment=Exists(assignments.filter(status=Assignment.Status.DRAFT)),
        has_full_day_assignment=Exists(assignments.filter(period=StationSlot.Period.FULL_DAY)),
        has_morning_assignment=Exists(assignments.filter(period=StationSlot.Period.MORNING)),
        has_afternoon_assignment=Exists(assignments.filter(period=StationSlot.Period.AFTERNOON)),
    )
    covered = (
        Q(has_full_day_assignment=True)
        | Q(
            availability=Registration.Availability.FULL_DAY,
            has_morning_assignment=True,
            has_afternoon_assignment=True,
        )
        | Q(availability=Registration.Availability.MORNING, has_morning_assignment=True)
        | Q(availability=Registration.Availability.AFTERNOON, has_afternoon_assignment=True)
    )
    return registrations.annotate(
        needs_assignment=Case(
            When(_actionable_registrations(today) & ~covered, then=Value(True)),
            default=Value(False),
            output_field=BooleanField(),
        ),
    )


def get_registration_attention(*, event_id=None, today=None):
    """Return global and per-event counts, in registration-days rather than people.

    ``pending_count`` includes both unassigned and partially assigned presences.
    ``draft_registration_count`` counts presences with one or more drafts; it is
    separate from pending coverage and does not count individual assignments.
    Only active registrations for today or later in nonarchived events count.
    """
    today = today if today is not None else timezone.localdate()
    registrations = Registration.objects.filter(_actionable_registrations(today))
    if event_id is not None:
        registrations = registrations.filter(event_day__event_id=event_id)
    registrations = annotate_registration_attention(registrations, today=today)
    events = list(
        registrations.values(
            event_id=F("event_day__event_id"), event_name=F("event_day__event__name")
        ).annotate(
            pending_count=Count("pk", filter=Q(needs_assignment=True)),
            unassigned_count=Count("pk", filter=Q(needs_assignment=True, has_active_assignment=False)),
            partial_count=Count("pk", filter=Q(needs_assignment=True, has_active_assignment=True)),
            draft_registration_count=Count("pk", filter=Q(has_draft_assignment=True)),
        ).order_by("event_name", "event_id")
    )
    counts = {
        key: sum(event[key] for event in events)
        for key in ("pending_count", "unassigned_count", "partial_count", "draft_registration_count")
    }
    return {**counts, "events": events}
