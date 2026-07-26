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

### macOS

1. `codesign --deep --force --options runtime --sign "Developer ID Application: …" dist/FurTag.app`
2. Notarize with `notarytool` / `xcrun notarytool submit`
3. Staple: `xcrun stapler staple dist/FurTag.app`
4. Only then verify that credentials saved in v1 still load after installing v2

Bundle id: `org.furtag.FurTag` (stable across updates).

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
