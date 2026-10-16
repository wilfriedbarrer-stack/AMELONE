#!/usr/bin/env bash
set -o errexit

pip install --disable-pip-version-check -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
python manage.py bootstrap_admin
python manage.py check --deploy
