import base64
from functools import wraps
from io import BytesIO

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from PIL import Image, UnidentifiedImageError

from .models import Event, Family, SiteBranding, User
from .views_organizer import organizer_required


def administrator_required(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_active or not request.user.is_superuser:
            raise PermissionDenied("Les droits administrateur sont nécessaires.")
        return view(request, *args, **kwargs)
    return wrapped


class ProfileForm(forms.ModelForm):
    role = forms.ChoiceField(label="Droits", choices=[
        ("family", "Famille"), ("organizer", "Organisateur"),
        ("administrator", "Administrateur (tous les droits)"),
    ])

    class Meta:
        model = User
        fields = ("email", "first_name", "last_name", "phone", "is_active")
        labels = {"is_active": "Compte actif"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        user = self.instance
        self.fields["role"].initial = (
            "administrator" if user.is_superuser else
            "organizer" if user.is_organizer or user.is_staff else "family"
        )

    def clean_email(self):
        email = self.cleaned_data["email"].lower().strip()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Cette adresse e-mail est déjà utilisée.")
        return email


class LogoForm(forms.Form):
    logo = forms.FileField(label="Logo de l’association", help_text="PNG, JPEG ou WebP, 1 Mo maximum.")

    def clean_logo(self):
        upload = self.cleaned_data["logo"]
        if upload.size > 1024 * 1024:
            raise ValidationError("Le logo doit faire moins de 1 Mo.")
        data = upload.read()
        try:
            with Image.open(BytesIO(data)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"}:
                    raise ValidationError("Choisissez une image PNG, JPEG ou WebP.")
                if image.width * image.height > 16_000_000:
                    raise ValidationError("L’image est trop grande (16 millions de pixels maximum).")
                mime = Image.MIME[image.format]
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
            raise ValidationError("Le fichier n’est pas une image valide.") from error
        return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


@organizer_required
def administration_dashboard(request):
    events = Event.objects.prefetch_related("days__menus", "stations").order_by("name")
    return render(request, "administration/dashboard.html", {"events": events})


@administrator_required
def profile_list(request):
    return render(request, "administration/profiles.html", {"profiles": User.objects.all()})


@administrator_required
def profile_update(request, user_id):
    if request.method == "POST":
        with transaction.atomic():
            # Serialize role edits, including concurrent attempts to remove the last administrator.
            users = list(User.objects.select_for_update().order_by("pk"))
            actor = next((user for user in users if user.pk == request.user.pk), None)
            if actor is None or not actor.is_active or not actor.is_superuser:
                raise PermissionDenied("Les droits administrateur sont nécessaires.")
            profile = next((user for user in users if user.pk == user_id), None)
            if profile is None:
                profile = get_object_or_404(User, pk=user_id)
            form = ProfileForm(request.POST, instance=profile)
            if form.is_valid():
                role = form.cleaned_data["role"]
                keeps_admin = role == "administrator" and form.cleaned_data["is_active"]
                if profile.pk == request.user.pk and not keeps_admin:
                    form.add_error(None, "Vous ne pouvez pas retirer vos propres droits administrateur ou désactiver votre compte.")
                elif profile.is_superuser and not keeps_admin and not any(
                    user.pk != profile.pk and user.is_superuser and user.is_active for user in users
                ):
                    form.add_error(None, "Il faut conserver au moins un administrateur actif.")
                else:
                    profile = form.save(commit=False)
                    profile.is_superuser = role == "administrator"
                    profile.is_staff = role == "administrator"
                    profile.is_organizer = role in {"administrator", "organizer"}
                    # A role change must remove any previously delegated Django permissions.
                    profile.save()
                    if role == "family":
                        Family.objects.get_or_create(account=profile)
                    profile.groups.clear()
                    profile.user_permissions.clear()
                    messages.success(request, "Le profil et ses droits ont été enregistrés.")
                    return redirect("profile_list")
    else:
        profile = get_object_or_404(User, pk=user_id)
        form = ProfileForm(instance=profile)
    return render(request, "administration/profile_form.html", {"form": form, "profile": profile})


@administrator_required
def branding_update(request):
    form = LogoForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        SiteBranding.objects.update_or_create(pk=1, defaults={"logo_data": form.cleaned_data["logo"]})
        messages.success(request, "Le logo est maintenant visible sur toutes les pages.")
        return redirect("administration_dashboard")
    return render(request, "administration/logo_form.html", {"form": form})
