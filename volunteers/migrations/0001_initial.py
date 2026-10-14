import uuid

import django.db.models.deletion
import django.db.models.expressions
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

import volunteers.models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ('auth', '0012_alter_user_first_name_max_length'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('is_superuser', models.BooleanField(default=False, help_text='Designates that this user has all permissions without explicitly assigning them.', verbose_name='superuser status')),
                ('first_name', models.CharField(blank=True, max_length=150, verbose_name='first name')),
                ('last_name', models.CharField(blank=True, max_length=150, verbose_name='last name')),
                ('is_staff', models.BooleanField(default=False, help_text='Designates whether the user can log into this admin site.', verbose_name='staff status')),
                ('is_active', models.BooleanField(default=True, help_text='Designates whether this user should be treated as active. Unselect this instead of deleting accounts.', verbose_name='active')),
                ('date_joined', models.DateTimeField(default=django.utils.timezone.now, verbose_name='date joined')),
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('email', models.EmailField(max_length=254, unique=True, verbose_name='adresse e-mail')),
                ('phone', models.CharField(blank=True, max_length=32, verbose_name='téléphone')),
                ('is_organizer', models.BooleanField(default=False, verbose_name='organisateur')),
                ('groups', models.ManyToManyField(blank=True, help_text='The groups this user belongs to. A user will get all permissions granted to each of their groups.', related_name='user_set', related_query_name='user', to='auth.group', verbose_name='groups')),
                ('user_permissions', models.ManyToManyField(blank=True, help_text='Specific permissions for this user.', related_name='user_set', related_query_name='user', to='auth.permission', verbose_name='user permissions')),
            ],
            options={
                'verbose_name': 'utilisateur',
                'verbose_name_plural': 'utilisateurs',
                'ordering': ('last_name', 'first_name', 'email'),
            },
            managers=[
                ('objects', volunteers.models.UserManager()),
            ],
        ),
        migrations.CreateModel(
            name='Event',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('name', models.CharField(max_length=200, verbose_name='nom')),
                ('description', models.TextField(blank=True, verbose_name='description')),
                ('location', models.CharField(max_length=255, verbose_name='lieu')),
                ('registration_opens_at', models.DateTimeField(verbose_name='ouverture des inscriptions')),
                ('registration_closes_at', models.DateTimeField(verbose_name='clôture des inscriptions')),
                ('meal_orders_close_at', models.DateTimeField(verbose_name='clôture des repas')),
                ('sumup_payment_url', models.URLField(blank=True, verbose_name='lien de paiement SumUp')),
                ('general_instructions', models.TextField(blank=True, verbose_name='consignes générales')),
                ('status', models.CharField(choices=[('draft', 'Brouillon'), ('published', 'Publié'), ('archived', 'Archivé')], default='draft', max_length=16, verbose_name='statut')),
            ],
            options={
                'verbose_name': 'événement',
                'verbose_name_plural': 'événements',
                'ordering': ('-registration_opens_at', 'name'),
                'indexes': [models.Index(fields=['status', 'registration_closes_at'], name='event_status_close_idx')],
                'constraints': [models.CheckConstraint(condition=models.Q(('registration_closes_at__gt', models.F('registration_opens_at'))), name='event_registration_window_valid')],
            },
        ),
        migrations.CreateModel(
            name='EventDay',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('date', models.DateField(verbose_name='date')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='days', to='volunteers.event', verbose_name='événement')),
            ],
            options={
                'verbose_name': "journée d'événement",
                'verbose_name_plural': "journées d'événement",
                'ordering': ('date',),
            },
        ),
        migrations.CreateModel(
            name='Beverage',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('name', models.CharField(max_length=160, verbose_name='nom')),
                ('description', models.TextField(blank=True, verbose_name='description')),
                ('surcharge_cents', models.PositiveIntegerField(default=0, verbose_name='supplément (centimes)')),
                ('is_active', models.BooleanField(default=True, verbose_name='actif')),
                ('event_day', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='beverages', to='volunteers.eventday', verbose_name='journée')),
            ],
            options={
                'verbose_name': 'boisson',
                'verbose_name_plural': 'boissons',
                'ordering': ('name',),
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Family',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('name', models.CharField(blank=True, max_length=150, verbose_name='nom de la famille')),
                ('account', models.OneToOneField(on_delete=django.db.models.deletion.PROTECT, related_name='family', to=settings.AUTH_USER_MODEL, verbose_name='compte responsable')),
            ],
            options={
                'verbose_name': 'famille',
                'verbose_name_plural': 'familles',
                'ordering': ('name', 'account__last_name', 'account__first_name'),
            },
        ),
        migrations.CreateModel(
            name='EmailDelivery',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('kind', models.CharField(choices=[('registration', "Accusé d'inscription"), ('assignment', "Confirmation d'affectation"), ('assignment_update', "Mise à jour d'affectation")], max_length=24, verbose_name='type')),
                ('status', models.CharField(choices=[('draft', 'Brouillon'), ('pending', 'En attente'), ('accepted', 'Accepté par le service'), ('delivered', 'Livré'), ('failed', 'Échec')], default='draft', max_length=16, verbose_name='statut')),
                ('to_email', models.EmailField(max_length=254, verbose_name='destinataire')),
                ('subject', models.CharField(max_length=255, verbose_name='objet')),
                ('body_text', models.TextField(verbose_name='contenu')),
                ('extra_instructions', models.TextField(blank=True, verbose_name='consignes complémentaires')),
                ('idempotency_key', models.UUIDField(default=uuid.uuid4, unique=True, verbose_name="clé d'idempotence")),
                ('provider_message_id', models.CharField(blank=True, max_length=255, verbose_name='identifiant fournisseur')),
                ('last_attempt_at', models.DateTimeField(blank=True, null=True, verbose_name='dernière tentative')),
                ('accepted_at', models.DateTimeField(blank=True, null=True, verbose_name='accepté le')),
                ('delivered_at', models.DateTimeField(blank=True, null=True, verbose_name='livré le')),
                ('failure_reason', models.TextField(blank=True, verbose_name="motif d'échec")),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='email_deliveries', to='volunteers.event', verbose_name='événement')),
                ('family', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='email_deliveries', to='volunteers.family', verbose_name='famille')),
            ],
            options={
                'verbose_name': "envoi d'e-mail",
                'verbose_name_plural': "envois d'e-mails",
                'ordering': ('-created_at',),
            },
        ),
        migrations.CreateModel(
            name='LegacyImportRecord',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('source_type', models.CharField(choices=[('event', 'Événement'), ('station', 'Poste ou créneau'), ('registration', 'Inscription'), ('other', 'Autre')], max_length=24, verbose_name='type de source')),
                ('legacy_id', models.CharField(max_length=255, verbose_name="identifiant d'origine")),
                ('content_fingerprint', models.CharField(db_index=True, max_length=64, verbose_name='empreinte du contenu')),
                ('target_model', models.CharField(blank=True, max_length=100, verbose_name='modèle cible')),
                ('target_uuid', models.UUIDField(blank=True, null=True, verbose_name='identifiant cible')),
                ('status', models.CharField(choices=[('pending', 'À traiter'), ('imported', 'Importée'), ('needs_review', 'À compléter ou rattacher'), ('skipped', 'Ignorée'), ('failed', 'Échec')], default='pending', max_length=24, verbose_name='statut')),
                ('payload', models.JSONField(default=dict, verbose_name='données source')),
                ('report', models.JSONField(default=dict, verbose_name="rapport d'import")),
                ('imported_at', models.DateTimeField(blank=True, null=True, verbose_name='importée le')),
            ],
            options={
                'verbose_name': "ligne d'import historique",
                'verbose_name_plural': "lignes d'import historique",
                'ordering': ('source_type', 'legacy_id'),
                'indexes': [models.Index(fields=['status', 'source_type'], name='legacy_status_source_idx')],
                'constraints': [models.UniqueConstraint(fields=('source_type', 'legacy_id'), name='unique_legacy_source_record'), models.CheckConstraint(condition=models.Q(models.Q(('target_model', ''), ('target_uuid__isnull', True)), models.Q(models.Q(('target_model', ''), _negated=True), ('target_uuid__isnull', False)), _connector='OR'), name='legacy_target_both_set_or_empty')],
            },
        ),
        migrations.CreateModel(
            name='Member',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('first_name', models.CharField(max_length=150, verbose_name='prénom')),
                ('last_name', models.CharField(max_length=150, verbose_name='nom')),
                ('birth_date', models.DateField(verbose_name='date de naissance')),
                ('archived_at', models.DateTimeField(blank=True, null=True, verbose_name='archivé le')),
                ('family', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='members', to='volunteers.family', verbose_name='famille')),
            ],
            options={
                'verbose_name': 'membre',
                'verbose_name_plural': 'membres',
                'ordering': ('last_name', 'first_name', 'birth_date'),
            },
        ),
        migrations.CreateModel(
            name='Attendance',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('status', models.CharField(choices=[('unknown', 'Non renseignée'), ('present', 'Présent'), ('absent', 'Absent')], default='unknown', max_length=16, verbose_name='présence')),
                ('note', models.TextField(blank=True, verbose_name='remarque')),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recorded_attendances', to=settings.AUTH_USER_MODEL, verbose_name='renseignée par')),
                ('event_day', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attendances', to='volunteers.eventday', verbose_name='journée')),
                ('member', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='attendances', to='volunteers.member', verbose_name='membre')),
            ],
            options={
                'verbose_name': 'présence',
                'verbose_name_plural': 'présences',
                'ordering': ('-event_day__date', 'member__last_name', 'member__first_name'),
            },
        ),
        migrations.CreateModel(
            name='Menu',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('name', models.CharField(max_length=160, verbose_name='nom')),
                ('description', models.TextField(blank=True, verbose_name='description')),
                ('surcharge_cents', models.PositiveIntegerField(default=0, verbose_name='supplément (centimes)')),
                ('is_active', models.BooleanField(default=True, verbose_name='actif')),
                ('event_day', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='menus', to='volunteers.eventday', verbose_name='journée')),
            ],
            options={
                'verbose_name': 'menu',
                'verbose_name_plural': 'menus',
                'ordering': ('name',),
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Payment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('amount_cents', models.PositiveIntegerField(verbose_name='montant (centimes)')),
                ('paid_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='réglé le')),
                ('method', models.CharField(choices=[('on_site', 'Sur place'), ('sumup', 'SumUp')], max_length=16, verbose_name='mode')),
                ('note', models.TextField(blank=True, verbose_name='remarque')),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='volunteers.event', verbose_name='événement')),
                ('family', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='volunteers.family', verbose_name='famille')),
                ('recorded_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recorded_payments', to=settings.AUTH_USER_MODEL, verbose_name='enregistré par')),
            ],
            options={
                'verbose_name': 'paiement',
                'verbose_name_plural': 'paiements',
                'ordering': ('-paid_at',),
            },
        ),
        migrations.CreateModel(
            name='Registration',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('availability', models.CharField(choices=[('full_day', 'Journée entière'), ('morning', 'Matin'), ('afternoon', 'Après-midi')], max_length=16, verbose_name='disponibilité')),
                ('note', models.TextField(blank=True, verbose_name='remarque privée')),
                ('status', models.CharField(choices=[('active', 'Active'), ('cancelled', 'Annulée')], default='active', max_length=16, verbose_name='statut')),
                ('event_day', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='registrations', to='volunteers.eventday', verbose_name='journée')),
                ('member', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='registrations', to='volunteers.member', verbose_name='membre')),
            ],
            options={
                'verbose_name': 'inscription',
                'verbose_name_plural': 'inscriptions',
                'ordering': ('event_day__date', 'member__last_name', 'member__first_name'),
            },
        ),
        migrations.CreateModel(
            name='MealOrder',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('wants_meal', models.BooleanField(default=False, verbose_name='repas demandé')),
                ('menu_surcharge_cents', models.PositiveIntegerField(default=0, editable=False, verbose_name='supplément menu appliqué')),
                ('beverage_surcharge_cents', models.PositiveIntegerField(default=0, editable=False, verbose_name='supplément boisson appliqué')),
                ('total_surcharge_cents', models.PositiveIntegerField(default=0, editable=False, verbose_name='supplément total')),
                ('beverage', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='orders', to='volunteers.beverage', verbose_name='boisson')),
                ('corrected_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='corrected_meal_orders', to=settings.AUTH_USER_MODEL, verbose_name='corrigé par')),
                ('menu', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='orders', to='volunteers.menu', verbose_name='menu')),
                ('registration', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='meal_order', to='volunteers.registration', verbose_name='inscription')),
            ],
            options={
                'verbose_name': 'réservation de repas',
                'verbose_name_plural': 'réservations de repas',
                'ordering': ('registration__event_day__date', 'registration__member__last_name'),
            },
        ),
        migrations.CreateModel(
            name='Station',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('name', models.CharField(max_length=160, verbose_name='nom')),
                ('description', models.TextField(blank=True, verbose_name='description')),
                ('instructions', models.TextField(blank=True, verbose_name='consignes')),
                ('category', models.CharField(choices=[('volunteer', 'Bénévole'), ('official', 'Officiel')], default='volunteer', max_length=16, verbose_name='catégorie')),
                ('is_active', models.BooleanField(default=True, verbose_name='actif')),
                ('position', models.PositiveSmallIntegerField(default=0, verbose_name="ordre d'affichage")),
                ('event', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='stations', to='volunteers.event', verbose_name='événement')),
            ],
            options={
                'verbose_name': 'poste',
                'verbose_name_plural': 'postes',
                'ordering': ('position', 'name'),
            },
        ),
        migrations.AddField(
            model_name='registration',
            name='preferred_station',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='preference_registrations', to='volunteers.station', verbose_name='poste souhaité'),
        ),
        migrations.CreateModel(
            name='StationSlot',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('period', models.CharField(choices=[('morning', 'Matin'), ('afternoon', 'Après-midi')], max_length=16, verbose_name='période')),
                ('capacity', models.PositiveSmallIntegerField(default=0, verbose_name='personnes nécessaires')),
                ('start_time', models.TimeField(blank=True, null=True, verbose_name='heure de début')),
                ('end_time', models.TimeField(blank=True, null=True, verbose_name='heure de fin')),
                ('event_day', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='station_slots', to='volunteers.eventday', verbose_name='journée')),
                ('station', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='slots', to='volunteers.station', verbose_name='poste')),
            ],
            options={
                'verbose_name': 'créneau de poste',
                'verbose_name_plural': 'créneaux de poste',
                'ordering': ('event_day__date', 'period', 'station__position', 'station__name'),
            },
        ),
        migrations.CreateModel(
            name='Assignment',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('period', models.CharField(choices=[('morning', 'Matin'), ('afternoon', 'Après-midi')], editable=False, max_length=16, verbose_name='période')),
                ('status', models.CharField(choices=[('draft', 'Brouillon'), ('confirmed', 'Confirmée'), ('cancelled', 'Retirée')], default='draft', max_length=16, verbose_name='statut')),
                ('confirmed_at', models.DateTimeField(blank=True, null=True, verbose_name='confirmée le')),
                ('cancelled_at', models.DateTimeField(blank=True, null=True, verbose_name='retirée le')),
                ('cancellation_reason', models.TextField(blank=True, verbose_name='motif du retrait')),
                ('cancelled_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='cancelled_assignments', to=settings.AUTH_USER_MODEL, verbose_name='retirée par')),
                ('confirmed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='confirmed_assignments', to=settings.AUTH_USER_MODEL, verbose_name='confirmée par')),
                ('registration', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='assignments', to='volunteers.registration', verbose_name='inscription')),
                ('station_slot', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='assignments', to='volunteers.stationslot', verbose_name='créneau')),
            ],
            options={
                'verbose_name': 'affectation',
                'verbose_name_plural': 'affectations',
                'ordering': ('station_slot__event_day__date', 'period', 'station_slot__station__position'),
            },
        ),
        migrations.CreateModel(
            name='EmailAttempt',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('attempt_number', models.PositiveSmallIntegerField(verbose_name='numéro de tentative')),
                ('status', models.CharField(choices=[('pending', 'En attente'), ('accepted', 'Acceptée'), ('delivered', 'Livrée'), ('failed', 'Échec')], max_length=16, verbose_name='statut')),
                ('provider_message_id', models.CharField(blank=True, max_length=255, verbose_name='identifiant fournisseur')),
                ('error_message', models.TextField(blank=True, verbose_name='erreur')),
                ('attempted_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='tentée le')),
                ('delivery', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='attempts', to='volunteers.emaildelivery', verbose_name='envoi')),
            ],
            options={
                'verbose_name': "tentative d'e-mail",
                'verbose_name_plural': "tentatives d'e-mail",
                'ordering': ('delivery', 'attempt_number'),
                'constraints': [models.UniqueConstraint(fields=('delivery', 'attempt_number'), name='unique_email_attempt_number')],
            },
        ),
        migrations.CreateModel(
            name='AssignmentCommunication',
            fields=[
                ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='créé le')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='modifié le')),
                ('version', models.PositiveSmallIntegerField(verbose_name='version')),
                ('snapshot', models.JSONField(verbose_name='instantané communiqué')),
                ('signature', models.CharField(max_length=64, verbose_name='empreinte')),
                ('communicated_at', models.DateTimeField(default=django.utils.timezone.now, verbose_name='communiquée le')),
                ('assignment', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='communications', to='volunteers.assignment', verbose_name='affectation')),
                ('delivery', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='assignment_communications', to='volunteers.emaildelivery', verbose_name='envoi')),
            ],
            options={
                'verbose_name': "communication d'affectation",
                'verbose_name_plural': "communications d'affectation",
                'ordering': ('assignment', '-version'),
                'constraints': [models.UniqueConstraint(fields=('assignment', 'version'), name='unique_assignment_communication_version'), models.UniqueConstraint(fields=('assignment', 'delivery'), name='unique_assignment_per_delivery')],
            },
        ),
        migrations.AddConstraint(
            model_name='eventday',
            constraint=models.UniqueConstraint(fields=('event', 'date'), name='unique_event_day'),
        ),
        migrations.AddConstraint(
            model_name='beverage',
            constraint=models.UniqueConstraint(fields=('event_day', 'name'), name='unique_beverage_name_day'),
        ),
        migrations.AddIndex(
            model_name='emaildelivery',
            index=models.Index(fields=['event', 'family', 'status'], name='email_event_family_idx'),
        ),
        migrations.AddIndex(
            model_name='member',
            index=models.Index(fields=['family', 'archived_at'], name='member_family_arch_idx'),
        ),
        migrations.AddIndex(
            model_name='member',
            index=models.Index(fields=['last_name', 'first_name'], name='member_name_idx'),
        ),
        migrations.AddConstraint(
            model_name='attendance',
            constraint=models.UniqueConstraint(fields=('member', 'event_day'), name='unique_attendance_member_day'),
        ),
        migrations.AddConstraint(
            model_name='menu',
            constraint=models.UniqueConstraint(fields=('event_day', 'name'), name='unique_menu_name_day'),
        ),
        migrations.AddIndex(
            model_name='payment',
            index=models.Index(fields=['family', 'event'], name='payment_family_event_idx'),
        ),
        migrations.AddConstraint(
            model_name='payment',
            constraint=models.CheckConstraint(condition=models.Q(('amount_cents__gt', 0)), name='payment_amount_positive'),
        ),
        migrations.AddConstraint(
            model_name='mealorder',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('menu__isnull', False), ('wants_meal', True)), models.Q(('beverage__isnull', True), ('menu__isnull', True), ('wants_meal', False)), _connector='OR'), name='meal_order_coherent_choice'),
        ),
        migrations.AddConstraint(
            model_name='mealorder',
            constraint=models.CheckConstraint(condition=models.Q(('total_surcharge_cents', django.db.models.expressions.CombinedExpression(models.F('menu_surcharge_cents'), '+', models.F('beverage_surcharge_cents')))), name='meal_order_total_matches_parts'),
        ),
        migrations.AddConstraint(
            model_name='station',
            constraint=models.UniqueConstraint(fields=('event', 'name'), name='unique_station_name_per_event'),
        ),
        migrations.AddIndex(
            model_name='registration',
            index=models.Index(fields=['event_day', 'status', 'availability'], name='reg_day_status_avail_idx'),
        ),
        migrations.AddConstraint(
            model_name='registration',
            constraint=models.UniqueConstraint(fields=('member', 'event_day'), name='unique_member_registration_day'),
        ),
        migrations.AddConstraint(
            model_name='stationslot',
            constraint=models.UniqueConstraint(fields=('station', 'event_day', 'period'), name='unique_station_day_period'),
        ),
        migrations.AddConstraint(
            model_name='stationslot',
            constraint=models.CheckConstraint(condition=models.Q(models.Q(('end_time__isnull', True), ('start_time__isnull', True)), models.Q(('end_time__isnull', False), ('start_time__isnull', False)), _connector='OR'), name='slot_times_both_set_or_empty'),
        ),
        migrations.AddConstraint(
            model_name='stationslot',
            constraint=models.CheckConstraint(condition=models.Q(('start_time__isnull', True), ('end_time__gt', models.F('start_time')), _connector='OR'), name='slot_end_after_start'),
        ),
        migrations.AddIndex(
            model_name='assignment',
            index=models.Index(fields=['station_slot', 'status'], name='assign_slot_status_idx'),
        ),
        migrations.AddIndex(
            model_name='assignment',
            index=models.Index(fields=['registration', 'status'], name='assign_reg_status_idx'),
        ),
        migrations.AddConstraint(
            model_name='assignment',
            constraint=models.UniqueConstraint(condition=models.Q(('status__in', ('draft', 'confirmed'))), fields=('registration', 'period'), name='unique_active_assignment_reg_period'),
        ),
    ]
