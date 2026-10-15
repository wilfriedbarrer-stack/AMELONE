"""Consultation privée des familles et habilitation individuelle des officiels."""

from uuid import UUID

from django import forms
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from .forms_family_administration import AdministratorFamilyAccountForm, AdministratorMemberForm
from .models import Family, Member, User
from .views_administration import administrator_required
from .views_family import _report_validation_error


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


def _locked_administrator_accounts(request, *account_ids):
    """Lock accounts before the family/member and reject a concurrent role change."""
    users = {
        user.pk: user for user in User.objects.select_for_update()
        .filter(pk__in={request.user.pk, *account_ids}).order_by("pk")
    }
    actor = users.get(request.user.pk)
    if actor is None or not actor.is_active or not actor.is_superuser:
        raise PermissionDenied("Les droits administrateur sont nécessaires.")
    return users


@administrator_required
@require_http_methods(["GET", "POST"])
def administrator_member_update(request, family_id, member_id):
    family = get_object_or_404(Family.objects.select_related("account"), pk=family_id)
    member = get_object_or_404(Member, pk=member_id, family_id=family.pk)
    form = AdministratorMemberForm(request.POST if request.method == "POST" else None, instance=member)
    if request.method == "POST":
        try:
            with transaction.atomic():
                linked_account_id = member.user_id
                users = _locked_administrator_accounts(request, *(
                    (linked_account_id,) if linked_account_id else ()
                ))
                member = get_object_or_404(
                    Member.objects.select_for_update(of=("self",)),
                    pk=member_id, family_id=family.pk,
                )
                if member.user_id != linked_account_id:
                    raise ValidationError("Le compte lié à ce membre a changé. Actualisez cette page.")
                # Rebuild and validate against the locked row, preserving account,
                # family and archive status even if forged values were submitted.
                form = AdministratorMemberForm(request.POST, instance=member)
                if form.is_valid():
                    member = form.save()
                    if member.user_id:
                        account = users[member.user_id]
                        account.is_competition_official = member.is_competition_official
                        account.save(update_fields=["is_competition_official"])
                    messages.success(request, f"Le profil de {member.full_name} a été enregistré.")
                    return redirect("administrator_family_detail", family_id=family.pk)
        except ValidationError as error:
            _report_validation_error(form, error)
    return render(request, "administration/member_form.html", {
        "family": family, "member": member, "form": form,
    })


@administrator_required
@require_http_methods(["GET", "POST"])
def administrator_family_update(request, family_id):
    family = get_object_or_404(Family.objects.select_related("account"), pk=family_id)
    form = AdministratorFamilyAccountForm(
        request.POST if request.method == "POST" else None, family=family,
    )
    if request.method == "POST":
        try:
            with transaction.atomic():
                account_id = family.account_id
                users = _locked_administrator_accounts(request, account_id)
                family = get_object_or_404(
                    Family.objects.select_for_update(of=("self",)), pk=family.pk,
                )
                if family.account_id != account_id:
                    raise ValidationError("Le compte responsable a changé. Actualisez cette page.")
                family.account = users[account_id]
                form = AdministratorFamilyAccountForm(request.POST, family=family)
                if form.is_valid():
                    account = family.account
                    for field in ("first_name", "last_name", "email", "phone"):
                        setattr(account, field, form.cleaned_data[field].strip())
                    family.name = form.cleaned_data["family_name"].strip()
                    account.full_clean()
                    family.full_clean()
                    form.save()
                    messages.success(request, "La famille et les coordonnées du responsable ont été enregistrées.")
                    return redirect("administrator_family_detail", family_id=family.pk)
        except ValidationError as error:
            _report_validation_error(form, error)
        except IntegrityError:
            # A concurrent account creation/edit can win the database's email
            # uniqueness constraint after clean_email; the whole edit rolls back.
            form.add_error("email", "Cette adresse e-mail est déjà utilisée.")
    return render(request, "administration/family_form.html", {"family": family, "form": form})
