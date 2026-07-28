# Changelog

All notable changes to FurTag are documented here.

## Unreleased

- **Direct source notes** — e621 descriptions and InkBunny titles/descriptions now go straight from their existing API responses to Hydrus `/add_notes/set_notes`; the slow URL-downloader enrichment path is now optional and off by default.
- **Persistent recent folders** — the GUI remembers a bounded MRU list in the platform-specific user settings file, including temporarily disconnected volumes.
- **Safer retries** — transient source failures, partial additive hash hits, exhausted SauceNAO quota, and incomplete Hydrus/sidecar writes no longer become permanent `nomatch`/`matched` ledger records; permanent credential/media failures no longer loop forever.
- **Ledger backfill + manifests** — old resolved entries retry once for direct notes, and directory fast-skip fingerprints now cover names, nanosecond mtimes, and sidecar state.
- **Capability-aware backfill** — sidecar/offline scans defer direct-note migration until Hydrus can actually write notes, while missing optional permissions no longer cause full-library retry loops.
- **Duplicate completion safety** — exact copies remain pending until the canonical result and each per-copy sidecar write complete successfully.
- **PDF resume correctness** — partial page folders no longer count as completed renders; an atomic completion manifest validates every page.
- **Live credential safety** — reloading/removing credentials clears stale source sessions and Hydrus permissions before reconnecting.
- **GUI lifecycle hardening** — stale discovery results cannot populate a newly selected folder, indexing workers are retained safely, and scan-folder controls lock during active work.
- **Privacy hardening** — local settings, recent paths, Codex memory, and FurTag PDF runtime metadata are explicitly gitignored.

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
