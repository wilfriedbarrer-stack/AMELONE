# AMELONE

Application web française pour gérer les familles de bénévoles, leurs disponibilités,
les affectations, les repas, les paiements de suppléments et le suivi annuel.

## Architecture

- Django 5.2, pages HTML rendues côté serveur et contrôles d'autorisation systématiques ;
- PostgreSQL 17, migrations versionnées et opérations sensibles transactionnelles ;
- identifiants UUID pour toutes les données métier ;
- authentification par email et récupération de mot de passe ;
- SMTP côté serveur, désactivé tant qu'il n'est pas explicitement configuré ;
- interface responsive sans dépendance à un CDN.

Les dates et échéances utilisent `Europe/Paris`. Les dates de naissance sont stockées
sans heure et les montants en centimes. Pour les personnes nées le 29 février,
l'anniversaire est considéré le 28 février pendant une année non bissextile.

## Lancement local

Prérequis : Python 3.12+, `uv`, Docker et Docker Compose.

```bash
cp .env.example .env
UV_CACHE_DIR=/workspace/.cache/uv uv sync --all-groups --frozen
docker compose up -d db
UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py migrate
UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py seed_demo
UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py runserver 0.0.0.0:8000
```

L'application répond sur <http://localhost:8000>. Les identifiants fictifs créés par
`seed_demo` sont imprimés par la commande et ne doivent jamais être utilisés en
production.

Pour créer le premier vrai compte organisateur, utiliser une saisie interactive :

```bash
UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py createsuperuser
```

Aucun mot de passe organisateur de production n'est fourni dans le dépôt.

## Vérifications

```bash
UV_CACHE_DIR=/workspace/.cache/uv uv run python manage.py check
UV_CACHE_DIR=/workspace/.cache/uv uv run ruff check .
UV_CACHE_DIR=/workspace/.cache/uv uv run coverage run manage.py test
UV_CACHE_DIR=/workspace/.cache/uv uv run coverage report
```

## Déploiement et intégrations

[![Déployer sur Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/wilfriedbarrer-stack/AMELONE)

Le déploiement Render crée automatiquement le service web, PostgreSQL et le premier
compte organisateur. Render demande l'adresse e-mail et le mot de passe initial au
moment de la création. Choisir un mot de passe long et unique, puis le modifier dans
l'application après la première connexion.

- [Configuration des emails](docs/emails.md)
- [Déploiement](docs/deploiement.md)
- [Import depuis Google Sheets](docs/import-google-sheets.md)
- [Architecture et règles métier](docs/architecture.md)

## Limites explicites de cette version

- SumUp est volontairement un simple lien externe : aucun paiement n'est confirmé
  automatiquement et le montant n'est pas transmis implicitement.
- Un SMTP configuré indique que le fournisseur a accepté le message, pas qu'il a été
  effectivement lu ou délivré dans la boîte du destinataire.
- L'ancien fichier `Code-complet.txt` n'ayant pas été fourni, l'import repose sur des
  exports CSV documentés et produit un rapport des données à compléter sans les inventer.

