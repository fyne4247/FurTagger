# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## What This Is

FurTag is a single Python script (`furtag.py`) that reverse-image-searches media files against furry/booru services and writes the retrieved metadata into Hydrus Network — either via the **Client API** (import + tags + URLs, no sidecars) or as classic Hydrus-compatible sidecar files. It is the consolidation of four earlier iterations (now deleted).

This repository is the GUI branch of FurTag. The current script is the working
behavioral baseline; the objective is a cross-platform Python desktop GUI in
the same spirit as Hydrus. It must remain runnable from source on macOS,
Windows, and Linux, with optional native packages for each platform, without
changing FurTag's matching, ledger, duplicate, rate-limit, or Hydrus semantics.

## GUI Implementation Plan

### Product target

The first release should feel like a focused desktop application, not a terminal
embedded in a window:

- Run from source with `python furtag_gui.py`; packaged builds may launch through
  `FurTag.app`, `FurTag.exe`, or a Linux desktop entry.
- Show configured source/Hydrus availability before a scan.
- Provide a Credentials screen that stores API keys and passwords through the
  operating system's secure credential store rather than a plaintext project
  file.
- Choose a folder with a native folder picker or drag-and-drop.
- Set session-wide Hydrus options once; keep them until the app closes.
- Show the read-only inventory before work starts, then ask only
  folder-specific questions such as PDF render quality and sidecar sync.
- Run the hash and perceptual tracks in the background with separate progress,
  current-file status, elapsed time, and ETA.
- Keep a visible rolling issue list plus a complete scrollable run log.
- Support Cancel, Scan Another Folder, Reveal Results, and Quit.
- Preserve the guarded `NUKE!` workflow as an explicitly named Reset action
  with a preview and two confirmations.

The initial GUI does not need an in-app tag editor, thumbnail browser, Hydrus
replacement, account-creation flow, or automatic updater.

### Architectural direction

Use **PySide6 (Qt for Python)**. This matches the Python/Qt desktop model used
by applications such as Hydrus and gives FurTag a mature cross-platform widget,
dialog, drag-and-drop, accessibility, and threading foundation. The larger
dependency is acceptable; avoid a web runtime or a separate JavaScript frontend.
The GUI should remain a thin adapter over the existing Python engine.

Do not call Qt widgets from worker threads. The Qt main thread owns every
widget. The pipeline runs on one coordinator thread and keeps its existing
internal hash/perceptual worker threads. Deliver structured engine events to the
main thread through Qt queued signals (or a small queue drained by a `QTimer`).

Refactor toward these contracts:

```python
@dataclass
class RunOptions:
    import_unmatched: bool
    result_page_limit: int
    build_already_tagged_page: bool
    sync_sidecars: bool
    pdf_dpi: Optional[int]

@dataclass
class ScanSummary:
    tagged: int
    unmatched: int
    duplicates: int
    source_hits: Dict[str, int]
    cancelled: bool = False

class RunObserver:
    def emit(self, event: "RunEvent") -> None: ...
```

Exact class names can change, but preserve the separation:

- Engine methods perform work and emit structured events.
- `TerminalObserver` renders events through `LiveDisplay`/`notify()`.
- `QtObserver` emits/queues events for the GUI.
- CLI prompt functions build `RunOptions`; GUI controls build the same object.
- `TagIntegrator.run(...)` returns a summary instead of making the UI scrape
  terminal text.

Keep the CLI operational throughout the conversion. Avoid a big-bang rewrite
or an early package/module split. Once both frontends work against the same
contracts, moving engine code out of `furtag.py` is optional cleanup.

### Suggested UI flow

1. **Launch**
   - Load credentials/config and show source status.
   - Show the persistent session options in one compact settings area.
2. **Choose folder**
   - Native directory picker and file-manager drag-and-drop target.
   - Run discovery/indexing without network mutations.
3. **Review**
   - Show media/to-process/already-checked/PDF counts.
   - Offer sidecar-to-Hydrus sync and PDF DPI only when relevant.
4. **Run**
   - Disable settings, folder changes, Reset, and a second Start.
   - Show independent Hash and Perceptual cards plus Recent Issues.
5. **Finish**
   - Show `ScanSummary`.
   - Re-enable controls and offer Scan Another Folder / Reveal in File Manager.

### Implementation phases

#### Phase 0 — protect the baseline

- Add focused unit tests for ledger skip rules, exact deduplication, Hydrus
  result routing, deleted-file duplicate-group tagging, sidecar sync, and
  SauceNAO quota exhaustion.
- Add request/session fakes; tests must never call live booru or Hydrus APIs.
- Record a small fixture run for non-TTY output before display refactoring.
- Keep `credentials.txt`, ledgers, sidecars, and generated PDF pages out of test
  fixtures unless created inside a temporary directory.

#### Phase 1 — make the engine frontend-neutral

- Introduce `RunOptions`, `ScanSummary`, structured progress events, and a
  cancellation `threading.Event`.
- Route pipeline status through the observer while retaining `notify()` as the
  terminal adapter.
- Split discovery from execution so the GUI can present inventory before Start.
- Replace `input()` calls inside engine execution with values in `RunOptions`.
- Cancellation must be cooperative: stop scheduling new files, allow in-flight
  requests to finish their existing timeouts, join workers, flush result pages,
  finalize safe directory fingerprints, and save all ledgers.

#### Phase 2 — build the GUI

- Add `furtag_gui.py` as the PySide6 entry point; it must run directly from a
  normal Python virtual environment on every supported platform.
- Build one main window with source status, folder selection, session settings,
  scan summary, two progress tracks, issues, and action buttons.
- Use modal dialogs only for destructive Reset confirmation, credentials
  failures that prevent all useful work, and folder-specific PDF quality.
- Use `platformdirs` for non-secret settings (`settings.json`) so each operating
  system gets its conventional per-user application-data directory. Never copy
  API secrets there.
- Window close during a run should request cancellation, show “Finishing current
  request…”, and close only after cleanup completes.

#### Phase 3 — secure credentials

- Use Python's `keyring` package so secrets go to macOS Keychain, Windows
  Credential Locker, or a supported Linux Secret Service/KWallet backend. Do
  not invent an encryption format or keep a local encryption key beside
  encrypted secrets.
- Store usernames and harmless preferences in the platform settings directory
  if useful, but store API keys, Hydrus access keys, and the InkBunny password
  as keyring items under a stable service name such as `org.furtag.FurTag`.
- Add a Credentials window with one section per service, masked secret fields,
  Save/Update/Remove actions, and a connection/status test. Empty fields disable
  that source just as missing keys do today.
- Never put secrets in progress events, logs, exception text, screenshots,
  command-line arguments, crash reports, or generated support bundles. Redact
  request headers and URLs that may contain keys.
- Do **not** detect, request, create, document, or expose an import button for
  `credentials.txt` in the released application. New users enter credentials
  directly into the secure Credentials window and never need a plaintext file.
- The repository owner's existing `credentials.txt` is a private, one-time
  migration only. During development, make a temporary local migration helper,
  run it explicitly on the owner's machine, verify the imported values, and
  remove the helper before the first distributable build. It must not ship in
  the GUI, packaged artifacts, normal CLI, or public setup documentation.
- Refactor GUI and CLI credential loading behind the same keyring-backed
  `CredentialStore`; the released CLI should not require plaintext credentials
  either. Preserve plaintext parsing only long enough to complete the private
  migration, then remove that path.
- If no usable keyring backend exists or access is denied, show a clear
  disabled/error state. Never silently fall back to saving a new plaintext
  credentials file. A future encrypted local vault may be added only with a
  user-supplied master password and a reviewed password-based key derivation
  design; it is not part of the first release.
- Use stable application/bundle identifiers so credential access remains
  predictable across app updates. Test secure storage both from source and from
  packaged builds on all supported operating systems.

#### Phase 4 — cross-platform packaging

- Add PyInstaller specs/build scripts for macOS, Windows, and Linux with the GUI
  entry point, icons, Qt plugins, PyMuPDF/Pillow hidden imports if needed, and
  no console window.
- Build natively on each target OS; do not assume one machine can cross-compile
  reliable artifacts for the other two.
- Test on clean user accounts with no project venv and no developer packages.
- Store mutable settings beside neither the executable nor its installation
  directory; always use the platformdirs location.
- Add macOS signing/notarization and Windows code-signing after unsigned builds
  are stable. Do not treat “works from the repo” as packaging verification.

#### Phase 5 — optional Homebrew cask

- A cask distributes the finished `.app`; it is not part of the GUI runtime.
- It is only the macOS distribution option; Windows/Linux releases remain
  independent artifacts.
- Publish a versioned, immutable release archive with a SHA-256 checksum.
- Put the cask in a personal tap first.
- Only pursue inclusion in a shared/public tap after releases, signing,
  notarization, versioning, and download URLs are stable.

### Safety and behavior invariants

- Never mutate widgets outside the Qt main thread.
- Never allow two scans to run concurrently in one process.
- Preserve per-service `Pacer` behavior and the invariant that one service is
  not called concurrently with itself.
- Preserve atomic ledger saves and interruption recovery.
- Preserve the directory fingerprint rule: seal only when every current media
  file is resolved.
- Reset must keep the existing exact target discovery, filesystem-root refusal,
  preview counts, source-PDF protection, and separate rendered-PDF confirmation.
- Secrets must live in an OS-backed keyring in the GUI flow, must never enter
  logs/events, and must never fall back to new plaintext storage without
  explicit user action.
- Existing tag sidecars map only to Hydrus tags; `.urls.txt` entries map only to
  Hydrus URLs. A sidecar sync must not change ledgers or repeat online searches.
- For a known-deleted Hydrus file, only relationship type `8` (duplicates) may
  receive metadata. Relationship type `3` (alternates) must remain excluded.
- SauceNAO daily exhaustion disables it for the rest of the application
  session; the UI should show that state once without repeating alerts.
- A GUI error must not strand the terminal display, worker threads, temporary
  ledger files, or half-flushed Hydrus result pages.

### Acceptance criteria for the first GUI release

- `./FurTag.command` still runs the CLI successfully.
- `python furtag_gui.py` launches the same GUI from source on macOS, Windows,
  and Linux.
- The GUI can scan a folder, process a mixed image/video/PDF set, cancel safely,
  and scan another folder without restarting.
- GUI and CLI runs produce equivalent sidecars, ledger records, Hydrus tags,
  URLs, duplicate handling, and result-page membership for the same options.
- Progress remains responsive while hashing, waiting on rate limits, rendering
  PDFs, and making network requests.
- Closing during a run saves resumable progress and leaves no live worker.
- Packaged GUI builds launch on clean macOS, Windows, and Linux environments
  without opening a terminal/console window.
- Credentials saved by the packaged app survive a normal app update, remain
  absent from platform settings files and logs, and can be removed from the GUI.
- Fresh-install UI and documentation never instruct users to create or import a
  `credentials.txt` file, and release artifacts contain no legacy importer.
- The owner's private migration is verified before the plaintext loader and
  temporary helper are removed.
- Automated tests and `python -m py_compile` pass before packaging.

## Running It

```bash
./FurTag.command         # double-clickable; bootstraps .venv + deps on first run
# or manually:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python furtag.py
```

`FurTag.command` is the intended entry point — it runs `furtag.py` inside the project venv (system `python3` lacks the deps). On launch it (re)installs deps whenever `requirements.txt` is newer than `.venv/.deps-stamp`, so adding a dependency doesn't require deleting the venv. It prompts for a folder; a blank entry defaults to the current directory. Files that already have a tag sidecar **or** are recorded in the session ledger (see below) are skipped.

`prompt_for_folder()` handles the folder prompt: it accepts **Finder drag-and-drop paths** (`_unescape_path()` runs the input through `shlex.split` to undo the backslash/quote escaping Terminal inserts), **re-prompts** on an invalid path instead of exiting, and quits cleanly on `q`/`quit`/`exit`, Ctrl+C, or Ctrl+D. Typing exact command `NUKE!` enters `_prompt_for_nuke()`: it asks for a target, previews recursive ledger/report/sidecar counts, requires `ARE YOU SURE? [y/N]` with exact `y`, then separately asks whether precisely named PDF page PNGs should also be removed for re-export. It never deletes source PDFs or arbitrary output-folder contents, removes an emptied page folder only when safe, refuses a filesystem root, and returns the reset folder so the same launch immediately rescans it.

### Terminal display

`LiveDisplay` renders an in-place panel (previous / current / next file, a **phase label**, and a bottom progress bar with elapsed/ETA). The current line carries a live sub-status: during the hash tier it shows each site ticking off (`hash ▸ e621 ✓  ib ·  dan ✓  gel …` — ✓ hit, ✗/· miss, … in-flight); during perceptual it names the engine and any enrichment step. It is **thread-safe** (a lock guards every write) because the hash tier renders from a worker pool. All warnings/errors go through the module-level **`notify()`** (not `print`), which routes them into a three-item rolling **Recent issues** history inside the live panel; redirected/non-TTY output still retains every issue as a normal line. The engine itself never calls a `LiveDisplay` method: every progress point is a `RunEvent` on the run's single observer, and `TerminalObserver` is what drives the panel (a GUI installs `QtObserver` instead). `notify()` emits an `issue` event to the **active observer** (`furtag.set_active_observer()`), so the same call sites feed the CLI panel's rolling **Recent issues** history and the GUI's issue pane. When stdout isn't a TTY it degrades to one line per file. **Use `notify()` for any user-facing status message inside the processing loop** — a raw `print` would corrupt the panel.

## Output

**Preferred:** Hydrus Client API (`has_hydrus`) — import file, add tags to `hydrus_tag_service` (default `downloader tags`), associate source URLs. No sidecar files.

**Fallback / dual-write:** sidecars when the API is off, or when `hydrus_also_sidecars = true`:

- `<file>.<ext>.txt` — tags, one per line
- `<file>.<ext>.urls.txt` — source URLs, one per line

The `<file>.<ext>.txt` suffix scheme (not extension-stripped `<file>.txt`) is deliberate: it avoids collisions when a folder holds `cat.jpg` and `cat.png`.

## Credentials

A single `credentials.txt` alongside the script (`key = value`, one per line). Any missing/incomplete key disables that source instead of crashing. Keys:

```
e621_username     e621_api_key
inkbunny_username inkbunny_password
danbooru_username danbooru_api_key
gelbooru_user_id  gelbooru_api_key
sauce_nao_api_key

# optional Hydrus Client API sink (no sidecars when on):
hydrus_api_url       hydrus_access_key
hydrus_tag_service   (= "downloader tags" by default)
hydrus_import        (true/false, default true)
hydrus_also_sidecars (true/false, default false)
hydrus_results_page  (name/on/off, default on — master toggle for the result pages below)
hydrus_new_imports_page      (= "FurTag New Imports" — brand-new imports this run)
hydrus_newly_tagged_page     (= "FurTag Newly Tagged" — already in Hydrus, newly tagged)
hydrus_duplicate_tagged_page (= "FurTag Duplicate Tagged" — current duplicate-group
                              members tagged for a previously-deleted file)
hydrus_already_tagged_page   (= "Already Tagged" — ledger-history review page; false disables)
```

**Non-secret keys only apply on the explicit `load_credentials(creds=<path>)` path.** The preferred keyring/store path (`load_credentials_from_store()`, which the GUI uses) merges **only** the secret/identity fields in `furtag_credentials.ALL_FIELDS` from a legacy `credentials.txt`; every non-secret preference above (`hydrus_import`, `hydrus_tag_service`, the page names, `hydrus_results_page`, …) then comes from `Settings`, and ignored keys are reported once at startup. This is deliberate: a stale `credentials.txt` used to silently override what the user had just set in the Settings tab.

For InkBunny, enable API access **and** adult ratings in account settings, or explicit results stay hidden. `load_credentials()` reads the file once into a dict and the `_init_<source>` helpers each pull their keys. `_init_hydrus()` verifies the access key and resolves the tag service name → `service_key`; on failure FurTag keeps writing sidecars.

### Hydrus Client API output

When `has_hydrus` is true, `write_results()` calls `_hydrus_push()` instead of (or in addition to, if `hydrus_also_sidecars`) writing `.txt` sidecars:

1. `POST /add_files/add_file` with `{path}` when `hydrus_import` (status 1/2 → hash)
2. `POST /add_tags/add_tags` with `service_keys_to_tags` and `override_previously_deleted_mappings: false` (downloader-like)
3. `POST /add_urls/associate_url` with `urls_to_add`
4. `_hydrus_add_to_page(kind, hash)` records the hash on one of three rolling newest-N lists in `self.hydrus_result_pages` — `new` (import status 1), `updated` (status 2 / tag-only), `duplicates` (a current duplicate-group member tagged by `_hydrus_push_to_deleted_duplicates()` for a known-deleted file, added only after its `add_tags` succeeded). At end of run `_hydrus_flush_result_pages()` builds each non-empty page once through the shared `_hydrus_create_hash_page()` (unfocused, `system_hash_locked`, filled in `HYDRUS_PAGE_BATCH` chunks). All three share the `hydrus_results_page` master toggle and the Manage Pages permission check; a per-page failure disables just that page.

The interactive `hydrus_import_unmatched` run choice calls `write_unmatched()` for final perceptual misses and hash-only video misses. It also imports unchanged prior `nomatch` records lacking cached SHA-256, then caches Hydrus's returned hash so subsequent runs do not repeat the import.

At startup, unchanged files whose per-directory ledger status is `matched` can be collected on a separate unfocused `Already Tagged` hash-locked page. The prompt is suppressed unless at least one valid unchanged matched record exists. Older ledgers only contain MD5, so FurTag computes the SHA-256 values in parallel once and caches them back into those records; subsequent runs reuse the cache. Page submissions are batched in groups of 256.

Pushes are serialised with `_hydrus_lock` because the hash tier and perceptual worker can both call `write_results`. PDF pages still get `comic:`/`page:` via `_pdf_page_base_tags()` even when convert sidecars are skipped.

## Pipeline (the core architecture)

`TagIntegrator.run()` drives four stages (preceded by a PDF pre-pass):

- **PDF background render** (`plan_pdf_renders()` → `render_pdf_jobs()`): discover PDFs and existing page folders first, show a numbered menu for standard 300 DPI, archival 600 DPI, or custom 72–2400 DPI, then render unconverted PDFs as lossless PNGs on one dedicated worker. Pending output folders are excluded from the initial index so partial pages never leak in. Ordinary files continue through indexing, hashing, and tagging; the index emits a TTY heartbeat every 100 folders. Completed PDFs are handed through a queue one at a time, indexed, exact-deduplicated, and appended to the still-live perceptual worker while later PDFs continue rendering. An `Overly large image` failure clears only that PDF's partial outputs and retries at half DPI until it succeeds. Already-rendered PDFs are skipped (guarded on an existing `.png` in the out-dir) so a re-run doesn't churn page mtimes and defeat the ledger. Missing PyMuPDF is non-fatal.
0. **Index** (`index()`): one `os.walk` of the tree. Skips dotfiles / macOS `._` metadata (`fn.startswith(".")`), non-media extensions, files with an existing tag sidecar, and files the ledger already recorded as matched/no-match (unchanged). Returns the survivors **videos-first, then images** (each group in **natural path order** via `_natural_key` — `PAGE2` before `PAGE10` — for stable, resumable runs). A PNG living in a `pdf_page_dirs` folder is flagged `perceptual_only` and is **exempt from the has-sidecar skip** (its sidecar holds only the base `comic:`/`page:` tags), so it still gets perceptually searched — the ledger alone rules it out on a re-run.
1. **Hash + exact dedup** (`hash_all()` → `deduplicate()`): compute every candidate's local MD5 in a thread pool (disk-bound, safe to parallelize), including perceptual-only PDF pages for local duplicate detection. Before any network request, keep one deterministic canonical path per MD5 (preferring an unchanged prior `matched`/`nomatch` ledger record), mark copies as `duplicate` with `duplicate_of`, and write grouped details to `duplicates.log` in the scan root. `index()` and directory fingerprint finalization treat unchanged `duplicate` records as resolved; if their canonical path disappears, the copy becomes eligible again.
2. **Hash tier — run ALL and merge** (`hash_tier()`): e621 + InkBunny + Danbooru + Gelbooru queried by MD5 and unioned. The four boorus are **queried concurrently per file** via a `ThreadPoolExecutor` (four different hosts), each self-paced by its own `Pacer`. MD5 identity means byte-identical file, so there is zero false-positive risk and the tag sets genuinely differ. **Never short-circuit between these.** Gelbooru's post API returns a *flat* tag list, so `_gelbooru_categorize()` makes one extra batched call to map tags to `character:`/`creator:`/`series:`, falling back to unnamespaced tags if it fails.
3. **Perceptual fallback — only images that missed every hash lookup**: Fluffle (furry-oriented, exact perceptual) → SauceNAO (broad, last resort). Run **sequentially** on purpose: Fluffle only serves one request per client at a time (parallelizing gains nothing per its docs) and SauceNAO has a tiny daily quota. Perceptual is fuzzy and a wrong match injects wrong `character:`/`creator:` tags, so it never runs when a hash hit already gave authoritative tags.

The hash tier and perceptual tier are **separate passes** over the file list (phase 1/2 then phase 2/2), matching the "check all hashes first, then reverse-search the leftovers" model. Videos go through the hash tier only (no perceptual search — they can't be reverse-image-searched) and are processed first since they rarely hash-match. **PDF pages are the mirror image**: `perceptual_only` items skip the hash tier entirely (a re-rendered page never MD5-matches a booru) and are seeded straight into the phase-2 perceptual queue; their perceptual tags append to the sidecar that already carries `comic:`/`page:`.

### PDFs

`convert_pdf(pdf, out_root, dpi=300, write_sidecars=True) -> [png_paths]` lives in `furtag.py`. `plan_pdf_renders()` probes for PyMuPDF via `_import_fitz()` once before launching the background render worker (missing PyMuPDF degrades gracefully). PNG compression is lossless; DPI controls raster resolution. The sidecar is written with a **lowercase `.txt`** extension to match `tag_sidecar_path` (`<file>.<ext>.txt`), so perceptual tags append to the *same* file even on a case-sensitive volume. Requires **PyMuPDF** (in `requirements.txt`, import name `fitz`/`pymupdf`). Comic pages therefore never hit the boorus by hash — Fluffle/SauceNAO are their only shot, which is the intended behavior for re-rendered art.

### Session ledger

`Ledger` writes `.furtag_ledger.json` in the scanned root, keyed by **relative path + (size, mtime)**, recording each file as `"matched"` or `"nomatch"`. On the next run `index()` rules a file out **before hashing or querying it** — the whole point of keying on path+size+mtime rather than MD5. A file is re-checked only if its size or mtime changed (edited/replaced). Saved atomically (`.tmp` + `replace`), checkpointed every 25 records and in a `finally`, so an interrupted run keeps its progress. Its `.tmp`/dotfile name is auto-excluded by the indexer's dotfile skip.

### Perceptual → Authoritative Enrichment

The key pattern: a perceptual hit identifies *which* booru post the image is, so we re-query that booru's API by **post ID** for the full, properly-namespaced tag set — even though the local file was recompressed and didn't hash-match directly.

- Prefer **post ID** over MD5-from-URL (the URL-MD5 trick only works when the CDN URL embeds the hash). `_post_id_from_url()` handles both `/posts/N` and e621's legacy `/post/show/N`.
- **Fluffle**: `find_best_exact_match()` priority is exact-e621 > exact-other > tossUp-e621. `tossUp` is accepted **only** on e621 (gated by `matching.fluffle_tossup_e621_only` in settings, read per instance as `self.fluffle_tossup_e621`) because we then re-query e621 by ID via `e621_lookup_by_id()`, so a near-miss stays low-risk. All other `tossUp`/`alternative`/`unlikely` are rejected.
- **SauceNAO**: `_saucenao_best_authoritative()` reads the `e621_id`/`danbooru_id`/`gelbooru_id` fields directly (preferring e621 → danbooru → gelbooru, and only for sources we hold creds for), then `_authoritative_lookup()` re-queries that booru. Gated behind `saucenao_auth_similarity` (default **88%**), a **higher** bar than `saucenao_min_similarity` (default **80%**) used to accept SauceNAO's own thinner tags. Both are settings-driven (GUI / `settings.json`).

### SauceNAO own-tags (the messy fallback)

When no booru-ID match clears the gate, `_extract_saucenao_tags()` is used. SauceNAO's per-index `data` is inconsistent, so:
- **URLs are gathered from all qualifying results, but tags come from only the single highest-similarity result** (`_saucenao_result_tags()`) — merging tags across results produced Frankenstein tag sets (multiple creators, character-as-series).
- `source` is **excluded** from series-fields (for e-hentai etc. it's the gallery title or a URL, not a series).
- Characters split on **commas/semicolons only**, never `and`/`/` (those live inside disambiguators like `calvin (calvin and hobbes)`).
- `_clean_tag_text()` converts underscores → spaces (Hydrus style), the opposite of its original behavior.

## Tag Namespaces (Hydrus convention)

- `creator:` artist/author · `character:` · `species:` · `series:` franchise the characters are *from* (fanart) · `comic:` e621 pool name · `page:` numeric position in a pool · `site:` origin platform · `title:` SauceNAO work title (fallback tier only) · unnamespaced = e621 general/meta tags

### Pools / Comics

`_e621_pool_tags()` reads `post["pools"]`, fetches each pool (cached in `self._pool_cache` so a multi-page comic isn't re-fetched per page), emits `comic:<name>` for each pool and `page:<n>` from the post's index in the pool's ordered `post_ids`.

## Rate Limiting

Each service gets its own thread-safe **`Pacer`** (`self.pace[<service>]`) — a minimum-interval limiter that reserves the next free time slot, so successive calls to one service stay ≥ its interval apart even across worker threads, while different services never block each other. Every HTTP getter calls `self.pace[...].wait()` before the request; there are no scattered `time.sleep()` calls anymore. A paced wait sleeps on the run's **cancel event**, not `time.sleep()`, so cancelling never has to wait out a long interval (SauceNAO paces at 6s and backs off further). Intervals (top of file) come from each API's documented/observed limit:

- `E621_INTERVAL=1.0` (hard cap 2/s, e621 recommends sustained ≤1/s) · `INKBUNNY_INTERVAL=1.0` · `DANBOORU_INTERVAL=0.3` (posts endpoint allows 10/s) · `GELBOORU_INTERVAL=0.7` (two calls per hit) · `FLUFFLE_INTERVAL=1.2` (one concurrent request per client) · `SAUCENAO_INTERVAL=6.0` (~6 req/30s).

`Pacer.backoff(s)` pushes the next slot out on HTTP 429 (e621, Fluffle, SauceNAO). SauceNAO additionally self-regulates from its own response headers: `_saucenao_check_quota()` backs off on `short_remaining<=0` and sets `saucenao_exhausted` (disables SauceNAO for the run) on `long_remaining<=0`. Because the hash tier runs the four boorus concurrently, its throughput is gated by the *slowest* enabled service (~1/s), not the sum of their intervals.

## Known Verification Points

- **InkBunny `md5` is a boolean toggle, not the hash.** `_inkbunny_search_md5()` must send `text=<md5>&md5=yes` (the hash goes in `text`; `md5=yes` makes it search file checksums). Sending the hash in `md5=` makes InkBunny ignore it and return the entire site — the cause of an earlier bug that flooded every sidecar with unrelated tags.
- **Danbooru API keys have per-key permission scopes.** A key with no scopes authenticates but returns `403 PrivilegeError` (vs `401 Invalid API key` for a wrong key). `_danbooru_get()` falls back to anonymous reads (which Danbooru allows for md5 lookups) on any 401/403.
- **e-hentai was evaluated and rejected** — its API gives only gallery-level (not per-image), sparse, user-suggested tags, and adult galleries need exhentai login cookies. Not worth the SHA-1 tier + scraping. Don't re-add it without a strong reason.
- Adding another exact-hash source (e.g. derpibooru via SHA-512 — needs a second local hash pass since Philomena doesn't index MD5) is a small drop-in: add it to `enabled_hash_services()` + the `_hash_lookup()` dispatch, plus a `_parse_*` method, an `_init_*` helper, and a `Pacer` in `self.pace`.
- **Concurrency invariant.** Within one file the four hash lookups run on separate threads, but they hit *different* services, so no service is called twice at once and per-service mutable state (`ib_sid`, `danbooru_anon`) is never raced. Files are processed one at a time, so a service's own re-login/anon-fallback can't overlap another call to itself. Keep it that way if you refactor — the `requests.Session` is shared read-only across threads.
