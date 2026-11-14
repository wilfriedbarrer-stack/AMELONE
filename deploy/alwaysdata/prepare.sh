#!/usr/bin/env bash
# Installs the application only; restores and schema changes remain explicit.
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
env_file=""
venv_dir=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env-file) env_file="${2:?Chemin du fichier privé manquant}"; shift 2 ;;
    --venv) venv_dir="${2:?Chemin du virtualenv manquant}"; shift 2 ;;
    *) echo "Usage : bash deploy/alwaysdata/prepare.sh --env-file /chemin/prive.env --venv /chemin/venv" >&2; exit 2 ;;
  esac
done
if [[ -z "$env_file" || -z "$venv_dir" || "$env_file" != /* || "$venv_dir" != /* ]]; then
  echo "Les deux chemins absolus --env-file et --venv sont obligatoires." >&2
  exit 2
fi
if [[ ! -f "$env_file" ]]; then
  echo "Le fichier de configuration privé est absent." >&2
  exit 2
fi
# Administrative configuration created by init_env.py; never enable shell tracing.
source "$env_file"
export DJANGO_SETTINGS_MODULE=amelone.settings_alwaysdata
if [[ ! -x "$venv_dir/bin/python" ]]; then
  PYTHON_VERSION=3.12 python -m venv "$venv_dir"
fi
cd -- "$project_dir"
"$venv_dir/bin/python" -c 'import sys; assert sys.version_info[:2] == (3, 12), "Sélectionner Python 3.12 pour ce virtualenv."'
"$venv_dir/bin/python" -m pip install --require-hashes -r requirements.txt
"$venv_dir/bin/python" manage.py collectstatic --noinput
"$venv_dir/bin/python" manage.py check --deploy
echo "Préparation terminée. Aucune restauration, migration SQL ou création de compte n’a été exécutée."
