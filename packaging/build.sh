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

# ── macOS signing ────────────────────────────────────────────────────────────
# Keychain ACLs bind to the code signature, not the bundle id. An unsigned or
# ad-hoc-signed bundle gets a new designated requirement on every rebuild, so
# macOS forgets "Always Allow" and re-prompts for saved credentials forever.
# Signing with a stable identity — even a self-signed one — fixes that.
#
#   export FURTAG_CODESIGN_IDENTITY="FurTag Self-Signed"
#   ./packaging/build.sh
#
# Hardened runtime (--options runtime) is deliberately NOT used: it is only
# required for notarization and would force a set of Python entitlements that
# a locally-built app does not need.
APP="dist/FurTag.app"
if [[ "$(uname -s)" == "Darwin" && -d "$APP" ]]; then
  if [[ -n "${FURTAG_CODESIGN_IDENTITY:-}" ]]; then
    ID="$FURTAG_CODESIGN_IDENTITY"
    echo "Signing $APP as '$ID'…"

    # Sign inside-out: nested frameworks and Mach-O libraries first, then the
    # main executable, then the bundle. PySide6 ships nested .framework bundles
    # that must be signed as bundles in their own right.
    while IFS= read -r fw; do
      codesign --force --timestamp=none --sign "$ID" "$fw"
    done < <(find "$APP/Contents" -type d -name "*.framework" -print)

    while IFS= read -r lib; do
      codesign --force --timestamp=none --sign "$ID" "$lib"
    done < <(find "$APP/Contents" -type f \( -name "*.dylib" -o -name "*.so" \) -print)

    codesign --force --timestamp=none --sign "$ID" "$APP/Contents/MacOS/FurTag"
    codesign --force --timestamp=none --sign "$ID" "$APP"

    echo "Verifying signature…"
    if ! codesign --verify --deep --strict "$APP"; then
      echo "⚠️  Explicit signing left something unsigned; retrying with --deep."
      codesign --force --deep --timestamp=none --sign "$ID" "$APP"
      codesign --verify --deep --strict "$APP"
    fi
    codesign -dvv "$APP" 2>&1 | grep -E "Authority|Signature" || true
    echo "✅ Signed. Launch dist/FurTag.app to migrate credentials (one-time prompts)."
  else
    echo ""
    echo "⚠️  FURTAG_CODESIGN_IDENTITY is not set — the bundle is ad-hoc signed."
    echo "   macOS will re-prompt for keychain access after every rebuild."
    echo "   Create a self-signed Code Signing certificate in Keychain Access,"
    echo "   then re-run with:  export FURTAG_CODESIGN_IDENTITY=\"FurTag Self-Signed\""
  fi
fi

echo ""
echo "Artifacts under dist/"
ls -la dist/ || true
echo ""
echo "Next steps:"
echo "  · macOS: sign with a STABLE identity before testing keyring persistence"
echo "           (notarize as well only if distributing to other machines)"
echo "  · Windows: signtool sign the .exe before update-persistence tests"
echo "  · Settings always live in the platformdirs user config dir, never beside the binary"
