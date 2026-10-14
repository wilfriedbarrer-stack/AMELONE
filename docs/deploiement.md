# Déploiement

## Render en un clic

Le fichier `render.yaml` crée un service web et une base PostgreSQL. Pendant la création
du Blueprint, renseigner `INITIAL_ADMIN_EMAIL` et `INITIAL_ADMIN_PASSWORD`. Le mot de
passe doit être long et unique ; il n'est jamais écrit dans les journaux. La commande de
préparation crée ce compte une seule fois et refuse de promouvoir silencieusement un
compte familial existant.

Après la première connexion, modifier le mot de passe puis supprimer les deux variables
`INITIAL_ADMIN_*` dans Render. Les offres gratuites de Render peuvent être mises en
veille ou limitées dans le temps ; vérifier le récapitulatif tarifaire affiché avant de
confirmer la création.

## Variables obligatoires

- `DJANGO_SECRET_KEY` : valeur aléatoire longue ;
- `DATABASE_URL` : PostgreSQL géré avec sauvegardes ;
- `ALLOWED_HOSTS` et `CSRF_TRUSTED_ORIGINS` ;
- `SITE_URL` : URL HTTPS publique ;
- paramètres SMTP décrits dans `emails.md` si les emails sont activés.

Activer en production :

```dotenv
DJANGO_DEBUG=0
SESSION_COOKIE_SECURE=1
CSRF_COOKIE_SECURE=1
```

## Publication

L'image `Dockerfile` exécute Gunicorn. Avant chaque mise en production :

```bash
python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py check --deploy
```

Créer le premier organisateur avec `python manage.py createsuperuser`, puis supprimer
l'accès au shell d'administration conformément aux règles de l'hébergeur. Ne jamais
précharger un mot de passe de production.

Sauvegarder PostgreSQL quotidiennement et tester la restauration. Un reverse proxy doit
terminer TLS, transmettre `X-Forwarded-Proto` et limiter la taille/fréquence des requêtes.

