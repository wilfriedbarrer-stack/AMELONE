from datetime import date, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from volunteers.models import (
    Assignment,
    Beverage,
    Event,
    EventDay,
    Family,
    MealOrder,
    Member,
    Menu,
    Payment,
    Registration,
    Station,
    StationSlot,
    User,
)


class Command(BaseCommand):
    help = "Crée un jeu de démonstration fictif et idempotent."

    @transaction.atomic
    def handle(self, *args, **options):
        now = timezone.now()
        first_day_date = timezone.localdate() + timedelta(days=7)
        second_day_date = first_day_date + timedelta(days=1)

        organizer, created = User.objects.get_or_create(
            email="organisateur@demo.invalid",
            defaults={
                "first_name": "Camille",
                "last_name": "Organisation",
                "is_organizer": True,
                "is_staff": True,
            },
        )
        if created:
            organizer.set_password("Demo-Amelone-2026!")
            organizer.save(update_fields=["password"])

        account, created = User.objects.get_or_create(
            email="famille@demo.invalid",
            defaults={
                "first_name": "Alex",
                "last_name": "Martin",
                "phone": "0600000000",
            },
        )
        if created:
            account.set_password("Demo-Amelone-2026!")
            account.save(update_fields=["password"])
        family, _ = Family.objects.get_or_create(account=account, defaults={"name": "Martin"})

        birthday_year = 1992 if (first_day_date.month, first_day_date.day) == (2, 29) else 1994
        member, _ = Member.objects.get_or_create(
            family=family,
            first_name="Léa",
            last_name="Martin",
            defaults={
                "birth_date": date(birthday_year, first_day_date.month, first_day_date.day),
            },
        )
        second_member, _ = Member.objects.get_or_create(
            family=family,
            first_name="Noé",
            last_name="Martin",
            defaults={"birth_date": date(2008, 5, 12)},
        )

        event, _ = Event.objects.get_or_create(
            name="Fête associative — démonstration",
            defaults={
                "description": "Événement fictif pour découvrir toutes les fonctions.",
                "location": "Salle des associations",
                "registration_opens_at": now - timedelta(days=2),
                "registration_closes_at": now + timedelta(days=4),
                "meal_orders_close_at": now + timedelta(days=5),
                "sumup_payment_url": "https://pay.sumup.com/b2c/example",
                "general_instructions": "Présentez-vous quinze minutes avant votre créneau.",
                "status": Event.Status.PUBLISHED,
            },
        )
        first_day, _ = EventDay.objects.get_or_create(event=event, date=first_day_date)
        second_day, _ = EventDay.objects.get_or_create(event=event, date=second_day_date)

        welcome, _ = Station.objects.get_or_create(
            event=event,
            name="Accueil",
            defaults={
                "description": "Accueil et orientation du public.",
                "instructions": "Récupérer le badge à l'entrée.",
                "position": 1,
            },
        )
        refreshments, _ = Station.objects.get_or_create(
            event=event,
            name="Buvette",
            defaults={
                "description": "Service des boissons.",
                "instructions": "Se présenter au responsable de la buvette.",
                "position": 2,
            },
        )
        official, _ = Station.objects.get_or_create(
            event=event,
            name="Chronométrage",
            defaults={
                "category": Station.Category.OFFICIAL,
                "description": "Poste officiel de chronométrage.",
                "position": 3,
            },
        )

        slots = {}
        for day in (first_day, second_day):
            for station, capacity in ((welcome, 3), (refreshments, 4), (official, 2)):
                for period, start, end in (
                    (StationSlot.Period.MORNING, time(8, 30), time(12, 30)),
                    (StationSlot.Period.AFTERNOON, time(13, 30), time(18, 0)),
                ):
                    slot, _ = StationSlot.objects.get_or_create(
                        station=station,
                        event_day=day,
                        period=period,
                        defaults={"capacity": capacity, "start_time": start, "end_time": end},
                    )
                    slots[(day.id, station.id, period)] = slot

        registration, _ = Registration.objects.get_or_create(
            member=member,
            event_day=first_day,
            defaults={
                "availability": Registration.Availability.FULL_DAY,
                "preferred_station": welcome,
                "note": "Peut aider à installer avant l'ouverture.",
            },
        )
        Registration.objects.get_or_create(
            member=second_member,
            event_day=second_day,
            defaults={"availability": Registration.Availability.MORNING},
        )

        for period in (StationSlot.Period.MORNING, StationSlot.Period.AFTERNOON):
            Assignment.objects.get_or_create(
                registration=registration,
                period=period,
                defaults={
                    "station_slot": slots[(first_day.id, welcome.id, period)],
                    "status": Assignment.Status.CONFIRMED,
                    "confirmed_at": now,
                    "confirmed_by": organizer,
                },
            )

        menu, _ = Menu.objects.get_or_create(
            event_day=first_day,
            name="Assiette végétarienne",
            defaults={"description": "Plat et dessert", "surcharge_cents": 550},
        )
        beverage, _ = Beverage.objects.get_or_create(
            event_day=first_day,
            name="Jus de pomme",
            defaults={"surcharge_cents": 150},
        )
        if not hasattr(registration, "meal_order"):
            MealOrder.objects.create(
                registration=registration,
                wants_meal=True,
                menu=menu,
                beverage=beverage,
            )
        Payment.objects.get_or_create(
            family=family,
            event=event,
            amount_cents=300,
            method=Payment.Method.ON_SITE,
            defaults={"recorded_by": organizer, "note": "Acompte fictif"},
        )

        self.stdout.write(self.style.SUCCESS("Données de démonstration prêtes."))
        self.stdout.write("Organisateur : organisateur@demo.invalid / Demo-Amelone-2026!")
        self.stdout.write("Famille : famille@demo.invalid / Demo-Amelone-2026!")
        self.stdout.write(self.style.WARNING("Ces comptes sont fictifs et réservés au développement."))

