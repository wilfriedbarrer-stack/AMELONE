from django.conf import settings


def navigation_context(request):
    return {
        "debug_mode": settings.DEBUG,
        "email_delivery_available": settings.EMAIL_ENABLED or settings.DEBUG,
        "email_enabled": settings.EMAIL_ENABLED,
        "is_organizer": bool(
            request.user.is_authenticated
            and (request.user.is_staff or getattr(request.user, "is_organizer", False))
        )
    }

