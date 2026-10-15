# Profils d’événements, restauration et récapitulatifs

Dans **Administration**, chaque événement propose : **Enregistrer le profil
réutilisable**, **Importer des menus**, **Importer des boissons**, **Récapitulatif
des postes** et **Récapitulatif des repas**.

## Réutiliser un événement

Ouvrir **Administration → Profils d’événements → Créer un profil d’événement**.
Renseigner son nom, son lieu et ses consignes, puis ajouter les postes. Pour
chaque poste, définir sa catégorie, sa règle d’âge et ses créneaux : matin,
après-midi ou journée, capacité et horaires facultatifs. Le profil se prépare
sans créer d’événement daté. La liste permet de modifier ensuite ses informations,
ses postes et ses créneaux.

Dans **Créer un événement**, choisir le profil, saisir le nom et la date, puis
cliquer **Créer depuis ce profil**. Les postes et leurs réglages sont copiés dans
un nouveau brouillon. Modifier ensuite le modèle n’altère pas cet événement.

Il reste possible d’enregistrer un profil depuis un événement configuré : ses
menus et boissons sont alors conservés en plus des postes. Les échéances
d’inscription et de restauration se décalent avec la nouvelle date, en heure de
Paris. Pour un profil créé directement, les valeurs initiales sont une ouverture
à J−30 à 9 h, une clôture à J−1 à 18 h et une clôture des repas à J−1 à 12 h.
Vérifier et ajuster ces échéances dans le brouillon avant publication.
Aucun participant, affectation, paiement ou repas réservé n’est copié.

La création et la modification utilisent **Date de l’événement** dans le
formulaire général. Les dates multiples des événements anciens sont conservées.
Une date ayant déjà des inscriptions ne peut pas être déplacée : créer un nouvel
événement depuis son profil.

## Présence, repas et paiement

Le participant choisit dans le même formulaire sa présence, son poste souhaité,
son repas ou sa collation, sa boisson et son paiement souhaité :

- journée complète : menus de type **repas complet**, avec choix facultatifs
  d’une collation le matin et d’une collation l’après-midi, chacune avec sa boisson
  facultative : chaude le matin, fraîche l’après-midi ;
- matin ou après-midi : menus de type **collation**, avec une boisson facultative
  chaude le matin ou fraîche l’après-midi ;
- possibilité explicite de ne demander ni repas ni collation ;
- paiement au comptoir ou via lien SumUp.

Le lien SumUp se configure une seule fois dans **Administration → Règlement de
l’association**. Les anciens liens d’événement servent de secours si le lien
commun n’est pas renseigné. Choisir SumUp ou suivre un lien ne confirme aucun
règlement reçu ; l’organisateur enregistre les paiements comme auparavant.

Les choix existants et leurs suppléments sont conservés. Modifier les tarifs ne
recalcule pas les réservations précédentes. Un menu déjà réservé ne peut changer
de type si cela rend les réservations actives incompatibles.
Les officiels ne paient aucun supplément, y compris pour ces deux collations
et la boisson, lorsqu’ils ne sont pas inscrits sur un poste bénévole ce jour-là.
Un poste bénévole actif, même en brouillon ou combiné avec un poste officiel,
rétablit les tarifs réservés pour cette journée. Sans affectation active, le poste
souhaité est pris en compte. Le changement de poste actualise les suppléments et
le solde ; les paiements déjà enregistrés et l’historique restent conservés.
Les collations supplémentaires ne sont proposées qu’à la journée.

Dans **Modifier l’événement**, les deux cases **Proposer une boisson chaude avec
la collation du matin** et **Proposer une boisson fraîche avec la collation de
l’après-midi** permettent d’activer ou de désactiver ces choix indépendamment.
Le participant peut toujours choisir **Sans boisson**. Une désactivation conserve
les choix déjà réservés et leurs tarifs ; elle empêche les nouvelles réservations
de cette boisson. Les profils réutilisables et la duplication conservent ces réglages.

## Catalogue commun à tous les événements

Ouvrir **Administration → Menus et boissons** pour créer, modifier ou désactiver
les repas, collations et boissons communs. Les choix actifs sont proposés
automatiquement sur tous les événements, y compris ceux déjà créés. Les tarifs
enregistrés sur les réservations antérieures restent conservés.

Pour chaque boisson, renseigner **Chaude** ou **Fraîche**. Les anciennes boissons
sont conservées avec la catégorie **À classer** : les modifier dans ce catalogue
pour les proposer avec les collations. Elles restent proposées avec le repas
principal et leurs réservations existantes sont conservées.

Les anciennes options propres à une date restent disponibles pour cette date.
Pour les proposer partout, utiliser **Rendre commun** et confirmer : les choix
déjà réservés sont conservés. Un conflit de nom est signalé avant validation.
Une désactivation retire l’option des nouveaux choix sans supprimer les commandes.

Les boissons déjà présentes ont été regroupées dans ce catalogue commun : une
offre par boisson est proposée sur toutes les dates. Les copies locales identiques
restent désactivées pour préserver les anciennes réservations, leurs références
et leurs tarifs. Les profils d’événements utilisent aussi cette offre commune
sans reproposer leurs anciennes copies identiques.

## Importer des menus ou boissons

Utiliser un CSV UTF-8 (séparateur `;`, `,` ou tabulation) ou un Excel `.xlsx`
contenant une seule feuille. Limites : 2 Mo et 500 lignes de données. Le modèle
CSV est téléchargeable sur l’écran d’import ; remplacer les lignes EXEMPLE.

Menus :

```csv
nom;description;supplement_euros;type
Menu végétarien;Plat et dessert;0;repas
Collation;Fruit et biscuit;0;collation
```

Boissons :

```csv
nom;description;supplement_euros;temperature
Eau;Bouteille;0;fraîche
Jus de pomme;Verre;1,50;fraîche
Café;Tasse;1;chaude
```

Seul `nom` est obligatoire. Un montant absent vaut 0 ; un type absent vaut repas
complet. Les valeurs `full_meal` et `snack` sont également acceptées.
Pour les boissons, `temperature` est facultative et accepte `chaude`, `fraîche`
ou `à classer` (également `hot`, `cold`, `unspecified`). Une colonne absente ou vide
conserve la catégorie d’une boisson existante ; une nouvelle boisson reste à classer.
Les noms sont
uniques par date pour l’import local. Dans le catalogue commun, les menus sont
identifiés par leur nom et leur type, les boissons par leur nom : un choix
existant sera mis à jour seulement après confirmation
explicite. L’aperçu affiche toutes les lignes et erreurs ; l’import n’applique
aucun changement si une ligne est invalide. Un aperçu expire après 15 minutes et
ne peut être confirmé deux fois. Aucune réservation n’est supprimée.

## Récapitulatifs séparés

**Postes** : participants, date, présence, poste, période (journée, matin,
après-midi) et état de l’affectation. Les participants non affectés sont inclus.

**Repas** : présence, formule, menu, collations du matin et de l’après-midi,
boisson, mode de paiement souhaité et
supplément par participant, puis les quantités à préparer. Les absences de choix
et les refus de repas sont distincts. L’organisateur peut renseigner un choix
manquant depuis ce rapport.

Les deux rapports disposent de leur export CSV et d’une vue imprimable. Ils sont
réservés aux organisateurs ; aucun récapitulatif nominatif de repas n’est public.

## Tickets nominatifs : six par feuille A4

Dans un événement, ouvrir **Tickets repas**. Choisir éventuellement une date
ou une personne, puis **Imprimer les tickets**. Le navigateur permet aussi
d’enregistrer un PDF : choisir A4, portrait, échelle 100 % et désactiver ses
en-têtes et pieds de page.

Chaque ticket indique le nom du participant, l’événement, la date, le repas
ou la collation, les deux collations facultatives et la boisson choisis.
Le logo de l’association apparaît sur chaque ticket. La mention **Note famille
soldée** ou **Note famille non soldée** reprend le solde de la famille pour tout
l’événement, même si l’impression est filtrée par date ou participant. Elle est
recalculée à chaque consultation et réimpression depuis les paiements enregistrés.
Le montant restant est affiché uniquement pour une note non soldée. Ce montant
familial est commun aux tickets d’une même famille : il ne faut pas les additionner.
Une note sans supplément est soldée ; le choix d’un mode de paiement ne vaut pas
enregistrement d’un règlement.
Les inscriptions annulées et les personnes sans repas ne produisent pas de ticket.
Six emplacements sont disposés sur chaque feuille, avec des emplacements vides
sur la dernière si nécessaire. L’impression ne modifie ni réservation ni paiement.
