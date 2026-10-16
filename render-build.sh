#!/usr/bin/env bash
set -o errexit

pip install --disable-pip-version-check -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py bootstrap_admin
python manage.py check --deploy

# Contrôle ponctuel demandé lors du raccordement, sans envoi ni sortie de secret.
if [ "${BREVO_VERIFY_ON_DEPLOY:-0}" = "1" ]; then
    python manage.py check_brevo_connection --no-fail
fi
