# FurTag

**FurTag** reverse-image-searches your media files against furry/booru services and writes the tags and source URLs it finds into [Hydrus Network](https://hydrusnetwork.github.io/hydrus/). Point it at a folder of images, videos, and PDFs — it can either **push straight into a running Hydrus client** via the Client API (import + tags + URLs, no sidecars), or write classic Hydrus-compatible sidecars (`<file>.<ext>.txt` + `<file>.<ext>.urls.txt`) for drag-and-drop import.

It's built for tagging a large personal media archive with as little manual work as possible: it queries multiple sources, resumes where it left off, and rate-limits itself so you don't get throttled or banned. macOS-oriented (double-clickable launcher, Finder drag-and-drop), but the Python script itself is cross-platform.

---

## Features

- **Multi-source hash tier** — every file's local MD5 is looked up on **e621, InkBunny, Danbooru, and Gelbooru concurrently**, and all results are merged. MD5 identity means a byte-identical file, so there's zero false-positive risk and each site contributes tags the others miss.
- **Perceptual fallback** — images that get no hash hit fall through to **Fluffle** (furry-oriented exact perceptual matching), then **SauceNAO** as a last resort. When a perceptual match identifies a specific booru post, FurTag re-queries that booru by post ID to pull the full, properly-namespaced tag set.
- **Concurrent, pipelined tiers** — the hash tier and perceptual tier run *at the same time*: as soon as a file misses every hash lookup it's handed to a perceptual worker thread that runs alongside the rest of the hash tier. A **two-track live terminal display** shows both a hash-tier panel and a perceptual panel, framed and separated by rules, each with its own progress bar and ETA.
- **PDF support** — every PDF is rendered to per-page PNGs (with `comic:`/`page:` sidecars) and sent straight to the perceptual tier (a re-rendered page never MD5-matches an original). Gracefully skipped if PyMuPDF isn't installed.
- **Hydrus Client API output** — with `hydrus_api_url` + `hydrus_access_key` set, FurTag imports each hit into Hydrus and applies tags/URLs on the spot (default service: **downloader tags**). No `.txt` sidecars required.
- **Hydrus sidecar fallback** — without the API (or with `hydrus_also_sidecars = true`), writes separate tag and URL sidecars per file using Hydrus namespace conventions.
- **Session ledger / resumable runs** — a `.furtag_ledger.json` in the scanned folder records every file as matched/no-match keyed by path + size + mtime, so re-runs skip already-done work without re-hashing or re-querying. A file is only re-checked if it was edited or replaced.
- **Per-service rate limiting** — each service has its own thread-safe pacer tuned to its documented/observed limit, so successive calls to one service stay polite while different services never block each other.
- **Junk-tag stripping** — "artist unknown / anonymous" placeholder tags that boorus emit are automatically dropped before writing.
- **SauceNAO rate auto-detection** — SauceNAO's own API responses report your account's short-window allowance, so FurTag adapts its pace automatically: enhanced/donor accounts speed up, free accounts stay within limits, with no configuration needed. It also backs off and disables SauceNAO for the run when the daily limit is reached.

---

## Requirements

- **macOS** (the launcher and drag-and-drop are macOS-oriented; the script is otherwise cross-platform)
- **Python 3.7+**
- Python packages (installed automatically on first run — see below):
  - `pillow`, `requests`, `regex`
  - `PyMuPDF` (import name `fitz` / `pymupdf`) — only needed for PDF support. **PDFs are gracefully skipped if it's missing**; everything else still works.

### Running it

The intended entry point is the double-clickable **`FurTag.command`**. On first run it creates a `.venv`, installs the dependencies, and launches the tool. It re-installs deps automatically whenever `requirements.txt` changes, so you never have to delete the venv by hand.

```bash
./FurTag.command
```

Or set it up and run manually:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python furtag.py
```

> The project venv is required — your system `python3` won't have the dependencies.

---

## Setup: credentials

Create a single **`credentials.txt`** alongside the script, one `key = value` per line. **Any missing or incomplete key simply disables that source** instead of crashing — so you can fill in only the services you have accounts for.

```ini
# credentials.txt — one key = value per line
e621_username     = your_e621_username
e621_api_key      = your_e621_api_key_here
inkbunny_username = your_inkbunny_username
inkbunny_password = your_inkbunny_password
danbooru_username = your_danbooru_username
danbooru_api_key  = your_danbooru_api_key_here
gelbooru_user_id  = your_gelbooru_user_id
gelbooru_api_key  = your_gelbooru_api_key_here
sauce_nao_api_key = your_saucenao_api_key_here

# Optional — push straight into Hydrus (Client API). No sidecars needed.
hydrus_api_url       = http://127.0.0.1:45869
hydrus_access_key    = your_64char_client_api_access_key
hydrus_tag_service   = downloader tags
hydrus_import        = true    # import the file then tag it (false = tag-only by hash)
hydrus_also_sidecars = false   # also write .txt sidecars when the API is on
hydrus_results_page  = FurTag Results  # silently collect accepted files; false disables
hydrus_already_tagged_page = Already Tagged  # matched ledger history; false disables
```

**InkBunny note:** in your InkBunny account settings you must enable **API access** *and* **adult ratings**, or explicit results stay hidden from the API.

**Danbooru note:** API auth requires a verified-email account; if the key is rejected FurTag falls back to anonymous Danbooru access (which still allows MD5 lookups).

**Hydrus Client API note:** enable the API under *services → manage services*, then create an access key under *review services* with permissions to **import files**, **edit tags**, **edit URLs**, and **manage pages**. FurTag verifies the key and resolves `hydrus_tag_service` (name or service key) on startup; if the client is offline it falls back to sidecars. Successfully imported/already-present files collect on a new, unfocused `hydrus_results_page` each run. Files marked `matched` by existing ledgers collect on a separate, unfocused `hydrus_already_tagged_page`; their SHA-256 values are cached into the ledgers after the first page load. Set either page option to `false` to disable it.

---

## ⚠️ Security warning — read this

`credentials.txt` stores API keys **and account passwords in plaintext**. Treat the file as a secret.

- **It's git-ignored by default.** This repo's `.gitignore` already excludes `credentials.txt`, so it won't be committed. **If you fork or push your own copy, verify it's still ignored before you push** (`git status` should never list it).
- **Lock down its permissions.** Restrict it to owner read/write only:
  ```bash
  chmod 600 credentials.txt
  ```
- **Some fields are real account passwords** (InkBunny), which makes a leak worse than exposing a mere API key. Where a service supports it, prefer app-specific or throwaway credentials, and **rotate anything that ever gets exposed**.
- **"Should I just delete the file after each run?"** You *can* — but the tool needs it again on every run, so deleting and recreating it each time is more hassle than it's worth. In practice, locking it down (`chmod 600` + gitignore) is the more useful protection than deleting it.
- **Future enhancement:** secrets could be stored in the macOS Keychain (via the `keyring` library) instead of plaintext, for anyone who wants that. **This is not implemented yet.**

---

## Usage

Run `./FurTag.command` (or `.venv/bin/python furtag.py`). FurTag prompts for a folder to scan:

- **Blank entry** = the current directory.
- **Finder drag-and-drop** paths are accepted (the escaping Terminal inserts is undone automatically).
- Type `q`, `quit`, or `exit` (or press Ctrl+C / Ctrl+D) to quit. An invalid path re-prompts rather than exiting.
- Type **`NUKE!`** instead of a folder to enter reset mode. FurTag asks for the target folder, counts its generated ledgers/reports and media sidecars recursively, and requires `ARE YOU SURE? [y/N]` confirmation. A separate second `[y/N]` question then offers to remove precisely named rendered PDF pages so they are re-exported. Source PDFs, ordinary media, and unrelated output-folder contents are never selected; filesystem roots are refused. The reset then immediately scans that folder from scratch.

After local hashing, FurTag detects byte-identical files by MD5 before making any network requests. One deterministic canonical path is searched; the other copies are recorded as `duplicate` in their ledgers and skipped on later runs. A readable `duplicates.log` in the scanned folder lists each exact hash, the selected canonical file, and every skipped location. An unchanged file already represented by a matched/no-match ledger takes precedence over a new copy.

It then walks the folder tree and processes files, showing the **two-track live display** — one panel for the hash tier, one for the perceptual tier — each with a previous/current/next file view, a phase label, and a progress bar with elapsed time and ETA. The current file carries a live sub-status showing which site is being checked.

**Symbol legend:**

| Symbol | Meaning |
| ------ | ------- |
| `…` | querying |
| `✓` | found |
| `✗` | not found (clean miss) |
| `⚠` | error / blocked |

When it finishes, each media file that matched has two sidecars written next to it (see below).

---

## How it works

The pipeline runs in stages (see `CLAUDE.md` for the full architecture):

1. **Index** — one walk of the folder tree. Dotfiles, macOS `._` metadata, non-media files, files that already have a tag sidecar, and files the ledger already recorded (unchanged) are skipped. Survivors are returned videos-first, then images, each in natural path order for stable, resumable runs.
2. **Hash** — every candidate's local MD5 is computed in a thread pool.
3. **Hash tier** — e621, InkBunny, Danbooru, and Gelbooru are queried by MD5 concurrently and their results merged. Videos go through this tier only (they can't be reverse-image-searched).
4. **Perceptual tier** — images that missed every hash lookup run through Fluffle, then SauceNAO. This tier runs *concurrently* with the hash tier via a worker thread: a file that misses the hash tier is handed off immediately.

**Resumability** — the `.furtag_ledger.json` session ledger, written in the scanned folder, records each file as matched or no-match keyed by path + size + mtime. On the next run those files are ruled out before any hashing or querying, so interrupted runs pick up right where they stopped. It's checkpointed periodically and saved atomically.

**PDFs** — each PDF is rendered to per-page, lossless PNGs in a subfolder beside it, each with a `comic:`/`page:` sidecar, then fed straight into the perceptual tier (their perceptual tags append to that same sidecar). When new PDFs are found, a numbered menu offers standard 300 DPI, archival 600 DPI, or a custom 72–2400 DPI value. Rendering runs on a dedicated background worker while ordinary files continue through indexing, hashing, and tagging; the index shows a live folder/media count on large trees. Each completed PDF joins the perceptual queue while later PDFs continue rendering. If PyMuPDF rejects an oversized page bitmap, that PDF automatically retries at half the DPI until it fits, remaining lossless PNG throughout. Already-rendered PDFs are skipped unless the optional PDF portion of `NUKE!` removes them for re-export.

For the complete architecture — rate-limiting internals, perceptual→authoritative enrichment, per-source parsing quirks — see **[`CLAUDE.md`](./CLAUDE.md)**.

---

## Output format

Two sidecars are written per matched file (Hydrus imports them via two separate sidecar routers):

- **`<file>.<ext>.txt`** — tags, one per line
- **`<file>.<ext>.urls.txt`** — source URLs, one per line

The `<file>.<ext>.txt` suffix (rather than an extension-stripped `<file>.txt`) is deliberate — it avoids collisions when a folder holds, say, `cat.jpg` and `cat.png`.

Tags follow Hydrus namespace conventions:

| Namespace | Meaning |
| --------- | ------- |
| `creator:` | artist / author |
| `character:` | character name |
| `species:` | species |
| `series:` | franchise the character is *from* (for fanart) |
| `comic:` | e621 pool / comic name |
| `page:` | numeric page position within a pool |
| `site:` | origin platform |
| `title:` | SauceNAO work title (fallback tier only) |
| *(none)* | general / meta tags |

---

## Notes

FurTag is a consolidation of earlier iterations into a single script (`furtag.py`). Double-click `FurTag.command` (or run the script in the project venv) to launch it. PDF rendering lives in the same file and runs as a pre-pass when you scan a folder.

No license specified yet.
