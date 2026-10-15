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
from django.db import models, transaction
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
    is_competition_official = models.BooleanField(
        _("officiel de compétition"), default=False, db_default=False
    )

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
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name=_("compte personnel"),
        on_delete=models.PROTECT,
        related_name="participant_profile",
        null=True,
        blank=True,
    )
    first_name = models.CharField(_("prénom"), max_length=150)
    last_name = models.CharField(_("nom"), max_length=150)
    birth_date = models.DateField(_("date de naissance"))
    is_competition_official = models.BooleanField(
        _("officiel de compétition"), default=False, db_default=False
    )
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
        if self.user_id and self.family_id and self.family.account_id != self.user_id:
            raise ValidationError(
                {"user": _("Le compte personnel doit être le compte responsable de cette famille.")}
            )
        if self.birth_date and self.birth_date > timezone.localdate():
            raise ValidationError({"birth_date": _("La date de naissance ne peut pas être future.")})
        if self._state.adding:
            return
        previous = Member.objects.filter(pk=self.pk).values(
            "birth_date", "is_competition_official"
        ).first()
        if not previous:
            return
        active = Assignment.objects.filter(
            registration__member_id=self.pk,
            registration__event_day__date__gte=timezone.localdate(),
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
        ).select_related("station_slot__station", "registration__event_day")
        if previous["is_competition_official"] and not self.is_competition_official:
            if active.filter(station_slot__station__category=Station.Category.OFFICIAL).exists():
                raise ValidationError({
                    "is_competition_official": _(
                        "Retirez les affectations officielles à venir avant de retirer cette fonction."
                    )
                })
        if self.birth_date and previous["birth_date"] != self.birth_date:
            from .eligibility import (
                validate_adult_coverage_for_confirmation,
                validate_member_station,
            )

            try:
                for assignment in active:
                    validate_member_station(
                        self, assignment.station_slot.station, assignment.registration.event_day
                    )
                    if assignment.status == Assignment.Status.CONFIRMED:
                        assignment.registration.member = self
                        validate_adult_coverage_for_confirmation([assignment])
            except ValidationError as error:
                raise ValidationError({
                    "birth_date": _(
                        "Cette date rend une affectation à venir incompatible. "
                        "Retirez ou réorganisez cette affectation avant de modifier la date de naissance."
                    )
                }) from error

    @transaction.atomic
    def save(self, *args, **kwargs):
        meal_orders = []
        official_changed = False
        if not self._state.adding:
            previous = Member.objects.select_for_update(of=("self",)).filter(pk=self.pk).values(
                "birth_date", "is_competition_official"
            ).first()
            update_fields = kwargs.get("update_fields")
            if previous and update_fields is not None:
                for field in ("birth_date", "is_competition_official"):
                    if field not in update_fields:
                        setattr(self, field, previous[field])
            if previous and (
                self.birth_date != previous["birth_date"]
                or self.is_competition_official != previous["is_competition_official"]
            ):
                official_changed = (
                    self.is_competition_official != previous["is_competition_official"]
                )
                affected = Assignment.objects.filter(
                    registration__member_id=self.pk,
                    registration__event_day__date__gte=timezone.localdate(),
                    status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
                )
                day_ids = list(affected.values_list("registration__event_day_id", flat=True))
                slot_ids = list(affected.values_list("station_slot_id", flat=True))
                meal_registrations = Registration.objects.filter(
                    member_id=self.pk,
                    status=Registration.Status.ACTIVE,
                    event_day__date__gte=timezone.localdate(),
                    meal_order__isnull=False,
                ) if official_changed else Registration.objects.none()
                meal_registration_ids = list(meal_registrations.values_list("pk", flat=True))
                day_ids.extend(meal_registrations.values_list("event_day_id", flat=True))
                # Deux adultes d'un même créneau ne peuvent pas devenir mineurs
                # simultanément en se considérant chacun couvert par l'autre.
                list(EventDay.objects.select_for_update(of=("self",)).filter(
                    pk__in=day_ids
                ).order_by("pk"))
                list(Registration.objects.select_for_update(of=("self",)).filter(
                    pk__in=meal_registration_ids
                ).order_by("pk"))
                meal_orders = list(MealOrder.objects.select_for_update(of=("self",)).filter(
                    registration_id__in=meal_registration_ids
                ).order_by("pk"))
                list(StationSlot.objects.select_for_update(of=("self",)).filter(
                    pk__in=slot_ids
                ).order_by("pk"))
                self.clean()
        super().save(*args, **kwargs)
        if official_changed and meal_orders:
            from .official_pricing import reconcile_official_meal_orders

            reconcile_official_meal_orders(meal_orders, member=self)

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
    morning_hot_beverage_enabled = models.BooleanField(
        _("proposer une boisson chaude avec la collation du matin"), default=True, db_default=True,
    )
    afternoon_cold_beverage_enabled = models.BooleanField(
        _("proposer une boisson fraîche avec la collation de l'après-midi"), default=True, db_default=True,
    )
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

    @transaction.atomic
    def save(self, *args, **kwargs):
        fields = ("morning_hot_beverage_enabled", "afternoon_cold_beverage_enabled")
        previous = Event.objects.filter(pk=self.pk).values(*fields).first()
        update_fields = kwargs.get("update_fields")
        if previous and update_fields is not None:
            for field in fields:
                if field not in update_fields:
                    setattr(self, field, previous[field])
        if previous and any(previous[field] != getattr(self, field) for field in fields):
            # A meal reservation holds Day while reading these flags. Serialize
            # offering changes with that reservation without changing its price.
            Event.objects.select_for_update(of=("self",), no_key=True).get(pk=self.pk)
            list(EventDay.objects.select_for_update(of=("self",)).filter(
                event_id=self.pk
            ).order_by("pk"))
        super().save(*args, **kwargs)

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

    class AgePolicy(models.TextChoices):
        NONE = "none", _("Aucune condition d'âge")
        AT_LEAST_ONE_ADULT = "at_least_one_adult", _("Au moins un adulte (18 ans ou plus)")
        ADULTS_ONLY = "adults_only", _("Uniquement des adultes (18 ans ou plus)")

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
    age_policy = models.CharField(
        _("règle d'âge"), max_length=24, choices=AgePolicy.choices,
        default=AgePolicy.NONE, db_default=AgePolicy.NONE,
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

    def clean(self):
        super().clean()
        previous = Station.objects.filter(pk=self.pk).values("category", "age_policy").first()
        if not previous or (
            previous["category"] == self.category and previous["age_policy"] == self.age_policy
        ):
            return
        from .eligibility import validate_adult_coverage_for_confirmation, validate_member_station

        confirmed = []
        for assignment in Assignment.objects.filter(
            station_slot__station_id=self.pk,
            registration__event_day__date__gte=timezone.localdate(),
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED),
        ).select_related("registration__member", "registration__event_day", "station_slot"):
            assignment.station_slot.station = self
            try:
                validate_member_station(
                    assignment.registration.member, self, assignment.registration.event_day
                )
            except ValidationError as error:
                raise ValidationError(
                    "Retirez ou réorganisez les affectations à venir incompatibles avant "
                    "de changer les conditions de ce poste. " + " ".join(error.messages)
                ) from error
            if assignment.status == Assignment.Status.CONFIRMED:
                confirmed.append(assignment)
        validate_adult_coverage_for_confirmation(confirmed)

    @transaction.atomic
    def save(self, *args, **kwargs):
        meal_orders = []
        if self.event_id:
            # La clé étrangère d'un nouveau poste prend aussi un verrou sur
            # l'événement. Le prendre d'abord évite l'ordre inverse Day/Event
            # face à une inscription personnelle qui tient déjà Event.
            Event.objects.select_for_update(of=("self",)).get(pk=self.event_id)
        previous = Station.objects.filter(pk=self.pk).values("category", "age_policy").first()
        if previous and (
            previous["category"] != self.category or previous["age_policy"] != self.age_policy
        ):
            # Les services d'affectation tiennent aussi ce verrou de journée.
            # Ainsi un changement de règle ne peut valider un état périmé.
            list(EventDay.objects.select_for_update(of=("self",)).filter(
                event_id=self.event_id
            ).order_by("pk"))
            if previous["category"] != self.category:
                registration_ids = set(Registration.objects.filter(
                    Q(preferred_station_id=self.pk) | Q(assignments__station_slot__station_id=self.pk),
                    status=Registration.Status.ACTIVE,
                    event_day__date__gte=timezone.localdate(),
                ).values_list("pk", flat=True))
                list(Registration.objects.select_for_update(of=("self",)).filter(
                    pk__in=registration_ids
                ).order_by("pk"))
                meal_orders = list(MealOrder.objects.select_for_update(of=("self",)).filter(
                    registration_id__in=registration_ids
                ).select_related("registration__member", "registration__event_day").order_by("pk"))
            list(StationSlot.objects.select_for_update(of=("self",)).filter(
                station_id=self.pk
            ).order_by("pk"))
            self.clean()
        super().save(*args, **kwargs)
        if meal_orders:
            from .official_pricing import reconcile_official_meal_orders

            reconcile_official_meal_orders(meal_orders)

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
        if self.preferred_station_id and self.event_day_id and self.member_id:
            from .eligibility import validate_member_station

            try:
                validate_member_station(self.member, self.preferred_station, self.event_day)
            except ValidationError as error:
                raise ValidationError({"preferred_station": error.messages}) from error

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Direct admin edits must use the same pricing rule as public services.
        previous = Registration.objects.filter(pk=self.pk).values("member_id", "event_day_id").first()
        member_ids = {self.member_id}
        day_ids = {self.event_day_id}
        if previous:
            member_ids.add(previous["member_id"])
            day_ids.add(previous["event_day_id"])
        list(Member.objects.select_for_update(of=("self",)).filter(
            pk__in=member_ids
        ).order_by("pk"))
        list(EventDay.objects.select_for_update(of=("self",)).filter(
            pk__in=day_ids
        ).order_by("pk"))
        Registration.objects.select_for_update(of=("self",)).filter(pk=self.pk).first()
        orders = list(MealOrder.objects.select_for_update(of=("self",)).filter(
            registration_id=self.pk
        ).order_by("pk"))
        super().save(*args, **kwargs)
        if orders:
            from .official_pricing import reconcile_official_meal_orders

            current = Registration.objects.select_related("member", "event_day").get(pk=self.pk)
            for order in orders:
                order.registration = current
            reconcile_official_meal_orders(orders)

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
            from .eligibility import (
                validate_adult_coverage_for_confirmation,
                validate_member_station,
            )

            try:
                validate_member_station(registration.member, slot.station, registration.event_day)
                if self.status == self.Status.CONFIRMED:
                    validate_adult_coverage_for_confirmation([self])
            except ValidationError as error:
                errors["station_slot"] = error.messages
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

    @transaction.atomic
    def save(self, *args, **kwargs):
        # Les écritures directes de l'administration suivent le même ordre que
        # les services : membre, journée, inscription, créneau, affectation.
        previous = Assignment.objects.filter(pk=self.pk).values(
            "registration_id", "station_slot_id", "status"
        ).first()
        update_fields = kwargs.get("update_fields")
        if previous and update_fields is not None:
            for field in ("registration", "station_slot"):
                if field not in update_fields and f"{field}_id" not in update_fields:
                    setattr(self, f"{field}_id", previous[f"{field}_id"])
            if "status" not in update_fields:
                self.status = previous["status"]
        registration_ids = {self.registration_id}
        slot_ids = {self.station_slot_id}
        if previous:
            registration_ids.add(previous["registration_id"])
            slot_ids.add(previous["station_slot_id"])
        coordinates = list(Registration.objects.filter(pk__in=registration_ids).values_list(
            "member_id", "event_day_id"
        ))
        list(Member.objects.select_for_update(of=("self",)).filter(
            pk__in={item[0] for item in coordinates}
        ).order_by("pk"))
        list(EventDay.objects.select_for_update(of=("self",)).filter(
            pk__in={item[1] for item in coordinates}
        ).order_by("pk"))
        registrations = {item.pk: item for item in Registration.objects.select_for_update(
            of=("self",)
        ).filter(pk__in=registration_ids).select_related("member", "event_day").order_by("pk")}
        meal_orders = list(MealOrder.objects.select_for_update(of=("self",)).filter(
            registration_id__in=registration_ids
        ).select_related("registration__member", "registration__event_day__event").order_by("pk"))
        slots = {item.pk: item for item in StationSlot.objects.select_for_update(
            of=("self",)
        ).filter(pk__in=slot_ids).select_related("station", "event_day").order_by("pk")}
        if previous:
            Assignment.objects.select_for_update(of=("self",)).get(pk=self.pk)
        if self.registration_id in registrations:
            self.registration = registrations[self.registration_id]
        if self.station_slot_id in slots:
            self.station_slot = slots[self.station_slot_id]
            self.period = self.station_slot.period
        self.full_clean()
        super().save(*args, **kwargs)
        from .eligibility import ensure_confirmed_adult_coverage

        self.coverage_invalidated_assignment_ids = ensure_confirmed_adult_coverage(slot_ids)
        from .official_pricing import reconcile_official_meal_orders

        reconcile_official_meal_orders(meal_orders)

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
    event_day = models.ForeignKey(EventDay, on_delete=models.CASCADE, null=True, blank=True, default=None, db_default=None)
    name = models.CharField(_("nom"), max_length=160)
    description = models.TextField(_("description"), blank=True)
    surcharge_cents = models.PositiveIntegerField(_("supplément (centimes)"), default=0)
    is_active = models.BooleanField(_("actif"), default=True)

    class Meta:
        abstract = True
        ordering = ("name",)

    def __str__(self):
        scope = f"{self.event_day.date:%d/%m/%Y}" if self.event_day_id else _("Catalogue commun")
        return f"{self.name} — {scope}"


class Menu(PricedOption):
    class MealType(models.TextChoices):
        FULL_MEAL = "full_meal", _("Repas complet — journée")
        SNACK = "snack", _("Collation")

    meal_type = models.CharField(
        _("type de repas"), max_length=16, choices=MealType.choices, default=MealType.FULL_MEAL, db_default=MealType.FULL_MEAL
    )
    event_day = models.ForeignKey(
        EventDay, on_delete=models.CASCADE, related_name="menus", verbose_name=_("journée"),
        null=True, blank=True, default=None, db_default=None,
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
            snack_reserved = (
                self.meal_type != self.MealType.SNACK
                and (
                    self.morning_snack_orders.filter(
                        wants_meal=True, registration__status=Registration.Status.ACTIVE,
                    ).exists()
                    or self.afternoon_snack_orders.filter(
                        wants_meal=True, registration__status=Registration.Status.ACTIVE,
                    ).exists()
                )
            )
            if incompatible.exists() or snack_reserved:
                raise ValidationError({"meal_type": _("Ce changement rendrait des réservations incompatibles. Créez un autre menu ou corrigez d’abord les réservations.")})

    class Meta(PricedOption.Meta):
        verbose_name = _("menu")
        verbose_name_plural = _("menus")
        constraints = [
            models.UniqueConstraint(fields=("event_day", "name"), name="unique_menu_name_day"),
            models.UniqueConstraint(
                fields=("name", "meal_type"), condition=Q(event_day__isnull=True),
                name="unique_global_menu_name_type",
            ),
        ]


class Beverage(PricedOption):
    class Temperature(models.TextChoices):
        HOT = "hot", _("Chaude")
        COLD = "cold", _("Fraîche")
        UNSPECIFIED = "unspecified", _("À classer")

    temperature = models.CharField(
        _("température"), max_length=16, choices=Temperature.choices,
        default=Temperature.UNSPECIFIED, db_default=Temperature.UNSPECIFIED,
    )
    event_day = models.ForeignKey(
        EventDay, on_delete=models.CASCADE, related_name="beverages", verbose_name=_("journée"),
        null=True, blank=True, default=None, db_default=None,
    )

    class Meta(PricedOption.Meta):
        verbose_name = _("boisson")
        verbose_name_plural = _("boissons")
        constraints = [
            models.UniqueConstraint(fields=("event_day", "name"), name="unique_beverage_name_day"),
            models.UniqueConstraint(
                fields=("name",), condition=Q(event_day__isnull=True),
                name="unique_global_beverage_name",
            ),
        ]


class MealOrder(UUIDTimeStampedModel):
    PRICE_COMPONENTS = (
        "menu", "beverage", "morning_snack", "afternoon_snack",
        "morning_beverage", "afternoon_beverage",
    )
    MENU_COMPONENTS = ("menu", "morning_snack", "afternoon_snack")
    BEVERAGE_COMPONENTS = ("beverage", "morning_beverage", "afternoon_beverage")

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
    morning_snack = models.ForeignKey(
        Menu, verbose_name=_("collation du matin"), on_delete=models.PROTECT,
        related_name="morning_snack_orders", null=True, blank=True, default=None, db_default=None,
    )
    afternoon_snack = models.ForeignKey(
        Menu, verbose_name=_("collation de l'après-midi"), on_delete=models.PROTECT,
        related_name="afternoon_snack_orders", null=True, blank=True, default=None, db_default=None,
    )
    morning_beverage = models.ForeignKey(
        Beverage, verbose_name=_("boisson chaude du matin"), on_delete=models.PROTECT,
        related_name="morning_orders", null=True, blank=True, default=None, db_default=None,
    )
    afternoon_beverage = models.ForeignKey(
        Beverage, verbose_name=_("boisson fraîche de l'après-midi"), on_delete=models.PROTECT,
        related_name="afternoon_orders", null=True, blank=True, default=None, db_default=None,
    )
    beverage_availability = models.CharField(
        _("période de la boisson réservée"), max_length=16, blank=True,
        default="legacy", db_default="legacy", editable=False,
    )
    menu_surcharge_cents = models.PositiveIntegerField(
        _("supplément menu appliqué"), default=0, editable=False
    )
    beverage_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson appliqué"), default=0, editable=False
    )
    morning_snack_surcharge_cents = models.PositiveIntegerField(
        _("supplément collation du matin appliqué"), default=0, db_default=0, editable=False,
    )
    afternoon_snack_surcharge_cents = models.PositiveIntegerField(
        _("supplément collation de l'après-midi appliqué"), default=0, db_default=0, editable=False,
    )
    morning_beverage_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson du matin appliqué"), default=0, db_default=0, editable=False,
    )
    afternoon_beverage_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson de l'après-midi appliqué"), default=0, db_default=0, editable=False,
    )
    total_surcharge_cents = models.PositiveIntegerField(
        _("supplément total"), default=0, editable=False
    )
    menu_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément menu avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    beverage_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    morning_snack_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément collation du matin avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    afternoon_snack_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément collation de l'après-midi avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    morning_beverage_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson du matin avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    afternoon_beverage_standard_surcharge_cents = models.PositiveIntegerField(
        _("supplément boisson de l'après-midi avant exonération"), null=True, default=None,
        db_default=None, editable=False,
    )
    official_exemption_applied = models.BooleanField(
        _("exonération officiel appliquée"), default=False, db_default=False, editable=False,
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
                    | Q(wants_meal=False, menu__isnull=True, beverage__isnull=True,
                        morning_snack__isnull=True, afternoon_snack__isnull=True,
                        morning_beverage__isnull=True, afternoon_beverage__isnull=True)
                ),
                name="meal_order_coherent_choice",
            ),
            models.CheckConstraint(
                condition=Q(
                    total_surcharge_cents=F("menu_surcharge_cents")
                    + F("beverage_surcharge_cents")
                    + F("morning_snack_surcharge_cents")
                    + F("afternoon_snack_surcharge_cents")
                    + F("morning_beverage_surcharge_cents")
                    + F("afternoon_beverage_surcharge_cents")
                ),
                name="meal_order_total_matches_parts",
            ),
        ]

    @property
    def legacy_choice_availability(self):
        return self.registration.availability if self.legacy_availability == "legacy" else self.legacy_availability

    @property
    def beverage_choice_availability(self):
        return self.registration.availability if self.beverage_availability == "legacy" else self.beverage_availability

    def clean(self):
        super().clean()
        errors = {}
        if self.wants_meal and not self.menu_id:
            errors["menu"] = _("Choisissez un menu ou indiquez « pas de repas ».")
        if not self.wants_meal and any(getattr(self, f"{field}_id") for field in self.PRICE_COMPONENTS):
            errors["wants_meal"] = _("Un repas non demandé ne peut contenir ni menu, ni collation, ni boisson.")
        if self.registration_id:
            event_day_id = self.registration.event_day_id
            previous = MealOrder.objects.filter(pk=self.pk).first()
            for field in self.PRICE_COMPONENTS:
                option = getattr(self, field)
                if option is None:
                    continue
                if option.event_day_id not in (None, event_day_id):
                    errors[field] = _("Ce choix doit être commun ou appartenir à la journée d'inscription.")
                preserved = (
                    previous is not None and previous.registration_id == self.registration_id
                    and getattr(previous, f"{field}_id") == option.pk
                )
                if not option.is_active and not preserved:
                    errors[field] = _("Ce choix n'est plus proposé.")
            for field in ("morning_snack", "afternoon_snack"):
                snack = getattr(self, field)
                if snack is None:
                    continue
                if self.registration.availability != Registration.Availability.FULL_DAY:
                    errors[field] = _("Les collations supplémentaires sont réservées à une présence à la journée.")
                elif snack.meal_type != Menu.MealType.SNACK:
                    errors[field] = _("Choisissez une collation pour cette période.")
            event = self.registration.event_day.event
            availability = self.registration.availability
            phase_rules = {
                Registration.Availability.MORNING: (
                    Beverage.Temperature.HOT, event.morning_hot_beverage_enabled,
                ),
                Registration.Availability.AFTERNOON: (
                    Beverage.Temperature.COLD, event.afternoon_cold_beverage_enabled,
                ),
            }
            if self.beverage_id and availability in phase_rules:
                preserved = (
                    previous is not None and previous.registration_id == self.registration_id
                    and previous.beverage_id == self.beverage_id
                    and previous.beverage_choice_availability == availability
                )
                temperature, enabled = phase_rules[availability]
                if not preserved:
                    if not enabled:
                        errors["beverage"] = _("La boisson de cette collation est désactivée pour cet événement.")
                    elif self.beverage.temperature != temperature:
                        errors["beverage"] = _("Choisissez une boisson chaude le matin ou fraîche l'après-midi.")
            for field, snack_field, temperature, enabled in (
                ("morning_beverage", "morning_snack", Beverage.Temperature.HOT, event.morning_hot_beverage_enabled),
                ("afternoon_beverage", "afternoon_snack", Beverage.Temperature.COLD, event.afternoon_cold_beverage_enabled),
            ):
                beverage = getattr(self, field)
                if beverage is None:
                    continue
                preserved = (
                    previous is not None and previous.registration_id == self.registration_id
                    and getattr(previous, f"{field}_id") == beverage.pk
                )
                if availability != Registration.Availability.FULL_DAY:
                    errors[field] = _("Ces boissons supplémentaires sont réservées à une présence à la journée.")
                elif not getattr(self, f"{snack_field}_id"):
                    errors[field] = _("Choisissez la collation correspondante avant d'ajouter sa boisson.")
                elif not preserved:
                    if not enabled:
                        errors[field] = _("Cette boisson de collation est désactivée pour cet événement.")
                    elif beverage.temperature != temperature:
                        errors[field] = _("Choisissez une boisson chaude le matin ou fraîche l'après-midi.")
            if self.menu_id:
                expected = (
                    Menu.MealType.FULL_MEAL
                    if self.registration.availability == Registration.Availability.FULL_DAY
                    else Menu.MealType.SNACK
                )
                if self.menu.meal_type != expected:
                    preserved = (
                        previous is not None and previous.registration_id == self.registration_id
                        and previous.menu_id == self.menu_id
                        and previous.legacy_availability in (self.registration.availability, "legacy")
                        and self.legacy_availability in (previous.legacy_availability, self.registration.availability)
                    )
                    if not preserved:
                        errors["menu"] = _("Une journée donne accès à un repas complet ; une demi-journée à une collation.")
        if errors:
            raise ValidationError(errors)

    @classmethod
    def pricing_update_fields(cls):
        return tuple(
            field
            for component in cls.PRICE_COMPONENTS
            for field in (f"{component}_standard_surcharge_cents", f"{component}_surcharge_cents")
        ) + ("official_exemption_applied", "total_surcharge_cents")

    def apply_official_pricing(self, is_official):
        """Applique l'exonération sans perdre les tarifs réservés ni toucher les paiements."""
        for component in self.PRICE_COMPONENTS:
            standard_field = f"{component}_standard_surcharge_cents"
            applied_field = f"{component}_surcharge_cents"
            if getattr(self, standard_field) is None:
                setattr(self, standard_field, getattr(self, applied_field))
            setattr(self, applied_field, 0 if is_official else getattr(self, standard_field))
        self.official_exemption_applied = bool(is_official)
        self.total_surcharge_cents = sum(getattr(self, f"{component}_surcharge_cents") for component in self.PRICE_COMPONENTS)

    @transaction.atomic
    def save(self, *args, **kwargs):
        previous = MealOrder.objects.filter(pk=self.pk).first() if self.pk else None
        update_fields = kwargs.get("update_fields")
        if previous is not None and update_fields is not None:
            if "registration" not in update_fields and "registration_id" not in update_fields:
                self.registration_id = previous.registration_id
        registration_ids = {self.registration_id}
        if previous is not None:
            registration_ids.add(previous.registration_id)
        coordinates = list(Registration.objects.filter(pk__in=registration_ids).values_list(
            "member_id", "event_day_id"
        ))
        members = {member.pk: member for member in Member.objects.select_for_update(of=("self",))
                   .filter(pk__in={item[0] for item in coordinates}).order_by("pk")}
        days = {day.pk: day for day in EventDay.objects.select_for_update(of=("self",))
                .filter(pk__in={item[1] for item in coordinates}).order_by("pk")}
        registrations = {registration.pk: registration for registration in
                         Registration.objects.select_for_update(of=("self",))
                         .filter(pk__in=registration_ids).order_by("pk")}
        # Relire après les verrous pour ignorer un objet ancien ou un statut forgé.
        previous = MealOrder.objects.select_for_update(of=("self",)).filter(pk=self.pk).first()
        if previous is not None:
            if previous.registration_id not in registrations:
                raise ValidationError(_("La réservation a été déplacée. Rechargez la page avant de réessayer."))
            if update_fields is not None:
                for field in ("registration", *self.PRICE_COMPONENTS, "wants_meal", "legacy_availability"):
                    if field not in update_fields and f"{field}_id" not in update_fields:
                        attribute = f"{field}_id" if field in ("registration", *self.PRICE_COMPONENTS) else field
                        setattr(self, attribute, getattr(previous, attribute))
        self.registration = registrations[self.registration_id]
        self.registration.member = members[self.registration.member_id]
        self.registration.event_day = days[self.registration.event_day_id]
        menu_ids = {getattr(self, f"{field}_id") for field in self.MENU_COMPONENTS} - {None}
        menus = {option.pk: option for option in Menu.objects.select_for_update(of=("self",))
                 .filter(pk__in=menu_ids).order_by("pk")}
        if set(menus) != menu_ids:
            raise ValidationError(_("Un menu ou une collation n'est plus disponible. Rechargez la page."))
        for field in self.MENU_COMPONENTS:
            setattr(self, field, menus.get(getattr(self, f"{field}_id")))
        beverage_ids = {getattr(self, f"{field}_id") for field in self.BEVERAGE_COMPONENTS} - {None}
        beverages = {option.pk: option for option in Beverage.objects.select_for_update(of=("self",))
                     .filter(pk__in=beverage_ids).order_by("pk")}
        if set(beverages) != beverage_ids:
            raise ValidationError(_("Une boisson n'est plus disponible. Rechargez la page."))
        for field in self.BEVERAGE_COMPONENTS:
            setattr(self, field, beverages.get(getattr(self, f"{field}_id")))
        if not self.wants_meal:
            for component in self.PRICE_COMPONENTS:
                setattr(self, component, None)
                setattr(self, f"{component}_standard_surcharge_cents", 0)
        else:
            for component in self.PRICE_COMPONENTS:
                if previous is None or getattr(previous, f"{component}_id") != getattr(self, f"{component}_id"):
                    option = getattr(self, component)
                    standard = option.surcharge_cents if option is not None else 0
                else:
                    standard = getattr(previous, f"{component}_standard_surcharge_cents")
                    if standard is None:
                        standard = getattr(previous, f"{component}_surcharge_cents")
                setattr(self, f"{component}_standard_surcharge_cents", standard)
        is_current = (
            self.registration.status == Registration.Status.ACTIVE
            and self.registration.event_day.date >= timezone.localdate()
            and self.registration.event_day.event.status != Event.Status.ARCHIVED
        )
        from .official_pricing import registration_has_official_meal_exemption

        is_official = (
            registration_has_official_meal_exemption(self.registration)
            if previous is None or is_current else previous.official_exemption_applied
        )
        self.apply_official_pricing(is_official)
        self.beverage_availability = self.registration.availability if self.beverage_id else ""
        self.full_clean()
        if update_fields is not None:
            kwargs["update_fields"] = set(update_fields) | set(self.pricing_update_fields()) | {"beverage_availability"}
            if not self.wants_meal:
                kwargs["update_fields"].update(self.PRICE_COMPONENTS)
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
