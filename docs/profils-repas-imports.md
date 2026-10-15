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

- journée complète : menus de type **repas complet** ;
- matin ou après-midi : menus de type **collation** ;
- possibilité explicite de ne demander ni repas ni collation ;
- paiement au comptoir ou via lien SumUp.

Le lien SumUp se configure une seule fois dans **Administration → Règlement de
l’association**. Les anciens liens d’événement servent de secours si le lien
commun n’est pas renseigné. Choisir SumUp ou suivre un lien ne confirme aucun
règlement reçu ; l’organisateur enregistre les paiements comme auparavant.

Les choix existants et leurs suppléments sont conservés. Modifier les tarifs ne
recalcule pas les réservations précédentes. Un menu déjà réservé ne peut changer
de type si cela rend les réservations actives incompatibles.

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
nom;description;supplement_euros
Eau;Bouteille;0
Jus de pomme;Verre;1,50
```

Seul `nom` est obligatoire. Un montant absent vaut 0 ; un type absent vaut repas
complet. Les valeurs `full_meal` et `snack` sont également acceptées. Les noms sont
uniques par date : un nom existant sera mis à jour seulement après confirmation
explicite. L’aperçu affiche toutes les lignes et erreurs ; l’import n’applique
aucun changement si une ligne est invalide. Un aperçu expire après 15 minutes et
ne peut être confirmé deux fois. Aucune réservation n’est supprimée.

## Récapitulatifs séparés

**Postes** : participants, date, présence, poste, période (journée, matin,
après-midi) et état de l’affectation. Les participants non affectés sont inclus.

**Repas** : présence, formule, menu, boisson, mode de paiement souhaité et
supplément par participant, puis les quantités à préparer. Les absences de choix
et les refus de repas sont distincts. L’organisateur peut renseigner un choix
manquant depuis ce rapport.

Les deux rapports disposent de leur export CSV et d’une vue imprimable. Ils sont
réservés aux organisateurs ; aucun récapitulatif nominatif de repas n’est public.
