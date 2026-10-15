# Architecture et règles métier

## Espaces et autorisations

L'espace public ne lit que les événements publiés et les affectations confirmées.
L'espace famille filtre chaque requête par la famille du compte connecté. L'espace
organisateur exige `is_organizer` ou `is_staff`. Ces contrôles sont appliqués dans les
vues et dans les services métier ; masquer un bouton n'est jamais considéré comme une
autorisation.

## Modèle relationnel

Une famille contient plusieurs membres conservés d'une année à l'autre. Un événement
contient des journées, des postes et des créneaux matin/après-midi. Une inscription
décrit une disponibilité et un souhait, tandis qu'une affectation distincte occupe un
créneau. Les repas conservent une copie du tarif choisi. Les paiements sont des écritures
manuelles, jamais déduites d'un clic SumUp.

Un événement utilisé est retiré par archivage pour conserver son historique.
Après confirmation, les événements sans activité et les profils réutilisables
peuvent être supprimés ; les copies déjà créées depuis un profil restent
indépendantes. Les identifiants UUID ne dépendent pas des noms : renommer un poste
ou modifier ses horaires ne détache aucune donnée.

## Concurrence

Les opérations d'affectation verrouillent les inscriptions, créneaux et affectations
concernés avec `SELECT ... FOR UPDATE` dans une transaction. Les capacités, doublons,
disponibilités et chevauchements horaires sont revérifiés au moment de l'écriture.

## Confidentialité

Le planning public utilise une requête dédiée et ne transmet jamais emails, téléphones,
dates de naissance, âges, remarques, repas ou paiements. Les exports organisateur sont
protégés. Les secrets restent exclusivement dans l'environnement du serveur.
Le filtre familial du planning déduit la famille du compte connecté actif et
retient les seuls créneaux avec une affectation confirmée d’une inscription active
de cette famille. Les noms publics et l’occupation totale de ces créneaux sont
conservés. La réponse ne peut pas être mise en cache partagé et aucun identifiant
de famille fourni dans l’URL ne permet de changer de famille.

## Anniversaires

L'âge est calculé à la date de chaque participation et n'est pas stocké. Un anniversaire
le 29 février est observé le 28 février lors d'une année non bissextile. Les alertes ne
sont visibles que par les organisateurs.

