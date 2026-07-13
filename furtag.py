#!/usr/bin/env python3

"""
Unified Furry Tag Integrator for Hydrus Network
================================================

Pipeline (per run):

  1. INDEX     Walk the target folder tree once. Skip dotfiles / macOS ._ metadata,
               non-media files, files that already have a tag sidecar, and files the
               session ledger already recorded as matched/no-match (unchanged since).
               Only the survivors are candidates.

  2. HASH      Compute the local MD5 of every candidate in parallel (fast, disk-bound).

  3. HASH TIER Exact MD5 lookups — run for every candidate, results merged:
                 e621 · InkBunny · Danbooru · Gelbooru
               The four boorus are queried CONCURRENTLY per file (four different
               hosts), each self-paced to its own rate limit. Videos go first
               (they can't be reverse-image-searched and rarely hash-match).

  4. PERCEPTUAL  Only images that missed every hash lookup, run sequentially:
                 Fluffle (furry-oriented exact perceptual) → SauceNAO (broad, last
                 resort). Fluffle only serves one request per client at a time and
                 SauceNAO has a tiny daily quota, so this tier is intentionally serial.

Session ledger (.furtag_ledger.json, one per folder, keyed by filename + size +
mtime): records every file as "matched" or "nomatch" so re-runs skip work
already done — without needing to re-hash or re-query anything (each record
also caches the file's MD5). Living inside the folder it describes rather than
the scan root, a subfolder's ledger is honored no matter which ancestor
directory a later run scans. Each ledger also seals a directory-level
fingerprint (file count + total size) once every file in it is accounted for,
so an unchanged folder can be skipped wholesale on the next run without
checking any individual file. A file/folder is only re-checked if something
in it actually changed (size, mtime, or membership).

Output — pick one (or both) via credentials.txt:

  A) Hydrus Client API (preferred when configured):
        import file → add tags → associate source URLs
        No sidecar files. Tags land on a local tag service
        (default: "downloader tags").

  B) Hydrus-compatible sidecars (default when API is off):
        <file>.<ext>.txt       → tags (one per line)
        <file>.<ext>.urls.txt  → source URLs (one per line)

Python 3.7+ compatible.

Dependencies:
    pip install pillow requests regex
    (+ PyMuPDF for PDFs; optional)

Credentials live in a single credentials.txt alongside this script
(any missing/incomplete key just disables that source instead of crashing):

    credentials.txt
        e621_username     = your_e621_username
        e621_api_key      = your_64char_api_key
        inkbunny_username = your_inkbunny_username
        inkbunny_password = your_inkbunny_password
        danbooru_username = your_danbooru_username
        danbooru_api_key  = your_danbooru_api_key
        gelbooru_user_id  = your_gelbooru_user_id
        gelbooru_api_key  = your_gelbooru_api_key
        sauce_nao_api_key = your_saucenao_api_key

        # Optional — push straight into a running Hydrus client (no sidecars):
        hydrus_api_url       = http://127.0.0.1:45869
        hydrus_access_key    = your_64char_client_api_access_key
        hydrus_tag_service   = downloader tags
        hydrus_import        = true    # import file then tag (false = tag-only)
        hydrus_also_sidecars = false   # also write .txt sidecars when API is on
        hydrus_results_page  = FurTag Results  # blank/false disables background page
        hydrus_already_tagged_page = Already Tagged  # matched ledger history; false disables

    Note: Danbooru requires a verified-email account for API auth; if the key
    is rejected (403) the script falls back to anonymous Danbooru access.
    Hydrus Client API needs permissions: import files, edit tags, edit URLs,
    and manage pages for the optional unfocused results page.
"""

import concurrent.futures as cf
import hashlib
import json
import os
import queue
import re
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import requests
from PIL import Image, ImageFile
import regex  # for emoji stripping

ImageFile.LOAD_TRUNCATED_IMAGES = True  # don't crash on slightly-truncated files

# ── Constants ────────────────────────────────────────────────────────────────

THUMB_MAX = 256

# Minimum seconds between successive requests to each service, chosen from each
# API's documented / recommended limit. Because the four hash boorus run
# concurrently (different hosts), the hash tier's throughput is gated only by the
# slowest of these — not their sum.
E621_INTERVAL     = 1.0   # hard cap 2/s; e621 recommends sustained ≤1/s
INKBUNNY_INTERVAL = 1.0   # no published number; docs ask you to be gentle
DANBOORU_INTERVAL = 0.3   # posts endpoint allows 10/s; stay well under it
GELBOORU_INTERVAL = 0.7   # no published number; two calls per hit, so be polite
FLUFFLE_INTERVAL  = 1.2   # one concurrent request per client — strictly serial
SAUCENAO_INTERVAL = 6.0   # ~6 requests / 30s short limit (+ daily cap via headers)

# Gelbooru tag "type" → Hydrus namespace prefix ("" = unnamespaced)
GELBOORU_TYPE = {0: "", 1: "creator:", 3: "series:", 4: "character:", 5: ""}

MIN_SIMILARITY = 80.0           # accept SauceNAO's own (thinner) tags above this
SAUCENAO_AUTH_SIMILARITY = 88.0  # trust a booru-ID match enough to re-query that
                                 # booru for its authoritative tag set

# Fluffle: always trust "exact". Also accept "tossUp" BUT only when it resolves
# to e621 — there we re-query the post by ID for the authoritative tag set, so a
# near-miss stays low-risk. Set False to require exact matches only.
FLUFFLE_TOSSUP_E621 = True

IMG_EXTS   = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv"}
PDF_EXTS   = {".pdf"}
PDF_DPI    = 300            # render resolution for PDF pages
PDF_ARCHIVAL_DPI = 600      # lossless archival preset; custom values are allowed

CREDENTIALS_FILE = "credentials.txt"
LEDGER_FILE      = ".furtag_ledger.json"
DUPLICATES_FILE  = "duplicates.log"

# "Artist unknown" placeholder tags that every booru emits in some form — useless
# noise in a Hydrus library, so they're dropped before writing. Compared against
# the tag lowercased with underscores already normalised to spaces (see
# _clean_tag_text / parsers). Bare general-tag forms plus any creator:<value>
# whose value is one of _JUNK_CREATOR_VALUES are removed.
_JUNK_TAGS = {
    "unknown artist", "artist request", "anonymous artist",
    "unknown_artist", "artist_request", "anonymous_artist",
    "creator:unknown", "creator:anonymous",
}
_JUNK_CREATOR_VALUES = {
    "unknown", "unknown artist", "anonymous", "anonymous artist", "artist request",
}


def _is_junk_tag(tag: str) -> bool:
    """True for 'artist unknown' placeholder tags that shouldn't be written."""
    low = tag.lower().strip()
    if low in _JUNK_TAGS:
        return True
    if low.startswith("creator:"):
        return low[len("creator:"):].strip() in _JUNK_CREATOR_VALUES
    return False


def _truthy(val: str, default: bool = False) -> bool:
    """Parse a credentials.txt boolean (true/yes/1/on). Empty → default."""
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

EMOJI_PATTERN = regex.compile(
    r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
    r'\U0001F1E0-\U0001F1FF\U00002700-\U000027BF\U0001F900-\U0001F9FF'
    r'\U0001FA70-\U0001FAFF\U00002600-\U000026FF\U0001F170-\U0001F251]',
    flags=regex.UNICODE
)


# ── Per-service rate limiter ──────────────────────────────────────────────────

class Pacer:
    """Thread-safe minimum-interval pacer. Each caller reserves the next free
    time slot (spaced `interval` apart) and sleeps until then, so successive
    requests to one service never come closer than `interval` — even from
    different threads. Different services use different Pacers and never block
    each other."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = self._next if self._next > now else now
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def backoff(self, seconds: float) -> None:
        """Push the next allowed slot out by `seconds` (used on 429 / quota hits)."""
        with self._lock:
            target = time.monotonic() + seconds
            if target > self._next:
                self._next = target


# ── Live terminal display ────────────────────────────────────────────────────

@dataclass
class _Track:
    """Per-track progress state for LiveDisplay (one for the hash tier, one for
    perceptual). `growing=True` while the total isn't final yet — perceptual's
    total grows as the hash tier discovers new misses, so its ETA is only
    computed once the producer signals no more items are coming."""
    phase: str = ""
    total: int = 0
    done: int = 0
    idx: int = 0
    current: str = ""
    nxt: str = "—"
    prev: Tuple[str, str] = ("—", "")   # (name, result)
    sub: str = ""
    start: float = field(default_factory=time.monotonic)
    growing: bool = False
    interval: float = 0.0   # seconds/file for a rate-limit-based ETA (hash track only)


class LiveDisplay:
    """A fixed, in-place status panel with two independently-updating tracks
    (hash tier / perceptual tier), each showing previous / current / next
    file, a phase label, and a bottom progress bar with elapsed time and ETA.
    The current line carries a live sub-status (which site is being checked,
    ✓ hit / ✗ miss, etc.). Thread-safe — both tracks share one lock since the
    hash tier and perceptual worker run on different threads and can update
    concurrently. Falls back to one plain line per file when stdout isn't a TTY."""

    _ABBR = {"e621": "e621", "inkbunny": "ib", "danbooru": "dan", "gelbooru": "gel"}
    # · not started yet · … querying · ✓ found · ✗ not found (clean miss) · ⚠ error/blocked
    _SYM  = {"pending": "·", "run": "…", "hit": "✓", "miss": "✗", "err": "⚠"}
    _LEGEND = ("legend:  … querying   ✓ found   ✗ not found   ⚠ error/blocked")
    _TRACK_ORDER = ("hash", "perceptual")
    _SEP = "  " + "─" * 60
    _MAX_ISSUES = 3

    def __init__(self) -> None:
        self.tracks: Dict[str, _Track] = {k: _Track() for k in self._TRACK_ORDER}
        self._drawn = 0
        self._lock = threading.Lock()
        self._issues: List[str] = []
        self._issue_total = 0
        self.tty = sys.stdout.isatty()

    @staticmethod
    def _fmt(sec: float) -> str:
        sec = max(0, int(sec))
        h, rem = divmod(sec, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    @staticmethod
    def _trim(name: str, width: int = 44) -> str:
        return name if len(name) <= width else name[:width - 1] + "…"

    def hash_line(self, state: Dict[str, str]) -> str:
        return "hash ▸ " + "  ".join(
            f"{self._ABBR.get(s, s)} {self._SYM.get(st, '?')}" for s, st in state.items())

    def begin_phase(self, track: str, label: str, total: int,
                     growing: bool = False, interval: float = 0.0) -> None:
        with self._lock:
            t = self.tracks[track]
            t.phase, t.total, t.done, t.idx = label, total, 0, 0
            t.prev, t.sub = ("—", ""), ""
            t.start = time.monotonic()
            t.growing = growing
            t.interval = interval
            if not self.tty:
                print(f"\n=== {label} — {total} file(s) ===")

    def grow(self, track: str, by: int = 1) -> None:
        """Bump a still-growing track's total (perceptual gains items as the
        hash tier discovers misses)."""
        with self._lock:
            self.tracks[track].total += by
            if self.tty:
                self._render()

    def freeze_total(self, track: str) -> None:
        """Stop treating this track's total as still-increasing, so its ETA
        becomes computable (called once the producer feeding it is done)."""
        with self._lock:
            self.tracks[track].growing = False
            if self.tty:
                self._render()

    def start_file(self, track: str, idx: int, current: str,
                    nxt: Optional[str]) -> None:
        with self._lock:
            t = self.tracks[track]
            t.idx, t.current, t.nxt, t.sub = idx, current, nxt or "—", "…"
            if self.tty:
                self._render()

    def status(self, track: str, sub: str) -> None:
        with self._lock:
            self.tracks[track].sub = sub
            if self.tty:
                self._render()

    def finish_file(self, track: str, result: str) -> None:
        with self._lock:
            t = self.tracks[track]
            t.done = t.idx
            t.prev = (t.current, result)
            if self.tty:
                self._render()
            else:
                print(f"[{track}] [{t.idx}/{t.total}] {self._trim(t.current)} → {result}")

    def log(self, msg: str) -> None:
        """Keep warnings/errors in a three-line rolling panel while live.

        Non-interactive output stays line-oriented so redirected logs retain
        every issue. In a terminal, old issues roll off instead of permanently
        accumulating above the progress display.
        """
        with self._lock:
            if not self.tty:
                print(msg)
                return
            clean = " ".join(str(msg).split())
            self._issue_total += 1
            self._issues.append(self._trim(clean, 56))
            self._issues = self._issues[-self._MAX_ISSUES:]
            if any(t.phase for t in self.tracks.values()):
                self._render()

    def close(self) -> None:
        with self._lock:
            if self.tty and self._drawn:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._drawn = 0

    def _render_track(self, t: _Track) -> List[str]:
        elapsed = time.monotonic() - t.start
        width = 28
        filled = int(width * t.done / t.total) if t.total else 0
        bar = "█" * filled + "░" * (width - filled)
        pname, presult = t.prev

        if t.interval > 0:
            # Rate-limit-bound track (hash tier): throughput is capped by the
            # slowest enabled service's pacer, so files-left × that interval
            # is a tighter, non-jittery estimate than an observed-rate ETA.
            eta_part = f" · ETA {self._fmt(t.interval * max(0, t.total - t.done))}"
        else:
            # Perceptual's pace is conditional (Fluffle vs. SauceNAO, growing
            # total) — an ETA here is more misleading than useful.
            eta_part = ""

        return [
            f"  ✓ prev:    {self._trim(pname)}   {presult}",
            f"  ▶ current: {self._trim(t.current)}   {t.sub}",
            f"    next:    {self._trim(t.nxt)}",
            f"  {t.phase}",
            f"  [{bar}] {t.done}/{t.total}   ⏱ {self._fmt(elapsed)}{eta_part}",
        ]

    def _render(self) -> None:
        """Draw both tracks' panels stacked, framed and separated by rules so the
        two are easy to tell apart. Caller must hold self._lock."""
        lines: List[str] = [self._SEP]
        for i, key in enumerate(self._TRACK_ORDER):
            lines += self._render_track(self.tracks[key])
            lines.append(self._SEP)          # rule after each block
        if self._issues:
            shown = len(self._issues)
            history = (f" · latest {shown} of {self._issue_total}"
                       if self._issue_total > shown else "")
            lines.append(f"  Recent issues{history}")
            lines += [f"    {issue}" for issue in self._issues]
            lines.append(self._SEP)
        out = (f"\033[{self._drawn}A" if self._drawn else "")
        out += "".join("\033[2K" + ln + "\n" for ln in lines)
        sys.stdout.write(out)
        sys.stdout.flush()
        self._drawn = len(lines)


# Active display, if any. notify() routes warnings into its rolling history.
_display: Optional["LiveDisplay"] = None

# Sentinel pushed onto the perceptual queue once the hash tier is done
# producing, so the perceptual worker thread knows to stop and exit.
_PERCEPTUAL_DONE = object()


def notify(msg: str) -> None:
    if _display is not None:
        _display.log(msg)
    else:
        print(msg)


def _natural_key(s: str) -> List:
    """Sort key that orders embedded numbers numerically, so `PAGE2` precedes
    `PAGE10` instead of the lexical `PAGE1, PAGE10, PAGE2`. `re.split` on `(\\d+)`
    always alternates text/number chunks, so two keys only ever compare text-vs-
    text or int-vs-int at any position — never a TypeError."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r'(\d+)', s)]


# ── File index item ──────────────────────────────────────────────────────────

@dataclass
class FileItem:
    path: Path
    relpath: str
    size: int
    mtime: float
    kind: str                       # "image" | "video"
    ledger: "Ledger" = None          # this file's directory-scoped ledger
    md5: Optional[str] = None
    perceptual_only: bool = False   # PDF-derived page: skip hash tier, go perceptual


# ── Session ledger ───────────────────────────────────────────────────────────

class Ledger:
    """Per-directory JSON record of every file in that directory already
    processed, keyed by filename with a (size, mtime) fingerprint, plus a
    cached MD5 so an unchanged file is never re-hashed. Lives inside the
    directory it describes (not the scan root), so it travels with that
    folder and is picked up no matter which ancestor directory a later run
    scans from.

    Also carries a directory-level fingerprint (media file count + total
    size). Once every file in the directory has a resolved record, that
    fingerprint is "sealed" (`mark_dir_complete`) — a future run can then
    skip the entire folder on one count/size comparison, without touching
    any individual file, as long as the fingerprint still matches."""

    MTIME_EPS = 1e-3

    def __init__(self, dir_path: Path) -> None:
        self.dir = dir_path
        self.path = dir_path / LEDGER_FILE
        self.records: Dict[str, Dict] = {}
        self.dir_count: Optional[int] = None
        self.dir_size: Optional[int] = None
        self._dirty = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict):
                if isinstance(data.get("records"), dict):
                    self.records = data["records"]
                fp = data.get("dir_fingerprint")
                if isinstance(fp, dict):
                    self.dir_count = fp.get("count")
                    self.dir_size = fp.get("size")
        except Exception as e:
            notify(f"⚠️  Couldn't read ledger {self.path} ({e}); starting fresh.")

    def status_for(self, name: str, size: int, mtime: float) -> Optional[str]:
        """'matched' / 'nomatch' if this exact file was already processed, else None."""
        rec = self.records.get(name)
        if not rec or rec.get("size") != size:
            return None
        try:
            if abs(float(rec.get("mtime", -1)) - mtime) > self.MTIME_EPS:
                return None
        except (TypeError, ValueError):
            return None
        return rec.get("status")

    def md5_for(self, name: str, size: int, mtime: float) -> Optional[str]:
        """Reuse a previously-computed MD5 for an unchanged file even if its
        status isn't matched/nomatch (e.g. a booru was briefly unreachable
        last time) — saves re-hashing on retry."""
        rec = self.records.get(name)
        if not rec or rec.get("size") != size:
            return None
        try:
            if abs(float(rec.get("mtime", -1)) - mtime) > self.MTIME_EPS:
                return None
        except (TypeError, ValueError):
            return None
        return rec.get("md5")

    def sha256_for(self, name: str, size: int, mtime: float) -> Optional[str]:
        """Return a cached Hydrus/SHA-256 hash for this unchanged file."""
        rec = self.records.get(name)
        if not rec or rec.get("size") != size:
            return None
        try:
            if abs(float(rec.get("mtime", -1)) - mtime) > self.MTIME_EPS:
                return None
        except (TypeError, ValueError):
            return None
        return rec.get("sha256")

    def cache_sha256(self, name: str, size: int, mtime: float, sha256: str) -> None:
        """Add SHA-256 to an existing unchanged record for future page loads."""
        with self._lock:
            rec = self.records.get(name)
            if not rec or rec.get("size") != size:
                return
            try:
                unchanged = abs(float(rec.get("mtime", -1)) - mtime) <= self.MTIME_EPS
            except (TypeError, ValueError):
                unchanged = False
            if unchanged and rec.get("sha256") != sha256:
                rec["sha256"] = sha256
                self._dirty += 1

    def record(self, name: str, size: int, mtime: float, md5: Optional[str],
               status: str, sources: List[str], duplicate_of: str = "") -> None:
        with self._lock:
            record = {
                "size": size,
                "mtime": round(mtime, 3),
                "md5": md5,
                "status": status,
                "sources": sources,
            }
            if duplicate_of:
                record["duplicate_of"] = duplicate_of
            self.records[name] = record
            self._dirty += 1

    def fingerprint_matches(self, count: int, total_size: int) -> bool:
        return self.dir_count is not None and (self.dir_count, self.dir_size) == (count, total_size)

    def mark_dir_complete(self, count: int, total_size: int) -> None:
        """Seal the directory-level fingerprint. Only call once every current
        media file in the directory has a sidecar or a matched/nomatch record —
        otherwise an interrupted run could make a future scan wrongly skip
        files that were never actually processed."""
        with self._lock:
            if (self.dir_count, self.dir_size) != (count, total_size):
                self.dir_count, self.dir_size = count, total_size
                self._dirty += 1

    def save(self) -> None:
        with self._lock:
            if self._dirty == 0 and self.path.exists():
                return
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                payload: Dict = {"version": 2, "records": self.records}
                if self.dir_count is not None:
                    payload["dir_fingerprint"] = {"count": self.dir_count, "size": self.dir_size}
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=0),
                    encoding="utf-8")
                tmp.replace(self.path)   # atomic
                self._dirty = 0
            except Exception as e:
                notify(f"⚠️  Couldn't write ledger {self.path}: {e}")


class LedgerManager:
    """Lazily creates and caches one Ledger per directory encountered during a
    scan, so a tree with many folders doesn't reload the same ledger twice and
    concurrent workers touching different directories don't contend on a
    single lock (each Ledger has its own)."""

    def __init__(self) -> None:
        self._ledgers: Dict[Path, Ledger] = {}
        self._lock = threading.Lock()

    def get(self, dir_path: Path) -> Ledger:
        with self._lock:
            led = self._ledgers.get(dir_path)
            if led is None:
                led = Ledger(dir_path)
                led.load()
                self._ledgers[dir_path] = led
            return led

    def touched(self) -> List[Ledger]:
        with self._lock:
            return list(self._ledgers.values())

    def save_all(self) -> None:
        for led in self.touched():
            led.save()


# ── PDF → per-page PNGs ──────────────────────────────────────────────────────

def _import_fitz():
    """Load PyMuPDF (import name `pymupdf` or legacy `fitz`). Raises ImportError
    if neither is available — callers treat that as optional PDF support."""
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # PyMuPDF (older package layout)
    return fitz


def convert_pdf(pdf_path: Path, output_root: Path, dpi: int = PDF_DPI,
                write_sidecars: bool = True) -> List[Path]:
    """Render every page of ``pdf_path`` to a PNG under ``output_root/<stem>/``.

    Returns the list of PNG paths written. When ``write_sidecars`` is True each
    PNG also gets a ``comic:``/``page:`` ``.txt`` sidecar (lowercase extension
    so it matches ``tag_sidecar_path`` on case-sensitive volumes and perceptual
    tags append to the same file later).
    """
    fitz = _import_fitz()
    stem = pdf_path.stem
    out_dir = output_root / stem
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        print(f"  ! Failed to open {pdf_path.name}: {e}", file=sys.stderr)
        return []

    generated: List[Path] = []
    for i, page in enumerate(doc, start=1):
        base_name = f"{stem} PAGE{i}.PNG"
        png_path = out_dir / base_name
        txt_path = out_dir / f"{base_name}.txt"

        pix = page.get_pixmap(dpi=dpi)
        pix.save(png_path)
        generated.append(png_path)

        if write_sidecars:
            txt_path.write_text(f"comic:{stem}\npage:{i}\n", encoding="utf-8")

    doc.close()
    print(f"  {pdf_path.name}: {len(generated)} page(s) -> {out_dir}")
    return generated


def prompt_for_pdf_dpi(pdf_count: int) -> int:
    """Choose lossless PNG render resolution when new PDFs need conversion."""
    print(f"\n📄 {pdf_count} PDF(s) need page rendering (PNG is lossless).")
    print(f"   1) Standard — {PDF_DPI} DPI (recommended for reverse search)")
    print(f"   2) Archival — {PDF_ARCHIVAL_DPI} DPI (larger, maximum practical preset)")
    print("   3) Custom DPI (higher can use dramatically more memory and disk)")
    while True:
        try:
            raw = input("Choose PDF quality [1-3, default 1]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n↩️  Using standard {PDF_DPI} DPI.")
            return PDF_DPI
        if raw in {"", "1"}:
            return PDF_DPI
        if raw == "2":
            return PDF_ARCHIVAL_DPI
        if raw != "3":
            print("‼️  Choose 1, 2, or 3.")
            continue
        try:
            custom = input("Custom DPI [72-2400]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n↩️  Using standard {PDF_DPI} DPI.")
            return PDF_DPI
        try:
            dpi = int(custom)
        except ValueError:
            print("‼️  Enter a whole-number DPI.")
            continue
        if 72 <= dpi <= 2400:
            return dpi
        print("‼️  Custom DPI must be between 72 and 2400.")


# ── TagIntegrator ────────────────────────────────────────────────────────────

class TagIntegrator:

    def __init__(self) -> None:
        self.session = requests.Session()

        # Per-service pacers
        self.pace = {
            "e621":     Pacer(E621_INTERVAL),
            "inkbunny": Pacer(INKBUNNY_INTERVAL),
            "danbooru": Pacer(DANBOORU_INTERVAL),
            "gelbooru": Pacer(GELBOORU_INTERVAL),
            "fluffle":  Pacer(FLUFFLE_INTERVAL),
            "saucenao": Pacer(SAUCENAO_INTERVAL),
        }

        # Fluffle
        self.fluffle_api  = "https://api.fluffle.xyz/v1/search"
        self.headers_fluf = {"User-Agent": "HydrusIntegrator/5.0 (Fluffle+e621+InkBunny+SauceNAO)"}

        # e621
        self.e621_username = ""
        self.e621_api_key  = ""
        self.headers_e6: Dict[str, str] = {}
        self.has_e621 = False
        self._pool_cache: Dict[int, Dict] = {}   # pool_id → pool JSON

        # InkBunny
        self.ib_username = ""
        self.ib_password = ""
        self.ib_sid = ""
        self.has_inkbunny = False

        # Danbooru
        self.danbooru_username = ""
        self.danbooru_api_key  = ""
        self.has_danbooru = False
        self.danbooru_anon = False   # set True after a 401/403 → drop auth

        # Gelbooru
        self.gelbooru_user_id = ""
        self.gelbooru_api_key = ""
        self.has_gelbooru = False

        # SauceNAO
        self.saucenao_api_key = ""
        self.headers_saucenao: Dict[str, str] = {}
        self.has_saucenao = False
        self.saucenao_exhausted = False   # set True when the daily quota runs out

        # Hydrus Client API (optional output sink — skip sidecars when on)
        self.hydrus_api_url = ""
        self.hydrus_access_key = ""
        self.hydrus_tag_service_name = "downloader tags"
        self.hydrus_tag_service_key = ""
        self.hydrus_import = True
        self.hydrus_also_sidecars = False
        self.hydrus_results_page_name = "FurTag Results"
        self.hydrus_results_page_enabled = False
        self._hydrus_results_page_key = ""
        self.hydrus_already_tagged_page_name = "Already Tagged"
        self.hydrus_already_tagged_page_enabled = False
        self.has_hydrus = False
        self._hydrus_lock = threading.Lock()  # serialise API writes (hash + perc)

    # ── Credential loading ───────────────────────────────────────────────────

    @staticmethod
    def _read_kv(creds: Path) -> Dict[str, str]:
        cfg: Dict[str, str] = {}
        for line in creds.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = map(str.strip, line.split("=", 1))
                cfg[k.lower()] = v
        return cfg

    def load_credentials(self, creds: Path) -> None:
        """Load every source's credentials from a single credentials.txt.
        Any missing/incomplete set just disables that source."""
        print(f"🔑 Loading credentials from {creds.name}")
        if not creds.exists():
            print("‼️  Missing credentials.txt – all API sources disabled.")
            return

        cfg = self._read_kv(creds)
        self._init_e621(cfg)
        self._init_inkbunny(cfg)
        self._init_danbooru(cfg)
        self._init_gelbooru(cfg)
        self._init_saucenao(cfg)
        self._init_hydrus(cfg)

    def _init_e621(self, cfg: Dict[str, str]) -> None:
        self.e621_username = cfg.get("e621_username", "")
        self.e621_api_key  = cfg.get("e621_api_key", "")
        if not (self.e621_username and self.e621_api_key):
            print("‼️  e621 credentials incomplete – e621 disabled.")
            return
        self.headers_e6 = {
            "User-Agent": f"HydrusIntegrator/5.0 (by {self.e621_username} on e621)"
        }
        print(f"✅ e621 credentials loaded for {self.e621_username}")
        self.has_e621 = True

    def _init_inkbunny(self, cfg: Dict[str, str]) -> None:
        self.ib_username = cfg.get("inkbunny_username", "")
        self.ib_password = cfg.get("inkbunny_password", "")
        if not (self.ib_username and self.ib_password):
            print("‼️  InkBunny credentials incomplete – InkBunny disabled.")
            return
        if self.inkbunny_login():
            self.has_inkbunny = True

    def _init_danbooru(self, cfg: Dict[str, str]) -> None:
        self.danbooru_username = cfg.get("danbooru_username", "")
        self.danbooru_api_key  = cfg.get("danbooru_api_key", "")
        if not (self.danbooru_username and self.danbooru_api_key):
            print("‼️  Danbooru credentials incomplete – Danbooru disabled.")
            return
        print(f"✅ Danbooru credentials loaded for {self.danbooru_username}")
        self.has_danbooru = True

    def _init_gelbooru(self, cfg: Dict[str, str]) -> None:
        self.gelbooru_user_id = cfg.get("gelbooru_user_id", "")
        self.gelbooru_api_key = cfg.get("gelbooru_api_key", "")
        if not (self.gelbooru_user_id and self.gelbooru_api_key):
            print("‼️  Gelbooru credentials incomplete – Gelbooru disabled.")
            return
        print("✅ Gelbooru credentials loaded")
        self.has_gelbooru = True

    def _init_saucenao(self, cfg: Dict[str, str]) -> None:
        self.saucenao_api_key = cfg.get("sauce_nao_api_key", "")
        if not self.saucenao_api_key:
            print("‼️  No sauce_nao_api_key found – SauceNAO fallback disabled.")
            return
        self.headers_saucenao = {"User-Agent": "HydrusIntegrator/5.0 (SauceNAO)"}
        print("✅ SauceNAO API key loaded")
        self.has_saucenao = True

    def _init_hydrus(self, cfg: Dict[str, str]) -> None:
        """Optional Client API sink. Missing/unreachable → sidecars only."""
        url = (cfg.get("hydrus_api_url") or cfg.get("hydrus_url") or "").rstrip("/")
        key = (cfg.get("hydrus_access_key") or cfg.get("hydrus_api_key") or "").strip()
        if not (url and key):
            return  # silent — Hydrus API is optional

        self.hydrus_api_url = url
        self.hydrus_access_key = key
        self.hydrus_tag_service_name = (
            cfg.get("hydrus_tag_service") or cfg.get("hydrus_tag_service_name")
            or "downloader tags"
        ).strip()
        self.hydrus_import = _truthy(cfg.get("hydrus_import", "true"), default=True)
        self.hydrus_also_sidecars = _truthy(
            cfg.get("hydrus_also_sidecars", "false"), default=False)
        page_setting = cfg.get("hydrus_results_page", "FurTag Results").strip()
        self.hydrus_results_page_name = page_setting or "FurTag Results"
        page_requested = page_setting.lower() not in {"", "0", "false", "no", "off"}
        old_page_setting = cfg.get(
            "hydrus_already_tagged_page", "Already Tagged").strip()
        self.hydrus_already_tagged_page_name = old_page_setting or "Already Tagged"
        old_page_requested = old_page_setting.lower() not in {
            "", "0", "false", "no", "off"}

        try:
            r = self.session.get(
                f"{self.hydrus_api_url}/verify_access_key",
                headers=self._hydrus_headers(),
                timeout=10,
            )
            if r.status_code != 200:
                print(f"‼️  Hydrus API rejected access key (HTTP {r.status_code}) – "
                      f"sidecars only.")
                return
            access = r.json()
            permissions = access.get("basic_permissions") or []
            can_manage_pages = access.get("permits_everything", False) or 4 in permissions
            self.hydrus_results_page_enabled = page_requested and can_manage_pages
            self.hydrus_already_tagged_page_enabled = (
                old_page_requested and can_manage_pages)
            if (page_requested or old_page_requested) and not can_manage_pages:
                print("⚠️  Hydrus pages disabled – access key needs Manage Pages permission.")
            svc_key = self._hydrus_resolve_tag_service(self.hydrus_tag_service_name)
            if not svc_key:
                print(f"‼️  Hydrus tag service '{self.hydrus_tag_service_name}' not found – "
                      f"sidecars only.")
                return
            self.hydrus_tag_service_key = svc_key
            self.has_hydrus = True
            print(f"✅ Hydrus Client API → {self.hydrus_api_url}  "
                  f"[{self.hydrus_tag_service_name}]  ({self.hydrus_mode_desc()})")
        except requests.RequestException as e:
            print(f"‼️  Hydrus API unreachable ({e}) – sidecars only. "
                  f"Is the client running with the API enabled?")

    def hydrus_mode_desc(self) -> str:
        """e.g. "import+tag" / "tag-only + sidecars" — used in startup banners."""
        mode = "import+tag" if self.hydrus_import else "tag-only"
        extra = " + sidecars" if self.hydrus_also_sidecars else ""
        return f"{mode}{extra}"

    def _hydrus_headers(self) -> Dict[str, str]:
        return {
            "Hydrus-Client-API-Access-Key": self.hydrus_access_key,
            "User-Agent": "FurTag/1.0 (Hydrus Client API)",
        }

    def _hydrus_resolve_tag_service(self, name_or_key: str) -> str:
        """Map a tag service display name (or raw key) to its service_key."""
        r = self.session.get(
            f"{self.hydrus_api_url}/get_services",
            headers=self._hydrus_headers(),
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()

        # Normalize both the modern services_v2 list and the legacy services
        # object into one (name, type, service_key) iterable.
        services = data.get("services_v2")
        if isinstance(services, list):
            entries = [(s.get("name") or "", s.get("type"), s.get("service_key") or "")
                       for s in services if isinstance(s, dict)]
        else:
            legacy = data.get("services") or {}
            entries = [(sname, (sinfo or {}).get("type"), (sinfo or {}).get("service_key") or "")
                       for sname, sinfo in legacy.items()]

        if any(key == name_or_key for _, _, key in entries):
            return name_or_key  # already a raw service_key

        # Prefer a local tag service (type 5) on a name collision.
        want = name_or_key.lower()
        matches = [(typ, key) for name, typ, key in entries if name.lower() == want]
        matches.sort(key=lambda m: m[0] != 5)
        return matches[0][1] if matches else ""

    @property
    def write_sidecars(self) -> bool:
        """Sidecars when Hydrus API is off, or when hydrus_also_sidecars is set."""
        return (not self.has_hydrus) or self.hydrus_also_sidecars

    def any_source(self) -> bool:
        return any((self.has_e621, self.has_inkbunny, self.has_danbooru,
                    self.has_gelbooru, self.has_saucenao))

    def enabled_hash_services(self) -> List[str]:
        return [s for s, on in (("e621", self.has_e621),
                                ("inkbunny", self.has_inkbunny),
                                ("danbooru", self.has_danbooru),
                                ("gelbooru", self.has_gelbooru)) if on]

    # ── Thumbnail / MD5 helpers ──────────────────────────────────────────────

    @staticmethod
    def _thumb_size(w: int, h: int, tgt: int) -> Tuple[int, int]:
        return (round(tgt / h * w), tgt) if w > h else (tgt, round(tgt / w * h))

    def _prepare_thumb(self, img: Path) -> Optional[BytesIO]:
        try:
            im = Image.open(img)
            if im.mode not in ("RGB", "RGBA", "L"):
                im = im.convert("RGB")   # CMYK / P / etc. don't save cleanly to PNG
            im.thumbnail(self._thumb_size(*im.size, THUMB_MAX))
            buf = BytesIO()
            im.save(buf, "PNG")
            buf.seek(0)
            return buf
        except Exception as e:
            notify(f"❌ Pillow failed on {img.name}: {e}")
            return None

    @staticmethod
    def _hash_local(fp: Path, algo: str) -> Optional[str]:
        h = hashlib.new(algo)
        try:
            with fp.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            notify(f"❌ {algo.upper()} failed on {fp.name}: {e}")
            return None

    @classmethod
    def _md5_local(cls, fp: Path) -> Optional[str]:
        return cls._hash_local(fp, "md5")

    @classmethod
    def _sha256_local(cls, fp: Path) -> Optional[str]:
        return cls._hash_local(fp, "sha256")

    @staticmethod
    def _md5_from_url(url: str) -> str:
        m = re.search(r"([0-9a-fA-F]{32})", url or "")
        return m.group(1).lower() if m else ""

    @staticmethod
    def _post_id_from_url(url: str) -> str:
        # matches e621/danbooru "/posts/N" and e621's legacy "/post/show/N"
        m = re.search(r"/posts?(?:/show)?/(\d+)", url or "")
        return m.group(1) if m else ""

    # ── e621 API ─────────────────────────────────────────────────────────────

    def _e621_get(self, url: str) -> Optional[Dict]:
        """GET from e621 with auth (rate-paced). Returns parsed JSON or None."""
        self.pace["e621"].wait()
        try:
            r = self.session.get(
                url, headers=self.headers_e6,
                auth=(self.e621_username, self.e621_api_key), timeout=15,
            )
            if r.status_code == 429:
                notify("⚠️  e621 rate limit (429) – backing off 10s")
                self.pace["e621"].backoff(10)
                return None
            if r.status_code != 200:
                notify(f"⚠️  e621 returned {r.status_code} for {url}")
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ e621 request failed: {e}")
            return None

    def e621_lookup_by_md5(self, md5: str) -> Tuple[Set[str], Set[str]]:
        if not md5 or not self.has_e621:
            return set(), set()
        data = self._e621_get(f"https://e621.net/posts.json?tags=md5:{md5}")
        posts = data.get("posts", []) if data else []
        return self._parse_e6_post(posts[0]) if posts else (set(), set())

    def e621_lookup_by_id(self, pid: str) -> Tuple[Set[str], Set[str]]:
        if not pid or not self.has_e621:
            return set(), set()
        data = self._e621_get(f"https://e621.net/posts/{pid}.json")
        post = data.get("post", {}) if data else {}
        return self._parse_e6_post(post) if post else (set(), set())

    def _parse_e6_post(self, post: Dict) -> Tuple[Set[str], Set[str]]:
        """Convert an e621 post into (tags, urls). Includes pool/comic tags."""
        tags: Set[str] = {"site:e621"}
        urls: Set[str] = set()

        post_id = post.get("id")
        if post_id:
            urls.add(f"https://e621.net/posts/{post_id}")

        for ns, lst in post.get("tags", {}).items():
            if not isinstance(lst, list):
                continue
            for raw_tag in lst:
                clean = raw_tag.replace("_", " ").strip()
                if not clean:
                    continue
                if ns == "artist":
                    tags.add(f"creator:{clean}")
                elif ns == "copyright":
                    tags.add(f"series:{clean}")
                elif ns in ("general", "meta"):
                    tags.add(clean)
                elif ns == "invalid":
                    continue
                else:                       # character, species, lore, ...
                    tags.add(f"{ns}:{clean}")

        # Pool / comic detection
        pool_ids = post.get("pools", []) or []
        if pool_ids and post_id:
            tags |= self._e621_pool_tags(pool_ids, post_id)

        return tags, urls

    def _e621_pool_tags(self, pool_ids: List[int], post_id: int) -> Set[str]:
        """
        Resolve e621 pools to comic:/page: tags.
        - comic:<pool name>  for every pool the post belongs to
        - page:<n>           numeric position within the (first) pool
        Pools are cached so a multi-page comic isn't re-fetched per page.
        """
        tags: Set[str] = set()
        first_page_set = False

        for pid in pool_ids:
            pool = self._pool_cache.get(pid)
            if pool is None:
                data = self._e621_get(f"https://e621.net/pools/{pid}.json")
                pool = data if isinstance(data, dict) else {}
                self._pool_cache[pid] = pool

            name = (pool.get("name") or "").replace("_", " ").strip()
            if name:
                tags.add(f"comic:{name}")

            if not first_page_set:
                post_ids = pool.get("post_ids", []) or []
                if post_id in post_ids:
                    tags.add(f"page:{post_ids.index(post_id) + 1}")
                    first_page_set = True

        return tags

    # ── InkBunny API ─────────────────────────────────────────────────────────

    def inkbunny_login(self) -> bool:
        self.pace["inkbunny"].wait()
        try:
            r = self.session.get(
                "https://inkbunny.net/api_login.php",
                params={"username": self.ib_username, "password": self.ib_password},
                timeout=15,
            )
            data = r.json() if r.status_code == 200 else {}
            sid = data.get("sid", "")
            if sid:
                self.ib_sid = sid
                print(f"✅ InkBunny logged in as {self.ib_username}")
                return True
            notify(f"‼️  InkBunny login failed: {data.get('error_message', data)}")
            return False
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ InkBunny login request failed: {e}")
            return False

    def inkbunny_lookup_by_md5(self, md5: str) -> Tuple[Set[str], Set[str]]:
        """Search InkBunny by file MD5, then pull keywords from matching submissions."""
        if not md5 or not self.has_inkbunny:
            return set(), set()
        sub_ids = self._inkbunny_search_md5(md5)
        if not sub_ids:
            return set(), set()
        return self._inkbunny_submission_tags(sub_ids)

    def _inkbunny_search_md5(self, md5: str, _retry: bool = True) -> List[str]:
        """InkBunny's `md5` param is a boolean toggle — the hash goes in `text`,
        with md5=yes to search file checksums instead of keywords."""
        self.pace["inkbunny"].wait()
        try:
            r = self.session.get(
                "https://inkbunny.net/api_search.php",
                params={"sid": self.ib_sid, "text": md5, "md5": "yes"},
                timeout=15,
            )
            data = r.json() if r.status_code == 200 else {}
            # Expired/invalid session → re-login once and retry.
            if data.get("error_code") in ("2", 2) and _retry and self.inkbunny_login():
                return self._inkbunny_search_md5(md5, _retry=False)
            return [str(s.get("submission_id")) for s in data.get("submissions", [])
                    if s.get("submission_id")]
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ InkBunny search failed: {e}")
            return []

    def _inkbunny_submission_tags(self, sub_ids: List[str]) -> Tuple[Set[str], Set[str]]:
        tags: Set[str] = set()
        urls: Set[str] = set()
        self.pace["inkbunny"].wait()
        try:
            r = self.session.get(
                "https://inkbunny.net/api_submissions.php",
                params={"sid": self.ib_sid,
                        "submission_ids": ",".join(sub_ids),
                        "show_description": "no"},
                timeout=20,
            )
            data = r.json() if r.status_code == 200 else {}
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ InkBunny submissions fetch failed: {e}")
            return tags, urls

        for sub in data.get("submissions", []):
            tags.add("site:inkbunny")
            sub_id = sub.get("submission_id")
            if sub_id:
                urls.add(f"https://inkbunny.net/s/{sub_id}")

            username = (sub.get("username") or "").strip()
            if username:
                tags.add(f"creator:{username}")

            for kw in sub.get("keywords", []):
                name = (kw.get("keyword_name") or "").replace("_", " ").strip()
                name = EMOJI_PATTERN.sub("", name).strip()
                if name:
                    tags.add(name)   # InkBunny keywords are freeform/un-namespaced

        return tags, urls

    # ── Danbooru API ─────────────────────────────────────────────────────────

    def _danbooru_get(self, url: str, params: Dict) -> Optional[object]:
        auth = {} if self.danbooru_anon else {"login": self.danbooru_username,
                                              "api_key": self.danbooru_api_key}
        self.pace["danbooru"].wait()
        try:
            r = self.session.get(
                url, params={**params, **auth},
                headers={"User-Agent": "HydrusIntegrator/5.0"}, timeout=15,
            )
            # Key rejected/unprivileged → drop auth and use anonymous reads
            # (Danbooru allows anonymous md5 lookups) for the rest of the run.
            if r.status_code in (401, 403) and not self.danbooru_anon:
                notify("⚠️  Danbooru auth rejected – falling back to anonymous access.")
                self.danbooru_anon = True
                return self._danbooru_get(url, params)
            if r.status_code != 200:
                notify(f"⚠️  Danbooru returned {r.status_code}")
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ Danbooru request failed: {e}")
            return None

    def danbooru_lookup_by_md5(self, md5: str) -> Tuple[Set[str], Set[str]]:
        if not md5 or not self.has_danbooru:
            return set(), set()
        data = self._danbooru_get("https://danbooru.donmai.us/posts.json",
                                  {"tags": f"md5:{md5}", "limit": 1})
        posts = data if isinstance(data, list) else []
        return self._parse_danbooru_post(posts[0]) if posts else (set(), set())

    def danbooru_lookup_by_id(self, pid: str) -> Tuple[Set[str], Set[str]]:
        if not pid or not self.has_danbooru:
            return set(), set()
        data = self._danbooru_get(f"https://danbooru.donmai.us/posts/{pid}.json", {})
        return self._parse_danbooru_post(data) if isinstance(data, dict) else (set(), set())

    @staticmethod
    def _parse_danbooru_post(post: Dict) -> Tuple[Set[str], Set[str]]:
        tags: Set[str] = {"site:danbooru"}
        urls: Set[str] = set()

        pid = post.get("id")
        if pid:
            urls.add(f"https://danbooru.donmai.us/posts/{pid}")
        src = (post.get("source") or "").strip()
        if src.startswith(("http://", "https://")):
            urls.add(src)

        namespaced = {"tag_string_artist": "creator:",
                      "tag_string_character": "character:",
                      "tag_string_copyright": "series:"}
        for field_name, prefix in namespaced.items():
            for t in (post.get(field_name) or "").split():
                clean = t.replace("_", " ").strip()
                if clean:
                    tags.add(f"{prefix}{clean}")
        for field_name in ("tag_string_general", "tag_string_meta"):
            for t in (post.get(field_name) or "").split():
                clean = t.replace("_", " ").strip()
                if clean:
                    tags.add(clean)
        return tags, urls

    # ── Gelbooru API ─────────────────────────────────────────────────────────

    def _gelbooru_get(self, params: Dict) -> Optional[object]:
        self.pace["gelbooru"].wait()
        try:
            r = self.session.get(
                "https://gelbooru.com/index.php",
                params={**params, "api_key": self.gelbooru_api_key,
                        "user_id": self.gelbooru_user_id},
                headers={"User-Agent": "HydrusIntegrator/5.0"},
                timeout=15,
            )
            if r.status_code != 200:
                notify(f"⚠️  Gelbooru returned {r.status_code}")
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ Gelbooru request failed: {e}")
            return None

    def gelbooru_lookup_by_md5(self, md5: str) -> Tuple[Set[str], Set[str]]:
        if not md5 or not self.has_gelbooru:
            return set(), set()
        return self._gelbooru_lookup({"tags": f"md5:{md5}"})

    def gelbooru_lookup_by_id(self, pid: str) -> Tuple[Set[str], Set[str]]:
        if not pid or not self.has_gelbooru:
            return set(), set()
        return self._gelbooru_lookup({"id": pid})

    def _gelbooru_lookup(self, query: Dict) -> Tuple[Set[str], Set[str]]:
        data = self._gelbooru_get(
            {"page": "dapi", "s": "post", "q": "index", "json": "1", **query})
        posts = data.get("post", []) if isinstance(data, dict) else data
        if isinstance(posts, dict):
            posts = [posts]
        return self._parse_gelbooru_post(posts[0]) if posts else (set(), set())

    def _parse_gelbooru_post(self, post: Dict) -> Tuple[Set[str], Set[str]]:
        tags: Set[str] = {"site:gelbooru"}
        urls: Set[str] = set()

        pid = post.get("id")
        if pid:
            urls.add(f"https://gelbooru.com/index.php?page=post&s=view&id={pid}")
        src = (post.get("source") or "").strip()
        if src.startswith(("http://", "https://")):
            urls.add(src)

        names = [t for t in (post.get("tags") or "").split() if t]
        cats = self._gelbooru_categorize(names)   # name → type int ({} on failure)
        for name in names:
            clean = name.replace("_", " ").strip()
            if not clean:
                continue
            try:
                prefix = GELBOORU_TYPE.get(int(cats.get(name)), "")
            except (TypeError, ValueError):
                prefix = ""   # uncategorized → unnamespaced general tag
            tags.add(f"{prefix}{clean}")
        return tags, urls

    def _gelbooru_categorize(self, names: List[str]) -> Dict[str, object]:
        """One batched call mapping tag name → Gelbooru type int. {} on failure."""
        if not names:
            return {}
        data = self._gelbooru_get(
            {"page": "dapi", "s": "tag", "q": "index", "json": "1",
             "names": " ".join(names)})
        tags = data.get("tag", []) if isinstance(data, dict) else data
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            return {}
        return {t.get("name"): t.get("type")
                for t in tags if isinstance(t, dict) and t.get("name") is not None}

    # ── Fluffle API ──────────────────────────────────────────────────────────

    def fluffle_search(self, img: Path) -> Optional[Dict]:
        thumb = self._prepare_thumb(img)
        if not thumb:
            return None
        self.pace["fluffle"].wait()
        try:
            r = self.session.post(
                self.fluffle_api, headers=self.headers_fluf,
                files={"file": ("image.png", thumb, "image/png")},
                data={"includeNsfw": True, "limit": 32}, timeout=30,
            )
            if r.status_code == 429:
                notify("⚠️  Fluffle rate limit (429) – backing off 30s")
                self.pace["fluffle"].backoff(30)
                return None
            if r.status_code != 200:
                notify(f"⚠️  Fluffle returned {r.status_code}")
                return None
            return r.json()
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ Fluffle request failed: {e}")
            return None

    def find_best_exact_match(self, j: Dict) -> Tuple[Set[str], Set[str], str, str]:
        """
        Parse Fluffle results. Priority: exact-e621 > exact-other > tossUp-e621
        (the last only when FLUFFLE_TOSSUP_E621, since we re-query e621 by ID).
        Returns (tags, urls, md5_from_url, post_id).
        """
        results = j.get("results") if j else None
        if not results or not isinstance(results, list):
            return set(), set(), "", ""

        def is_e621(r: Dict) -> bool:
            return ("e621" in r.get("platform", "").lower()
                    or "e621.net" in r.get("location", ""))

        exact_e621 = exact_other = tossup_e621 = None
        for result in results:
            match = result.get("match")
            if match == "exact":
                if is_e621(result):
                    exact_e621 = exact_e621 or result
                elif exact_other is None:
                    exact_other = result
            elif match == "tossUp" and FLUFFLE_TOSSUP_E621 and is_e621(result):
                tossup_e621 = tossup_e621 or result

        chosen = exact_e621 or exact_other or tossup_e621
        if not chosen:
            return set(), set(), "", ""

        platform = chosen.get("platform", "")
        loc      = chosen.get("location", "")
        tags: Set[str] = set()
        urls: Set[str] = set()

        for c in chosen.get("credits", []):
            name = EMOJI_PATTERN.sub("", c.get("name", "")).strip()
            if name:
                tags.add(f"creator:{name}")
        if platform:
            platform_clean = EMOJI_PATTERN.sub("", platform).strip()
            if platform_clean:
                tags.add(f"site:{platform_clean}")
        if loc:
            urls.add(loc)

        return tags, urls, self._md5_from_url(loc), self._post_id_from_url(loc)

    # ── SauceNAO API ─────────────────────────────────────────────────────────

    def saucenao_search(self, img: Path,
                        similarity_threshold: float = MIN_SIMILARITY
                        ) -> Tuple[Optional[str], Optional[str], Set[str], Set[str]]:
        """
        Returns (service, post_id, own_tags, own_urls).
        (service, post_id) is set only when a result above SAUCENAO_AUTH_SIMILARITY
        resolves to a booru we hold credentials for — the caller should then
        re-query that booru for the authoritative tag set rather than trusting
        SauceNAO's own thinner tags. own_tags/own_urls are the fallback for
        matches that resolve to sites we can't re-query.
        """
        if not self.has_saucenao or self.saucenao_exhausted:
            return None, None, set(), set()
        thumb = self._prepare_thumb(img)
        if not thumb:
            return None, None, set(), set()

        self.pace["saucenao"].wait()
        try:
            r = self.session.post(
                "https://saucenao.com/search.php",
                headers=self.headers_saucenao,
                files={"file": ("image.png", thumb, "image/png")},
                data={"api_key": self.saucenao_api_key, "output_type": "2",
                      "numres": "16", "db": "999", "testmode": "0"},
                timeout=30,
            )
            if r.status_code == 429:
                notify("⚠️  SauceNAO rate limit (429) – backing off 30s")
                self.pace["saucenao"].backoff(30)
                return None, None, set(), set()
            if r.status_code != 200:
                return None, None, set(), set()
            j = r.json()
            self._saucenao_check_quota(j.get("header", {}))
            if j.get("header", {}).get("status", 0) != 0:
                return None, None, set(), set()
            service, post_id = self._saucenao_best_authoritative(j, SAUCENAO_AUTH_SIMILARITY)
            tags, urls = self._extract_saucenao_tags(j, similarity_threshold)
            return service, post_id, tags, urls
        except (requests.RequestException, ValueError):
            return None, None, set(), set()

    def _saucenao_check_quota(self, header: Dict) -> None:
        """Read SauceNAO's own quota counters and self-throttle/disable as needed."""
        # Auto-adapt the pace to the account's actual short-window allowance:
        # SauceNAO reports `short_limit` (calls per 30s) on every response — 4 for
        # free, higher for enhanced/donor accounts. Spacing calls to 30/limit means
        # a paid account speeds up automatically, and a lapsed one slows back down,
        # with no config key to keep in sync. Only the single perceptual worker
        # thread ever calls SauceNAO, so mutating the pacer here is race-free.
        try:
            short_limit = int(header.get("short_limit"))
        except (TypeError, ValueError):
            short_limit = None
        if short_limit and short_limit > 0:
            self.pace["saucenao"].interval = 30.0 / short_limit

        try:
            short_remaining = int(header.get("short_remaining"))
        except (TypeError, ValueError):
            short_remaining = None
        try:
            long_remaining = int(header.get("long_remaining"))
        except (TypeError, ValueError):
            long_remaining = None
        if short_remaining is not None and short_remaining <= 0:
            self.pace["saucenao"].backoff(30)   # short window exhausted
        if long_remaining is not None and long_remaining <= 0:
            self.saucenao_exhausted = True
            notify("⚠️  SauceNAO daily search limit reached – disabling SauceNAO for this run.")

    def _saucenao_best_authoritative(self, json_data: Dict, threshold: float
                                     ) -> Tuple[Optional[str], Optional[str]]:
        """Highest-confidence result above threshold that carries a booru ID we
        can re-query (and hold creds for). Returns (service, id) or (None, None).
        Preference order e621 → danbooru → gelbooru when a result has several."""
        candidates = [("e621", "e621_id", self.has_e621),
                      ("danbooru", "danbooru_id", self.has_danbooru),
                      ("gelbooru", "gelbooru_id", self.has_gelbooru)]
        best: Tuple[Optional[str], Optional[str]] = (None, None)
        best_sim = threshold
        for result in json_data.get("results", []):
            try:
                sim = float(result.get("header", {}).get("similarity", 0))
            except (ValueError, TypeError):
                continue
            if sim < best_sim:
                continue
            data = result.get("data", {})
            for service, field_name, enabled in candidates:
                if not enabled:
                    continue
                rid = data.get(field_name)
                if isinstance(rid, list):
                    rid = rid[0] if rid else None
                if rid:
                    best, best_sim = (service, str(rid)), sim
                    break
        return best

    def _authoritative_lookup(self, service: str, post_id: str
                              ) -> Tuple[Set[str], Set[str]]:
        if service == "e621":
            return self.e621_lookup_by_id(post_id)
        if service == "danbooru":
            return self.danbooru_lookup_by_id(post_id)
        if service == "gelbooru":
            return self.gelbooru_lookup_by_id(post_id)
        return set(), set()

    @staticmethod
    def _is_url(text: str) -> bool:
        if not text:
            return False
        t = text.lower()
        return (t.startswith(("http://", "https://", "www.")) or "/" in t
                or ".com" in t or ".net" in t or ".org" in t)

    @staticmethod
    def _sim(result: Dict) -> float:
        try:
            return float(result.get("header", {}).get("similarity", 0))
        except (ValueError, TypeError):
            return 0.0

    def _extract_saucenao_tags(self, json_data: Dict,
                               similarity_threshold: float
                               ) -> Tuple[Set[str], Set[str]]:
        """URLs are gathered from *all* qualifying results (max source links),
        but tags come from only the single best (highest-similarity) result —
        merging tags across results produces a Frankenstein of unrelated sites."""
        results = [r for r in json_data.get("results", [])
                   if self._sim(r) >= similarity_threshold]

        urls: Set[str] = set()
        for r in results:
            for url in r.get("data", {}).get("ext_urls", []):
                if isinstance(url, str) and url.strip().startswith(("http://", "https://")):
                    urls.add(url.strip())

        tags = self._saucenao_result_tags(max(results, key=self._sim)) if results else set()
        return tags, urls

    def _saucenao_result_tags(self, result: Dict) -> Set[str]:
        tags: Set[str] = set()
        data = result.get("data", {})

        site = self._extract_site_name(result.get("header", {}).get("index_name", ""))
        if site and site != "unknown_source" and not site.startswith("index"):
            tags.add(f"site:{site}")

        for field_name in ("member_name", "author_name", "author", "artist", "creator", "creator_name"):
            c = data.get(field_name)
            if isinstance(c, list):
                c = c[0] if c else ""
            if c and isinstance(c, str) and not self._is_url(c):
                clean = self._clean_tag_text(c)
                if clean:
                    tags.add(f"creator:{clean}")
                    break

        for field_name in ("title", "jp_name", "eng_name"):
            t = data.get(field_name, "")
            if t and isinstance(t, str) and not self._is_url(t):
                clean = self._clean_tag_text(t)
                if clean:
                    tags.add(f"title:{clean}")
                    break

        # NOTE: 'source' is excluded — for many indexes (e-hentai) it's the
        # gallery title or a URL, not a series.
        for field_name in ("material", "anime_name", "manga_name"):
            sname = data.get(field_name, "")
            if sname and isinstance(sname, str) and not self._is_url(sname):
                clean = self._clean_tag_text(sname)
                if clean:
                    tags.add(f"series:{clean}")
                    break

        for field_name in ("characters", "character"):
            chars = data.get(field_name, "")
            if isinstance(chars, str) and chars:
                # Split on commas/semicolons only — NOT on "and" or "/", which
                # live inside disambiguators like "calvin (calvin and hobbes)".
                for ch in re.split(r'\s*[,;]\s*', chars):
                    clean = self._clean_tag_text(ch)
                    if clean:
                        tags.add(f"character:{clean}")
                break
            if isinstance(chars, list):
                for ch in chars:
                    if isinstance(ch, str):
                        clean = self._clean_tag_text(ch)
                        if clean:
                            tags.add(f"character:{clean}")
                break

        return tags

    @staticmethod
    def _clean_tag_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r'<[^>]+>', '', text)         # strip HTML
        text = text.replace('_', ' ')                # underscores → spaces (Hydrus style)
        text = re.sub(r'[^\w\-.:()&\s]', '', text)   # drop junk, keep spaces & word chars
        text = re.sub(r'\s+', ' ', text).strip()     # collapse whitespace
        return text

    @staticmethod
    def _extract_site_name(index_name: str) -> str:
        if not index_name:
            return ""
        site_mappings = {
            "pixiv": "pixiv", "danbooru": "danbooru", "gelbooru": "gelbooru",
            "e-hentai": "e-hentai", "exhentai": "e-hentai", "sankaku": "sankaku_channel",
            "yandere": "yande.re", "konachan": "konachan", "furaffinity": "furaffinity",
            "deviantart": "deviantart", "twitter": "twitter", "artstation": "artstation",
            "nijie": "nijie", "e621": "e621", "inkbunny": "inkbunny",
        }
        index_lower = index_name.lower()
        for key, value in site_mappings.items():
            if key in index_lower:
                return value
        paren = re.search(r'\(([^)]+)\)', index_name)
        if paren:
            site_text = paren.group(1).lower().strip()
            for key, value in site_mappings.items():
                if key in site_text:
                    return value
        clean = re.sub(r'index\s*\d+:\s*', '', index_lower)
        clean = re.sub(r'\s*-\s*[a-f0-9]{40,}.*$', '', clean)
        clean = re.sub(r'[^\w\s]', '', clean)
        clean = re.sub(r'\s+', '_', clean.strip())
        if clean.startswith("index") or len(clean) > 30:
            return ""
        return clean

    # ── Sidecar I/O ──────────────────────────────────────────────────────────

    @staticmethod
    def tag_sidecar_path(media: Path) -> Path:
        return media.with_suffix(media.suffix + ".txt")

    @staticmethod
    def url_sidecar_path(media: Path) -> Path:
        return media.with_suffix(media.suffix + ".urls.txt")

    def has_sidecar(self, media: Path) -> bool:
        return self.tag_sidecar_path(media).exists()

    @staticmethod
    def _append_lines(path: Path, lines: Set[str]) -> int:
        """Append only lines not already present. Returns count written."""
        try:
            existing = set(path.read_text("utf-8").splitlines()) if path.exists() else set()
            diff = sorted(l for l in lines if l and l not in existing)
            if not diff:
                return 0
            with path.open("a", encoding="utf-8") as f:
                for line in diff:
                    f.write(line + "\n")
            return len(diff)
        except Exception as e:
            notify(f"❌ Write failed for {path.name}: {e}")
            return 0

    @staticmethod
    def _pdf_page_base_tags(media: Path) -> Set[str]:
        """comic:/page: for a PDF-rendered page named like ``STEM PAGEN.PNG``."""
        tags: Set[str] = {f"comic:{media.parent.name}"}
        m = re.search(r"PAGE(\d+)", media.name, re.I)
        if m:
            tags.add(f"page:{int(m.group(1))}")
        return tags

    def write_results(self, media: Path, tags: Set[str], urls: Set[str]) -> None:
        # Drop "artist unknown / anonymous" placeholder tags from every source
        # before writing — they're noise in a Hydrus library.
        tags = {t for t in tags if not _is_junk_tag(t)}
        urls = {u for u in urls if u}

        if self.has_hydrus and (tags or urls):
            self._hydrus_push(media, tags, urls)

        if self.write_sidecars:
            if tags:
                self._append_lines(self.tag_sidecar_path(media), tags)
            if urls:
                self._append_lines(self.url_sidecar_path(media), urls)

    # ── Hydrus Client API push ───────────────────────────────────────────────

    def _hydrus_push(self, media: Path, tags: Set[str], urls: Set[str]) -> None:
        """Import (optional) + tag + associate URLs for one file. Thread-safe.

        Safety: only *adds* content (never deletes files/tags/URLs). If import
        is on and the import is refused (previously deleted, vetoed, error),
        we abort the whole push — we do NOT fall through to bare-hash tagging.
        """
        with self._hydrus_lock:
            try:
                if self.hydrus_import:
                    # Must get an accepted import (status 1/2). No hash → stop.
                    file_hash = self._hydrus_add_file(media)
                    if not file_hash:
                        return
                else:
                    # Tag-only mode: file must already live in Hydrus under this hash.
                    file_hash = self._sha256_local(media)
                    if not file_hash:
                        notify(f"❌ Hydrus: no hash for {media.name}; skipped push.")
                        return

                if tags:
                    self._hydrus_add_tags(file_hash, tags)
                if urls:
                    self._hydrus_associate_urls(file_hash, urls)
                self._hydrus_add_to_results_page(file_hash)
            except Exception as e:
                notify(f"❌ Hydrus push failed for {media.name}: {e}")

    def _hydrus_post(self, endpoint: str, body: dict, timeout: int) -> requests.Response:
        """POST to a Hydrus Client API endpoint with the standard headers."""
        return self.session.post(
            f"{self.hydrus_api_url}/{endpoint}",
            headers={**self._hydrus_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )

    def _hydrus_add_file(self, media: Path) -> Optional[str]:
        """POST /add_files/add_file by path. Returns SHA-256 hex on success/already-in."""
        try:
            r = self._hydrus_post("add_files/add_file", {"path": str(media.resolve())}, 120)
        except requests.RequestException as e:
            notify(f"❌ Hydrus import request failed for {media.name}: {e}")
            return None

        if r.status_code != 200:
            notify(f"⚠️  Hydrus import HTTP {r.status_code} for {media.name}: "
                   f"{r.text[:200]}")
            return None

        try:
            data = r.json()
        except ValueError:
            notify(f"⚠️  Hydrus import returned non-JSON for {media.name}")
            return None

        status = data.get("status")
        h = data.get("hash") or ""
        note = (data.get("note") or "").strip()
        # 1 = imported, 2 = already in db — both give us a usable hash
        if status in (1, 2) and h:
            return h
        if status == 3:
            notify(f"⚠️  Hydrus: {media.name} previously deleted"
                   + (f" ({note})" if note else "") + " — not tagging.")
            return None
        if status == 7:
            notify(f"⚠️  Hydrus vetoed {media.name}"
                   + (f": {note}" if note else ""))
            return None
        notify(f"⚠️  Hydrus import failed for {media.name} (status={status})"
               + (f": {note}" if note else ""))
        return None

    def _hydrus_add_tags(self, file_hash: str, tags: Set[str]) -> None:
        """POST /add_tags/add_tags — act like a downloader (don't override deletes)."""
        body = {
            "hash": file_hash,
            "service_keys_to_tags": {
                self.hydrus_tag_service_key: sorted(tags),
            },
            # Behave like a gallery parser: don't re-add human-deleted mappings.
            "override_previously_deleted_mappings": False,
        }
        r = self._hydrus_post("add_tags/add_tags", body, 30)
        if r.status_code != 200:
            raise RuntimeError(f"add_tags HTTP {r.status_code}: {r.text[:200]}")

    def _hydrus_associate_urls(self, file_hash: str, urls: Set[str]) -> None:
        """POST /add_urls/associate_url."""
        body = {"hash": file_hash, "urls_to_add": sorted(urls)}
        r = self._hydrus_post("add_urls/associate_url", body, 30)
        if r.status_code != 200:
            raise RuntimeError(f"associate_url HTTP {r.status_code}: {r.text[:200]}")

    def _hydrus_add_to_results_page(self, file_hash: str) -> None:
        """Silently append a known file to this run's Hydrus results page."""
        if not self.hydrus_results_page_enabled:
            return

        if not self._hydrus_results_page_key:
            body = {
                "page_type": 6,
                "page_name": self.hydrus_results_page_name,
                "hashes": [file_hash],
                "system_hash_locked": True,
                "focus_page": False,
            }
            r = self._hydrus_post("manage_pages/new_page", body, 30)
            if r.status_code != 200:
                self.hydrus_results_page_enabled = False
                notify(f"⚠️  Hydrus results page unavailable (HTTP {r.status_code}); "
                       "continuing without it.")
                return
            try:
                self._hydrus_results_page_key = r.json()["page_key"]
            except (ValueError, KeyError, TypeError):
                self.hydrus_results_page_enabled = False
                notify("⚠️  Hydrus results page returned no page key; "
                       "continuing without it.")
            return

        body = {
            "page_key": self._hydrus_results_page_key,
            "hashes": [file_hash],
        }
        r = self._hydrus_post("manage_pages/add_files", body, 30)
        if r.status_code != 200:
            self.hydrus_results_page_enabled = False
            notify(f"⚠️  Hydrus could not update the results page "
                   f"(HTTP {r.status_code}); continuing without it.")

    def _hydrus_populate_already_tagged_page(self, ledger_mgr: LedgerManager) -> int:
        """Create an unfocused page from unchanged `matched` ledger records.

        Old ledgers only contain MD5, while Hydrus page APIs require SHA-256.
        Missing SHA-256 values are calculated in parallel once and cached back
        into their ledger records. Unknown/non-local hashes are harmlessly
        omitted by Hydrus's local file-search page.
        """
        if not (self.has_hydrus and self.hydrus_already_tagged_page_enabled):
            return 0

        entries: List[Tuple[Path, Ledger, str, int, float, Optional[str]]] = []
        for ledger in ledger_mgr.touched():
            for name, rec in ledger.records.items():
                if not isinstance(rec, dict) or rec.get("status") != "matched":
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) != "matched":
                    continue
                entries.append((path, ledger, name, st.st_size, st.st_mtime,
                                ledger.sha256_for(name, st.st_size, st.st_mtime)))

        if not entries:
            return 0

        missing = [entry for entry in entries if not entry[5]]
        if missing:
            print(f"🏷️  Preparing {self.hydrus_already_tagged_page_name} page "
                  f"({len(entries)} ledger match(es))…")
            workers = min(8, max(1, os.cpu_count() or 1))
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(self._sha256_local, entry[0]): entry
                           for entry in missing}
                for future in cf.as_completed(futures):
                    path, ledger, name, size, mtime, _ = futures[future]
                    try:
                        sha256 = future.result()
                    except Exception as e:
                        notify(f"❌ SHA256 failed on {path.name}: {e}")
                        continue
                    if sha256:
                        ledger.cache_sha256(name, size, mtime, sha256)

        hashes: List[str] = []
        seen_hashes: Set[str] = set()
        for path, ledger, name, size, mtime, cached in entries:
            sha256 = cached or ledger.sha256_for(name, size, mtime)
            if sha256 and sha256 not in seen_hashes:
                hashes.append(sha256)
                seen_hashes.add(sha256)
        if not hashes:
            return 0

        batch_size = 256
        first = hashes[:batch_size]
        body = {
            "page_type": 6,
            "page_name": self.hydrus_already_tagged_page_name,
            "hashes": first,
            "system_hash_locked": True,
            "focus_page": False,
        }
        with self._hydrus_lock:
            try:
                r = self._hydrus_post("manage_pages/new_page", body, 30)
                if r.status_code != 200:
                    notify(f"⚠️  Hydrus Already Tagged page unavailable "
                           f"(HTTP {r.status_code}).")
                    return 0
                page_key = r.json()["page_key"]
                for start in range(batch_size, len(hashes), batch_size):
                    r = self._hydrus_post("manage_pages/add_files", {
                        "page_key": page_key,
                        "hashes": hashes[start:start + batch_size],
                    }, 30)
                    if r.status_code != 200:
                        notify(f"⚠️  Hydrus stopped filling Already Tagged page "
                               f"(HTTP {r.status_code}).")
                        break
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                notify(f"⚠️  Hydrus Already Tagged page failed: {e}")
                return 0
        return len(hashes)

    # ── PDF pre-pass ───────────────────────────────────────────────────────────

    def plan_pdf_renders(self, root: Path) -> Tuple[Set[Path], List[Path]]:
        """Discover PDF page folders and return the PDFs that still need rendering.

        The caller launches `render_pdf_jobs` in a background worker, while
        excluding those jobs' output folders from its initial index so a
        half-written page can never enter the pipeline.
        """
        pdfs: List[Path] = []
        for dp, dirs, files in os.walk(root):
            dirs.sort()
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                if Path(fn).suffix.lower() in PDF_EXTS:
                    pdfs.append(Path(dp) / fn)
        if not pdfs:
            return set(), []

        try:
            _import_fitz()                      # probe once before the loop
        except Exception as e:                  # PyMuPDF missing / import error
            print(f"⚠️  {len(pdfs)} PDF(s) found but PDF support is unavailable "
                  f"({e}). Install PyMuPDF to tag them; skipping for now.")
            return set(), []

        page_dirs: Set[Path] = set()
        jobs: List[Path] = []
        for pdf in pdfs:
            out_dir = pdf.parent / pdf.stem
            page_dirs.add(out_dir)
            already = out_dir.is_dir() and any(
                f.suffix.lower() == ".png" for f in out_dir.iterdir())
            if already:
                continue                        # rendered on a previous run
            jobs.append(pdf)
        return page_dirs, jobs

    def render_pdf_jobs(self, pdfs: List[Path], dpi: int) -> List[Path]:
        """Render planned PDFs serially on the dedicated background worker."""
        generated: List[Path] = []
        for pdf in pdfs:
            try:
                generated += convert_pdf(
                    pdf, pdf.parent, dpi, write_sidecars=self.write_sidecars)
            except Exception as e:
                notify(f"⚠️  Failed to render {pdf.name}: {e}")
        return generated

    # ── Index ────────────────────────────────────────────────────────────────

    @staticmethod
    def _media_kind(fn: str) -> Optional[str]:
        ext = Path(fn).suffix.lower()
        if ext in IMG_EXTS:
            return "image"
        if ext in VIDEO_EXTS:
            return "video"
        return None

    def index(self, root: Path, ledger_mgr: LedgerManager,
              pdf_page_dirs: Set[Path],
              excluded_dirs: Optional[Set[Path]] = None
              ) -> Tuple[List[FileItem], Set[Path]]:
        """Walk the tree once and return the files that actually need work,
        videos first, plus the set of directories that needed a per-file check
        (for `finalize_dir_fingerprints` to potentially seal afterwards).

        Each directory carries its own Ledger (found in-place, or created).
        Before checking any individual file, a directory whose ledger has a
        sealed fingerprint (`file_count`, `total_size`) matching the current
        listing is skipped wholesale — no stat, hash, or lookup work at all.
        Otherwise it falls back to the old per-file check: skip files with an
        existing tag sidecar, then ones the ledger already recorded as
        matched/no-match (unchanged size+mtime). PNGs inside a `pdf_page_dirs`
        folder are flagged perceptual-only and are exempt from the has-sidecar
        skip (their sidecar holds only the base comic:/page: tags) — the
        ledger alone rules them out on a re-run."""
        print("📂 Scanning folder tree…")
        media = tagged = seen = skipped_dirs = 0
        items: List[FileItem] = []
        candidate_dirs: Set[Path] = set()

        for dp, dirs, files in os.walk(root):
            dirs.sort()
            dp_path = Path(dp)
            if excluded_dirs:
                dirs[:] = [d for d in dirs if dp_path / d not in excluded_dirs]

            media_files = [fn for fn in sorted(files)
                           if not fn.startswith(".") and self._media_kind(fn)]
            if not media_files:
                continue

            dir_ledger = ledger_mgr.get(dp_path)

            stats: Dict[str, os.stat_result] = {}
            total_size = 0
            for fn in media_files:
                try:
                    st = (dp_path / fn).stat()
                except OSError:
                    continue
                stats[fn] = st
                total_size += st.st_size
            count = len(stats)

            if count and dir_ledger.fingerprint_matches(count, total_size):
                media += count
                seen += count
                skipped_dirs += 1
                continue   # whole folder unchanged since it was last fully processed

            candidate_dirs.add(dp_path)
            for fn in media_files:
                st = stats.get(fn)
                if st is None:
                    continue
                media += 1
                p = dp_path / fn
                kind = self._media_kind(fn)
                is_pdf_page = kind == "image" and p.suffix.lower() == ".png" and p.parent in pdf_page_dirs
                if self.has_sidecar(p) and not is_pdf_page:
                    tagged += 1
                    continue

                status = dir_ledger.status_for(fn, st.st_size, st.st_mtime)
                if status == "duplicate":
                    canonical = dir_ledger.records.get(fn, {}).get("duplicate_of", "")
                    if canonical and not (root / canonical).is_file():
                        status = None  # chosen copy disappeared; elect/search again
                if status in ("matched", "nomatch", "duplicate"):
                    seen += 1
                    continue

                item = FileItem(path=p, relpath=str(p.relative_to(root)),
                                size=st.st_size, mtime=st.st_mtime, kind=kind,
                                ledger=dir_ledger, perceptual_only=is_pdf_page,
                                md5=dir_ledger.md5_for(fn, st.st_size, st.st_mtime))
                items.append(item)

        # Videos first (can't reverse-image-search; rarely hash-match), then
        # images; each group in natural path order (PAGE2 before PAGE10) for
        # stable, resumable runs.
        items.sort(key=lambda it: (0 if it.kind == "video" else 1,
                                   _natural_key(it.relpath)))
        if skipped_dirs:
            print(f"⏭️  {skipped_dirs} folder(s) skipped wholesale (unchanged since last run)")

        print(f"📊 {media} media files · {tagged} already tagged · "
              f"{seen} previously checked · {len(items)} to process")
        return items, candidate_dirs

    def index_rendered_pdf_pages(self, paths: List[Path], root: Path,
                                 ledger_mgr: LedgerManager) -> List[FileItem]:
        """Turn completely rendered pages into perceptual-only queue items."""
        items: List[FileItem] = []
        for path in paths:
            try:
                st = path.stat()
                relpath = str(path.relative_to(root))
            except (OSError, ValueError):
                continue
            ledger = ledger_mgr.get(path.parent)
            status = ledger.status_for(path.name, st.st_size, st.st_mtime)
            if status in ("matched", "nomatch", "duplicate"):
                continue
            items.append(FileItem(
                path=path, relpath=relpath, size=st.st_size, mtime=st.st_mtime,
                kind="image", ledger=ledger, perceptual_only=True,
                md5=ledger.md5_for(path.name, st.st_size, st.st_mtime)))
        items.sort(key=lambda it: _natural_key(it.relpath))
        return items

    def finalize_dir_fingerprints(self, candidate_dirs: Set[Path],
                                   pdf_page_dirs: Set[Path],
                                   ledger_mgr: LedgerManager) -> None:
        """After a run, re-check each directory that wasn't wholesale-skipped:
        if every media file currently in it now has a tag sidecar or a
        matched/nomatch ledger record, seal that directory's fingerprint so
        the *next* run can skip it wholesale. A directory left incomplete
        (interrupted run, persistent no-match-pending file) simply isn't
        sealed and gets rechecked file-by-file next time — never wrongly
        skipped."""
        for dp_path in candidate_dirs:
            dir_ledger = ledger_mgr.get(dp_path)
            try:
                names = sorted(f for f in os.listdir(dp_path) if not f.startswith("."))
            except OSError:
                continue

            count = 0
            total_size = 0
            complete = True
            for fn in names:
                kind = self._media_kind(fn)
                if kind is None:
                    continue
                p = dp_path / fn
                try:
                    st = p.stat()
                except OSError:
                    complete = False
                    break
                count += 1
                total_size += st.st_size
                is_pdf_page = kind == "image" and p.suffix.lower() == ".png" and p.parent in pdf_page_dirs
                if self.has_sidecar(p) and not is_pdf_page:
                    continue
                if dir_ledger.status_for(fn, st.st_size, st.st_mtime) not in (
                        "matched", "nomatch", "duplicate"):
                    complete = False
                    break

            if complete and count:
                dir_ledger.mark_dir_complete(count, total_size)

    # ── Parallel local hashing ───────────────────────────────────────────────

    def hash_all(self, items: List[FileItem]) -> None:
        # Perceptual-only PDF pages still need a local hash for exact duplicate
        # detection, even though that hash is never sent to a booru.
        todo = [it for it in items if it.md5 is None]
        if not todo:
            return
        workers = min(8, (os.cpu_count() or 2))
        print(f"🔢 Hashing {len(todo)} files (×{workers})…")
        done = 0
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futmap = {ex.submit(self._md5_local, it.path): it for it in todo}
            for fut in cf.as_completed(futmap):
                futmap[fut].md5 = fut.result()
                done += 1
                if sys.stdout.isatty() and (done % 25 == 0 or done == len(todo)):
                    sys.stdout.write(f"\r  hashed {done}/{len(todo)}")
                    sys.stdout.flush()
        if sys.stdout.isatty():
            sys.stdout.write("\n")

    def deduplicate(self, root: Path, items: List[FileItem],
                    ledger_mgr: LedgerManager,
                    canonical_items: Optional[List[FileItem]] = None
                    ) -> Tuple[List[FileItem], int]:
        """Remove exact-MD5 duplicates from this run before network searching.

        An unchanged earlier matched/no-match ledger record wins over a new
        candidate. Otherwise the first item in the stable videos/images +
        natural-path order is canonical. Skipped copies receive a `duplicate`
        ledger status and `duplicate_of` path, then all valid duplicate records
        are rendered to ``duplicates.log`` in the scan root.
        """
        canonical_by_md5: Dict[str, Path] = {}
        for ledger in sorted(ledger_mgr.touched(), key=lambda led: str(led.dir)):
            for name, rec in sorted(ledger.records.items()):
                if not isinstance(rec, dict) or rec.get("status") not in (
                        "matched", "nomatch"):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) not in (
                        "matched", "nomatch"):
                    continue
                md5 = rec.get("md5")
                if md5:
                    canonical_by_md5.setdefault(md5, path)
        for item in canonical_items or []:
            if item.md5:
                canonical_by_md5.setdefault(item.md5, item.path)

        survivors: List[FileItem] = []
        duplicate_count = 0
        for item in items:
            if not item.md5:
                survivors.append(item)
                continue
            canonical = canonical_by_md5.get(item.md5)
            if canonical is None:
                canonical_by_md5[item.md5] = item.path
                survivors.append(item)
                continue
            if canonical == item.path:
                survivors.append(item)
                continue
            try:
                canonical_rel = str(canonical.relative_to(root))
            except ValueError:
                canonical_rel = str(canonical)
            item.ledger.record(item.path.name, item.size, item.mtime, item.md5,
                               "duplicate", [], duplicate_of=canonical_rel)
            duplicate_count += 1

        self._write_duplicates_log(root, ledger_mgr)
        return survivors, duplicate_count

    @staticmethod
    def _write_duplicates_log(root: Path, ledger_mgr: LedgerManager) -> None:
        groups: Dict[Tuple[str, str], List[str]] = {}
        for ledger in ledger_mgr.touched():
            for name, rec in ledger.records.items():
                if not isinstance(rec, dict) or rec.get("status") != "duplicate":
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) != "duplicate":
                    continue
                canonical = rec.get("duplicate_of") or "(canonical unknown)"
                md5 = rec.get("md5") or "(hash unavailable)"
                try:
                    duplicate = str(path.relative_to(root))
                except ValueError:
                    duplicate = str(path)
                groups.setdefault((canonical, md5), []).append(duplicate)

        log_path = root / DUPLICATES_FILE
        if not groups:
            if log_path.exists():
                try:
                    log_path.unlink()
                except OSError as e:
                    notify(f"⚠️  Couldn't remove stale {DUPLICATES_FILE}: {e}")
            return

        lines = [
            "FurTag exact duplicate report",
            "Only the canonical file is searched; copies are skipped.",
            "Reason for every group: byte-identical MD5 hash.",
            "",
        ]
        for (canonical, md5), duplicates in sorted(groups.items()):
            lines.append(f"MD5:       {md5}")
            lines.append(f"SEARCH:    {canonical}")
            lines += [f"DUPLICATE: {path}" for path in sorted(duplicates, key=_natural_key)]
            lines.append("")
        try:
            tmp = log_path.with_name(log_path.name + ".tmp")
            tmp.write_text("\n".join(lines), encoding="utf-8")
            tmp.replace(log_path)
        except OSError as e:
            notify(f"⚠️  Couldn't write {DUPLICATES_FILE}: {e}")

    # ── Hash tier (four boorus, concurrent per file) ─────────────────────────

    def _hash_lookup(self, service: str, md5: str) -> Tuple[Set[str], Set[str]]:
        if service == "e621":
            return self.e621_lookup_by_md5(md5)
        if service == "inkbunny":
            return self.inkbunny_lookup_by_md5(md5)
        if service == "danbooru":
            return self.danbooru_lookup_by_md5(md5)
        if service == "gelbooru":
            return self.gelbooru_lookup_by_md5(md5)
        return set(), set()

    def hash_tier(self, item: FileItem, disp: LiveDisplay, ex: cf.Executor
                  ) -> Tuple[Set[str], Set[str], List[str]]:
        """Query every enabled booru for this file's MD5 concurrently and merge.
        MD5 identity is byte-exact, so there is zero false-positive risk and the
        tag sets genuinely differ — never short-circuit between them."""
        services = self.enabled_hash_services()
        tags: Set[str] = set()
        urls: Set[str] = set()
        hit: Set[str] = set()
        if not item.md5 or not services:
            return tags, urls, []

        state = {s: "run" for s in services}
        futs = {ex.submit(self._hash_lookup, s, item.md5): s for s in services}
        disp.status("hash", disp.hash_line(state))
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                t, u = fut.result()
            except Exception as e:
                # Network/HTTP failure — distinct from a clean "not found" miss,
                # so surface it as ⚠ rather than ✗ (the file may still exist there).
                notify(f"❌ {s} failed on {item.path.name}: {e}")
                state[s] = "err"
                disp.status("hash", disp.hash_line(state))
                continue
            if t or u:
                tags |= t
                urls |= u
                hit.add(s)
                state[s] = "hit"
            else:
                state[s] = "miss"
            disp.status("hash", disp.hash_line(state))

        sources = [s for s in services if s in hit]   # deterministic order
        return tags, urls, sources

    # ── Perceptual tier (Fluffle → SauceNAO, sequential) ─────────────────────

    def perceptual_tier(self, item: FileItem, disp: LiveDisplay
                        ) -> Tuple[Set[str], Set[str], List[str]]:
        tags: Set[str] = set()
        urls: Set[str] = set()
        sources: List[str] = []
        fp = item.path

        disp.status("perceptual", "Fluffle…")
        js = self.fluffle_search(fp)
        if js:
            f_tags, f_urls, md5_u, pid = self.find_best_exact_match(js)
            if f_tags or f_urls:
                tags |= f_tags
                urls |= f_urls
                # A perceptual hit only tells us which post this is — re-query
                # e621 by ID for the full, properly-namespaced tag set. Prefer
                # the post ID (reliable) over the MD5-from-URL trick.
                if self.has_e621 and (pid or md5_u):
                    disp.status("perceptual", "Fluffle → e621 enrich…")
                    e_tags, e_urls = (self.e621_lookup_by_id(pid)
                                      if pid else (set(), set()))
                    if not e_tags and md5_u:
                        e_tags, e_urls = self.e621_lookup_by_md5(md5_u)
                    tags |= e_tags
                    urls |= e_urls
                sources.append("fluffle")

        if not (tags or urls) and self.has_saucenao and not self.saucenao_exhausted:
            disp.status("perceptual", "SauceNAO…")
            service, rid, s_tags, s_urls = self.saucenao_search(fp)
            if service and rid:
                # High-confidence booru match → pull the authoritative,
                # properly-namespaced tag set instead of SauceNAO's own.
                disp.status("perceptual", f"SauceNAO → {service} enrich…")
                a_tags, a_urls = self._authoritative_lookup(service, rid)
                if a_tags or a_urls:
                    tags |= a_tags
                    urls |= a_urls | s_urls
                    sources.append("saucenao")
                elif s_tags or s_urls:      # post gone — use own tags
                    tags |= s_tags
                    urls |= s_urls
                    sources.append("saucenao")
            elif s_tags or s_urls:
                # Resolved to a site we can't re-query (FA/Twitter/...) —
                # SauceNAO's own thinner tags are the best we've got.
                tags |= s_tags
                urls |= s_urls
                sources.append("saucenao")

        return tags, urls, sources

    # ── Orchestration ────────────────────────────────────────────────────────

    def run(self, root: Path) -> None:
        global _display

        ledger_mgr = LedgerManager()
        pdf_page_dirs, pdf_jobs = self.plan_pdf_renders(root)
        pdf_executor: Optional[cf.ThreadPoolExecutor] = None
        pdf_future = None
        pending_pdf_dirs = {pdf.parent / pdf.stem for pdf in pdf_jobs}
        if pdf_jobs:
            pdf_dpi = prompt_for_pdf_dpi(len(pdf_jobs))
            print(f"📄 Rendering {len(pdf_jobs)} PDF(s) at {pdf_dpi} DPI in background…")
            pdf_executor = cf.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="pdf-render")
            pdf_future = pdf_executor.submit(self.render_pdf_jobs, pdf_jobs, pdf_dpi)

        items, candidate_dirs = self.index(
            root, ledger_mgr, pdf_page_dirs, excluded_dirs=pending_pdf_dirs)
        already_tagged = self._hydrus_populate_already_tagged_page(ledger_mgr)
        if already_tagged:
            ledger_mgr.save_all()
            print(f"✅ Already Tagged page → {already_tagged} ledger-matched file(s)")
        if not items and pdf_future is None:
            print("✅ Nothing to do — everything is tagged or already checked.")
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            ledger_mgr.save_all()
            return
        self.hash_all(items)
        items, duplicates = self.deduplicate(root, items, ledger_mgr)
        if duplicates:
            ledger_mgr.save_all()
            print(f"♊ {duplicates} exact duplicate(s) skipped — see {DUPLICATES_FILE}")
        if not items and pdf_future is None:
            print("✅ Nothing unique left to search.")
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            ledger_mgr.save_all()
            return

        print("🔄 Hash tier: e621 · InkBunny · Danbooru · Gelbooru (concurrent, merged)"
              "  →  Perceptual: Fluffle → SauceNAO")
        print(f"   {LiveDisplay._LEGEND}\n")

        counts = {k: 0 for k in ("e621", "inkbunny", "danbooru",
                                 "gelbooru", "fluffle", "saucenao")}
        tagged = nomatch = 0
        counts_lock = threading.Lock()

        def _bump_hit(sources: List[str]) -> None:
            nonlocal tagged
            with counts_lock:
                tagged += 1
                for s in sources:
                    counts[s] += 1

        def _bump_miss() -> None:
            nonlocal nomatch
            with counts_lock:
                nomatch += 1

        save_lock = threading.Lock()
        save_counter = 0

        def _maybe_save_ledgers(every: int = 25) -> None:
            nonlocal save_counter
            with save_lock:
                save_counter += 1
                if save_counter < every:
                    return
                save_counter = 0
            ledger_mgr.save_all()

        # PDF pages bypass the hash tier and go straight into the perceptual
        # queue, seeded before either tier starts so they're worked on as
        # soon as the perceptual worker is free.
        hash_items = [it for it in items if not it.perceptual_only]
        perceptual_q: "queue.Queue" = queue.Queue()
        seed_count = 0
        for it in items:
            if it.perceptual_only:
                perceptual_q.put(it)
                seed_count += 1

        disp = LiveDisplay()
        _display = disp
        hash_workers = max(1, len(self.enabled_hash_services()))

        def perceptual_worker() -> None:
            idx = 0
            while True:
                item = perceptual_q.get()
                if item is _PERCEPTUAL_DONE:
                    perceptual_q.task_done()
                    return
                idx += 1
                try:
                    disp.start_file("perceptual", idx, item.path.name,
                                     f"{perceptual_q.qsize()} queued")
                    tags, urls, sources = self.perceptual_tier(item, disp)
                    if tags or urls:
                        if item.perceptual_only:
                            tags = set(tags) | self._pdf_page_base_tags(item.path)
                        self.write_results(item.path, tags, urls)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "matched", sources)
                        _bump_hit(sources)
                        disp.finish_file(
                            "perceptual", f"{'+'.join(sources)}  ({len(tags)} tags)")
                    else:
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "nomatch", [])
                        _bump_miss()
                        disp.finish_file("perceptual", "— no match")
                    _maybe_save_ledgers()
                except Exception as e:
                    notify(f"❌ perceptual worker error on {item.path.name}: {e}")
                finally:
                    perceptual_q.task_done()

        try:
            hash_interval = max(
                (self.pace[s].interval for s in self.enabled_hash_services()),
                default=0.0)
            disp.begin_phase(
                "hash", "Phase · hash lookups (e621·InkBunny·Danbooru·Gelbooru)",
                len(hash_items), interval=hash_interval)
            disp.begin_phase(
                "perceptual", "Phase · perceptual (Fluffle → SauceNAO)",
                seed_count, growing=True)

            perc_thread = threading.Thread(
                target=perceptual_worker, name="perceptual-worker", daemon=True)
            perc_thread.start()

            # ── Hash tier over every hashable candidate (videos first). Images
            # that miss are handed straight to the perceptual worker, which
            # runs concurrently on its own thread rather than waiting for the
            # whole hash tier to finish — the two tiers hit disjoint services,
            # each still individually rate-paced by its own Pacer.
            with cf.ThreadPoolExecutor(max_workers=hash_workers) as ex:
                for i, item in enumerate(hash_items):
                    nxt = hash_items[i + 1].path.name if i + 1 < len(hash_items) else None
                    disp.start_file("hash", i + 1, item.path.name, nxt)

                    tags, urls, sources = self.hash_tier(item, disp, ex)
                    if tags or urls:
                        self.write_results(item.path, tags, urls)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "matched", sources)
                        _bump_hit(sources)
                        disp.finish_file("hash", f"{'+'.join(sources)}  ({len(tags)} tags)")
                    elif item.kind == "image":
                        perceptual_q.put(item)
                        disp.grow("perceptual")
                        disp.finish_file("hash", "no hash match → perceptual")
                    else:                                  # video: hash-only
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "nomatch", [])
                        _bump_miss()
                        disp.finish_file("hash", "— no match")
                    _maybe_save_ledgers()

            # Let the ordinary perceptual queue settle before reading ledgers
            # for PDF deduplication; the worker remains alive for new pages.
            perceptual_q.join()

            # PDF rendering has overlapped the ordinary hash/perceptual work
            # above. Once complete, only fully-written pages are indexed,
            # locally hashed/deduplicated, and appended to the live perceptual
            # queue. They never enter the booru hash tier.
            if pdf_future is not None:
                try:
                    rendered_paths = pdf_future.result()
                except Exception as e:
                    notify(f"⚠️  Background PDF rendering failed: {e}")
                    rendered_paths = []
                pdf_items = self.index_rendered_pdf_pages(
                    rendered_paths, root, ledger_mgr)
                self.hash_all(pdf_items)
                pdf_items, pdf_duplicates = self.deduplicate(
                    root, pdf_items, ledger_mgr, canonical_items=items)
                if pdf_duplicates:
                    duplicates += pdf_duplicates
                    notify(f"♊ {pdf_duplicates} duplicate PDF page(s) skipped; "
                           f"see {DUPLICATES_FILE}.")
                for item in pdf_items:
                    candidate_dirs.add(item.path.parent)
                    perceptual_q.put(item)
                    disp.grow("perceptual")
                items.extend(pdf_items)

            disp.freeze_total("perceptual")
            perceptual_q.put(_PERCEPTUAL_DONE)
            perc_thread.join()
        finally:
            if pdf_executor is not None:
                pdf_executor.shutdown(wait=True)
            disp.close()
            _display = None
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            ledger_mgr.save_all()

        # ── Summary ──────────────────────────────────────────────────────────
        total = len(items)
        print("\n🏁 DONE")
        print(f"Total tagged:        {tagged}/{total}")
        print(f"  ├─ e621 hits:      {counts['e621']}")
        print(f"  ├─ InkBunny hits:  {counts['inkbunny']}")
        print(f"  ├─ Danbooru hits:  {counts['danbooru']}")
        print(f"  ├─ Gelbooru hits:  {counts['gelbooru']}")
        print(f"  ├─ Fluffle hits:   {counts['fluffle']}")
        print(f"  ├─ SauceNAO hits:  {counts['saucenao']}")
        print(f"  └─ No match:       {nomatch}")
        print(f"🗒️  Session ledgers updated across the scanned tree "
              f"({LEDGER_FILE} per folder)")


# ── Entry point ──────────────────────────────────────────────────────────────

def _unescape_path(raw: str) -> str:
    """Turn a Finder drag-and-drop path into a plain filesystem path.

    Dragging a folder from Finder into Terminal inserts it shell-escaped —
    spaces become ``\\ ``, and specials like ``()&`` get backslashed (or the
    whole path may be quoted), often with a trailing space. ``shlex`` undoes
    exactly that quoting. A single dragged item parses to one token; if we get
    anything else (e.g. a manually typed path with unescaped literal spaces, or
    an unbalanced quote), fall back to the raw string untouched.
    """
    try:
        parts = shlex.split(raw)
    except ValueError:
        return raw
    return parts[0] if len(parts) == 1 else raw


QUIT_WORDS = {"q", "quit", "exit"}
NUKE_COMMAND = "NUKE!"


def _is_furtag_sidecar(path: Path) -> bool:
    """True only for FurTag's media-extension-preserving sidecar names."""
    name = path.name.lower()
    media_exts = IMG_EXTS | VIDEO_EXTS
    return any(name.endswith(ext + ".txt") or
               name.endswith(ext + ".urls.txt") for ext in media_exts)


def _nuke_candidates(root: Path) -> Tuple[List[Path], List[Path]]:
    """Find generated ledgers and sidecars below root without following links."""
    ledgers: List[Path] = []
    sidecars: List[Path] = []
    ledger_names = {LEDGER_FILE, LEDGER_FILE + ".tmp", DUPLICATES_FILE,
                    DUPLICATES_FILE + ".tmp"}
    for dp, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for fn in sorted(files):
            path = Path(dp) / fn
            if fn in ledger_names:
                ledgers.append(path)
            elif _is_furtag_sidecar(path):
                sidecars.append(path)
    return ledgers, sidecars


def _pdf_render_candidates(root: Path) -> Tuple[List[Path], Set[Path]]:
    """Find only PNG pages whose names exactly match a sibling source PDF."""
    pages: List[Path] = []
    page_dirs: Set[Path] = set()
    pdfs: List[Path] = []
    for dp, dirs, files in os.walk(root, followlinks=False):
        dirs.sort()
        for fn in sorted(files):
            if not fn.startswith(".") and Path(fn).suffix.lower() in PDF_EXTS:
                pdfs.append(Path(dp) / fn)
    for pdf in pdfs:
        out_dir = pdf.parent / pdf.stem
        if not out_dir.is_dir():
            continue
        pattern = re.compile(rf"^{re.escape(pdf.stem)} PAGE\d+\.PNG$", re.I)
        try:
            matches = [p for p in out_dir.iterdir()
                       if p.is_file() and pattern.match(p.name)]
        except OSError:
            continue
        if matches:
            pages.extend(sorted(matches, key=lambda p: _natural_key(p.name)))
            page_dirs.add(out_dir)
    return pages, page_dirs


def _prompt_for_nuke() -> Optional[Path]:
    """Confirm and remove FurTag-generated state, returning the folder to scan.

    Blank input and cancellation return to the normal folder prompt. The
    filesystem root is deliberately refused even with confirmation.
    """
    print("\n💣 NUKE mode — remove FurTag ledgers and sidecars, then rescan.")
    try:
        raw = input("Folder to reset (drag it here, blank = cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n↩️  Nuke cancelled.")
        return None
    if not raw:
        print("↩️  Nuke cancelled.")
        return None

    root = Path(_unescape_path(raw)).expanduser().resolve()
    if not root.is_dir():
        print(f"‼️  '{root}' is not a valid directory. Nuke cancelled.")
        return None
    if root == Path(root.anchor):
        print("⛔ Refusing to nuke an entire filesystem root.")
        return None

    ledgers, sidecars = _nuke_candidates(root)
    pdf_pages, pdf_page_dirs = _pdf_render_candidates(root)
    print(f"\nTarget:   {root}")
    print(f"Ledgers/reports: {len(ledgers)}")
    print(f"Sidecars: {len(sidecars)}")
    print(f"Rendered PDF pages: {len(pdf_pages)} (optional second question)")
    try:
        answer = input("\nARE YOU SURE? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "y":
        print("↩️  Nuke cancelled; nothing was deleted.\n")
        return None

    reexport_pdfs = False
    if pdf_pages:
        try:
            answer = input(
                f"Also delete {len(pdf_pages)} rendered PDF page(s) so they "
                "are re-exported? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        reexport_pdfs = answer == "y"

    removed = 0
    failed = 0
    targets = ledgers + sidecars + (pdf_pages if reexport_pdfs else [])
    for path in targets:
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            failed += 1
            print(f"⚠️  Could not delete {path}: {e}")
    if reexport_pdfs:
        for out_dir in sorted(pdf_page_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                out_dir.rmdir()  # succeeds only when no unrelated content remains
            except OSError:
                pass
    print(f"\n💥 Reset complete — removed {removed} generated file(s).")
    if failed:
        print(f"⚠️  {failed} file(s) could not be removed and may still be skipped.")
    print("🔄 Starting a fresh scan of that folder…\n")
    return root


def prompt_for_folder() -> Path:
    """Ask for a folder, re-prompting until a real directory is given.

    Blank defaults to the current directory. Typing NUKE! enters the confirmed
    recursive reset flow. Typing q/quit/exit — or Ctrl+C / Ctrl+D — quits
    cleanly. An invalid path re-prompts instead of exiting.
    """
    while True:
        try:
            raw = input("Folder to scan (blank = current dir, NUKE! = reset): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n👋 Bye.")
        if raw.lower() in QUIT_WORDS:
            sys.exit("👋 Bye.")
        if raw.upper() == NUKE_COMMAND:
            nuked_root = _prompt_for_nuke()
            if nuked_root is not None:
                return nuked_root
            continue
        root = Path(_unescape_path(raw) if raw else ".").expanduser().resolve()
        if root.is_dir():
            return root
        print(f"‼️  '{root}' is not a valid directory. Try again (q to quit).\n")


def main() -> None:
    print("🐾 Unified Furry Tag Integrator for Hydrus 🐾")
    print("📋 e621 + InkBunny + Danbooru + Gelbooru MD5 (concurrent) → Fluffle → SauceNAO")
    print("⏭️  Skips files already tagged or logged in .furtag_ledger.json\n")

    root = prompt_for_folder()

    ti = TagIntegrator()
    ti.load_credentials(Path(__file__).with_name(CREDENTIALS_FILE))

    if ti.has_hydrus:
        print(f"📝 Output → Hydrus Client API  ({ti.hydrus_mode_desc()})")
    else:
        print("📝 Output → sidecars  "
              "(<file>.<ext>.txt + <file>.<ext>.urls.txt)")
        print("   Tip: set hydrus_api_url + hydrus_access_key in credentials.txt "
              "to push straight into Hydrus.")

    if not ti.any_source():
        print("\n⚠️  No API credentials loaded! Only Fluffle will be used.")
        try:
            input("Press Enter to continue anyway, or Ctrl+C to quit...")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n👋 Bye.")

    try:
        ti.run(root)
    except KeyboardInterrupt:
        sys.exit("\n⛔ Interrupted (progress saved to ledger).")


if __name__ == "__main__":
    main()
