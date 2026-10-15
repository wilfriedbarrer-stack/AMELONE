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
- [Charte graphique](docs/charte-graphique.md)
- [Déploiement](docs/deploiement.md)
- [Import depuis Google Sheets](docs/import-google-sheets.md)
- [Architecture et règles métier](docs/architecture.md)
- [Profils réutilisables, restauration, imports et récapitulatifs](docs/profils-repas-imports.md)

## Limites explicites de cette version

- SumUp est volontairement un simple lien externe : aucun paiement n'est confirmé
  automatiquement et le montant n'est pas transmis implicitement.
- Un SMTP configuré indique que le fournisseur a accepté le message, pas qu'il a été
  effectivement lu ou délivré dans la boîte du destinataire.
- L'ancien fichier `Code-complet.txt` n'ayant pas été fourni, l'import repose sur des
  exports CSV documentés et produit un rapport des données à compléter sans les inventer.


## Administration de l’association

Après connexion, le lien **Administration** ouvre `/administration/` pour les
organisateurs et les administrateurs. Chaque événement donne accès à la création
et à la modification des postes, de leurs créneaux, des menus et des boissons.
Les menus et boissons peuvent être désactivés sans supprimer les réservations.

Les administrateurs (comptes superutilisateurs Django) disposent aussi de :

- **Profils et droits** : identité, activation et rôles famille, organisateur ou
  administrateur ; rôle complémentaire **Officiel de compétition**, cumulable
  avec ces droits ; création de comptes via l’administration avancée ;
- **Logo de l’association** : téléversement PNG, JPEG ou WebP jusqu’à 1 Mo.
  Le fichier est validé et conservé en base, sans dépendre du disque temporaire
  de Render. Il apparaît sur toutes les pages et dans l’accueil.

Un organisateur peut gérer les événements, postes et repas, mais pas attribuer des
privilèges. Un administrateur ne peut ni désactiver son propre compte, ni retirer
ses propres droits, ni se supprimer. La gestion avancée des utilisateurs et des
groupes est réservée aux administrateurs afin d’empêcher une élévation de droits.
L’application des changements de rôle supprime les anciennes permissions
individuelles et les anciens groupes du compte concerné.

Déploiement : installer les dépendances mises à jour, appliquer la migration
`0002_sitebranding` et exécuter `collectstatic`. Le script Render effectue ces
étapes automatiquement. Pour le premier administrateur, utiliser
`createsuperuser` ou le compte initial créé par `bootstrap_admin`.


## Profils, restauration et imports

Dans Administration, enregistrez un profil réutilisable depuis un événement puis
créez un nouveau brouillon à une autre date. La configuration des postes, menus et
boissons est reprise sans copier les participants ni les paiements.

Les présences et la restauration se remplissent ensemble : repas complet à la
journée, collation à la demi-journée, boisson et préférence de paiement au comptoir
ou via SumUp. Le lien commun se configure dans le règlement de l’association.
Les postes acceptent désormais la période Journée.

L’import CSV/Excel affiche un aperçu à valider. Les récapitulatifs des postes et
de la restauration sont distincts, imprimables et exportables en CSV. Voir le
[guide et les colonnes des fichiers](docs/profils-repas-imports.md).

## Participation personnelle des administrateurs et officiels

Dans **Profils et droits**, cochez **Officiel de compétition** sur le compte
souhaité. Cette option conserve les droits famille, organisateur ou administrateur.
Un officiel seul n’obtient aucun droit de gestion ni de confirmation d’affectation.

Le lien **Ma participation** ouvre `/participation/`. Complétez votre identité
une seule fois, ou rattachez un membre existant de votre propre famille. Ce profil
personnel peut ensuite être réutilisé pour tous les événements.

Un administrateur choisit sa présence, ses repas et les créneaux de ses postes.
Les affectations sont confirmées directement sous réserve des capacités et des
périodes compatibles. Décocher un poste conserve son historique d’annulation ;
enregistrer plusieurs fois le même choix ne crée pas de doublons. L’inscription,
la restauration et les affectations sont enregistrées ensemble : en cas d’erreur,
aucune modification partielle n’est conservée.

Un officiel sans droits administrateur indique son poste souhaité ; l’organisation
confirme ensuite l’affectation. Les échéances habituelles restent applicables.
La migration `0005_competition_official_personal_member` est appliquée par le script
Render lors de la publication.

## Habilitations individuelles et règles d’âge des postes

**Administration → Familles, membres et officiels** affiche les personnes de
chaque famille, avec leur identité et leur âge dans cet espace privé.
L’administrateur attribue la fonction **Officiel de compétition** à chaque membre.
Seule cette habilitation individuelle permet de choisir un poste officiel,
y compris lorsque la personne est elle-même administratrice. Le profil d’accès
du compte responsable ne qualifie pas automatiquement les autres membres.
Les postes officiels portent la mention **Licence et formation à jour nécessaires.**

La fiche d’un poste propose trois règles : aucune condition d’âge, au moins un
adulte confirmé par créneau, ou uniquement des adultes. L’âge retenu est celui
à la date de l’événement ; une personne est adulte dès ses 18 ans révolus.
Avec la règle d’encadrement, les affectations mineures peuvent être préparées
en brouillon, puis confirmées avec un adulte sur le même créneau. Si le dernier
adulte se retire, les confirmations des mineurs repassent en brouillon pour être
réorganisées. L’historique est conservé. Les règles sont reprises lors de la
duplication d’un événement et de la création depuis un profil réutilisable.

Le **Planning public** est mis en évidence sur l’accueil et dans la navigation,
avec ou sans connexion. Il affiche les noms confirmés, les postes, horaires et
capacités, sans âge individuel, date de naissance, coordonnées, repas ni remarque.

La migration `0006_member_official_station_age_policy` ajoute ces règles. Elle
conserve les habilitations de comptes déjà reliés à leur participant personnel.
Les anciennes confirmations officielles futures sans habilitation individuelle
repassent en brouillon ; l’administrateur doit habiliter le membre avant de les
confirmer à nouveau.

## Repas et boissons sans supplément pour les officiels

Un participant habilité **Officiel de compétition** ne paie aucun supplément pour
son repas ou sa collation et sa boisson. Cette gratuité suit son habilitation
individuelle ; les autres membres de sa famille gardent leurs tarifs habituels.
Les formulaires, récapitulatifs et soldes affichent les montants réellement dus.

Attribuer ou retirer la fonction recalcule les réservations actives d’aujourd’hui
et à venir. Les tarifs initialement réservés sont conservés pour être réappliqués
en cas de retrait, même si le catalogue a changé entre-temps. Les réservations
passées et les paiements enregistrés sont conservés. Un éventuel trop-perçu reste
visible dans le solde de la famille.
