#!/usr/bin/env bash
# Mac/Linux: installs dependencies into a local virtualenv and starts the site.
set -e
cd "$(dirname "$0")"
PY=$(command -v python3 || command -v python)
if [ -z "$PY" ]; then echo "Python 3.10+ is required: https://www.python.org/downloads/"; exit 1; fi
[ -d .venv ] || "$PY" -m venv .venv
. .venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
echo "Open http://localhost:5000/care-recruitment-compliance-checklist"
python app.py
