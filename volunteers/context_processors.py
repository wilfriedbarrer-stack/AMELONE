from django.conf import settings

from .models import SiteBranding
from .registration_attention import get_registration_attention


def request_assignment_attention(request):
    """Share one read-only summary across the view and navigation rendering."""
    if not hasattr(request, "_assignment_attention"):
        user = request.user
        can_manage = user.is_authenticated and user.is_active and (
            user.is_staff or getattr(user, "is_organizer", False)
        )
        request._assignment_attention = get_registration_attention() if can_manage else {
            "pending_count": 0, "unassigned_count": 0, "partial_count": 0,
            "draft_registration_count": 0, "events": [],
        }
    return request._assignment_attention


def _navigation_section(request):
    """A nested page keeps its parent section visible in the navigation."""
    name = getattr(getattr(request, "resolver_match", None), "url_name", "") or ""
    if name == "public_planning":
        return "planning"
    if name in {"personal_participation", "personal_profile", "personal_registration"}:
        return "participation"
    if name == "administrator_families" or name.startswith("administrator_family") or name.startswith("administrator_member"):
        return "families"
    if name in {"administration_dashboard", "profile_list", "profile_update", "branding_update", "payment_settings"}:
        return "settings"
    if name.startswith("shared_catalogue") or name in {"menu_update", "beverage_update", "catalogue_template"}:
        return "catalogue"
    if name == "organizer_dashboard" or name == "annual_tracking":
        return "management"
    if request.path.startswith("/organisateur/"):
        return "events"
    if name in {"family_dashboard", "dashboard"}:
        return "family_home"
    if name.startswith("member_"):
        return "members"
    if name in {"family_assignments", "family_history"}:
        return "family_assignments"
    if name == "family_account":
        return "account"
    if name in {"public_events", "open_events", "family_event_detail", "family_registration_edit", "family_registration_cancel", "family_meal_edit"}:
        return "family_events"
    return "home" if name == "home" else ""


def navigation_context(request):
    attention = request_assignment_attention(request)
    return {
        "assignment_attention": attention,
        "pending_assignment_count": attention["pending_count"],
        "navigation_section": _navigation_section(request),
        "debug_mode": settings.DEBUG,
        "association_logo": SiteBranding.objects.filter(pk=1).values_list("logo_data", flat=True).first() or "",
        "is_administrator": request.user.is_authenticated and request.user.is_active and request.user.is_superuser,
        "can_self_participate": bool(request.user.is_authenticated and request.user.is_active and (
            request.user.is_superuser or request.user.is_competition_official
        )),
        "email_delivery_available": settings.EMAIL_ENABLED or settings.DEBUG,
        "email_enabled": settings.EMAIL_ENABLED,
        "is_organizer": bool(
            request.user.is_authenticated
            and request.user.is_active
            and (request.user.is_staff or getattr(request.user, "is_organizer", False))
        )
    }

