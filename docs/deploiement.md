# Déploiement

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

