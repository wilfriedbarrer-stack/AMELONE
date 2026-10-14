from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from datetime import date, datetime

from django.conf import settings
from django.contrib.auth.base_user import BaseUserManager
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Gestionnaire d'utilisateurs identifiés par leur adresse électronique."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra_fields):
        if not email:
            raise ValueError("Une adresse e-mail est obligatoire.")
        email = self.normalize_email(email).lower()
        user = self.model(email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra_fields)

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_organizer", True)
        if extra_fields.get("is_staff") is not True:
            raise ValueError("Un superutilisateur doit avoir is_staff=True.")
        if extra_fields.get("is_superuser") is not True:
            raise ValueError("Un superutilisateur doit avoir is_superuser=True.")
        return self._create_user(email, password, **extra_fields)


class User(AbstractUser):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    username = None
    email = models.EmailField(_("adresse e-mail"), unique=True)
    phone = models.CharField(_("téléphone"), max_length=32, blank=True)
    is_organizer = models.BooleanField(_("organisateur"), default=False)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = _("utilisateur")
        verbose_name_plural = _("utilisateurs")
        ordering = ("last_name", "first_name", "email")

    def save(self, *args, **kwargs):
        self.email = self.__class__.objects.normalize_email(self.email).lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return self.get_full_name().strip() or self.email


class UUIDTimeStampedModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(_("créé le"), auto_now_add=True)
    updated_at = models.DateTimeField(_("modifié le"), auto_now=True)

    class Meta:
        abstract = True


class Family(UUIDTimeStampedModel):
    account = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("compte responsable"),
        on_delete=models.PROTECT,
        related_name="family",
    )
    name = models.CharField(_("nom de la famille"), max_length=150, blank=True)

    class Meta:
        verbose_name = _("famille")
        verbose_name_plural = _("familles")
        ordering = ("name", "account__last_name", "account__first_name")

    @property
    def display_name(self):
        return self.name or self.account.get_full_name().strip() or self.account.email

    def __str__(self):
        return self.display_name


class Member(UUIDTimeStampedModel):
    family = models.ForeignKey(
        Family,
        verbose_name=_("famille"),
        on_delete=models.PROTECT,
        related_name="members",
    )
    first_name = models.CharField(_("prénom"), max_length=150)
    last_name = models.CharField(_("nom"), max_length=150)
    birth_date = models.DateField(_("date de naissance"))
    archived_at = models.DateTimeField(_("archivé le"), null=True, blank=True)

    class Meta:
        verbose_name = _("membre")
        verbose_name_plural = _("membres")
        ordering = ("last_name", "first_name", "birth_date")
        indexes = [
            models.Index(fields=("family", "archived_at"), name="member_family_arch_idx"),
            models.Index(fields=("last_name", "first_name"), name="member_name_idx"),
        ]

    def clean(self):
        super().clean()
        if self.birth_date and self.birth_date > timezone.localdate():
            raise ValidationError({"birth_date": _("La date de naissance ne peut pas être future.")})

    @property
    def is_archived(self):
        return self.archived_at is not None

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def birthday_in_year(self, year: int) -> date:
        """Retourne l'anniversaire légal retenu; le 29/02 devient le 28/02."""

        day = self.birth_date.day
        if self.birth_date.month == 2 and day == 29 and not calendar.isleap(year):
            day = 28
        return date(year, self.birth_date.month, day)

    def age_on(self, on_date: date | datetime | None = None) -> int:
        if on_date is None:
            on_date = timezone.localdate()
        elif isinstance(on_date, datetime):
            on_date = timezone.localtime(on_date).date() if timezone.is_aware(on_date) else on_date.date()
        birthday = self.birthday_in_year(on_date.year)
        return on_date.year - self.birth_date.year - (on_date < birthday)

    def has_birthday_on(self, on_date: date) -> bool:
        return self.birthday_in_year(on_date.year) == on_date

    def __str__(self):
        return self.full_name


class Event(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Brouillon")
        PUBLISHED = "published", _("Publié")
        ARCHIVED = "archived", _("Archivé")

    name = models.CharField(_("nom"), max_length=200)
    description = models.TextField(_("description"), blank=True)
    location = models.CharField(_("lieu"), max_length=255)
    registration_opens_at = models.DateTimeField(_("ouverture des inscriptions"))
    registration_closes_at = models.DateTimeField(_("clôture des inscriptions"))
    meal_orders_close_at = models.DateTimeField(_("clôture des repas"))
    sumup_payment_url = models.URLField(_("lien de paiement SumUp"), blank=True)
    general_instructions = models.TextField(_("consignes générales"), blank=True)
    status = models.CharField(
        _("statut"), max_length=16, choices=Status.choices, default=Status.DRAFT
    )

    class Meta:
        verbose_name = _("événement")
        verbose_name_plural = _("événements")
        ordering = ("-registration_opens_at", "name")
        constraints = [
            models.CheckConstraint(
                condition=Q(registration_closes_at__gt=F("registration_opens_at")),
                name="event_registration_window_valid",
            )
        ]
        indexes = [models.Index(fields=("status", "registration_closes_at"), name="event_status_close_idx")]

    def clean(self):
        super().clean()
        if (
            self.registration_opens_at
            and self.registration_closes_at
            and self.registration_opens_at >= self.registration_closes_at
        ):
            raise ValidationError(
                {"registration_closes_at": _("La clôture doit suivre l'ouverture des inscriptions.")}
            )

    def registration_is_open(self, at=None):
        at = at or timezone.now()
        return (
            self.status == self.Status.PUBLISHED
            and self.registration_opens_at <= at <= self.registration_closes_at
        )

    def meal_orders_are_open(self, at=None):
        at = at or timezone.now()
        return self.status == self.Status.PUBLISHED and at <= self.meal_orders_close_at

    def __str__(self):
        return self.name


class EventProfile(UUIDTimeStampedModel):
    """Configuration réutilisable, sans inscriptions ni données personnelles."""

    name = models.CharField(_("nom du profil"), max_length=200, unique=True)
    description = models.TextField(_("description du profil"), blank=True)
    snapshot = models.JSONField(_("configuration"), default=dict, editable=False)

    class Meta:
        verbose_name = _("profil d'événement")
        verbose_name_plural = _("profils d'événement")
        ordering = ("name",)

    def __str__(self):
        return self.name


class EventDay(UUIDTimeStampedModel):
    event = models.ForeignKey(
        Event,
        verbose_name=_("événement"),
        on_delete=models.CASCADE,
        related_name="days",
    )
    date = models.DateField(_("date"))

    class Meta:
        verbose_name = _("journée d'événement")
        verbose_name_plural = _("journées d'événement")
        ordering = ("date",)
        constraints = [
            models.UniqueConstraint(fields=("event", "date"), name="unique_event_day")
        ]

    def __str__(self):
        return f"{self.event.name} — {self.date:%d/%m/%Y}"


class Station(UUIDTimeStampedModel):
    class Category(models.TextChoices):
        VOLUNTEER = "volunteer", _("Bénévole")
        OFFICIAL = "official", _("Officiel")

    event = models.ForeignKey(
        Event,
        verbose_name=_("événement"),
        on_delete=models.CASCADE,
        related_name="stations",
    )
    name = models.CharField(_("nom"), max_length=160)
    description = models.TextField(_("description"), blank=True)
    instructions = models.TextField(_("consignes"), blank=True)
    category = models.CharField(
        _("catégorie"), max_length=16, choices=Category.choices, default=Category.VOLUNTEER
    )
    is_active = models.BooleanField(_("actif"), default=True)
    position = models.PositiveSmallIntegerField(_("ordre d'affichage"), default=0)

    class Meta:
        verbose_name = _("poste")
        verbose_name_plural = _("postes")
        ordering = ("position", "name")
        constraints = [
            models.UniqueConstraint(fields=("event", "name"), name="unique_station_name_per_event")
        ]

    def __str__(self):
        return f"{self.name} ({self.event.name})"


class StationSlot(UUIDTimeStampedModel):
    class Period(models.TextChoices):
        MORNING = "morning", _("Matin")
        AFTERNOON = "afternoon", _("Après-midi")
        FULL_DAY = "full_day", _("Journée")

    station = models.ForeignKey(
        Station,
        verbose_name=_("poste"),
        on_delete=models.CASCADE,
        related_name="slots",
    )
    event_day = models.ForeignKey(
        EventDay,
        verbose_name=_("journée"),
        on_delete=models.CASCADE,
        related_name="station_slots",
    )
    period = models.CharField(_("période"), max_length=16, choices=Period.choices)
    capacity = models.PositiveSmallIntegerField(_("personnes nécessaires"), default=0)
    start_time = models.TimeField(_("heure de début"), null=True, blank=True)
    end_time = models.TimeField(_("heure de fin"), null=True, blank=True)

    class Meta:
        verbose_name = _("créneau de poste")
        verbose_name_plural = _("créneaux de poste")
        ordering = ("event_day__date", "period", "station__position", "station__name")
        constraints = [
            models.UniqueConstraint(
                fields=("station", "event_day", "period"), name="unique_station_day_period"
            ),
            models.CheckConstraint(
                condition=(
                    Q(start_time__isnull=True, end_time__isnull=True)
                    | Q(start_time__isnull=False, end_time__isnull=False)
                ),
                name="slot_times_both_set_or_empty",
            ),
            models.CheckConstraint(
                condition=Q(start_time__isnull=True) | Q(end_time__gt=F("start_time")),
                name="slot_end_after_start",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.station_id and self.event_day_id:
            if self.station.event_id != self.event_day.event_id:
                errors["event_day"] = _("La journée et le poste doivent appartenir au même événement.")
        if bool(self.start_time) != bool(self.end_time):
            errors["end_time"] = _("Renseignez les deux horaires ou laissez-les tous les deux vides.")
        elif self.start_time and self.end_time and self.start_time >= self.end_time:
            errors["end_time"] = _("L'heure de fin doit suivre l'heure de début.")
        original = StationSlot.objects.filter(pk=self.pk).values(
            "station_id", "event_day_id", "period"
        ).first()
        if original and self.assignments.filter(
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED)
        ).exists():
            # Changer ces champs laisserait les affectations existantes hors
            # disponibilité ou en conflit, avec une période dénormalisée périmée.
            for field, message in (
                ("station", _("Retirez les affectations actives avant de changer de poste.")),
                ("event_day", _("Retirez les affectations actives avant de changer la date du créneau.")),
                ("period", _("Retirez les affectations actives avant de changer cette période.")),
            ):
                attribute = field if field == "period" else f"{field}_id"
                if getattr(self, attribute) != original[attribute]:
                    errors[field] = message
        if errors:
            raise ValidationError(errors)

    @property
    def remaining_capacity(self):
        occupied = self.assignments.filter(
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED)
        ).count()
        return max(self.capacity - occupied, 0)

    def overlaps(self, other: "StationSlot") -> bool:
        if self.event_day_id != other.event_day_id:
            return False
        # Une affectation à la journée mobilise la personne sur les deux
        # demi-journées, même si les horaires indicatifs ne se chevauchent pas.
        if self.Period.FULL_DAY in (self.period, other.period):
            return True
        if self.start_time and self.end_time and other.start_time and other.end_time:
            return self.start_time < other.end_time and other.start_time < self.end_time
        return self.period == other.period

    def __str__(self):
        hours = (
            f"{self.start_time:%H:%M}–{self.end_time:%H:%M}"
            if self.start_time and self.end_time
            else self.get_period_display()
        )
        return f"{self.station.name} — {self.event_day.date:%d/%m/%Y} ({hours})"


class Registration(UUIDTimeStampedModel):
    class Availability(models.TextChoices):
        FULL_DAY = "full_day", _("Journée entière")
        MORNING = "morning", _("Matin")
        AFTERNOON = "afternoon", _("Après-midi")

    class Status(models.TextChoices):
        ACTIVE = "active", _("Active")
        CANCELLED = "cancelled", _("Annulée")

    member = models.ForeignKey(
        Member,
        verbose_name=_("membre"),
        on_delete=models.PROTECT,
        related_name="registrations",
    )
    event_day = models.ForeignKey(
        EventDay,
        verbose_name=_("journée"),
        on_delete=models.CASCADE,
        related_name="registrations",
    )
    availability = models.CharField(
        _("disponibilité"), max_length=16, choices=Availability.choices
    )
    preferred_station = models.ForeignKey(
        Station,
        verbose_name=_("poste souhaité"),
        on_delete=models.SET_NULL,
        related_name="preference_registrations",
        null=True,
        blank=True,
    )
    note = models.TextField(_("remarque privée"), blank=True)
    status = models.CharField(
        _("statut"), max_length=16, choices=Status.choices, default=Status.ACTIVE
    )

    class Meta:
        verbose_name = _("inscription")
        verbose_name_plural = _("inscriptions")
        ordering = ("event_day__date", "member__last_name", "member__first_name")
        constraints = [
            models.UniqueConstraint(fields=("member", "event_day"), name="unique_member_registration_day")
        ]
        indexes = [
            models.Index(fields=("event_day", "status", "availability"), name="reg_day_status_avail_idx")
        ]

    def clean(self):
        super().clean()
        if (
            self.preferred_station_id
            and self.event_day_id
            and self.preferred_station.event_id != self.event_day.event_id
        ):
            raise ValidationError(
                {"preferred_station": _("Le poste souhaité doit appartenir à cet événement.")}
            )

    def covers_period(self, period: str) -> bool:
        return self.availability == self.Availability.FULL_DAY or self.availability == period

    @property
    def age_on_event_day(self):
        return self.member.age_on(self.event_day.date)

    @property
    def is_birthday(self):
        return self.member.has_birthday_on(self.event_day.date)

    def __str__(self):
        return f"{self.member} — {self.event_day}"


class Assignment(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", _("Brouillon")
        CONFIRMED = "confirmed", _("Confirmée")
        CANCELLED = "cancelled", _("Retirée")

    registration = models.ForeignKey(
        Registration,
        verbose_name=_("inscription"),
        on_delete=models.CASCADE,
        related_name="assignments",
    )
    station_slot = models.ForeignKey(
        StationSlot,
        verbose_name=_("créneau"),
        on_delete=models.PROTECT,
        related_name="assignments",
    )
    # Dénormalisé pour garantir en base une seule affectation par période.
    # Les chevauchements journée/demi-journée sont contrôlés sous le verrou de
    # l'inscription dans les services d'affectation.
    period = models.CharField(
        _("période"), max_length=16, choices=StationSlot.Period.choices, editable=False
    )
    status = models.CharField(
        _("statut"), max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    confirmed_at = models.DateTimeField(_("confirmée le"), null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("confirmée par"),
        on_delete=models.SET_NULL,
        related_name="confirmed_assignments",
        null=True,
        blank=True,
    )
    cancelled_at = models.DateTimeField(_("retirée le"), null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("retirée par"),
        on_delete=models.SET_NULL,
        related_name="cancelled_assignments",
        null=True,
        blank=True,
    )
    cancellation_reason = models.TextField(_("motif du retrait"), blank=True)

    class Meta:
        verbose_name = _("affectation")
        verbose_name_plural = _("affectations")
        ordering = ("station_slot__event_day__date", "period", "station_slot__station__position")
        constraints = [
            models.UniqueConstraint(
                fields=("registration", "period"),
                condition=Q(status__in=("draft", "confirmed")),
                name="unique_active_assignment_reg_period",
            )
        ]
        indexes = [
            models.Index(fields=("station_slot", "status"), name="assign_slot_status_idx"),
            models.Index(fields=("registration", "status"), name="assign_reg_status_idx"),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not self.registration_id or not self.station_slot_id:
            return
        registration = self.registration
        slot = self.station_slot
        if registration.event_day_id != slot.event_day_id:
            errors["station_slot"] = _("Le créneau doit être rattaché à la journée d'inscription.")
        if self.status != self.Status.CANCELLED:
            if registration.status != Registration.Status.ACTIVE:
                errors["registration"] = _("Une inscription annulée ne peut pas être affectée.")
            if not registration.covers_period(slot.period):
                errors["station_slot"] = _("Ce créneau est hors de la disponibilité déclarée.")

            active_statuses = (self.Status.DRAFT, self.Status.CONFIRMED)
            others = Assignment.objects.filter(
                registration=registration, status__in=active_statuses
            ).exclude(pk=self.pk)
            if others.filter(period=slot.period).exists():
                errors["station_slot"] = _("Cette personne est déjà affectée sur cette période.")
            for other in others.select_related("station_slot"):
                if slot.overlaps(other.station_slot):
                    errors["station_slot"] = _("Ce créneau chevauche une autre affectation.")
                    break

            occupied = Assignment.objects.filter(
                station_slot=slot, status__in=active_statuses
            ).exclude(pk=self.pk).count()
            if occupied >= slot.capacity:
                errors["station_slot"] = _("Ce créneau ne dispose plus de place.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if self.station_slot_id:
            self.period = self.station_slot.period
        self.full_clean()
        super().save(*args, **kwargs)

    def communication_payload(self):
        slot = self.station_slot
        return {
            "assignment_id": str(self.pk),
            "member_name": self.registration.member.full_name,
            "event": self.registration.event_day.event.name,
            "location": self.registration.event_day.event.location,
            "date": self.registration.event_day.date.isoformat(),
            "station": slot.station.name,
            "period": slot.period,
            "start_time": slot.start_time.isoformat() if slot.start_time else None,
            "end_time": slot.end_time.isoformat() if slot.end_time else None,
            "instructions": slot.station.instructions,
            "status": self.status,
        }

    def communication_signature(self):
        encoded = json.dumps(
            self.communication_payload(), sort_keys=True, ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @property
    def needs_communication(self):
        latest = self.communications.order_by("-version").first()
        return latest is None or latest.signature != self.communication_signature()

    def __str__(self):
        return f"{self.registration.member} → {self.station_slot}"


class PricedOption(UUIDTimeStampedModel):
    event_day = models.ForeignKey(EventDay, on_delete=models.CASCADE)
    name = models.CharField(_("nom"), max_length=160)
    description = models.TextField(_("description"), blank=True)
    surcharge_cents = models.PositiveIntegerField(_("supplément (centimes)"), default=0)
    is_active = models.BooleanField(_("actif"), default=True)

    class Meta:
        abstract = True
        ordering = ("name",)

    def __str__(self):
        return f"{self.name} — {self.event_day.date:%d/%m/%Y}"


class Menu(PricedOption):
    class MealType(models.TextChoices):
        FULL_MEAL = "full_meal", _("Repas complet — journée")
        SNACK = "snack", _("Collation — demi-journée")

    meal_type = models.CharField(
        _("type de repas"), max_length=16, choices=MealType.choices, default=MealType.FULL_MEAL, db_default=MealType.FULL_MEAL
    )
    event_day = models.ForeignKey(
        EventDay, on_delete=models.CASCADE, related_name="menus", verbose_name=_("journée")
    )

    def clean(self):
        super().clean()
        previous = Menu.objects.filter(pk=self.pk).only("meal_type").first()
        if previous and previous.meal_type != self.meal_type:
            orders = self.orders.filter(wants_meal=True, registration__status=Registration.Status.ACTIVE)
            incompatible = (
                orders.exclude(registration__availability=Registration.Availability.FULL_DAY)
                if self.meal_type == self.MealType.FULL_MEAL
                else orders.filter(registration__availability=Registration.Availability.FULL_DAY)
            )
            if incompatible.exists():
                raise ValidationError({"meal_type": _("Ce changement rendrait des réservations incompatibles. Créez un autre menu ou corrigez d’abord les réservations.")})

    class Meta(PricedOption.Meta):
        verbose_name = _("menu")
        verbose_name_plural = _("menus")
        constraints = [
            models.UniqueConstraint(fields=("event_day", "name"), name="unique_menu_name_day")
        ]


class Beverage(PricedOption):
    event_day = models.ForeignKey(
        EventDay, on_delete=models.CASCADE, related_name="beverages", verbose_name=_("journée")
    )

    class Meta(PricedOption.Meta):
        verbose_name = _("boisson")
        verbose_name_plural = _("boissons")
        constraints = [
            models.UniqueConstraint(fields=("event_day", "name"), name="unique_beverage_name_day")
        ]


class MealOrder(UUIDTimeStampedModel):
    class PaymentMethod(models.TextChoices):
        COUNTER = "counter", _("Paiement au comptoir")
        SUMUP = "sumup", _("Paiement via lien SumUp")

    payment_method = models.CharField(
        _("mode de paiement souhaité"), max_length=16,
        choices=PaymentMethod.choices, default=PaymentMethod.COUNTER, db_default=PaymentMethod.COUNTER,
    )
    legacy_availability = models.CharField(max_length=16, blank=True, default="", db_default="legacy", editable=False)
    registration = models.OneToOneField(
        Registration,
        verbose_name=_("inscription"),
        on_delete=models.CASCADE,
        related_name="meal_order",
    )
    wants_meal = models.BooleanField(_("repas demandé"), default=False)
    menu = models.ForeignKey(
        Menu,
        verbose_name=_("menu"),
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    beverage = models.ForeignKey(
        Beverage,
        verbose_name=_("boisson"),
        on_delete=models.PROTECT,
        related_name="orders",
        null=True,
        blank=True,
    )
    menu_surcharge_cents = models.PositiveIntegerField(
        _("supplément menu appliqué"), default=0, editable=False
    )
    beverage_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson appliqué"), default=0, editable=False
    )
    total_surcharge_cents = models.PositiveIntegerField(
        _("supplément total"), default=0, editable=False
    )
    corrected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("corrigé par"),
        on_delete=models.SET_NULL,
        related_name="corrected_meal_orders",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("réservation de repas")
        verbose_name_plural = _("réservations de repas")
        ordering = ("registration__event_day__date", "registration__member__last_name")
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(wants_meal=True, menu__isnull=False)
                    | Q(wants_meal=False, menu__isnull=True, beverage__isnull=True)
                ),
                name="meal_order_coherent_choice",
            ),
            models.CheckConstraint(
                condition=Q(
                    total_surcharge_cents=F("menu_surcharge_cents")
                    + F("beverage_surcharge_cents")
                ),
                name="meal_order_total_matches_parts",
            ),
        ]

    @property
    def legacy_choice_availability(self):
        return self.registration.availability if self.legacy_availability == "legacy" else self.legacy_availability

    def clean(self):
        super().clean()
        errors = {}
        if self.wants_meal and not self.menu_id:
            errors["menu"] = _("Choisissez un menu ou indiquez « pas de repas ».")
        if not self.wants_meal and (self.menu_id or self.beverage_id):
            errors["wants_meal"] = _("Un repas non demandé ne peut contenir ni menu ni boisson.")
        if self.registration_id:
            event_day_id = self.registration.event_day_id
            if self.menu_id and self.menu.event_day_id != event_day_id:
                errors["menu"] = _("Le menu doit appartenir à la journée d'inscription.")
            if self.beverage_id and self.beverage.event_day_id != event_day_id:
                errors["beverage"] = _("La boisson doit appartenir à la journée d'inscription.")
            if self.menu_id:
                expected = (
                    Menu.MealType.FULL_MEAL
                    if self.registration.availability == Registration.Availability.FULL_DAY
                    else Menu.MealType.SNACK
                )
                if self.menu.meal_type != expected:
                    previous = MealOrder.objects.filter(pk=self.pk).only("menu_id", "legacy_availability").first()
                    preserved = (
                        previous is not None and previous.menu_id == self.menu_id
                        and previous.legacy_availability in (self.registration.availability, "legacy")
                        and self.legacy_availability in (previous.legacy_availability, self.registration.availability)
                    )
                    if not preserved:
                        errors["menu"] = _("Une journée donne accès à un repas complet ; une demi-journée à une collation.")
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        previous = None
        if self.pk:
            previous = MealOrder.objects.filter(pk=self.pk).only("menu_id", "beverage_id").first()
        if not self.wants_meal:
            self.menu = None
            self.beverage = None
            self.menu_surcharge_cents = 0
            self.beverage_surcharge_cents = 0
        else:
            if previous is None or previous.menu_id != self.menu_id:
                self.menu_surcharge_cents = self.menu.surcharge_cents if self.menu_id else 0
            if previous is None or previous.beverage_id != self.beverage_id:
                self.beverage_surcharge_cents = (
                    self.beverage.surcharge_cents if self.beverage_id else 0
                )
        self.total_surcharge_cents = self.menu_surcharge_cents + self.beverage_surcharge_cents
        self.full_clean()
        super().save(*args, **kwargs)

    def __str__(self):
        choice = self.menu.name if self.menu_id else _("Pas de repas")
        return f"{self.registration.member} — {choice}"


class Payment(UUIDTimeStampedModel):
    class Method(models.TextChoices):
        ON_SITE = "on_site", _("Sur place")
        SUMUP = "sumup", _("SumUp")

    family = models.ForeignKey(
        Family,
        verbose_name=_("famille"),
        on_delete=models.PROTECT,
        related_name="payments",
    )
    event = models.ForeignKey(
        Event,
        verbose_name=_("événement"),
        on_delete=models.PROTECT,
        related_name="payments",
    )
    amount_cents = models.PositiveIntegerField(_("montant (centimes)"))
    paid_at = models.DateTimeField(_("réglé le"), default=timezone.now)
    method = models.CharField(_("mode"), max_length=16, choices=Method.choices)
    note = models.TextField(_("remarque"), blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("enregistré par"),
        on_delete=models.SET_NULL,
        related_name="recorded_payments",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("paiement")
        verbose_name_plural = _("paiements")
        ordering = ("-paid_at",)
        constraints = [
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name="payment_amount_positive")
        ]
        indexes = [models.Index(fields=("family", "event"), name="payment_family_event_idx")]

    def __str__(self):
        return f"{self.family} — {self.amount_cents / 100:.2f} €"


class Attendance(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        UNKNOWN = "unknown", _("Non renseignée")
        PRESENT = "present", _("Présent")
        ABSENT = "absent", _("Absent")

    member = models.ForeignKey(
        Member,
        verbose_name=_("membre"),
        on_delete=models.PROTECT,
        related_name="attendances",
    )
    event_day = models.ForeignKey(
        EventDay,
        verbose_name=_("journée"),
        on_delete=models.PROTECT,
        related_name="attendances",
    )
    status = models.CharField(
        _("présence"), max_length=16, choices=Status.choices, default=Status.UNKNOWN
    )
    note = models.TextField(_("remarque"), blank=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name=_("renseignée par"),
        on_delete=models.SET_NULL,
        related_name="recorded_attendances",
        null=True,
        blank=True,
    )

    class Meta:
        verbose_name = _("présence")
        verbose_name_plural = _("présences")
        ordering = ("-event_day__date", "member__last_name", "member__first_name")
        constraints = [
            models.UniqueConstraint(fields=("member", "event_day"), name="unique_attendance_member_day")
        ]

    def __str__(self):
        return f"{self.member} — {self.event_day.date:%d/%m/%Y} : {self.get_status_display()}"


class EmailDelivery(UUIDTimeStampedModel):
    class Kind(models.TextChoices):
        REGISTRATION_RECEIPT = "registration", _("Accusé d'inscription")
        ASSIGNMENT_CONFIRMATION = "assignment", _("Confirmation d'affectation")
        ASSIGNMENT_UPDATE = "assignment_update", _("Mise à jour d'affectation")

    class Status(models.TextChoices):
        DRAFT = "draft", _("Brouillon")
        PENDING = "pending", _("En attente")
        ACCEPTED = "accepted", _("Accepté par le service")
        DELIVERED = "delivered", _("Livré")
        FAILED = "failed", _("Échec")

    event = models.ForeignKey(
        Event,
        verbose_name=_("événement"),
        on_delete=models.PROTECT,
        related_name="email_deliveries",
    )
    family = models.ForeignKey(
        Family,
        verbose_name=_("famille"),
        on_delete=models.PROTECT,
        related_name="email_deliveries",
    )
    kind = models.CharField(_("type"), max_length=24, choices=Kind.choices)
    status = models.CharField(
        _("statut"), max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    to_email = models.EmailField(_("destinataire"))
    subject = models.CharField(_("objet"), max_length=255)
    body_text = models.TextField(_("contenu"))
    extra_instructions = models.TextField(_("consignes complémentaires"), blank=True)
    idempotency_key = models.UUIDField(_("clé d'idempotence"), default=uuid.uuid4, unique=True)
    provider_message_id = models.CharField(_("identifiant fournisseur"), max_length=255, blank=True)
    last_attempt_at = models.DateTimeField(_("dernière tentative"), null=True, blank=True)
    accepted_at = models.DateTimeField(_("accepté le"), null=True, blank=True)
    delivered_at = models.DateTimeField(_("livré le"), null=True, blank=True)
    failure_reason = models.TextField(_("motif d'échec"), blank=True)

    class Meta:
        verbose_name = _("envoi d'e-mail")
        verbose_name_plural = _("envois d'e-mails")
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=("event", "family", "status"), name="email_event_family_idx")
        ]

    def __str__(self):
        return f"{self.get_kind_display()} — {self.family} ({self.get_status_display()})"


class EmailAttempt(UUIDTimeStampedModel):
    class Status(models.TextChoices):
        PENDING = "pending", _("En attente")
        ACCEPTED = "accepted", _("Acceptée")
        DELIVERED = "delivered", _("Livrée")
        FAILED = "failed", _("Échec")

    delivery = models.ForeignKey(
        EmailDelivery,
        verbose_name=_("envoi"),
        on_delete=models.CASCADE,
        related_name="attempts",
    )
    attempt_number = models.PositiveSmallIntegerField(_("numéro de tentative"))
    status = models.CharField(_("statut"), max_length=16, choices=Status.choices)
    provider_message_id = models.CharField(_("identifiant fournisseur"), max_length=255, blank=True)
    error_message = models.TextField(_("erreur"), blank=True)
    attempted_at = models.DateTimeField(_("tentée le"), default=timezone.now)

    class Meta:
        verbose_name = _("tentative d'e-mail")
        verbose_name_plural = _("tentatives d'e-mail")
        ordering = ("delivery", "attempt_number")
        constraints = [
            models.UniqueConstraint(
                fields=("delivery", "attempt_number"), name="unique_email_attempt_number"
            )
        ]

    def __str__(self):
        return f"{self.delivery} — tentative {self.attempt_number}"


class AssignmentCommunication(UUIDTimeStampedModel):
    assignment = models.ForeignKey(
        Assignment,
        verbose_name=_("affectation"),
        on_delete=models.PROTECT,
        related_name="communications",
    )
    delivery = models.ForeignKey(
        EmailDelivery,
        verbose_name=_("envoi"),
        on_delete=models.PROTECT,
        related_name="assignment_communications",
    )
    version = models.PositiveSmallIntegerField(_("version"))
    snapshot = models.JSONField(_("instantané communiqué"))
    signature = models.CharField(_("empreinte"), max_length=64)
    communicated_at = models.DateTimeField(_("communiquée le"), default=timezone.now)

    class Meta:
        verbose_name = _("communication d'affectation")
        verbose_name_plural = _("communications d'affectation")
        ordering = ("assignment", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("assignment", "version"), name="unique_assignment_communication_version"
            ),
            models.UniqueConstraint(
                fields=("assignment", "delivery"), name="unique_assignment_per_delivery"
            ),
        ]

    def clean(self):
        super().clean()
        if self.assignment_id and self.delivery_id:
            event_id = self.assignment.registration.event_day.event_id
            family_id = self.assignment.registration.member.family_id
            errors = {}
            if self.delivery.event_id != event_id:
                errors["delivery"] = _("L'envoi doit concerner le même événement.")
            if self.delivery.family_id != family_id:
                errors["delivery"] = _("L'envoi doit concerner la famille de l'affectation.")
            if errors:
                raise ValidationError(errors)

    def __str__(self):
        return f"{self.assignment} — version {self.version}"


class LegacyImportRecord(UUIDTimeStampedModel):
    """Journal idempotent d'une ligne issue de l'ancien Google Sheets."""

    class SourceType(models.TextChoices):
        EVENT = "event", _("Événement")
        STATION = "station", _("Poste ou créneau")
        REGISTRATION = "registration", _("Inscription")
        OTHER = "other", _("Autre")

    class Status(models.TextChoices):
        PENDING = "pending", _("À traiter")
        IMPORTED = "imported", _("Importée")
        NEEDS_REVIEW = "needs_review", _("À compléter ou rattacher")
        SKIPPED = "skipped", _("Ignorée")
        FAILED = "failed", _("Échec")

    source_type = models.CharField(_("type de source"), max_length=24, choices=SourceType.choices)
    legacy_id = models.CharField(_("identifiant d'origine"), max_length=255)
    content_fingerprint = models.CharField(_("empreinte du contenu"), max_length=64, db_index=True)
    target_model = models.CharField(_("modèle cible"), max_length=100, blank=True)
    target_uuid = models.UUIDField(_("identifiant cible"), null=True, blank=True)
    status = models.CharField(
        _("statut"), max_length=24, choices=Status.choices, default=Status.PENDING
    )
    payload = models.JSONField(_("données source"), default=dict)
    report = models.JSONField(_("rapport d'import"), default=dict)
    imported_at = models.DateTimeField(_("importée le"), null=True, blank=True)

    class Meta:
        verbose_name = _("ligne d'import historique")
        verbose_name_plural = _("lignes d'import historique")
        ordering = ("source_type", "legacy_id")
        constraints = [
            models.UniqueConstraint(
                fields=("source_type", "legacy_id"), name="unique_legacy_source_record"
            ),
            models.CheckConstraint(
                condition=(
                    Q(target_model="", target_uuid__isnull=True)
                    | (~Q(target_model="") & Q(target_uuid__isnull=False))
                ),
                name="legacy_target_both_set_or_empty",
            ),
        ]
        indexes = [models.Index(fields=("status", "source_type"), name="legacy_status_source_idx")]

    def __str__(self):
        return f"{self.get_source_type_display()} {self.legacy_id} — {self.get_status_display()}"


class SiteBranding(models.Model):
    """Le logo est conservé en base pour survivre aux redéploiements Render."""

    logo_data = models.TextField(blank=True)
    sumup_payment_url = models.URLField(_("lien SumUp de l’association"), blank=True, db_default="")
