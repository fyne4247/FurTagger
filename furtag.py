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
directory a later run scans. Each ledger also seals a directory manifest
(names, sizes, nanosecond mtimes, and sidecar state) once every file in it is
accounted for, so an unchanged folder can be skipped wholesale on the next run
without checking individual records.

Output — choose one or both in Settings:

  A) Hydrus Client API (preferred when configured):
        import file → add tags + direct source notes → associate source URLs
        No sidecar files. Tags land on a local tag service
        (default: "downloader tags").

  B) Hydrus-compatible sidecars (default when API is off):
        <file>.<ext>.txt       → tags (one per line)
        <file>.<ext>.urls.txt  → source URLs (one per line)

Secrets resolve from ``FURTAG_*`` environment variables and the operating
system keyring. Non-secret preferences live in platform-specific Settings.
Missing credentials disable only the affected source.
"""

import concurrent.futures as cf
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple

import requests
from PIL import Image, ImageFile, UnidentifiedImageError
import regex  # for emoji stripping

from furtag_settings import (
    DEFAULT_JSON_PATTERN,
    DEFAULT_PDF_ARCHIVAL_DPI,
    DEFAULT_PDF_DPI,
    DEFAULT_TAG_PATTERN,
    DEFAULT_URL_PATTERN,
    RunOptions,
    ScanSummary,
    Settings,
    SettingsStore,
    atomic_write_text,
    render_sidecar_name,
)
from furtag_events import NullObserver, RunEvent, RunObserver, TerminalObserver
from furtag_review import PendingReview, ReviewQueue
from furtag_credentials import CredentialStore
from furtag_urls import UrlWritePolicy
from furtag_hydrus import (
    HydrusMixin,
    HydrusResultPageState,
    HYDRUS_HASH_LOOKUP_BATCH,
    HYDRUS_PAGE_BATCH,
    HYDRUS_RELATIONSHIP_DUPLICATES,
)

ImageFile.LOAD_TRUNCATED_IMAGES = True  # don't crash on slightly-truncated files
# Pillow's own decompression-bomb guard fires inside Image.open() on a pixel
# count we have not vetted yet, which turned huge-but-legitimate comic scans
# into an endless retry loop. We enforce our own THUMB_SOURCE_MAX_PIXELS ceiling
# immediately after open() (which is lazy and decodes nothing), and hand
# anything above it to ImageMagick, so Pillow's heuristic only gets in the way.
Image.MAX_IMAGE_PIXELS = None

# ── Constants ────────────────────────────────────────────────────────────────

THUMB_MAX = 256
# Pillow must decode many formats into memory before it can resize them. Keep a
# single pathological image from consuming hundreds of megabytes (or more) in
# the perceptual worker. 64 MP still comfortably covers 8K images and long
# comic pages while bounding an RGBA decode to roughly 256 MiB.
THUMB_SOURCE_MAX_PIXELS = 64_000_000

# Seconds to pause the Fluffle lane after a 5xx, so a transient backend outage
# doesn't chew through the remaining queue at full rate.
FLUFFLE_SERVER_ERROR_BACKOFF = 15.0

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


def _prune_hidden_walk_dirs(dirs: List[str]) -> None:
    """Keep ``os.walk`` out of dot-directories and preserve natural ordering.

    Volume-root scans otherwise descend into macOS internals such as
    ``.DocumentRevisions-V100``, ``.Spotlight-V100``, and ``.Trashes``. Some are
    unreadable even to ordinary desktop apps; others contain metadata that is
    not user media.

    Shared by the main scanner and sidecar sync (BF-10).
    """
    dirs[:] = sorted(d for d in dirs if not d.startswith("."))


# Ledger statuses treated as "resolved" for skip / fingerprint sealing.
# pending_review is intentionally absent — those files stay eligible.
RESOLVED_LEDGER_STATUSES = frozenset({
    "matched", "nomatch", "duplicate", "unreadable",
    # Legacy only: older runs wrote top-level hydrus_deleted. New code writes
    # matched/nomatch + nested hydrus_output instead; still readable for skip.
    "hydrus_deleted",
})

# Exact-hash (MD5) sources, then every search source. One ordered definition so
# the toggle lookups and the per-tier service lists can't drift apart.
HASH_SOURCES = ("e621", "inkbunny", "danbooru", "gelbooru")
SEARCH_SOURCES = HASH_SOURCES + ("fluffle", "saucenao")

LEDGER_FILE      = ".furtag_ledger.json"
DUPLICATES_FILE  = "duplicates.log"
LEDGER_VERSION = 5
# Increment when a completed match needs source metadata backfilled. Version 2
# adds direct e621/Inkbunny notes, so old resolved records are retried once
# while retaining their cached MD5.
LEDGER_METADATA_VERSION = 2
# Bump when the canonical search-profile digest inputs change shape.
SEARCH_PROFILE_VERSION = 1
# Bump when unreadable-media preparation/decoder policy changes (BF-17).
DECODER_PROFILE_VERSION = 2  # v2: ImageMagick downscale for oversized sources

#: Resolved lazily by ``_magick_binary()``; "unset" means "not looked up yet",
#: None means "looked up, not installed".
_MAGICK_CMD: Any = "unset"
# Written beside rendered PDF page PNGs so comic:/creator: survive later runs.
PDF_META_FILE    = ".furtag_pdf.json"
PDF_COMPLETE_FILE = ".furtag_pdf_complete.json"

# "Artist unknown" placeholder tags that every booru emits in some form — useless
# noise in a Hydrus library, so they're dropped before writing. Compared against
# the tag lowercased with underscores already normalised to spaces (see
# _clean_tag_text / parsers). Bare general-tag forms plus any creator:<value>
# whose value is one of _JUNK_CREATOR_VALUES are removed.
#
# InkBunny also has a sitewide "keywording policy" keyword that many artists
# stamp on every submission — not content, just policy noise.
_JUNK_TAGS = {
    "unknown artist", "artist request", "anonymous artist",
    "unknown_artist", "artist_request", "anonymous_artist",
    "creator:unknown", "creator:anonymous",
    "keywording policy", "keyword policy", "inkbunny keywording policy",
}
_JUNK_CREATOR_VALUES = {
    "unknown", "unknown artist", "anonymous", "anonymous artist", "artist request",
}


def _is_junk_tag(tag: str) -> bool:
    """True for placeholder / policy noise tags that shouldn't be written."""
    low = tag.lower().strip().replace("_", " ")
    if low in _JUNK_TAGS:
        return True
    if low.startswith("creator:"):
        return low[len("creator:"):].strip() in _JUNK_CREATOR_VALUES
    return False


def _truthy(val: str, default: bool = False) -> bool:
    """Parse a serialized boolean (true/yes/1/on). Empty → default."""
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _bool_str(val: bool) -> str:
    """Render a bool in the spelling `_truthy` parses back."""
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
    _SYM  = {
        "pending": "·", "run": "…", "hit": "✓", "miss": "✗",
        "err": "⚠", "cancel": "⏹",
    }
    _LEGEND = ("legend:  … querying   ✓ found   ✗ not found   ⚠ error/blocked")
    _TRACK_ORDER = ("hash", "perceptual")
    _SEP = "  " + "─" * 60
    _MAX_ISSUES = 3

    def __init__(self) -> None:
        self.tracks: Dict[str, _Track] = {k: _Track() for k in self._TRACK_ORDER}
        self.source_hits: Dict[str, int] = {
            name: 0 for name in
            ("e621", "inkbunny", "danbooru", "gelbooru",
             "fluffle", "saucenao")
        }
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

    @classmethod
    def hash_line(cls, state: Dict[str, str]) -> str:
        """Render the per-site hash ticker (`hash ▸ e621 ✓  ib ·  dan …`).

        A classmethod so the engine can format the sub-status for the event
        stream without owning (or needing) a live panel."""
        return "hash ▸ " + "  ".join(
            f"{cls._ABBR.get(s, s)} {cls._SYM.get(st, '?')}" for s, st in state.items())

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
            if self._live():
                self._render()

    def freeze_total(self, track: str) -> None:
        """Stop treating this track's total as still-increasing, so its ETA
        becomes computable (called once the producer feeding it is done)."""
        with self._lock:
            self.tracks[track].growing = False
            if self._live():
                self._render()

    def start_file(self, track: str, idx: int, current: str,
                    nxt: Optional[str]) -> None:
        with self._lock:
            t = self.tracks[track]
            t.idx, t.current, t.nxt, t.sub = idx, current, nxt or "—", "…"
            if self._live():
                self._render()

    def status(self, track: str, sub: str) -> None:
        with self._lock:
            self.tracks[track].sub = sub
            # Status can arrive before the first phase (local hashing, Hydrus
            # pre-passes); don't paint a half-empty panel over the startup log.
            if self._live():
                self._render()

    def finish_file(
            self, track: str, result: str,
            source_hits: Optional[Dict[str, int]] = None) -> None:
        with self._lock:
            t = self.tracks[track]
            t.done = t.idx
            t.prev = (t.current, result)
            if source_hits:
                self.source_hits.update(source_hits)
            if self._live():
                self._render()
            else:
                print(f"[{track}] [{t.idx}/{t.total}] {self._trim(t.current)} → {result}")

    def log(self, msg: str) -> None:
        """Keep warnings/errors in a three-line rolling panel while live.

        Non-interactive output stays line-oriented so redirected logs retain
        every issue. In a terminal, old issues roll off instead of permanently
        accumulating above the progress display. Before the first phase starts
        there is no panel on screen yet, so those messages print inline rather
        than disappearing into a history nobody has rendered.
        """
        with self._lock:
            if not self._live():
                print(msg)
                return
            clean = " ".join(str(msg).split())
            self._issue_total += 1
            self._issues.append(self._trim(clean, 56))
            self._issues = self._issues[-self._MAX_ISSUES:]
            self._render()

    def _live(self) -> bool:
        """True once a phase has started, i.e. the panel owns the screen and a
        raw `print` would corrupt it. Caller must hold self._lock."""
        return self.tty and any(t.phase for t in self.tracks.values())

    def active(self) -> bool:
        """Locked `_live()`, for callers outside the display."""
        with self._lock:
            return self._live()

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
        lines.append(
            "  Tagged files · "
            + "  ".join(
                f"{self._ABBR.get(name, name)} {self.source_hits.get(name, 0)}"
                for name in (
                    "e621", "inkbunny", "danbooru", "gelbooru",
                    "fluffle", "saucenao"
                )
            )
        )
        lines.append(self._SEP)
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


# Active live panel, if any. Only LiveDisplay-specific fallbacks (the plain
# `hashed n/m` counter) consult this; all rendering goes through the observer.
_display: Optional["LiveDisplay"] = None

# The observer every module-level message is routed to. Defaults to a
# display-less TerminalObserver, which prints — the right behaviour for a CLI
# before the panel exists and for any headless caller. `_run_impl` swaps in the
# run's observer for its duration; a GUI installs a long-lived one at startup
# via set_active_observer() so credential/Hydrus warnings reach its issue pane.
_active_observer: RunObserver = TerminalObserver()


def set_active_observer(observer: Optional[RunObserver]) -> RunObserver:
    """Install the observer notify() routes to; returns the previous one."""
    global _active_observer
    prev = _active_observer
    _active_observer = observer if observer is not None else TerminalObserver()
    return prev


# Sentinel pushed onto the perceptual queue once the hash tier is done
# producing, so the perceptual worker thread knows to stop and exit.
_PERCEPTUAL_DONE = object()


def notify(msg: str, *, severity: str = "warning") -> None:
    """User-facing messages for the active observer (BF-12).

    *severity*:
      - ``warning`` / ``error`` → ``issue`` event (issue pane / panel log)
      - ``info`` → ``log`` event (audit/success; not treated as a problem)

    Never ``print`` from inside the processing loop — that corrupts the live
    panel.
    """
    kind = "log" if severity == "info" else "issue"
    _active_observer.emit(RunEvent(kind=kind, message=str(msg)))


def notify_info(msg: str) -> None:
    """Informational / success line — does not enter the issue stream."""
    notify(msg, severity="info")


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
    lookup_errors: Set[str] = field(default_factory=set)
    mtime_ns: Optional[int] = None
    # A transient source failure is not a clean miss. Keep this file's ledger
    # at ``hashed`` so a later run retries the network work without re-hashing.


@dataclass
class SourceMetadata:
    """Metadata collected for one source match.

    Notes are keyed by their final, stable Hydrus note name. Keeping them next
    to tags and URLs prevents descriptions from being lost between a source
    lookup and the eventual SHA-256 Hydrus write.
    """
    tags: Set[str] = field(default_factory=set)
    urls: Set[str] = field(default_factory=set)
    notes: Dict[str, str] = field(default_factory=dict)
    force_associate_urls: Set[str] = field(default_factory=set)

    def merge(self, other: "SourceMetadata") -> None:
        self.tags |= other.tags
        self.urls |= other.urls
        self.force_associate_urls |= other.force_associate_urls
        self.notes.update(other.notes)


class RetryableLookupError(RuntimeError):
    """A source did not produce a trustworthy hit-or-miss answer."""


class RetryableMediaError(RetryableLookupError):
    """Local media could not be read reliably, but may succeed next run."""


class UnusableMediaError(RuntimeError):
    """An unchanged local file cannot be prepared for perceptual search."""


@dataclass
class HashTierResult:
    """Typed hash-tier result with legacy four-value tuple unpacking."""
    metadata: SourceMetadata
    sources: List[str]

    def __iter__(self):
        yield self.metadata.tags
        yield self.metadata.urls
        yield self.sources
        yield self.metadata.force_associate_urls


@dataclass
class PerceptualTierResult:
    """Typed perceptual result with legacy four-value tuple unpacking."""
    metadata: SourceMetadata
    sources: List[str]
    review_raw: Optional[Dict]

    def __iter__(self):
        yield self.metadata.tags
        yield self.metadata.urls
        yield self.sources
        yield self.review_raw


@dataclass(frozen=True)
class WriteOutcome:
    """Result of writing every configured metadata sink for one file."""

    sha256: Optional[str]
    complete: bool
    # Search/ledger status for a completed write. None → "matched".
    # New code does not write top-level hydrus_deleted; use hydrus_output.
    ledger_status: Optional[str] = None
    # Nested scoped Hydrus checkpoint (import_state / metadata_state / …).
    hydrus_output: Optional[Dict] = None
    # Nested unmatched-import checkpoint (search may be nomatch independently).
    unmatched_import: Optional[Dict] = None
    # Per-sink detail lets the pipeline distinguish "Hydrus failed after the
    # sidecar was safely written" from a sidecar failure that still requires a
    # fresh lookup. ``None`` preserves the conservative behavior of legacy
    # callers and test doubles.
    hydrus_complete: Optional[bool] = None
    sidecar_complete: Optional[bool] = None


# ── Session ledger ───────────────────────────────────────────────────────────

class Ledger:
    """Per-directory JSON record of every file in that directory already
    processed, keyed by filename with a (size, mtime) fingerprint, plus a
    cached MD5 so an unchanged file is never re-hashed. Lives inside the
    directory it describes (not the scan root), so it travels with that
    folder and is picked up no matter which ancestor directory a later run
    scans from.

    Also carries a directory-level manifest fingerprint. Once every file in
    the directory has a resolved record, that
    fingerprint is "sealed" (`mark_dir_complete`) — a future run can then
    skip the entire folder on one count/size comparison, without touching
    any individual file, as long as the fingerprint still matches."""

    MTIME_EPS = 1e-6

    def __init__(self, dir_path: Path) -> None:
        self.dir = dir_path
        self.path = dir_path / LEDGER_FILE
        self.records: Dict[str, Dict] = {}
        self.dir_count: Optional[int] = None
        self.dir_size: Optional[int] = None
        self.dir_manifest: Optional[str] = None
        self.dir_metadata_version: Optional[int] = None
        self.dir_direct_notes_applied: Optional[bool] = None
        self.dir_search_profile_hash: Optional[str] = None
        self.dir_sidecars_required: Optional[bool] = None
        self.dir_sidecar_format: Optional[str] = None
        self.dir_output_policy_hash: Optional[str] = None
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
                    self.dir_manifest = fp.get("manifest")
                    self.dir_metadata_version = fp.get("metadata_version")
                    self.dir_direct_notes_applied = fp.get(
                        "direct_notes_applied")
                    self.dir_search_profile_hash = fp.get(
                        "search_profile_hash")
                    if "sidecars_required" in fp:
                        self.dir_sidecars_required = bool(
                            fp.get("sidecars_required"))
                    self.dir_sidecar_format = fp.get("sidecar_format")
                    self.dir_output_policy_hash = fp.get("output_policy_hash")
        except Exception as e:
            notify(f"⚠️  Couldn't read ledger {self.path} ({e}); starting fresh.")

    def _fresh_record(
            self, name: str, size: int, mtime: float,
            mtime_ns: Optional[int] = None) -> Optional[Dict]:
        """The stored record for `name` iff its fingerprint still matches disk.

        Prefers nanosecond mtime when both sides have it (BF-16); falls back to
        float ``mtime`` with ``MTIME_EPS`` for legacy rows.
        """
        rec = self.records.get(name)
        if not rec or rec.get("size") != size:
            return None
        stored_ns = rec.get("mtime_ns")
        if stored_ns is not None and mtime_ns is not None:
            try:
                if int(stored_ns) != int(mtime_ns):
                    return None
            except (TypeError, ValueError):
                return None
            return rec
        try:
            if abs(float(rec.get("mtime", -1)) - mtime) > self.MTIME_EPS:
                return None
        except (TypeError, ValueError):
            return None
        return rec

    def status_for(
            self, name: str, size: int, mtime: float,
            require_direct_notes: bool = True,
            search_profile_hash: Optional[str] = None,
            mtime_ns: Optional[int] = None,
            decoder_profile: Optional[str] = None) -> Optional[str]:
        """'matched' / 'nomatch' if this exact file was already processed, else None.

        When *search_profile_hash* is provided, matched/nomatch rows only reuse
        if they were sealed under that same profile (BF-03). Rows missing a
        profile hash are treated as stale once so they refresh once under the
        current profile.

        Metadata-version invalidation is limited to matched rows that still
        need direct-note backfill (BF-15) — not nomatch/unreadable/duplicate.
        """
        rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
        if not rec:
            return None
        status = rec.get("status")
        # BF-15: only matched+notes care about LEDGER_METADATA_VERSION.
        if (status == "matched" and require_direct_notes
                and self.needs_direct_notes(
                    name, size, mtime, mtime_ns=mtime_ns)):
            return None
        if (status in ("matched", "nomatch")
                and search_profile_hash is not None):
            rec_profile = rec.get("search_profile_hash")
            if not rec_profile or rec_profile != search_profile_hash:
                return None
        # BF-17: unreadable seals reopen when decoder/prep policy changes.
        if status == "unreadable" and decoder_profile is not None:
            rec_dec = rec.get("decoder_profile")
            if not rec_dec or rec_dec != decoder_profile:
                return None
        return status

    def needs_direct_notes(
            self, name: str, size: int, mtime: float,
            mtime_ns: Optional[int] = None) -> bool:
        """Whether this exact prior match still needs source-note backfill."""
        rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
        return bool(
            rec
            and rec.get("status") == "matched"
            and set(rec.get("sources") or ()) & {"e621", "inkbunny"}
            and (rec.get("metadata_version") != LEDGER_METADATA_VERSION
                 or rec.get("direct_notes_applied") is not True))

    def md5_for(
            self, name: str, size: int, mtime: float,
            mtime_ns: Optional[int] = None) -> Optional[str]:
        """Reuse a previously-computed MD5 for an unchanged file even if its
        status isn't matched/nomatch (e.g. a booru was briefly unreachable
        last time) — saves re-hashing on retry."""
        rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
        return rec.get("md5") if rec else None

    def cache_md5(
            self, name: str, size: int, mtime: float, md5: str,
            mtime_ns: Optional[int] = None) -> None:
        """Checkpoint a local MD5 before the network stages finish.

        ``status: hashed`` deliberately remains unresolved, so the next run
        retries its lookups while reusing this disk-expensive MD5.
        """
        if not md5:
            return
        with self._lock:
            rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
            if rec is not None:
                if rec.get("md5") != md5:
                    rec["md5"] = md5
                    self._dirty += 1
                if mtime_ns is not None and rec.get("mtime_ns") != mtime_ns:
                    rec["mtime_ns"] = int(mtime_ns)
                    self._dirty += 1
                return
            row = {
                "size": size,
                "mtime": mtime,
                "md5": md5,
                "status": "hashed",
                "sources": [],
            }
            if mtime_ns is not None:
                row["mtime_ns"] = int(mtime_ns)
            self.records[name] = row
            self._dirty += 1

    def sha256_for(
            self, name: str, size: int, mtime: float,
            mtime_ns: Optional[int] = None) -> Optional[str]:
        """Return a cached Hydrus/SHA-256 hash for this unchanged file."""
        rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
        return rec.get("sha256") if rec else None

    def cache_sha256(
            self, name: str, size: int, mtime: float, sha256: str,
            mtime_ns: Optional[int] = None) -> None:
        """Add SHA-256 to an existing unchanged record for future page loads."""
        with self._lock:
            rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
            if rec is not None and rec.get("sha256") != sha256:
                rec["sha256"] = sha256
                self._dirty += 1

    def sidecar_sync_matches(
            self, name: str, size: int, mtime: float,
            signature: str,
            *,
            scope_id: Optional[str] = None,
            tag_deleted_duplicates: Optional[bool] = None,
            mtime_ns: Optional[int] = None,
    ) -> bool:
        """Whether these exact media bytes and sidecar payload were synced.

        Terminal deleted outcomes (no live dups, etc.) are also checkpointed
        so they are not retried forever under the same Hydrus scope/policy
        (BF-07). Scope or deleted-dup policy changes invalidate the checkpoint.
        """
        rec = self._fresh_record(name, size, mtime, mtime_ns=mtime_ns)
        sync = rec.get("sidecar_sync") if rec else None
        if not (isinstance(sync, dict) and signature
                and sync.get("signature") == signature):
            return False
        # Incomplete / pending checkpoints never count as matched.
        if sync.get("complete") is False:
            return False
        # Legacy checkpoints only stored signature (+ optional sha256).
        if "scope_id" not in sync and "disposition" not in sync:
            return scope_id is None
        if (scope_id is not None
                and sync.get("scope_id") != scope_id):
            return False
        if (tag_deleted_duplicates is not None
                and "tag_deleted_duplicates" in sync
                and bool(sync.get("tag_deleted_duplicates"))
                != bool(tag_deleted_duplicates)):
            return False
        return True

    def record_sidecar_sync(
            self, name: str, size: int, mtime: float,
            signature: str, sha256: Optional[str] = None,
            *,
            mtime_ns: Optional[int] = None,
            scope_id: Optional[str] = None,
            disposition: Optional[str] = None,
            import_state: Optional[str] = None,
            complete: bool = True,
            tag_deleted_duplicates: Optional[bool] = None,
    ) -> None:
        """Checkpoint a sidecar→Hydrus reconciliation outcome.

        Independent of normal matched/nomatch scan status. Accepts terminal
        deleted dispositions as complete so they are not retried under the
        same scope/policy (BF-07).
        """
        if not signature:
            return
        with self._lock:
            rec = self._fresh_record(
                name, size, mtime, mtime_ns=mtime_ns)
            if rec is None:
                rec = {
                    "size": size,
                    "mtime": mtime,
                    "md5": None,
                    "status": "sidecar_only",
                    "sources": [],
                }
                if mtime_ns is not None:
                    rec["mtime_ns"] = int(mtime_ns)
                self.records[name] = rec
            elif mtime_ns is not None and rec.get("mtime_ns") != mtime_ns:
                rec["mtime_ns"] = int(mtime_ns)
                self._dirty += 1
            sync: Dict = {
                "signature": signature,
                "synced_at": time.time(),
                "complete": bool(complete),
            }
            if sha256:
                sync["sha256"] = sha256
                rec["sha256"] = sha256
            if scope_id is not None:
                sync["scope_id"] = scope_id
            if disposition:
                sync["disposition"] = disposition
            if import_state:
                sync["import_state"] = import_state
            if tag_deleted_duplicates is not None:
                sync["tag_deleted_duplicates"] = bool(tag_deleted_duplicates)
            if rec.get("sidecar_sync") != sync:
                rec["sidecar_sync"] = sync
                self._dirty += 1

    def record(self, name: str, size: int, mtime: float, md5: Optional[str],
               status: str, sources: List[str], duplicate_of: str = "",
               sha256: Optional[str] = None,
               direct_notes_applied: Optional[bool] = None,
               hydrus_output: Optional[Dict] = None,
               unmatched_import: Optional[Dict] = None,
               review: Optional[Dict] = None,
               search_profile_hash: Optional[str] = None,
               mtime_ns: Optional[int] = None,
               tagged_at: Optional[float] = None,
               stamp_tagged_at: Optional[bool] = None,
               decoder_profile: Optional[str] = None,
               unreadable_reason: Optional[str] = None,
               metadata_version: Optional[int] = None) -> None:
        # New writers must not introduce top-level hydrus_deleted; map to
        # matched + nested checkpoint if a caller still passes the legacy name.
        if status == "hydrus_deleted":
            status = "matched"
            if hydrus_output is None:
                hydrus_output = {
                    "import_state": "previously_deleted",
                    "metadata_state": "no_duplicate_targets",
                    "sha256": sha256,
                    "target_hashes": [],
                    "complete": True,
                    "updated_at": time.time(),
                    "legacy_status_rewrite": True,
                }
        with self._lock:
            previous = self._fresh_record(
                name, size, mtime, mtime_ns=mtime_ns) or {}
            record = {
                "size": size,
                "mtime": mtime,
                "md5": md5,
                "status": status,
                "sources": sources,
                "metadata_version": (
                    LEDGER_METADATA_VERSION if metadata_version is None
                    else int(metadata_version)),
            }
            if mtime_ns is not None:
                record["mtime_ns"] = int(mtime_ns)
            elif previous.get("mtime_ns") is not None:
                record["mtime_ns"] = previous["mtime_ns"]
            if status == "matched" and direct_notes_applied is None:
                # Compatibility for direct Ledger callers: a newly-written
                # current-version match historically meant all requested
                # metadata completed. The pipeline passes an explicit False
                # when Hydrus notes are unavailable.
                direct_notes_applied = True
            if direct_notes_applied is not None:
                record["direct_notes_applied"] = bool(direct_notes_applied)
            if not sha256:
                # No fresh hash from this write (sidecar-only mode, or unmatched
                # files with hydrus_import_unmatched off) — keep the one already
                # cached for these exact bytes rather than dropping it. Guarded on
                # the same (size, mtime) freshness `sha256_for` uses, so a changed
                # file never inherits a hash computed from different bytes.
                sha256 = (self._fresh_record(
                    name, size, mtime, mtime_ns=mtime_ns) or {}).get("sha256")
            if sha256:
                # Persist the SHA-256 Hydrus already handed us on import, so the
                # Already Tagged page never has to recompute it on a later run.
                record["sha256"] = sha256
            if status == "matched":
                # BF-11: idempotent rewrites keep the original tag time so
                # Already Tagged is not reshuffled; first match stamps now.
                if tagged_at is not None:
                    record["tagged_at"] = float(tagged_at)
                elif stamp_tagged_at is True:
                    record["tagged_at"] = time.time()
                elif stamp_tagged_at is False:
                    if previous.get("tagged_at") is not None:
                        record["tagged_at"] = previous["tagged_at"]
                elif (previous.get("status") == "matched"
                        and previous.get("tagged_at") is not None):
                    record["tagged_at"] = previous["tagged_at"]
                else:
                    record["tagged_at"] = time.time()
            if status == "unreadable":
                if decoder_profile:
                    record["decoder_profile"] = decoder_profile
                elif previous.get("decoder_profile"):
                    record["decoder_profile"] = previous["decoder_profile"]
                if unreadable_reason:
                    record["unreadable_reason"] = str(unreadable_reason)[:240]
                elif previous.get("unreadable_reason"):
                    record["unreadable_reason"] = previous["unreadable_reason"]
            if status in ("matched", "nomatch"):
                if search_profile_hash:
                    record["search_profile_hash"] = search_profile_hash
                elif previous.get("search_profile_hash"):
                    record["search_profile_hash"] = previous[
                        "search_profile_hash"]
            if duplicate_of:
                record["duplicate_of"] = duplicate_of
            if isinstance(hydrus_output, dict):
                record["hydrus_output"] = hydrus_output
            elif isinstance(previous.get("hydrus_output"), dict):
                record["hydrus_output"] = previous["hydrus_output"]
            if isinstance(unmatched_import, dict):
                record["unmatched_import"] = unmatched_import
            elif isinstance(previous.get("unmatched_import"), dict):
                record["unmatched_import"] = previous["unmatched_import"]
            if isinstance(review, dict):
                record["review"] = review
            elif isinstance(previous.get("review"), dict):
                record["review"] = previous["review"]
            # Sidecar reconciliation is orthogonal to online scan status. Keep
            # its checkpoint when this same file later gains a matched,
            # nomatch, duplicate, or pending-review record.
            if isinstance(previous.get("sidecar_sync"), dict):
                record["sidecar_sync"] = previous["sidecar_sync"]
            self.records[name] = record
            self._dirty += 1

    def fingerprint_matches(
            self, count: int, total_size: int,
            manifest: Optional[str] = None,
            require_direct_notes: bool = True,
            search_profile_hash: Optional[str] = None,
            sidecars_required: Optional[bool] = None,
            sidecar_format: Optional[str] = None,
            output_policy_hash: Optional[str] = None) -> bool:
        if self.dir_count is None:
            return False
        if require_direct_notes:
            if self.dir_metadata_version != LEDGER_METADATA_VERSION:
                return False
            if self.dir_direct_notes_applied is not True:
                return False
        if (self.dir_count, self.dir_size) != (count, total_size):
            return False
        if manifest is not None and self.dir_manifest != manifest:
            return False
        # BF-03: missing stored profile forces one re-check after upgrade.
        if search_profile_hash is not None:
            if not self.dir_search_profile_hash:
                return False
            if self.dir_search_profile_hash != search_profile_hash:
                return False
        # BF-04: seals made without sidecars cannot authorize sidecar-required runs.
        if sidecars_required is not None:
            if self.dir_sidecars_required is None:
                if sidecars_required:
                    return False
            elif bool(self.dir_sidecars_required) != bool(sidecars_required):
                return False
            if (sidecars_required and sidecar_format
                    and self.dir_sidecar_format
                    and self.dir_sidecar_format != sidecar_format):
                return False
        if (output_policy_hash is not None
                and self.dir_output_policy_hash != output_policy_hash):
            return False
        return True

    def mark_dir_complete(
            self, count: int, total_size: int,
            manifest: Optional[str] = None,
            direct_notes_applied: bool = True,
            search_profile_hash: Optional[str] = None,
            sidecars_required: bool = False,
            sidecar_format: Optional[str] = None,
            output_policy_hash: Optional[str] = None) -> None:
        """Seal the directory-level fingerprint. Only call once every current
        media file in the directory has a sidecar or a matched/nomatch record —
        otherwise an interrupted run could make a future scan wrongly skip
        files that were never actually processed."""
        with self._lock:
            state = (
                count, total_size, manifest, LEDGER_METADATA_VERSION,
                bool(direct_notes_applied), search_profile_hash,
                bool(sidecars_required), sidecar_format, output_policy_hash)
            current = (
                self.dir_count, self.dir_size, self.dir_manifest,
                self.dir_metadata_version, self.dir_direct_notes_applied,
                self.dir_search_profile_hash, self.dir_sidecars_required,
                self.dir_sidecar_format, self.dir_output_policy_hash)
            if current != state:
                (self.dir_count, self.dir_size, self.dir_manifest,
                 self.dir_metadata_version,
                 self.dir_direct_notes_applied,
                 self.dir_search_profile_hash,
                 self.dir_sidecars_required,
                 self.dir_sidecar_format,
                 self.dir_output_policy_hash) = state
                self._dirty += 1

    def save(self) -> None:
        with self._lock:
            if self._dirty == 0 and self.path.exists():
                return
            try:
                payload: Dict = {
                    "version": LEDGER_VERSION,
                    "records": self.records,
                }
                if self.dir_count is not None:
                    payload["dir_fingerprint"] = {
                        "count": self.dir_count,
                        "size": self.dir_size,
                        "manifest": self.dir_manifest,
                        "metadata_version": self.dir_metadata_version,
                        "direct_notes_applied":
                            self.dir_direct_notes_applied,
                        "search_profile_hash": self.dir_search_profile_hash,
                        "sidecars_required": self.dir_sidecars_required,
                        "sidecar_format": self.dir_sidecar_format,
                        "output_policy_hash": self.dir_output_policy_hash,
                    }
                atomic_write_text(
                    self.path, json.dumps(payload, ensure_ascii=False, indent=0))
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


def _normalize_pdf_meta(comic: Optional[str], creator: Optional[str],
                        default_comic: str) -> Dict[str, str]:
    """Return cleaned comic/creator strings for tagging + meta persistence."""
    comic_name = (comic or "").strip() or default_comic
    creator_name = (creator or "").strip()
    meta = {"comic": comic_name}
    if creator_name:
        meta["creator"] = creator_name
    return meta


def _write_pdf_meta(out_dir: Path, meta: Dict[str, str]) -> None:
    """Persist comic/creator next to rendered pages for later runs."""
    try:
        atomic_write_text(
            out_dir / PDF_META_FILE,
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    except OSError as e:
        notify(f"⚠️  Couldn't write {PDF_META_FILE} in {out_dir.name}: {e}")


def _read_pdf_meta(out_dir: Path) -> Dict[str, str]:
    """Load ``.furtag_pdf.json`` if present; empty dict on missing/invalid."""
    path = out_dir / PDF_META_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    comic = data.get("comic")
    if isinstance(comic, str) and comic.strip():
        out["comic"] = comic.strip()
    creator = data.get("creator")
    if isinstance(creator, str) and creator.strip():
        out["creator"] = creator.strip()
    return out


def _pdf_base_tags_from_meta(meta: Dict[str, str], page: Optional[int] = None,
                             fallback_comic: str = "") -> Set[str]:
    """comic:/creator:/page: set from stored or prompted meta."""
    tags: Set[str] = set()
    comic = (meta.get("comic") or fallback_comic or "").strip()
    if comic:
        tags.add(f"comic:{comic}")
    creator = (meta.get("creator") or "").strip()
    if creator:
        tags.add(f"creator:{creator}")
    if page is not None and page > 0:
        tags.add(f"page:{page}")
    return tags


def convert_pdf(pdf_path: Path, output_root: Path, dpi: int = PDF_DPI,
                write_sidecars: bool = True,
                sidecar_format: str = "txt",
                tag_pattern: str = "{name}{ext}.txt",
                json_pattern: str = "{name}{ext}.json",
                should_cancel: Optional[Callable[[], bool]] = None,
                comic: Optional[str] = None,
                creator: Optional[str] = None) -> List[Path]:
    """Render every page of ``pdf_path`` to a PNG under ``output_root/<stem>/``.

    Returns the list of PNG paths written. When ``write_sidecars`` is True each
    PNG also gets a ``comic:``/``page:`` (and optional ``creator:``) base-tag
    sidecar (txt or json per *sidecar_format*) so perceptual tags append to the
    same file later. Meta is also written to ``.furtag_pdf.json`` in the page
    folder so later runs keep the same comic/artist without re-prompting.

    *should_cancel* is polled between pages so a cancel doesn't have to wait out
    a whole multi-hundred-page render.
    """
    fitz = _import_fitz()
    stem = pdf_path.stem
    out_dir = output_root / stem
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = _normalize_pdf_meta(comic, creator, default_comic=stem)
    _write_pdf_meta(out_dir, meta)

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
                tags = _pdf_base_tags_from_meta(meta, page=i)
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
    label = meta["comic"]
    if meta.get("creator"):
        label = f"{label} · creator:{meta['creator']}"
    print(f"  {pdf_path.name}: {len(generated)} page(s) at {dpi} DPI "
          f"[{label}] -> {out_dir}")
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


def prompt_for_pdf_meta(pdfs: List[Path]) -> Dict[str, Dict[str, str]]:
    """Interactive comic/artist tags for each PDF about to be rendered.

    Returns a map of resolved PDF path → ``{"comic": …, "creator": …?}``.
    Empty Enter keeps the PDF stem as the comic name; blank artist is skipped.
    """
    result: Dict[str, Dict[str, str]] = {}
    if not pdfs:
        return result
    print(f"\n📚 Tag {len(pdfs)} PDF(s) before rendering "
          f"(Enter = default; artist optional).")
    for pdf in pdfs:
        key = str(pdf.resolve())
        print(f"\n  📄 {pdf.name}")
        try:
            comic_raw = input(f"     Comic name [{pdf.stem}]: ").strip()
            creator_raw = input("     Artist / creator (optional): ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n↩️  Using PDF filename as comic name for remaining PDFs.")
            for rest in pdfs[pdfs.index(pdf):]:
                result[str(rest.resolve())] = {"comic": rest.stem}
            return result
        result[key] = _normalize_pdf_meta(comic_raw, creator_raw, pdf.stem)
    return result


# ── TagIntegrator ────────────────────────────────────────────────────────────

class TagIntegrator(HydrusMixin):

    def __init__(self, settings: Optional[Settings] = None,
                 session: Optional[requests.Session] = None) -> None:
        self.settings = (settings or Settings()).clone()
        self.session = session if session is not None else requests.Session()
        self.cancel_event = threading.Event()
        self._observer: RunObserver = NullObserver()
        self._display_detached = True
        self._review_queue: Optional[ReviewQueue] = None
        self._run_lock = threading.Lock()  # one scan at a time
        self._fatal_network_lock = threading.Lock()
        self._fatal_network_error = False
        self._repeated_issue_lock = threading.Lock()
        self._repeated_issues: Dict[str, Tuple[int, str, str, str]] = {}

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
        self._gelbooru_tag_type_cache: Dict[str, object] = {}

        # SauceNAO
        self.saucenao_api_key = ""
        self.headers_saucenao: Dict[str, str] = {}
        self.has_saucenao = False
        self.enabled_saucenao = True
        self.saucenao_exhausted = False   # set True when the daily quota runs out
        self._saucenao_consecutive_429 = 0

        # Hash sources switched off mid-run by an HTTP 401/403. A rejected key
        # is recoverable across runs (the user fixes it), so every file seen
        # while a source sits in here must stay unresolved in the ledger.
        self.auth_rejected_sources: Set[str] = set()

        # Fluffle has no credentials — availability is always True when enabled
        self.has_fluffle = True
        self.enabled_fluffle = True

        # Hydrus Client API (optional output sink — skip sidecars when on)
        self.hydrus_api_url = ""
        self.hydrus_access_key = ""
        self.hydrus_tag_service_key = ""
        self.hydrus_can_edit_urls = False   # access key has "Import and Edit URLs"
        self.hydrus_can_edit_notes = False  # access key has "Edit File Notes"
        # Safe default for callers predating the settings toggle. The settings
        # layer may override this in apply_settings().
        self.hydrus_direct_notes_enabled = True
        self.hydrus_can_search_files = False  # MD5 → current SHA-256 lookup
        self.hydrus_can_manage_relationships = False
        self.hydrus_exact_url_enrichment = True
        self.hydrus_exact_url_enrichment_page_name = "FurTag Metadata"
        self.hydrus_result_pages: Dict[str, HydrusResultPageState] = {
            "new": HydrusResultPageState("new"),
            "updated": HydrusResultPageState("updated"),
            "duplicates": HydrusResultPageState("duplicates"),
        }
        self.hydrus_already_tagged_page_name = ""
        self.hydrus_already_tagged_page_enabled = False
        self.hydrus_already_tagged_page_limit = 0
        self.hydrus_live_page_update_interval = 10
        self.has_hydrus = False
        self.hydrus_can_manage_pages = False
        self._hydrus_lock = threading.Lock()  # serialise API writes (hash + perc)
        self._hydrus_page_api_lock = threading.Lock()
        self._hydrus_page_condition = threading.Condition()
        self._hydrus_page_publisher: Optional[threading.Thread] = None
        self._hydrus_page_stop = False
        self._hydrus_page_run_active = False
        self._hydrus_page_failures: Dict[str, str] = {}
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
        prev_service = getattr(self, "hydrus_tag_service_name", "")
        self.hydrus_tag_service_name = out.hydrus_tag_service
        if (self.hydrus_tag_service_name != prev_service
                and getattr(self, "hydrus_tag_service_key", "")):
            # The resolved service_key belongs to the OLD name; pushing tags with
            # it now would silently write to the wrong tag service. Re-resolve, and
            # if that fails drop to sidecars rather than tag the wrong service.
            self._reresolve_tag_service()
        self.hydrus_import_unmatched = out.hydrus_import_unmatched
        hy = self.settings.hydrus
        self.hydrus_direct_notes_enabled = bool(hy.direct_source_notes)
        self.hydrus_exact_url_enrichment = bool(hy.exact_url_enrichment)
        self.hydrus_exact_url_enrichment_page_name = (
            hy.exact_url_enrichment_page_name.strip() or "FurTag Metadata")
        for kind, prefix in (("new", "new_imports"),
                             ("updated", "newly_tagged"),
                             ("duplicates", "duplicate_tagged")):
            page = self.hydrus_result_pages[kind]
            page.name = getattr(hy, f"{prefix}_page_name")
            page.configured_enabled = bool(
                getattr(hy, f"{prefix}_page_enabled"))
            page.limit = getattr(hy, f"{prefix}_page_limit")
            page.mode = getattr(hy, f"{prefix}_page_mode")
        self.hydrus_already_tagged_page_name = hy.already_tagged_page_name
        self.hydrus_already_tagged_page_enabled = bool(
            hy.results_pages_enabled and hy.already_tagged_page_enabled
            and self.hydrus_can_manage_pages)
        self.hydrus_already_tagged_page_limit = hy.already_tagged_page_limit
        self.hydrus_live_page_update_interval = hy.live_page_update_interval
        self._apply_source_toggles()

    def _reresolve_tag_service(self) -> None:
        """Re-resolve `hydrus_tag_service_name` → service_key after a settings
        change. On any failure the stale key is cleared and the Hydrus sink is
        disabled (sidecars only) — never keep writing to the previous service."""
        try:
            svc_key = self._hydrus_resolve_tag_service(self.hydrus_tag_service_name)
        except Exception as e:                       # network / API / parse failure
            svc_key = ""
            notify(f"‼️  Couldn't re-resolve Hydrus tag service "
                   f"'{self.hydrus_tag_service_name}' ({e}) – sidecars only.")
        else:
            if not svc_key:
                notify(f"‼️  Hydrus tag service '{self.hydrus_tag_service_name}' "
                       f"not found – sidecars only.")
        self.hydrus_tag_service_key = svc_key
        if not svc_key:
            self.has_hydrus = False

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

    def _stop_for_broken_ca_bundle(self, error: Exception) -> bool:
        """Cancel safely when Requests can no longer access its trust store."""
        message = str(error).lower()
        broken = (
            ("tls ca certificate bundle" in message and "invalid path" in message)
            or ("ssl" in message and "certificate" in message
                and ("not found" in message or "no such file" in message))
        )
        if not broken:
            # Proactive: certifi path vanished under a renamed install.
            try:
                import certifi
                from pathlib import Path as _P
                if not _P(certifi.where()).is_file():
                    broken = True
            except Exception:
                pass
        if not broken:
            return False
        with self._fatal_network_lock:
            first = not self._fatal_network_error
            self._fatal_network_error = True
        self.request_cancel()
        if first:
            notify(
                "❌ HTTPS certificate bundle became unavailable; stopping "
                "safely. Restart FurTag from its current folder.")
        return True

    # ── Credential loading ───────────────────────────────────────────────────

    def load_credentials(self, cfg: Optional[Dict[str, str]] = None) -> None:
        """Load credentials from keyring/environment or an in-memory mapping.

        Any missing/incomplete set just marks that source unavailable. Secret
        material is never read from a project file.
        """
        if cfg is None:
            cfg = CredentialStore().load_all().as_cfg()
        print("🔑 Loading credentials from secure store / environment")

        # Credential editing is live in the GUI. Clear every capability derived
        # from the previous snapshot before probing the new one, or removing a
        # key could leave the old source/session/Hydrus permissions active.
        self.has_e621 = False
        self.has_inkbunny = False
        self.has_danbooru = False
        self.has_gelbooru = False
        self.has_saucenao = False
        self.ib_sid = ""
        self.danbooru_anon = False
        self.saucenao_exhausted = False
        self._saucenao_consecutive_429 = 0
        self.auth_rejected_sources = set()
        self.has_hydrus = False
        self.hydrus_tag_service_key = ""
        self.hydrus_can_edit_urls = False
        self.hydrus_can_edit_notes = False
        self.hydrus_can_search_files = False
        self.hydrus_can_manage_relationships = False
        self.hydrus_can_manage_pages = False
        for page in self.hydrus_result_pages.values():
            page.enabled = False
        self.hydrus_already_tagged_page_enabled = False

        # Non-secret Hydrus preferences always come from Settings.
        out = self.settings.output
        hy = self.settings.hydrus
        defaults = {
            "hydrus_import": _bool_str(out.hydrus_import),
            "hydrus_also_sidecars": _bool_str(out.sidecars_enabled),
            "hydrus_tag_deleted_duplicates": _bool_str(out.hydrus_tag_deleted_duplicates),
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

    def load_credentials_from_store(
            self, store: Optional[CredentialStore] = None) -> None:
        """Load only from environment variables and the OS-backed keyring."""
        store = store or CredentialStore()
        self.load_credentials(cfg=store.load_all().as_cfg())

    def _init_e621(self, cfg: Dict[str, str]) -> None:
        self.e621_username = cfg.get("e621_username", "")
        self.e621_api_key  = cfg.get("e621_api_key", "")
        if not (self.e621_username and self.e621_api_key):
            notify("‼️  e621 credentials incomplete – e621 disabled.")
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
            notify("‼️  InkBunny credentials incomplete – InkBunny disabled.")
            return
        try:
            if self.inkbunny_login():
                self.has_inkbunny = True
        except RetryableLookupError as e:
            # Credential loading runs during GUI startup. A temporary InkBunny
            # outage should disable only this source, not prevent FurTag from
            # opening or using every other source.
            notify(f"‼️  InkBunny temporarily unavailable – disabled for this "
                   f"credential load ({e}).")

    def _init_danbooru(self, cfg: Dict[str, str]) -> None:
        self.danbooru_username = cfg.get("danbooru_username", "")
        self.danbooru_api_key  = cfg.get("danbooru_api_key", "")
        if not (self.danbooru_username and self.danbooru_api_key):
            notify("‼️  Danbooru credentials incomplete – Danbooru disabled.")
            return
        print(f"✅ Danbooru credentials loaded for {self.danbooru_username}")
        self.has_danbooru = True

    def _init_gelbooru(self, cfg: Dict[str, str]) -> None:
        self.gelbooru_user_id = cfg.get("gelbooru_user_id", "")
        self.gelbooru_api_key = cfg.get("gelbooru_api_key", "")
        if not (self.gelbooru_user_id and self.gelbooru_api_key):
            notify("‼️  Gelbooru credentials incomplete – Gelbooru disabled.")
            return
        print("✅ Gelbooru credentials loaded")
        self.has_gelbooru = True

    def _init_saucenao(self, cfg: Dict[str, str]) -> None:
        self.saucenao_api_key = cfg.get("sauce_nao_api_key", "")
        if not self.saucenao_api_key:
            notify("‼️  No sauce_nao_api_key found – SauceNAO fallback disabled.")
            return
        self.headers_saucenao = {"User-Agent": "HydrusIntegrator/5.0 (SauceNAO)"}
        print("✅ SauceNAO API key loaded")
        self.has_saucenao = True

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

    def direct_notes_effective(self) -> bool:
        """Whether this run can actually persist source descriptions."""
        return bool(
            self.hydrus_direct_notes_enabled
            and self.has_hydrus
            and self.hydrus_can_edit_notes)

    def search_profile_hash(self) -> str:
        """Canonical digest of effective sources + matching policy (BF-03).

        Availability belongs in the profile: a clean miss made without an
        enabled source's credentials must reopen once that source can actually
        participate. Mid-run auth rejection is handled separately as a
        retryable lookup failure and never writes a resolved row.
        """
        matching = self.settings.matching
        payload = {
            "v": SEARCH_PROFILE_VERSION,
            "sources": {
                name: {
                    "enabled": bool(self.source_enabled(name)),
                    "available": bool(self.source_available(name)),
                    "mode": (
                        "anonymous" if name == "danbooru" and self.danbooru_anon
                        else "authenticated" if self.source_available(name)
                        else "unavailable"),
                }
                for name in SEARCH_SOURCES
            },
            "matching": {
                "saucenao_min_similarity": float(
                    matching.saucenao_min_similarity),
                "saucenao_auth_similarity": float(
                    matching.saucenao_auth_similarity),
                "fluffle_accepted_matches": sorted(
                    matching.fluffle_accepted_matches or []),
                "fluffle_tossup_e621_only": bool(
                    matching.fluffle_tossup_e621_only),
                "fluffle_review_mode": str(matching.fluffle_review_mode or "off"),
            },
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:32]

    @staticmethod
    def _digest_policy(payload: Dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:32]

    def hydrus_output_policy_hash(self) -> str:
        """Identity of policy affecting terminal Hydrus dispositions."""
        return self._digest_policy({
            "v": 1,
            "tag_deleted_duplicates": bool(
                self.hydrus_tag_deleted_duplicates),
        })

    def output_policy_hash(self) -> str:
        """Non-secret identity of every output decision used by dir seals."""
        scope_id = None
        if self.has_hydrus:
            try:
                scope_id = self._hydrus_scope_id()
            except Exception:
                scope_id = None
        return self._digest_policy({
            "v": 1,
            "hydrus_active": bool(self.has_hydrus),
            "hydrus_scope_id": scope_id,
            "hydrus_import": bool(self.hydrus_import),
            "hydrus_import_unmatched": bool(self.hydrus_import_unmatched),
            "hydrus_policy": self.hydrus_output_policy_hash(),
            "sidecars_required": bool(self.write_sidecars),
            "sidecar_format": self.sidecar_format_key(),
            "sidecar_patterns": {
                "tag": self.settings.output.sidecar_tag_filename,
                "url": self.settings.output.sidecar_url_filename,
                "json": self.settings.output.sidecar_json_filename,
            },
        })

    def sidecar_format_key(self) -> str:
        """Output sidecar format identity for directory seals (BF-04)."""
        out = self.settings.output
        return str(out.sidecar_format or "txt").lower()

    def ledger_record(
            self, ledger: "Ledger", name: str, size: int, mtime: float,
            md5: Optional[str], status: str, sources: List[str],
            **kwargs: Any) -> None:
        """Ledger.record with automatic search_profile_hash for search results."""
        if status in ("matched", "nomatch"):
            kwargs.setdefault("search_profile_hash", self.search_profile_hash())
        if "mtime_ns" not in kwargs:
            try:
                st = (ledger.dir / name).stat()
                if (st.st_size == size
                        and abs(st.st_mtime - mtime) <= Ledger.MTIME_EPS):
                    kwargs["mtime_ns"] = st.st_mtime_ns
            except OSError:
                pass
        ledger.record(name, size, mtime, md5, status, sources, **kwargs)

    def decoder_profile(self) -> str:
        """Behavior-relevant decoder identity for unreadable seals (BF-17)."""
        pillow_ver = "unknown"
        try:
            from PIL import Image as _PilImage
            pillow_ver = getattr(_PilImage, "__version__", None) or getattr(
                __import__("PIL", fromlist=["__version__"]), "__version__",
                "unknown")
        except Exception:
            try:
                import PIL
                pillow_ver = getattr(PIL, "__version__", "unknown")
            except Exception:
                pass
        magick = "yes" if self._magick_binary() else "no"
        return f"v{DECODER_PROFILE_VERSION};pillow={pillow_ver};magick={magick}"

    def local_path_complete(
            self, path: Path, ledger: "Ledger", st: os.stat_result, *,
            is_pdf_page: bool = False,
            root: Optional[Path] = None,
            search_profile_hash: Optional[str] = None,
            require_output_complete: bool = True,
    ) -> bool:
        """Shared completeness predicate for index and finalize (BF-05).

        True when this path needs no further work under the current policy.
        Missing required sidecars, profile mismatch, pending review, incomplete
        unmatched import (when *require_output_complete*), and broken duplicate
        links all return False.

        Index uses ``require_output_complete=False`` so a profile-compatible
        ``nomatch`` still skips re-search while a pending unmatched Hydrus
        import is reconciled by ``_hydrus_import_prior_nomatches``. Finalize
        uses the default True so directories cannot seal with pending sinks.
        """
        if search_profile_hash is None:
            search_profile_hash = self.search_profile_hash()
        fn = path.name
        mtime_ns = getattr(st, "st_mtime_ns", None)
        note_backfill = (
            self.direct_notes_effective()
            and ledger.needs_direct_notes(
                fn, st.st_size, st.st_mtime, mtime_ns=mtime_ns))
        has_sidecar = self.has_sidecar(path)
        rec = ledger._fresh_record(
            fn, st.st_size, st.st_mtime, mtime_ns=mtime_ns)
        # A pre-existing/manual sidecar with no ledger row remains a supported
        # import boundary. Once FurTag has a row, however, the sidecar satisfies
        # only the sidecar sink; it cannot hide a stale search profile, a
        # pending hash/search, or an incomplete Hydrus write.
        if (has_sidecar and not is_pdf_page and not note_backfill
                and (rec is None or rec.get("status") == "sidecar_only")):
            return True

        status = ledger.status_for(
            fn, st.st_size, st.st_mtime,
            self.direct_notes_effective(),
            search_profile_hash=search_profile_hash,
            mtime_ns=mtime_ns,
            decoder_profile=self.decoder_profile())
        if status is None or status not in RESOLVED_LEDGER_STATUSES:
            return False

        # Sidecar-required reopen (index previously cleared status; finalize
        # must agree so a cancelled recovery cannot reseal).
        if (status in ("matched", "hydrus_deleted")
                and self.write_sidecars
                and not has_sidecar
                and not is_pdf_page):
            return False

        if status == "duplicate":
            rec = ledger.records.get(fn) or {}
            canonical = rec.get("duplicate_of") or ""
            if root is not None and canonical and not (root / canonical).is_file():
                return False

        # Index may defer only the optional unmatched-import sink; matched
        # Hydrus checkpoints must be checked even during search-only reuse so
        # a database or deleted-file policy change actually queues the file.
        check_output = require_output_complete or (
            self.has_hydrus and status in ("matched", "hydrus_deleted"))
        if check_output and not self.path_is_output_complete(rec):
            return False
        return True

    def _reject_source_auth(self, name: str, message: str) -> "RetryableLookupError":
        """Disable *name* for the rest of the run after an HTTP 401/403.

        Two things have to happen at once. The source is switched off so the
        run does not hammer a rejecting API once per file (``has_*`` = False,
        the long-standing behaviour). But a rejected key is *not* a clean
        "this post does not exist" answer — it is recoverable as soon as the
        user fixes their credentials, and the ledger keys on (size, mtime),
        which never change. So the source is also remembered here; `hash_tier`
        turns that into a per-file lookup error for every remaining file, which
        keeps them out of ``RESOLVED_LEDGER_STATUSES`` and eligible for a
        later run.

        Returns the error to raise, so callers read as ``raise self._reject…``.
        """
        setattr(self, f"has_{name}", False)
        if name not in self.auth_rejected_sources:
            self.auth_rejected_sources.add(name)
            notify(message)
        return RetryableLookupError(
            f"{name} authentication rejected (HTTP 401/403); "
            "retry once credentials are fixed")

    def source_available(self, name: str) -> bool:
        """Credentials present. Called per file per service — no dict building."""
        if name == "saucenao":
            # Quota exhaustion is a retryable/deferred state, not missing
            # credentials. Keep the source active so remaining files are not
            # incorrectly sealed as clean misses.
            return self.has_saucenao
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

    @staticmethod
    def prune_walk_dirs(dirs: List[str]) -> None:
        """Host hook for HydrusMixin walks — same rules as the main scanner."""
        _prune_hidden_walk_dirs(dirs)

    def enabled_pipeline_description(self) -> str:
        """Human-readable pipeline containing only user-enabled sources."""
        labels = {
            "e621": "e621",
            "inkbunny": "InkBunny",
            "danbooru": "Danbooru",
            "gelbooru": "Gelbooru",
            "fluffle": "Fluffle",
            "saucenao": "SauceNAO",
        }
        hash_sources = [
            labels[name] for name in HASH_SOURCES if self.source_enabled(name)]
        perceptual_sources = [
            labels[name] for name in ("fluffle", "saucenao")
            if self.source_enabled(name)]
        tiers: List[str] = []
        if hash_sources:
            hash_desc = " + ".join(hash_sources) + " MD5"
            if len(hash_sources) > 1:
                hash_desc += " (concurrent)"
            tiers.append(hash_desc)
        if perceptual_sources:
            tiers.append(" → ".join(perceptual_sources))
        return " → ".join(tiers) or "No enabled search sources"

    # ── Thumbnail / MD5 helpers ──────────────────────────────────────────────

    @staticmethod
    def _magick_binary() -> Optional[List[str]]:
        """Resolve an ImageMagick invocation once per process (None if absent)."""
        global _MAGICK_CMD
        if _MAGICK_CMD != "unset":
            return _MAGICK_CMD
        cmd: Optional[List[str]] = None
        magick = shutil.which("magick")
        if magick:
            cmd = [magick]
        else:
            convert = shutil.which("convert")
            if convert:
                cmd = [convert]
        _MAGICK_CMD = cmd
        if cmd is None:
            notify("⚠️  ImageMagick not found; very large images cannot be "
                   "thumbnailed for perceptual search. Install it with "
                   "`brew install imagemagick`.")
        return cmd

    def _prepare_thumb_external(self, img: Path) -> Optional[BytesIO]:
        """Downscale an oversized source with ImageMagick, out of process.

        Returns a PNG buffer, or None when ImageMagick is unavailable or fails.
        Memory limits keep the child from trading a Pillow blow-up for its own;
        past them ImageMagick pages through its on-disk pixel cache instead.
        """
        binary = self._magick_binary()
        if not binary:
            return None
        cmd = binary + [
            "-limit", "memory", "512MiB",
            "-limit", "map", "2GiB",
            "-limit", "thread", "2",
            # [0] takes the first frame/page so animations and multi-page
            # sources produce a single thumbnail.
            f"{img}[0]",
            "-strip",
            # -thumbnail samples down before the final resize. On a 360 MP page
            # that measured ~19s against ~140s for a plain -resize, at a
            # quality difference invisible in a 256 px perceptual thumbnail.
            "-thumbnail", f"{THUMB_MAX}x{THUMB_MAX}>",
            "PNG:-",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=180, check=False)
        except subprocess.TimeoutExpired:
            notify(f"⚠️  ImageMagick timed out downscaling {img.name}.")
            return None
        except OSError as e:
            notify(f"⚠️  ImageMagick could not run for {img.name}: {e}")
            return None
        if proc.returncode != 0 or not proc.stdout:
            detail = (proc.stderr or b"").decode(
                "utf-8", "replace").strip().splitlines()
            notify(f"⚠️  ImageMagick failed on {img.name}"
                   + (f": {detail[-1][:200]}" if detail else ""))
            return None
        return BytesIO(proc.stdout)

    def _prepare_thumb(self, img: Path) -> Optional[BytesIO]:
        try:
            with Image.open(img) as im:
                width, height = im.size
                if width <= 0 or height <= 0:
                    raise ValueError(f"invalid image dimensions {width}×{height}")
                if width * height > THUMB_SOURCE_MAX_PIXELS:
                    # Too big for an in-process decode. ImageMagick streams the
                    # source and spills to its disk cache, so it can shrink a
                    # 350 MP page without a multi-gigabyte RGBA buffer.
                    oversized = (
                        f"{width}×{height} exceeds the "
                        f"{THUMB_SOURCE_MAX_PIXELS:,}-pixel in-process limit")
                    external = self._prepare_thumb_external(img)
                    if external is not None:
                        return external
                    raise ValueError(
                        f"image is too large to thumbnail safely ({oversized}) "
                        f"and ImageMagick could not downscale it")

                # JPEG can ask its decoder for a reduced-resolution source.
                # Other formats still benefit from shrinking before a mode
                # conversion, which avoids a second full-resolution buffer.
                im.draft("RGB", (THUMB_MAX, THUMB_MAX))
                # Pillow < 9.1 lacks Image.Resampling; requirements floor is
                # 9.1+, but keep a fallback for partially upgraded envs (BF-14).
                lanczos = getattr(
                    getattr(Image, "Resampling", Image), "LANCZOS",
                    getattr(Image, "LANCZOS", 1))
                im.thumbnail(
                    (THUMB_MAX, THUMB_MAX),
                    resample=lanczos,
                    reducing_gap=2.0,
                )
                converted = None
                if im.mode not in ("RGB", "RGBA", "L"):
                    converted = im.convert("RGB")

                buf = BytesIO()
                try:
                    prepared = converted if converted is not None else im
                    prepared.save(buf, "PNG")
                finally:
                    if converted is not None:
                        converted.close()
            buf.seek(0)
            return buf
        except (UnidentifiedImageError, ValueError, SyntaxError,
                Image.DecompressionBombError) as e:
            notify(f"❌ Pillow failed on {img.name}: {e}")
            return None
        except OSError as e:
            raise RetryableMediaError(
                f"temporary image read failure for {img.name}: {e}") from e
        except Exception as e:
            raise RetryableMediaError(
                f"thumbnail preparation failed for {img.name}: {e}") from e

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
        """Extract an e621 post ID, never a numeric prefix from another site.

        Bluesky record keys commonly begin with ``3`` (``/post/3m…``); the old
        domain-agnostic regex mistook that prefix for e621 post 3.
        """
        m = re.match(
            r"https?://(?:www\.)?e621\.net/"
            r"(?:posts/|post/show/)([1-9]\d*)(?:[/?#]|$)",
            url or "", flags=re.IGNORECASE)
        return m.group(1) if m else ""

    # ── e621 API ─────────────────────────────────────────────────────────────

    def _e621_get(self, url: str) -> Optional[Dict]:
        """GET from e621 with auth; failures remain retryable, never misses."""
        self.pace["e621"].wait()
        if self.cancelled():
            raise RetryableLookupError("e621 lookup cancelled")
        try:
            r = self.session.get(
                url, headers=self.headers_e6,
                auth=(self.e621_username, self.e621_api_key), timeout=15,
            )
            if r.status_code == 429:
                notify("⚠️  e621 rate limit (429) – backing off 10s")
                self.pace["e621"].backoff(10)
                raise RetryableLookupError("e621 rate limited (HTTP 429)")
            if r.status_code in (401, 403):
                raise self._reject_source_auth(
                    "e621",
                    "‼️  e621 authentication rejected – disabled until "
                    "credentials are reloaded.")
            if r.status_code == 404:
                return {}
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"e621 returned HTTP {r.status_code}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(f"e621 request failed: {e}") from e

    def e621_lookup_by_md5(self, md5: str) -> Tuple[Set[str], Set[str]]:
        metadata = self._e621_metadata_by_md5(md5)
        return metadata.tags, metadata.urls

    def _e621_metadata_by_md5(self, md5: str) -> SourceMetadata:
        if not md5 or not self.has_e621:
            return SourceMetadata()
        data = self._e621_get(f"https://e621.net/posts.json?tags=md5:{md5}")
        posts = data.get("posts", []) if data else []
        return self._parse_e6_metadata(posts[0]) if posts else SourceMetadata()

    def e621_lookup_by_id(self, pid: str) -> Tuple[Set[str], Set[str]]:
        metadata = self._e621_metadata_by_id(pid)
        return metadata.tags, metadata.urls

    def _e621_metadata_by_id(self, pid: str) -> SourceMetadata:
        if not pid or not self.has_e621:
            return SourceMetadata()
        data = self._e621_get(f"https://e621.net/posts/{pid}.json")
        post = data.get("post", {}) if data else {}
        return self._parse_e6_metadata(post) if post else SourceMetadata()

    def _parse_e6_post(self, post: Dict) -> Tuple[Set[str], Set[str]]:
        """Convert an e621 post into (tags, urls). Includes pool/comic tags."""
        metadata = self._parse_e6_metadata(post)
        return metadata.tags, metadata.urls

    def _parse_e6_metadata(self, post: Dict) -> SourceMetadata:
        """Convert an e621 post into tags, URLs, and its source description."""
        tags: Set[str] = {"site:e621"}
        urls: Set[str] = set()
        notes: Dict[str, str] = {}

        post_id = post.get("id")
        if post_id:
            urls.add(f"https://e621.net/posts/{post_id}")
            description = post.get("description")
            if (self.direct_notes_effective()
                    and isinstance(description, str)
                    and description.strip()):
                notes[f"e621 description — post {post_id}"] = description.strip()

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

        return SourceMetadata(tags=tags, urls=urls, notes=notes)

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
        if self.cancelled():
            raise RetryableLookupError("InkBunny lookup cancelled")
        try:
            r = self.session.get(
                "https://inkbunny.net/api_login.php",
                params={"username": self.ib_username, "password": self.ib_password},
                timeout=15,
            )
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"InkBunny search returned HTTP {r.status_code}")
            data = r.json()
            sid = data.get("sid", "")
            if sid:
                self.ib_sid = sid
                print(f"✅ InkBunny logged in as {self.ib_username}")
                return True
            self.has_inkbunny = False
            notify(f"‼️  InkBunny login failed: {data.get('error_message', data)}")
            return False
        except (requests.RequestException, ValueError) as e:
            notify(f"❌ InkBunny login request failed: {e}")
            return False

    def inkbunny_lookup_by_md5(
            self, md5: str,
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        """Search InkBunny by file MD5, then pull keywords from matching submissions.

        Returns ``(tags, urls, force_associate_urls)``. Multi-file submissions
        still contribute their submission URL for association/sidecars, but that
        URL is also listed in *force_associate_urls* so Hydrus enrichment will
        not queue the whole multi-page post through the downloader.
        """
        if not md5 or not self.has_inkbunny:
            return set(), set(), set()
        sub_ids = self._inkbunny_search_md5(md5)
        if not sub_ids:
            return set(), set(), set()
        metadata = self._inkbunny_submission_metadata(sub_ids)
        return metadata.tags, metadata.urls, metadata.force_associate_urls

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
            if r.status_code == 429:
                self.pace["inkbunny"].backoff(10)
                raise RetryableLookupError(
                    "InkBunny rate limited (HTTP 429)")
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"InkBunny search returned HTTP {r.status_code}")
            data = r.json()
            # Expired/invalid session → re-login once and retry.
            if data.get("error_code") in ("2", 2) and _retry:
                if self.inkbunny_login():
                    return self._inkbunny_search_md5(md5, _retry=False)
                self.has_inkbunny = False
                return []
            if data.get("error_code"):
                raise RetryableLookupError(
                    "InkBunny search error: "
                    f"{data.get('error_message') or data.get('error_code')}")
            return [str(s.get("submission_id")) for s in data.get("submissions", [])
                    if s.get("submission_id")]
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"InkBunny search failed: {e}") from e

    @staticmethod
    def _inkbunny_file_count(sub: Dict) -> int:
        """How many files/pages a submission carries (1 = safe to enrich)."""
        files = sub.get("files")
        if isinstance(files, list) and files:
            return len(files)
        for key in ("pagecount", "page_count"):
            raw = sub.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            try:
                return max(1, int(raw))
            except (TypeError, ValueError):
                continue
        return 1

    def _inkbunny_submission_tags(
            self, sub_ids: List[str],
    ) -> Tuple[Set[str], Set[str], Set[str]]:
        metadata = self._inkbunny_submission_metadata(sub_ids)
        return metadata.tags, metadata.urls, metadata.force_associate_urls

    def _inkbunny_submission_metadata(
            self, sub_ids: List[str],
    ) -> SourceMetadata:
        tags: Set[str] = set()
        urls: Set[str] = set()
        notes: Dict[str, str] = {}
        force_associate: Set[str] = set()
        collect_notes = self.direct_notes_effective()
        self.pace["inkbunny"].wait()
        if self.cancelled():
            raise RetryableLookupError("InkBunny submission fetch cancelled")
        try:
            r = self.session.get(
                "https://inkbunny.net/api_submissions.php",
                params={"sid": self.ib_sid,
                        "submission_ids": ",".join(sub_ids),
                        "show_description": "yes" if collect_notes else "no"},
                timeout=20,
            )
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"InkBunny submissions returned HTTP {r.status_code}")
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"InkBunny submissions fetch failed: {e}") from e
        if data.get("error_code"):
            raise RetryableLookupError(
                "InkBunny submissions error: "
                f"{data.get('error_message') or data.get('error_code')}")

        for sub in data.get("submissions", []):
            tags.add("site:inkbunny")
            sub_id = sub.get("submission_id")
            if sub_id:
                url = f"https://inkbunny.net/s/{sub_id}"
                urls.add(url)
                if collect_notes:
                    title = sub.get("title")
                    if isinstance(title, str) and title.strip():
                        notes[f"Inkbunny title — submission {sub_id}"] = (
                            title.strip())
                    description = sub.get("description")
                    if isinstance(description, str) and description.strip():
                        notes[f"Inkbunny description — submission {sub_id}"] = (
                            description.strip())
                # Multi-file IB posts share one /s/{id} page. Queuing that
                # URL through Hydrus's downloader imports every page, not
                # just the MD5-matched file — associate only instead.
                if self._inkbunny_file_count(sub) > 1:
                    force_associate.add(url)

            username = (sub.get("username") or "").strip()
            if username:
                tags.add(f"creator:{username}")

            for kw in sub.get("keywords", []):
                name = (kw.get("keyword_name") or "").replace("_", " ").strip()
                name = EMOJI_PATTERN.sub("", name).strip()
                if name:
                    tags.add(name)   # InkBunny keywords are freeform/un-namespaced

        return SourceMetadata(tags, urls, notes, force_associate)

    # ── Danbooru API ─────────────────────────────────────────────────────────

    def _danbooru_get(self, url: str, params: Dict) -> Optional[object]:
        auth = {} if self.danbooru_anon else {"login": self.danbooru_username,
                                              "api_key": self.danbooru_api_key}
        self.pace["danbooru"].wait()
        if self.cancelled():
            raise RetryableLookupError("Danbooru lookup cancelled")
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
            if r.status_code in (401, 403):
                raise self._reject_source_auth(
                    "danbooru",
                    "‼️  Danbooru anonymous access rejected – disabled "
                    "until credentials are reloaded.")
            if r.status_code == 404:
                return {}
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"Danbooru returned HTTP {r.status_code}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"Danbooru request failed: {e}") from e

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
        if self.cancelled():
            raise RetryableLookupError("Gelbooru lookup cancelled")
        try:
            r = self.session.get(
                "https://gelbooru.com/index.php",
                params={**params, "api_key": self.gelbooru_api_key,
                        "user_id": self.gelbooru_user_id},
                headers={"User-Agent": "HydrusIntegrator/5.0"},
                timeout=15,
            )
            if r.status_code in (401, 403):
                raise self._reject_source_auth(
                    "gelbooru",
                    "‼️  Gelbooru authentication rejected – disabled until "
                    "credentials are reloaded.")
            if r.status_code == 404:
                return []
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"Gelbooru returned HTTP {r.status_code}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"Gelbooru request failed: {e}") from e

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
        """Resolve tag types in one batched call, caching across file hits."""
        if not names:
            return {}
        result = {
            name: self._gelbooru_tag_type_cache[name]
            for name in names if name in self._gelbooru_tag_type_cache
        }
        missing = [
            name for name in names
            if name not in self._gelbooru_tag_type_cache
        ]
        if not missing:
            return result
        data = self._gelbooru_get(
            {"page": "dapi", "s": "tag", "q": "index", "json": "1",
             "names": " ".join(missing)})
        tags = data.get("tag", []) if isinstance(data, dict) else data
        if isinstance(tags, dict):
            tags = [tags]
        if not isinstance(tags, list):
            return result
        fetched = {
            t.get("name"): t.get("type")
            for t in tags
            if isinstance(t, dict) and t.get("name") is not None
        }
        self._gelbooru_tag_type_cache.update(fetched)
        result.update(fetched)
        return result

    # ── Fluffle API ──────────────────────────────────────────────────────────

    def fluffle_search(self, img: Path) -> Optional[Dict]:
        thumb = self._prepare_thumb(img)
        if not thumb:
            raise UnusableMediaError(
                f"Fluffle could not prepare thumbnail for {img.name}")
        self.pace["fluffle"].wait()
        if self.cancelled():
            raise RetryableLookupError("Fluffle lookup cancelled")
        try:
            r = self.session.post(
                self.fluffle_api, headers=self.headers_fluf,
                files={"file": ("image.png", thumb, "image/png")},
                data={"includeNsfw": True, "limit": 32}, timeout=30,
            )
            if r.status_code == 429:
                notify("⚠️  Fluffle rate limit (429) – backing off 30s")
                self.pace["fluffle"].backoff(30)
                raise RetryableLookupError("Fluffle rate limited (HTTP 429)")
            if r.status_code >= 500:
                # Fluffle's backend is briefly unwell (or choked on this
                # thumbnail). Ease off so a wobble doesn't burn through the
                # rest of the queue at full rate; the file retries next run.
                self.pace["fluffle"].backoff(FLUFFLE_SERVER_ERROR_BACKOFF)
                raise RetryableLookupError(
                    f"Fluffle server error (HTTP {r.status_code}); "
                    f"backing off {FLUFFLE_SERVER_ERROR_BACKOFF:g}s")
            if r.status_code != 200:
                raise RetryableLookupError(
                    f"Fluffle returned HTTP {r.status_code}")
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"Fluffle request failed: {e}") from e

    #: Fluffle match slots, strictly highest confidence first. Each row is a
    #: ``(match class, is-e621)`` pair; ``_fluffle_slot_verdict()`` turns a row
    #: into accept / review / reject using the three user settings. A real
    #: ``exact`` therefore always outranks ``tossUp``, which outranks
    #: ``alternative``, which outranks ``unlikely`` — no matter what order
    #: Fluffle returned them in.
    _FLUFFLE_SLOTS: Tuple[Tuple[str, bool], ...] = (
        ("exact", True),
        ("exact", False),
        ("tossUp", True),
        ("tossUp", False),
        ("alternative", True),
        ("alternative", False),
        ("unlikely", True),
        ("unlikely", False),
    )

    def _fluffle_slot_verdict(self, match: str, e621: bool,
                              accepted: Set[str], review_mode: str) -> str:
        """``"accept"`` / ``"review"`` / ``"reject"`` for one Fluffle slot.

        The whole accept/reject matrix lives here:

        ==============  ================================================
        exact           accept when the user accepts ``exact``
        tossUp (e621)   accept when the user accepts ``tossUp`` **or**
                        ``fluffle_tossup_e621`` (the documented low-risk
                        gate: we re-query e621 by post ID afterwards)
        tossUp (other)  accept only when the user accepts ``tossUp`` and
                        the e621-only gate is off
        alternative     accept only when the user accepts ``alternative``
        unlikely        accept only when the user accepts ``unlikely``
        ==============  ================================================

        Anything not accepted falls to ``review`` when *review_mode* covers
        its class (``tossups`` → tossUps, ``tossups_alternatives`` → tossUps
        and alternatives), else ``reject``. ``exact`` and ``unlikely`` are
        never review candidates.
        """
        if match == "exact":
            return "accept" if "exact" in accepted else "reject"
        if match == "tossUp":
            if "tossUp" in accepted:
                accept = e621 or not self.fluffle_tossup_e621
            else:
                accept = e621 and self.fluffle_tossup_e621
            if accept:
                return "accept"
            return ("review"
                    if review_mode in ("tossups", "tossups_alternatives")
                    else "reject")
        if match == "alternative":
            if "alternative" in accepted:
                return "accept"
            return "review" if review_mode == "tossups_alternatives" else "reject"
        if match == "unlikely":
            return "accept" if "unlikely" in accepted else "reject"
        return "reject"

    def find_best_exact_match(
            self, j: Dict
            ) -> Tuple[Set[str], Set[str], str, str, Optional[Dict]]:
        """
        Parse Fluffle results. Priority among auto-accepted classes:
        exact-e621 > exact-other > tossUp-e621 (when fluffle_tossup_e621),
        then the opt-in classes in the order of ``_FLUFFLE_SLOTS``.

        Returns (tags, urls, md5_from_url, post_id, review_candidate).
        *review_candidate* is a raw Fluffle result dict when nothing was
        auto-accepted but the best hit falls in the human-review band (and
        review mode is on); otherwise None.
        """
        results = j.get("results") if j else None
        if not results or not isinstance(results, list):
            return set(), set(), "", "", None

        def is_e621(r: Dict) -> bool:
            return ("e621" in (r.get("platform") or "").lower()
                    or "e621.net" in (r.get("location") or ""))

        accepted = set(self.fluffle_accepted_matches or ["exact"])
        # Always allow exact if the accepted list is empty/broken
        if not accepted:
            accepted = {"exact"}
        review_mode = self.fluffle_review_mode or "off"

        # Best (= first-returned) result per slot; Fluffle orders by confidence
        # within a class, so first wins.
        best: Dict[Tuple[str, bool], Dict] = {}
        for result in results:
            if not isinstance(result, dict):
                continue
            slot = (result.get("match"), is_e621(result))
            if slot in self._FLUFFLE_SLOTS and slot not in best:
                best[slot] = result

        chosen: Optional[Dict] = None
        review_candidate: Optional[Dict] = None
        for match, e621 in self._FLUFFLE_SLOTS:
            result = best.get((match, e621))
            if result is None:
                continue
            verdict = self._fluffle_slot_verdict(match, e621, accepted, review_mode)
            if verdict == "accept":
                chosen = result
                break                       # an auto-accept beats any review
            if verdict == "review" and review_candidate is None:
                review_candidate = result

        if not chosen:
            return set(), set(), "", "", review_candidate

        loc = chosen.get("location", "") or ""
        tags, urls = self._fluffle_result_payload(chosen)
        return tags, urls, self._md5_from_url(loc), self._post_id_from_url(loc), None

    @staticmethod
    def _fluffle_result_payload(result: Dict) -> Tuple[Set[str], Set[str]]:
        """`creator:`/`site:` tags and the source URL from one Fluffle result.

        Shared by auto-accept and review queueing so an approved review yields
        exactly the tags an auto-accepted hit would have.
        """
        tags: Set[str] = set()
        urls: Set[str] = set()
        for c in result.get("credits") or []:
            name = EMOJI_PATTERN.sub("", (c or {}).get("name", "")).strip()
            if name:
                tags.add(f"creator:{name}")
        platform_clean = EMOJI_PATTERN.sub("", result.get("platform", "") or "").strip()
        if platform_clean:
            tags.add(f"site:{platform_clean}")
        loc = result.get("location", "") or ""
        if loc:
            urls.add(loc)
        return tags, urls

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
        if not self.source_active("saucenao"):
            return None, None, set(), set()
        if self.saucenao_exhausted:
            raise RetryableLookupError(
                "SauceNAO quota exhausted; retry on a later run")
        if similarity_threshold is None:
            similarity_threshold = self.saucenao_min_similarity
        thumb = self._prepare_thumb(img)
        if not thumb:
            raise UnusableMediaError(
                f"SauceNAO could not prepare thumbnail for {img.name}")

        self.pace["saucenao"].wait()
        if self.cancelled():
            raise RetryableLookupError("SauceNAO lookup cancelled")
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
                self._saucenao_consecutive_429 += 1
                if self._saucenao_consecutive_429 >= 2:
                    self._disable_saucenao(
                        "repeatedly returned HTTP 429")
                    raise RetryableLookupError(
                        "SauceNAO repeatedly rate limited")
                try:
                    retry_after = max(
                        30.0, float(r.headers.get("Retry-After", 30)))
                except (TypeError, ValueError):
                    retry_after = 30.0
                notify(
                    f"⚠️  SauceNAO rate limit (429) – pausing "
                    f"{int(retry_after)}s; another 429 will disable it.")
                self.pace["saucenao"].backoff(retry_after)
                raise RetryableLookupError(
                    "SauceNAO rate limited; retry later")
            self._saucenao_consecutive_429 = 0
            if r.status_code != 200:
                # SauceNAO normally puts quota state in a successful JSON
                # response, but some deployments reply with a plain daily-limit
                # error instead. Do not keep spending time retrying it.
                if "daily" in r.text.lower() and "limit" in r.text.lower():
                    self._disable_saucenao("daily search limit reached")
                raise RetryableLookupError(
                    f"SauceNAO returned HTTP {r.status_code}")
            j = r.json()
            self._saucenao_check_quota(j.get("header", {}))
            if j.get("header", {}).get("status", 0) != 0:
                raise RetryableLookupError(
                    "SauceNAO returned a non-success API status")
            service, post_id = self._saucenao_best_authoritative(
                j, self.saucenao_auth_similarity)
            tags, urls = self._extract_saucenao_tags(j, similarity_threshold)
            return service, post_id, tags, urls
        except (requests.RequestException, ValueError) as e:
            raise RetryableLookupError(
                f"SauceNAO request failed: {e}") from e

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

    def _authoritative_metadata(
            self, service: str, post_id: str) -> SourceMetadata:
        """Authoritative lookup retaining richer source metadata when known."""
        if service == "e621":
            return self._e621_metadata_by_id(post_id)
        tags, urls = self._authoritative_lookup(service, post_id)
        return SourceMetadata(tags=tags, urls=urls)

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

    def has_sidecar(self, media: Path) -> bool:
        """True if any recognized sidecar exists (configured + legacy .txt).

        Legacy ``.txt`` sidecars are always recognized even when the format
        setting is JSON, so switching formats never re-scans a library.

        A ``<media>.<ext>.json`` neighbour must additionally *look* like FurTag's
        own payload: that name is also gallery-dl's default metadata filename, and
        counting a foreign file here would silently exclude the media from every
        future scan (same root cause as the Reset-deletes-metadata bug).
        """
        for p in self._tag_sidecar_candidates(media) + self._url_sidecar_candidates(media):
            if p.exists():
                return True
        return any(p.is_file() and _looks_like_furtag_json_sidecar(p)
                   for p in self._json_sidecar_candidates(media))

    def read_sidecar_payload(self, media: Path) -> Tuple[Set[str], Set[str]]:
        """Read tags and URLs from any supported sidecar shape beside *media*."""
        tags: Set[str] = set()
        urls: Set[str] = set()
        # JSON first (single file with both). Same guard as has_sidecar: a
        # gallery-dl `<media>.<ext>.json` is not ours, and ingesting its "tags"
        # would push a foreign tool's vocabulary into Hydrus.
        for jp in self._json_sidecar_candidates(media):
            if jp.is_file() and _looks_like_furtag_json_sidecar(jp):
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
    def _append_lines(path: Path, lines: Set[str]) -> Optional[int]:
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
            return None

    def _write_sidecar_results(self, media: Path, tags: Set[str],
                               urls: Set[str]) -> bool:
        """Write an already-cleaned result payload without touching Hydrus."""
        if not tags and not urls:
            # Nothing to record. Creating an empty ``{"tags": [], "urls": []}``
            # would make has_sidecar() true forever, so index() would count the
            # file as already tagged and never retry it. Leave any existing
            # sidecar (and its content) exactly as it is.
            return True
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
                atomic_write_text(
                    path,
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            except Exception as e:
                notify(f"❌ Write failed for {path.name}: {e}")
                return False
            return True
        complete = True
        if tags:
            complete = (
                self._append_lines(self.tag_sidecar_path(media), tags)
                is not None) and complete
        if urls:
            complete = (
                self._append_lines(self.url_sidecar_path(media), urls)
                is not None) and complete
        return complete

    @staticmethod
    def _pdf_page_base_tags(media: Path) -> Set[str]:
        """comic:/page:/creator: for a PDF-rendered page named ``STEM PAGEN.PNG``.

        Prefers ``.furtag_pdf.json`` written at render time (user-chosen comic
        and artist). Falls back to the page folder name (= PDF stem) for comic.
        """
        meta = _read_pdf_meta(media.parent)
        page_n: Optional[int] = None
        m = re.search(r"PAGE(\d+)", media.name, re.I)
        if m:
            page_n = int(m.group(1))
        return _pdf_base_tags_from_meta(
            meta, page=page_n, fallback_comic=media.parent.name)

    def write_results(
            self, media: Path, tags: Set[str], urls: Set[str],
            known_sha256: Optional[str] = None,
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Push to Hydrus and/or write sidecars. Returns the file's SHA-256 when
        it was pushed to Hydrus (so the caller can cache it), else None.

        Prefer ``url_policy``. ``exact_match=True`` is legacy shorthand for
        :attr:`UrlWritePolicy.ENRICH_HASH_POSTS` (byte-exact MD5 hash-tier
        hits). Perceptual matches stay :attr:`UrlWritePolicy.ASSOCIATE_ONLY`.

        *force_associate_urls* are never queued through Hydrus's downloader
        (see multi-file InkBunny submissions).
        """
        return self.write_results_detailed(
            media, tags, urls, known_sha256=known_sha256,
            exact_match=exact_match, url_policy=url_policy,
            force_associate_urls=force_associate_urls,
            notes=notes).sha256

    def write_results_detailed(
            self, media: Path, tags: Set[str], urls: Set[str],
            known_sha256: Optional[str] = None,
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
    ) -> WriteOutcome:
        """Write configured sinks and retain whether every write succeeded."""
        # Drop "artist unknown / anonymous" placeholder tags from every source
        # before writing — they're noise in a Hydrus library.
        tags = {t for t in tags if not _is_junk_tag(t)}
        urls = {u for u in urls if u}
        force_associate = {u for u in (force_associate_urls or set()) if u}
        notes = {str(name): str(text) for name, text in (notes or {}).items()
                 if str(name).strip() and str(text).strip()}

        if url_policy is None:
            url_policy = (
                UrlWritePolicy.ENRICH_HASH_POSTS if exact_match
                else UrlWritePolicy.ASSOCIATE_ONLY)

        if self.has_hydrus and (tags or urls or notes):
            push = self._hydrus_push_detailed(
                media, tags, urls, known_sha256, url_policy=url_policy,
                force_associate_urls=force_associate, notes=notes)
            sidecar_complete = True
            if self.write_sidecars:
                sidecar_complete = self._write_sidecar_results(
                    media, tags, urls)
            complete = push.complete and sidecar_complete
            checkpoint = push.to_ledger_checkpoint()
            if not push.complete and sidecar_complete:
                # Tags/URLs already have a durable copy in the sidecars. Keep
                # only the small pieces sidecars cannot express so the next
                # launch can retry this one Hydrus sink without querying the
                # source sites again. A successful retry replaces this whole
                # checkpoint, automatically dropping the transient context.
                checkpoint["resume_from_sidecars"] = {
                    "requires_sidecar": bool(tags or urls),
                    "url_policy": url_policy.value,
                    "force_associate_urls": sorted(force_associate),
                    "notes": notes,
                }
            # Search resolution stays "matched"; Hydrus details nest under
            # hydrus_output (never a new top-level hydrus_deleted status).
            return WriteOutcome(
                push.sha256, complete, ledger_status="matched",
                hydrus_output=checkpoint,
                hydrus_complete=push.complete,
                sidecar_complete=sidecar_complete)

        if self.write_sidecars:
            sidecar_complete = self._write_sidecar_results(media, tags, urls)
            return WriteOutcome(
                None, sidecar_complete,
                hydrus_complete=True, sidecar_complete=sidecar_complete)
        return WriteOutcome(
            None, not (tags or urls),
            hydrus_complete=True, sidecar_complete=True)

    def _propagate_duplicate_results(
            self, root: Path, canonical: FileItem, duplicates: List[FileItem],
            tags: Set[str], urls: Set[str], sources: List[str],
            canonical_sha256: Optional[str],
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
            ledger_status: str = "matched",
            hydrus_output: Optional[Dict] = None,
    ) -> int:
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
        force_associate = {u for u in (force_associate_urls or set()) if u}
        try:
            canonical_rel = str(canonical.path.relative_to(root))
        except ValueError:
            canonical_rel = str(canonical.path)
        canonical_rec = (
            canonical.ledger._fresh_record(
                canonical.path.name, canonical.size, canonical.mtime,
                mtime_ns=canonical.mtime_ns)
            if canonical.ledger is not None else None) or {}

        # Previously-deleted content: copies share nested hydrus_output and
        # must not re-trigger import + relationship lookups.
        from furtag_hydrus import HydrusImportState, HydrusMetadataState
        out = hydrus_output if isinstance(hydrus_output, dict) else {}
        deleted_import = (
            out.get("import_state") == HydrusImportState.PREVIOUSLY_DELETED.value
            or ledger_status == "hydrus_deleted")
        terminal_no_targets = out.get("metadata_state") in (
            HydrusMetadataState.NO_DUPLICATE_TARGETS.value,
            HydrusMetadataState.POLICY_SKIPPED.value,
            HydrusMetadataState.APPLIED_DUPLICATES.value,
        ) or ledger_status == "hydrus_deleted"

        completed = 0
        for duplicate in duplicates:
            copy_tags = tags
            if canonical.perceptual_only:
                # Each rendered PDF page already owns its own comic:/page:
                # sidecar. Do not append the canonical page number to a copy.
                copy_tags = tags - self._pdf_page_base_tags(canonical.path)
            complete = True
            if self.write_sidecars:
                complete = self._write_sidecar_results(
                    duplicate.path, copy_tags, urls)
            # Normally this is the canonical's Hydrus SHA-256, which proves
            # the tags already belong to this same byte-identical record. If
            # its earlier push failed, let this copy have one recovery attempt.
            sha256 = canonical_sha256 if (deleted_import or canonical_sha256) else None
            copy_hydrus = out if out else None
            if (not terminal_no_targets and self.has_hydrus and not sha256
                    and (copy_tags or urls or notes)):
                push = self._hydrus_push_detailed(
                    duplicate.path, copy_tags, urls,
                    force_associate_urls=force_associate, notes=notes)
                sha256 = push.sha256
                complete = complete and push.complete
                copy_hydrus = push.to_ledger_checkpoint()
            if not complete:
                continue
            # Always search status matched for successful fan-out of a hit.
            self.ledger_record(
                duplicate.ledger,
                duplicate.path.name, duplicate.size, duplicate.mtime,
                duplicate.md5, "matched", sources,
                duplicate_of=canonical_rel, sha256=sha256,
                direct_notes_applied=canonical_rec.get(
                    "direct_notes_applied", self.direct_notes_effective()),
                hydrus_output=copy_hydrus,
                tagged_at=canonical_rec.get("tagged_at"),
                stamp_tagged_at=False,
                metadata_version=canonical_rec.get(
                    "metadata_version", 0))
            completed += 1
        return completed

    @staticmethod
    def _resolve_duplicate_nomatches(
            root: Path, canonical: FileItem,
            duplicates: List[FileItem]) -> None:
        """Promote pending copies once their canonical is a clean no-match."""
        try:
            canonical_rel = str(canonical.path.relative_to(root))
        except ValueError:
            canonical_rel = str(canonical.path)
        for duplicate in duplicates:
            duplicate.ledger.record(
                duplicate.path.name, duplicate.size, duplicate.mtime,
                duplicate.md5, "duplicate", [],
                duplicate_of=canonical_rel,
                mtime_ns=duplicate.mtime_ns)

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
        """Resolve new copies whose canonical was resolved on an earlier run.

        Includes prior ``matched`` (with nested hydrus_output when present) and
        legacy top-level ``hydrus_deleted`` seals so byte-identical copies do
        not re-search or re-import (BF-08).
        """
        profile = self.search_profile_hash()
        copied_total = 0
        for canonical_path, copies in list(duplicate_groups.items()):
            try:
                st = canonical_path.stat()
            except OSError:
                continue
            ledger = ledger_mgr.get(canonical_path.parent)
            rec = ledger.records.get(canonical_path.name) or {}
            raw_status = rec.get("status")
            status = ledger.status_for(
                canonical_path.name, st.st_size, st.st_mtime,
                self.direct_notes_effective(),
                search_profile_hash=(
                    None if raw_status == "hydrus_deleted" else profile),
                mtime_ns=st.st_mtime_ns)
            if status not in ("matched", "nomatch", "hydrus_deleted"):
                continue
            canonical = FileItem(
                canonical_path, str(canonical_path.relative_to(root)),
                st.st_size, st.st_mtime,
                self._media_kind(canonical_path.name) or "image",
                ledger=ledger, md5=rec.get("md5"),
                sha256=ledger.sha256_for(
                    canonical_path.name, st.st_size, st.st_mtime,
                    mtime_ns=st.st_mtime_ns),
                mtime_ns=st.st_mtime_ns)
            if status == "nomatch":
                self._resolve_duplicate_nomatches(root, canonical, copies)
                duplicate_groups.pop(canonical_path, None)
                continue
            hydrus_output = rec.get("hydrus_output") if isinstance(
                rec.get("hydrus_output"), dict) else None
            # A canonical whose Hydrus sink is still pending must be recovered
            # first. Trying each byte-identical copy as an import fallback would
            # turn one outage into repeated 120-second requests and defeat the
            # selective sidecar resume path.
            if (status == "matched" and hydrus_output is not None
                    and not hydrus_output.get("complete")):
                continue
            if status == "hydrus_deleted" and not hydrus_output:
                hydrus_output = {
                    "import_state": "previously_deleted",
                    "metadata_state": "no_duplicate_targets",
                    "sha256": rec.get("sha256"),
                    "target_hashes": [],
                    "complete": True,
                    "legacy_status_rewrite": True,
                }
            c_tags, c_urls = self.read_sidecar_payload(canonical_path)
            copied_total += self._propagate_duplicate_results(
                root, canonical, copies, c_tags, c_urls,
                list(rec.get("sources") or []), canonical.sha256,
                hydrus_output=hydrus_output,
                ledger_status=(
                    "hydrus_deleted" if status == "hydrus_deleted"
                    else "matched"))
            duplicate_groups.pop(canonical_path, None)
        return copied_total

    def write_unmatched(self, media: Path,
                        known_sha256: Optional[str] = None) -> Optional[str]:
        """Optionally import a no-match file. Returns SHA-256 if known."""
        return self.write_unmatched_detailed(media, known_sha256).sha256

    def write_unmatched_detailed(
            self, media: Path,
            known_sha256: Optional[str] = None,
    ) -> WriteOutcome:
        """Import a no-match file and return a typed sink outcome (BF-02).

        Search resolution stays ``nomatch``; Hydrus import completion lives in
        the nested ``unmatched_import`` checkpoint and drives ``complete``.
        """
        from furtag_hydrus import HydrusImportState, HydrusMetadataState

        requested = bool(
            self.has_hydrus and self.hydrus_import
            and self.hydrus_import_unmatched)
        if not requested:
            return WriteOutcome(
                known_sha256, True, ledger_status="nomatch",
                unmatched_import={
                    "requested": False,
                    "complete": True,
                    "import_state": HydrusImportState.NOT_REQUESTED.value,
                    "metadata_state": HydrusMetadataState.NOT_REQUESTED.value,
                    "sha256": known_sha256,
                    "target_hashes": [],
                    "scope_id": None,
                    "updated_at": time.time(),
                })
        if known_sha256:
            # Already known local/current; nothing to re-import.
            scope = None
            if hasattr(self, "_hydrus_scope_id"):
                try:
                    scope = self._hydrus_scope_id()
                except Exception:
                    scope = None
            return WriteOutcome(
                known_sha256, True, ledger_status="nomatch",
                unmatched_import={
                    "requested": True,
                    "complete": True,
                    "import_state": HydrusImportState.LIVE.value,
                    "metadata_state": HydrusMetadataState.NOT_REQUESTED.value,
                    "sha256": known_sha256,
                    "target_hashes": [],
                    "scope_id": scope,
                    "policy_hash": self.hydrus_output_policy_hash(),
                    "updated_at": time.time(),
                })
        push = self._hydrus_push_detailed(media, set(), set())
        checkpoint = push.to_ledger_checkpoint()
        checkpoint["requested"] = True
        return WriteOutcome(
            push.sha256, push.complete, ledger_status="nomatch",
            unmatched_import=checkpoint)

    @staticmethod
    def unmatched_import_is_complete(
            rec: Optional[Dict], *, required: bool,
            scope_id: Optional[str] = None) -> bool:
        """Whether a nomatch row's optional Hydrus import sink is done."""
        if not required:
            return True
        if not isinstance(rec, dict):
            return False
        ui = rec.get("unmatched_import")
        if not isinstance(ui, dict):
            # Legacy nomatch without a checkpoint: incomplete when import is on.
            return False
        if ui.get("requested") is not True:
            return False
        if scope_id is not None and ui.get("scope_id") != scope_id:
            return False
        return bool(ui.get("complete"))

    def path_is_output_complete(
            self, rec: Optional[Dict], *,
            require_unmatched_import: Optional[bool] = None) -> bool:
        """Path-local completeness for directory seals (not search skip)."""
        if not isinstance(rec, dict):
            return False
        status = rec.get("status")
        if status not in RESOLVED_LEDGER_STATUSES:
            return False
        if require_unmatched_import is None:
            require_unmatched_import = bool(
                self.has_hydrus and self.hydrus_import
                and self.hydrus_import_unmatched)
        scope_id = None
        if self.has_hydrus:
            try:
                scope_id = self._hydrus_scope_id()
            except Exception:
                return False
        if status == "nomatch":
            if not self.unmatched_import_is_complete(
                    rec, required=require_unmatched_import,
                    scope_id=scope_id if require_unmatched_import else None):
                return False
            return True
        if self.has_hydrus and status in ("matched", "hydrus_deleted"):
            checkpoint = rec.get("hydrus_output")
            if not isinstance(checkpoint, dict):
                return False
            if not checkpoint.get("complete"):
                return False
            if checkpoint.get("scope_id") != scope_id:
                return False
            if (checkpoint.get("metadata_state") == "policy_skipped"
                    and checkpoint.get("policy_hash")
                    != self.hydrus_output_policy_hash()):
                return False
        return True

    # ── PDF pre-pass ───────────────────────────────────────────────────────────

    @staticmethod
    def _find_pdfs(root: Path) -> List[Path]:
        """Every non-dotfile PDF under *root*, in natural walk order.

        One definition, so the render planner and the "PDF rendering disabled"
        path agree on which PDFs exist (and both skip macOS `._` shadow files).
        """
        pdfs: List[Path] = []
        for dp, dirs, files in os.walk(root):
            _prune_hidden_walk_dirs(dirs)
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
            if self._valid_pdf_render(pdf, out_dir):
                continue                        # rendered on a previous run
            jobs.append(pdf)
        return page_dirs, jobs

    @staticmethod
    def _valid_pdf_render(pdf: Path, out_dir: Path) -> bool:
        """True only for an atomically completed render of these PDF bytes."""
        manifest_path = out_dir / PDF_COMPLETE_FILE
        try:
            data = json.loads(manifest_path.read_text("utf-8"))
            source = data.get("source") if isinstance(data, dict) else None
            pages = data.get("pages") if isinstance(data, dict) else None
            st = pdf.stat()
            if not isinstance(source, dict) or not isinstance(pages, list):
                return False
            if (source.get("size") != st.st_size
                    or source.get("mtime_ns") != st.st_mtime_ns
                    or not pages):
                return False
            return all(
                isinstance(name, str)
                and Path(name).name == name
                and (out_dir / name).is_file()
                for name in pages)
        except (OSError, ValueError, TypeError):
            return False

    def _pdf_sidecar_patterns(self) -> List[str]:
        """Sidecar name patterns a PDF page render could have written.

        The configured ones (whichever format is active) plus the shipped
        defaults, so a purge still cleans pages rendered before a format or
        pattern change.
        """
        out = self.settings.output
        patterns: List[str] = []
        for pattern in (out.sidecar_json_filename, out.sidecar_tag_filename,
                        out.sidecar_url_filename, DEFAULT_JSON_PATTERN,
                        DEFAULT_TAG_PATTERN, DEFAULT_URL_PATTERN):
            pattern = str(pattern or "").strip()
            if pattern and pattern not in patterns:
                patterns.append(pattern)
        return patterns

    def _clear_partial_pdf_render(self, pdf: Path) -> None:
        """Remove only this PDF's precisely named partial page outputs."""
        out_dir = pdf.parent / pdf.stem
        if not out_dir.is_dir():
            return
        pattern = _pdf_page_pattern(pdf, self._pdf_sidecar_patterns())
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
        try:
            (out_dir / PDF_COMPLETE_FILE).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    def render_pdf_jobs(
            self, pdfs: List[Path], dpi: int,
            completed: Optional["queue.Queue"] = None,
            pdf_meta: Optional[Dict[str, Dict[str, str]]] = None,
    ) -> List[Path]:
        """Render planned PDFs serially with adaptive oversized-page fallback.

        *pdf_meta* maps resolved PDF path → ``{"comic": …, "creator": …?}``
        from the pre-render prompt. Missing entries default to the PDF stem.
        """
        generated: List[Path] = []
        meta_map = pdf_meta or {}
        for pdf in pdfs:
            if self.cancelled():
                break
            # Jobs are scheduled only when no valid completion manifest exists.
            # Clear stale/partial page files before rendering from page 1.
            self._clear_partial_pdf_render(pdf)
            attempt_dpi = dpi
            pdf_generated: List[Path] = []
            entry = meta_map.get(str(pdf.resolve())) or meta_map.get(str(pdf)) or {}
            comic = entry.get("comic") if isinstance(entry, dict) else None
            creator = entry.get("creator") if isinstance(entry, dict) else None
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
                        should_cancel=self.cancelled,
                        comic=comic,
                        creator=creator)
                    if self.cancelled() or not pdf_generated:
                        self._clear_partial_pdf_render(pdf)
                        pdf_generated = []
                    else:
                        st = pdf.stat()
                        atomic_write_text(
                            pdf.parent / pdf.stem / PDF_COMPLETE_FILE,
                            json.dumps({
                                "version": 1,
                                "source": {
                                    "size": st.st_size,
                                    "mtime_ns": st.st_mtime_ns,
                                },
                                "dpi": attempt_dpi,
                                "pages": [path.name for path in pdf_generated],
                            }, ensure_ascii=False, indent=2) + "\n")
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

    def _directory_manifest(
            self, directory: Path,
            stats: Dict[str, os.stat_result]) -> str:
        """Digest the exact directory state used by the wholesale-skip path.

        Count + total bytes cannot distinguish renames, swaps, same-size edits,
        or deleted sidecars. This manifest includes names, nanosecond mtimes,
        and recognized FurTag sidecar state while storing no absolute paths.
        """
        entries: List[object] = []
        for name in sorted(stats):
            media = directory / name
            st = stats[name]
            sidecars: List[Tuple[str, int, int]] = []
            candidates = (
                self._tag_sidecar_candidates(media)
                + self._url_sidecar_candidates(media)
                + self._json_sidecar_candidates(media)
            )
            for sidecar in dict.fromkeys(candidates):
                try:
                    sidecar_st = sidecar.stat()
                except OSError:
                    continue
                if (sidecar in self._json_sidecar_candidates(media)
                        and not _looks_like_furtag_json_sidecar(sidecar)):
                    continue
                sidecars.append((
                    sidecar.name,
                    sidecar_st.st_size,
                    sidecar_st.st_mtime_ns,
                ))
            entries.append((
                name, st.st_size, st.st_mtime_ns, sorted(sidecars)))
        encoded = json.dumps(
            entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def index(self, root: Path, ledger_mgr: LedgerManager,
              pdf_page_dirs: Set[Path],
              excluded_dirs: Optional[Set[Path]] = None
              ) -> Tuple[List[FileItem], Set[Path]]:
        """Walk the tree once and return the files that actually need work,
        videos first, plus the set of directories that needed a per-file check
        (for `finalize_dir_fingerprints` to potentially seal afterwards).

        Each directory carries its own Ledger (found in-place, or created).
        Before checking any individual record, a directory whose ledger has a
        sealed manifest matching names, sizes, mtimes, and sidecar state is
        skipped wholesale — no hash or lookup work at all.
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
            _prune_hidden_walk_dirs(dirs)
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
            manifest = self._directory_manifest(dp_path, stats)
            profile = self.search_profile_hash()
            sidecars_req = bool(self.write_sidecars)
            output_policy = self.output_policy_hash()
            if count and dir_ledger.fingerprint_matches(
                    count, total_size, manifest,
                    self.direct_notes_effective(),
                    search_profile_hash=profile,
                    sidecars_required=sidecars_req,
                    sidecar_format=self.sidecar_format_key(),
                    output_policy_hash=output_policy):
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
                # Search-complete (may still have pending unmatched import —
                # that is reconciled without re-search). Finalize seals only
                # when output is also complete.
                if self.local_path_complete(
                        p, dir_ledger, st, is_pdf_page=is_pdf_page,
                        root=root, search_profile_hash=profile,
                        require_output_complete=False):
                    if self.has_sidecar(p) and not is_pdf_page:
                        tagged += 1
                    else:
                        seen += 1
                    continue

                item = FileItem(path=p, relpath=str(p.relative_to(root)),
                                size=st.st_size, mtime=st.st_mtime, kind=kind,
                                ledger=dir_ledger, perceptual_only=is_pdf_page,
                                md5=dir_ledger.md5_for(
                                    fn, st.st_size, st.st_mtime,
                                    mtime_ns=st.st_mtime_ns),
                                mtime_ns=st.st_mtime_ns)
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
            if self.local_path_complete(
                    path, ledger, st, is_pdf_page=True,
                    root=root):
                continue
            items.append(FileItem(
                path=path, relpath=relpath, size=st.st_size, mtime=st.st_mtime,
                kind="image", ledger=ledger, perceptual_only=True,
                md5=ledger.md5_for(
                    path.name, st.st_size, st.st_mtime,
                    mtime_ns=st.st_mtime_ns),
                mtime_ns=st.st_mtime_ns))
        items.sort(key=lambda it: _natural_key(it.relpath))
        return items

    def finalize_dir_fingerprints(
            self, candidate_dirs: Set[Path],
            pdf_page_dirs: Set[Path],
            ledger_mgr: LedgerManager,
            root: Optional[Path] = None) -> None:
        """After a run, seal directories that are fully complete under policy.

        Uses the same :meth:`local_path_complete` predicate as index (BF-05),
        including search profile (BF-03), required sidecars (BF-04), and
        unmatched-import completeness. Does not seal after cancellation.
        """
        if self.cancelled():
            return
        profile = self.search_profile_hash()
        sidecars_req = bool(self.write_sidecars)
        fmt = self.sidecar_format_key()
        output_policy = self.output_policy_hash()
        for dp_path in candidate_dirs:
            dir_ledger = ledger_mgr.get(dp_path)
            try:
                names = sorted(
                    f for f in os.listdir(dp_path) if not f.startswith("."))
            except OSError:
                continue

            count = 0
            total_size = 0
            complete = True
            stats: Dict[str, os.stat_result] = {}
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
                stats[fn] = st
                is_pdf_page = (
                    kind == "image" and p.suffix.lower() == ".png"
                    and p.parent in pdf_page_dirs)
                if not self.local_path_complete(
                        p, dir_ledger, st, is_pdf_page=is_pdf_page,
                        root=root, search_profile_hash=profile):
                    complete = False
                    break

            if complete and count:
                dir_ledger.mark_dir_complete(
                    count, total_size,
                    self._directory_manifest(dp_path, stats),
                    self.direct_notes_effective(),
                    search_profile_hash=profile,
                    sidecars_required=sidecars_req,
                    sidecar_format=fmt,
                    output_policy_hash=output_policy)

    # ── Progress events ──────────────────────────────────────────────────────

    def _emit(self, kind: str, **fields: Any) -> None:
        """Single write path for progress. The engine never touches a display:
        `TerminalObserver` renders the CLI panel, `QtObserver` feeds the GUI, so
        each progress point is written exactly once."""
        self._observer.emit(RunEvent(kind=kind, **fields))

    def _notify_repeated(
            self, key: str, label: str, message: str,
            severity: str) -> None:
        """Report one repeated event, then bounded progress summaries."""
        with self._repeated_issue_lock:
            count, _, _, _ = self._repeated_issues.get(
                key, (0, label, message, severity))
            count += 1
            self._repeated_issues[key] = (
                count, label, message, severity)
        emit = notify_info if severity == "info" else notify
        if count == 1:
            emit(message)
        elif count % 25 == 0:
            emit(f"{label}: {count} so far; latest: {message}")

    def _notify_repeated_issue(
            self, key: str, label: str, message: str) -> None:
        self._notify_repeated(key, label, message, "warning")

    def _notify_repeated_info(
            self, key: str, label: str, message: str) -> None:
        self._notify_repeated(key, label, message, "info")

    def _flush_repeated_issues(self) -> None:
        with self._repeated_issue_lock:
            entries = list(self._repeated_issues.values())
            self._repeated_issues.clear()
        for count, label, message, severity in entries:
            if count > 1:
                emit = notify_info if severity == "info" else notify
                emit(
                    f"{label}: {count} files this run; latest: {message}")

    # ── Parallel local hashing ───────────────────────────────────────────────

    def hash_all(self, items: List[FileItem]) -> None:
        # Perceptual-only PDF pages still need a local hash for exact duplicate
        # detection, even though that hash is never sent to a booru.
        todo = [it for it in items if it.md5 is None]
        if not todo:
            return
        workers = min(8, (os.cpu_count() or 2))
        self._emit("status", track="perceptual",
                   sub=f"local hash · {len(todo)} file(s)")
        # The panel only owns the screen once a phase has begun; before that
        # (and headless / in the GUI) the plain lines are the only feedback.
        panel_live = _display is not None and _display.active()
        if not panel_live:
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
                        item.path.name, item.size, item.mtime, item.md5,
                        mtime_ns=item.mtime_ns)
                    dirty_ledgers.add(item.ledger)
                else:
                    # A local read/hash failure is not evidence that every
                    # remote source missed. In particular, hash-only videos
                    # must remain unresolved rather than becoming nomatch.
                    item.lookup_errors.add("local_hash")
                done += 1
                # Persist reusable hashes during the disk pass. An interrupted
                # run loses at most one small batch rather than the whole pass.
                if done % 25 == 0:
                    for ledger in dirty_ledgers:
                        ledger.save()
                    dirty_ledgers.clear()
                if (not panel_live and sys.stdout.isatty() and
                        (done % 25 == 0 or done == len(todo))):
                    sys.stdout.write(f"\r  hashed {done}/{len(todo)}")
                    sys.stdout.flush()
        for ledger in dirty_ledgers:
            ledger.save()
        if not panel_live and sys.stdout.isatty():
            sys.stdout.write("\n")

    def deduplicate(self, root: Path, items: List[FileItem],
                    ledger_mgr: LedgerManager,
                    canonical_items: Optional[List[FileItem]] = None
                    ) -> Tuple[List[FileItem], int, Dict[Path, List[FileItem]]]:
        """Remove exact-MD5 duplicates from this run before network searching.

        An unchanged earlier matched / nomatch / legacy hydrus_deleted ledger
        record wins over a new candidate (BF-08). Otherwise the first item in
        the stable videos/images + natural-path order is canonical. Skipped
        copies remain ``duplicate_pending`` until the canonical's output
        succeeds (or it is a clean no-match), preventing failed sidecar
        propagation from being checkpointed as complete.
        """
        profile = self.search_profile_hash()
        prior_statuses = frozenset({"matched", "nomatch", "hydrus_deleted"})
        canonical_by_md5: Dict[str, Path] = {}
        for ledger in sorted(ledger_mgr.touched(), key=lambda led: str(led.dir)):
            for name, rec in sorted(ledger.records.items()):
                if not isinstance(rec, dict) or rec.get("status") not in (
                        prior_statuses):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                # hydrus_deleted is legacy and not profile-gated; matched/nomatch
                # require a compatible search profile (BF-03).
                status = ledger.status_for(
                    name, st.st_size, st.st_mtime,
                    self.direct_notes_effective(),
                    search_profile_hash=(
                        None if rec.get("status") == "hydrus_deleted"
                        else profile))
                if status not in prior_statuses:
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
                               "duplicate_pending", [],
                               duplicate_of=canonical_rel,
                               mtime_ns=item.mtime_ns)
            duplicate_groups.setdefault(canonical, []).append(item)
            duplicate_count += 1

        self._write_duplicates_log(root, ledger_mgr)
        return survivors, duplicate_count, duplicate_groups

    @classmethod
    def _write_duplicates_log(cls, root: Path, ledger_mgr: LedgerManager) -> None:
        groups: Dict[Tuple[str, str], List[str]] = {}
        for path, _ledger, _name, _st, rec in cls._unchanged_records(
                ledger_mgr,
                lambda r: r.get("status") == "duplicate" or r.get("duplicate_of"),
                {"duplicate", "matched"}):
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
            atomic_write_text(log_path, "\n".join(lines))
        except OSError as e:
            notify(f"⚠️  Couldn't write {DUPLICATES_FILE}: {e}")

    # ── Hash tier (four boorus, concurrent per file) ─────────────────────────

    def _hash_lookup(
            self, service: str, md5: str,
    ) -> SourceMetadata:
        """Return the complete metadata payload for one exact-hash source."""
        if service == "e621":
            return self._e621_metadata_by_md5(md5)
        if service == "inkbunny":
            if not md5 or not self.has_inkbunny:
                return SourceMetadata()
            sub_ids = self._inkbunny_search_md5(md5)
            return (self._inkbunny_submission_metadata(sub_ids)
                    if sub_ids else SourceMetadata())
        if service == "danbooru":
            t, u = self.danbooru_lookup_by_md5(md5)
            return SourceMetadata(tags=t, urls=u)
        if service == "gelbooru":
            t, u = self.gelbooru_lookup_by_md5(md5)
            return SourceMetadata(tags=t, urls=u)
        return SourceMetadata()

    @staticmethod
    def _coerce_source_metadata(value) -> SourceMetadata:
        """Accept pre-payload test hooks and third-party overrides."""
        if isinstance(value, SourceMetadata):
            return value
        if isinstance(value, tuple):
            tags = set(value[0]) if len(value) > 0 else set()
            urls = set(value[1]) if len(value) > 1 else set()
            force = set(value[2]) if len(value) > 2 else set()
            notes = dict(value[3]) if len(value) > 3 else {}
            return SourceMetadata(tags, urls, notes, force)
        return SourceMetadata()

    def hash_tier(self, item: FileItem, ex: cf.Executor
                  ) -> HashTierResult:
        """Query every enabled booru for this file's MD5 concurrently and merge.
        MD5 identity is byte-exact, so there is zero false-positive risk and the
        tag sets genuinely differ — never short-circuit between them.

        The fourth return value is the set of source URLs that must be
        associated only (never queued for Hydrus downloader enrichment), e.g.
        multi-file InkBunny submission pages.
        """
        services = self.enabled_hash_services()
        metadata = SourceMetadata()
        hit: Set[str] = set()
        local_errors = {
            error for error in item.lookup_errors
            if error in ("local_hash", "local_media")}
        item.lookup_errors.clear()
        item.lookup_errors.update(local_errors)
        if item.md5:
            # A source disabled mid-run by a 401/403 is not a clean miss for the
            # files that come after it — they never even ask. Without this the
            # rest of the run would be sealed as "nomatch", and since the ledger
            # keys on (size, mtime) those files would stay skipped forever even
            # after the user fixes the key. Recording the error here (rather
            # than re-querying) keeps them unresolved without hammering the API.
            item.lookup_errors.update(
                s for s in HASH_SOURCES
                if s in self.auth_rejected_sources and self.source_enabled(s))
        if not item.md5 or not services:
            return HashTierResult(metadata, [])

        def _tick(state: Dict[str, str]) -> None:
            # Per-site ticker as a status event, so the GUI's sub-status slot
            # updates too. `extra["hash_state"]` carries the raw per-service
            # states for frontends that want to render them differently.
            self._emit("status", track="hash",
                       sub=LiveDisplay.hash_line(state),
                       extra={"hash_state": dict(state)})

        state = {s: "run" for s in services}
        futs = {ex.submit(self._hash_lookup, s, item.md5): s for s in services}
        _tick(state)
        for fut in cf.as_completed(futs):
            s = futs[fut]
            try:
                found = self._coerce_source_metadata(fut.result())
            except Exception as e:
                if self.cancelled() and isinstance(e, RetryableLookupError):
                    state[s] = "cancel"
                    _tick(state)
                    continue
                # Network/HTTP failure — distinct from a clean "not found" miss,
                # so surface it as ⚠ rather than ✗ (the file may still exist there).
                if not self._stop_for_broken_ca_bundle(e):
                    self._notify_repeated_issue(
                        f"hash_source:{s}", f"{s} lookup failures",
                        f"❌ {s} failed on {item.path.name}: {e}")
                item.lookup_errors.add(s)
                state[s] = "err"
                _tick(state)
                continue
            if found.tags or found.urls or found.notes:
                metadata.merge(found)
                hit.add(s)
                state[s] = "hit"
            else:
                state[s] = "miss"
            _tick(state)

        sources = [s for s in services if s in hit]   # deterministic order
        return HashTierResult(metadata, sources)

    # ── Perceptual tier (Fluffle → SauceNAO, sequential) ─────────────────────

    def perceptual_tier(self, item: FileItem
                        ) -> PerceptualTierResult:
        """Run Fluffle → SauceNAO. Returns (tags, urls, sources, review_raw).

        When *review_raw* is set, the caller should queue a PendingReview and
        not write final results / nomatch for this file.
        """
        metadata = SourceMetadata()
        sources: List[str] = []
        review_raw: Optional[Dict] = None
        fp = item.path

        def _status(msg: str) -> None:
            self._emit("status", track="perceptual", sub=msg)

        if self.source_active("fluffle"):
            _status("Fluffle…")
            js = self.fluffle_search(fp)
            if js:
                f_tags, f_urls, md5_u, pid, review_raw = self.find_best_exact_match(js)
                if f_tags or f_urls:
                    metadata.tags |= f_tags
                    metadata.urls |= f_urls
                    # A perceptual hit only tells us which post this is — re-query
                    # e621 by ID for the full, properly-namespaced tag set.
                    if self.source_active("e621") and (pid or md5_u):
                        _status("Fluffle → e621 enrich…")
                        e_metadata = (self._e621_metadata_by_id(pid)
                                      if pid else SourceMetadata())
                        if not (e_metadata.tags or e_metadata.urls) and md5_u:
                            e_metadata = self._e621_metadata_by_md5(md5_u)
                        metadata.merge(e_metadata)
                    sources.append("fluffle")
                    review_raw = None  # auto-accepted; no review needed

        # SauceNAO is the slowest stage (6s pace, longer after a quota backoff),
        # so don't start it for a file the user has already cancelled out of.
        if (not (metadata.tags or metadata.urls) and review_raw is None
                and not self.cancelled() and self.source_active("saucenao")):
            _status("SauceNAO…")
            service, rid, s_tags, s_urls = self.saucenao_search(fp)
            if service and rid:
                # High-confidence booru match → pull the authoritative,
                # properly-namespaced tag set instead of SauceNAO's own.
                _status(f"SauceNAO → {service} enrich…")
                authoritative = self._authoritative_metadata(service, rid)
                if authoritative.tags or authoritative.urls:
                    metadata.merge(authoritative)
                    metadata.urls |= s_urls
                    sources.append("saucenao")
                elif s_tags or s_urls:      # post gone — use own tags
                    metadata.tags |= s_tags
                    metadata.urls |= s_urls
                    sources.append("saucenao")
            elif s_tags or s_urls:
                # Resolved to a site we can't re-query (FA/Twitter/...) —
                # SauceNAO's own thinner tags are the best we've got.
                metadata.tags |= s_tags
                metadata.urls |= s_urls
                sources.append("saucenao")

        return PerceptualTierResult(metadata, sources, review_raw)

    def _queue_pending_review(self, item: FileItem, root: Path,
                              review_raw: Dict) -> PendingReview:
        """Persist a pending_review ledger status + ReviewQueue entry."""
        platform = review_raw.get("platform", "") or ""
        loc = review_raw.get("location", "") or ""
        match_class = review_raw.get("match", "") or ""
        tags, urls = self._fluffle_result_payload(review_raw)
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
                           "pending_review", ["pending_review"],
                           mtime_ns=item.mtime_ns)
        return pending

    def resolve_pending_review(self, pending: PendingReview, approve: bool,
                               root: Optional[Path] = None) -> bool:
        """Approve (enrich + write) or reject (nomatch) a pending review item.

        Reject persists the human decision even when a required unmatched
        Hydrus import is incomplete, so the UI does not re-prompt. Incomplete
        output remains on the ledger for later reconciliation.
        """
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
            notes: Dict[str, str] = {}
            sources = ["fluffle"]
            pid = pending.post_id
            md5_u = pending.md5_from_url
            if self.source_active("e621") and (pid or md5_u):
                e_metadata = (self._e621_metadata_by_id(pid)
                              if pid else SourceMetadata())
                if not (e_metadata.tags or e_metadata.urls) and md5_u:
                    e_metadata = self._e621_metadata_by_md5(md5_u)
                tags |= e_metadata.tags
                urls |= e_metadata.urls
                notes.update(e_metadata.notes)
                if e_metadata.tags or e_metadata.urls:
                    sources.append("e621")
            # Only a genuine rendered PDF page gets comic:/page: — an ordinary
            # PNG would otherwise pick up comic:<its folder name>.
            if _is_pdf_page_render(path):
                tags |= self._pdf_page_base_tags(path)
            outcome = self.write_results_detailed(
                path, tags, urls, notes=notes)
            resumable_hydrus = (
                outcome.hydrus_complete is False
                and outcome.sidecar_complete is True
                and isinstance(outcome.hydrus_output, dict)
                and isinstance(
                    outcome.hydrus_output.get("resume_from_sidecars"), dict))
            if not outcome.complete and not resumable_hydrus:
                notify(
                    f"⚠️  Metadata output incomplete for {path.name}; "
                    "keeping it in the review queue for retry.")
                return False
            sha = outcome.sha256
            status = outcome.ledger_status or "matched"
            self.ledger_record(
                ledger,
                path.name, st.st_size, st.st_mtime, pending.md5,
                status, sources, sha256=sha,
                direct_notes_applied=(
                    self.direct_notes_effective()
                    if outcome.complete else False),
                hydrus_output=outcome.hydrus_output,
                review={
                    "decision": "approved",
                    "decided_at": time.time(),
                    "output_complete": bool(outcome.complete),
                })
            if resumable_hydrus:
                notify(
                    f"⚠️  Approved {path.name}; tags were saved to sidecars "
                    "and Hydrus output will retry on the next launch.")
        else:
            outcome = self.write_unmatched_detailed(path)
            # Always record the reject decision. Incomplete unmatched import
            # stays on the ledger for prior-nomatch reconciliation without
            # re-opening the interactive review UI.
            self.ledger_record(
                ledger,
                path.name, st.st_size, st.st_mtime, pending.md5,
                "nomatch", [], sha256=outcome.sha256,
                unmatched_import=outcome.unmatched_import,
                review={
                    "decision": "rejected",
                    "decided_at": time.time(),
                    "output_complete": bool(outcome.complete),
                })
            if not outcome.complete:
                notify(
                    f"⚠️  Rejected {path.name}; required Hydrus import is "
                    "still pending and will retry on a later scan.")
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
        """Install this run's single observer, then run the pipeline.

        Terminal mode wraps a `LiveDisplay` in a `TerminalObserver`; the GUI
        passes its own. Either way the engine below only ever emits events, so
        every progress point is rendered exactly once.
        """
        global _display

        disp = LiveDisplay() if use_terminal_display else None
        prev_observer, prev_display = self._observer, _display
        self._observer = observer or TerminalObserver(disp)
        prev_active = set_active_observer(self._observer)
        self._display_detached = False
        if disp is not None:
            _display = disp
        if options and options.settings_override is not None:
            self.apply_settings(options.settings_override)
        self._hydrus_start_result_page_run()
        try:
            return self._run_pipeline(
                root, options, cancel_event, use_terminal_display, pdf_dpi)
        finally:
            self._hydrus_finalize_result_page_run()
            self._detach_display()
            _display = prev_display
            set_active_observer(prev_active)
            self._observer = prev_observer

    def _detach_display(self) -> None:
        """Tear the live panel down and stop routing messages into it.

        Called from the pipeline's own `finally` (before the closing ledger /
        fingerprint work, which still calls `notify()`) and again by `_run_impl`
        as a backstop; the second call is a no-op."""
        global _display
        if self._display_detached:
            return
        self._display_detached = True
        self._emit("close_display")
        _display = None
        if isinstance(self._observer, TerminalObserver) and self._observer.display:
            # The panel is gone; later warnings must print, not redraw it.
            self._observer = TerminalObserver(None)
            set_active_observer(self._observer)

    def _run_pipeline(self, root: Path,
                      options: Optional[RunOptions],
                      cancel_event: Optional[threading.Event],
                      use_terminal_display: bool,
                      pdf_dpi: Optional[int]) -> ScanSummary:
        if cancel_event is None:
            self.cancel_event = threading.Event()
        else:
            # A caller owns this event. If it was set between worker creation
            # and pipeline entry, clearing it here would lose the cancellation.
            self.cancel_event = cancel_event
        self._fatal_network_error = False
        with self._repeated_issue_lock:
            self._repeated_issues.clear()
        self._bind_cancel_to_pacers()
        summary = ScanSummary(
            source_hits={k: 0 for k in
                         ("e621", "inkbunny", "danbooru",
                          "gelbooru", "fluffle", "saucenao")})

        # Apply run options onto instance
        if options is not None:
            self.hydrus_import_unmatched = options.import_unmatched
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

        # Resolve PDF DPI + comic/artist meta from options / settings / prompt
        # — only when rendering is enabled and discover actually queued jobs.
        if pdf_jobs and self.settings.pdf.pdf_enabled:
            if pdf_dpi is not None:
                chosen_dpi = pdf_dpi
            elif options is not None and options.pdf_dpi is not None:
                chosen_dpi = options.pdf_dpi
            elif use_terminal_display and sys.stdin.isatty():
                chosen_dpi = prompt_for_pdf_dpi(len(pdf_jobs))
            else:
                chosen_dpi = self.settings.pdf.pdf_dpi or PDF_DPI

            pdf_meta: Dict[str, Dict[str, str]] = {}
            if options is not None and options.pdf_meta:
                pdf_meta = dict(options.pdf_meta)
            elif use_terminal_display and sys.stdin.isatty():
                pdf_meta = prompt_for_pdf_meta(pdf_jobs)
            # Non-interactive fallback: comic = PDF stem (historical default).

            print(f"📄 Rendering {len(pdf_jobs)} PDF(s) at {chosen_dpi} DPI in background…")
            pdf_executor = cf.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="pdf-render")
            pdf_future = pdf_executor.submit(
                self.render_pdf_jobs, pdf_jobs, chosen_dpi, pdf_completed,
                pdf_meta)
        elif pdf_jobs and not self.settings.pdf.pdf_enabled:
            # Defensive: discover should have returned [] when disabled.
            pdf_jobs = []

        prior_matches = self._hydrus_reconcile_prior_matches(
            ledger_mgr, items)
        if (prior_matches.attempted or prior_matches.missing_payload):
            ledger_mgr.save_all()
        if prior_matches.completed:
            print(
                f"✅ Hydrus recovered {prior_matches.completed} previously "
                "tagged file(s) directly from their sidecars; no source "
                "lookups needed.")
        if prior_matches.failed:
            notify(
                f"⚠️  Hydrus output remains pending for "
                f"{prior_matches.failed} previously tagged file(s); they will "
                "retry on the next launch without source lookups.")
        if prior_matches.missing_payload:
            notify(
                f"⚠️  {prior_matches.missing_payload} pending Hydrus file(s) "
                "lost their required sidecar payload and will use the normal "
                "lookup pipeline instead.")

        prior_reconciled = self._hydrus_import_prior_nomatches(
            ledger_mgr, items)
        if prior_reconciled.completed or prior_reconciled.failed:
            ledger_mgr.save_all()
        if prior_reconciled.completed:
            parts = []
            if prior_reconciled.live:
                parts.append(f"{prior_reconciled.live} live/present")
            if prior_reconciled.previously_deleted:
                parts.append(
                    f"{prior_reconciled.previously_deleted} previously deleted")
            if prior_reconciled.vetoed:
                parts.append(f"{prior_reconciled.vetoed} vetoed")
            if prior_reconciled.other_terminal:
                parts.append(f"{prior_reconciled.other_terminal} other terminal")
            print(
                f"✅ Hydrus reconciled {prior_reconciled.completed} prior "
                f"no-match file(s) ({', '.join(parts)}); removed them from "
                "this run's search queue")
        if prior_reconciled.failed:
            notify(
                f"⚠️  Hydrus reconciliation remains pending for "
                f"{prior_reconciled.failed} prior no-match file(s).")
        has_prior_matches = self._has_prior_matched_files(ledger_mgr)
        build_already = bool(
            self.has_hydrus and self.hydrus_already_tagged_page_enabled
            and has_prior_matches)
        already_tagged = self._hydrus_populate_already_tagged_page(
            ledger_mgr, self.hydrus_already_tagged_page_limit
            if build_already else None)
        if already_tagged:
            ledger_mgr.save_all()
            print(f"✅ Already Tagged page → {already_tagged} ledger-matched file(s)")
        if not items and pdf_future is None:
            print("✅ Nothing to do — everything is tagged or already checked.")
            self.finalize_dir_fingerprints(
                candidate_dirs, pdf_page_dirs, ledger_mgr, root=root)
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
            self.finalize_dir_fingerprints(
                candidate_dirs, pdf_page_dirs, ledger_mgr, root=root)
            ledger_mgr.save_all()
            return summary

        print(f"🔄 {self.enabled_pipeline_description()}")
        print(f"   {LiveDisplay._LEGEND}\n")

        counts = summary.source_hits
        tagged = nomatch = pending_review_count = 0
        counts_lock = threading.Lock()
        duplicate_lock = threading.Lock()
        duplicates_tagged = prior_duplicates_tagged

        def _bump_hit(sources: List[str]) -> Dict[str, int]:
            nonlocal tagged
            with counts_lock:
                tagged += 1
                for s in sources:
                    counts[s] = counts.get(s, 0) + 1
                return dict(counts)

        def _bump_miss() -> None:
            nonlocal nomatch
            with counts_lock:
                nomatch += 1

        def _bump_pending() -> None:
            nonlocal pending_review_count
            with counts_lock:
                pending_review_count += 1

        def _propagate_duplicates(
                item: FileItem, tags: Set[str], urls: Set[str],
                sources: List[str], sha256: Optional[str],
                force_associate_urls: Optional[Set[str]] = None,
                notes: Optional[Dict[str, str]] = None,
                ledger_status: str = "matched",
                hydrus_output: Optional[Dict] = None,
        ) -> None:
            nonlocal duplicates_tagged
            with duplicate_lock:
                copies = duplicate_groups.pop(item.path, [])
            copied = self._propagate_duplicate_results(
                root, item, copies, tags, urls, sources, sha256,
                force_associate_urls=force_associate_urls, notes=notes,
                ledger_status=ledger_status, hydrus_output=hydrus_output)
            if copied:
                with counts_lock:
                    duplicates_tagged += copied

        def _resolve_duplicate_nomatches(item: FileItem) -> None:
            with duplicate_lock:
                copies = duplicate_groups.pop(item.path, [])
            self._resolve_duplicate_nomatches(root, item, copies)

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

        def _checkpoint_pending_hydrus_match(
                item: FileItem, outcome: WriteOutcome,
                sources: List[str], track: str) -> bool:
            """Seal the lookup when sidecars can resume only the failed sink."""
            checkpoint = outcome.hydrus_output
            resumable = (
                outcome.hydrus_complete is False
                and outcome.sidecar_complete is True
                and isinstance(checkpoint, dict)
                and isinstance(
                    checkpoint.get("resume_from_sidecars"), dict))
            if not resumable:
                return False
            self.ledger_record(
                item.ledger,
                item.path.name, item.size, item.mtime,
                item.md5, "matched", sources,
                sha256=outcome.sha256,
                direct_notes_applied=False,
                hydrus_output=checkpoint)
            source_totals = _bump_hit(sources)
            self._emit(
                "finish_file", track=track,
                result="tagged in sidecar — Hydrus import pending",
                source_hits=source_totals,
                extra={"retryable": True,
                       "failed_sources": ["hydrus_output"],
                       "hydrus_pending": True})
            _maybe_save_ledgers()
            return True

        hash_items = [it for it in items if not it.perceptual_only]
        perceptual_q: "queue.Queue" = queue.Queue()
        seed_count = 0
        for it in items:
            if it.perceptual_only:
                perceptual_q.put(it)
                seed_count += 1

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
                    self._emit("start_file", track="perceptual", index=idx,
                               current=item.path.name,
                               nxt=f"{perceptual_q.qsize()} queued")
                    perceptual_result = self.perceptual_tier(item)
                    tags, urls, sources, review_raw = perceptual_result
                    notes = (perceptual_result.metadata.notes
                             if isinstance(perceptual_result, PerceptualTierResult)
                             else {})
                    if review_raw is not None and not (tags or urls):
                        self._queue_pending_review(item, root, review_raw)
                        _bump_pending()
                        self._emit("finish_file", track="perceptual",
                                   result="⏳ needs review",
                                   extra={"pending_review": True})
                    elif tags or urls:
                        if item.perceptual_only:
                            tags = set(tags) | self._pdf_page_base_tags(item.path)
                        outcome = self.write_results_detailed(
                            item.path, tags, urls, item.sha256,
                            **({"notes": notes} if notes else {}))
                        if not outcome.complete:
                            if _checkpoint_pending_hydrus_match(
                                    item, outcome, sources, "perceptual"):
                                continue
                            item.lookup_errors.add("output")
                            if outcome.sha256:
                                item.ledger.cache_sha256(
                                    item.path.name, item.size, item.mtime,
                                    outcome.sha256,
                                    mtime_ns=item.mtime_ns)
                            self._emit(
                                "finish_file", track="perceptual",
                                result="retry later — output incomplete",
                                extra={"retryable": True,
                                       "failed_sources": ["output"]})
                            _maybe_save_ledgers()
                            continue
                        sha = outcome.sha256
                        status = outcome.ledger_status or "matched"
                        self.ledger_record(
                            item.ledger,
                            item.path.name, item.size, item.mtime,
                            item.md5, status, sources, sha256=sha,
                            direct_notes_applied=self.direct_notes_effective(),
                            hydrus_output=outcome.hydrus_output)
                        _propagate_duplicates(
                            item, tags, urls, sources, sha, notes=notes,
                            ledger_status=status,
                            hydrus_output=outcome.hydrus_output)
                        source_totals = _bump_hit(sources)
                        result = f"{'+'.join(sources)}  ({len(tags)} tags)"
                        ho = outcome.hydrus_output or {}
                        if ho.get("import_state") == "previously_deleted":
                            result += " · hydrus deleted"
                        self._emit("finish_file", track="perceptual",
                                   result=result, source_hits=source_totals)
                    else:
                        if item.lookup_errors:
                            failed = "+".join(sorted(item.lookup_errors))
                            self._emit(
                                "finish_file", track="perceptual",
                                result=f"retry later — {failed} error",
                                extra={"retryable": True,
                                       "failed_sources":
                                           sorted(item.lookup_errors)})
                        else:
                            outcome = self.write_unmatched_detailed(
                                item.path, item.sha256)
                            self.ledger_record(
                                item.ledger,
                                item.path.name, item.size, item.mtime,
                                item.md5, "nomatch", [],
                                sha256=outcome.sha256,
                                unmatched_import=outcome.unmatched_import)
                            if outcome.complete:
                                _resolve_duplicate_nomatches(item)
                                _bump_miss()
                                self._emit(
                                    "finish_file", track="perceptual",
                                    result="— no match")
                            else:
                                self._emit(
                                    "finish_file", track="perceptual",
                                    result="no match — hydrus import pending",
                                    extra={"retryable": True,
                                           "failed_sources": ["output"]})
                    _maybe_save_ledgers()
                except RetryableMediaError as e:
                    item.lookup_errors.add("local_media")
                    self._emit(
                        "finish_file", track="perceptual",
                        result="retry later — local media error",
                        extra={"retryable": True,
                               "failed_sources": ["local_media"]})
                    notify(f"⚠️  {e}; the file will be retried.")
                    _maybe_save_ledgers()
                except RetryableLookupError as e:
                    if self.cancelled():
                        self._emit(
                            "finish_file", track="perceptual",
                            result="cancelled",
                            extra={"retryable": True, "cancelled": True})
                    else:
                        item.lookup_errors.add("perceptual")
                        self._emit(
                            "finish_file", track="perceptual",
                            result="retry later — perceptual source error",
                            extra={"retryable": True,
                                   "failed_sources": ["perceptual"]})
                        self._notify_repeated_issue(
                            "perceptual_lookup", "Perceptual lookup failures",
                            f"❌ perceptual lookup failed on "
                            f"{item.path.name}: {e}")
                    _maybe_save_ledgers()
                except UnusableMediaError as e:
                    self.ledger_record(
                        item.ledger,
                        item.path.name, item.size, item.mtime,
                        item.md5, "unreadable", [],
                        decoder_profile=self.decoder_profile(),
                        unreadable_reason=str(e))
                    _resolve_duplicate_nomatches(item)
                    self._emit(
                        "finish_file", track="perceptual",
                        result="skipped — unreadable media",
                        extra={"retryable": False,
                               "failed_sources": ["local_media"]})
                    notify(f"⚠️  {e}; marked unreadable until the file changes.")
                    _maybe_save_ledgers()
                except Exception as e:
                    if not self._stop_for_broken_ca_bundle(e):
                        notify(
                            f"❌ perceptual worker error on "
                            f"{item.path.name}: {e}")
                finally:
                    perceptual_q.task_done()

        try:
            hash_interval = max(
                (self.pace[s].interval for s in self.enabled_hash_services()),
                default=0.0)
            hash_phase_sources = "·".join(
                s for s in HASH_SOURCES if self.source_enabled(s))
            perceptual_phase_sources = " → ".join(
                {"fluffle": "Fluffle", "saucenao": "SauceNAO"}[s]
                for s in ("fluffle", "saucenao") if self.source_enabled(s))
            self._emit(
                "begin_phase", track="hash",
                phase=(
                    "Phase · hash lookups ("
                    + (hash_phase_sources or "none enabled")
                    + ")"),
                total=len(hash_items), extra={"interval": hash_interval})
            self._emit(
                "begin_phase", track="perceptual",
                phase=(
                    "Phase · perceptual ("
                    + (perceptual_phase_sources or "none enabled")
                    + ")"),
                total=seed_count, extra={"growing": True})

            perc_thread = threading.Thread(
                target=perceptual_worker, name="perceptual-worker", daemon=True)
            perc_thread.start()

            with cf.ThreadPoolExecutor(max_workers=hash_workers) as ex:
                for i, item in enumerate(hash_items):
                    if self.cancelled():
                        break
                    nxt = hash_items[i + 1].path.name if i + 1 < len(hash_items) else None
                    self._emit("start_file", track="hash", index=i + 1,
                               current=item.path.name, nxt=nxt or "")

                    hash_result = self.hash_tier(item, ex)
                    tags, urls, sources, force_assoc = hash_result
                    notes = (hash_result.metadata.notes
                             if isinstance(hash_result, HashTierResult) else {})
                    if tags or urls:
                        # Hash sources are additive: an e621 hit does not make a
                        # failed InkBunny/Danbooru/Gelbooru lookup irrelevant.
                        # Committing the partial hit would permanently skip the
                        # failed source on later scans and could lose its tags or
                        # description. Keep the cached MD5 unresolved and retry
                        # the complete fan-out later.
                        if item.lookup_errors:
                            failed = "+".join(sorted(item.lookup_errors))
                            self._emit(
                                "finish_file", track="hash",
                                result=(
                                    f"partial match — retry later ({failed})"),
                                extra={"retryable": True,
                                       "failed_sources":
                                           sorted(item.lookup_errors)})
                            _maybe_save_ledgers()
                            continue
                        outcome = self.write_results_detailed(
                            item.path, tags, urls, item.sha256,
                            url_policy=UrlWritePolicy.ENRICH_HASH_POSTS,
                            force_associate_urls=force_assoc,
                            **({"notes": notes} if notes else {}))
                        if not outcome.complete:
                            if _checkpoint_pending_hydrus_match(
                                    item, outcome, sources, "hash"):
                                continue
                            item.lookup_errors.add("output")
                            if outcome.sha256:
                                item.ledger.cache_sha256(
                                    item.path.name, item.size, item.mtime,
                                    outcome.sha256,
                                    mtime_ns=item.mtime_ns)
                            self._emit(
                                "finish_file", track="hash",
                                result="retry later — output incomplete",
                                extra={"retryable": True,
                                       "failed_sources": ["output"]})
                            _maybe_save_ledgers()
                            continue
                        sha = outcome.sha256
                        status = outcome.ledger_status or "matched"
                        self.ledger_record(
                            item.ledger,
                            item.path.name, item.size, item.mtime,
                            item.md5, status, sources, sha256=sha,
                            direct_notes_applied=self.direct_notes_effective(),
                            hydrus_output=outcome.hydrus_output)
                        _propagate_duplicates(
                            item, tags, urls, sources, sha,
                            force_associate_urls=force_assoc, notes=notes,
                            ledger_status=status,
                            hydrus_output=outcome.hydrus_output)
                        source_totals = _bump_hit(sources)
                        result = f"{'+'.join(sources)}  ({len(tags)} tags)"
                        ho = outcome.hydrus_output or {}
                        if ho.get("import_state") == "previously_deleted":
                            result += " · hydrus deleted"
                        self._emit(
                            "finish_file", track="hash", result=result,
                            source_hits=source_totals)
                    elif item.kind == "image":
                        if item.lookup_errors:
                            # Do not spend slow perceptual quota—or commit a
                            # weaker match—while any exact, additive hash source
                            # still has an unknown result.
                            failed = "+".join(sorted(item.lookup_errors))
                            self._emit(
                                "finish_file", track="hash",
                                result=f"retry later — {failed} error",
                                extra={"retryable": True,
                                       "failed_sources":
                                           sorted(item.lookup_errors)})
                        else:
                            perceptual_q.put(item)
                            # One grow per queued file — the observer is the only
                            # write path, so the perceptual total can't double-count.
                            self._emit("grow", track="perceptual")
                            self._emit("finish_file", track="hash",
                                       result="no hash match → perceptual")
                    else:                                  # video: hash-only
                        if item.lookup_errors:
                            failed = "+".join(sorted(item.lookup_errors))
                            self._emit(
                                "finish_file", track="hash",
                                result=f"retry later — {failed} error",
                                extra={"retryable": True,
                                       "failed_sources":
                                           sorted(item.lookup_errors)})
                        else:
                            outcome = self.write_unmatched_detailed(
                                item.path, item.sha256)
                            self.ledger_record(
                                item.ledger,
                                item.path.name, item.size, item.mtime,
                                item.md5, "nomatch", [],
                                sha256=outcome.sha256,
                                unmatched_import=outcome.unmatched_import)
                            if outcome.complete:
                                _resolve_duplicate_nomatches(item)
                                _bump_miss()
                                self._emit(
                                    "finish_file", track="hash",
                                    result="— no match")
                            else:
                                self._emit(
                                    "finish_file", track="hash",
                                    result="no match — hydrus import pending",
                                    extra={"retryable": True,
                                           "failed_sources": ["output"]})
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
                        # Real event, not just a panel poke: without it the GUI's
                        # perceptual card looks frozen for the whole PDF wait.
                        self._emit("status", track="perceptual",
                                   sub=f"waiting for PDF render · "
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
                        notify_info(
                            f"♊ {pdf_duplicates} duplicate PDF page(s) skipped; "
                            f"see {DUPLICATES_FILE}.")
                    for item in pdf_items:
                        candidate_dirs.add(item.path.parent)
                        perceptual_q.put(item)
                        self._emit("grow", track="perceptual")
                    items.extend(pdf_items)
                    pdf_completed.task_done()
                    self._emit("status", track="perceptual",
                               sub=f"PDF {pdfs_received}/{len(pdf_jobs)} · "
                                   f"{effective_dpi} DPI · {pdf_name}")
                    perceptual_q.join()
                try:
                    pdf_future.result()
                except Exception as e:
                    notify(f"⚠️  Background PDF rendering failed: {e}")

            self._emit("freeze_total", track="perceptual")
            perceptual_q.put(_PERCEPTUAL_DONE)
            perc_thread.join()
        finally:
            if pdf_executor is not None:
                pdf_executor.shutdown(wait=True)
            self._flush_repeated_issues()
            # Close the panel before the closing bookkeeping below, whose
            # notify()s must print rather than redraw a torn-down panel.
            self._detach_display()
            self.finalize_dir_fingerprints(
                candidate_dirs, pdf_page_dirs, ledger_mgr, root=root)
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


def _sidecar_name_regex(pattern: str, name_re: str, ext_re: str) -> str:
    """Translate a ``{name}``/``{ext}`` sidecar pattern into a regex body.

    Placeholder-order agnostic (``{name}.tags{ext}`` works), so purge/detection
    logic always derives its names from the same patterns the writer used.
    """
    parts: List[str] = []
    for chunk in re.split(r"(\{name\}|\{ext\})", pattern):
        if chunk == "{name}":
            parts.append(name_re)
        elif chunk == "{ext}":
            parts.append(ext_re)
        elif chunk:
            parts.append(re.escape(chunk))
    return "".join(parts)


def _json_sidecar_patterns(settings: Optional[Settings] = None) -> List[str]:
    """JSON sidecar name patterns FurTag could have written under *settings*."""
    patterns = [DEFAULT_JSON_PATTERN]
    if settings is None:
        try:
            settings = SettingsStore().load()
        except Exception:
            settings = None
    if settings is not None:
        configured = str(settings.output.sidecar_json_filename or "").strip()
        if configured and configured not in patterns:
            patterns.append(configured)
    return patterns


def _looks_like_furtag_json_sidecar(path: Path) -> bool:
    """True only for a JSON object of exactly FurTag's own sidecar shape.

    ``<media>.<ext>.json`` is also gallery-dl's default metadata filename, so a
    name match alone must never mark a file deletable — the content has to be
    FurTag's ``{"tags": [...], "urls": [...]}`` payload and nothing else.
    """
    try:
        if path.stat().st_size > 4 * 1024 * 1024:
            return False                        # far larger than any FurTag sidecar
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict) or not data:
        return False
    if set(data.keys()) - {"tags", "urls"}:
        return False                            # extra keys → someone else's file
    return all(isinstance(v, list) for v in data.values())


# Namespaces FurTag itself writes into a ``.txt`` tag sidecar: the booru
# category mappings (e621 artist/copyright/character/species/lore, Danbooru and
# Gelbooru artist/character/copyright), Fluffle's ``site:``/``creator:``, the
# SauceNAO ``title:``/``series:`` fields, and the rendered-PDF ``comic:``/
# ``page:`` base tags. Everything else FurTag emits is a bare general tag.
_FURTAG_TAG_NAMESPACES = (
    "character", "comic", "creator", "lore", "page", "series", "site",
    "species", "title",
)
_NAMESPACED_TAG_RE = re.compile(
    r"(?:%s):[^\s]" % "|".join(_FURTAG_TAG_NAMESPACES), re.I)
_URL_LINE_RE = re.compile(r"https?://[^\s]+")
# A real sidecar is a handful of short lines; anything past this is someone
# else's file and must never be read whole into memory.
_MAX_TXT_SIDECAR_BYTES = 1024 * 1024
_MAX_TAG_LINE_CHARS = 200


def _read_small_utf8(path: Path, max_bytes: int) -> Optional[str]:
    """Strict-UTF-8 contents of *path*, or None if unreadable/too big/not UTF-8.

    Reads at most ``max_bytes + 1`` bytes, so a huge file costs one bounded read
    rather than its full size in memory. ``None`` always means "can't vouch for
    this file", which callers must treat as *not* FurTag's.
    """
    try:
        with path.open("rb") as fh:
            blob = fh.read(max_bytes + 1)
    except OSError:
        return None
    if len(blob) > max_bytes:
        return None
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _is_furtag_tag_line(line: str) -> bool:
    """True for a line shaped like one tag as ``_append_lines`` would write it.

    Tags reach the sidecar already stripped and single-line, never contain a URL
    (those go to the ``.urls.txt``), and are short. Prose, indented notes, and
    Stable Diffusion prompt blobs all fail at least one of these.

    Spaces are allowed *only* inside a namespaced value, because SauceNAO writes
    real titles (``title:Some Comic Name``). A bare tag from any booru is
    underscore-joined and never contains a space, so a spaced un-namespaced line
    is prose — which is what keeps a notes file that happens to open with
    ``title:my great idea`` from being classed as ours and deleted.
    """
    if not (line and line == line.strip()
            and len(line) <= _MAX_TAG_LINE_CHARS
            and "://" not in line
            and not any(ord(ch) < 32 for ch in line)):
        return False
    return " " not in line or bool(_NAMESPACED_TAG_RE.match(line))


def _looks_like_furtag_txt_sidecar(path: Path, *, urls: bool) -> bool:
    """True only for text of exactly FurTag's own ``.txt`` sidecar shape.

    ``<media>.<ext>.txt`` is also what gallery-dl ``--write-tags``, Stable
    Diffusion prompt dumps, and plain hand-written notes are called, and Reset
    deletes with ``unlink()`` — no trash, no undo. So the bias is hard toward
    keeping: unreadable, non-UTF-8, oversized, empty, and ambiguous files are
    all reported as *not* ours. Failing to delete a real sidecar leaves clutter
    the user can remove by hand; the other mistake is unrecoverable.

    A URL sidecar must be nothing but ``http(s)`` URLs. A tag sidecar must be
    nothing but tag-shaped lines *and* carry at least one tag in a namespace
    FurTag actually emits (``creator:``, ``character:``, ``species:``, ...).
    """
    text = _read_small_utf8(path, _MAX_TXT_SIDECAR_BYTES)
    if text is None:
        return False
    # split("\n") rather than splitlines(): splitlines() also breaks on \x0b,
    # \x0c and \u2028, which would hide the control characters we screen for.
    lines = [ln.rstrip("\r") for ln in text.split("\n")]
    body = [ln for ln in lines if ln.strip()]
    if not body:
        # FurTag never writes an empty text sidecar: _write_sidecar_results
        # returns early with no tags/urls, and _append_lines only opens the file
        # when it has something to append.
        return False
    if urls:
        return all(_URL_LINE_RE.fullmatch(ln) for ln in body)
    if not all(_is_furtag_tag_line(ln) for ln in body):
        return False
    return any(_NAMESPACED_TAG_RE.match(ln) for ln in body)


def _is_furtag_sidecar(path: Path, json_patterns: Optional[List[str]] = None
                       ) -> bool:
    """True only for FurTag sidecars — by name *and* by content.

    Every branch verifies the body, because each of these names is also used by
    other tools (gallery-dl metadata/tag dumps) and by users keeping notes
    beside their media, and Reset deletes what this returns True for.
    """
    name = path.name.lower()
    media_exts = IMG_EXTS | VIDEO_EXTS
    if any(name.endswith(ext + ".urls.txt") for ext in media_exts):
        return _looks_like_furtag_txt_sidecar(path, urls=True)
    if any(name.endswith(ext + ".txt") for ext in media_exts):
        return _looks_like_furtag_txt_sidecar(path, urls=False)
    if not name.endswith(".json"):
        return False
    if json_patterns is None:
        json_patterns = _json_sidecar_patterns()
    ext_re = "(?:%s)" % "|".join(sorted(
        (re.escape(e) for e in media_exts), key=len, reverse=True))
    for pattern in json_patterns:
        try:
            body = _sidecar_name_regex(pattern, r".+", ext_re)
            if re.fullmatch(body, path.name, re.I):
                return _looks_like_furtag_json_sidecar(path)
        except re.error:
            continue
    return False


def _nuke_candidates(root: Path, settings: Optional[Settings] = None
                     ) -> Tuple[List[Path], List[Path]]:
    """Find generated ledgers and sidecars below root without following links."""
    ledgers: List[Path] = []
    sidecars: List[Path] = []
    json_patterns = _json_sidecar_patterns(settings)
    ledger_names = {LEDGER_FILE, LEDGER_FILE + ".tmp", DUPLICATES_FILE,
                    DUPLICATES_FILE + ".tmp"}
    for dp, dirs, files in os.walk(root, followlinks=False):
        _prune_hidden_walk_dirs(dirs)
        for fn in sorted(files):
            path = Path(dp) / fn
            if fn in ledger_names:
                ledgers.append(path)
            elif _is_furtag_sidecar(path, json_patterns):
                sidecars.append(path)
    return ledgers, sidecars


def _pdf_page_pattern(pdf: Path,
                      sidecar_patterns: Optional[List[str]] = None
                      ) -> "re.Pattern":
    """Regex matching this PDF's rendered page files (optionally their sidecars).

    When *sidecar_patterns* is given, the sidecar alternatives are derived from
    the very patterns ``convert_pdf`` writes with, so a txt/json/custom base
    sidecar is always recognized (and purged) rather than only ``.txt``.
    """
    page_re = rf"{re.escape(pdf.stem)} PAGE\d+"
    alts = [page_re + r"\.PNG"]
    for pattern in sidecar_patterns or []:
        if not pattern:
            continue
        try:
            alts.append(_sidecar_name_regex(pattern, page_re, r"\.PNG"))
        except re.error:
            continue
    return re.compile("^(?:%s)$" % "|".join(alts), re.I)


def _is_pdf_page_render(path: Path) -> bool:
    """True when *path* is a page PNG this tool rendered from a sibling PDF.

    A rendered page always lives at ``<dir>/<stem>/<stem> PAGEn.PNG`` beside
    ``<dir>/<stem>.pdf`` — the same test ``_pdf_render_candidates()`` uses to
    build ``pdf_page_dirs``. Anything else is an ordinary PNG and must not be
    given ``comic:``/``page:`` tags from its parent folder name.
    """
    if path.suffix.lower() != ".png":
        return False
    out_dir = path.parent
    for ext in PDF_EXTS:
        for candidate in (out_dir.name + ext, out_dir.name + ext.upper()):
            pdf = out_dir.parent / candidate
            if pdf.is_file() and _pdf_page_pattern(pdf).match(path.name):
                return True
    return False


def _pdf_render_candidates(root: Path) -> Tuple[List[Path], Set[Path]]:
    """Find only PNG pages whose names exactly match a sibling source PDF."""
    pages: List[Path] = []
    page_dirs: Set[Path] = set()
    pdfs: List[Path] = []
    for dp, dirs, files in os.walk(root, followlinks=False):
        _prune_hidden_walk_dirs(dirs)
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


def perform_nuke(root: Path, include_pdf_pages: bool = False,
                 settings: Optional[Settings] = None, *,
                 include_ledgers_reports: bool = True,
                 include_sidecars: bool = True,
                 ) -> Tuple[int, List[Tuple[Path, OSError]]]:
    """Delete FurTag-generated state under *root*.

    The single implementation behind both the CLI ``NUKE!`` prompt and the GUI
    Reset dialog, so the two can't drift. Each generated-data category can be
    selected independently. Returns ``(removed, failures)``; callers own how
    failures are reported.
    """
    ledgers, sidecars = _nuke_candidates(root, settings)
    pdf_pages, pdf_page_dirs = _pdf_render_candidates(root)

    candidates: List[Path] = []
    if include_ledgers_reports:
        candidates.extend(ledgers)
    if include_sidecars:
        candidates.extend(sidecars)
    if include_pdf_pages:
        candidates.extend(pdf_pages)

    removed = 0
    failures: List[Tuple[Path, OSError]] = []
    for path in candidates:
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


def _prompt_for_nuke(settings: Optional[Settings] = None) -> Optional[Path]:
    """Confirm and remove FurTag-generated state, returning the folder to scan.

    Blank input and cancellation return to the normal folder prompt. The
    filesystem root is deliberately refused even with confirmation.
    """
    print("\n💣 NUKE mode — choose which FurTag-generated data to remove, "
          "then rescan.")
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

    ledgers, sidecars = _nuke_candidates(root, settings)
    pdf_pages, _ = _pdf_render_candidates(root)
    print(f"\nTarget:   {root}")
    print(f"Ledgers/reports: {len(ledgers)}")
    print(f"Sidecars: {len(sidecars)}")
    print(f"Rendered PDF pages: {len(pdf_pages)}")

    def choose(prompt: str, *, default: bool) -> bool:
        suffix = "[Y/n]" if default else "[y/N]"
        try:
            answer = input(f"{prompt} {suffix}: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if not answer:
            return default
        return answer == "y"

    print("\nChoose what to remove:")
    remove_ledgers_reports = choose(
        f"  Remove {len(ledgers)} ledger/report file(s)?", default=True)
    remove_sidecars = choose(
        f"  Remove {len(sidecars)} sidecar file(s)?", default=True)
    remove_pdf_pages = choose(
        f"  Remove {len(pdf_pages)} rendered PDF page file(s)?", default=False)

    if not any((remove_ledgers_reports, remove_sidecars, remove_pdf_pages)):
        print("↩️  Nuke cancelled; no categories were selected.\n")
        return None

    try:
        answer = input("\nARE YOU SURE? [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "y":
        print("↩️  Nuke cancelled; nothing was deleted.\n")
        return None

    removed, failures = perform_nuke(
        root,
        include_pdf_pages=remove_pdf_pages,
        settings=settings,
        include_ledgers_reports=remove_ledgers_reports,
        include_sidecars=remove_sidecars,
    )
    for path, err in failures:
        print(f"⚠️  Could not delete {path}: {err}")
    print(f"\n💥 Reset complete — removed {removed} generated file(s).")
    if failures:
        print(f"⚠️  {len(failures)} file(s) could not be removed "
              "and may still be skipped.")
    print("🔄 Starting a fresh scan of that folder…\n")
    return root


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


def prompt_for_folder(settings: Optional[Settings] = None) -> Path:
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
            nuked_root = _prompt_for_nuke(settings)
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
        if ans in {"a", "y", "yes", "approve", "r", "n", "no", "reject"}:
            approve = ans in {"a", "y", "yes", "approve"}
            # A source failure here (rate limit, or a 401 that disabled the
            # source mid-run) raises rather than returning a clean miss. The
            # GUI already treats that as "leave it queued"; do the same here
            # instead of dying with a traceback and dropping the rest of the
            # queue on the floor.
            try:
                done = ti.resolve_pending_review(pending, approve=approve,
                                                 root=root)
            except RetryableLookupError as e:
                print(f"  ⚠️  Deferred, still queued – {e}")
                continue
            if not done:
                print("  ⚠️  Could not resolve.")
            elif approve:
                print("  ✅ Approved.")
            else:
                print("  ❌ Rejected (nomatch).")


def main() -> None:
    print("🐾 Unified Furry Tag Integrator for Hydrus 🐾")

    store = SettingsStore()
    settings = store.load()
    ti = TagIntegrator(settings=settings)
    ti.load_credentials_from_store(CredentialStore())
    print(f"📋 {ti.enabled_pipeline_description()}")
    print("⏭️  Skips files already tagged or logged in .furtag_ledger.json\n")

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

    if ti.has_hydrus:
        ti.hydrus_import_unmatched = (
            prompt_for_unmatched_import() if ti.hydrus_import else False)

    while True:
        root = prompt_for_folder(settings)
        opts = RunOptions.from_settings(ti.settings)
        opts.import_unmatched = ti.hydrus_import_unmatched
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
