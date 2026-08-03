"""Settings, run options, and persistent preferences for FurTag.

Three tiers (resolution order: RunOptions override → Settings → shipped default):

1. Secrets — OS keyring / FURTAG_* env vars (never stored here)
2. Preferences — settings.json via platformdirs
3. Per-run — RunOptions in memory for one scan
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# Shipped defaults used by both frontends.
# Keep these identical to furtag.py module constants so an untouched install
# matches the current CLI baseline.

DEFAULT_E621_INTERVAL = 1.0
DEFAULT_INKBUNNY_INTERVAL = 1.0
DEFAULT_DANBOORU_INTERVAL = 0.3
DEFAULT_GELBOORU_INTERVAL = 0.7
DEFAULT_FLUFFLE_INTERVAL = 1.2
DEFAULT_SAUCENAO_INTERVAL = 6.0

# Floors for advanced rate-limit editing (hard caps / documented limits).
PACE_FLOORS = {
    "e621": 0.5,       # hard cap 2/s
    "inkbunny": 0.5,
    "danbooru": 0.1,
    "gelbooru": 0.3,
    "fluffle": 1.0,    # one concurrent request per client
    "saucenao": 3.0,
}

DEFAULT_SAUCENAO_MIN_SIMILARITY = 80.0
DEFAULT_SAUCENAO_AUTH_SIMILARITY = 88.0
DEFAULT_FLUFFLE_TOSSUP_E621 = True
DEFAULT_PDF_DPI = 300
DEFAULT_PDF_ARCHIVAL_DPI = 600

SETTINGS_VERSION = 3
RECENT_SCAN_PATH_LIMIT = 12
APP_NAME = "FurTag"
APP_AUTHOR = "FurTag"
# The keyring service name lives in furtag_credentials.KEYRING_SERVICE — this
# module deliberately keeps no second copy of it.

DEFAULT_TAG_PATTERN = "{name}{ext}.txt"
DEFAULT_URL_PATTERN = "{name}{ext}.urls.txt"
DEFAULT_JSON_PATTERN = "{name}{ext}.json"

FLUFFLE_MATCH_CLASSES = ("exact", "tossUp", "alternative", "unlikely")
FLUFFLE_REVIEW_MODES = ("off", "tossups", "tossups_alternatives")
HYDRUS_PAGE_MODES = ("live", "end_of_run")


@dataclass
class OutputSettings:
    """Output sinks: Hydrus master toggle + sidecars."""
    hydrus_enabled: bool = True
    hydrus_import: bool = True
    hydrus_import_unmatched: bool = False
    hydrus_tag_service: str = "downloader tags"
    hydrus_tag_deleted_duplicates: bool = True
    # When Hydrus is active, also write sidecars (maps to hydrus_also_sidecars).
    # When Hydrus is off/unavailable, sidecars are always written if enabled.
    sidecars_enabled: bool = False
    sidecar_format: str = "txt"  # "txt" | "json"
    sidecar_tag_filename: str = DEFAULT_TAG_PATTERN
    sidecar_url_filename: str = DEFAULT_URL_PATTERN
    sidecar_json_filename: str = DEFAULT_JSON_PATTERN


@dataclass
class HydrusSettings:
    # Pull source descriptions from API responses FurTag already fetched and
    # write them straight to Hydrus notes. This is dramatically cheaper than
    # asking Hydrus to download and parse every matched post URL.
    direct_source_notes: bool = True
    # Send byte-exact booru Post URLs through Hydrus's URL downloader so its
    # installed parsers can add notes/descriptions/timestamps without importing
    # a second file. Kept as an optional legacy/extra-metadata path; direct
    # source notes make it unnecessary for descriptions.
    exact_url_enrichment: bool = False
    exact_url_enrichment_page_name: str = "FurTag Metadata"
    results_pages_enabled: bool = True
    new_imports_page_enabled: bool = True
    new_imports_page_name: str = "FurTag New Imports"
    new_imports_page_limit: int = 0
    new_imports_page_mode: str = "live"
    newly_tagged_page_enabled: bool = True
    newly_tagged_page_name: str = "FurTag Newly Tagged"
    newly_tagged_page_limit: int = 0
    newly_tagged_page_mode: str = "live"
    # Current duplicate-group members tagged on behalf of a deleted file.
    duplicate_tagged_page_enabled: bool = True
    duplicate_tagged_page_name: str = "FurTag Duplicate Tagged"
    duplicate_tagged_page_limit: int = 0
    duplicate_tagged_page_mode: str = "live"
    already_tagged_page_enabled: bool = False
    already_tagged_page_name: str = "Already Tagged"
    already_tagged_page_limit: int = 0
    # Shared cadence for live result pages. Zero wakes the publisher for every
    # accepted result; positive values allow arrivals to coalesce into batches.
    live_page_update_interval: int = 10
    # Non-secret identity for this Hydrus database binding. Rotate via an
    # explicit "new/replaced Hydrus database" action; never derived from the
    # access key. Combined with API origin into hydrus_scope_id().
    hydrus_profile_uuid: str = field(
        default_factory=lambda: str(uuid.uuid4()))


@dataclass
class SourceSettings:
    e621_enabled: bool = True
    inkbunny_enabled: bool = True
    danbooru_enabled: bool = True
    gelbooru_enabled: bool = True
    fluffle_enabled: bool = True
    saucenao_enabled: bool = True


@dataclass
class MatchingSettings:
    saucenao_min_similarity: float = DEFAULT_SAUCENAO_MIN_SIMILARITY
    saucenao_auth_similarity: float = DEFAULT_SAUCENAO_AUTH_SIMILARITY
    # Which Fluffle match classes count as automatic hits.
    fluffle_accepted_matches: List[str] = field(
        default_factory=lambda: ["exact"])
    fluffle_tossup_e621_only: bool = DEFAULT_FLUFFLE_TOSSUP_E621
    # off | tossups | tossups_alternatives
    fluffle_review_mode: str = "off"


@dataclass
class PdfSettings:
    pdf_enabled: bool = True
    pdf_dpi: int = DEFAULT_PDF_DPI
    pdf_write_sidecars: bool = True


@dataclass
class PerformanceSettings:
    e621_interval: float = DEFAULT_E621_INTERVAL
    inkbunny_interval: float = DEFAULT_INKBUNNY_INTERVAL
    danbooru_interval: float = DEFAULT_DANBOORU_INTERVAL
    gelbooru_interval: float = DEFAULT_GELBOORU_INTERVAL
    fluffle_interval: float = DEFAULT_FLUFFLE_INTERVAL
    saucenao_interval: float = DEFAULT_SAUCENAO_INTERVAL
    hash_worker_count: int = 0  # 0 = auto (len of enabled hash services)


@dataclass
class HistorySettings:
    """Non-secret local GUI state stored in the user config directory."""

    # Absolute paths, most-recent first. These never belong in the repository,
    # ledgers, sidecars, or logs produced beside scanned media.
    recent_scan_paths: List[str] = field(default_factory=list)


@dataclass
class Settings:
    """Tier-2 persistent preferences. Never stores secrets."""
    version: int = SETTINGS_VERSION
    output: OutputSettings = field(default_factory=OutputSettings)
    hydrus: HydrusSettings = field(default_factory=HydrusSettings)
    sources: SourceSettings = field(default_factory=SourceSettings)
    matching: MatchingSettings = field(default_factory=MatchingSettings)
    pdf: PdfSettings = field(default_factory=PdfSettings)
    performance: PerformanceSettings = field(default_factory=PerformanceSettings)
    history: HistorySettings = field(default_factory=HistorySettings)

    def clone(self) -> "Settings":
        return deepcopy(self)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Settings":
        """Forward-compatible loader: unknown keys ignored, missing → defaults."""
        if not isinstance(data, dict):
            return cls()
        base = cls()
        try:
            loaded_version = int(data.get("version") or 1)
        except (TypeError, ValueError):
            loaded_version = 1
        base.version = SETTINGS_VERSION
        base.output = _merge_dataclass(OutputSettings, data.get("output"))
        hydrus_data = data.get("hydrus")
        base.hydrus = _merge_dataclass(HydrusSettings, hydrus_data)
        # v1 used downloader scraping as the only description path and shipped
        # it enabled. v2 imports source descriptions directly, so migrate old
        # installs away from the unexpectedly slow per-URL downloader queue.
        if (loaded_version < 2 and isinstance(hydrus_data, dict)
                and "direct_source_notes" not in hydrus_data):
            base.hydrus.direct_source_notes = True
            base.hydrus.exact_url_enrichment = False
        # v2 had one limit shared by all result pages and a session-style
        # Already Tagged switch. Distribute those values into the independent
        # v3 page settings and restore the historical live publication mode.
        if loaded_version < 3 and isinstance(hydrus_data, dict):
            old_limit = hydrus_data.get("result_page_limit", 0)
            for prefix in ("new_imports", "newly_tagged", "duplicate_tagged",
                           "already_tagged"):
                setattr(base.hydrus, f"{prefix}_page_limit", old_limit)
            base.hydrus.already_tagged_page_enabled = bool(
                hydrus_data.get("build_already_tagged_page", False))
            for prefix in ("new_imports", "newly_tagged", "duplicate_tagged"):
                setattr(base.hydrus, f"{prefix}_page_mode", "live")
        base.sources = _merge_dataclass(SourceSettings, data.get("sources"))
        base.matching = _merge_dataclass(MatchingSettings, data.get("matching"))
        base.pdf = _merge_dataclass(PdfSettings, data.get("pdf"))
        base.performance = _merge_dataclass(
            PerformanceSettings, data.get("performance"))
        base.history = _merge_dataclass(HistorySettings, data.get("history"))
        _normalize_settings(base)
        return base


def _merge_dataclass(cls, data: Any):
    inst = cls()
    if not isinstance(data, dict):
        return inst
    known = {f.name for f in fields(cls)}
    for key, value in data.items():
        if key not in known:
            continue
        setattr(inst, key, value)
    return inst


def _normalize_settings(s: Settings) -> None:
    """Clamp / coerce values into valid ranges."""
    m = s.matching
    try:
        m.saucenao_min_similarity = float(m.saucenao_min_similarity)
    except (TypeError, ValueError):
        m.saucenao_min_similarity = DEFAULT_SAUCENAO_MIN_SIMILARITY
    try:
        m.saucenao_auth_similarity = float(m.saucenao_auth_similarity)
    except (TypeError, ValueError):
        m.saucenao_auth_similarity = DEFAULT_SAUCENAO_AUTH_SIMILARITY
    if m.saucenao_auth_similarity < m.saucenao_min_similarity:
        m.saucenao_auth_similarity = m.saucenao_min_similarity
    accepted = m.fluffle_accepted_matches
    if not isinstance(accepted, list):
        accepted = ["exact"]
    m.fluffle_accepted_matches = [
        c for c in accepted if c in FLUFFLE_MATCH_CLASSES] or ["exact"]
    if m.fluffle_review_mode not in FLUFFLE_REVIEW_MODES:
        m.fluffle_review_mode = "off"
    fmt = (s.output.sidecar_format or "txt").lower()
    s.output.sidecar_format = "json" if fmt == "json" else "txt"
    # Sidecar name patterns are user-editable text that later feeds str.format
    # and a filesystem path, so normalize them here — the single place every
    # consumer (CLI included, not just the GUI's preflight) passes through.
    s.output.sidecar_tag_filename = safe_sidecar_pattern(
        s.output.sidecar_tag_filename, DEFAULT_TAG_PATTERN, label="tag sidecar")
    s.output.sidecar_url_filename = safe_sidecar_pattern(
        s.output.sidecar_url_filename, DEFAULT_URL_PATTERN, label="URL sidecar")
    s.output.sidecar_json_filename = safe_sidecar_pattern(
        s.output.sidecar_json_filename, DEFAULT_JSON_PATTERN,
        for_json=True, label="JSON sidecar")
    try:
        s.pdf.pdf_dpi = max(72, min(2400, int(s.pdf.pdf_dpi)))
    except (TypeError, ValueError):
        s.pdf.pdf_dpi = DEFAULT_PDF_DPI
    for prefix in ("new_imports", "newly_tagged", "duplicate_tagged",
                   "already_tagged"):
        limit_attr = f"{prefix}_page_limit"
        try:
            setattr(s.hydrus, limit_attr,
                    max(0, int(getattr(s.hydrus, limit_attr))))
        except (TypeError, ValueError):
            setattr(s.hydrus, limit_attr, 0)
        name_attr = f"{prefix}_page_name"
        default_name = getattr(HydrusSettings(), name_attr)
        name = getattr(s.hydrus, name_attr)
        setattr(s.hydrus, name_attr,
                name.strip() if isinstance(name, str) and name.strip()
                else default_name)
    for prefix in ("new_imports", "newly_tagged", "duplicate_tagged"):
        mode_attr = f"{prefix}_page_mode"
        mode = getattr(s.hydrus, mode_attr)
        if mode not in HYDRUS_PAGE_MODES:
            setattr(s.hydrus, mode_attr, "live")
    try:
        s.hydrus.live_page_update_interval = max(
            0, min(60, int(s.hydrus.live_page_update_interval)))
    except (TypeError, ValueError):
        s.hydrus.live_page_update_interval = 10
    enrichment_page = s.hydrus.exact_url_enrichment_page_name
    if not isinstance(enrichment_page, str) or not enrichment_page.strip():
        enrichment_page = "FurTag Metadata"
    s.hydrus.exact_url_enrichment_page_name = enrichment_page.strip()
    # Pace floors
    for name, floor in PACE_FLOORS.items():
        attr = f"{name}_interval"
        try:
            val = float(getattr(s.performance, attr))
        except (TypeError, ValueError):
            val = floor
        setattr(s.performance, attr, max(floor, val))
    try:
        s.performance.hash_worker_count = max(
            0, int(s.performance.hash_worker_count))
    except (TypeError, ValueError):
        s.performance.hash_worker_count = 0
    s.history.recent_scan_paths = normalize_recent_scan_paths(
        s.history.recent_scan_paths)
    # Stable non-secret Hydrus DB binding; generate once if missing.
    profile = s.hydrus.hydrus_profile_uuid
    if not isinstance(profile, str) or not profile.strip():
        s.hydrus.hydrus_profile_uuid = str(uuid.uuid4())
    else:
        s.hydrus.hydrus_profile_uuid = profile.strip()


def normalize_hydrus_api_origin(api_url: str) -> str:
    """Canonical origin for scope IDs (scheme + host[:port], no path/key)."""
    raw = (api_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw.rstrip("/").lower()
    scheme = (parsed.scheme or "http").lower()
    host = (parsed.hostname or "").lower()
    if not host:
        return raw.rstrip("/").lower()
    port = parsed.port
    if port is None:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def hydrus_scope_id(profile_uuid: str, api_url: str) -> str:
    """Non-secret scope id for Hydrus deletion/sync checkpoints.

    Rotating profile_uuid (explicit “new Hydrus database”) or changing API
    origin invalidates scoped seals. Access keys never enter the digest.
    """
    material = f"{(profile_uuid or '').strip()}|{normalize_hydrus_api_origin(api_url)}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def normalize_recent_scan_paths(
        values: Any, limit: int = RECENT_SCAN_PATH_LIMIT) -> List[str]:
    """Return bounded, de-duplicated absolute paths, newest first.

    Existence is deliberately not required: folders on removable/network
    volumes should remain remembered while temporarily disconnected.
    """
    if not isinstance(values, (list, tuple)):
        return []
    try:
        limit = max(0, int(limit))
    except (TypeError, ValueError):
        limit = RECENT_SCAN_PATH_LIMIT
    if limit == 0:
        return []

    result: List[str] = []
    seen: set[str] = set()
    for raw in values:
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw.strip()).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            candidate = candidate.resolve(strict=False)
        except OSError:
            pass
        normalized = os.path.normpath(str(candidate))
        # normcase is a no-op on case-sensitive POSIX filesystems, where
        # /Art and /art may genuinely be different folders.
        key = os.path.normcase(normalized)
        if key in seen:
            continue
        seen.add(key)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def remember_scan_path(
        values: Any, path: Path | str,
        limit: int = RECENT_SCAN_PATH_LIMIT) -> List[str]:
    """Prepend one selected scan folder to persistent recent-path history."""
    candidate = Path(path).expanduser().resolve(strict=False)
    return normalize_recent_scan_paths(
        [str(candidate), *(values if isinstance(values, (list, tuple)) else [])],
        limit=limit)


@dataclass
class RunOptions:
    """Tier-3 per-run options, seeded from Settings then overridden by prompts/GUI."""
    import_unmatched: bool = False
    sync_sidecars: bool = False
    pdf_dpi: Optional[int] = None  # None → use Settings.pdf.pdf_dpi
    # Resolved PDF path → {"comic": str, "creator": str?} from pre-render prompt.
    # Empty / missing → engine uses PDF stem as comic, no creator.
    pdf_meta: Optional[Dict[str, Dict[str, str]]] = None
    # Optional per-run overrides of any Settings subsection (shallow replace).
    settings_override: Optional[Settings] = None

    @classmethod
    def from_settings(cls, settings: Settings) -> "RunOptions":
        return cls(
            import_unmatched=settings.output.hydrus_import_unmatched,
            sync_sidecars=False,
            # pdf_dpi / pdf_meta deliberately left None: the engine resolves them
            # (settings → interactive prompt). Pinning them here would make the
            # CLI's questions unreachable. Callers that must never block on
            # input() (the GUI) set them explicitly.
        )


@dataclass
class ScanSummary:
    tagged: int = 0
    unmatched: int = 0
    duplicates: int = 0
    pending_review: int = 0
    source_hits: Dict[str, int] = field(default_factory=dict)
    cancelled: bool = False
    total_items: int = 0

    def empty(self) -> bool:
        return (self.tagged == 0 and self.unmatched == 0 and
                self.duplicates == 0 and self.pending_review == 0 and
                self.total_items == 0)


# ── Sidecar filename patterns ────────────────────────────────────────────────

_PATH_SEP_RE = re.compile(r"[/\\]")


class SidecarPatternError(ValueError):
    """Raised when a sidecar filename pattern is invalid."""


def validate_sidecar_pattern(pattern: str, *, for_json: bool = False) -> str:
    """Validate a sidecar filename pattern.

    Rules:
    - Must be non-empty
    - No path separators
    - Must contain ``{ext}`` (avoids cat.jpg / cat.png collision)
    - Only ``{name}`` / ``{ext}`` placeholders (anything else would blow up
      ``str.format`` deep inside the write path)
    - Must not resolve to the media file itself (must have extra suffix beyond
      ``{name}{ext}``)
    - Must not walk out of the media directory (``..``)
    """
    if not pattern or not str(pattern).strip():
        raise SidecarPatternError("Sidecar filename pattern cannot be empty.")
    pattern = str(pattern).strip()
    if _PATH_SEP_RE.search(pattern):
        raise SidecarPatternError(
            "Sidecar filename pattern must not contain path separators.")
    if "{ext}" not in pattern:
        raise SidecarPatternError(
            "Sidecar filename pattern must contain {ext} so files that share a "
            "stem but differ by extension (cat.jpg / cat.png) do not collide.")
    # Reject patterns that equal the media file name itself.
    try:
        resolved = pattern.format(name="file", ext=".jpg")
    except (KeyError, IndexError, ValueError) as e:
        raise SidecarPatternError(
            "Sidecar filename pattern may only use the {name} and {ext} "
            f"placeholders ({e}).") from e
    if _PATH_SEP_RE.search(resolved) or resolved in {".", ".."} or \
            resolved.startswith("..") or resolved.endswith(".."):
        raise SidecarPatternError(
            "Sidecar filename pattern must stay inside the media folder "
            "(no '..' or path separators).")
    if resolved == "file.jpg":
        raise SidecarPatternError(
            "Sidecar filename pattern must not resolve to the media file itself.")
    if for_json and not resolved.lower().endswith(".json"):
        # Soft preference — still allow if user wants custom.
        pass
    return pattern


def render_sidecar_name(pattern: str, media: Path) -> str:
    """Render a sidecar filename for *media* from a validated pattern."""
    return pattern.format(name=media.stem, ext=media.suffix)


def safe_sidecar_pattern(pattern: Any, default: str, *,
                         for_json: bool = False, label: str = "sidecar") -> str:
    """Validated pattern, or *default* with a warning — never raises.

    Settings can be hand-edited, so an invalid pattern must degrade to the
    documented default instead of raising ``KeyError`` deep in the write path
    (or escaping the media folder via ``../``).
    """
    try:
        return validate_sidecar_pattern(pattern, for_json=for_json)
    except SidecarPatternError as e:
        print(f"⚠️  Invalid {label} filename pattern {pattern!r}: {e} "
              f"Using default {default!r}.", file=sys.stderr)
        return default


def resolve_settings_path(explicit: Optional[Path] = None) -> Path:
    """Return the platformdirs settings.json path (or *explicit*)."""
    if explicit is not None:
        return Path(explicit)
    try:
        from platformdirs import user_config_dir
        base = Path(user_config_dir(APP_NAME, APP_AUTHOR))
    except ImportError:
        base = Path.home() / ".config" / "FurTag"
    return base / "settings.json"


def atomic_write_text(path: Path, text: str) -> None:
    """Write *text* to *path* via a sibling ``.tmp`` + replace.

    The one implementation of FurTag's crash-safe write, so an interrupted run
    never leaves a half-written ledger/queue/report. Raises OSError; callers
    decide whether that is fatal, warned, or ignored.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class SettingsStore:
    """Load/save settings.json. Never stores secrets."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = resolve_settings_path(path)

    def load(self) -> Settings:
        if not self.path.exists():
            settings = Settings()
            # The Hydrus profile UUID is a durable database identity, not a
            # per-process nonce. Persist the defaults immediately so two runs
            # before the user opens Preferences cannot mint different scopes.
            try:
                self.save(settings)
            except OSError as e:
                print(
                    f"⚠️  Couldn't persist default settings {self.path} ({e}).",
                    file=sys.stderr)
            return settings
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            return Settings()
        raw = data if isinstance(data, dict) else {}
        hydrus_data = raw.get("hydrus")
        missing_profile = not (
            isinstance(hydrus_data, dict)
            and isinstance(hydrus_data.get("hydrus_profile_uuid"), str)
            and hydrus_data["hydrus_profile_uuid"].strip())
        settings = Settings.from_dict(raw)
        if missing_profile:
            try:
                self.save(settings)
            except OSError as e:
                print(
                    f"⚠️  Couldn't persist Hydrus database identity "
                    f"to {self.path} ({e}).",
                    file=sys.stderr)
        return settings

    def save(self, settings: Settings) -> None:
        payload = settings.to_dict()
        payload["version"] = SETTINGS_VERSION
        atomic_write_text(
            self.path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def validate_output_patterns(output: OutputSettings) -> None:
    """Validate whichever sidecar name patterns the active format actually uses.

    One definition, so the GUI's save-time check and the pre-scan preflight
    can't disagree about what counts as a valid pattern set.
    """
    if output.sidecar_format == "json":
        validate_sidecar_pattern(output.sidecar_json_filename, for_json=True)
    else:
        validate_sidecar_pattern(output.sidecar_tag_filename)
        validate_sidecar_pattern(output.sidecar_url_filename)


def validate_run_preflight(
        settings: Settings,
        *,
        hydrus_available: bool,
        any_source_available: bool,
) -> List[str]:
    """Return human-readable blocking errors before a scan starts."""
    errors: List[str] = []
    hydrus_on = bool(settings.output.hydrus_enabled and hydrus_available)
    # Classic sink rules (matches TagIntegrator.write_sidecars):
    #   · Hydrus active → sidecars only if sidecars_enabled
    #   · Hydrus inactive/unavailable → always write sidecars (fallback)
    # Refuse only when the user explicitly disables BOTH master Hydrus and
    # sidecars — that combination has nowhere to put results.
    if not settings.output.hydrus_enabled and not settings.output.sidecars_enabled:
        errors.append(
            "No output sink: Hydrus and sidecars are both disabled. "
            "Enable at least one before starting a scan.")
    # If Hydrus is on and available but sidecars off, results still go to Hydrus
    # — fine. If Hydrus is wanted but unavailable, classic fallback writes
    # sidecars regardless of sidecars_enabled (see write_sidecars property).

    # Sources
    src = settings.sources
    any_enabled = any((
        src.e621_enabled, src.inkbunny_enabled, src.danbooru_enabled,
        src.gelbooru_enabled, src.fluffle_enabled, src.saucenao_enabled,
    ))
    if not any_enabled:
        errors.append(
            "All sources are disabled. Enable at least one search source.")
    elif not any_source_available:
        # Credentials missing — Fluffle still works without keys.
        if not src.fluffle_enabled:
            errors.append(
                "No available sources have credentials, and Fluffle is disabled.")

    if settings.matching.saucenao_auth_similarity < settings.matching.saucenao_min_similarity:
        errors.append(
            "SauceNAO auth similarity must be ≥ min similarity.")

    try:
        validate_output_patterns(settings.output)
    except SidecarPatternError as e:
        errors.append(str(e))

    return errors


def effective_settings(base: Settings, options: Optional[RunOptions]) -> Settings:
    """Apply optional RunOptions.settings_override onto a clone of *base*."""
    if options is None or options.settings_override is None:
        return base
    return options.settings_override.clone()
