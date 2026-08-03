# Changelog

All notable changes to FurTag are documented here.

## Unreleased

- **No-match queue reconciliation** — prior no-match files completed by the Hydrus pre-scan pass are now removed from the already-built search queue, preventing an immediate redundant booru/perceptual rescan in the same run. The summary reports live, previously deleted, vetoed, terminal, and still-pending outcomes accurately instead of calling every result a new import.
- **Legacy match migration** — `tools/migrate_legacy_ledgers.py` can conservatively bind unchanged legacy matches to the current search profile and Hydrus database. It defaults to dry-run, verifies each SHA-256 (or MD5-resolved SHA-256) is currently local in Hydrus, backs up every changed ledger, and leaves unverifiable rows open.
- **Quieter cancellation/deleted-file logging** — expected lookup cancellation no longer appears as a source failure, and repeated “previously deleted; no current duplicates” notices are informational and aggregated instead of flooding the issue stream one file at a time.
- **Typed Hydrus outcomes** — imports now preserve separate import and metadata states, including live, previously deleted, vetoed, permission-missing, policy-skipped, and retryable failures. Deleted originals retain their SHA-256 and any successfully tagged duplicate targets.
- **Correct deleted-file completion** — only a successful empty relationship query creates a terminal no-duplicate result. Missing relationship permission and relationship/API failures remain retryable; disabling duplicate tagging is terminal only for that policy and reopens when enabled.
- **Hydrus database scoping** — normal output, unmatched imports, sidecar sync, and directory seals are bound to a persisted non-secret Hydrus profile UUID plus normalized API origin. The GUI has an explicit action for replacing/rebuilding a Hydrus database.
- **Search-profile invalidation** — source toggles, credential availability/auth mode, perceptual thresholds, and matching policy now participate in ledger and directory reuse decisions. Newly available sources reopen old matches/no-matches without discarding reusable MD5s.
- **Output-completeness fixes** — a sidecar can satisfy its own sink but no longer hides stale search state, unresolved hashes, failed Hydrus writes, or a newly required unmatched import. Directory seals include current sidecar/output policy and cannot reseal cancelled or incomplete recovery work.
- **Resumable unmatched imports** — no-match search truth and Hydrus import completion are independent checkpoints. Status-3 and veto outcomes retain their SHA/reason, while failed required imports remain queued for reconciliation.
- **Duplicate correctness** — prior clean no-match and scoped deleted outcomes resolve new exact copies without redundant searches/imports. Mirrored historical matches preserve known tag time and metadata version instead of inventing current freshness.
- **Sidecar-sync hardening** — candidates stream in bounded batches, hidden directories follow main-scan traversal rules, terminal deleted outcomes are checkpointed separately from failures, and legacy unscoped checkpoints cannot cross Hydrus databases.
- **Fingerprint and decoder hardening** — nanosecond mtimes now flow through normal scans, duplicate/review rows, and sidecar sync; decoder-profile changes reopen deterministic unreadable rows, while transient image and local-hash I/O failures remain retryable. Pillow is explicitly constrained to `>=9.1.0` with a compatibility fallback.
- **Cleaner diagnostics** — success messages no longer pollute the issue pane, relationship-permission warnings are session-bounded, and repeated per-source failures are aggregated with a final count.
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

### Upgrade note

The first scan after this upgrade may revalidate legacy rows and directory seals that lack a current search profile, output policy, or Hydrus database scope. Existing MD5 checkpoints are retained, so this should avoid repeating the disk-heavy hash pass even when network/output reconciliation is required.

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
