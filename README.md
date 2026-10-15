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
- [Modèles d’événements, restauration, imports et récapitulatifs](docs/profils-repas-imports.md)

## Limites explicites de cette version

- SumUp est volontairement un simple lien externe : aucun paiement n'est confirmé
  automatiquement et le montant n'est pas transmis implicitement.
- Un SMTP configuré indique que le fournisseur a accepté le message, pas qu'il a été
  effectivement lu ou délivré dans la boîte du destinataire.
- L'ancien fichier `Code-complet.txt` n'ayant pas été fourni, l'import repose sur des
  exports CSV documentés et produit un rapport des données à compléter sans les inventer.


## Parcours de gestion

Après connexion, **Accueil gestion** ouvre `/organisateur/` : événements à gérer,
indicateurs et accès au **Suivi annuel**. La navigation sépare **Événements**,
**Familles**, **Catalogue** et **Réglages** selon les droits du compte.
Le **Planning public** reste directement accessible, avec ou sans connexion.

Chaque événement se gère dans quatre espaces : **Participants**, **Postes**,
**Restauration** et **Paiements**. **Autres actions** regroupe la modification des
informations, les messages aux participants, la duplication, l’enregistrement
comme modèle et la suppression ou l’archivage. Le lien vers son planning public
apparaît lorsque l’événement est publié.

Dans **Réglages** (`/administration/`), les administrateurs configurent le logo,
le règlement de l’association et les comptes. Le logo PNG, JPEG ou WebP, limité
à 1 Mo, est validé et conservé en base ; il apparaît aussi sur les tickets.
L’administration avancée reste accessible aux administrateurs.

## Modèles et événements

Ouvrez **Événements → Modèles d’événements** pour préparer un modèle indépendant,
puis ses postes et créneaux : matin, après-midi ou journée, capacités, horaires,
catégorie et règle d’âge. La note interne du modèle se distingue de la
présentation publique proposée aux événements. Les journées uniques sont
renseignées automatiquement ; les choix de journée restent visibles pour un
modèle à plusieurs jours.

**Créer un événement** utilise un seul formulaire : choisissez un modèle ou
**Événement vierge**, puis renseignez le nom et la date. Depuis un modèle, les
postes sont copiés dans un brouillon indépendant et les échéances sont décalées.
Vérifiez les informations et échéances avant publication. Les participants,
affectations, commandes et paiements ne sont jamais copiés. Modifier ou supprimer
un modèle ne change pas les événements déjà créés.

Les informations, échéances, réglages de restauration et publication sont
regroupés dans l’éditeur. La date d’un événement ayant des inscriptions est
verrouillée. Les dates multiples des événements anciens sont conservées.
**Supprimer ou archiver** demande une confirmation : un événement sans historique
peut être supprimé ; un événement avec historique est retiré de la liste active
et reste consultable dans **Archives**.

## Familles, membres et droits

La fiche **Familles** réunit **Responsable**, **Membres** et **Droits du compte**.
Les coordonnées du responsable se modifient depuis cette fiche, au même endroit.
Pour un compte sans famille associée, elles restent modifiables dans
**Réglages → Comptes et droits**.

L’administrateur corrige l’identité, la date de naissance et la fonction
**Officiel de compétition** depuis la fiche de chaque **membre**, y compris
archivé. Cette fonction se cumule avec les droits du compte et ne donne aucun
droit de gestion. Elle ne s’attribue plus sur le formulaire des droits du compte.
Le responsable ne qualifie pas automatiquement les autres membres de sa famille.
Les postes officiels portent la mention **Licence et formation à jour nécessaires**.

**Comptes et droits** gère l’activation et les accès famille, organisateur ou
administrateur. Un organisateur gère les événements, postes et repas sans
attribuer de privilèges. Un administrateur ne peut pas désactiver son compte ni
retirer ses propres droits ; au moins un administrateur actif doit être conservé.
Les anciennes permissions individuelles et anciens groupes sont retirés lors
d’un changement de rôle. Les liens entre compte, famille et membre sont conservés.

## Participation personnelle

**Mon compte → Ma participation** ouvre `/participation/` pour les comptes
concernés. Complétez votre identité une fois ou rattachez un membre existant de
votre propre famille. Un administrateur peut aussi utiliser **M’inscrire à cet
événement** depuis la fiche événement.

L’administrateur choisit sa présence, sa restauration et ses créneaux. Les
confirmations restent soumises aux capacités, habilitations, règles d’âge,
périodes et horaires compatibles. Décocher un poste conserve l’historique de son
annulation. Présence, repas et affectations sont enregistrés ensemble, sans
modification partielle en cas d’erreur.
Un officiel sans droits administrateur propose un poste que l’organisation
confirme ensuite ; les échéances habituelles restent applicables.

## Protection du planning

Un poste peut n’avoir aucune condition d’âge, exiger au moins un adulte confirmé
par créneau, ou être réservé aux adultes. L’âge est calculé à la date de
l’événement, avec 18 ans révolus pour un adulte. Pour un poste encadré, les
mineurs peuvent être préparés en brouillon puis confirmés avec un adulte.
Si le dernier adulte se retire, leurs confirmations repassent en brouillon pour
être réorganisées, avec conservation de l’historique.

Pour un même membre, les chevauchements d’horaires sont bloqués lors des
affectations et des modifications d’horaires, y compris entre deux événements
à la même date. Ce contrôle ne déplace ni ne supprime automatiquement les
affectations existantes : l’organisation corrige explicitement les créneaux
concernés.
Les événements archivés sont exclus du contrôle. Leur réactivation conserve
l’historique et signale les conflits éventuels ; les confirmations restent
bloquées jusqu’à leur correction dans **Postes → Gérer les affectations**.

Le **Planning public** affiche les affectations confirmées, noms, postes,
horaires et capacités. Il n’affiche ni âge individuel, date de naissance,
coordonnées, repas ou remarque. **Tous les postes** et **Uniquement les postes de
ma famille** permettent de filtrer les créneaux ; la vue familiale demande une
connexion et conserve les coéquipiers et capacités visibles.

## Catalogue et restauration

Dans **Catalogue**, créez, importez, modifiez ou désactivez les repas, collations
et boissons communs. Les choix actifs sont proposés automatiquement sur tous les
événements. La recherche et les filtres d’état et de température permettent de
retrouver une option. Les anciennes exceptions locales restent accessibles dans
un volet dédié et peuvent devenir communes avec **Rendre commun**.
Les réservations existantes et leurs tarifs enregistrés sont conservés.

La présence et la restauration se remplissent ensemble, par moment : **Matin**,
**Midi** et **Après-midi** pour une journée complète, ou seulement la collation
de la demi-journée choisie. Une journée peut comprendre six composants distincts :
repas, boisson du repas, collation et boisson chaude du matin,
collation et boisson fraîche de l’après-midi. Les collations et boissons restent
facultatives ; une boisson de collation accompagne la collation correspondante.
Les deux offres se désactivent séparément dans chaque événement. Le participant peut aussi choisir sans boisson ou sans repas.

La restauration est sans supplément pour un membre habilité officiel lorsqu’il
n’est pas inscrit à un poste bénévole ce jour-là. Toute affectation bénévole
active, même en brouillon ou cumulée avec un poste officiel, applique le tarif
bénévole à la journée. Sans affectation active, le poste souhaité détermine la
règle. Les six composants suivent cette règle avec leurs tarifs réservés.
Les réservations passées, annulées ou archivées et les paiements reçus restent
conservés. Les autres membres de la famille gardent leurs tarifs habituels.

L’espace **Restauration** de l’événement réunit les choix des **Participants**,
les **Quantités à préparer** par moment et les **Tickets · 6 par A4**. Les
récapitulatifs des postes et des repas restent distincts, imprimables et
exportables en CSV. Le [guide détaillé](docs/profils-repas-imports.md) décrit les
imports et les règles de conservation des réservations.

## Paiements et tickets

**Réglages → Règlement** configure le lien SumUp commun. Le choix **Au comptoir**
ou **Via lien SumUp** est une préférence, pas un paiement reçu.
L’espace **Paiements** affiche le dû, le déjà réglé, le reste et les trop-perçus
par famille pour tout l’événement. **Encaisser** préremplit la famille et le
reste à régler ; le montant reste modifiable pour un paiement partiel.
Seul un règlement effectivement reçu doit être enregistré.

Les tickets nominatifs reprennent le logo et tous les choix, à raison de six
emplacements par feuille A4. **Note famille soldée** ou **Note famille non
soldée** utilise le solde de toute la famille pour l’événement. Le montant restant
est indiqué uniquement si la note n’est pas soldée et ne doit pas être additionné
entre les tickets. La consultation et l’impression ne modifient aucun paiement.

Le script Render applique les migrations et exécute `collectstatic` lors de la
publication. Le premier administrateur se crée avec `createsuperuser` ou le
compte initial prévu par `bootstrap_admin`.

## Annonces et rappels automatiques

Dans un événement, **Autres actions → Annonces et rappels** présente les aperçus
email/SMS, les familles concernées et l’historique. Les réglages se trouvent dans
**Modifier les informations → Annonces et rappels**.

L’annonce de première publication concerne toutes les familles actives, une seule
fois par événement. Le rappel intervient 48 h avant la clôture s’il reste des
places ; seules les familles sans inscription active à cet événement sont
concernées. Une famille reçoit au plus un message de chaque type par canal.
Les anciens événements ne sont pas réannoncés lors de la mise à jour.

Le workflow GitHub vérifie la file toutes les quinze minutes, avec retards
possibles. Il utilise l’identité temporaire GitHub de ce dépôt et de ce workflow,
sans mot de passe de base de données ni secret partagé dans GitHub Actions.
Les envois restent désactivés tant que le compte d’envoi et les expéditeurs ne
sont pas configurés. Brevo est proposé : emails gratuits dans la limite de son
offre, SMS sur crédits payants. Voir la [configuration et les offres](docs/emails.md).

Une acceptation par le prestataire ne prouve pas la livraison. Les échecs certains
peuvent être remis en attente depuis l’interface ; les résultats incertains
nécessitent une vérification chez le prestataire pour éviter les doublons.
