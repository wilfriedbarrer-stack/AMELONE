from django.conf import settings

from .models import SiteBranding


def navigation_context(request):
    return {
        "debug_mode": settings.DEBUG,
        "association_logo": SiteBranding.objects.filter(pk=1).values_list("logo_data", flat=True).first() or "",
        "is_administrator": request.user.is_authenticated and request.user.is_active and request.user.is_superuser,
        "email_delivery_available": settings.EMAIL_ENABLED or settings.DEBUG,
        "email_enabled": settings.EMAIL_ENABLED,
        "is_organizer": bool(
            request.user.is_authenticated
            and (request.user.is_staff or getattr(request.user, "is_organizer", False))
        )
    }

