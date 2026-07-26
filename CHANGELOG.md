# Changelog

All notable changes to FurTag are documented here.

## [0.1.0] — 2026-07-26

First public snapshot of the current `main` tree. Suitable for people who already run Hydrus and want automated booru-backed tagging.

### Highlights

- **Multi-source hash tier** — concurrent MD5 lookup on e621, InkBunny, Danbooru, and Gelbooru; results merged.
- **Perceptual fallback** — Fluffle → SauceNAO when hash misses; optional Fluffle review queue.
- **Hydrus Client API sink** — import, tags, URL association, result pages, deleted-file duplicate tagging, resumable sidecar sync.
- **Exact-URL metadata enrichment** — byte-exact post URLs can be queued through Hydrus downloaders for notes/descriptions (toggleable).
- **PDF comics** — render pages to PNG; before render, set **comic name** and optional **artist** (`comic:` / `creator:` / `page:`), stored in `.furtag_pdf.json`.
- **Desktop GUI + CLI** — PySide6 app and double-clickable macOS launchers; secrets in OS keyring or `FURTAG_*` env vars.
- **Resumable ledgers** — per-folder `.furtag_ledger.json` so interrupted runs pick up cleanly.

### Recent fixes (leading into 0.1.0)

- **Multi-file InkBunny** — submission URLs with more than one file are **associated only**, not queued for Hydrus download (avoids importing every page of a multi-file post).
- **InkBunny noise** — filter ubiquitous `keywording policy` keywords as junk tags.
- **GUI status lights** — source indicators are green when active, red when off/unavailable (so they stop looking like radio buttons).

### Install (this release)

No signed binary yet. Clone the repo and run:

```bash
./FurTag-GUI.command
# or
./FurTag.command
```

See the README for credentials, Hydrus API permissions, and source setup.

### Not in this release

- Notarized macOS `.app` / Homebrew cask (skeleton only under `packaging/homebrew/`)
- Windows/Linux first-class installers
