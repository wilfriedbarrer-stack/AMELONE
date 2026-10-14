from django.contrib.auth.decorators import login_required
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .models import Event


def home(request):
    events = (
        Event.objects.filter(status=Event.Status.PUBLISHED)
        .prefetch_related("days")
        .order_by("days__date", "name")
        .distinct()
    )
    return render(request, "home.html", {"events": events})


def health(request):
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return JsonResponse({"status": "ok", "database": "ok"})


@login_required
def dashboard(request):
    if request.user.is_staff or request.user.is_organizer:
        return redirect("organizer_dashboard")
    return redirect("family_dashboard")

