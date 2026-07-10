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

Session ledger (.furtag_ledger.json in the scanned root, keyed by relative
path + size + mtime): records every file as "matched" or "nomatch" so re-runs
skip work already done — without needing to re-hash or re-query anything. A file
is only re-checked if it was edited/replaced (size or mtime changed).

Output (Hydrus-compatible sidecars):
    - <file>.<ext>.txt        → tags (one per line)
    - <file>.<ext>.urls.txt   → source URLs (one per line)

Python 3.7+ compatible.

Dependencies:
    pip install pillow requests regex

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

    Note: Danbooru requires a verified-email account for API auth; if the key
    is rejected (403) the script falls back to anonymous Danbooru access.
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
PDF_DPI    = 300            # render resolution for PDF pages (see pdf_to_pages)

CREDENTIALS_FILE = "credentials.txt"
LEDGER_FILE      = ".furtag_ledger.json"

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
    _SEP = "  " + "═" * 60

    def __init__(self) -> None:
        self.tracks: Dict[str, _Track] = {k: _Track() for k in self._TRACK_ORDER}
        self._drawn = 0
        self._lock = threading.Lock()
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
                     growing: bool = False) -> None:
        with self._lock:
            t = self.tracks[track]
            t.phase, t.total, t.done, t.idx = label, total, 0, 0
            t.prev, t.sub = ("—", ""), ""
            t.start = time.monotonic()
            t.growing = growing
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
        """Print a message above the live panel (warnings, errors), then redraw."""
        with self._lock:
            if self.tty and self._drawn:
                sys.stdout.write(f"\033[{self._drawn}A\033[J")  # to panel top, clear below
                self._drawn = 0
            print(msg)
            if self.tty and any(t.idx for t in self.tracks.values()):
                self._render()

    def close(self) -> None:
        with self._lock:
            if self.tty and self._drawn:
                sys.stdout.write("\n")
                sys.stdout.flush()
                self._drawn = 0

    def _render_track(self, t: _Track) -> List[str]:
        elapsed = time.monotonic() - t.start
        rate = t.done / elapsed if (elapsed > 0 and t.done > 0) else 0
        if t.growing or rate <= 0:
            eta = "…"
        else:
            eta = self._fmt((t.total - t.done) / rate)
        width = 28
        filled = int(width * t.done / t.total) if t.total else 0
        bar = "█" * filled + "░" * (width - filled)
        pname, presult = t.prev

        return [
            f"  ✓ prev:    {self._trim(pname)}   {presult}",
            f"  ▶ current: {self._trim(t.current)}   {t.sub}",
            f"    next:    {self._trim(t.nxt)}",
            f"  {t.phase}",
            f"  [{bar}] {t.done}/{t.total}   ⏱ {self._fmt(elapsed)} · ETA {eta}",
        ]

    def _render(self) -> None:
        """Draw both tracks' panels stacked, framed and separated by rules so the
        two are easy to tell apart. Caller must hold self._lock."""
        lines: List[str] = [self._SEP]
        for i, key in enumerate(self._TRACK_ORDER):
            lines += self._render_track(self.tracks[key])
            lines.append(self._SEP)          # rule after each block
        out = (f"\033[{self._drawn}A" if self._drawn else "")
        out += "".join("\033[2K" + ln + "\n" for ln in lines)
        sys.stdout.write(out)
        sys.stdout.flush()
        self._drawn = len(lines)


# Active display, if any. notify() routes warnings around the live panel.
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
    md5: Optional[str] = None
    perceptual_only: bool = False   # PDF-derived page: skip hash tier, go perceptual


# ── Session ledger ───────────────────────────────────────────────────────────

class Ledger:
    """Per-root JSON record of every file already processed, keyed by relative
    path with a (size, mtime) fingerprint. Lets a re-run rule a file out BEFORE
    hashing or querying it. A file is re-checked only if its size or mtime
    changed (i.e. it was edited/replaced)."""

    MTIME_EPS = 1e-3

    def __init__(self, root: Path) -> None:
        self.path = root / LEDGER_FILE
        self.records: Dict[str, Dict] = {}
        self._dirty = 0
        self._lock = threading.Lock()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("records"), dict):
                self.records = data["records"]
        except Exception as e:
            notify(f"⚠️  Couldn't read ledger ({e}); starting fresh.")

    def status_for(self, item: FileItem) -> Optional[str]:
        """'matched' / 'nomatch' if this exact file was already processed, else None."""
        rec = self.records.get(item.relpath)
        if not rec or rec.get("size") != item.size:
            return None
        try:
            if abs(float(rec.get("mtime", -1)) - item.mtime) > self.MTIME_EPS:
                return None
        except (TypeError, ValueError):
            return None
        return rec.get("status")

    def record(self, item: FileItem, status: str, sources: List[str]) -> None:
        with self._lock:
            self.records[item.relpath] = {
                "size": item.size,
                "mtime": round(item.mtime, 3),
                "md5": item.md5,
                "status": status,
                "sources": sources,
            }
            self._dirty += 1

    def maybe_save(self, every: int = 25) -> None:
        if self._dirty >= every:
            self.save()

    def save(self) -> None:
        with self._lock:
            if self._dirty == 0 and self.path.exists():
                return
            try:
                tmp = self.path.with_name(self.path.name + ".tmp")
                tmp.write_text(
                    json.dumps({"version": 1, "records": self.records},
                               ensure_ascii=False, indent=0),
                    encoding="utf-8")
                tmp.replace(self.path)   # atomic
                self._dirty = 0
            except Exception as e:
                notify(f"⚠️  Couldn't write ledger: {e}")


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
    def _md5_local(fp: Path) -> Optional[str]:
        h = hashlib.md5()
        try:
            with fp.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except Exception as e:
            notify(f"❌ MD5 failed on {fp.name}: {e}")
            return None

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

    def write_results(self, media: Path, tags: Set[str], urls: Set[str]) -> None:
        # Drop "artist unknown / anonymous" placeholder tags from every source
        # before writing — they're noise in a Hydrus library.
        tags = {t for t in tags if not _is_junk_tag(t)}
        if tags:
            self._append_lines(self.tag_sidecar_path(media), tags)
        if urls:
            self._append_lines(self.url_sidecar_path(media), urls)

    # ── PDF pre-pass ───────────────────────────────────────────────────────────

    def expand_pdfs(self, root: Path) -> Set[Path]:
        """Render every PDF under `root` to per-page PNGs *before* indexing, so
        the pages are tagged like any other image. Reuses `pdf_to_pages` and
        returns the set of page-folder paths it produced/owns; `index()` routes
        PNGs living in those folders to perceptual-only (a re-rendered page never
        MD5-matches a booru, so the hash tier is pure waste on them).

        Already-rendered PDFs are skipped, so a re-run doesn't churn the pages
        (which would bump their mtime and defeat the ledger). Missing PyMuPDF is
        non-fatal — PDFs are simply left untouched, like a missing credential."""
        pdfs: List[Path] = []
        for dp, dirs, files in os.walk(root):
            dirs.sort()
            for fn in sorted(files):
                if fn.startswith("."):
                    continue
                if Path(fn).suffix.lower() in PDF_EXTS:
                    pdfs.append(Path(dp) / fn)
        if not pdfs:
            return set()

        try:
            from pdf_to_pages import convert_pdf
        except Exception as e:                  # PyMuPDF missing / import error
            print(f"⚠️  {len(pdfs)} PDF(s) found but PDF support is unavailable "
                  f"({e}). Install PyMuPDF to tag them; skipping for now.")
            return set()

        page_dirs: Set[Path] = set()
        print(f"📄 {len(pdfs)} PDF(s) → rendering pages (once each)…")
        for pdf in pdfs:
            out_dir = pdf.parent / pdf.stem
            page_dirs.add(out_dir)
            already = out_dir.is_dir() and any(
                f.suffix.lower() == ".png" for f in out_dir.iterdir())
            if already:
                continue                        # rendered on a previous run
            try:
                convert_pdf(pdf, pdf.parent, PDF_DPI)
            except Exception as e:
                print(f"  ! Failed to render {pdf.name}: {e}")
        return page_dirs

    # ── Index ────────────────────────────────────────────────────────────────

    def index(self, root: Path, ledger: Ledger,
              pdf_page_dirs: Set[Path]) -> List[FileItem]:
        """Walk the tree once and return the files that actually need work,
        videos first. Skips dotfiles/._ metadata, non-media, already-tagged, and
        ledger-recorded (matched or no-match, unchanged) files. PNGs inside a
        `pdf_page_dirs` folder are flagged perceptual-only and are exempt from
        the has-sidecar skip (their sidecar holds only the base comic:/page:
        tags) — the ledger alone rules them out on a re-run."""
        print("📂 Scanning folder tree…")
        media = tagged = seen = 0
        items: List[FileItem] = []

        for dp, dirs, files in os.walk(root):
            dirs.sort()
            for fn in sorted(files):
                if fn.startswith("."):          # dotfiles + macOS ._ metadata
                    continue
                p = Path(dp) / fn
                ext = p.suffix.lower()
                if ext in IMG_EXTS:
                    kind = "image"
                elif ext in VIDEO_EXTS:
                    kind = "video"
                else:
                    continue                    # not postable media → ignore
                media += 1

                is_pdf_page = ext == ".png" and p.parent in pdf_page_dirs
                if self.has_sidecar(p) and not is_pdf_page:
                    tagged += 1
                    continue
                try:
                    stat = p.stat()
                except OSError:
                    continue

                item = FileItem(path=p, relpath=str(p.relative_to(root)),
                                size=stat.st_size, mtime=stat.st_mtime, kind=kind,
                                perceptual_only=is_pdf_page)
                if ledger.status_for(item) in ("matched", "nomatch"):
                    seen += 1
                    continue
                items.append(item)

        # Videos first (can't reverse-image-search; rarely hash-match), then
        # images; each group in natural path order (PAGE2 before PAGE10) for
        # stable, resumable runs.
        items.sort(key=lambda it: (0 if it.kind == "video" else 1,
                                   _natural_key(it.relpath)))

        print(f"📊 {media} media files · {tagged} already tagged · "
              f"{seen} previously checked · {len(items)} to process")
        return items

    # ── Parallel local hashing ───────────────────────────────────────────────

    def hash_all(self, items: List[FileItem]) -> None:
        # PDF pages skip the hash tier entirely, so don't waste I/O hashing them.
        todo = [it for it in items if it.md5 is None and not it.perceptual_only]
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

        ledger = Ledger(root)
        ledger.load()
        pdf_page_dirs = self.expand_pdfs(root)
        items = self.index(root, ledger, pdf_page_dirs)
        if not items:
            print("✅ Nothing to do — everything is tagged or already checked.")
            return
        self.hash_all(items)

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
                        self.write_results(item.path, tags, urls)
                        ledger.record(item, "matched", sources)
                        _bump_hit(sources)
                        disp.finish_file(
                            "perceptual", f"{'+'.join(sources)}  ({len(tags)} tags)")
                    else:
                        ledger.record(item, "nomatch", [])
                        _bump_miss()
                        disp.finish_file("perceptual", "— no match")
                    ledger.maybe_save()
                except Exception as e:
                    notify(f"❌ perceptual worker error on {item.path.name}: {e}")
                finally:
                    perceptual_q.task_done()

        try:
            disp.begin_phase(
                "hash", "Phase · hash lookups (e621·InkBunny·Danbooru·Gelbooru)",
                len(hash_items))
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
                        ledger.record(item, "matched", sources)
                        _bump_hit(sources)
                        disp.finish_file("hash", f"{'+'.join(sources)}  ({len(tags)} tags)")
                    elif item.kind == "image":
                        perceptual_q.put(item)
                        disp.grow("perceptual")
                        disp.finish_file("hash", "no hash match → perceptual")
                    else:                                  # video: hash-only
                        ledger.record(item, "nomatch", [])
                        _bump_miss()
                        disp.finish_file("hash", "— no match")
                    ledger.maybe_save()

            disp.freeze_total("perceptual")
            perceptual_q.put(_PERCEPTUAL_DONE)
            perc_thread.join()
        finally:
            disp.close()
            _display = None
            ledger.save()

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
        print(f"🗒️  Session ledger updated: {ledger.path.name}")


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


def prompt_for_folder() -> Path:
    """Ask for a folder, re-prompting until a real directory is given.

    Blank defaults to the current directory. Typing q/quit/exit — or Ctrl+C /
    Ctrl+D — quits cleanly. An invalid path re-prompts instead of exiting.
    """
    while True:
        try:
            raw = input("Folder to scan (blank = current dir, q to quit): ").strip()
        except (EOFError, KeyboardInterrupt):
            sys.exit("\n👋 Bye.")
        if raw.lower() in QUIT_WORDS:
            sys.exit("👋 Bye.")
        root = Path(_unescape_path(raw) if raw else ".").expanduser().resolve()
        if root.is_dir():
            return root
        print(f"‼️  '{root}' is not a valid directory. Try again (q to quit).\n")


def main() -> None:
    print("🐾 Unified Furry Tag Integrator for Hydrus 🐾")
    print("📋 e621 + InkBunny + Danbooru + Gelbooru MD5 (concurrent) → Fluffle → SauceNAO")
    print("📝 Tags → <file>.<ext>.txt   |   URLs → <file>.<ext>.urls.txt")
    print("⏭️  Skips files already tagged or logged in .furtag_ledger.json\n")

    root = prompt_for_folder()

    ti = TagIntegrator()
    ti.load_credentials(Path(__file__).with_name(CREDENTIALS_FILE))

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
