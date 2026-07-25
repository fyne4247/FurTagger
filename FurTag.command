#!/bin/bash
# Double-clickable launcher. Bootstraps the project venv + deps, then runs
# FurTag CLI or GUI.

cd "$(dirname "$0")" || exit 1

setup_venv() {
    if [ ! -x ".venv/bin/python" ]; then
        echo "🔧 First run: creating virtual environment…"
        python3 -m venv .venv || {
            echo "❌ Could not create venv (is python3 installed?)."
            read -r -n1 -s
            exit 1
        }
        ./.venv/bin/python -m pip install --quiet --upgrade pip
    fi

    # Reinstall whenever requirements.txt is newer than the stamp so new deps
    # (PySide6, keyring, …) are picked up without deleting .venv.
    if [ ! -f ".venv/.deps-stamp" ] || [ "requirements.txt" -nt ".venv/.deps-stamp" ]; then
        echo "📦 Installing/updating dependencies…"
        ./.venv/bin/python -m pip install --quiet -r requirements.txt || {
            echo "❌ Dependency install failed."
            read -r -n1 -s
            exit 1
        }
        touch ".venv/.deps-stamp"
        echo "✅ Dependencies ready."
    fi

    # Requests needs certifi's bundled CA file for every HTTPS source. Package
    # metadata can survive a partial/corrupt install even when cacert.pem does
    # not, so verify the actual data file on every launch and repair just that
    # lightweight dependency when necessary.
    if ! ./.venv/bin/python -c \
        'import certifi, pathlib, sys; sys.exit(not pathlib.Path(certifi.where()).is_file())'
    then
        echo "🔧 Repairing HTTPS certificate bundle…"
        ./.venv/bin/python -m pip install --quiet --upgrade --force-reinstall certifi || {
            echo "❌ Could not repair the HTTPS certificate bundle."
            read -r -n1 -s
            exit 1
        }
        echo "✅ HTTPS certificate bundle ready."
    fi
}

setup_venv

# Mode selection:
#   · Double-click / no args  → ask CLI or GUI
#   · FurTag.command gui      → GUI
#   · FurTag.command cli      → CLI
#   · FurTag.command --help
MODE="${1:-}"

if [ -z "$MODE" ]; then
    # Interactive when launched from Terminal / double-click (TTY available).
    if [ -t 0 ]; then
        echo "🐾 FurTag"
        echo "  1) GUI  (desktop app)     [default]"
        echo "  2) CLI  (terminal)"
        echo "  q) Quit"
        printf "Choose [1/2/q]: "
        read -r choice || choice="1"
        case "${choice:-1}" in
            2|cli|CLI|c|C) MODE="cli" ;;
            q|Q|quit|exit) echo "👋 Bye."; exit 0 ;;
            *) MODE="gui" ;;
        esac
    else
        # Double-click often has no interactive stdin — prefer GUI.
        MODE="gui"
    fi
fi

case "$MODE" in
    gui|GUI|--gui|-g)
        echo "🖥️  Launching FurTag GUI…"
        exec ./.venv/bin/python furtag_gui.py
        ;;
    cli|CLI|--cli|-c)
        echo "💻 Launching FurTag CLI…"
        ./.venv/bin/python furtag.py
        echo
        echo "Press any key to close…"
        read -r -n1 -s
        ;;
    -h|--help|help)
        echo "Usage: $(basename "$0") [gui|cli]"
        echo "  (no args)  prompt, or GUI if non-interactive"
        echo "  gui        desktop app (furtag_gui.py)"
        echo "  cli        terminal app (furtag.py)"
        exit 0
        ;;
    *)
        echo "Unknown mode: $MODE  (try: gui | cli)"
        read -r -n1 -s
        exit 1
        ;;
esac
