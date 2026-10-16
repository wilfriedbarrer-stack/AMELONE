# Configuration des emails

Le choix retenu pour AMELONE est **Brevo pour les emails automatiques et WhatsApp
manuel dans le groupe bénévoles**. L'envoi email reste désactivé tant que le
compte Brevo, la clé API et l'adresse expéditrice ne sont pas prêts. Aucun SMS
n'est envoyé.

## Partage manuel WhatsApp

Dans un événement, ouvrir **Autres actions → Annonces et rappels · WhatsApp et
emails**. Les messages « nouvel événement » et « postes à pourvoir » sont prêts à
partager. **Ouvrir WhatsApp** ouvre un lien générique `wa.me/?text=…` : choisir le
groupe bénévoles et confirmer l'envoi dans WhatsApp. Le bouton **Copier le
message** utilise le presse-papiers du navigateur ; si l'accès est refusé, le
texte est sélectionné pour une copie manuelle. Sans JavaScript, le lien et la
zone de texte restent utilisables.

Seuls les détails de l'événement et les postes disponibles sont inclus, sans
coordonnées ni noms de participants. Le groupe entier reçoit le message : le
rappel s'adresse donc explicitement aux personnes pas encore inscrites, sans
prétendre filtrer le groupe. La liste est limitée à huit créneaux et quatre
dates, avec un lien vers le détail complet. Les places peuvent changer après la
préparation du message.

Le partage demande un événement publié, avec une journée présente ou future et
une clôture à venir. Le rappel exige aussi des inscriptions ouvertes et des
places libres. Il fonctionne indépendamment des réglages email. Aucun envoi
automatique WhatsApp à 48 h, appel à une API Meta, numéro de groupe stocké ou
statut « envoyé » n'est créé lors d'un clic. Aucun abonnement professionnel ni
achat de crédits WhatsApp/SMS n'est nécessaire à cette fonction.
Source : [partage WhatsApp avec texte préparé](https://faq.whatsapp.com/5913398998672934).

## Configuration SMTP alternative

Cette alternative concerne un autre hébergement ou une offre autorisant SMTP.
Pour le service Render Free actuel, utiliser la configuration Brevo HTTPS
ci-dessous.

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

Brevo est retenu pour les emails ; WhatsApp reste un partage manuel.
Son offre gratuite permet 300 emails par jour, sans limite de durée ; le quota
inutilisé n’est pas reporté au lendemain. Aucun crédit SMS n'est à acheter pour
la configuration retenue.
Source : [offre gratuite Brevo](https://help.brevo.com/hc/fr/articles/208580669-FAQ-Quels-sont-les-limites-de-l-offre-Gratuit).

Avant d’activer les emails :

1. Créer le compte Brevo de l’association, puis configurer et faire valider
   l’expéditeur email, avec l’authentification de domaine demandée par Brevo.
2. Créer une clé API privée. Elle reste dans les variables du serveur, jamais
   dans le dépôt, les pages publiques ou les journaux.
3. Vérifier le fonctionnement sur les coordonnées du responsable avant
   d’autoriser les campagnes vers les familles.

Source : [expéditeurs et domaines Brevo](https://developers.brevo.com/docs/getting-started-with-senders-and-domains).

La préparation du code ou la présence d’une clé ne crée pas un compte prestataire,
ne prouve pas l’approbation de l’expéditeur.

### Vérifier la connexion sans envoyer de message

La commande `python manage.py check_brevo_connection` contrôle la clé déjà
stockée sur le serveur et l'expéditeur configuré, via les lectures HTTPS Brevo
`GET /account` et `GET /senders`. Elle ne consulte pas les familles, ne crée
aucune notification et n'envoie aucun email. Sa sortie contient seulement des
états et un motif générique, sans clé, adresse de compte ni réponse brute.

Pour un contrôle ponctuel sur Render, définir `BREVO_VERIFY_ON_START=1`, en
gardant `EMAIL_ENABLED=0` et `EVENT_NOTIFICATIONS_ENABLED=0`. Le déploiement
effectue la vérification au démarrage du service web, depuis le même contexte
réseau que les emails, et écrit un résultat `BREVO_CONNECTION_CHECK` dans ses
journaux. Le contrôle tourne une seule fois par démarrage du serveur, dans un
sous-processus borné à 25 secondes ; il ne bloque pas la publication de WhatsApp
si Brevo n'est pas prêt. Après un résultat positif, remettre `BREVO_VERIFY_ON_START=0` avant
d'activer les emails. Le contrôle confirme l'accès au compte et la validation
de l'expéditeur, pas la livraison d'un message.

L'ancienne sonde de build `BREVO_VERIFY_ON_DEPLOY` n'est plus utilisée : les
serveurs de construction peuvent avoir des adresses IP différentes du service
web. Une erreur 403 est classée sans afficher la réponse du prestataire, afin
de distinguer une IP non autorisée, un compte suspendu ou en attente d'activation,
des droits insuffisants et un refus générique.

### Variables de l’application

Le transport lit les variables suivantes. L’exemple conserve tous les envois
désactivés tant que le compte et ses expéditeurs ne sont pas prêts :

```dotenv
EMAIL_PROVIDER=brevo
EMAIL_BACKEND=
EMAIL_ENABLED=0
BREVO_API_KEY=cle-privee-a-renseigner-sur-le-serveur
DEFAULT_FROM_EMAIL=AMELONE <adresse-expeditrice-validee@example.org>
SITE_URL=https://amelone.onrender.com
SMS_ENABLED=0
EVENT_NOTIFICATIONS_ENABLED=0
```

`EMAIL_ENABLED=1` autorise le canal email. Laisser `SMS_ENABLED=0` : le groupe
bénévoles reçoit les messages via le partage manuel WhatsApp. Le formulaire
d'événement ne propose plus d'option SMS et désactive cet ancien réglage lors
de l'enregistrement. Les anciens historiques SMS sont conservés.
`EMAIL_BACKEND` vide laisse Django sélectionner automatiquement le backend
Brevo HTTPS ; un ancien backend SMTP explicite doit être retiré.
Le commutateur `EVENT_NOTIFICATIONS_ENABLED=1` autorise le traitement des campagnes
d’événements, sous réserve de leur activation et de leur échéance. Conserver ce
commutateur à `0` pendant la préparation. Les trois réglages d'événement
concernent uniquement les emails : annonce, rappel et autorisation email.

Les emails utilisent l'endpoint HTTPS `https://api.brevo.com/v3/smtp/email`.
La clé API est envoyée uniquement dans l’en-tête d’authentification du serveur.
Une acceptation par le prestataire ne prouve pas la livraison ni la lecture.
Les tentatives et résultats sont conservés ; un résultat incertain demande une
vérification avant une éventuelle nouvelle tentative.
Source : [API email Brevo](https://developers.brevo.com/reference/send-transac-email).

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

AMELONE ayant été créé après le 15 juillet 2026, le sujet OIDC attendu contient
les identifiants immuables du propriétaire et du dépôt :
`repo:wilfriedbarrer-stack@342539682/AMELONE@1419056471:ref:refs/heads/main`.
L'ancien format sans identifiants est refusé. Les diagnostics internes indiquent
seulement la catégorie d'erreur ou le nom du champ rejeté, sans jeton ni valeur.
Source : [sujets immuables GitHub OIDC](https://docs.github.com/en/actions/reference/security/oidc#immutable-subject-claims).

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
la validation de l'adresse expéditrice et la saisie de la clé API sur Render.
Renseigner la clé et l’adresse expéditrice dans Render, hors du chat et du dépôt,
puis activer les emails lorsqu'ils sont prêts. La configuration OIDC ne nécessite
pas de recopier une clé GitHub dans Render ni de transmettre la clé Brevo à
GitHub Actions.

**Render Cron n’est pas retenu et aucun service cron payant n’est créé.** Pour
comparaison, Render facture au minimum **1 USD par mois par service cron**, avec
facturation du calcul à la seconde et une seule exécution simultanée par tâche.
Source : [Render Cron Jobs](https://render.com/docs/cronjobs).
