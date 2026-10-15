"""Consultation privée des familles et habilitation individuelle des officiels."""

from uuid import UUID

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .models import Family, Member, User
from .views_administration import administrator_required


class MemberOfficialForm(forms.Form):
    member_id = forms.UUIDField(widget=forms.HiddenInput)
    is_competition_official = forms.BooleanField(
        label="Officiel de compétition",
        required=False,
        help_text="Licence et formation à jour nécessaires.",
    )


@administrator_required
def administrator_families(request):
    families = Family.objects.select_related("account").annotate(
        member_count=Count("members"),
        active_member_count=Count("members", filter=Q(members__archived_at__isnull=True)),
        official_count=Count("members", filter=Q(members__is_competition_official=True)),
    )
    return render(request, "administration/families.html", {"families": families})


@administrator_required
@require_http_methods(["GET", "POST"])
def administrator_family_detail(request, family_id):
    family = get_object_or_404(Family.objects.select_related("account"), pk=family_id)
    bound_form = None
    selected_id = None
    if request.method == "POST":
        try:
            selected_id = UUID(request.POST.get("member_id", ""))
        except (TypeError, ValueError, AttributeError) as error:
            raise Http404("Ce membre est introuvable.") from error
        # Read the optional account link before taking locks. All account locks precede
        # the member lock, in the same order as the profile rights editor.
        selected = get_object_or_404(Member, pk=selected_id, family_id=family.pk)
        bound_form = MemberOfficialForm({
            "member_id": str(selected_id),
            "is_competition_official": request.POST.get("is_competition_official", ""),
        })
        if bound_form.is_valid():
            try:
                with transaction.atomic():
                    user_ids = {request.user.pk}
                    if selected.user_id:
                        user_ids.add(selected.user_id)
                    users = {
                        user.pk: user for user in User.objects.select_for_update()
                        .filter(pk__in=user_ids).order_by("pk")
                    }
                    actor = users.get(request.user.pk)
                    if actor is None or not actor.is_active or not actor.is_superuser:
                        raise PermissionDenied("Les droits administrateur sont nécessaires.")
                    member = get_object_or_404(
                        Member.objects.select_for_update(), pk=selected_id, family_id=family.pk,
                    )
                    if member.user_id != selected.user_id:
                        raise ValidationError("Le compte lié à ce membre a changé. Actualisez cette page.")
                    member.is_competition_official = bound_form.cleaned_data["is_competition_official"]
                    member.full_clean()
                    member.save(update_fields=["is_competition_official", "updated_at"])
                    if member.user_id:
                        linked_user = users[member.user_id]
                        linked_user.is_competition_official = member.is_competition_official
                        linked_user.save(update_fields=["is_competition_official"])
                messages.success(request, f"La fonction de {member.full_name} a été enregistrée.")
                return redirect("administrator_family_detail", family_id=family.pk)
            except ValidationError as error:
                for message in error.messages:
                    bound_form.add_error(None, message)

    members = list(family.members.select_related("user"))
    for member in members:
        member.current_age = member.age_on()
        if member.pk == selected_id:
            member.official_form = bound_form
        else:
            member.official_form = MemberOfficialForm(initial={
                "member_id": member.pk,
                "is_competition_official": member.is_competition_official,
            })
        # The checkbox and its help text need a distinct accessible identifier per row.
        member.official_form.fields["is_competition_official"].widget.attrs.update({
            "id": f"official-{member.pk}",
            "aria-describedby": f"official-help-{member.pk}",
        })
    return render(request, "administration/family_detail.html", {
        "family": family, "members": members,
    })
