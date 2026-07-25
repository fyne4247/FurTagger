# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Authoritative product/architecture plan:** see `AGENTS.md` and `PLAN.md`.
> This file is kept for Claude Code compatibility; when the two disagree, prefer
> `AGENTS.md` / the live code.

## What This Is

FurTag is a single Python script (`furtag.py`) that reverse-image-searches media files against furry/booru services and writes the retrieved metadata into Hydrus Network — either via the **Client API** (import + tags + URLs, no sidecars) or as classic Hydrus-compatible sidecar files. It is the consolidation of four earlier iterations (now deleted).

## Running It

```bash
./FurTag.command         # double-clickable; bootstraps .venv + deps on first run
# or manually:
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python furtag.py
```

`FurTag.command` is the intended entry point — it runs `furtag.py` inside the project venv (system `python3` lacks the deps). On launch it (re)installs deps whenever `requirements.txt` is newer than `.venv/.deps-stamp`, so adding a dependency doesn't require deleting the venv. It prompts for a folder; a blank entry defaults to the current directory. Files that already have a tag sidecar **or** are recorded in the session ledger (see below) are skipped.

`prompt_for_folder()` handles the folder prompt: it accepts **Finder drag-and-drop paths** (`_unescape_path()` runs the input through `shlex.split` to undo the backslash/quote escaping Terminal inserts), **re-prompts** on an invalid path instead of exiting, and quits cleanly on `q`/`quit`/`exit`, Ctrl+C, or Ctrl+D.

### Terminal display

`LiveDisplay` renders an in-place panel (previous / current / next file, a **phase label**, and a bottom progress bar with elapsed/ETA). The current line carries a live sub-status: during the hash tier it shows each site ticking off (`hash ▸ e621 ✓  ib ·  dan ✓  gel …` — ✓ hit, ✗/· miss, … in-flight); during perceptual it names the engine and any enrichment step. It is **thread-safe** (a lock guards every write) because the hash tier renders from a worker pool. All warnings/errors go through the module-level **`notify()`** (not `print`), which routes them *above* the live panel via `_display.log()`. When stdout isn't a TTY it degrades to one line per file. **Use `notify()` for any user-facing status message inside the processing loop** — a raw `print` would corrupt the panel.

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
hydrus_results_page       (name/on/off, default on — master toggle for the two result pages below)
hydrus_new_imports_page   (= "FurTag New Imports" — brand-new imports this run)
hydrus_newly_tagged_page  (= "FurTag Newly Tagged" — files already in Hydrus, newly tagged)
hydrus_already_tagged_page(= "Already Tagged" — ledger-history review page; false disables)
```

For InkBunny, enable API access **and** adult ratings in account settings, or explicit results stay hidden. `load_credentials()` reads the file once into a dict and the `_init_<source>` helpers each pull their keys. `_init_hydrus()` verifies the access key and resolves the tag service name → `service_key`; on failure FurTag keeps writing sidecars.

### Hydrus Client API output

When `has_hydrus` is true, `write_results()` calls `_hydrus_push()` instead of (or in addition to, if `hydrus_also_sidecars`) writing `.txt` sidecars:

1. `POST /add_files/add_file` with `{path}` when `hydrus_import` (status 1/2 → hash)
2. `POST /add_tags/add_tags` with `service_keys_to_tags` and `override_previously_deleted_mappings: false` (downloader-like)
3. `POST /add_urls/associate_url` with `urls_to_add` — **only when the access key holds Hydrus permission 0 ("Import and Edit URLs")**, checked once at startup (`hydrus_can_edit_urls`) and warned about if missing. A URL-association failure is caught and warned per file; it never aborts the tag push, the results-page add, or hash caching.
4. `_hydrus_add_to_page(kind, hash)` files it onto one of two review pages by **import status**: status 1 (brand-new import) → **New Imports** page, status 2 / tag-only mode (already in Hydrus, just tagged) → **Newly Tagged** page. Pages are created lazily on first file, cached by page key in `self.hydrus_result_pages`, and any per-page failure disables just that page for the run.

Pushes are serialised with `_hydrus_lock` because the hash tier and perceptual worker can both call `write_results`. PDF pages still get `comic:`/`page:` via `_pdf_page_base_tags()` even when convert sidecars are skipped.

### Already Tagged review page

`_hydrus_populate_already_tagged_page(ledger_mgr, limit)` builds an unfocused page from unchanged `matched` ledger records (files skipped this run). At the start of `run()`, `prompt_for_already_tagged()` asks whether to build it and how many to show: **None** = skip, **0** = all, **N** = the N most recently tagged. "Most recently tagged" sorts on each record's `tagged_at` wall-clock stamp (written by `Ledger.record()` for matched files; records predating the field sort oldest). A non-TTY stdin skips the prompt and builds the full page (prior behavior).

## Pipeline (the core architecture)

`TagIntegrator.run()` drives four stages (preceded by a PDF pre-pass):

- **PDF background render** (`plan_pdf_renders()` → `render_pdf_jobs()`): discover PDFs and existing page folders first, then render unconverted PDFs as lossless PNGs on one dedicated worker. Pending output folders are excluded from the initial index so partial pages never leak in. Already-rendered PDFs are skipped. Missing PyMuPDF is non-fatal. See **PDFs** below.
0. **Index** (`index()`): one `os.walk` of the tree. Skips dotfiles / macOS `._` metadata (`fn.startswith(".")`), non-media extensions, files with an existing tag sidecar, and files the ledger already recorded as matched/no-match (unchanged). Returns the survivors **videos-first, then images** (each group in **natural path order** via `_natural_key` — `PAGE2` before `PAGE10` — for stable, resumable runs). A PNG living in a `pdf_page_dirs` folder is flagged `perceptual_only` and is **exempt from the has-sidecar skip** (its sidecar holds only the base `comic:`/`page:` tags), so it still gets perceptually searched — the ledger alone rules it out on a re-run.
1. **Hash** (`hash_all()`): compute every candidate's local MD5 in a thread pool (disk-bound, safe to parallelize) so the network stage never recomputes it.
2. **Hash tier — run ALL and merge** (`hash_tier()`): e621 + InkBunny + Danbooru + Gelbooru queried by MD5 and unioned. The four boorus are **queried concurrently per file** via a `ThreadPoolExecutor` (four different hosts), each self-paced by its own `Pacer`. MD5 identity means byte-identical file, so there is zero false-positive risk and the tag sets genuinely differ. **Never short-circuit between these.** Gelbooru's post API returns a *flat* tag list, so `_gelbooru_categorize()` makes one extra batched call to map tags to `character:`/`creator:`/`series:`, falling back to unnamespaced tags if it fails.
3. **Perceptual fallback — only images that missed every hash lookup**: Fluffle (furry-oriented, exact perceptual) → SauceNAO (broad, last resort). Run **sequentially** on purpose: Fluffle only serves one request per client at a time (parallelizing gains nothing per its docs) and SauceNAO has a tiny daily quota. Perceptual is fuzzy and a wrong match injects wrong `character:`/`creator:` tags, so it never runs when a hash hit already gave authoritative tags.

The hash tier and perceptual tier are **separate passes** over the file list (phase 1/2 then phase 2/2), matching the "check all hashes first, then reverse-search the leftovers" model. Videos go through the hash tier only (no perceptual search — they can't be reverse-image-searched) and are processed first since they rarely hash-match. **PDF pages are the mirror image**: `perceptual_only` items skip the hash tier entirely (a re-rendered page never MD5-matches a booru) and are seeded straight into the phase-2 perceptual queue; their perceptual tags append to the sidecar that already carries `comic:`/`page:`.

### PDFs

`convert_pdf(pdf, out_root, dpi=300, write_sidecars=True) -> [png_paths]` lives in `furtag.py`. `plan_pdf_renders()` probes for PyMuPDF via `_import_fitz()` once before launching the background render worker (missing PyMuPDF degrades gracefully). Base-tag sidecars (`comic:`/`page:`) are written in the configured format (txt or json). Requires **PyMuPDF** (in `requirements.txt`, import name `fitz`/`pymupdf`). Comic pages therefore never hit the boorus by hash — Fluffle/SauceNAO are their only shot, which is the intended behavior for re-rendered art.

### Session ledger

`Ledger` writes `.furtag_ledger.json` in the scanned root, keyed by **relative path + (size, mtime)**, recording each file as `"matched"` or `"nomatch"`. On the next run `index()` rules a file out **before hashing or querying it** — the whole point of keying on path+size+mtime rather than MD5. A file is re-checked only if its size or mtime changed (edited/replaced). Saved atomically (`.tmp` + `replace`), checkpointed every 25 records and in a `finally`, so an interrupted run keeps its progress. Its `.tmp`/dotfile name is auto-excluded by the indexer's dotfile skip.

### Perceptual → Authoritative Enrichment

The key pattern: a perceptual hit identifies *which* booru post the image is, so we re-query that booru's API by **post ID** for the full, properly-namespaced tag set — even though the local file was recompressed and didn't hash-match directly.

- Prefer **post ID** over MD5-from-URL (the URL-MD5 trick only works when the CDN URL embeds the hash). `_post_id_from_url()` handles both `/posts/N` and e621's legacy `/post/show/N`.
- **Fluffle**: `find_best_exact_match()` priority is exact-e621 > exact-other > tossUp-e621. `tossUp` is accepted **only** on e621 (gated by `matching.fluffle_tossup_e621_only` in settings, read per instance as `self.fluffle_tossup_e621`) because we then re-query e621 by ID via `e621_lookup_by_id()`, so a near-miss stays low-risk. All other `tossUp`/`alternative`/`unlikely` are rejected.
- **SauceNAO**: `_saucenao_best_authoritative()` reads the `e621_id`/`danbooru_id`/`gelbooru_id` fields directly (preferring e621 → danbooru → gelbooru, and only for sources we hold creds for), then `_authoritative_lookup()` re-queries that booru. Gated behind `saucenao_auth_similarity` (default **88%**), a **higher** bar than `saucenao_min_similarity` (default **80%**) used to accept SauceNAO's own thinner tags.

### SauceNAO own-tags (the messy fallback)

When no booru-ID match clears the gate, `_extract_saucenao_tags()` is used. SauceNAO's per-index `data` is inconsistent, so:
- **URLs are gathered from all qualifying results, but tags come from only the single highest-similarity result** (`_saucenao_result_tags()`) — merging tags across results produced Frankenstein tag sets (multiple creators, character-as-series).
- **If the qualifying results yield no URL at all, the bare `site:` tag is dropped** — SauceNAO often returns an e-hentai/exhentai index match with an empty `ext_urls`, and a `site:e-hentai` tag you can't follow up on is just noise. Standalone `creator:`/`title:`/`character:` tags are kept.
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
