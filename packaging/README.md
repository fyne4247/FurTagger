# FurTag packaging

## Build (native per OS)

```bash
./packaging/build.sh
```

Or:

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm packaging/furtag.spec
```

Artifacts land in `dist/`. **Do not cross-compile.**

## Signing (required before credential-persistence tests)

Keyring ACLs key to the **code signature identity**, not just the bundle id.
Unsigned or ad-hoc-signed rebuilds look like a different app to the OS, so
saved credentials will appear to "vanish" after every rebuild.

### macOS — local use (self-signed)

This is enough to stop the repeating keychain prompt on your own machine.
Notarization is **not** needed: it only matters for Gatekeeper, which never
inspects an app you built locally rather than downloaded.

1. Keychain Access → Certificate Assistant → *Create a Certificate…*
   - Name: `FurTag Self-Signed`
   - Identity Type: **Self Signed Root**
   - Certificate Type: **Code Signing**
2. Confirm it registered: `security find-identity -v -p codesigning`
3. Build and sign:
   ```bash
   export FURTAG_CODESIGN_IDENTITY="FurTag Self-Signed"
   ./packaging/build.sh
   ```
4. Verify: `codesign -dvvv dist/FurTag.app` — the Authority must name the
   certificate. If it says `Signature=adhoc`, signing did not take effect.
5. Launch `dist/FurTag.app` and let it migrate saved credentials (one burst of
   prompts, once). Relaunch to confirm it is now silent.

**Back the certificate up** — export it as a `.p12` into `certs/` (gitignored).
The keychain ACL binds to *this* certificate. Recreating it produces a different
designated requirement and the prompts return.

### macOS — distribution (Developer ID)

1. `codesign --deep --force --options runtime --sign "Developer ID Application: …" dist/FurTag.app`
2. Notarize with `notarytool` / `xcrun notarytool submit`
3. Staple: `xcrun stapler staple dist/FurTag.app`
4. Only then verify that credentials saved in v1 still load after installing v2

Bundle id: `org.furtag.FurTag` (stable across updates).

### Why signing matters here

A keychain "Always Allow" grant records the trusted app by its *designated
requirement*. For ad-hoc-signed code that requirement is the exact `cdhash` of
the binary, so any rebuild — or, when running from source, any
`brew upgrade python@3.14` — invalidates every grant at once. A stable signing
identity gives the bundle a requirement that survives rebuilds.

Running from source (`./FurTag.command`) uses Homebrew's ad-hoc-signed Python
and will therefore keep prompting. Use the signed `.app`, or set the `FURTAG_*`
environment variables, which take precedence over the keychain.

### Windows

Sign `FurTag.exe` with `signtool` before update-persistence tests.

### Linux

No code-signing required for keyring, but a working Secret Service (or
`FURTAG_*` env vars) is needed for secrets.

## Settings location

Mutable settings always use `platformdirs` user config — never beside the
executable. Secrets never enter `settings.json`.

## Homebrew cask

See `packaging/homebrew/furtag.rb`.

**How much work is it, really?**

| Piece | Effort |
| ----- | ------ |
| Cask Ruby formula (version, URL, sha256, `app`, zap) | Small — under an hour once the zip exists |
| Personal tap (`brew tap you/furtag`) | Small — new GitHub repo with the cask file |
| Official `homebrew-cask` PR | Extra process/review; do personal tap first |
| **Signed + notarized `FurTag.app` zip on a Release** | **Most of the work** — PyInstaller build, Developer ID, notarytool, staple, smoke-test keyring across upgrades |

Until the signed archive exists, Homebrew is not a useful install path. Ship
clone + `./FurTag-GUI.command` (or a plain GitHub Release with source/tag only).
