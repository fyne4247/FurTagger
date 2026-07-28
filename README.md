# FurTag

**Point it at a folder. It finds where the art came from and tags it for [Hydrus](https://hydrusnetwork.github.io/hydrus/).**

FurTag reverse-image-searches your media against furry/booru sources (**e621, InkBunny, Danbooru, Gelbooru**, then **Fluffle** / **SauceNAO**), then **imports + tags + source URLs** into a running Hydrus client — or writes Hydrus-compatible sidecars if you prefer files only.

| | |
| --- | --- |
| **For** | Big personal archives, Hydrus users, furry art libraries |
| **Runs on** | macOS (double-click `.command` / GUI); Python engine is otherwise cross-platform |
| **Needs** | Python 3.10+, API keys for the sites you use, optional Hydrus Client API |
| **Repo** | [github.com/fyne4247/FurTagger](https://github.com/fyne4247/FurTagger) |

Built for large libraries: multi-source MD5 lookups, resumable ledgers, polite rate limits, PDF page rendering, desktop GUI or CLI.

```bash
# GUI
./FurTag-GUI.command

# CLI
./FurTag.command
```

> **Screenshots:** drop a couple under `docs/` when you have them (status bar + a finished scan is enough). Discovery cares more about a clear pitch than a marketing site.

---

## Features

### Matching

- **Multi-source hash tier** — every file’s local MD5 is looked up on **e621, InkBunny, Danbooru, and Gelbooru** concurrently; results are merged. MD5 identity is byte-exact (no false positives).
- **Perceptual fallback** — images with no hash hit go to **Fluffle**, then **SauceNAO**. Authoritative booru re-queries fill namespaced tags when a post ID is known.
- **Pipelined tiers** — hash and perceptual work run together; a two-track progress display shows each tier’s current file, phase, and ETA.
- **PDF support** — pages render to PNG and enter the perceptual tier. Before rendering, you can set **comic name** and optional **artist** per PDF (`comic:` / `creator:` / `page:`). Choices are saved in `.furtag_pdf.json` beside the pages. Optional if PyMuPDF is installed.
- **Exact-duplicate fan-out** — byte-identical files share one network search; copies get matching ledger records and sidecars.

### Hydrus

- **Client API output** — import files, apply tags, associate URLs (default service: **downloader tags**).
- **Direct source notes** — e621 descriptions and InkBunny titles/descriptions are reused from source API responses FurTag already fetched, then written straight to Hydrus notes by SHA-256. This is the default and does **not** queue a downloader job per URL.
- **Optional legacy URL enrichment** — exact post URLs can still be queued through Hydrus for parser-only metadata such as timestamps. It is off by default because it is substantially slower. Perceptual/external URLs and multi-file InkBunny submissions remain associate-only.
- **Resumable sidecar sync** — push existing `<file>.txt` / `<file>.urls.txt` into Hydrus without re-searching; successful payloads are checkpointed in the ledger.
- **Result pages** — optional New Imports / Newly Tagged / Duplicate Tagged / Already Tagged pages.
- **Deleted-file duplicates** — when import hits a previously deleted file, tags/URLs can be applied to current Hydrus duplicate-group members (same URL policy as a normal push).

### Reliability

- **Session ledger** — `.furtag_ledger.json` per folder, keyed by path + size + mtime; interrupted runs resume without re-querying finished files.
- **Transient network errors stay retryable** — source failures are not recorded as clean `nomatch`; partial hash hits wait for every additive source instead of permanently losing metadata.
- **Incomplete output stays retryable** — failed Hydrus tag/URL/note writes are not recorded as completed matches.
- **Permanent failures do not loop** — rejected source credentials disable only that source for the current credential load, unavailable optional Hydrus permissions degrade once, and unchanged unreadable media is checkpointed until its file changes.
- **Safe directory fast-skip** — the sealed fingerprint includes filenames, nanosecond mtimes, and FurTag sidecar state rather than only file count and total bytes.
- **Atomic PDF completion** — a PDF is considered rendered only when its completion manifest names every finished page; partial renders restart cleanly.
- **Selective safe reset** — NUKE! previews generated files and lets you independently remove ledgers/reports, sidecars, or rendered PDF pages without touching source media.
- **Per-service rate limiting** — independent pacers; SauceNAO adapts to reported quotas and backs off on repeated 429s.
- **Secrets in the OS keyring** (or `FURTAG_*` env vars) — never in `settings.json` or project files.

---

## Requirements

- **Python 3.10+** recommended (3.9+ may work)
- **macOS** for the double-click launchers and Finder drag-and-drop (the Python engine is otherwise cross-platform)
- Packages from `requirements.txt` (installed automatically by the launchers):
  - `pillow`, `requests`, `certifi`, `regex`
  - `platformdirs`, `keyring`
  - `PySide6` (GUI)
  - `PyMuPDF` (optional PDF support)

---

## Quick start

### GUI

```bash
./FurTag-GUI.command
# or
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python furtag_gui.py
```

### CLI

```bash
./FurTag.command
# or
.venv/bin/python furtag.py
```

On first run the launcher creates `.venv`, installs dependencies, and verifies the HTTPS CA bundle (`certifi`). If you **rename or move** the project folder while a scan is running, restart FurTag so workers pick up the new path.

### Selective reset (NUKE!)

Use **Reset…** in the GUI, or enter `NUKE!` at the CLI folder prompt. After choosing a folder, FurTag previews its generated files and lets you select any combination of:

- ledgers and reports (`.furtag_ledger.json`, `duplicates.log`, and temporary versions)
- FurTag tag/URL sidecars
- rendered PDF page PNGs

Ledgers/reports and sidecars are selected by default; rendered PDF pages are not. Each category is independent, so you can remove only the ledger/report state, only sidecars, or only rendered pages. FurTag requires confirmation, refuses filesystem roots, and never deletes source media or source PDFs.

---

## Credentials

Open **Credentials** in the GUI, or set environment variables:

| Field | Environment variable |
| ----- | -------------------- |
| e621 username / API key | `FURTAG_E621_USERNAME` / `FURTAG_E621_API_KEY` |
| InkBunny username / password | `FURTAG_INKBUNNY_USERNAME` / `FURTAG_INKBUNNY_PASSWORD` |
| Danbooru username / API key | `FURTAG_DANBOORU_USERNAME` / `FURTAG_DANBOORU_API_KEY` |
| Gelbooru user id / API key | `FURTAG_GELBOORU_USER_ID` / `FURTAG_GELBOORU_API_KEY` |
| SauceNAO API key | `FURTAG_SAUCE_NAO_API_KEY` |
| Hydrus API URL / access key | `FURTAG_HYDRUS_API_URL` / `FURTAG_HYDRUS_ACCESS_KEY` |

Resolution order: **environment → OS keyring**. Missing credentials disable only that source.

Non-secret options (thresholds, source toggles, page names, sidecar patterns, rate limits, direct notes, optional URL enrichment) live in platform-specific `settings.json` via `platformdirs`.

The GUI also remembers up to 12 recently selected scan folders there, including temporarily disconnected volumes; the **Clear** button forgets the list. This history is local machine state: it is never written into the repository, media ledgers, or sidecars, and local settings/state filenames are gitignored.

**InkBunny:** enable API access and adult ratings in account settings.  
**Danbooru:** verified-email account for auth; FurTag falls back to anonymous MD5 lookup if the key is rejected.  
**Hydrus Client API:** enable under *services → manage services*; access key needs import files, edit tags, edit URLs, **Add Notes / Edit File Notes**, and manage pages. Add **Search for and Fetch Files** for MD5→SHA-256 caching / sidecar resume. Add **Manage File Relationships** for deleted-duplicate tagging. URL classes/parsers are needed only if you enable the optional slow URL-enrichment path.

---

## Security

- Secrets belong in the keyring or environment — **not** in the repo, logs, or progress events.
- There is no end-user `credentials.txt` import path.
- Local signing materials (if any) go under `certs/` (gitignored); see `certs/README.md` and `packaging/README.md`.

---

## How it works

1. **Index** — walk the tree; skip unchanged ledger entries, existing tag sidecars (when configured), and junk files.
2. **Hash** — MD5 (and optional Hydrus SHA-256 cache) in a thread pool.
3. **Hash tier** — e621 / InkBunny / Danbooru / Gelbooru by MD5, concurrent.
4. **Perceptual tier** — Fluffle → SauceNAO for images that missed every hash lookup.
5. **Write** — Hydrus push and/or sidecars; source descriptions go directly to Hydrus notes. Exact URLs may optionally enter the legacy downloader-enrichment path.

**URL write policy**

| Source of URLs | Hydrus behavior |
| -------------- | ---------------- |
| MD5 hash-tier post URLs (e621 / single-file IB / Danbooru / Gelbooru) | Associated normally; may also be queued for optional legacy enrichment |
| Multi-file InkBunny submission pages (`/s/{id}` with pagecount > 1) | Associated only (never queued for download) |
| Perceptual / external / artist “source” links | Associated only |

**Ledger statuses** include `matched`, `nomatch`, `duplicate`, `hashed` (retry later after network errors), plus independent `sidecar_sync` checkpoints.

The metadata ledger version was bumped for direct notes. Once Hydrus has note-editing permission, the next scan intentionally revisits older e621/Inkbunny matches once, reusing cached MD5s. If Hydrus is offline, notes are disabled, or the key lacks permission, that backfill is deferred without repeatedly querying sources.

---

## Output (sidecars)

When sidecars are enabled:

| File | Contents |
| ---- | -------- |
| `<file>.<ext>.txt` | tags, one per line |
| `<file>.<ext>.urls.txt` | source URLs, one per line |

Namespaces follow Hydrus conventions (`creator:`, `character:`, `species:`, `series:`, `comic:`, `page:`, `site:`, …).

---

## Project layout

| Path | Role |
| ---- | ---- |
| `furtag.py` | Engine orchestration, sources, scanning |
| `furtag_hydrus.py` | Hydrus Client API sink + URL routing |
| `furtag_urls.py` | URL write policy (associate vs enrich) |
| `furtag_gui.py` | PySide6 desktop app |
| `furtag_settings.py` / `furtag_credentials.py` | Preferences + secrets |
| `furtag_events.py` / `furtag_review.py` | Progress events + Fluffle review queue |
| `tests/` | Unit tests (`python -m unittest discover -s tests`) |
| `packaging/` | PyInstaller / signing notes |

---

## Packaging

See `packaging/README.md`. Build natively per OS; **sign** macOS/Windows builds before testing keyring persistence across upgrades.

A Homebrew cask skeleton lives in `packaging/homebrew/` — it is **not** ready to publish until you have a signed, notarized `.app` zip on a GitHub Release (see packaging notes). Until then, clone + `./FurTag-GUI.command` is the supported install path.

---

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

---

## License

MIT — see [LICENSE](LICENSE).
