# FurTag GUI — Implementation Plan

Standalone working plan for putting a cross-platform desktop GUI on FurTag.
This document is self-contained; it does not require reading `AGENTS.md` to follow.

Engine: `furtag.py` (3,523 lines) · baseline branch `hydrus-pages-and-hash-caching`

> **Line references in this document** point at the working-tree `furtag.py` (3,523 lines)
> — the branch head `01f6f34` *plus its uncommitted changes*. They will not resolve against
> a clean checkout of that commit (2,979 lines). Re-anchor them once the working changes
> are committed.

---

## 1. Current state and goal

FurTag today is a single terminal-driven Python script. `furtag.py` reverse-image-searches
media against e621 / InkBunny / Danbooru / Gelbooru by MD5, falls back to Fluffle and
SauceNAO perceptually, and writes results into Hydrus Network via the Client API (or to
sidecar files). It works, and it is the behavioral baseline.

The goal is a cross-platform Python desktop GUI in the same spirit as Hydrus — runnable
from source on macOS, Windows, and Linux, with optional native packages per platform —
that is **fully customizable from the ground up**: every behavior currently fixed in a
module constant or a hand-edited `credentials.txt` line becomes a visible control with a
persistent default.

**Hard constraint:** default behavior is unchanged. Every new toggle ships with its default
set to today's behavior, so an untouched install matches the current baseline exactly. The
CLI stays operational throughout. No big-bang rewrite.

---

## 2. Product target

The first release should feel like a focused desktop application, not a terminal embedded
in a window:

- Runs from source with `python furtag_gui.py`; packaged builds launch via `FurTag.app`,
  `FurTag.exe`, or a Linux desktop entry.
- Shows configured source/Hydrus availability before a scan.
- Credentials screen backed by the OS secure credential store, not a plaintext project file.
- Folder chosen via native picker or drag-and-drop.
- Every option toggleable in the UI, and saveable as a persistent default.
- Read-only inventory shown before work starts; only folder-specific questions after.
- Hash and perceptual tracks run in the background with separate progress, current-file
  status, elapsed time, and ETA.
- Optional human review of uncertain perceptual matches (§6.3).
- Visible rolling issue list plus a complete scrollable run log.
- Cancel, Scan Another Folder, Reveal Results, Quit.
- The guarded `NUKE!` workflow preserved as an explicit **Reset** button with preview and
  two confirmations.

**Explicit non-goals for v1:** no in-app tag editor, no thumbnail browser, no Hydrus
replacement, no account-creation flow, no automatic updater.

---

## 3. Architecture

Use **PySide6 (Qt for Python)**. This matches the Python/Qt desktop model Hydrus itself
runs on, and gives FurTag a mature cross-platform widget, dialog, drag-and-drop,
accessibility, and threading foundation. Avoid a web runtime or a JavaScript frontend.

The GUI stays a **thin adapter** over the existing Python engine.

**Threading rule — non-negotiable:** never call Qt widgets from worker threads. The Qt main
thread owns every widget. The pipeline runs on one coordinator thread and keeps its
existing internal hash/perceptual worker threads. Structured engine events reach the main
thread through Qt queued signals, or a small queue drained by a `QTimer`.

---

## 4. Why the frontend work is cheaper than it looks

Verified against the current source. Budget the phases accordingly:

- **`notify()` (`furtag.py:416`) is already the single chokepoint** for all 53 user-facing
  status messages, and already delegates to a swappable module-level `_display` (`:409`).
  `LiveDisplay` already exposes `log()` / `status()` / `freeze_total()`. A `QtObserver` is a
  genuine drop-in.
- **The engine is nearly prompt-free.** Of 12 `input()` sites, exactly **one** is reachable
  from engine execution: `prompt_for_pdf_dpi()` at `:2941`. The rest live in `main()`
  (`:3499–3511`) and already set plain attributes on the instance.
- **One `requests.Session`** at `:744`, shared read-only — a single injection seam for fakes.

The *configuration* work (§6) is the larger half of this project, not the Qt work.

---

## 5. Contracts

```python
@dataclass
class Settings:            # tier 2 — persistent, non-secret, settings.json
    output: OutputSettings
    hydrus: HydrusSettings
    sources: SourceSettings
    matching: MatchingSettings
    pdf: PdfSettings
    performance: PerformanceSettings

@dataclass
class RunOptions:          # tier 3 — per-run, seeded from Settings
    import_unmatched: bool
    result_page_limit: int
    build_already_tagged_page: bool
    sync_sidecars: bool
    pdf_dpi: Optional[int]
    # …plus a per-run override for any Settings field the user changes pre-scan

@dataclass
class ScanSummary:
    tagged: int
    unmatched: int
    duplicates: int
    pending_review: int
    source_hits: Dict[str, int]
    cancelled: bool = False

class RunObserver:
    def emit(self, event: "RunEvent") -> None: ...
```

Preserve the separation:

- Engine methods perform work and emit structured events.
- `TerminalObserver` renders events through `LiveDisplay` / `notify()`.
- `QtObserver` emits or queues events for the GUI.
- CLI prompt functions build `RunOptions`; GUI controls build the same object.
- `TagIntegrator.run(...)` **returns** a `ScanSummary` instead of printing one.

Avoid an early package/module split. Once both frontends work against these contracts,
moving engine code out of `furtag.py` is optional cleanup.

---

## 6. Configuration model

Three tiers. Every GUI control resolves through all three, in order.

| Tier | Store | Contains | Lifetime |
|---|---|---|---|
| 1 — Secrets | OS keyring, or `FURTAG_*` env vars | API keys, passwords, Hydrus access key | Until removed |
| 2 — Preferences | `settings.json` via `platformdirs` | Every toggle in §6.1 | Persistent default |
| 3 — Per-run | `RunOptions` in memory | Any tier-2 value the user overrides for this scan | One scan |

**Resolution:** tier 3 if set, else tier 2, else the shipped default (= today's behavior).

**"Save as default"** in each settings pane writes the current tier-3 values back to tier 2.
Every pane also has **Restore defaults**. Secrets never enter tier 2 or tier 3.

### 6.1 Settings inventory

Every row is a GUI control with a persistent default. "Today" shows where the value lives
now — module constant, `credentials.txt` key, interactive prompt, or nothing (new).

**Output sinks**

| Setting | Type | Today |
|---|---|---|
| `hydrus_enabled` — master Hydrus on/off | bool | *new* (implied by presence of URL+key) |
| `hydrus_import` — import files, or tag-only | bool | `credentials.txt` |
| `hydrus_import_unmatched` | bool | prompt |
| `hydrus_tag_service` — dropdown from live service list | choice | `credentials.txt` |
| `hydrus_tag_deleted_duplicates` | bool | `credentials.txt` (undocumented) |
| `sidecars_enabled` — write sidecars at all | bool | *new* (was implied by `hydrus_also_sidecars` + Hydrus state) |
| `sidecar_format` — `txt` \| `json` | choice | *new* |
| `sidecar_tag_filename` — pattern, default `{name}{ext}.txt` | string | hardcoded `:1819` |
| `sidecar_url_filename` — pattern, default `{name}{ext}.urls.txt` | string | hardcoded `:1823` |

**Hydrus review pages**

| Setting | Type | Today |
|---|---|---|
| `results_pages_enabled` — master | bool | `credentials.txt` |
| `new_imports_page_name` | string | `credentials.txt` |
| `newly_tagged_page_name` | string | `credentials.txt` |
| `already_tagged_page_name` | string | `credentials.txt` |
| `build_already_tagged_page` | bool | prompt |
| `result_page_limit` — 0 = unlimited | int | prompt + `credentials.txt` |

**Sources** — each independently toggleable, separate from whether credentials exist

| Setting | Type | Today |
|---|---|---|
| `e621_enabled` / `inkbunny_enabled` / `danbooru_enabled` / `gelbooru_enabled` | bool ×4 | *new* (only creds-presence) |
| `fluffle_enabled` / `saucenao_enabled` | bool ×2 | *new* |

**Matching thresholds**

| Setting | Type | Today |
|---|---|---|
| `saucenao_min_similarity` — accept SauceNAO's own tags above this | float % | `MIN_SIMILARITY = 80.0` (`:133`) |
| `saucenao_auth_similarity` — trust a booru-ID match enough to re-query | float % | `SAUCENAO_AUTH_SIMILARITY = 88.0` (`:134`) |
| `fluffle_accepted_matches` — which match classes count as an automatic hit | multi-choice | see §6.2 |
| `fluffle_tossup_e621_only` | bool | `FLUFFLE_TOSSUP_E621 = True` (`:140`) |
| `fluffle_review_mode` — send uncertain matches to human review | choice | *new*, see §6.3 |

**PDF**

| Setting | Type | Today |
|---|---|---|
| `pdf_enabled` — render PDFs at all | bool | *new* |
| `pdf_dpi` default + 300/600/custom presets | int | prompt, `PDF_DPI` (`:145`) |
| `pdf_write_sidecars` — emit `comic:`/`page:` base tags | bool | `convert_pdf(write_sidecars=)` arg |

**Performance** — advanced pane, with floors and a warning banner

| Setting | Type | Today |
|---|---|---|
| Per-service pacing intervals ×6 | float s | constants `:123–128` |
| Hash worker count | int | derived |

**Maintenance**

| Setting | Type | Today |
|---|---|---|
| **Reset** (the `NUKE!` workflow) as a button | action | typed command `:3237` |

### 6.2 Fluffle has no similarity percentage

Worth stating plainly because it changes the control type. `find_best_exact_match()`
(`:1512–1535`) reads a categorical `result["match"]` field — `exact`, `tossUp`,
`alternative`, `unlikely` — and there is no score to threshold. So:

- **SauceNAO** gets two percentage sliders (`min` and `auth`), validated `auth >= min`.
- **Fluffle** gets checkboxes for which match classes constitute an automatic hit, plus the
  existing "accept `tossUp` only on e621" guard — because a `tossUp` on e621 is re-queried
  by post ID (`e621_lookup_by_id()`), which is what makes a near-miss low-risk there and
  not elsewhere.

Presenting Fluffle as a slider would be a lie about what the API returns. Keep the two
panes visually distinct so it doesn't read as an inconsistency.

### 6.3 Human approval for uncertain matches

Today `find_best_exact_match()` auto-accepts a `tossUp` only on e621 (`:1534`) and
**silently discards** every other `tossUp`, `alternative`, and `unlikely`. Those discarded
candidates are often correct — there is just no safe way to confirm them automatically. A
review queue makes them recoverable.

**Setting:** `fluffle_review_mode` — `off` · `review tossUps` · `review tossUps +
alternatives`. Default **off**, preserving today's behavior exactly.

**Deferred, never inline.** The perceptual tier is strictly sequential and rate-limited
(`FLUFFLE_INTERVAL = 1.2`, one concurrent Fluffle request per client). Blocking on a human
decision per file would stall the pipeline and idle every pacer. Instead:

1. A candidate in the review band produces a `PendingReview` record, and the pipeline
   **continues immediately**.
2. No results are written and no final ledger status is set for that file.
3. The GUI shows a live "Needs review — N" badge. The user reviews during or after the run.
4. **Approve** → run the enrichment path already in `perceptual_tier` (`:2879–2889`):
   `e621_lookup_by_id(pid)`, falling back to `e621_lookup_by_md5(md5_u)`, then
   `write_results()`. **Reject** → record `nomatch`.

**Ledger correctness — the load-bearing detail.** A file awaiting review must not be
recorded `matched` or `nomatch`. Recording `nomatch` means `index()` rules it out on every
future run and the pending review is lost permanently. Add a `pending_review` ledger status
that `index()` treats as **unresolved** (the file stays eligible) and that **blocks
directory fingerprint sealing** — consistent with the existing rule that a directory seals
only once every current media file is resolved. Persist the queue itself so decisions
survive an app restart.

**What this unlocks.** Non-e621 `tossUp` is rejected outright today because there is no safe
re-query path. With a human confirming identity that reasoning no longer applies — but
authoritative enrichment exists only for e621/Danbooru/Gelbooru, so an approved non-e621
match yields Fluffle's own thinner tags plus the source URL. Say so in the UI, so the
tag-quality difference isn't a surprise.

**Review UI.**

- Reuse `_prepare_thumb()` (`:1116`, already produces a 256px PNG via `THUMB_MAX` for the
  Fluffle upload) for the local-image pane. No new imaging code needed.
- Show the candidate's `platform`, `location`, and match class from the Fluffle result.
- **Do not auto-fetch remote preview images.** No `Pacer` covers arbitrary booru CDNs, so
  bulk-fetching previews would hammer hosts we otherwise pace carefully. Show the candidate
  URL as a clickable link; make any remote thumbnail fetch explicit, lazy, and paced.
- Bulk approve/reject plus keyboard-driven single-item review.

**Generalize the queue, populate it narrowly.** The same mechanism can serve SauceNAO
results scoring between a new `review_floor` and `saucenao_min_similarity` — one queue, two
producers. Design it generically; ship v1 with only Fluffle populating it.

**CLI parity.** A post-run review loop over the same queue, plus a non-interactive path that
leaves items `pending_review` (not `nomatch`) so a later GUI session can resolve them.

### 6.4 Engineering notes — the risky parts

**Sidecar format and filename are the highest-risk change in this document.**
`tag_sidecar_path()` (`:1819`) and `has_sidecar()` (`:1826`) are load-bearing for `index()`
skip logic — they decide which files are considered already done. Changing format or name
changes what gets re-scanned. Requirements:

- Detection must recognize **legacy `.txt` sidecars regardless of the current format
  setting**, or switching to JSON silently re-scans and re-tags the entire library.
- Sidecar sync (`sync_sidecars_to_hydrus`) must read both formats.
- PDF base-tag sidecars (`comic:`/`page:`) must be written in the configured format —
  perceptual tags append to that same file, so a format mismatch splits a page's tags.
- JSON should be **one file holding both tags and URLs** (`{"tags": [], "urls": []}`),
  replacing the two-file split. The reader must handle both shapes.
- Filename patterns need a validator: no path separators, must contain `{ext}`, must not
  resolve to the media file itself. The `{name}{ext}.txt` scheme (rather than
  extension-stripped) exists specifically so `cat.jpg` and `cat.png` don't collide — a
  pattern omitting `{ext}` reintroduces that bug and must be rejected, not warned about.

**Service toggles need an `available` vs `enabled` split.** Today `has_e621`, `has_inkbunny`,
`has_danbooru`, `has_gelbooru`, `has_saucenao` (`:764–787`) conflate "credentials present"
with "should be used." Introduce a separate user-facing `enabled_*`, and make
`enabled_hash_services()` (`:1102`) and `any_source()` (`:1098`) the single seams that
combine them. The perceptual gates at `:1572`, `:2883`, and `:2893` also consult it. The UI
must distinguish *unavailable* (no credentials — greyed with a reason) from *disabled by you*
(available, switched off).

**Thresholds are module constants read at call sites** — `:1534`, `:1562`, `:1603` reference
`MIN_SIMILARITY`, `SAUCENAO_AUTH_SIMILARITY`, and `FLUFFLE_TOSSUP_E621` directly. They must
become instance/settings values, or the GUI cannot change them at runtime. This is engine
work (Phase 1), not GUI work.

**Validation must catch self-defeating combinations** before a scan starts, with a clear
message rather than a silent no-op run:

- Hydrus disabled **and** sidecars disabled → no output sink; results would be computed and
  discarded. Refuse to start.
- All sources disabled → nothing can match. Refuse to start.
- `saucenao_auth_similarity < saucenao_min_similarity` → incoherent gate. Clamp and warn.
- PDF disabled is fine (PDFs are simply skipped), but say so in the pre-scan inventory.

**Rate-limit editing needs guardrails.** These intervals reflect documented or observed API
limits; lowering them gets users banned, and e621's is a hard cap. Put them behind an
Advanced disclosure, enforce a per-service floor, and show a warning banner when any value
is below the shipped default.

**Reset (`NUKE!`) as a button** keeps every existing guard: exact target discovery,
filesystem-root refusal, preview counts before anything is removed, source-PDF protection,
a separate confirmation for rendered PDF pages, and safe removal of an emptied page folder.
Two confirmations, the second typed, and it must be disabled entirely while a scan runs.

**Documentation drift to fix while here:** the code has `MIN_SIMILARITY = 80.0` and
`SAUCENAO_AUTH_SIMILARITY = 88.0` (`:133–134`), but `AGENTS.md` and `CLAUDE.md` both still
document 70 and 80. These are about to become user-visible defaults, so correct the docs
before they mislead someone tuning them.

---

## 7. Suggested UI flow

1. **Launch** — load settings and credentials, show source status (available / disabled /
   unavailable-with-reason).
2. **Choose folder** — native picker plus drag-and-drop; discovery/indexing with no network
   mutations.
3. **Review** — media / to-process / already-checked / PDF counts; per-run overrides for
   anything folder-specific; validation failures block Start here.
4. **Run** — settings, folder changes, Reset, and a second Start all disabled; independent
   Hash and Perceptual cards, Recent Issues, and the Needs-review badge.
5. **Finish** — `ScanSummary`; controls re-enabled; pending reviews resolvable; Scan Another
   Folder and Reveal in File Manager.

Settings live in a tabbed panel matching §6.1's groups: Output · Hydrus · Sources ·
Matching · PDF · Advanced · Maintenance.

---

## 8. Phases

### Phase 0 — protect the baseline

- Unit tests for: ledger skip rules, exact deduplication, Hydrus result routing,
  deleted-file duplicate-group tagging, sidecar sync, SauceNAO quota exhaustion.
- **Plus, for the configuration work:** sidecar detection across `.txt`/JSON/custom patterns,
  legacy-`.txt` recognition when format is JSON, filename-pattern validator rejection cases,
  per-service disable paths, threshold boundary behavior at exactly `min` and `auth`, and
  `pending_review` ledger semantics (stays eligible, blocks fingerprint sealing).
- Request/session fakes injected at `:744`. Tests must never call live booru or Hydrus APIs.
- Record a fixture run of non-TTY output **before** any display refactoring.
- Keep `credentials.txt`, ledgers, sidecars, and generated PDF pages out of fixtures unless
  created inside a temporary directory.

### Phase 1 — make the engine frontend-neutral and settings-driven

- Introduce `Settings`, `RunOptions`, `ScanSummary`, structured progress events, and a
  cancellation `threading.Event`.
- **Move module constants to instance settings**: the two SauceNAO thresholds,
  `FLUFFLE_TOSSUP_E621`, pacing intervals, `PDF_DPI`. Constants remain as shipped defaults,
  but call sites must read instance state.
- **Split `available` from `enabled`** for all six sources; route everything through
  `enabled_hash_services()` (`:1102`) and `any_source()` (`:1098`).
- **Add the review queue and the `pending_review` ledger status** (§6.3), including
  persistence and the fingerprint-sealing block. Engine-side only in this phase — the CLI
  post-run loop is the first consumer.
- Route pipeline status through the observer, retaining `notify()` as the terminal adapter.
- **Split `run()` into discovery / execution / finalization.** This is a *prerequisite for
  cancellation*, not merely a convenience for the inventory screen. `run()` is one ~300-line
  method (`:2920–3213`) owning four thread pools — hash (`:2296`), SHA-256 (`:2676`), PDF
  render (`:2943`), hash-tier (`:3105`) — plus a queue-driven perceptual worker joined in a
  `finally`. Sprinkling `Event` checks through a method already at the edge of reviewable is
  how this goes wrong.
- Move the summary out of the terminal print block at `:3199–3213` into `ScanSummary` +
  `TerminalObserver`. That block is currently the counters' only consumer.
- Replace the one in-engine `input()` (`:2941`) with a value on `RunOptions`.
- **Cancellation must be cooperative:** stop scheduling new files, let in-flight requests
  finish their existing timeouts, join workers, flush result pages, finalize safe directory
  fingerprints, save all ledgers.

### Phase 2 — build the GUI and the settings layer

- `furtag_gui.py` as the PySide6 entry point, running from a normal venv on every platform.
- **`SettingsStore`**: load/save `settings.json` via `platformdirs`, schema-versioned with a
  `version` field and a forward-compatible loader that ignores unknown keys and fills
  missing ones with defaults. Never store secrets.
- Main window plus the tabbed settings panel from §7, with **Save as default** and
  **Restore defaults** per pane.
- **Sidecar format/filename support** (§6.4), including the dual-format reader and the
  pattern validator.
- **Review pane** for the pending queue (§6.3): local thumbnail, candidate metadata, link
  out, bulk and keyboard-driven decisions, live badge during a run.
- **Reset button** with preview and two confirmations, disabled during a run.
- Pre-scan validation blocking the self-defeating combinations in §6.4.
- Modal dialogs **only** for destructive Reset, fatal credential failure, and folder-specific
  PDF quality.
- Window close during a run requests cancellation, shows "Finishing current request…", and
  closes only after cleanup completes.

### Phase 3 — secure credentials

- Use `keyring` so secrets go to macOS Keychain, Windows Credential Locker, or a supported
  Linux Secret Service / KWallet backend. Do not invent an encryption format or keep a local
  encryption key beside encrypted secrets.
- **Support an environment-variable credential source** (`FURTAG_E621_API_KEY`,
  `FURTAG_HYDRUS_ACCESS_KEY`, …) as a first-class input beside keyring, checked in a
  documented order. Required, not optional: `keyring` needs an unlocked, session-attached
  secret store, and a headless Linux box or Hydrus-on-NAS install — a very plausible FurTag
  deployment, since the Hydrus API sink exists precisely to drive a remote client — has no
  Secret Service. Env vars are not a plaintext file on disk, so the security goal holds, and
  this is the standard answer for headless credential supply.
- Store secrets as keyring items under a stable service name such as `org.furtag.FurTag`.
- Credentials window: one section per service, masked fields, Save/Update/Remove, and a
  connection test. Empty fields mark that source *unavailable*, distinct from *disabled*.
- **Never** put secrets in progress events, logs, exception text, screenshots, command-line
  arguments, crash reports, or support bundles. Redact headers and URLs that may carry keys.
- Do **not** detect, request, create, document, or expose an import button for
  `credentials.txt` in the released application.
- The owner's existing `credentials.txt` is a private one-time migration: build a temporary
  local helper, run it explicitly, verify the imported values, and remove the helper before
  the first distributable build. It must not ship anywhere.
- Refactor GUI and CLI credential loading behind one keyring-backed `CredentialStore`.
  Preserve plaintext parsing only until the private migration completes, then remove it.
- If no keyring backend exists **and** no environment variables are set, show a clear
  disabled/error state. Never silently fall back to writing a new plaintext file. A future
  encrypted local vault requires a user-supplied master password and a reviewed KDF design;
  not part of v1.

### Phase 4 — cross-platform packaging

- PyInstaller specs/build scripts for macOS, Windows, and Linux: GUI entry point, icons, Qt
  plugins, PyMuPDF/Pillow hidden imports if needed, no console window.
- Build natively on each target OS; do not assume one machine can cross-compile reliable
  artifacts for the other two.
- Test on clean user accounts with no project venv and no developer packages.
- Store mutable settings beside neither the executable nor its install directory — always the
  platformdirs location.
- **Do macOS signing/notarization *before* validating credential persistence.** The Keychain
  ACL keys to the **code signature identity**, not the bundle identifier, so an unsigned or
  ad-hoc-signed build changes identity every rebuild and structurally cannot demonstrate that
  saved credentials survive an update. Windows code-signing follows the same logic. "Works
  from the repo" is not packaging verification.

### Phase 5 — optional Homebrew cask

- A cask distributes the finished `.app`; it is not part of the GUI runtime.
- macOS-only; Windows/Linux releases remain independent artifacts.
- Publish a versioned, immutable release archive with a SHA-256 checksum.
- Personal tap first. Pursue a shared tap only after releases, signing, notarization,
  versioning, and download URLs are stable.

---

## 9. Safety and behavior invariants

- Never mutate widgets outside the Qt main thread.
- **Never allow two scans to run concurrently in one process.** Engine-required, not just UI
  hygiene: `run()` clears `hydrus_result_pages` at entry (`:2926`) and the engine carries
  process-level mutable state (`_pool_cache`, `ib_sid`, `danbooru_anon`). Overlapping scans
  corrupt results, not merely the display.
- Every new setting defaults to today's behavior. A fresh install with an untouched settings
  file must be equivalent to the current CLI.
- Preserve per-service `Pacer` behavior and the invariant that one service is never called
  concurrently with itself — including under user-edited intervals.
- Preserve atomic ledger saves and interruption recovery.
- Preserve the directory fingerprint rule: seal only when every current media file is
  resolved. A `pending_review` file is **not** resolved.
- A settings change must never silently invalidate the ledger or trigger a full re-scan.
  Sidecar-format changes in particular must keep recognizing legacy sidecars.
- Human review is advisory on *identity*, not a bypass of the tag pipeline: an approved match
  still goes through the same authoritative-enrichment path as an automatic hit.
- Reset keeps its exact target discovery, filesystem-root refusal, preview counts,
  source-PDF protection, and separate rendered-PDF confirmation.
- Secrets live in keyring or env vars, never enter logs or events, and never fall back to new
  plaintext storage without explicit user action.
- Existing tag sidecars map only to Hydrus tags; URL sidecar entries map only to Hydrus URLs.
  A sidecar sync must not change ledgers or repeat online searches.
- For a known-deleted Hydrus file, only relationship type `8` (duplicates) may receive
  metadata. Type `3` (alternates) stays excluded.
- SauceNAO daily exhaustion disables it for the rest of the session; the UI shows that state
  once, without repeating alerts.
- A GUI error must not strand the terminal display, worker threads, temporary ledger files,
  or half-flushed Hydrus result pages.

---

## 10. Acceptance criteria for the first GUI release

- `./FurTag.command` still runs the CLI successfully.
- `python furtag_gui.py` launches the same GUI from source on macOS, Windows, and Linux.
- The GUI can scan a folder, process a mixed image/video/PDF set, cancel safely, and scan
  another folder without restarting.
- GUI and CLI runs produce equivalent sidecars, ledger records, Hydrus tags, URLs, duplicate
  handling, and result-page membership for the same options.
- **Every setting in §6.1 is reachable from the UI, persists across an app restart, and can
  be restored to its default.**
- A fresh install with no `settings.json` reproduces current CLI behavior exactly — including
  `fluffle_review_mode = off`.
- Switching `sidecar_format` to JSON does **not** cause previously-tagged files to be
  re-scanned or re-tagged.
- Disabling an individual source removes it from the run without disturbing the others'
  pacing or results; disabling Hydrus entirely falls back cleanly to sidecars.
- Hydrus off + sidecars off is refused before the scan starts, with a clear message.
- Adjusted SauceNAO thresholds and Fluffle match classes measurably change accept/reject
  behavior on a fixture set.
- With review enabled, uncertain matches queue without stalling the pipeline; pending items
  survive an app restart; approving writes the same tags an automatic hit would have; and a
  rejected item is recorded `nomatch` and skipped on re-run.
- A file left `pending_review` is re-offered on the next run and does not seal its directory
  fingerprint.
- Reset from the button performs exactly what the typed `NUKE!` command does today, with no
  path reachable in one confirmation.
- Progress remains responsive while hashing, waiting on rate limits, rendering PDFs, and
  making network requests.
- Closing during a run saves resumable progress and leaves no live worker.
- Packaged builds launch on clean macOS, Windows, and Linux environments without a console
  window.
- Credentials stay absent from `settings.json` and logs, and can be removed from the GUI.
  **Persistence across app updates is validated on the signed build** (Phase 4); it is not
  demonstrable on unsigned artifacts.
- The CLI can authenticate on a headless machine with no keyring backend, using environment
  variables alone.
- Fresh-install UI and docs never instruct users to create or import a `credentials.txt`,
  and release artifacts contain no legacy importer.
- The owner's private migration is verified before the plaintext loader and helper are removed.
- Automated tests and `python -m py_compile` pass before packaging.

---

## 11. Housekeeping (flagged, not scheduled)

`CLAUDE.md` has drifted stale against `AGENTS.md`: it still documents `expand_pdfs()` (now
`plan_pdf_renders()` / `render_pdf_jobs()`), describes the older single-results-page
credential scheme, and omits exact deduplication and `duplicates.log`. Both files also carry
the wrong similarity thresholds (§6.4). Two near-identical ~300-line instruction files will
diverge further once a GUI lands — and this plan makes three. Worth collapsing one into a
pointer at the other before then.
