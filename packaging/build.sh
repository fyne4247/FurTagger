#!/usr/bin/env bash
# Native PyInstaller build for the current platform.
# Run on macOS / Windows (Git Bash) / Linux separately — do not cross-compile.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt pyinstaller

echo "Building FurTag for $(uname -s)…"
pyinstaller --noconfirm packaging/furtag.spec

echo ""
echo "Artifacts under dist/"
ls -la dist/ || true
echo ""
echo "Next steps:"
echo "  · macOS: codesign + notarize BEFORE testing keyring persistence"
echo "  · Windows: signtool sign the .exe before update-persistence tests"
echo "  · Settings always live in the platformdirs user config dir, never beside the binary"
