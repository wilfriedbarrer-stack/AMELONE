from django.contrib import admin
from django.contrib.auth.admin import GroupAdmin as DjangoGroupAdmin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import AdminUserCreationForm, UserChangeForm
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.urls import reverse
from django.utils.html import format_html

from .models import (
    Assignment,
    AssignmentCommunication,
    Attendance,
    Beverage,
    EmailAttempt,
    EmailDelivery,
    Event,
    EventDay,
    Family,
    LegacyImportRecord,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
    User,
)


class ReadOnlyTimestampsMixin:
    readonly_fields = ("id", "created_at", "updated_at")


class PreserveHistoryMixin:
    def has_delete_permission(self, request, obj=None):
        return False

    def delete_model(self, request, obj):
        raise PermissionDenied("Utilisez les écrans organisateur pour retirer une participation ou une affectation.")

    def delete_queryset(self, request, queryset):
        raise PermissionDenied("L’historique des participations et des affectations doit être conservé.")


class NormalizedEmailMixin:
    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exclude(pk=self.instance.pk).exists():
            raise ValidationError("Cette adresse e-mail est déjà utilisée.")
        return email


class AdminProfileCreationForm(NormalizedEmailMixin, AdminUserCreationForm):
    class Meta(AdminUserCreationForm.Meta):
        model = User
        fields = ("email",)


class AdminProfileChangeForm(NormalizedEmailMixin, UserChangeForm):
    class Meta(UserChangeForm.Meta):
        model = User
        fields = "__all__"


class AdministratorOnlyMixin:
    def has_module_permission(self, request):
        return request.user.is_active and request.user.is_superuser

    def has_view_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_add_permission(self, request):
        return self.has_module_permission(request)

    def has_change_permission(self, request, obj=None):
        return self.has_module_permission(request)

    def has_delete_permission(self, request, obj=None):
        return self.has_module_permission(request)


admin.site.unregister(Group)


@admin.register(Group)
class GroupAdmin(AdministratorOnlyMixin, DjangoGroupAdmin):
    pass


admin.site.site_header = "Amelone Loisirs — Administration"
admin.site.site_title = "Administration AMELONE"
admin.site.index_title = "Gestion de l’association"


@admin.register(User)
class UserAdmin(AdministratorOnlyMixin, DjangoUserAdmin):
    add_form = AdminProfileCreationForm
    form = AdminProfileChangeForm

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        users = {user.pk: user for user in User.objects.select_for_update().filter(
            pk__in={request.user.pk, obj.pk},
        ).order_by("pk")}
        actor = users.get(request.user.pk)
        if actor is None or not actor.is_active or not actor.is_superuser:
            raise PermissionDenied("Les droits administrateur sont nécessaires.")
        member = Member.objects.select_for_update().filter(user_id=obj.pk).first() if change else None
        if member:
            # L'habilitation est attribuée à une personne depuis l'écran familles.
            obj.is_competition_official = member.is_competition_official
        super().save_model(request, obj, form, change)
        if not (obj.is_staff or obj.is_superuser or obj.is_organizer):
            Family.objects.get_or_create(account=obj)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj and Member.objects.filter(user=obj).exists():
            fields = (*fields, "is_competition_official")
        if obj and obj.pk == request.user.pk:
            return (*fields, "is_active", "is_superuser", "is_staff")
        return fields

    @admin.display(description="Habilitation individuelle")
    def official_permission_management(self, obj):
        member = Member.objects.filter(user=obj).first() if obj else None
        if member:
            return format_html(
                '<a href="{}">Gérer l’habilitation officielle dans la famille</a>',
                reverse("administrator_family_detail", args=[member.family_id]),
            )
        return "L’habilitation individuelle sera attribuée après la création du profil personnel."

    def has_delete_permission(self, request, obj=None):
        return super().has_delete_permission(request, obj) and (
            obj is None or obj.pk != request.user.pk
        )

    def delete_queryset(self, request, queryset):
        if queryset.filter(pk=request.user.pk).exists():
            raise PermissionDenied("Vous ne pouvez pas supprimer votre propre compte administrateur.")
        super().delete_queryset(request, queryset)

    ordering = ("email",)
    list_display = (
        "email",
        "first_name",
        "last_name",
        "phone",
        "is_competition_official",
        "is_organizer",
        "is_staff",
        "is_active",
    )
    list_filter = ("is_competition_official", "is_organizer", "is_staff", "is_active", "groups")
    search_fields = ("email", "first_name", "last_name", "phone")
    readonly_fields = ("id", "last_login", "date_joined", "official_permission_management")
    fieldsets = (
        (None, {"fields": ("id", "email", "password")}),
        ("Identité", {"fields": ("first_name", "last_name", "phone")}),
        ("Participation", {"fields": ("is_competition_official", "official_permission_management")}),
        (
            "Droits",
            {
                "fields": (
                    "is_active",
                    "is_organizer",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Dates", {"fields": ("last_login", "date_joined")}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "email",
                    "first_name",
                    "last_name",
                    "phone",
                    "password1",
                    "password2",
                    "is_competition_official",
                    "is_organizer",
                    "is_staff",
                    "is_active",
                ),
            },
        ),
    )


class MemberInline(admin.TabularInline):
    model = Member
    extra = 0
    fields = ("first_name", "last_name", "birth_date", "archived_at")
    show_change_link = True


@admin.register(Family)
class FamilyAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("display_name", "account", "member_count", "created_at")
    search_fields = (
        "name",
        "account__email",
        "account__first_name",
        "account__last_name",
        "members__first_name",
        "members__last_name",
    )
    autocomplete_fields = ("account",)
    inlines = (MemberInline,)

    @transaction.atomic
    def save_formset(self, request, form, formset, change):
        if formset.model is not Member:
            return super().save_formset(request, form, formset, change)
        instances = formset.save(commit=False)
        for member in formset.deleted_objects:
            member.delete()
        for member in instances:
            if member._state.adding:
                member.is_competition_official = False
                member.user_id = None
                member.save()
            else:
                current = Member.objects.select_for_update().get(pk=member.pk)
                member.is_competition_official = current.is_competition_official
                member.user_id = current.user_id
                member.save(update_fields=[*MemberInline.fields, "updated_at"])
        formset.save_m2m()

    @admin.display(description="Membres")
    def member_count(self, obj):
        return obj.members.count()


@admin.register(Member)
class MemberAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("last_name", "first_name", "family", "birth_date", "current_age", "archived_at")
    list_filter = ("archived_at",)
    search_fields = ("first_name", "last_name", "family__name", "family__account__email")
    autocomplete_fields = ("family",)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if not request.user.is_superuser:
            return (*fields, "is_competition_official", "user")
        return (*fields, "user") if obj else fields

    @transaction.atomic
    def save_model(self, request, obj, form, change):
        user_ids = {request.user.pk}
        if obj.user_id:
            user_ids.add(obj.user_id)
        users = {user.pk: user for user in User.objects.select_for_update().filter(
            pk__in=user_ids,
        ).order_by("pk")}
        actor = users.get(request.user.pk)
        permission = "volunteers.change_member" if change else "volunteers.add_member"
        if actor is None or not actor.is_active or not actor.is_staff or not actor.has_perm(permission):
            raise PermissionDenied("Les droits de gestion de ce membre sont nécessaires.")
        previous = Member.objects.select_for_update().filter(pk=obj.pk).first() if change else None
        if previous and previous.user_id != obj.user_id:
            # Les changements de compte personnel passent par les écrans dédiés.
            raise PermissionDenied("Le compte personnel de ce membre ne peut pas être modifié ici.")
        if not actor.is_superuser:
            obj.is_competition_official = previous.is_competition_official if previous else False
            obj.user_id = previous.user_id if previous else None
        super().save_model(request, obj, form, change)
        if obj.user_id:
            linked_user = users[obj.user_id]
            linked_user.is_competition_official = obj.is_competition_official
            linked_user.save(update_fields=["is_competition_official"])

    @admin.display(description="Âge")
    def current_age(self, obj):
        return obj.age_on()


class EventDayInline(admin.TabularInline):
    model = EventDay
    extra = 1
    fields = ("date",)
    show_change_link = True
    can_delete = False

    def get_readonly_fields(self, request, obj=None):
        if obj and Registration.objects.filter(event_day__event=obj).exists():
            return ("date",)
        return ()

    def has_add_permission(self, request, obj=None):
        return super().has_add_permission(request, obj) and not (
            obj and Registration.objects.filter(event_day__event=obj).exists()
        )


class StationInline(admin.TabularInline):
    model = Station
    extra = 0
    fields = ("name", "category", "position", "is_active")
    show_change_link = True


@admin.register(Event)
class EventAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "name",
        "location",
        "status",
        "first_day",
        "registration_opens_at",
        "registration_closes_at",
        "meal_orders_close_at",
    )
    list_filter = ("status",)
    search_fields = ("name", "description", "location")
    date_hierarchy = "registration_opens_at"
    inlines = (EventDayInline, StationInline)

    @admin.display(description="Première journée", ordering="days__date")
    def first_day(self, obj):
        day = obj.days.order_by("date").first()
        return day.date if day else None


@admin.register(EventDay)
class EventDayAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("event", "date", "registration_count")
    list_filter = ("event", "date")
    search_fields = ("event__name", "event__location")
    autocomplete_fields = ("event",)
    date_hierarchy = "date"

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        return (*fields, "event", "date") if obj and obj.registrations.exists() else fields

    @admin.display(description="Inscriptions")
    def registration_count(self, obj):
        return obj.registrations.filter(status=Registration.Status.ACTIVE).count()


class StationSlotInline(admin.TabularInline):
    model = StationSlot
    extra = 0
    fields = ("event_day", "period", "capacity", "start_time", "end_time")
    autocomplete_fields = ("event_day",)
    show_change_link = True


@admin.register(Station)
class StationAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("name", "event", "category", "position", "is_active")
    list_filter = ("event", "category", "is_active")
    search_fields = ("name", "description", "instructions", "event__name")
    autocomplete_fields = ("event",)
    inlines = (StationSlotInline,)


@admin.register(StationSlot)
class StationSlotAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "station",
        "event_day",
        "period",
        "start_time",
        "end_time",
        "capacity",
        "active_assignment_count",
    )
    list_filter = ("period", "event_day__event", "event_day__date")
    search_fields = ("station__name", "station__event__name")
    autocomplete_fields = ("station", "event_day")

    @admin.display(description="Affectations actives")
    def active_assignment_count(self, obj):
        return obj.assignments.filter(
            status__in=(Assignment.Status.DRAFT, Assignment.Status.CONFIRMED)
        ).count()


class AssignmentInline(admin.TabularInline):
    model = Assignment
    extra = 0
    fields = ("station_slot", "period", "status", "confirmed_at", "cancelled_at")
    readonly_fields = ("period",)
    autocomplete_fields = ("station_slot",)
    show_change_link = True
    can_delete = False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(Registration)
class RegistrationAdmin(PreserveHistoryMixin, ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "member",
        "event_day",
        "availability",
        "preferred_station",
        "status",
        "age_on_event_day",
        "is_birthday",
    )
    list_filter = ("status", "availability", "event_day__event", "event_day__date")
    search_fields = (
        "member__first_name",
        "member__last_name",
        "member__family__name",
        "member__family__account__email",
        "event_day__event__name",
        "note",
    )
    autocomplete_fields = ("member", "event_day", "preferred_station")
    inlines = (AssignmentInline,)

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        if obj:
            return (*fields, "member", "event_day", "availability", "preferred_station",
                    "status", "organizer_management")
        return fields

    @admin.display(description="Modifier ou annuler l’inscription")
    def organizer_management(self, obj):
        return format_html(
            '<a href="{}">Ouvrir la gestion organisateur de cette inscription</a>',
            reverse("registration_update", args=[obj.pk]),
        )


class AssignmentCommunicationInline(admin.TabularInline):
    model = AssignmentCommunication
    extra = 0
    can_delete = False
    fields = ("version", "delivery", "signature", "communicated_at")
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(Assignment)
class AssignmentAdmin(PreserveHistoryMixin, ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "member_name",
        "station_slot",
        "period",
        "status",
        "confirmed_at",
        "cancelled_at",
        "communication_pending",
    )
    list_filter = ("status", "period", "station_slot__event_day__event", "station_slot__event_day__date")
    search_fields = (
        "registration__member__first_name",
        "registration__member__last_name",
        "station_slot__station__name",
        "station_slot__event_day__event__name",
    )
    autocomplete_fields = ("registration", "station_slot", "confirmed_by", "cancelled_by")
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + ("period",)
    inlines = (AssignmentCommunicationInline,)

    @admin.display(description="Membre", ordering="registration__member__last_name")
    def member_name(self, obj):
        return obj.registration.member.full_name

    @admin.display(description="À communiquer", boolean=True)
    def communication_pending(self, obj):
        return obj.needs_communication


class PricedOptionAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("name", "event_day", "surcharge_cents", "is_active")
    list_filter = ("is_active", "event_day__event", "event_day__date")
    search_fields = ("name", "description", "event_day__event__name")
    autocomplete_fields = ("event_day",)


admin.site.register(Menu, PricedOptionAdmin)
admin.site.register(Beverage, PricedOptionAdmin)


@admin.register(MealOrder)
class MealOrderAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "member_name",
        "event_day",
        "wants_meal",
        "menu",
        "beverage",
        "total_surcharge_cents",
    )
    list_filter = ("wants_meal", "registration__event_day__event", "registration__event_day__date")
    search_fields = (
        "registration__member__first_name",
        "registration__member__last_name",
        "menu__name",
        "beverage__name",
    )
    autocomplete_fields = ("registration", "menu", "beverage", "corrected_by")
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + (
        "menu_surcharge_cents",
        "beverage_surcharge_cents",
        "total_surcharge_cents",
    )

    @admin.display(description="Membre", ordering="registration__member__last_name")
    def member_name(self, obj):
        return obj.registration.member.full_name

    @admin.display(description="Journée", ordering="registration__event_day__date")
    def event_day(self, obj):
        return obj.registration.event_day


@admin.register(Payment)
class PaymentAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("family", "event", "amount_cents", "method", "paid_at", "recorded_by")
    list_filter = ("method", "event", "paid_at")
    search_fields = ("family__name", "family__account__email", "event__name", "note")
    autocomplete_fields = ("family", "event", "recorded_by")
    date_hierarchy = "paid_at"


@admin.register(Attendance)
class AttendanceAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("member", "event_day", "status", "recorded_by", "updated_at")
    list_filter = ("status", "event_day__event", "event_day__date")
    search_fields = ("member__first_name", "member__last_name", "event_day__event__name", "note")
    autocomplete_fields = ("member", "event_day", "recorded_by")


class EmailAttemptInline(admin.TabularInline):
    model = EmailAttempt
    extra = 0
    can_delete = False
    fields = (
        "attempt_number",
        "status",
        "attempted_at",
        "provider_message_id",
        "error_message",
    )
    readonly_fields = fields
    show_change_link = True

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(EmailDelivery)
class EmailDeliveryAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("family", "event", "kind", "status", "to_email", "last_attempt_at", "delivered_at")
    list_filter = ("kind", "status", "event")
    search_fields = (
        "family__name",
        "family__account__email",
        "to_email",
        "subject",
        "provider_message_id",
    )
    autocomplete_fields = ("event", "family")
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + (
        "idempotency_key",
        "provider_message_id",
        "last_attempt_at",
        "accepted_at",
        "delivered_at",
        "failure_reason",
    )
    inlines = (EmailAttemptInline,)


@admin.register(EmailAttempt)
class EmailAttemptAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("delivery", "attempt_number", "status", "attempted_at", "provider_message_id")
    list_filter = ("status", "attempted_at")
    search_fields = ("delivery__to_email", "provider_message_id", "error_message")
    autocomplete_fields = ("delivery",)
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + ("attempted_at",)


@admin.register(AssignmentCommunication)
class AssignmentCommunicationAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = ("assignment", "version", "delivery", "communicated_at")
    list_filter = ("delivery__event", "communicated_at")
    search_fields = (
        "assignment__registration__member__first_name",
        "assignment__registration__member__last_name",
        "delivery__to_email",
        "signature",
    )
    autocomplete_fields = ("assignment", "delivery")
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + (
        "assignment",
        "delivery",
        "version",
        "snapshot",
        "signature",
        "communicated_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(LegacyImportRecord)
class LegacyImportRecordAdmin(ReadOnlyTimestampsMixin, admin.ModelAdmin):
    list_display = (
        "source_type",
        "legacy_id",
        "status",
        "target_model",
        "target_uuid",
        "imported_at",
        "updated_at",
    )
    list_filter = ("source_type", "status", "target_model")
    search_fields = ("legacy_id", "content_fingerprint", "target_uuid")
    readonly_fields = ReadOnlyTimestampsMixin.readonly_fields + (
        "source_type",
        "legacy_id",
        "content_fingerprint",
        "payload",
        "target_model",
        "target_uuid",
        "imported_at",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
