#!/usr/bin/env bash
# Create a self-signed CODE SIGNING identity for local FurTag builds.
#
# Why this exists: keychain "Always Allow" grants bind to a program's code
# signature. Ad-hoc-signed code (anything PyInstaller or Homebrew Python
# produces unsigned) has no stable identity, so macOS forgets the grant and
# re-prompts forever. A stable signature — even a self-signed one — fixes that.
#
# This identity is trusted by THIS Mac only. Distributing to other machines
# needs a paid Apple "Developer ID Application" certificate instead.
#
# Undo:  Keychain Access → login → delete the "FurTag Self-Signed" cert + key.
set -euo pipefail

CN="${FURTAG_CODESIGN_IDENTITY:-FurTag Self-Signed}"
DIR="$(cd "$(dirname "$0")" && pwd)"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

if security find-identity -v -p codesigning | grep -qF "$CN"; then
    echo "✅ '$CN' already exists — nothing to do."
    exit 0
fi

echo "🔧 Generating key + certificate for '$CN'…"
cat > "$DIR/openssl.cnf" <<CNF
[ req ]
distinguished_name = dn
x509_extensions    = codesign
prompt             = no
[ dn ]
CN = $CN
[ codesign ]
basicConstraints       = critical,CA:false
keyUsage               = critical,digitalSignature
extendedKeyUsage       = critical,codeSigning
subjectKeyIdentifier   = hash
CNF

openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -config "$DIR/openssl.cnf" \
    -keyout "$DIR/furtag-signing.key" \
    -out "$DIR/furtag-signing.crt" 2>/dev/null

# macOS's `security import` cannot open an empty-password PKCS#12, so bundle it
# under a throwaway passphrase that only lives for the next two commands.
# -legacy keeps the encryption to what SecKeychainItemImport can read.
PASS="$(openssl rand -hex 16)"
openssl pkcs12 -export -legacy \
    -inkey "$DIR/furtag-signing.key" \
    -in "$DIR/furtag-signing.crt" \
    -name "$CN" -passout "pass:$PASS" -out "$DIR/furtag-signing.p12" 2>/dev/null

# -T /usr/bin/codesign pre-authorizes codesign to use the private key, so
# signing does not raise its own keychain prompt on every build.
echo "🔐 Importing into your login keychain (may ask for your password)…"
security import "$DIR/furtag-signing.p12" -k "$KEYCHAIN" -P "$PASS" \
    -T /usr/bin/codesign -T /usr/bin/security

echo "🔏 Marking it trusted for code signing…"
security add-trusted-cert -r trustRoot -p codeSign \
    -k "$KEYCHAIN" "$DIR/furtag-signing.crt"

# Stop macOS prompting for the private key on each signing run.
security set-key-partition-list -S apple-tool:,apple: -s -k "" \
    "$KEYCHAIN" >/dev/null 2>&1 || true

rm -f "$DIR/openssl.cnf" "$DIR/furtag-signing.p12"

echo ""
security find-identity -v -p codesigning
echo ""
echo "✅ Done. Now build with:"
echo "     export FURTAG_CODESIGN_IDENTITY=\"$CN\""
echo "     ./packaging/build.sh"
