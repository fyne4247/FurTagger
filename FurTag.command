#!/bin/bash
# Double-clickable launcher. Runs furtag.py inside the project's venv,
# bootstrapping the venv + dependencies on first run.

cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
    echo "🔧 First run: creating virtual environment and installing dependencies…"
    python3 -m venv .venv || { echo "❌ Could not create venv."; read -r -n1 -s; exit 1; }
    ./.venv/bin/python -m pip install --quiet --upgrade pip
fi

# (Re)install deps whenever requirements.txt is newer than the last install stamp,
# so adding a dependency (e.g. PyMuPDF) is picked up without deleting .venv.
if [ ! -f ".venv/.deps-stamp" ] || [ "requirements.txt" -nt ".venv/.deps-stamp" ]; then
    echo "📦 Installing/updating dependencies…"
    ./.venv/bin/python -m pip install --quiet -r requirements.txt \
        || { echo "❌ Dependency install failed."; read -r -n1 -s; exit 1; }
    touch ".venv/.deps-stamp"
    echo "✅ Dependencies ready."
fi

./.venv/bin/python furtag.py

echo
echo "Press any key to close…"
read -r -n1 -s
