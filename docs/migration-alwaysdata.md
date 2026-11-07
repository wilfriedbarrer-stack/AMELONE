# Préparer puis transférer AMELONE vers Alwaysdata

Ce guide accompagne la branche `migration/alwaysdata`. Il prépare une migration ; il
ne constitue pas la preuve d'un déploiement réussi. L'utilisateur a créé le compte
et fourni l'adresse **https://amelone.alwaysdata.net**. Aucun accès SSH/API n'est
encore configuré ici ; aucune base cible ni tâche planifiée n'a été créée pendant
cette préparation. Aucune donnée de production n'a été copiée. La base Render a une échéance constatée
au **13 novembre 2026** lors du contrôle du 6 novembre : effectuer la sauvegarde et
la répétition avant cette date, puis revérifier l'échéance dans Render.

Informations communiquées par l'utilisateur le **7 novembre 2026** : base cible
`amelone_prod`, utilisateur PostgreSQL `amelone`. La connexion, les droits et la
version du serveur cible ne sont pas encore vérifiés. Le nom de l'utilisateur
PostgreSQL ne confirme pas celui du compte SSH.

La capture fournie ensuite confirme un compte SSH **`amelone`**, un répertoire
HOME **`/home/amelone/`** et une authentification par mot de passe active. Une clé
Ed25519 dédiée à cette migration a été préparée dans un répertoire privé hors du
dépôt ; la capture suivante confirme l'ajout de sa partie publique dans Alwaysdata.
Elle n'a pas encore été utilisée pour se connecter. Empreinte publique :
`SHA256:+MNBVCz94JPTi/cuXbVxkz0+yQ7GuryUEF7O1IBkpN4`.

## 1. Choisir le compte et le coût avant la bascule

Les [tarifs officiels Alwaysdata](https://help.alwaysdata.com/en/docs/admin-billing/billing/public-cloud-prices/)
annoncent les montants hors taxes :

| Offre | Ressources | Paiement | Sauvegardes quotidiennes |
| --- | --- | --- | --- |
| Free | 1 Go disque, 256 Mo RAM, ¼ CPU | Gratuit | 3 jours |
| Plus Small | 50 Go disque, 1 Go RAM, 1 CPU | 5 € HT/mois si paiement annuel ; 6 € HT/mois si mensuel | 7 jours |

Avec une TVA de 20 %, Small revient à **72 € TTC payés pour l'année**, soit 6 € TTC
par mois en moyenne, ou **7,20 € TTC par mois** avec paiement mensuel. Ce n'est donc
pas une offre à 5 € TTC/mois. Vérifier le montant final dans le formulaire avant de
souscrire ; aucun abonnement payant n'est autorisé par ce guide.

Free propose désormais **1 Go**, et non les anciens 100 Mo. Il limite le site à
`amelone.alwaysdata.net`, exclut les usages lucratifs et les services permanents.
Le [site commercial](https://www.alwaysdata.com/fr/offers/) parle aussi de besoins
personnels : confirmer l'admissibilité de l'usage associatif auprès d'Alwaysdata
avant de choisir Free pour AMELONE. Si admissible, ce compte peut servir à la
préproduction, sous réserve que les dépendances et la base tiennent dans ses quotas.
Ne pas considérer les 256 Mo de RAM comme suffisants avant un essai réel.

Le compte est créé : confirmer dans le panneau le **nom de compte** et l'offre
retenue. Les exemples prennent `amelone` et `/home/amelone` ; confirmer ce chemin
dans le panneau avant de les exécuter. L'adresse de préproduction est
`https://amelone.alwaysdata.net`. Ces informations ne sont pas des secrets.
Saisir les mots de passe, clés Brevo et URL de base uniquement dans les écrans ou
fichiers privés prévus ; ne pas les envoyer au chat et ne pas les committer.

## 2. Préparer le code et les accès

Le déploiement de préparation doit utiliser `migration/alwaysdata`, ou le commit
validé de cette branche. Utiliser `main` seulement après sa fusion. Relever le SHA
du commit pour pouvoir comparer la répétition et la bascule finale.

Alwaysdata fournit [SSH et SFTP](https://help.alwaysdata.com/en/docs/web-hosting/remote-access/ssh/)
sur `ssh-amelone.alwaysdata.net`, sans accès root. Déposer le code dans
`/home/amelone/www/amelone` par SSH/SFTP ou cloner le dépôt avec un accès Git autorisé.
Ne pas copier un `.env`, un dump ou les fichiers de travail du poste dans le dépôt.
Adapter les chemins si le nom de compte confirmé dans le panneau diffère.

Dans **Accès distant > SSH/SFTP**, relever le compte SSH et vérifier son activation.
Pour ajouter la clé publique préparée, utiliser l'icône **Clés SSH** à côté du
compte `amelone`, puis **Ajouter une clé SSH**, selon la
[procédure officielle](https://help.alwaysdata.com/en/docs/web-hosting/remote-access/ssh/use-keys/).
Conserver les clés déjà présentes et retirer cette clé de migration lorsque son
accès n'est plus nécessaire.
L'utilisateur peut aussi ouvrir le [terminal web](https://ssh-amelone.alwaysdata.net)
et s'y authentifier lui-même, sans transmettre son mot de passe au chat. Pour une
connexion directe depuis l'environnement Codex actuel, l'authentification SSH ne
suffit pas : les services TCP nécessitent un chemin VPN/Tailscale IPv4, absent au
contrôle du 7 novembre. Ajouter le domaine à une liste d'accès HTTP(S) ne rend pas
le serveur SSH public joignable. L'indication antérieure d'une simple autorisation
TCP 22 dans les paramètres était incomplète et est corrigée ici. Voir la
[documentation Codex Cloud](https://learn.chatgpt.com/docs/environments/cloud-environments).
Le chemin retenu en attendant un accès adapté est donc le terminal de l'utilisateur,
ou le terminal web Alwaysdata ; aucune connexion réussie ni copie de données
n'est encore établie.

Dans **Environnement > Python**, sélectionner aussi 3.12 pour les commandes SSH.
Depuis SSH, dans le répertoire du projet :

```bash
mkdir -p /home/amelone/private /home/amelone/venv
chmod 700 /home/amelone/private
PYTHON_VERSION=3.12 python -m venv /home/amelone/venv/amelone
git branch --show-current
git rev-parse HEAD
```

Si le code a été transféré par SFTP, conserver le SHA de son archive au lieu des
deux commandes Git. L'[environnement Python officiel](https://help.alwaysdata.com/en/docs/web-hosting/languages/python/configuration/)
propose 3.12 et demande d'utiliser le wrapper `python` avec `PYTHON_VERSION` avant
la création du venv. Le site et le venv doivent utiliser la même version.

## 3. Créer la base cible vide et vérifier PostgreSQL

Dans **Bases de données > PostgreSQL**, créer une base réservée à cette migration
et son utilisateur. Les [versions annoncées](https://help.alwaysdata.com/en/docs/web-hosting/databases/postgresql/)
incluent PostgreSQL **18** : retenir cette version pour la source Render en 18.6.
Si le nouveau compte utilise encore 16 ou 17, passer par la migration PostgreSQL 18
du panneau avant l'import, ou demander l'aide du support. Ne pas tenter une
restauration de 18 vers 17 comme procédure habituelle.

L'hôte habituel est `postgresql-amelone.alwaysdata.net`, port 5432 ; relever les
valeurs exactes dans le panneau. Vérifier avec `SHOW server_version;` sur la cible
et avec `pg_dump --version` sur la machine qui exporte : utiliser un client
`pg_dump` de version majeure 18. Le numéro mineur cible et le mode TLS ne sont pas
confirmés par la seule documentation publique. Vérifier la connexion réelle et son
chiffrement avec `psql` puis `\conninfo` ; choisir le `sslmode` de l'URL selon cette
vérification et les indications du support. Ne pas reprendre aveuglément les
paramètres Render.

Pour les opérations de sauvegarde, préparer localement deux fichiers privés :
`/home/amelone/private/pg_service.conf` et `/home/amelone/private/pgpass`, tous deux en
mode 600. Utiliser des sections nommées `amelone_render` et `amelone_alwaysdata`
dans le premier, avec hôte, port, base, utilisateur et TLS adaptés ; mettre les
mots de passe dans le second, au format PostgreSQL. Ne pas mettre un mot de passe
dans une commande, une URL passée en argument ou l'historique du terminal.

Les commandes suivantes supposent que ces services privés ont été configurés :

```bash
export PGSERVICEFILE=/home/amelone/private/pg_service.conf
export PGPASSFILE=/home/amelone/private/pgpass
pg_dump --version
psql 'service=amelone_alwaysdata' -X -c 'SHOW server_version;'
psql 'service=amelone_alwaysdata' -X -c '\conninfo'
psql 'service=amelone_alwaysdata' -X -c "SELECT count(*) AS tables_publiques FROM pg_tables WHERE schemaname = 'public';"
```

Le dernier résultat doit être zéro avant le premier import. Confirmer les
extensions et la collation nécessaires au dump. La cible est une base de migration
vide ; elle ne doit contenir ni compte administrateur créé à la main, ni données
de démonstration.

## 4. Préparer l'environnement privé et le site WSGI

Le générateur fournit une nouvelle clé Django et demande l'URL PostgreSQL cible
avec une saisie masquée :

```bash
PYTHON_VERSION=3.12 python deploy/alwaysdata/init_env.py --host amelone.alwaysdata.net --output /home/amelone/private/amelone.env
```

Le fichier généré utilise le format POSIX `export NOM='valeur'` et doit rester en
mode 600, hors du dépôt et hors des répertoires servis sur le Web. Le modèle
`deploy/alwaysdata/environment.example` montre les variables attendues. Il contient
des valeurs fictives et ne doit pas être déployé tel quel.

Dans **Web > Sites > Ajouter un site**, choisir :

| Champ | Valeur |
| --- | --- |
| Adresse | `amelone.alwaysdata.net` |
| Type | Python WSGI |
| Chemin de l'application | `/home/amelone/www/amelone/deploy/alwaysdata/amelone.wsgi.py` |
| Répertoire de travail | `/home/amelone/www/amelone` |
| Version de Python | 3.12 |
| Répertoire du virtualenv | `/home/amelone/venv/amelone` |

Recopier les mêmes couples nom/valeur du fichier privé dans le champ **Variables
d'environnement du site**, sans `export` et sans les quotes de syntaxe shell. Le
processus WSGI ne lit pas le fichier privé. Un `export` effectué dans une session
SSH ne se propage pas au site. Les [sites Python](https://help.alwaysdata.com/en/docs/web-hosting/languages/python/configuration/)
utilisent uWSGI géré par Alwaysdata ; cette configuration ne lance pas Gunicorn.

Les commandes Django doivent sélectionner `amelone.settings_alwaysdata` via
`DJANGO_SETTINGS_MODULE`, comme dans le fichier généré. `ALWAYSDATA_SITE_HOST`
contient le nom exact, sans `https://` ni port. Le module en déduit `SITE_URL`, les
hôtes autorisés et l'origine CSRF. Il impose HTTPS et les cookies sécurisés ; HSTS
vaut 3600 secondes par défaut via `ALWAYSDATA_HSTS_SECONDS`, sans inclure les
sous-domaines. Ne pas recopier les variables d'hôte Render.

Pour toute la répétition, conserver :

```bash
export ALWAYSDATA_ENABLE_EMAILS='0'
export EMAIL_ENABLED='0'
export EVENT_NOTIFICATIONS_ENABLED='0'
export SMS_ENABLED='0'
```

Le module neutralise aussi les SMS et les envois tant que les indicateurs requis
ne sont pas explicitement activés. La nouvelle clé Django invalide les anciennes
sessions et les liens signés en cours ; les comptes et mots de passe hachés restent
dans la base restaurée. Prévoir une reconnexion après bascule.

Installer et vérifier la configuration :

```bash
bash deploy/alwaysdata/prepare.sh --env-file /home/amelone/private/amelone.env --venv /home/amelone/venv/amelone
```

Ce script installe les dépendances, exécute `collectstatic` puis `check --deploy`.
Il ne lance **ni migration de base, ni bootstrap, ni création de superutilisateur**.
WhiteNoise sert les fichiers collectés avec la configuration existante. Si des
médias sur disque sont ajoutés ultérieurement, organiser leur transfert et leur
service séparément ; Alwaysdata propose aussi un [type Fichiers statiques](https://help.alwaysdata.com/en/docs/web-hosting/sites/http-servers/static-files/).
Le logo AMELONE actuel est conservé en base.

## 5. Répéter l'export et la restauration

Définir un emplacement privé neuf pour le dump. L'export lit la source ; il ne
suspend pas les inscriptions et ne constitue donc pas encore l'export final.

```bash
umask 077
pg_dump --dbname='service=amelone_render' --format=custom --no-owner --no-privileges --file=/home/amelone/private/amelone-repetition.dump
sha256sum /home/amelone/private/amelone-repetition.dump
pg_restore --list /home/amelone/private/amelone-repetition.dump > /home/amelone/private/amelone-repetition.manifest
pg_restore --dbname='service=amelone_alwaysdata' --no-owner --no-privileges --exit-on-error --single-transaction /home/amelone/private/amelone-repetition.dump
```

Restaurer **uniquement dans la cible vide identifiée**, jamais dans la source. Ne
pas utiliser `--clean` et ne pas rejouer un dump sur une cible déjà remplie. Pour
une nouvelle répétition, créer une autre base vide et changer les connexions cible
dans le fichier privé et le panneau. Conserver les dumps hors Git et hors Web ;
ne publier ni contenu, ni identifiant de personne, ni adresse email.

Comparer le SHA-256 du fichier avant et après transfert s'il change de machine.
Comparer les schémas, les migrations enregistrées et les comptes exacts de lignes
des tables métier dans la source et la cible. Conserver un relevé de volumes
agrégés, sans données nominatives. Pour des volumes comparables, utiliser le même
instantané ou la même fenêtre sans écriture ; une source encore active peut
légitimement avoir évolué depuis le dump.

Charger ensuite l'environnement dans le shell et vérifier le schéma restauré :

```bash
. /home/amelone/private/amelone.env
/home/amelone/venv/amelone/bin/python manage.py migrate --check
/home/amelone/venv/amelone/bin/python manage.py check --deploy
/home/amelone/venv/amelone/bin/python manage.py process_event_notifications --dry-run --limit 100
```

`migrate --check` vérifie les migrations en attente sans les appliquer. Si des
migrations manquent, arrêter et comparer le commit déployé à celui de la source ;
planifier leur application après validation de leur effet. Ne pas lancer le
bootstrap Render ou `createsuperuser` : le dump contient déjà les comptes, les
permissions, les inscriptions et l'historique des notifications.

## 6. Valider la préproduction sans envois

Redémarrer le site depuis le panneau après tout changement de ses variables.
Vérifier l'accès HTTPS, la connexion organisateur, les événements, familles,
inscriptions, affectations, repas, paiements, logo et accès aux ressources statiques.
Vérifier les droits avec des comptes de test autorisés, puis les parcours sans
effectuer de vraies réservations ou paiements. Les essais modifient au besoin la
copie de répétition ; elle sera remplacée par une nouvelle base pour le dump final.
Les emails et SMS restent désactivés.

Les certificats de l'adresse `alwaysdata.net` sont pris en charge. Pour un domaine
personnalisé, Alwaysdata génère et renouvelle le certificat après ajout de l'adresse
et pointage DNS : [SSL automatique](https://help.alwaysdata.com/en/docs/web-hosting/sites/ssl-tls/lets-encrypt/).
Le proxy transmet [X-Forwarded-Proto](https://help.alwaysdata.com/fr/docs/hebergement-web/sites/stack-http/),
pris en compte par la configuration Django.

Consulter les journaux dans le panneau ou dans
`/home/amelone/admin/logs/uwsgi/ID_DU_SITE.log` et `admin/logs/http/`.
Ne pas partager des logs susceptibles de contenir des données personnelles.
Vérifier que mémoire, disque et durée des requêtes tiennent dans l'offre retenue.

## 7. Préparer la courte fenêtre de bascule

Avant toute coupure, disposer d'un export restauré avec succès, de son empreinte,
des contrôles de volumes et d'une personne capable de rétablir la source.
La procédure nécessite de pouvoir **empêcher les écritures sur Render** pendant
la fenêtre : maintenance ou suspension effective. Ce kit n'implémente pas cette
maintenance ; la préparer et la vérifier avant de commencer.

L'URL `amelone.onrender.com` appartient à Render et ne se transfère pas. Choisir
la nouvelle adresse publique, recenser les liens et QR codes déjà diffusés,
messages et favoris, puis organiser leur remplacement ou une redirection depuis
la source conservée. Un domaine personnalisé nécessite une offre adaptée.

Choisir une seule autorité d'envoi :

1. Désactiver les campagnes automatiques et les envois sur la **source Render**.
2. Désactiver le workflow GitHub **Notifications des événements**, fichier
   `.github/workflows/event-notifications.yml`, via GitHub Actions. Il appelle
   actuellement l'URL Render toutes les 15 minutes. Vérifier qu'aucune exécution
   n'est encore en cours.
3. Ne pas créer ni activer de tâche de traitement sur Alwaysdata avant ces arrêts.

Conserver la source silencieuse même si le service reste joignable. Le verrouillage
en base n'empêche pas les doublons entre deux bases distinctes.

## 8. Export final, contrôles et ouverture

Pendant la fenêtre convenue, après arrêt des envois source :

1. Bloquer les écritures sur Render et vérifier ce blocage.
2. Faire un **nouveau dump final** PostgreSQL 18, avec les mêmes options que la
   répétition et un nom neuf ; relever son empreinte et les volumes métier.
3. Le restaurer dans une **nouvelle base cible vide**, avec les mêmes options
   `pg_restore`, sans `--clean`. Mettre à jour `DATABASE_URL` dans le fichier privé
   et dans les variables du site. Ne pas réutiliser les données des essais.
4. Déployer le commit validé, vérifier les migrations avec `migrate --check`, les
   volumes, les droits, les statiques et le fonctionnement HTTPS. Garder les trois
   indicateurs d'envoi à 0.
5. Si l'adresse finale change, mettre à jour `ALWAYSDATA_SITE_HOST` dans le fichier
   privé **et** dans le panneau, puis l'adresse du site et les DNS si applicables.
   `SITE_URL` suit cet hôte ; vérifier les nouveaux liens d'inscription, de compte
   et d'email. Redémarrer le site et vérifier son certificat avant de l'annoncer.
6. Décider de l'ouverture avec les contrôles de restauration réussis, puis ouvrir
   les écritures uniquement sur Alwaysdata.

La [procédure officielle de transfert](https://help.alwaysdata.com/en/docs/web-hosting/sites/transfer-in/)
recommande également de configurer une adresse de préproduction et de tester avant
de déplacer les adresses publiques.

## 9. Activer Brevo et les tâches une seule fois

Après validation de la bascule et arrêt confirmé des traitements source/GitHub,
renseigner la clé Brevo et l'expéditeur validé hors chat. Dans le fichier privé
**et** dans le panneau du site, activer les trois indicateurs :

```bash
export ALWAYSDATA_ENABLE_EMAILS='1'
export EMAIL_ENABLED='1'
export EVENT_NOTIFICATIONS_ENABLED='1'
export EMAIL_PROVIDER='brevo'
export SMS_ENABLED='0'
```

Redémarrer le site. Vérifier les volumes en attente avec `--dry-run` et contrôler
les campagnes importées avant le premier traitement réel. Ne pas effacer leurs
états ni réinitialiser l'historique pour forcer un envoi. Effectuer un essai
fonctionnel autorisé avec une adresse contrôlée et vérifier les liens publics.

Créer ensuite, dans **Avancé > Tâches planifiées**, les tâches proposées ci-dessous.
Elles ne sont pas créées par le kit. Le champ d'environnement d'une tâche est
distinct de celui du site : ces commandes chargent explicitement le fichier privé,
qui doit exporter ses variables. Choisir le même utilisateur SSH et le répertoire
de travail `/home/amelone/www/amelone`.

Toutes les 15 minutes (`*/15 * * * *`) :

```bash
bash -c '. /home/amelone/private/amelone.env; exec /home/amelone/venv/amelone/bin/python /home/amelone/www/amelone/manage.py process_event_notifications --limit 100'
```

Tous les jours, par exemple à 03:17 (`17 3 * * *`) :

```bash
bash -c '. /home/amelone/private/amelone.env; exec /home/amelone/venv/amelone/bin/python /home/amelone/www/amelone/manage.py clearsessions'
```

Confirmer le fuseau horaire affiché dans le panneau avant de choisir l'horaire
quotidien. Les [tâches planifiées Alwaysdata](https://help.alwaysdata.com/en/docs/web-hosting/tasks/)
ignorent une occurrence si la précédente tourne encore ; leurs logs sont dans
`/home/amelone/admin/logs/jobs/`. Surveiller les premiers résultats, les erreurs
Brevo et l'épuisement des lots de 100 ; ajuster ensuite selon les volumes constatés.

## 10. Sauvegardes et retour arrière

Alwaysdata sauvegarde chaque jour fichiers, bases et emails, avec une rétention de
3 jours en Free ou 7 jours en Small ; ces sauvegardes sont hors quota et stockées
dans un autre datacenter. Vérifier leur présence dans
`/home/amelone/admin/backup`, tester une restauration dans une base séparée et garder
une copie privée indépendante du dump final. [Sauvegardes officielles](https://help.alwaysdata.com/en/docs/web-hosting/backups/)

Avant toute nouvelle écriture sur Alwaysdata, un retour sur la source gelée est
possible en rétablissant l'adresse publique, ses variables et le seul traitement
de notifications retenu, puis en rouvrant les écritures source. Réserver une
fenêtre réaliste pour les DNS et les liens déjà diffusés.

**Après une nouvelle écriture sur la cible, ne pas revenir simplement sur l'ancienne
base Render** : cela perdrait des inscriptions ou divergerait les états d'envoi.
Rebloquer les écritures et préparer une réconciliation ou une migration inverse
validée avant toute remise en service de Render.

Conserver Render, la base source, les sauvegardes et les anciens liens tant que
les contrôles et l'exploitation cible ne sont pas validés. La conservation reste
soumise à l'échéance et aux conditions Render : un dump indépendant est nécessaire
avant expiration. Ne supprimer aucune ressource source pour préparer le transfert.

## 11. Vérifications de la préparation du 6 novembre 2026

Le kit est testé localement ; ces résultats ne prouvent pas encore le bon
fonctionnement du compte Alwaysdata, de sa base ou de son certificat.

- 15 tests unitaires des settings de production, de l'entrée WSGI et du fichier
  privé : `python -m unittest volunteers.tests.test_alwaysdata_settings volunteers.tests.test_alwaysdata_environment_file -v` → `Ran 15 tests`, `OK`.
- 63 tests de régression sur un PostgreSQL **17.11 local** :
  `python manage.py test volunteers.tests.test_notification_job volunteers.tests.test_services volunteers.tests.test_views volunteers.tests.test_whatsapp_attention_views volunteers.tests.test_public_planning_privacy --noinput`
  → `Ran 63 tests`, `OK`, `System check identified no issues (0 silenced).`
  La version 18 du serveur Alwaysdata reste à vérifier avant l'import. Une
  première exécution SQLite avait rencontré son verrouillage de table lors du
  test concurrent ; la vérification PostgreSQL ci-dessus est celle retenue.
- Ruff sur les cinq nouveaux fichiers Python, `bash -n deploy/alwaysdata/prepare.sh`
  et `git diff --check` réussis.

Les dépendances figées incluent Django **5.2.17** et PyJWT **2.15.0**, avec les
empreintes dans `requirements.txt`. L'installation cible vérifie ces empreintes.
Cette préparation ne comprend pas une nouvelle sortie `pip-audit`, une
restauration réelle, une vérification de quota ou un test d'envoi Brevo sur
Alwaysdata. L'audit de sécurité commencé séparément n'est pas déclaré terminé.
