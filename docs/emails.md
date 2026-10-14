# Configuration des emails

L'envoi est désactivé par défaut. Tant que `EMAIL_ENABLED=1` et les paramètres SMTP ne
sont pas présents, l'interface affiche la limite et aucun message n'est marqué envoyé.

Variables nécessaires :

```dotenv
EMAIL_ENABLED=1
EMAIL_BACKEND=django.core.mail.backends.smtp.EmailBackend
EMAIL_HOST=smtp.example.org
EMAIL_PORT=587
EMAIL_HOST_USER=compte-smtp
EMAIL_HOST_PASSWORD=secret-fourni-hors-du-depot
EMAIL_USE_TLS=1
DEFAULT_FROM_EMAIL=AMELONE <benevoles@example.org>
SITE_URL=https://benevoles.example.org
```

Le mot de passe SMTP doit être stocké dans le gestionnaire de secrets de l'hébergeur,
jamais dans Git ni dans le navigateur.

Le workflow permet un aperçu, un envoi par famille ou sélection, et une relance des seuls
échecs. Chaque tentative est historisée. Un statut « accepté » signifie uniquement que
le backend SMTP n'a pas rejeté l'envoi. Après modification d'une affectation déjà
communiquée, son empreinte diffère du dernier instantané et l'interface demande une mise
à jour.

