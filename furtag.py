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
already done — without needing to re-hash or re-query anything. MD5s are also
checkpointed as soon as they are calculated, so an interrupted lookup pass
does not make the next run hash the same bytes again. Living inside the folder
it describes rather than
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
        hydrus_tag_deleted_duplicates = true  # tag current duplicate-group members
        hydrus_results_page  = on      # blank/false disables both result pages below
        hydrus_results_page_limit = 0  # newest N files per result page; 0 = unlimited
        hydrus_new_imports_page = FurTag New Imports   # brand-new imports this run
        hydrus_newly_tagged_page = FurTag Newly Tagged # files already in Hydrus, newly tagged
        hydrus_already_tagged_page = Already Tagged  # matched ledger history; false disables

    Note: Danbooru requires a verified-email account for API auth; if the key
    is rejected (403) the script falls back to anonymous Danbooru access.
    Hydrus Client API needs permissions: import files, edit tags, edit URLs,
    and manage pages for the optional unfocused results page. Adding Search for
    and Fetch Files lets FurTag batch-check its MD5s against Hydrus and skip
    redundant import checks for files already there. To send tags for a
    previously-deleted import to its current duplicate-group members, also add
    Manage File Relationships.
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
from typing import Callable, Dict, List, Optional, Set, Tuple

import requests
from PIL import Image, ImageFile
import regex  # for emoji stripping

from furtag_settings import (
    DEFAULT_PDF_ARCHIVAL_DPI,
    DEFAULT_PDF_DPI,
    RunOptions,
    ScanSummary,
    Settings,
    SettingsStore,
    render_sidecar_name,
)
from furtag_events import NullObserver, RunEvent, RunObserver, TerminalObserver
from furtag_review import PendingReview, ReviewQueue
from furtag_credentials import CredentialStore

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

# Matching thresholds and the Fluffle tossUp-only-on-e621 rule live in
# furtag_settings (DEFAULT_SAUCENAO_*, MatchingSettings) and are read per
# instance (TagIntegrator.saucenao_min_similarity etc.) so the GUI can change
# them at runtime. There are deliberately no module-level copies here.

IMG_EXTS   = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
VIDEO_EXTS = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".flv"}
PDF_EXTS   = {".pdf"}
PDF_DPI    = DEFAULT_PDF_DPI
PDF_ARCHIVAL_DPI = DEFAULT_PDF_ARCHIVAL_DPI

# Ledger statuses treated as "resolved" for skip / fingerprint sealing.
# pending_review is intentionally absent — those files stay eligible.
RESOLVED_LEDGER_STATUSES = frozenset({"matched", "nomatch", "duplicate"})

# Exact-hash (MD5) sources, then every search source. One ordered definition so
# the toggle lookups and the per-tier service lists can't drift apart.
HASH_SOURCES = ("e621", "inkbunny", "danbooru", "gelbooru")
SEARCH_SOURCES = HASH_SOURCES + ("fluffle", "saucenao")

CREDENTIALS_FILE = "credentials.txt"
LEDGER_FILE      = ".furtag_ledger.json"
DUPLICATES_FILE  = "duplicates.log"
HYDRUS_HASH_LOOKUP_BATCH = 256  # well below the Client API's 2 MB GET limit
HYDRUS_RELATIONSHIP_DUPLICATES = "8"  # Hydrus duplicate-status enum; "3" = alternates

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


def _bool_str(val: bool) -> str:
    """Render a bool in the credentials.txt spelling `_truthy` parses back."""
    return "true" if val else "false"


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

    def __init__(self, interval: float,
                 cancel: Optional[threading.Event] = None) -> None:
        self.interval = interval
        # Sleeping on this event instead of time.sleep() makes a paced wait
        # abort the instant a cancel arrives. SauceNAO paces at 6s and backs
        # off to 30s+, so an uninterruptible sleep is what made cancelling
        # look like it hung.
        self.cancel = cancel
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            slot = self._next if self._next > now else now
            self._next = slot + self.interval
        delay = slot - time.monotonic()
        if delay <= 0:
            return
        if self.cancel is not None:
            self.cancel.wait(delay)   # returns early when cancelled
        else:
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
    sha256: Optional[str] = None      # current Hydrus match found this run
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

    def cache_md5(self, name: str, size: int, mtime: float, md5: str) -> None:
        """Checkpoint a local MD5 before the network stages finish.

        ``status: hashed`` deliberately remains unresolved, so the next run
        retries its lookups while reusing this disk-expensive MD5.
        """
        if not md5:
            return
        with self._lock:
            rec = self.records.get(name)
            unchanged = bool(rec and rec.get("size") == size)
            if unchanged:
                try:
                    unchanged = abs(float(rec.get("mtime", -1)) - mtime) <= self.MTIME_EPS
                except (TypeError, ValueError):
                    unchanged = False
            if unchanged:
                if rec.get("md5") != md5:
                    rec["md5"] = md5
                    self._dirty += 1
                return
            self.records[name] = {
                "size": size,
                "mtime": round(mtime, 3),
                "md5": md5,
                "status": "hashed",
                "sources": [],
            }
            self._dirty += 1

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
               status: str, sources: List[str], duplicate_of: str = "",
               sha256: Optional[str] = None) -> None:
        with self._lock:
            record = {
                "size": size,
                "mtime": round(mtime, 3),
                "md5": md5,
                "status": status,
                "sources": sources,
            }
            if sha256:
                # Persist the SHA-256 Hydrus already handed us on import, so the
                # Already Tagged page never has to recompute it on a later run.
                record["sha256"] = sha256
            if status == "matched":
                # Wall-clock stamp so the Already Tagged page can be limited to
                # the N most recently tagged files on a later run.
                record["tagged_at"] = time.time()
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
                payload: Dict = {"version": 3, "records": self.records}
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
                write_sidecars: bool = True,
                sidecar_format: str = "txt",
                tag_pattern: str = "{name}{ext}.txt",
                json_pattern: str = "{name}{ext}.json",
                should_cancel: Optional[Callable[[], bool]] = None) -> List[Path]:
    """Render every page of ``pdf_path`` to a PNG under ``output_root/<stem>/``.

    Returns the list of PNG paths written. When ``write_sidecars`` is True each
    PNG also gets a ``comic:``/``page:`` base-tag sidecar (txt or json per
    *sidecar_format*) so perceptual tags append to the same file later.

    *should_cancel* is polled between pages so a cancel doesn't have to wait out
    a whole multi-hundred-page render.
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
    try:
        for i, page in enumerate(doc, start=1):
            if should_cancel is not None and should_cancel():
                break
            base_name = f"{stem} PAGE{i}.PNG"
            png_path = out_dir / base_name

            pix = page.get_pixmap(dpi=dpi)
            pix.save(png_path)
            generated.append(png_path)

            if write_sidecars:
                tags = {f"comic:{stem}", f"page:{i}"}
                if sidecar_format == "json":
                    sc_name = render_sidecar_name(json_pattern, png_path)
                    sc_path = png_path.parent / sc_name
                    payload = {"tags": sorted(tags), "urls": []}
                    sc_path.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
                else:
                    sc_name = render_sidecar_name(tag_pattern, png_path)
                    sc_path = png_path.parent / sc_name
                    sc_path.write_text(
                        "\n".join(sorted(tags)) + "\n", encoding="utf-8")
    finally:
        doc.close()
    print(f"  {pdf_path.name}: {len(generated)} page(s) at {dpi} DPI -> {out_dir}")
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

    def __init__(self, settings: Optional[Settings] = None,
                 session: Optional[requests.Session] = None) -> None:
        self.settings = (settings or Settings()).clone()
        self.session = session if session is not None else requests.Session()
        self.cancel_event = threading.Event()
        self._observer: RunObserver = NullObserver()
        self._review_queue: Optional[ReviewQueue] = None
        self._run_lock = threading.Lock()  # one scan at a time

        # Per-service pacers — intervals are filled in by apply_settings() below,
        # which is the single place settings → instance attrs is wired.
        self.pace = {name: Pacer(0.0) for name in
                     ("e621", "inkbunny", "danbooru", "gelbooru",
                      "fluffle", "saucenao")}

        # Fluffle
        self.fluffle_api  = "https://api.fluffle.xyz/v1/search"
        self.headers_fluf = {"User-Agent": "HydrusIntegrator/5.0 (Fluffle+e621+InkBunny+SauceNAO)"}

        # e621 — has_* = credentials present (available); enabled_* = user toggle
        self.e621_username = ""
        self.e621_api_key  = ""
        self.headers_e6: Dict[str, str] = {}
        self.has_e621 = False
        self.enabled_e621 = True
        self._pool_cache: Dict[int, Dict] = {}   # pool_id → pool JSON

        # InkBunny
        self.ib_username = ""
        self.ib_password = ""
        self.ib_sid = ""
        self.has_inkbunny = False
        self.enabled_inkbunny = True

        # Danbooru
        self.danbooru_username = ""
        self.danbooru_api_key  = ""
        self.has_danbooru = False
        self.enabled_danbooru = True
        self.danbooru_anon = False   # set True after a 401/403 → drop auth

        # Gelbooru
        self.gelbooru_user_id = ""
        self.gelbooru_api_key = ""
        self.has_gelbooru = False
        self.enabled_gelbooru = True

        # SauceNAO
        self.saucenao_api_key = ""
        self.headers_saucenao: Dict[str, str] = {}
        self.has_saucenao = False
        self.enabled_saucenao = True
        self.saucenao_exhausted = False   # set True when the daily quota runs out

        # Fluffle has no credentials — availability is always True when enabled
        self.has_fluffle = True
        self.enabled_fluffle = True

        # Hydrus Client API (optional output sink — skip sidecars when on)
        self.hydrus_api_url = ""
        self.hydrus_access_key = ""
        self.hydrus_tag_service_key = ""
        self.hydrus_can_edit_urls = False   # access key has "Import and Edit URLs"
        self.hydrus_can_search_files = False  # MD5 → current SHA-256 lookup
        self.hydrus_can_manage_relationships = False
        # Two result pages: genuinely new imports vs. files already in Hydrus
        # that merely gained tags. Hashes are retained as rolling newest-N
        # lists, then each page is created once when the scan finishes.
        # Page names come from settings via apply_settings().
        self.hydrus_result_pages: Dict[str, Dict] = {
            "new":     {"name": "", "enabled": False, "hashes": []},
            "updated": {"name": "", "enabled": False, "hashes": []},
        }
        self.hydrus_already_tagged_page_name = ""
        self.hydrus_already_tagged_page_enabled = False
        # Set once from the startup Hydrus menu. None means do not build an
        # Already Tagged page this session; 0 means include every match.
        self.hydrus_already_tagged_page_limit: Optional[int] = None
        self.has_hydrus = False
        self._hydrus_lock = threading.Lock()  # serialise API writes (hash + perc)
        self.apply_settings(self.settings)

    def apply_settings(self, settings: Settings) -> None:
        """Re-apply non-secret settings (thresholds, toggles, pacers)."""
        self.settings = settings.clone()
        m = self.settings.matching
        self.saucenao_min_similarity = float(m.saucenao_min_similarity)
        self.saucenao_auth_similarity = float(m.saucenao_auth_similarity)
        self.fluffle_tossup_e621 = bool(m.fluffle_tossup_e621_only)
        self.fluffle_accepted_matches = list(m.fluffle_accepted_matches or ["exact"])
        self.fluffle_review_mode = m.fluffle_review_mode or "off"
        perf = self.settings.performance
        for name in self.pace:
            self.pace[name].interval = getattr(perf, f"{name}_interval")
        self._bind_cancel_to_pacers()
        out = self.settings.output
        self.hydrus_import = out.hydrus_import
        self.hydrus_also_sidecars = out.sidecars_enabled
        self.hydrus_tag_deleted_duplicates = out.hydrus_tag_deleted_duplicates
        self.hydrus_tag_service_name = out.hydrus_tag_service
        self.hydrus_import_unmatched = out.hydrus_import_unmatched
        hy = self.settings.hydrus
        self.hydrus_results_page_limit = hy.result_page_limit
        self.hydrus_result_pages["new"]["name"] = hy.new_imports_page_name
        self.hydrus_result_pages["updated"]["name"] = hy.newly_tagged_page_name
        self.hydrus_already_tagged_page_name = hy.already_tagged_page_name
        self._apply_source_toggles()

    def _apply_source_toggles(self) -> None:
        src = self.settings.sources
        self.enabled_e621 = bool(src.e621_enabled)
        self.enabled_inkbunny = bool(src.inkbunny_enabled)
        self.enabled_danbooru = bool(src.danbooru_enabled)
        self.enabled_gelbooru = bool(src.gelbooru_enabled)
        self.enabled_fluffle = bool(src.fluffle_enabled)
        self.enabled_saucenao = bool(src.saucenao_enabled)

    def _bind_cancel_to_pacers(self) -> None:
        """Point every Pacer at the live cancel event so paced sleeps abort."""
        for pacer in self.pace.values():
            pacer.cancel = self.cancel_event

    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def request_cancel(self) -> None:
        self.cancel_event.set()

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

    def load_credentials(self, creds: Optional[Path] = None,
                         cfg: Optional[Dict[str, str]] = None) -> None:
        """Load credentials from a dict, CredentialStore, or legacy credentials.txt.

        Any missing/incomplete set just marks that source unavailable.
        """
        if cfg is None:
            if creds is None:
                creds = Path(__file__).with_name(CREDENTIALS_FILE)
            print(f"🔑 Loading credentials from {creds.name}")
            if not creds.exists():
                print("‼️  Missing credentials.txt – all API sources disabled.")
                # Still try env/keyring
                store = CredentialStore()
                cfg = store.load_all().as_cfg()
                if not any(cfg.values()):
                    return
            else:
                cfg = self._read_kv(creds)
                # Overlay env vars (env wins)
                store = CredentialStore()
                env_cfg = store.load_all().as_cfg()
                for k, v in env_cfg.items():
                    if v:
                        cfg[k] = v
        else:
            print("🔑 Loading credentials from secure store / environment")

        # Non-secret Hydrus prefs default from settings; anything already in cfg
        # (i.e. explicitly set in credentials.txt) wins.
        out = self.settings.output
        hy = self.settings.hydrus
        defaults = {
            "hydrus_import": _bool_str(out.hydrus_import),
            "hydrus_also_sidecars": _bool_str(out.sidecars_enabled),
            "hydrus_tag_deleted_duplicates": _bool_str(out.hydrus_tag_deleted_duplicates),
            "hydrus_results_page_limit": str(hy.result_page_limit),
            "hydrus_new_imports_page": hy.new_imports_page_name,
            "hydrus_newly_tagged_page": hy.newly_tagged_page_name,
            "hydrus_already_tagged_page": hy.already_tagged_page_name,
            "hydrus_results_page": "on" if hy.results_pages_enabled else "off",
        }
        if out.hydrus_tag_service:
            defaults["hydrus_tag_service"] = out.hydrus_tag_service
        cfg = {**defaults, **cfg}

        self._init_e621(cfg)
        self._init_inkbunny(cfg)
        self._init_danbooru(cfg)
        self._init_gelbooru(cfg)
        self._init_saucenao(cfg)
        if self.settings.output.hydrus_enabled:
            self._init_hydrus(cfg)
        else:
            self.has_hydrus = False
        self._apply_source_toggles()

    def load_credentials_from_store(self, store: Optional[CredentialStore] = None
                                    ) -> None:
        """Preferred path: keyring + env vars (no plaintext file required)."""
        store = store or CredentialStore()
        snap = store.load_all()
        cfg = snap.as_cfg()
        # Also merge legacy credentials.txt if present (private migration window)
        legacy = Path(__file__).with_name(CREDENTIALS_FILE)
        if legacy.exists():
            for k, v in self._read_kv(legacy).items():
                cfg.setdefault(k, v)
        self.load_credentials(cfg=cfg)

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
        self.hydrus_tag_deleted_duplicates = _truthy(
            cfg.get("hydrus_tag_deleted_duplicates", "true"), default=True)
        try:
            self.hydrus_results_page_limit = max(
                0, int((cfg.get("hydrus_results_page_limit") or "0").strip()))
        except ValueError:
            print("⚠️  Invalid hydrus_results_page_limit; using unlimited.")
            self.hydrus_results_page_limit = 0
        page_setting = cfg.get("hydrus_results_page", "on").strip()
        page_requested = page_setting.lower() not in {"", "0", "false", "no", "off"}
        new_name = cfg.get("hydrus_new_imports_page", "").strip()
        upd_name = cfg.get("hydrus_newly_tagged_page", "").strip()
        if new_name:
            self.hydrus_result_pages["new"]["name"] = new_name
        if upd_name:
            self.hydrus_result_pages["updated"]["name"] = upd_name
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
            everything = access.get("permits_everything", False)
            can_manage_pages = everything or 4 in permissions
            # Hydrus permission 0 = "Import and Edit URLs"; associate_url 403s
            # without it, so know up front rather than failing per file.
            self.hydrus_can_edit_urls = everything or 0 in permissions
            # Permission 3 lets us batch-check local MD5s and skip redundant
            # add_file calls for files Hydrus already has.
            self.hydrus_can_search_files = everything or 3 in permissions
            # Permission 8 permits querying a deleted file's *current* exact
            # duplicate-group members. It is deliberately separate from normal
            # file searching in Hydrus.
            self.hydrus_can_manage_relationships = everything or 8 in permissions
            if not self.hydrus_can_edit_urls:
                print("⚠️  Hydrus URLs disabled – access key needs the "
                      "'Import and Edit URLs' permission; tags still work.")
            if not self.hydrus_can_search_files:
                print("⚠️  Hydrus hash cache disabled – access key needs "
                      "'Search for and Fetch Files'; imports still work.")
            if (self.hydrus_tag_deleted_duplicates and
                    not self.hydrus_can_manage_relationships):
                print("⚠️  Deleted-file duplicate tagging disabled – access key needs "
                      "'Manage File Relationships'.")
            for page in self.hydrus_result_pages.values():
                page["enabled"] = page_requested and can_manage_pages
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

    def _hydrus_cache_current_hashes(self, items: List[FileItem]) -> int:
        """Cache Hydrus SHA-256s for candidate MD5s that are *currently local*.

        Hydrus retains non-SHA hashes even after deletion, so ``file_hashes``
        alone is not sufficient to bypass ``add_file``. We intersect its
        MD5→SHA-256 mapping with ``search_files`` results, which only cover
        current local files. The result is safe to tag directly this run.
        """
        if not (self.has_hydrus and self.hydrus_can_search_files):
            return 0
        by_md5: Dict[str, List[FileItem]] = {}
        for item in items:
            if item.md5:
                by_md5.setdefault(item.md5, []).append(item)
        if not by_md5:
            return 0

        found = 0
        md5s = sorted(by_md5)
        for offset in range(0, len(md5s), HYDRUS_HASH_LOOKUP_BATCH):
            batch = md5s[offset:offset + HYDRUS_HASH_LOOKUP_BATCH]
            try:
                mapping_r = self.session.get(
                    f"{self.hydrus_api_url}/get_files/file_hashes",
                    headers=self._hydrus_headers(),
                    params={
                        "hashes": json.dumps(batch),
                        "source_hash_type": "md5",
                        "desired_hash_type": "sha256",
                    },
                    timeout=30,
                )
                search_r = self.session.get(
                    f"{self.hydrus_api_url}/get_files/search_files",
                    headers=self._hydrus_headers(),
                    params={
                        "tags": json.dumps([
                            "system:hash = " + " ".join(batch) + " md5"
                        ]),
                        "return_hashes": "true",
                        "return_file_ids": "false",
                    },
                    timeout=30,
                )
                if mapping_r.status_code != 200 or search_r.status_code != 200:
                    raise RuntimeError(
                        f"file_hashes HTTP {mapping_r.status_code}; "
                        f"search_files HTTP {search_r.status_code}")
                mapping = mapping_r.json().get("hashes") or {}
                current = set(search_r.json().get("hashes") or [])
            except (requests.RequestException, ValueError, RuntimeError) as e:
                self.hydrus_can_search_files = False
                notify("⚠️  Hydrus MD5 cache unavailable; using normal imports "
                       f"for this run ({e}).")
                return found

            for md5, sha256 in mapping.items():
                if sha256 not in current:
                    continue
                for item in by_md5.get(md5, []):
                    item.sha256 = sha256
                    item.ledger.cache_sha256(
                        item.path.name, item.size, item.mtime, sha256)
                    found += 1
        return found

    @property
    def write_sidecars(self) -> bool:
        """Sidecars when Hydrus API is off, or when sidecars_enabled / also_sidecars."""
        if self.has_hydrus:
            # apply_settings() mirrors settings.output.sidecars_enabled onto this
            # attribute, so it is the single holder — don't OR it with its source.
            return bool(self.hydrus_also_sidecars)
        # Classic fallback: always write when Hydrus is unavailable, unless the
        # user explicitly disabled sidecars while also disabling Hydrus (blocked
        # by preflight). Prefer writing over silent data loss.
        return True

    def source_available(self, name: str) -> bool:
        """Credentials present. Called per file per service — no dict building."""
        if name == "saucenao":
            return self.has_saucenao and not self.saucenao_exhausted
        return name in SEARCH_SOURCES and getattr(self, f"has_{name}", False)

    def source_enabled(self, name: str) -> bool:
        """User toggle. Called per file per service — no dict building."""
        return name in SEARCH_SOURCES and getattr(self, f"enabled_{name}", False)

    def source_active(self, name: str) -> bool:
        """Available credentials AND user-enabled."""
        return self.source_available(name) and self.source_enabled(name)

    def any_source(self) -> bool:
        """True if any search source is active (available + enabled)."""
        return any(self.source_active(s) for s in SEARCH_SOURCES)

    def any_source_available(self) -> bool:
        return any(self.source_available(s) for s in SEARCH_SOURCES)

    def enabled_hash_services(self) -> List[str]:
        return [s for s in HASH_SOURCES if self.source_active(s)]

    def source_status_map(self) -> Dict[str, str]:
        """Map service → 'active' | 'disabled' | 'unavailable' for UI."""
        out: Dict[str, str] = {}
        for s in ("e621", "inkbunny", "danbooru", "gelbooru", "fluffle", "saucenao"):
            if not self.source_available(s):
                out[s] = "unavailable"
            elif not self.source_enabled(s):
                out[s] = "disabled"
            else:
                out[s] = "active"
        return out

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

    def find_best_exact_match(
            self, j: Dict
            ) -> Tuple[Set[str], Set[str], str, str, Optional[Dict]]:
        """
        Parse Fluffle results. Priority among auto-accepted classes:
        exact-e621 > exact-other > tossUp-e621 (when fluffle_tossup_e621).

        Returns (tags, urls, md5_from_url, post_id, review_candidate).
        *review_candidate* is a raw Fluffle result dict when the best hit falls
        in the human-review band (and review mode is on); otherwise None.
        """
        results = j.get("results") if j else None
        if not results or not isinstance(results, list):
            return set(), set(), "", "", None

        def is_e621(r: Dict) -> bool:
            return ("e621" in r.get("platform", "").lower()
                    or "e621.net" in r.get("location", ""))

        accepted = set(self.fluffle_accepted_matches or ["exact"])
        # Always allow exact if the accepted list is empty/broken
        if not accepted:
            accepted = {"exact"}

        exact_e621 = exact_other = tossup_e621 = None
        review_candidate = None
        review_mode = self.fluffle_review_mode or "off"

        for result in results:
            match = result.get("match")
            if match == "exact" and "exact" in accepted:
                if is_e621(result):
                    exact_e621 = exact_e621 or result
                elif exact_other is None:
                    exact_other = result
            elif match == "tossUp":
                if "tossUp" in accepted:
                    if is_e621(result) and self.fluffle_tossup_e621:
                        tossup_e621 = tossup_e621 or result
                    elif not self.fluffle_tossup_e621:
                        tossup_e621 = tossup_e621 or result
                elif review_mode in ("tossups", "tossups_alternatives"):
                    if review_candidate is None:
                        review_candidate = result
            elif match == "alternative":
                if "alternative" in accepted:
                    if exact_other is None and not is_e621(result):
                        exact_other = result  # treat as auto if configured
                    elif is_e621(result) and exact_e621 is None:
                        exact_e621 = result
                elif review_mode == "tossups_alternatives":
                    if review_candidate is None:
                        review_candidate = result
            elif match == "unlikely" and "unlikely" in accepted:
                if exact_other is None:
                    exact_other = result

        # Legacy tossUp-e621 auto-accept when exact is accepted and tossup guard on
        if (tossup_e621 is None and self.fluffle_tossup_e621 and
                "exact" in accepted and "tossUp" not in accepted):
            for result in results:
                if result.get("match") == "tossUp" and is_e621(result):
                    tossup_e621 = result
                    break

        chosen = exact_e621 or exact_other or tossup_e621
        if not chosen:
            return set(), set(), "", "", review_candidate

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

        return tags, urls, self._md5_from_url(loc), self._post_id_from_url(loc), None

    # ── SauceNAO API ─────────────────────────────────────────────────────────

    def saucenao_search(self, img: Path,
                        similarity_threshold: Optional[float] = None
                        ) -> Tuple[Optional[str], Optional[str], Set[str], Set[str]]:
        """
        Returns (service, post_id, own_tags, own_urls).
        (service, post_id) is set only when a result above saucenao_auth_similarity
        resolves to a booru we hold credentials for — the caller should then
        re-query that booru for the authoritative tag set rather than trusting
        SauceNAO's own thinner tags. own_tags/own_urls are the fallback for
        matches that resolve to sites we can't re-query.
        """
        if not self.source_active("saucenao") or self.saucenao_exhausted:
            return None, None, set(), set()
        if similarity_threshold is None:
            similarity_threshold = self.saucenao_min_similarity
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
                # SauceNAO normally puts quota state in a successful JSON
                # response, but some deployments reply with a plain daily-limit
                # error instead. Do not keep spending time retrying it.
                if "daily" in r.text.lower() and "limit" in r.text.lower():
                    self._disable_saucenao("daily search limit reached")
                return None, None, set(), set()
            j = r.json()
            self._saucenao_check_quota(j.get("header", {}))
            if j.get("header", {}).get("status", 0) != 0:
                return None, None, set(), set()
            service, post_id = self._saucenao_best_authoritative(
                j, self.saucenao_auth_similarity)
            tags, urls = self._extract_saucenao_tags(j, similarity_threshold)
            return service, post_id, tags, urls
        except (requests.RequestException, ValueError):
            return None, None, set(), set()

    def _disable_saucenao(self, reason: str) -> None:
        """Disable SauceNAO for the rest of this launcher session once."""
        if self.saucenao_exhausted:
            return
        self.saucenao_exhausted = True
        notify(f"⚠️  SauceNAO {reason} – skipping it for the rest of this session.")

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
            self._disable_saucenao("daily search limit reached")

    def _saucenao_best_authoritative(self, json_data: Dict, threshold: float
                                     ) -> Tuple[Optional[str], Optional[str]]:
        """Highest-confidence result above threshold that carries a booru ID we
        can re-query (and hold creds for). Returns (service, id) or (None, None).
        Preference order e621 → danbooru → gelbooru when a result has several."""
        candidates = [("e621", "e621_id", self.source_active("e621")),
                      ("danbooru", "danbooru_id", self.source_active("danbooru")),
                      ("gelbooru", "gelbooru_id", self.source_active("gelbooru"))]
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
        if not urls:
            # A lone site: tag with no link (e.g. an exhentai gallery SauceNAO
            # gives no ext_urls for) is dead weight — you can't follow it up.
            # Drop it, but keep any creator:/title:/character: that stand alone.
            tags = {t for t in tags if not t.startswith("site:")}
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

    def tag_sidecar_path(self, media: Path) -> Path:
        """Configured tag sidecar path (txt format)."""
        pattern = self.settings.output.sidecar_tag_filename or "{name}{ext}.txt"
        return media.parent / render_sidecar_name(pattern, media)

    def url_sidecar_path(self, media: Path) -> Path:
        """Configured URL sidecar path (txt format)."""
        pattern = self.settings.output.sidecar_url_filename or "{name}{ext}.urls.txt"
        return media.parent / render_sidecar_name(pattern, media)

    def json_sidecar_path(self, media: Path) -> Path:
        pattern = self.settings.output.sidecar_json_filename or "{name}{ext}.json"
        return media.parent / render_sidecar_name(pattern, media)

    @staticmethod
    def legacy_tag_sidecar_path(media: Path) -> Path:
        return media.with_suffix(media.suffix + ".txt")

    @staticmethod
    def legacy_url_sidecar_path(media: Path) -> Path:
        return media.with_suffix(media.suffix + ".urls.txt")

    def _tag_sidecar_candidates(self, media: Path) -> List[Path]:
        """Configured + legacy tag sidecar names, de-duplicated, in read order."""
        return list(dict.fromkeys((self.tag_sidecar_path(media),
                                   self.legacy_tag_sidecar_path(media))))

    def _url_sidecar_candidates(self, media: Path) -> List[Path]:
        """Configured + legacy URL sidecar names, de-duplicated, in read order."""
        return list(dict.fromkeys((self.url_sidecar_path(media),
                                   self.legacy_url_sidecar_path(media))))

    def _json_sidecar_candidates(self, media: Path) -> List[Path]:
        """Configured + common alternate JSON sidecar names, de-duplicated."""
        return list(dict.fromkeys((self.json_sidecar_path(media),
                                   media.with_suffix(media.suffix + ".json"))))

    def _sidecar_candidates(self, media: Path) -> List[Path]:
        """Every name that counts as a sidecar for *media*, de-duplicated.

        Single source of truth for the `index()` skip rule and for reading a
        payload back, so the two can never disagree. With the default patterns
        the configured and legacy names coincide, so this collapses to three
        paths rather than six.
        """
        return list(dict.fromkeys(self._tag_sidecar_candidates(media)
                                  + self._url_sidecar_candidates(media)
                                  + self._json_sidecar_candidates(media)))

    def has_sidecar(self, media: Path) -> bool:
        """True if any recognized sidecar exists (configured + legacy .txt).

        Legacy ``.txt`` sidecars are always recognized even when the format
        setting is JSON, so switching formats never re-scans a library.
        """
        return any(p.exists() for p in self._sidecar_candidates(media))

    def read_sidecar_payload(self, media: Path) -> Tuple[Set[str], Set[str]]:
        """Read tags and URLs from any supported sidecar shape beside *media*."""
        tags: Set[str] = set()
        urls: Set[str] = set()
        # JSON first (single file with both)
        for jp in self._json_sidecar_candidates(media):
            if jp.is_file():
                try:
                    data = json.loads(jp.read_text("utf-8"))
                    if isinstance(data, dict):
                        for t in data.get("tags") or []:
                            if t:
                                tags.add(str(t))
                        for u in data.get("urls") or []:
                            if u:
                                urls.add(str(u))
                    elif isinstance(data, list):
                        for t in data:
                            if t:
                                tags.add(str(t))
                except (OSError, ValueError, TypeError):
                    pass
        # Text sidecars (configured + legacy)
        for tp in self._tag_sidecar_candidates(media):
            if tp.is_file():
                tags |= {t for t in self._read_result_sidecar(tp) if t}
        for up in self._url_sidecar_candidates(media):
            if up.is_file():
                urls |= {u for u in self._read_result_sidecar(up) if u}
        return tags, urls

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

    def _write_sidecar_results(self, media: Path, tags: Set[str],
                               urls: Set[str]) -> None:
        """Write an already-cleaned result payload without touching Hydrus."""
        fmt = (self.settings.output.sidecar_format or "txt").lower()
        if fmt == "json":
            path = self.json_sidecar_path(media)
            try:
                existing_tags: Set[str] = set()
                existing_urls: Set[str] = set()
                if path.exists():
                    data = json.loads(path.read_text("utf-8"))
                    if isinstance(data, dict):
                        existing_tags = set(data.get("tags") or [])
                        existing_urls = set(data.get("urls") or [])
                merged_tags = sorted((existing_tags | tags) - {""})
                merged_urls = sorted((existing_urls | urls) - {""})
                payload = {"tags": merged_tags, "urls": merged_urls}
                tmp = path.with_name(path.name + ".tmp")
                tmp.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                tmp.replace(path)
            except Exception as e:
                notify(f"❌ Write failed for {path.name}: {e}")
            return
        if tags:
            self._append_lines(self.tag_sidecar_path(media), tags)
        if urls:
            self._append_lines(self.url_sidecar_path(media), urls)

    def sync_sidecars_to_hydrus(self, root: Path) -> Tuple[int, int]:
        """Push existing FurTag sidecars to Hydrus without touching ledgers.

        This is a migration/reconciliation pass: tag sidecars (txt or JSON)
        supply tags and URL sidecars supply source URLs. It deliberately does
        no booru lookup and never changes a ``.furtag_ledger.json`` record.
        """
        if not self.has_hydrus:
            return 0, 0
        candidates: List[Path] = []
        for dp, dirs, files in os.walk(root):
            dirs.sort()
            if self.cancelled():
                break
            for name in sorted(files):
                if name.startswith(".") or not self._media_kind(name):
                    continue
                media = Path(dp) / name
                if self.has_sidecar(media):
                    candidates.append(media)
        if not candidates:
            print("📤 No FurTag sidecars found to sync to Hydrus.")
            return 0, 0

        print(f"📤 Syncing sidecars to Hydrus for {len(candidates)} file(s)…")
        processed = 0
        # Reading a sidecar is disk-bound and independent per file, so prefetch
        # payloads on a pool while the serialised Hydrus pushes proceed —
        # otherwise every push waits on a fresh read first.
        workers = min(8, max(1, os.cpu_count() or 1))
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            payloads = dict(zip(
                candidates, ex.map(self.read_sidecar_payload, candidates)))
        for index, media in enumerate(candidates, start=1):
            if self.cancelled():
                print("\n⏹️  Sidecar sync cancelled.")
                break
            tags, urls = payloads.get(media, (set(), set()))
            tags = {tag for tag in tags if not _is_junk_tag(tag)}
            if not tags and not urls:
                continue
            # _hydrus_push reports detailed per-file failures itself. A
            # successful deleted-file duplicate-group transfer deliberately
            # returns no source SHA-256, so it must still count as processed.
            self._hydrus_push(media, tags, urls)
            processed += 1
            if sys.stdout.isatty() and (index % 25 == 0 or index == len(candidates)):
                sys.stdout.write(f"\r  synced {index}/{len(candidates)}")
                sys.stdout.flush()
        if sys.stdout.isatty():
            sys.stdout.write("\n")
        print(f"✅ Sidecar sync attempted for {processed} file(s); "
              "ledgers left unchanged.")
        return processed, 0

    @staticmethod
    def _pdf_page_base_tags(media: Path) -> Set[str]:
        """comic:/page: for a PDF-rendered page named like ``STEM PAGEN.PNG``."""
        tags: Set[str] = {f"comic:{media.parent.name}"}
        m = re.search(r"PAGE(\d+)", media.name, re.I)
        if m:
            tags.add(f"page:{int(m.group(1))}")
        return tags

    def write_results(self, media: Path, tags: Set[str], urls: Set[str],
                      known_sha256: Optional[str] = None) -> Optional[str]:
        """Push to Hydrus and/or write sidecars. Returns the file's SHA-256 when
        it was pushed to Hydrus (so the caller can cache it), else None."""
        # Drop "artist unknown / anonymous" placeholder tags from every source
        # before writing — they're noise in a Hydrus library.
        tags = {t for t in tags if not _is_junk_tag(t)}
        urls = {u for u in urls if u}

        if self.has_hydrus and (tags or urls):
            sha256 = self._hydrus_push(media, tags, urls, known_sha256)
            if self.write_sidecars:
                self._write_sidecar_results(media, tags, urls)
            return sha256

        if self.write_sidecars:
            self._write_sidecar_results(media, tags, urls)
        return None

    def _propagate_duplicate_results(
            self, root: Path, canonical: FileItem, duplicates: List[FileItem],
            tags: Set[str], urls: Set[str], sources: List[str],
            canonical_sha256: Optional[str]) -> int:
        """Give byte-identical filesystem copies the canonical result too.

        Hydrus stores byte-identical files as one hash record, so its tag push
        already applies to every copy. Sidecar output still needs to be written
        per path, and every copy gets a resolved ledger record so it never has
        to be searched on its own later.
        """
        if not duplicates:
            return 0
        tags = {t for t in tags if not _is_junk_tag(t)}
        urls = {u for u in urls if u}
        try:
            canonical_rel = str(canonical.path.relative_to(root))
        except ValueError:
            canonical_rel = str(canonical.path)

        for duplicate in duplicates:
            copy_tags = tags
            if canonical.perceptual_only:
                # Each rendered PDF page already owns its own comic:/page:
                # sidecar. Do not append the canonical page number to a copy.
                copy_tags = tags - self._pdf_page_base_tags(canonical.path)
            if self.write_sidecars:
                self._write_sidecar_results(duplicate.path, copy_tags, urls)
            # Normally this is the canonical's Hydrus SHA-256, which proves
            # the tags already belong to this same byte-identical record. If
            # its earlier push failed, let this copy have one recovery attempt.
            sha256 = canonical_sha256
            if self.has_hydrus and not sha256 and (copy_tags or urls):
                sha256 = self._hydrus_push(duplicate.path, copy_tags, urls)
            duplicate.ledger.record(
                duplicate.path.name, duplicate.size, duplicate.mtime,
                duplicate.md5, "matched", sources,
                duplicate_of=canonical_rel, sha256=sha256)
        return len(duplicates)

    @staticmethod
    def _read_result_sidecar(path: Path) -> Set[str]:
        try:
            return set(path.read_text("utf-8").splitlines()) if path.exists() else set()
        except OSError as e:
            notify(f"⚠️  Couldn't read duplicate source sidecar {path.name}: {e}")
            return set()

    def _propagate_prior_duplicate_groups(
            self, root: Path, duplicate_groups: Dict[Path, List[FileItem]],
            ledger_mgr: LedgerManager) -> int:
        """Resolve new copies whose canonical file was matched on an earlier run."""
        copied_total = 0
        for canonical_path, copies in list(duplicate_groups.items()):
            try:
                st = canonical_path.stat()
            except OSError:
                continue
            ledger = ledger_mgr.get(canonical_path.parent)
            if ledger.status_for(canonical_path.name, st.st_size, st.st_mtime) != "matched":
                continue
            rec = ledger.records.get(canonical_path.name) or {}
            canonical = FileItem(
                canonical_path, str(canonical_path.relative_to(root)),
                st.st_size, st.st_mtime, self._media_kind(canonical_path.name) or "image",
                ledger=ledger, md5=rec.get("md5"),
                sha256=ledger.sha256_for(canonical_path.name, st.st_size, st.st_mtime))
            c_tags, c_urls = self.read_sidecar_payload(canonical_path)
            copied_total += self._propagate_duplicate_results(
                root, canonical, copies, c_tags, c_urls,
                list(rec.get("sources") or []), canonical.sha256)
            duplicate_groups.pop(canonical_path, None)
        return copied_total

    def write_unmatched(self, media: Path,
                        known_sha256: Optional[str] = None) -> Optional[str]:
        """Optionally import a no-match file to Hydrus without tags or URLs."""
        if not (self.has_hydrus and self.hydrus_import and
                self.hydrus_import_unmatched):
            return None
        # It is already local and has no new metadata, so there is nothing to
        # send to Hydrus; return it solely for ledger SHA-256 caching.
        if known_sha256:
            return known_sha256
        return self._hydrus_push(media, set(), set())

    # ── Hydrus Client API push ───────────────────────────────────────────────

    def _hydrus_push(self, media: Path, tags: Set[str], urls: Set[str],
                     known_sha256: Optional[str] = None) -> Optional[str]:
        """Import (optional) + tag + associate URLs for one file. Thread-safe.

        Returns the file's SHA-256 (from Hydrus's import response, or computed
        locally in tag-only mode) so the caller can cache it in the ledger and
        avoid recomputing it later. Returns None if the push was aborted.

        Safety: only *adds* content (never deletes files/tags/URLs). If import
        is on and the import is refused (previously deleted, vetoed, error),
        we abort the whole push — we do NOT fall through to bare-hash tagging.
        """
        with self._hydrus_lock:
            try:
                if known_sha256:
                    # Found by this run's Hydrus MD5 search, so it is known to
                    # be currently local. Avoid making Hydrus hash/import it
                    # all over again just to receive the same SHA-256.
                    file_hash, import_status = known_sha256, 2
                elif self.hydrus_import:
                    # Must get an accepted import (status 1/2). A known-deleted
                    # file is the single exception: its metadata can be applied
                    # to current members of its Hydrus duplicate group.
                    added = self._hydrus_add_file(media)
                    if not added:
                        return None
                    file_hash, import_status = added
                    if import_status == 3:
                        self._hydrus_push_to_deleted_duplicates(
                            media, file_hash, tags, urls)
                        # Never cache or show the deleted source hash as if it
                        # were a current local file.
                        return None
                else:
                    # Tag-only mode: file must already live in Hydrus under this
                    # hash — so it's an existing file, never a fresh import.
                    file_hash = self._sha256_local(media)
                    if not file_hash:
                        notify(f"❌ Hydrus: no hash for {media.name}; skipped push.")
                        return None
                    import_status = 2

                if tags:
                    self._hydrus_add_tags(file_hash, tags)
                if urls and self.hydrus_can_edit_urls:
                    # Isolate URL failures: a bad/forbidden associate_url must not
                    # abort the tag push, the results-page add, or hash caching.
                    try:
                        self._hydrus_associate_urls(file_hash, urls)
                    except Exception as e:
                        notify(f"⚠️  Hydrus URL association failed for "
                               f"{media.name}: {e}")
                # A no-match status-2 file gained no metadata, so do not
                # mislabel it on the "Newly Tagged" page. Brand-new no-match
                # imports still belong on New Imports.
                if import_status == 1:
                    self._hydrus_add_to_page("new", file_hash)
                elif tags or urls:
                    self._hydrus_add_to_page("updated", file_hash)
                return file_hash
            except Exception as e:
                notify(f"❌ Hydrus push failed for {media.name}: {e}")
                return None

    def _hydrus_post(self, endpoint: str, body: dict, timeout: int) -> requests.Response:
        """POST to a Hydrus Client API endpoint with the standard headers."""
        return self.session.post(
            f"{self.hydrus_api_url}/{endpoint}",
            headers={**self._hydrus_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )

    def _hydrus_add_file(self, media: Path) -> Optional[Tuple[str, int]]:
        """POST /add_files/add_file by path. Returns (SHA-256 hex, import status)
        on success — status 1 = newly imported, 2 = already in the db."""
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
        # 1 = imported, 2 = already in db — both give us a usable hash.
        # 3 is known-deleted. Keep its hash long enough to look up current
        # duplicate-group members, but never tag or cache this deleted record.
        if status in (1, 2) and h:
            return h, status
        if status == 3 and h:
            return h, status
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

    def _hydrus_push_to_deleted_duplicates(
            self, media: Path, deleted_hash: str, tags: Set[str],
            urls: Set[str]) -> bool:
        """Tag only current members of a deleted file's Hydrus duplicate group.

        Hydrus relationship type ``8`` is the duplicate group; alternates use
        type ``3`` and are intentionally never considered. The API's default
        file domain is combined current local files, so trashed/deleted members
        are excluded before any tag or URL write is attempted.
        """
        if not tags and not urls:
            return False
        if not (self.hydrus_tag_deleted_duplicates and
                self.hydrus_can_manage_relationships):
            notify(f"⚠️  Hydrus: {media.name} was previously deleted — not tagging.")
            return False
        try:
            r = self.session.get(
                f"{self.hydrus_api_url}/manage_file_relationships/"
                "get_file_relationships",
                headers=self._hydrus_headers(), params={"hash": deleted_hash},
                timeout=30,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            relationships = (r.json().get("file_relationships") or {}).get(
                deleted_hash, {})
            targets = sorted({h for h in relationships.get(
                HYDRUS_RELATIONSHIP_DUPLICATES, [])
                if isinstance(h, str) and len(h) == 64 and h != deleted_hash})
            if not targets:
                notify(f"⚠️  Hydrus: {media.name} was previously deleted; no "
                       "current duplicate-group members to tag.")
                return False
            for target_hash in targets:
                if tags:
                    self._hydrus_add_tags(target_hash, tags)
                if urls and self.hydrus_can_edit_urls:
                    try:
                        self._hydrus_associate_urls(target_hash, urls)
                    except Exception as e:
                        # A bad URL must not prevent the remaining duplicate
                        # members from receiving the authoritative tags.
                        notify(f"⚠️  Hydrus URL association failed for deleted "
                               f"{media.name}: {e}")
                self._hydrus_add_to_page("updated", target_hash)
            notify(f"✅ Hydrus: {media.name} was deleted; tagged {len(targets)} "
                   "current duplicate-group file(s).")
            return True
        except (requests.RequestException, ValueError, RuntimeError, TypeError) as e:
            notify(f"⚠️  Hydrus: couldn't tag duplicate-group members for "
                   f"deleted {media.name}: {e}")
            return False

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

    def _hydrus_add_to_page(self, kind: str, file_hash: str) -> None:
        """Keep a result-page hash, retaining only the newest configured N.

        Hydrus's public page API can append but cannot remove individual files,
        so pages are created once at the end of the run from this rolling list.
        """
        page = self.hydrus_result_pages.get(kind)
        if not page or not page["enabled"]:
            return
        hashes = page["hashes"]
        if file_hash in hashes:
            hashes.remove(file_hash)
        hashes.append(file_hash)
        if self.hydrus_results_page_limit:
            del hashes[:-self.hydrus_results_page_limit]

    def _hydrus_flush_result_pages(self) -> None:
        """Create this run's hash-locked result pages from their rolling lists."""
        if not self.has_hydrus:
            return
        with self._hydrus_lock:
            for page in self.hydrus_result_pages.values():
                if not page["enabled"] or not page["hashes"]:
                    continue
                name = page["name"]
                body = {
                    "page_type": 6,
                    "page_name": name,
                    "hashes": page["hashes"],
                    "system_hash_locked": True,
                    "focus_page": False,
                }
                try:
                    r = self._hydrus_post("manage_pages/new_page", body, 30)
                    if r.status_code != 200:
                        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
                except Exception as e:
                    page["enabled"] = False
                    notify(f"⚠️  Hydrus '{name}' page unavailable ({e}); "
                           "continuing without it.")

    def _hydrus_populate_already_tagged_page(
            self, ledger_mgr: LedgerManager, limit: Optional[int]) -> int:
        """Create an unfocused page from unchanged `matched` ledger records.

        `limit` is None to skip the page entirely, 0 for every matched file, or
        N (>0) for the N most recently tagged (by each record's `tagged_at`).

        Old ledgers only contain MD5, while Hydrus page APIs require SHA-256.
        Missing SHA-256 values are calculated in parallel once and cached back
        into their ledger records. Unknown/non-local hashes are harmlessly
        omitted by Hydrus's local file-search page.
        """
        if limit is None:
            return 0
        if not (self.has_hydrus and self.hydrus_already_tagged_page_enabled):
            return 0

        entries: List[Tuple[Path, Ledger, str, int, float, Optional[str], float]] = []
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
                # Records predating tagged_at sort oldest (fall to the tail).
                tagged_at = rec.get("tagged_at") or 0.0
                entries.append((path, ledger, name, st.st_size, st.st_mtime,
                                ledger.sha256_for(name, st.st_size, st.st_mtime),
                                tagged_at))

        if not entries:
            return 0

        # Keep only the N most recently tagged before we hash anything.
        if limit > 0 and len(entries) > limit:
            entries.sort(key=lambda e: e[6], reverse=True)
            entries = entries[:limit]

        missing = [entry for entry in entries if not entry[5]]
        if missing:
            print(f"🏷️  Preparing {self.hydrus_already_tagged_page_name} page "
                  f"({len(entries)} ledger match(es))…")
            workers = min(8, max(1, os.cpu_count() or 1))
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(self._sha256_local, entry[0]): entry
                           for entry in missing}
                for future in cf.as_completed(futures):
                    path, ledger, name, size, mtime, _, _ = futures[future]
                    try:
                        sha256 = future.result()
                    except Exception as e:
                        notify(f"❌ SHA256 failed on {path.name}: {e}")
                        continue
                    if sha256:
                        ledger.cache_sha256(name, size, mtime, sha256)

        hashes: List[str] = []
        seen_hashes: Set[str] = set()
        for path, ledger, name, size, mtime, cached, _ in entries:
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

    @staticmethod
    def _has_prior_matched_files(ledger_mgr: LedgerManager) -> bool:
        """True only when an unchanged, valid matched ledger record exists."""
        for ledger in ledger_mgr.touched():
            if not ledger.path.exists():
                continue
            for name, rec in ledger.records.items():
                if not isinstance(rec, dict) or rec.get("status") != "matched":
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) == "matched":
                    return True
        return False

    def _hydrus_import_prior_nomatches(self, ledger_mgr: LedgerManager) -> int:
        """Import unchanged old no-match files once when the run toggle is on."""
        if not (self.has_hydrus and self.hydrus_import and
                self.hydrus_import_unmatched):
            return 0
        entries: List[Tuple[Path, Ledger, str, int, float]] = []
        for ledger in ledger_mgr.touched():
            for name, rec in ledger.records.items():
                if (not isinstance(rec, dict) or rec.get("status") != "nomatch" or
                        rec.get("sha256")):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) == "nomatch":
                    entries.append((path, ledger, name, st.st_size, st.st_mtime))
        if entries:
            print(f"📥 Importing {len(entries)} prior no-match file(s) to Hydrus…")
        imported = 0
        for path, ledger, name, size, mtime in entries:
            sha = self.write_unmatched(path)
            if sha:
                ledger.cache_sha256(name, size, mtime, sha)
                imported += 1
        return imported

    # ── PDF pre-pass ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_pdfs(root: Path) -> List[Path]:
        """Every non-dotfile PDF under *root*, in natural walk order.

        One definition, so the render planner and the "PDF rendering disabled"
        path agree on which PDFs exist (and both skip macOS `._` shadow files).
        """
        pdfs: List[Path] = []
        for dp, dirs, files in os.walk(root):
            dirs.sort()
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                if Path(fn).suffix.lower() in PDF_EXTS:
                    pdfs.append(Path(dp) / fn)
        return pdfs

    def pdf_page_dirs_for(self, root: Path) -> Set[Path]:
        """Page-output folder for every PDF under *root* (no rendering)."""
        return {pdf.parent / pdf.stem for pdf in self._find_pdfs(root)}

    def plan_pdf_renders(self, root: Path) -> Tuple[Set[Path], List[Path]]:
        """Discover PDF page folders and return the PDFs that still need rendering.

        The caller launches `render_pdf_jobs` in a background worker, while
        excluding those jobs' output folders from its initial index so a
        half-written page can never enter the pipeline.
        """
        pdfs = self._find_pdfs(root)
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

    @staticmethod
    def _clear_partial_pdf_render(pdf: Path) -> None:
        """Remove only this PDF's precisely named partial page outputs."""
        out_dir = pdf.parent / pdf.stem
        if not out_dir.is_dir():
            return
        pattern = _pdf_page_pattern(pdf, include_txt=True)
        try:
            targets = [p for p in out_dir.iterdir()
                       if p.is_file() and pattern.match(p.name)]
        except OSError:
            return
        for path in targets:
            try:
                path.unlink()
            except OSError:
                pass

    def render_pdf_jobs(self, pdfs: List[Path], dpi: int,
                        completed: Optional["queue.Queue"] = None) -> List[Path]:
        """Render planned PDFs serially with adaptive oversized-page fallback."""
        generated: List[Path] = []
        for pdf in pdfs:
            if self.cancelled():
                break
            attempt_dpi = dpi
            pdf_generated: List[Path] = []
            while True:
                try:
                    out = self.settings.output
                    pdf_generated = convert_pdf(
                        pdf, pdf.parent, attempt_dpi,
                        write_sidecars=(self.write_sidecars and
                                        self.settings.pdf.pdf_write_sidecars),
                        sidecar_format=out.sidecar_format,
                        tag_pattern=out.sidecar_tag_filename,
                        json_pattern=out.sidecar_json_filename,
                        should_cancel=self.cancelled)
                    break
                except Exception as e:
                    oversized = "overly large image" in str(e).lower()
                    next_dpi = max(72, attempt_dpi // 2)
                    if oversized and next_dpi < attempt_dpi:
                        self._clear_partial_pdf_render(pdf)
                        notify(f"⚠️  {pdf.name} is too large at {attempt_dpi} DPI; "
                               f"retrying losslessly at {next_dpi} DPI.")
                        attempt_dpi = next_dpi
                        continue
                    self._clear_partial_pdf_render(pdf)
                    notify(f"⚠️  Failed to render {pdf.name}: {e}")
                    break
            generated += pdf_generated
            if completed is not None:
                completed.put((pdf.name, pdf_generated, attempt_dpi))
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
        scanned_dirs = 0
        discovered_media = 0
        items: List[FileItem] = []
        candidate_dirs: Set[Path] = set()

        for dp, dirs, files in os.walk(root):
            scanned_dirs += 1
            dirs.sort()
            dp_path = Path(dp)
            if excluded_dirs:
                dirs[:] = [d for d in dirs if dp_path / d not in excluded_dirs]

            media_files = [fn for fn in sorted(files)
                           if not fn.startswith(".") and self._media_kind(fn)]
            discovered_media += len(media_files)
            if sys.stdout.isatty() and scanned_dirs % 100 == 0:
                sys.stdout.write(
                    f"\r  indexed {scanned_dirs:,} folders · "
                    f"{discovered_media:,} media found")
                sys.stdout.flush()
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
                # pending_review stays eligible (unresolved)
                if status in RESOLVED_LEDGER_STATUSES:
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
        if sys.stdout.isatty():
            sys.stdout.write("\r\033[2K")
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
            if status in RESOLVED_LEDGER_STATUSES:
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
                        RESOLVED_LEDGER_STATUSES):
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
        if _display is not None:
            _display.status("perceptual", f"local hash · {len(todo)} PDF page(s)")
        else:
            print(f"🔢 Hashing {len(todo)} files (×{workers})…")
        done = 0
        dirty_ledgers: Set[Ledger] = set()
        with cf.ThreadPoolExecutor(max_workers=workers) as ex:
            futmap = {ex.submit(self._md5_local, it.path): it for it in todo}
            for fut in cf.as_completed(futmap):
                item = futmap[fut]
                item.md5 = fut.result()
                if item.md5:
                    item.ledger.cache_md5(
                        item.path.name, item.size, item.mtime, item.md5)
                    dirty_ledgers.add(item.ledger)
                done += 1
                # Persist reusable hashes during the disk pass. An interrupted
                # run loses at most one small batch rather than the whole pass.
                if done % 25 == 0:
                    for ledger in dirty_ledgers:
                        ledger.save()
                    dirty_ledgers.clear()
                if (_display is None and sys.stdout.isatty() and
                        (done % 25 == 0 or done == len(todo))):
                    sys.stdout.write(f"\r  hashed {done}/{len(todo)}")
                    sys.stdout.flush()
        for ledger in dirty_ledgers:
            ledger.save()
        if _display is None and sys.stdout.isatty():
            sys.stdout.write("\n")

    def deduplicate(self, root: Path, items: List[FileItem],
                    ledger_mgr: LedgerManager,
                    canonical_items: Optional[List[FileItem]] = None
                    ) -> Tuple[List[FileItem], int, Dict[Path, List[FileItem]]]:
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
        duplicate_groups: Dict[Path, List[FileItem]] = {}
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
            duplicate_groups.setdefault(canonical, []).append(item)
            duplicate_count += 1

        self._write_duplicates_log(root, ledger_mgr)
        return survivors, duplicate_count, duplicate_groups

    @staticmethod
    def _write_duplicates_log(root: Path, ledger_mgr: LedgerManager) -> None:
        groups: Dict[Tuple[str, str], List[str]] = {}
        for ledger in ledger_mgr.touched():
            for name, rec in ledger.records.items():
                if (not isinstance(rec, dict) or
                        (rec.get("status") != "duplicate" and
                         not rec.get("duplicate_of"))):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) not in (
                        "duplicate", "matched"):
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

    def hash_tier(self, item: FileItem, disp: Optional[LiveDisplay], ex: cf.Executor
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

        def _tick(state: Dict[str, str]) -> None:
            # No panel (GUI/headless) → nothing to render; never allocate one.
            if disp is not None:
                disp.status("hash", disp.hash_line(state))

        state = {s: "run" for s in services}
        futs = {ex.submit(self._hash_lookup, s, item.md5): s for s in services}
        _tick(state)
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                t, u = fut.result()
            except Exception as e:
                # Network/HTTP failure — distinct from a clean "not found" miss,
                # so surface it as ⚠ rather than ✗ (the file may still exist there).
                notify(f"❌ {s} failed on {item.path.name}: {e}")
                state[s] = "err"
                _tick(state)
                continue
            if t or u:
                tags |= t
                urls |= u
                hit.add(s)
                state[s] = "hit"
            else:
                state[s] = "miss"
            _tick(state)

        sources = [s for s in services if s in hit]   # deterministic order
        return tags, urls, sources

    # ── Perceptual tier (Fluffle → SauceNAO, sequential) ─────────────────────

    def perceptual_tier(self, item: FileItem, disp: Optional[LiveDisplay] = None
                        ) -> Tuple[Set[str], Set[str], List[str], Optional[Dict]]:
        """Run Fluffle → SauceNAO. Returns (tags, urls, sources, review_raw).

        When *review_raw* is set, the caller should queue a PendingReview and
        not write final results / nomatch for this file.
        """
        tags: Set[str] = set()
        urls: Set[str] = set()
        sources: List[str] = []
        review_raw: Optional[Dict] = None
        fp = item.path

        def _status(msg: str) -> None:
            if disp is not None:
                disp.status("perceptual", msg)

        if self.source_active("fluffle"):
            _status("Fluffle…")
            js = self.fluffle_search(fp)
            if js:
                f_tags, f_urls, md5_u, pid, review_raw = self.find_best_exact_match(js)
                if f_tags or f_urls:
                    tags |= f_tags
                    urls |= f_urls
                    # A perceptual hit only tells us which post this is — re-query
                    # e621 by ID for the full, properly-namespaced tag set.
                    if self.source_active("e621") and (pid or md5_u):
                        _status("Fluffle → e621 enrich…")
                        e_tags, e_urls = (self.e621_lookup_by_id(pid)
                                          if pid else (set(), set()))
                        if not e_tags and md5_u:
                            e_tags, e_urls = self.e621_lookup_by_md5(md5_u)
                        tags |= e_tags
                        urls |= e_urls
                    sources.append("fluffle")
                    review_raw = None  # auto-accepted; no review needed

        # SauceNAO is the slowest stage (6s pace, longer after a quota backoff),
        # so don't start it for a file the user has already cancelled out of.
        if (not (tags or urls) and review_raw is None
                and not self.cancelled() and self.source_active("saucenao")):
            _status("SauceNAO…")
            service, rid, s_tags, s_urls = self.saucenao_search(fp)
            if service and rid:
                # High-confidence booru match → pull the authoritative,
                # properly-namespaced tag set instead of SauceNAO's own.
                _status(f"SauceNAO → {service} enrich…")
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

        return tags, urls, sources, review_raw

    def _queue_pending_review(self, item: FileItem, root: Path,
                              review_raw: Dict) -> PendingReview:
        """Persist a pending_review ledger status + ReviewQueue entry."""
        platform = review_raw.get("platform", "") or ""
        loc = review_raw.get("location", "") or ""
        match_class = review_raw.get("match", "") or ""
        tags: Set[str] = set()
        urls: Set[str] = set()
        for c in review_raw.get("credits", []) or []:
            name = EMOJI_PATTERN.sub("", (c or {}).get("name", "")).strip()
            if name:
                tags.add(f"creator:{name}")
        if platform:
            platform_clean = EMOJI_PATTERN.sub("", platform).strip()
            if platform_clean:
                tags.add(f"site:{platform_clean}")
        if loc:
            urls.add(loc)
        pending = PendingReview.create(
            path=str(item.path.resolve()),
            relpath=item.relpath,
            size=item.size,
            mtime=item.mtime,
            md5=item.md5,
            source="fluffle",
            match_class=match_class,
            platform=platform,
            location=loc,
            post_id=self._post_id_from_url(loc),
            md5_from_url=self._md5_from_url(loc),
            fluffle_tags=sorted(tags),
            fluffle_urls=sorted(urls),
        )
        if self._review_queue is not None:
            self._review_queue.add(pending)
        item.ledger.record(item.path.name, item.size, item.mtime, item.md5,
                           "pending_review", ["pending_review"])
        return pending

    def resolve_pending_review(self, pending: PendingReview, approve: bool,
                               root: Optional[Path] = None) -> bool:
        """Approve (enrich + write) or reject (nomatch) a pending review item."""
        path = Path(pending.path)
        if not path.is_file():
            return False
        try:
            st = path.stat()
        except OSError:
            return False
        ledger = Ledger(path.parent)
        ledger.load()
        if approve:
            tags: Set[str] = set(pending.fluffle_tags or [])
            urls: Set[str] = set(pending.fluffle_urls or [])
            sources = ["fluffle"]
            pid = pending.post_id
            md5_u = pending.md5_from_url
            if self.source_active("e621") and (pid or md5_u):
                e_tags, e_urls = (self.e621_lookup_by_id(pid)
                                  if pid else (set(), set()))
                if not e_tags and md5_u:
                    e_tags, e_urls = self.e621_lookup_by_md5(md5_u)
                tags |= e_tags
                urls |= e_urls
            if path.suffix.lower() == ".png":
                tags |= self._pdf_page_base_tags(path)
            sha = self.write_results(path, tags, urls)
            ledger.record(path.name, st.st_size, st.st_mtime, pending.md5,
                          "matched", sources, sha256=sha)
        else:
            sha = self.write_unmatched(path)
            ledger.record(path.name, st.st_size, st.st_mtime, pending.md5,
                          "nomatch", [], sha256=sha)
        ledger.save()
        if self._review_queue is not None:
            self._review_queue.remove(pending.id)
        elif root is not None:
            rq = ReviewQueue(root)
            rq.load()
            rq.remove(pending.id)
        return True

    # ── Orchestration ────────────────────────────────────────────────────────

    def discover(self, root: Path, ledger_mgr: Optional[LedgerManager] = None
                 ) -> Dict:
        """Read-only discovery: index + PDF plan. No network mutations.

        Returns a dict with keys: items, candidate_dirs, pdf_page_dirs,
        pdf_jobs, ledger_mgr, media counts via index side-effects printed.
        """
        root = Path(root).resolve()
        ledger_mgr = ledger_mgr or LedgerManager()
        if self.settings.pdf.pdf_enabled:
            pdf_page_dirs, pdf_jobs = self.plan_pdf_renders(root)
        else:
            # Still collect existing page dirs so perceptual_only flags work
            pdf_page_dirs, pdf_jobs = self.pdf_page_dirs_for(root), []
        pending_pdf_dirs = {pdf.parent / pdf.stem for pdf in pdf_jobs}
        items, candidate_dirs = self.index(
            root, ledger_mgr, pdf_page_dirs, excluded_dirs=pending_pdf_dirs)
        return {
            "root": root,
            "items": items,
            "candidate_dirs": candidate_dirs,
            "pdf_page_dirs": pdf_page_dirs,
            "pdf_jobs": pdf_jobs,
            "ledger_mgr": ledger_mgr,
        }

    def run(self, root: Path,
            options: Optional[RunOptions] = None,
            observer: Optional[RunObserver] = None,
            cancel_event: Optional[threading.Event] = None,
            use_terminal_display: bool = True,
            pdf_dpi: Optional[int] = None) -> ScanSummary:
        """Full pipeline. Returns ScanSummary. Cooperative cancellation via cancel_event."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("A scan is already running in this process.")
        try:
            return self._run_impl(
                root, options, observer, cancel_event,
                use_terminal_display, pdf_dpi)
        finally:
            self._run_lock.release()

    def _run_impl(self, root: Path,
                  options: Optional[RunOptions],
                  observer: Optional[RunObserver],
                  cancel_event: Optional[threading.Event],
                  use_terminal_display: bool,
                  pdf_dpi: Optional[int]) -> ScanSummary:
        global _display

        if options and options.settings_override is not None:
            self.apply_settings(options.settings_override)
        self.cancel_event = cancel_event or threading.Event()
        self.cancel_event.clear()
        self._bind_cancel_to_pacers()
        summary = ScanSummary(
            source_hits={k: 0 for k in
                         ("e621", "inkbunny", "danbooru",
                          "gelbooru", "fluffle", "saucenao")})

        # Per-scan result pages
        for page in self.hydrus_result_pages.values():
            page["hashes"].clear()

        # Apply run options onto instance
        if options is not None:
            self.hydrus_import_unmatched = options.import_unmatched
            self.hydrus_results_page_limit = options.result_page_limit
            if options.build_already_tagged_page:
                self.hydrus_already_tagged_page_limit = options.result_page_limit
            else:
                self.hydrus_already_tagged_page_limit = None
            if options.sync_sidecars and self.has_hydrus:
                self.sync_sidecars_to_hydrus(Path(root))

        discovery = self.discover(root)
        root = discovery["root"]
        items: List[FileItem] = discovery["items"]
        candidate_dirs: Set[Path] = discovery["candidate_dirs"]
        pdf_page_dirs: Set[Path] = discovery["pdf_page_dirs"]
        pdf_jobs: List[Path] = discovery["pdf_jobs"]
        ledger_mgr: LedgerManager = discovery["ledger_mgr"]

        self._review_queue = ReviewQueue(root)
        self._review_queue.load()

        pdf_executor: Optional[cf.ThreadPoolExecutor] = None
        pdf_future = None
        pdf_completed: "queue.Queue" = queue.Queue()

        # Resolve PDF DPI from options / settings / prompt — only when
        # rendering is enabled and discover actually queued jobs.
        if pdf_jobs and self.settings.pdf.pdf_enabled:
            if pdf_dpi is not None:
                chosen_dpi = pdf_dpi
            elif options is not None and options.pdf_dpi is not None:
                chosen_dpi = options.pdf_dpi
            elif use_terminal_display and sys.stdin.isatty():
                chosen_dpi = prompt_for_pdf_dpi(len(pdf_jobs))
            else:
                chosen_dpi = self.settings.pdf.pdf_dpi or PDF_DPI
            print(f"📄 Rendering {len(pdf_jobs)} PDF(s) at {chosen_dpi} DPI in background…")
            pdf_executor = cf.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="pdf-render")
            pdf_future = pdf_executor.submit(
                self.render_pdf_jobs, pdf_jobs, chosen_dpi, pdf_completed)
        elif pdf_jobs and not self.settings.pdf.pdf_enabled:
            # Defensive: discover should have returned [] when disabled.
            pdf_jobs = []

        prior_imported = self._hydrus_import_prior_nomatches(ledger_mgr)
        if prior_imported:
            ledger_mgr.save_all()
            print(f"✅ Hydrus imported {prior_imported} prior no-match file(s)")
        has_prior_matches = self._has_prior_matched_files(ledger_mgr)
        at_limit = (self.hydrus_already_tagged_page_limit
                    if (self.has_hydrus and self.hydrus_already_tagged_page_enabled
                        and has_prior_matches)
                    else None)
        already_tagged = self._hydrus_populate_already_tagged_page(
            ledger_mgr, at_limit)
        if already_tagged:
            ledger_mgr.save_all()
            print(f"✅ Already Tagged page → {already_tagged} ledger-matched file(s)")
        if not items and pdf_future is None:
            print("✅ Nothing to do — everything is tagged or already checked.")
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            ledger_mgr.save_all()
            summary.pending_review = len(self._review_queue) if self._review_queue else 0
            return summary
        if self.cancelled():
            summary.cancelled = True
            ledger_mgr.save_all()
            return summary

        self.hash_all(items)
        items, duplicates, duplicate_groups = self.deduplicate(
            root, items, ledger_mgr)
        summary.duplicates = duplicates
        prior_duplicates_tagged = self._propagate_prior_duplicate_groups(
            root, duplicate_groups, ledger_mgr)
        if prior_duplicates_tagged:
            self._write_duplicates_log(root, ledger_mgr)
            ledger_mgr.save_all()
            print(f"♊ {prior_duplicates_tagged} prior duplicate copy/copies inherited tags")
        hydrus_cached = self._hydrus_cache_current_hashes(items)
        if hydrus_cached:
            ledger_mgr.save_all()
            print(f"⚡ Hydrus already has {hydrus_cached} file(s); skipping re-import checks")
        if duplicates:
            ledger_mgr.save_all()
            print(f"♊ {duplicates} exact duplicate(s) skipped — see {DUPLICATES_FILE}")
        if not items and pdf_future is None:
            print("✅ Nothing unique left to search.")
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            ledger_mgr.save_all()
            return summary

        print("🔄 Hash tier: e621 · InkBunny · Danbooru · Gelbooru (concurrent, merged)"
              "  →  Perceptual: Fluffle → SauceNAO")
        print(f"   {LiveDisplay._LEGEND}\n")

        counts = summary.source_hits
        tagged = nomatch = pending_review_count = 0
        counts_lock = threading.Lock()
        duplicate_lock = threading.Lock()
        duplicates_tagged = prior_duplicates_tagged

        def _bump_hit(sources: List[str]) -> None:
            nonlocal tagged
            with counts_lock:
                tagged += 1
                for s in sources:
                    counts[s] = counts.get(s, 0) + 1

        def _bump_miss() -> None:
            nonlocal nomatch
            with counts_lock:
                nomatch += 1

        def _bump_pending() -> None:
            nonlocal pending_review_count
            with counts_lock:
                pending_review_count += 1

        def _propagate_duplicates(item: FileItem, tags: Set[str], urls: Set[str],
                                  sources: List[str], sha256: Optional[str]) -> None:
            nonlocal duplicates_tagged
            with duplicate_lock:
                copies = duplicate_groups.pop(item.path, [])
            copied = self._propagate_duplicate_results(
                root, item, copies, tags, urls, sources, sha256)
            if copied:
                with counts_lock:
                    duplicates_tagged += copied

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
            if self._review_queue is not None:
                self._review_queue.save()

        hash_items = [it for it in items if not it.perceptual_only]
        perceptual_q: "queue.Queue" = queue.Queue()
        seed_count = 0
        for it in items:
            if it.perceptual_only:
                perceptual_q.put(it)
                seed_count += 1

        disp: Optional[LiveDisplay] = None
        if use_terminal_display:
            disp = LiveDisplay()
            _display = disp
        self._observer = observer or (
            TerminalObserver(disp) if disp is not None else NullObserver())
        hw = self.settings.performance.hash_worker_count
        hash_workers = hw if hw > 0 else max(1, len(self.enabled_hash_services()) or 1)

        def perceptual_worker() -> None:
            idx = 0
            while True:
                item = perceptual_q.get()
                if item is _PERCEPTUAL_DONE:
                    perceptual_q.task_done()
                    return
                if self.cancelled():
                    perceptual_q.task_done()
                    # Drain remaining without processing
                    continue
                idx += 1
                try:
                    if disp is not None:
                        disp.start_file("perceptual", idx, item.path.name,
                                         f"{perceptual_q.qsize()} queued")
                    self._observer.emit(RunEvent(
                        kind="start_file", track="perceptual", index=idx,
                        current=item.path.name,
                        nxt=f"{perceptual_q.qsize()} queued"))
                    tags, urls, sources, review_raw = self.perceptual_tier(item, disp)
                    if review_raw is not None and not (tags or urls):
                        self._queue_pending_review(item, root, review_raw)
                        _bump_pending()
                        if disp is not None:
                            disp.finish_file("perceptual", "⏳ needs review")
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="perceptual",
                            result="needs review",
                            extra={"pending_review": True}))
                    elif tags or urls:
                        if item.perceptual_only:
                            tags = set(tags) | self._pdf_page_base_tags(item.path)
                        sha = self.write_results(
                            item.path, tags, urls, item.sha256)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "matched", sources,
                                            sha256=sha)
                        _propagate_duplicates(item, tags, urls, sources, sha)
                        _bump_hit(sources)
                        result = f"{'+'.join(sources)}  ({len(tags)} tags)"
                        if disp is not None:
                            disp.finish_file("perceptual", result)
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="perceptual", result=result))
                    else:
                        sha = self.write_unmatched(item.path, item.sha256)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "nomatch", [], sha256=sha)
                        _bump_miss()
                        if disp is not None:
                            disp.finish_file("perceptual", "— no match")
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="perceptual",
                            result="no match"))
                    _maybe_save_ledgers()
                except Exception as e:
                    notify(f"❌ perceptual worker error on {item.path.name}: {e}")
                finally:
                    perceptual_q.task_done()

        try:
            hash_interval = max(
                (self.pace[s].interval for s in self.enabled_hash_services()),
                default=0.0)
            if disp is not None:
                disp.begin_phase(
                    "hash", "Phase · hash lookups (e621·InkBunny·Danbooru·Gelbooru)",
                    len(hash_items), interval=hash_interval)
                disp.begin_phase(
                    "perceptual", "Phase · perceptual (Fluffle → SauceNAO)",
                    seed_count, growing=True)
            self._observer.emit(RunEvent(
                kind="begin_phase", track="hash",
                phase="Phase · hash lookups", total=len(hash_items),
                extra={"interval": hash_interval}))
            self._observer.emit(RunEvent(
                kind="begin_phase", track="perceptual",
                phase="Phase · perceptual", total=seed_count,
                extra={"growing": True}))

            perc_thread = threading.Thread(
                target=perceptual_worker, name="perceptual-worker", daemon=True)
            perc_thread.start()

            with cf.ThreadPoolExecutor(max_workers=hash_workers) as ex:
                for i, item in enumerate(hash_items):
                    if self.cancelled():
                        break
                    nxt = hash_items[i + 1].path.name if i + 1 < len(hash_items) else None
                    if disp is not None:
                        disp.start_file("hash", i + 1, item.path.name, nxt)
                    self._observer.emit(RunEvent(
                        kind="start_file", track="hash", index=i + 1,
                        current=item.path.name, nxt=nxt or ""))

                    tags, urls, sources = self.hash_tier(item, disp, ex)
                    if tags or urls:
                        sha = self.write_results(
                            item.path, tags, urls, item.sha256)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "matched", sources,
                                            sha256=sha)
                        _propagate_duplicates(item, tags, urls, sources, sha)
                        _bump_hit(sources)
                        result = f"{'+'.join(sources)}  ({len(tags)} tags)"
                        if disp is not None:
                            disp.finish_file("hash", result)
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="hash", result=result))
                    elif item.kind == "image":
                        perceptual_q.put(item)
                        if disp is not None:
                            disp.grow("perceptual")
                            disp.finish_file("hash", "no hash match → perceptual")
                        self._observer.emit(RunEvent(kind="grow", track="perceptual"))
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="hash",
                            result="no hash match → perceptual"))
                    else:                                  # video: hash-only
                        sha = self.write_unmatched(item.path, item.sha256)
                        item.ledger.record(item.path.name, item.size, item.mtime,
                                            item.md5, "nomatch", [], sha256=sha)
                        _bump_miss()
                        if disp is not None:
                            disp.finish_file("hash", "— no match")
                        self._observer.emit(RunEvent(
                            kind="finish_file", track="hash", result="no match"))
                    _maybe_save_ledgers()

            perceptual_q.join()

            if pdf_future is not None and not self.cancelled():
                pdfs_received = 0
                while not pdf_future.done() or not pdf_completed.empty():
                    if self.cancelled():
                        break
                    try:
                        pdf_name, rendered_paths, effective_dpi = pdf_completed.get(
                            timeout=0.25)
                    except queue.Empty:
                        if disp is not None:
                            disp.status(
                                "perceptual",
                                f"waiting for PDF render · "
                                f"{pdfs_received}/{len(pdf_jobs)} complete")
                        continue
                    pdfs_received += 1
                    pdf_items = self.index_rendered_pdf_pages(
                        rendered_paths, root, ledger_mgr)
                    self.hash_all(pdf_items)
                    pdf_items, pdf_duplicates, pdf_duplicate_groups = self.deduplicate(
                        root, pdf_items, ledger_mgr, canonical_items=items)
                    with duplicate_lock:
                        for canonical, copies in pdf_duplicate_groups.items():
                            duplicate_groups.setdefault(canonical, []).extend(copies)
                    hydrus_cached = self._hydrus_cache_current_hashes(pdf_items)
                    if hydrus_cached:
                        ledger_mgr.save_all()
                    if pdf_duplicates:
                        duplicates += pdf_duplicates
                        summary.duplicates = duplicates
                        notify(f"♊ {pdf_duplicates} duplicate PDF page(s) skipped; "
                               f"see {DUPLICATES_FILE}.")
                    for item in pdf_items:
                        candidate_dirs.add(item.path.parent)
                        perceptual_q.put(item)
                        if disp is not None:
                            disp.grow("perceptual")
                        self._observer.emit(RunEvent(kind="grow", track="perceptual"))
                    items.extend(pdf_items)
                    pdf_completed.task_done()
                    if disp is not None:
                        disp.status(
                            "perceptual",
                            f"PDF {pdfs_received}/{len(pdf_jobs)} · {effective_dpi} DPI · "
                            f"{pdf_name}")
                    perceptual_q.join()
                try:
                    pdf_future.result()
                except Exception as e:
                    notify(f"⚠️  Background PDF rendering failed: {e}")

            if disp is not None:
                disp.freeze_total("perceptual")
            self._observer.emit(RunEvent(kind="freeze_total", track="perceptual"))
            perceptual_q.put(_PERCEPTUAL_DONE)
            perc_thread.join()
        finally:
            if pdf_executor is not None:
                pdf_executor.shutdown(wait=True)
            self._hydrus_flush_result_pages()
            if disp is not None:
                disp.close()
            _display = None
            self.finalize_dir_fingerprints(candidate_dirs, pdf_page_dirs, ledger_mgr)
            self._write_duplicates_log(root, ledger_mgr)
            ledger_mgr.save_all()
            if self._review_queue is not None:
                self._review_queue.save()

        summary.tagged = tagged
        summary.unmatched = nomatch
        summary.pending_review = pending_review_count
        if self._review_queue is not None:
            summary.pending_review = max(
                summary.pending_review, len(self._review_queue))
        summary.duplicates = duplicates
        summary.total_items = len(items)
        summary.cancelled = self.cancelled()
        if duplicates_tagged:
            summary.duplicates = max(summary.duplicates, duplicates_tagged)

        # ── Summary ──────────────────────────────────────────────────────────
        total = len(items)
        label = "CANCELLED" if summary.cancelled else "DONE"
        print(f"\n🏁 {label}")
        print(f"Total tagged:        {tagged}/{total}")
        print(f"  ├─ e621 hits:      {counts['e621']}")
        print(f"  ├─ InkBunny hits:  {counts['inkbunny']}")
        print(f"  ├─ Danbooru hits:  {counts['danbooru']}")
        print(f"  ├─ Gelbooru hits:  {counts['gelbooru']}")
        print(f"  ├─ Fluffle hits:   {counts['fluffle']}")
        print(f"  ├─ SauceNAO hits:  {counts['saucenao']}")
        print(f"  └─ No match:       {nomatch}")
        if pending_review_count:
            print(f"  └─ Needs review:   {pending_review_count}")
        if self.saucenao_exhausted:
            print("⚠️  SauceNAO was skipped after its daily quota was exhausted.")
        if duplicates_tagged:
            print(f"♊ Duplicate copies tagged: {duplicates_tagged}")
        print(f"🗒️  Session ledgers updated across the scanned tree "
              f"({LEDGER_FILE} per folder)")
        return summary


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
    return any(
        name.endswith(ext + ".txt") or
        name.endswith(ext + ".urls.txt") or
        name.endswith(ext + ".json")
        for ext in media_exts
    )


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


def _pdf_page_pattern(pdf: Path, include_txt: bool = False) -> "re.Pattern":
    """Regex matching this PDF's rendered page files (optionally their sidecars)."""
    suffix = r"(?:\.txt)?" if include_txt else ""
    return re.compile(rf"^{re.escape(pdf.stem)} PAGE\d+\.PNG{suffix}$", re.I)


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
        pattern = _pdf_page_pattern(pdf)
        try:
            matches = [p for p in out_dir.iterdir()
                       if p.is_file() and pattern.match(p.name)]
        except OSError:
            continue
        if matches:
            pages.extend(sorted(matches, key=lambda p: _natural_key(p.name)))
            page_dirs.add(out_dir)
    return pages, page_dirs


def is_filesystem_root(path: Path) -> bool:
    """True for a filesystem root, which is never a legal reset target."""
    resolved = path.expanduser().resolve()
    return resolved == Path(resolved.anchor)


def perform_nuke(root: Path, include_pdf_pages: bool = False
                 ) -> Tuple[int, List[Tuple[Path, OSError]]]:
    """Delete FurTag-generated state under *root*.

    The single implementation behind both the CLI ``NUKE!`` prompt and the GUI
    Reset dialog, so the two can't drift. Returns ``(removed, failures)``;
    callers own how failures are reported.
    """
    ledgers, sidecars = _nuke_candidates(root)
    pdf_pages, pdf_page_dirs = _pdf_render_candidates(root)

    removed = 0
    failures: List[Tuple[Path, OSError]] = []
    for path in ledgers + sidecars + (pdf_pages if include_pdf_pages else []):
        try:
            path.unlink()
            removed += 1
        except OSError as e:
            failures.append((path, e))
    if include_pdf_pages:
        # Deepest-first, so nested page folders empty out before their parents.
        for out_dir in sorted(pdf_page_dirs, key=lambda p: len(p.parts), reverse=True):
            try:
                out_dir.rmdir()  # succeeds only when no unrelated content remains
            except OSError:
                pass
    return removed, failures


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
    if is_filesystem_root(root):
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

    removed, failures = perform_nuke(root, include_pdf_pages=reexport_pdfs)
    for path, err in failures:
        print(f"⚠️  Could not delete {path}: {err}")
    print(f"\n💥 Reset complete — removed {removed} generated file(s).")
    if failures:
        print(f"⚠️  {len(failures)} file(s) could not be removed "
              "and may still be skipped.")
    print("🔄 Starting a fresh scan of that folder…\n")
    return root


def prompt_for_already_tagged(page_limit: int) -> Optional[int]:
    """Set the session-wide Already Tagged page policy.

    Uses the same per-window newest-N cap as the other Hydrus review pages, so
    every page created before the launcher exits follows one consistent limit.
    """
    if not sys.stdin.isatty():
        return page_limit
    try:
        ans = input(
            "\n👀 Build an 'Already Tagged' page for ledger-skipped matches "
            "in each scanned folder? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    return page_limit if ans in {"y", "yes"} else None


def prompt_for_unmatched_import() -> bool:
    """Session-wide choice to import files with no tags or source URLs."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(
            "\n📥 Import no-match files to Hydrus without tags "
            "for this session? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def prompt_for_results_page_limit(default: int) -> int:
    """Ask for the launcher-session newest-N cap on every review page."""
    if not sys.stdin.isatty():
        return default
    while True:
        try:
            raw = input(
                "\n👀 Hydrus review-page limit (kept until FurTag closes) "
                f"[0 = unlimited, default {default}]: "
            ).strip().replace(",", "")
        except (EOFError, KeyboardInterrupt):
            return default
        if not raw:
            return default
        try:
            limit = int(raw)
        except ValueError:
            print("   Enter 0 for unlimited, or a whole positive number.")
            continue
        if limit >= 0:
            return limit
        print("   Enter 0 for unlimited, or a whole positive number.")


def prompt_for_sidecar_sync() -> bool:
    """Ask whether this selected folder's existing sidecars should be pushed."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(
            "\n📤 Push existing FurTag .txt tags and .urls.txt URLs to Hydrus "
            "for this folder? [y/N]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def prompt_for_another_folder() -> bool:
    """Offer another scan before the launcher exits."""
    if not sys.stdin.isatty():
        return False
    try:
        answer = input("\n📂 Scan another folder? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


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


def _cli_review_loop(ti: TagIntegrator, root: Path) -> None:
    """Interactive post-run review of pending Fluffle matches (CLI parity)."""
    rq = ti._review_queue or ReviewQueue(root)
    rq.load()
    items = rq.list_items()
    if not items:
        return
    if not sys.stdin.isatty():
        print(f"⏳ {len(items)} file(s) left pending_review for a later GUI session.")
        return
    print(f"\n👀 {len(items)} match(es) need review.")
    for pending in list(items):
        print(f"\n  File:     {pending.relpath}")
        print(f"  Match:    {pending.match_class} on {pending.platform or '?'}")
        print(f"  Location: {pending.location or '(none)'}")
        print(f"  Tags:     {', '.join(pending.fluffle_tags[:8]) or '(none)'}")
        try:
            ans = input("  [a]pprove / [r]eject / [s]kip / [q]uit review: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n  Leaving remaining items pending.")
            break
        if ans in {"q", "quit"}:
            break
        if ans in {"s", "skip", ""}:
            continue
        if ans in {"a", "y", "yes", "approve"}:
            if ti.resolve_pending_review(pending, approve=True, root=root):
                print("  ✅ Approved.")
            else:
                print("  ⚠️  Could not resolve.")
        elif ans in {"r", "n", "no", "reject"}:
            if ti.resolve_pending_review(pending, approve=False, root=root):
                print("  ❌ Rejected (nomatch).")
            else:
                print("  ⚠️  Could not resolve.")


def main() -> None:
    print("🐾 Unified Furry Tag Integrator for Hydrus 🐾")
    print("📋 e621 + InkBunny + Danbooru + Gelbooru MD5 (concurrent) → Fluffle → SauceNAO")
    print("⏭️  Skips files already tagged or logged in .furtag_ledger.json\n")

    store = SettingsStore()
    settings = store.load()
    ti = TagIntegrator(settings=settings)
    ti.load_credentials_from_store(CredentialStore())

    if ti.has_hydrus:
        print(f"📝 Output → Hydrus Client API  ({ti.hydrus_mode_desc()})")
    else:
        print("📝 Output → sidecars  "
              "(<file>.<ext>.txt + <file>.<ext>.urls.txt)")
        print("   Tip: store hydrus_api_url + hydrus_access_key via keyring or "
              "FURTAG_HYDRUS_* environment variables to push into Hydrus.")

    if not ti.any_source():
        print("\n⚠️  No API credentials loaded! Only Fluffle will be used "
              "(if enabled).")
        try:
            input("Press Enter to continue anyway, or Ctrl+C to quit...")
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n👋 Bye.")

    # These Hydrus choices apply to every folder scanned before FurTag exits.
    if ti.has_hydrus:
        if any(page["enabled"] for page in ti.hydrus_result_pages.values()):
            ti.hydrus_results_page_limit = prompt_for_results_page_limit(
                ti.hydrus_results_page_limit)
        if ti.hydrus_already_tagged_page_enabled:
            ti.hydrus_already_tagged_page_limit = prompt_for_already_tagged(
                ti.hydrus_results_page_limit)
        ti.hydrus_import_unmatched = (
            prompt_for_unmatched_import() if ti.hydrus_import else False)

    while True:
        root = prompt_for_folder()
        opts = RunOptions.from_settings(ti.settings)
        opts.import_unmatched = ti.hydrus_import_unmatched
        opts.result_page_limit = ti.hydrus_results_page_limit
        opts.build_already_tagged_page = (
            ti.hydrus_already_tagged_page_limit is not None)
        if ti.has_hydrus and prompt_for_sidecar_sync():
            opts.sync_sidecars = True
        try:
            summary = ti.run(root, options=opts)
            if summary.pending_review:
                _cli_review_loop(ti, root)
        except KeyboardInterrupt:
            ti.request_cancel()
            sys.exit("\n⛔ Interrupted (progress saved to ledger).")

        if not prompt_for_another_folder():
            break


if __name__ == "__main__":
    main()
