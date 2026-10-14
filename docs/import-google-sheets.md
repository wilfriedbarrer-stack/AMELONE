# Import depuis Google Sheets

L'import ne modifie jamais la feuille d'origine. Dans Google Sheets, exporter séparément
les onglets au format CSV UTF-8, puis exécuter la commande d'import en mode simulation.

```bash
uv run python manage.py import_legacy \
  --events exports/evenements.csv \
  --stations exports/postes.csv \
  --registrations exports/inscriptions.csv \
  --dry-run \
  --report rapport-import.json
```

Retirer `--dry-run` après vérification du rapport. La commande attend les en-têtes :

- événements : `legacy_id,nom,description,lieu,dates,statut,ouverture_inscriptions,cloture_inscriptions,cloture_repas,lien_sumup,consignes` (`dates` séparées par `;`, dates/heures ISO dans le fuseau Europe/Paris) ;
- postes : `legacy_id,event_legacy_id,nom,categorie,date,periode,capacite,debut,fin` ;
- inscriptions : `legacy_id,event_legacy_id,date,prenom,nom,periode,poste_souhaite,remarque,email_responsable,date_naissance`.

Les anciennes lignes sans email ou date de naissance sont importées dans une zone à
rattacher et signalées dans le rapport ; aucune valeur n'est inventée. Les familles et
membres existants sont rapprochés seulement par identifiants explicites ou après revue
humaine. Les ambiguïtés restent non importées.

Conserver le rapport avec l'export source. Les `legacy_id` servent à rendre une reprise
idempotente et à éviter les doublons.

