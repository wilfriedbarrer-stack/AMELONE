from django.urls import path

from . import views, views_administration, views_family, views_organizer, views_public

urlpatterns = [
    path("organisateur/menus/<uuid:menu_id>/modifier/", views_organizer.menu_update, name="menu_update"),
    path("organisateur/boissons/<uuid:beverage_id>/modifier/", views_organizer.beverage_update, name="beverage_update"),
    path("administration/", views_administration.administration_dashboard, name="administration_dashboard"),
    path("administration/profils/", views_administration.profile_list, name="profile_list"),
    path("administration/profils/<uuid:user_id>/", views_administration.profile_update, name="profile_update"),
    path("administration/logo/", views_administration.branding_update, name="branding_update"),
    path("", views_public.home, name="home"),
    path("sante/", views.health, name="health"),
    path("tableau-de-bord/", views.dashboard, name="dashboard"),
    path("inscription/", views_family.signup, name="signup"),
    path("famille/", views_family.dashboard, name="family_dashboard"),
    path("famille/compte/", views_family.family_account, name="family_account"),
    path("famille/membres/", views_family.member_list, name="member_list"),
    path("famille/membres/ajouter/", views_family.member_create, name="member_create"),
    path(
        "famille/membres/<uuid:member_id>/modifier/",
        views_family.member_update,
        name="member_update",
    ),
    path(
        "famille/membres/<uuid:member_id>/archiver/",
        views_family.member_archive,
        name="member_archive",
    ),
    path("famille/evenements/", views_family.open_events, name="open_events"),
    path(
        "famille/evenements/<uuid:event_id>/",
        views_family.family_event_detail,
        name="family_event_detail",
    ),
    path(
        "famille/inscriptions/<uuid:event_day_id>/<uuid:member_id>/",
        views_family.family_registration_edit,
        name="family_registration_edit",
    ),
    path(
        "famille/inscriptions/<uuid:registration_id>/annuler/",
        views_family.family_registration_cancel,
        name="family_registration_cancel",
    ),
    path(
        "famille/inscriptions/<uuid:registration_id>/repas/",
        views_family.family_meal_edit,
        name="family_meal_edit",
    ),
    path("famille/affectations/", views_family.family_assignments, name="family_assignments"),
    path("famille/historique/", views_family.family_history, name="family_history"),
    path("evenements/", views_public.public_events, name="public_events"),
    path("planning/", views_public.public_planning, name="public_planning"),
    path(
        "planning/<uuid:event_id>/",
        views_public.public_planning,
        name="public_planning",
    ),
    path("organisateur/", views_organizer.organizer_dashboard, name="organizer_dashboard"),
    path("organisateur/evenements/", views_organizer.event_list, name="event_list"),
    path("organisateur/evenements/ajouter/", views_organizer.event_create, name="event_create"),
    path(
        "organisateur/evenements/<uuid:event_id>/",
        views_organizer.event_detail,
        name="event_detail",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/modifier/",
        views_organizer.event_update,
        name="event_update",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/dupliquer/",
        views_organizer.event_duplicate,
        name="event_duplicate",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/archiver/",
        views_organizer.event_archive,
        name="event_archive",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/postes/ajouter/",
        views_organizer.station_create,
        name="station_create",
    ),
    path(
        "organisateur/postes/<uuid:station_id>/modifier/",
        views_organizer.station_update,
        name="station_update",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/inscriptions/",
        views_organizer.registration_list,
        name="registration_list",
    ),
    path(
        "organisateur/inscriptions/<uuid:registration_id>/modifier/",
        views_organizer.registration_update,
        name="registration_update",
    ),
    path("organisateur/affectations/", views_organizer.assignment_board, name="assignment_board"),
    path(
        "organisateur/evenements/<uuid:event_id>/affectations/",
        views_organizer.assignment_board,
        name="assignment_board_event",
    ),
    path(
        "organisateur/inscriptions/<uuid:registration_id>/affecter/",
        views_organizer.assignment_create,
        name="assignment_create",
    ),
    path(
        "organisateur/affectations/<uuid:assignment_id>/modifier/",
        views_organizer.assignment_update_view,
        name="assignment_update",
    ),
    path(
        "organisateur/affectations/<uuid:assignment_id>/retirer/",
        views_organizer.assignment_remove,
        name="assignment_remove",
    ),
    path(
        "organisateur/affectations/<uuid:assignment_id>/confirmer/",
        views_organizer.assignment_confirm,
        name="assignment_confirm",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/affectations/confirmer/",
        views_organizer.event_assignments_confirm,
        name="event_assignments_confirm",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/emails/",
        views_organizer.event_emails,
        name="event_emails",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/repas/",
        views_organizer.catering,
        name="catering",
    ),
    path(
        "organisateur/journees/<uuid:day_id>/menus/ajouter/",
        views_organizer.menu_create,
        name="menu_create",
    ),
    path(
        "organisateur/journees/<uuid:day_id>/boissons/ajouter/",
        views_organizer.beverage_create,
        name="beverage_create",
    ),
    path(
        "organisateur/repas/<uuid:order_id>/modifier/",
        views_organizer.meal_order_update,
        name="meal_order_update",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/repas/export.csv",
        views_organizer.catering_export,
        name="catering_export",
    ),
    path(
        "organisateur/evenements/<uuid:event_id>/paiements/",
        views_organizer.payments,
        name="payments",
    ),
    path("organisateur/suivi/", views_organizer.annual_tracking, name="annual_tracking"),
    path(
        "organisateur/inscriptions/<uuid:registration_id>/presence/",
        views_organizer.attendance_update,
        name="attendance_update",
    ),
    path(
        "organisateur/suivi/export.csv",
        views_organizer.tracking_export,
        name="tracking_export",
    ),
]

