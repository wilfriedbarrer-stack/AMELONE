# Configuration des emails

L'envoi est désactivé par défaut. Tant que `EMAIL_ENABLED=1` et les paramètres SMTP ne
sont pas présents, l'interface affiche la limite et aucun message n'est marqué envoyé.

Variables nécessaires :

```dotenv
EMAIL_ENABLED=1
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.org
EMAIL_PORT=587
EMAIL_HOST_USER=compte-smtp
EMAIL_HOST_PASSWORD=secret-fourni-hors-du-depot
EMAIL_USE_TLS=1
DEFAULT_FROM_EMAIL=AMELONE <benevoles@example.org>
SITE_URL=https://benevoles.example.org
```

Le mot de passe SMTP doit être stocké dans le gestionnaire de secrets de l'hébergeur,
jamais dans Git ni dans le navigateur.

Le workflow permet un aperçu, un envoi par famille ou sélection, et une relance des seuls
échecs. Chaque tentative est historisée. Un statut « accepté » signifie uniquement que
le backend SMTP n'a pas rejeté l'envoi. Après modification d'une affectation déjà
communiquée, son empreinte diffère du dernier instantané et l'interface demande une mise
à jour.


## Brevo, notifications et Render Free

La configuration SMTP ci-dessus concerne `EMAIL_PROVIDER=django`. Avec
`EMAIL_PROVIDER=brevo`, le transport des notifications utilise l’API HTTPS Brevo,
pas `EMAIL_HOST` ni les identifiants SMTP. Ce choix convient à Render Free, qui
bloque les sorties SMTP sur les ports 25, 465 et 587.
Source : [limitations des services gratuits Render](https://render.com/docs/free).

### Offre et prérequis du prestataire

Pour démarrer avec un seul compte email et SMS, Brevo est le choix proposé.
Son offre gratuite permet 300 emails par jour, sans limite de durée ; le quota
inutilisé n’est pas reporté au lendemain. Les SMS sont payants et s’achètent
séparément : consulter le calculateur France dans le compte avant tout achat.
Le nombre de crédits consommés dépend notamment de la destination et de la
longueur du message.
Sources : [offre gratuite Brevo](https://help.brevo.com/hc/fr/articles/208580669-FAQ-Quels-sont-les-limites-de-l-offre-Gratuit),
[crédits et tarifs SMS](https://help.brevo.com/hc/fr/articles/208717449-Tarification-des-SMS-et-pays-pris-en-charge).

Avant d’activer les canaux :

1. Créer le compte Brevo de l’association, puis configurer et faire valider
   l’expéditeur email, avec l’authentification de domaine demandée par Brevo.
2. Créer une clé API privée. Elle reste dans les variables du serveur, jamais
   dans le dépôt, les pages publiques ou les journaux.
3. Pour les SMS, faire enregistrer et approuver l’identifiant d’expéditeur pour
   la France, par exemple `AMELONE`, puis disposer de crédits suffisants.
   L’enregistrement est également nécessaire pour les SMS transactionnels.
4. Vérifier le fonctionnement sur les coordonnées du responsable avant
   d’autoriser les campagnes vers les familles.

Sources : [expéditeurs et domaines Brevo](https://developers.brevo.com/docs/getting-started-with-senders-and-domains),
[enregistrer un expéditeur SMS](https://help.brevo.com/hc/fr/articles/28255350696466-Enregistrer-un-ID-d-exp%C3%A9diteur-pour-envoyer-des-SMS).

La préparation du code ou la présence d’une clé ne crée pas un compte prestataire,
ne vérifie pas le solde de crédits et ne prouve pas l’approbation de l’expéditeur.

### Variables de l’application

Le transport lit les variables suivantes. L’exemple conserve tous les envois
désactivés tant que le compte et ses expéditeurs ne sont pas prêts :

```dotenv
EMAIL_PROVIDER=brevo
EMAIL_ENABLED=0
BREVO_API_KEY=cle-privee-a-renseigner-sur-le-serveur
DEFAULT_FROM_EMAIL=AMELONE <adresse-expeditrice-validee@example.org>
SITE_URL=https://amelone.onrender.com
SMS_PROVIDER=brevo
SMS_ENABLED=0
SMS_SENDER=AMELONE
EVENT_NOTIFICATIONS_ENABLED=0
```

`EMAIL_ENABLED=1` autorise le canal email ; `SMS_ENABLED=1` autorise le canal SMS.
Le commutateur `EVENT_NOTIFICATIONS_ENABLED=1` autorise le traitement des campagnes
d’événements, sous réserve de leur activation et de leur échéance. Conserver ce
commutateur à `0` pendant la préparation. Un canal peut rester désactivé même si
l’autre est prêt. `SMS_SENDER` comporte de 3 à 11 lettres ou chiffres et au moins
une lettre ; il doit correspondre à l’expéditeur approuvé par Brevo.

Les appels du transport utilisent les endpoints HTTPS
`https://api.brevo.com/v3/smtp/email` et
`https://api.brevo.com/v3/transactionalSMS/send`.
La clé API est envoyée uniquement dans l’en-tête d’authentification du serveur.
Une acceptation par le prestataire ne prouve pas la livraison ni la lecture.
Les tentatives et résultats sont conservés ; un résultat incertain demande une
vérification avant une éventuelle nouvelle tentative.
Sources : [API email Brevo](https://developers.brevo.com/reference/send-transac-email),
[API SMS transactionnels](https://developers.brevo.com/docs/transactional-sms-endpoints).

### Automatisation retenue : GitHub Actions toutes les 15 minutes

Les points d’entrée du dépôt sont le
[workflow GitHub](../.github/workflows/event-notifications.yml), la
[vérification OIDC](../volunteers/notification_job_auth.py),
l’[endpoint de traitement](../volunteers/views_notification_job.py) et la
[commande de traitement](../volunteers/management/commands/process_event_notifications.py).

L’ordonnanceur retenu est GitHub Actions, avec un runner standard et un appel
HTTPS à l’application. Il n’utilise ni service Render Cron payant, ni secret
GitHub à créer manuellement. Le workflow est planifié avec
`7,22,37,52 * * * *` : il se déclenche aux minutes 07, 22, 37 et 52 de chaque heure,
soit toutes les 15 minutes. Les heures du cron sont en UTC.

Le workflow obtient un **jeton d’identité OIDC temporaire** auprès de GitHub avec
la permission `id-token: write`, puis appelle **POST `/notifications/traiter/`**.
Le serveur vérifie la signature avec les clés publiques GitHub (JWKS), l’émetteur,
l’expiration, l’audience `amelone-event-notifications` et l’identité autorisée :
identifiants du dépôt et de son propriétaire, branche `main`, chemin exact du
workflow et déclenchement `schedule` ou `workflow_dispatch`. Un jeton pour un
autre dépôt, une autre branche ou un autre workflow ne doit pas autoriser un
traitement.

Les identités attendues sont fixées dans
[les paramètres Django](../amelone/settings.py), sous le préfixe
`EVENT_NOTIFICATIONS_JOB_` : audience, dépôt `wilfriedbarrer-stack/AMELONE`,
identifiants du dépôt et du propriétaire, référence `refs/heads/main` et
`WORKFLOW_REF` exact. Ces valeurs ne sont pas des secrets et ne nécessitent
aucune saisie manuelle dans GitHub ou Render. L’émetteur GitHub et l’URL de ses
clés publiques sont également épinglés dans la vérification OIDC.

Le workflow n’accède ni à PostgreSQL ni à la clé Brevo. Aucun jeton permanent
partagé n’est nécessaire ; le jeton OIDC temporaire reste dans l’en-tête HTTPS,
jamais dans une URL, une capture ou un journal. Les clés du prestataire restent
exclusivement dans les variables du service Render.

Chaque appel traite au maximum une tentative. Le délai réseau du transport
Brevo est limité à dix secondes ; cela ne borne pas la durée complète de la
requête, qui comprend aussi la vérification OIDC et les accès à la base.
Le workflow boucle au plus 100 fois, avec un budget de boucle de huit minutes
et une limite de job de dix minutes. Il renouvelle son jeton après deux minutes
si nécessaire. Cela borne le travail sans lancer un traitement
long en arrière-plan dans le processus web. Les campagnes, échéances et
résultats restent enregistrés dans PostgreSQL ; les protections contre les
doubles envois s’appliquent côté application. Une réponse ou une exécution
incertaine demande une vérification avant de réessayer.

Le traitement applicatif reste également accessible en ligne de commande :

```bash
python manage.py process_event_notifications --dry-run
python manage.py process_event_notifications --limit 100
```

`--dry-run` affiche les volumes sans écrire ni envoyer. La seconde commande peut
envoyer réellement lorsque les commutateurs, campagnes et canaux l’autorisent.
Le workflow périodique ne contourne pas ces réglages : conserver
`EVENT_NOTIFICATIONS_ENABLED=0`, `EMAIL_ENABLED=0` et `SMS_ENABLED=0` tant que le
prestataire n’est pas configuré et que les envois ne sont pas autorisés.

Les runners standard sont gratuits pour un dépôt public ; un dépôt privé dépend
des quotas du compte. Les déclenchements planifiés peuvent être retardés ou
abandonnés ; dans un dépôt public, GitHub désactive la planification après
60 jours sans activité. Le traitement doit retrouver les tâches arrivées à
échéance lors de l’exécution suivante, sans dépendre du disque temporaire du
runner. Une heure exacte de réception n’est pas garantie ; le réveil d’un
service Render Free peut ajouter environ une minute.
Si GitHub a désactivé le workflow, ouvrir
[Actions → Notifications des événements](https://github.com/wilfriedbarrer-stack/AMELONE/actions/workflows/event-notifications.yml),
choisir **Enable workflow**, puis **Run workflow** pour vérifier la reprise.
Les commutateurs d’envoi restent applicables à cette exécution manuelle.
Sources : [facturation GitHub Actions](https://docs.github.com/en/billing/concepts/product-billing/github-actions),
[restrictions des planifications](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule),
[limitations Render Free](https://render.com/docs/free).

L’intervention nécessaire côté association reste la préparation du compte Brevo,
la validation des expéditeurs et l’achat de crédits si les SMS sont souhaités.
Renseigner la clé et l’adresse expéditrice dans Render, hors du chat et du dépôt,
puis autoriser séparément les canaux prêts. La configuration OIDC ne nécessite
pas de recopier une clé GitHub dans Render ni de transmettre la clé Brevo à
GitHub Actions.

**Render Cron n’est pas retenu et aucun service cron payant n’est créé.** Pour
comparaison, Render facture au minimum **1 USD par mois par service cron**, avec
facturation du calcul à la seconde et une seule exécution simultanée par tâche.
Source : [Render Cron Jobs](https://render.com/docs/cronjobs).
