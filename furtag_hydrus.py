"""Hydrus Client API sink for FurTag.

Owns connection setup, file import/tagging, URL routing (associate vs
metadata enrichment), result pages, and resumable sidecar reconciliation.
Mixed into :class:`furtag.TagIntegrator` so orchestration stays in one place
while this file remains the canonical home for Hydrus concerns.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, TYPE_CHECKING,
)

import requests

from furtag_urls import UrlWritePolicy, is_enrichable_post_url, partition_urls

if TYPE_CHECKING:
    from furtag import FileItem, Ledger, LedgerManager

# Keep batch sizes next to the API that consumes them.
HYDRUS_HASH_LOOKUP_BATCH = 256  # well below the Client API's 2 MB GET limit
HYDRUS_PAGE_BATCH = 256         # hashes per manage_pages call
HYDRUS_RELATIONSHIP_DUPLICATES = "8"  # Hydrus duplicate-status enum; "3" = alternates


@dataclass
class HydrusResultPageState:
    """Persistent page configuration plus state owned by one scan.

    The small mapping compatibility shim is intentionally temporary-friendly:
    older callers that only inspect ``page["hashes"]`` keep working, while the
    engine itself gets typed fields and never shares keys/queues between runs.
    """

    kind: str
    name: str = ""
    configured_enabled: bool = True
    enabled: bool = False
    mode: str = "live"
    limit: int = 0
    hashes: List[str] = field(default_factory=list)
    pending: List[str] = field(default_factory=list)
    seen: Set[str] = field(default_factory=set)
    page_key: Optional[str] = None
    failed: bool = False

    def reset(self, *, enabled: bool) -> None:
        self.enabled = enabled
        self.hashes.clear()
        self.pending.clear()
        self.seen.clear()
        self.page_key = None
        self.failed = False

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def __setitem__(self, key: str, value: Any) -> None:
        setattr(self, key, value)

    def update(self, values: Dict[str, Any]) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class HydrusImportState(str, Enum):
    """What happened to the import of this content (axis A)."""

    LIVE = "live"
    PREVIOUSLY_DELETED = "previously_deleted"
    VETOED = "vetoed"
    RETRYABLE_FAILURE = "retryable_failure"
    NOT_REQUESTED = "not_requested"


class HydrusMetadataState(str, Enum):
    """What happened to tags/URLs/notes (axis B). Independent of import."""

    APPLIED_ORIGINAL = "applied_original"
    APPLIED_DUPLICATES = "applied_duplicates"
    NO_DUPLICATE_TARGETS = "no_duplicate_targets"
    POLICY_SKIPPED = "policy_skipped"
    PERMISSION_MISSING = "permission_missing"
    NOT_REQUESTED = "not_requested"
    RETRYABLE_FAILURE = "retryable_failure"


@dataclass(frozen=True)
class HydrusPushResult:
    """Two-axis outcome of one Hydrus import + metadata push attempt.

    Import and metadata states are independent so a previously-deleted
    unmatched file (no tags) can finish without a relationship lookup, while
    a match with missing relationship permission stays incomplete without a
    false permanent seal.
    """

    sha256: Optional[str] = None
    import_state: HydrusImportState = HydrusImportState.RETRYABLE_FAILURE
    metadata_state: HydrusMetadataState = HydrusMetadataState.RETRYABLE_FAILURE
    target_hashes: Tuple[str, ...] = ()
    scope_id: Optional[str] = None
    policy_hash: Optional[str] = None
    reason: Optional[str] = None

    @property
    def complete(self) -> bool:
        """Whether every *requested* import/metadata axis finished terminally."""
        if self.import_state == HydrusImportState.RETRYABLE_FAILURE:
            return False
        if self.metadata_state in (
                HydrusMetadataState.PERMISSION_MISSING,
                HydrusMetadataState.RETRYABLE_FAILURE):
            return False
        return True

    @property
    def hydrus_deleted(self) -> bool:
        """Compat: durable no-target deleted outcome only (not permission/policy).

        New call sites should switch on ``import_state`` / ``metadata_state``.
        """
        return (
            self.import_state == HydrusImportState.PREVIOUSLY_DELETED
            and self.metadata_state == HydrusMetadataState.NO_DUPLICATE_TARGETS
            and self.complete
        )

    def to_ledger_checkpoint(self) -> Dict[str, Any]:
        """Nested hydrus_output / unmatched_import checkpoint (no secrets)."""
        return {
            "scope_id": self.scope_id,
            "import_state": self.import_state.value,
            "metadata_state": self.metadata_state.value,
            "sha256": self.sha256,
            "target_hashes": list(self.target_hashes),
            "policy_hash": self.policy_hash,
            "reason": self.reason,
            "complete": self.complete,
            "updated_at": time.time(),
        }

    def __iter__(self):
        """Two-value unpacking: ``sha256, complete = result``."""
        yield self.sha256
        yield self.complete


@dataclass(frozen=True)
class HydrusAddFileResult:
    """Raw add_file outcome retained until it is mapped to typed state."""

    sha256: Optional[str]
    status: int
    note: str = ""


@dataclass
class PriorNomatchReconcileResult:
    """Outcome summary for the pre-scan unmatched-import reconciliation."""

    completed_paths: Set[Path] = field(default_factory=set)
    live: int = 0
    previously_deleted: int = 0
    vetoed: int = 0
    other_terminal: int = 0
    failed: int = 0

    @property
    def completed(self) -> int:
        return len(self.completed_paths)


@dataclass
class PriorMatchReconcileResult:
    """Selective retry summary for matched rows with pending Hydrus output."""

    attempted_paths: Set[Path] = field(default_factory=set)
    completed_paths: Set[Path] = field(default_factory=set)
    failed: int = 0
    missing_payload: int = 0

    @property
    def attempted(self) -> int:
        return len(self.attempted_paths)

    @property
    def completed(self) -> int:
        return len(self.completed_paths)


def _truthy(val: str, default: bool = False) -> bool:
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _notify(message: str, *, severity: str = "warning") -> None:
    """Forward to furtag.notify without importing it at module load time."""
    from furtag import notify
    notify(message, severity=severity)


def _notify_info(message: str) -> None:
    """Success/audit lines — do not pollute the issue stream (BF-12)."""
    _notify(message, severity="info")


class HydrusMixin:
    """Hydrus Client API operations. Expects TagIntegrator host attributes.

    Host must provide: session, settings, cancel_event / cancelled(), notify
    via the module-level :func:`furtag.notify`, hashing helpers, sidecar
    readers, ledger types, and the Hydrus configuration attributes set in
    ``TagIntegrator.__init__`` / ``apply_settings`` / credential load.
    """

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
        page_requested = bool(self.settings.hydrus.results_pages_enabled)

        try:
            r = self.session.get(
                f"{self.hydrus_api_url}/verify_access_key",
                headers=self._hydrus_headers(),
                timeout=10,
            )
            if r.status_code != 200:
                _notify(f"‼️  Hydrus API rejected access key (HTTP {r.status_code}) – "
                       f"sidecars only.")
                return
            access = r.json()
            permissions = access.get("basic_permissions") or []
            everything = access.get("permits_everything", False)
            can_manage_pages = everything or 4 in permissions
            self.hydrus_can_manage_pages = can_manage_pages
            # Hydrus permission 0 = "Import and Edit URLs"; associate_url 403s
            # without it, so know up front rather than failing per file.
            self.hydrus_can_edit_urls = everything or 0 in permissions
            # Permission 7 = "Edit File Notes". Source descriptions can be
            # written directly once add_file gives us the canonical SHA-256.
            self.hydrus_can_edit_notes = everything or 7 in permissions
            # Permission 3 lets us batch-check local MD5s and skip redundant
            # add_file calls for files Hydrus already has.
            self.hydrus_can_search_files = everything or 3 in permissions
            # Permission 8 permits querying a deleted file's *current* exact
            # duplicate-group members. It is deliberately separate from normal
            # file searching in Hydrus.
            self.hydrus_can_manage_relationships = everything or 8 in permissions
            if not self.hydrus_can_edit_urls:
                _notify("⚠️  Hydrus URLs disabled – access key needs the "
                       "'Import and Edit URLs' permission; tags still work.")
            if (getattr(self, "hydrus_direct_notes_enabled", True)
                    and not self.hydrus_can_edit_notes):
                _notify("⚠️  Hydrus direct notes disabled – access key needs "
                       "'Add Notes / Edit File Notes'; legacy URL enrichment "
                       "still works.")
            if not self.hydrus_can_search_files:
                _notify("⚠️  Hydrus hash cache disabled – access key needs "
                       "'Search for and Fetch Files'; imports still work.")
            if (self.hydrus_tag_deleted_duplicates and
                    not self.hydrus_can_manage_relationships):
                _notify("⚠️  Deleted-file duplicate tagging disabled – access key needs "
                       "'Manage File Relationships'.")
            for page in self.hydrus_result_pages.values():
                page.enabled = (
                    page_requested and page.configured_enabled
                    and can_manage_pages)
            self.hydrus_already_tagged_page_enabled = (
                page_requested
                and self.settings.hydrus.already_tagged_page_enabled
                and can_manage_pages)
            if page_requested and not can_manage_pages:
                _notify("⚠️  Hydrus pages disabled – access key needs Manage Pages permission.")
            svc_key = self._hydrus_resolve_tag_service(self.hydrus_tag_service_name)
            if not svc_key:
                _notify(f"‼️  Hydrus tag service '{self.hydrus_tag_service_name}' not found – "
                       f"sidecars only.")
                return
            self.hydrus_tag_service_key = svc_key
            self.has_hydrus = True
            print(f"✅ Hydrus Client API → {self.hydrus_api_url}  "
                  f"[{self.hydrus_tag_service_name}]  ({self.hydrus_mode_desc()})")
        except requests.RequestException as e:
            _notify(f"‼️  Hydrus API unreachable ({e}) – sidecars only. "
                   f"Is the client running with the API enabled?")


    def hydrus_mode_desc(self) -> str:
        """e.g. "import+tag" / "tag-only + sidecars" — used in startup banners."""
        mode = "import+tag" if self.hydrus_import else "tag-only"
        if (getattr(self, "hydrus_direct_notes_enabled", True)
                and self.hydrus_can_edit_notes):
            mode += " + direct notes"
        if (self.hydrus_exact_url_enrichment
                and self.hydrus_can_edit_urls):
            mode += " + exact-URL enrichment"
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
                _notify("⚠️  Hydrus MD5 cache unavailable; using normal imports "
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


    def _hydrus_current_sha256s(
            self, hashes: Iterable[str]) -> Optional[Set[str]]:
        """Return requested SHA-256s that are current local Hydrus files.

        ``None`` means the lookup is unavailable and callers must retain their
        normal import behavior. An empty set is a successful lookup with no
        current matches. Hydrus remembers hashes for deleted files, so this
        deliberately uses ``search_files`` rather than identifier metadata.
        """
        if not (self.has_hydrus and self.hydrus_can_search_files):
            return None
        wanted = sorted({
            value.lower() for value in hashes
            if isinstance(value, str)
            and re.fullmatch(r"[0-9a-fA-F]{64}", value)
        })
        if not wanted:
            return set()

        current: Set[str] = set()
        for offset in range(0, len(wanted), HYDRUS_HASH_LOOKUP_BATCH):
            batch = wanted[offset:offset + HYDRUS_HASH_LOOKUP_BATCH]
            try:
                r = self.session.get(
                    f"{self.hydrus_api_url}/get_files/search_files",
                    headers=self._hydrus_headers(),
                    params={
                        "tags": json.dumps([
                            "system:hash = " + " ".join(batch) + " sha256"
                        ]),
                        "return_hashes": "true",
                        "return_file_ids": "false",
                    },
                    timeout=30,
                )
                if r.status_code != 200:
                    raise RuntimeError(
                        f"search_files HTTP {r.status_code}: {r.text[:200]}")
                returned = r.json().get("hashes") or []
                current.update(
                    value.lower() for value in returned
                    if isinstance(value, str)
                    and re.fullmatch(r"[0-9a-fA-F]{64}", value)
                )
            except (requests.RequestException, ValueError, RuntimeError) as e:
                self.hydrus_can_search_files = False
                _notify("⚠️  Hydrus sidecar resume check unavailable; using "
                       f"normal imports for this run ({e}).")
                return None
        return current


    def _sidecar_sync_signature(
            self, tags: Set[str], urls: Set[str]) -> str:
        """Stable digest of normalized payload plus its Hydrus destination."""
        payload = {
            "hydrus_api_url": self.hydrus_api_url.rstrip("/"),
            "hydrus_tag_service_key": self.hydrus_tag_service_key,
            "tags": sorted(tags),
            "urls": sorted(urls),
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


    @staticmethod
    def _sidecar_sync_disposition(push: HydrusPushResult) -> Optional[str]:
        """Map two-axis push outcome to a durable sidecar-sync disposition."""
        if not push.complete:
            return None
        meta = push.metadata_state
        if meta == HydrusMetadataState.APPLIED_ORIGINAL:
            return "live"
        if meta == HydrusMetadataState.APPLIED_DUPLICATES:
            return "deleted_tagged_duplicates"
        if meta == HydrusMetadataState.NO_DUPLICATE_TARGETS:
            return "deleted_no_duplicates"
        if meta == HydrusMetadataState.POLICY_SKIPPED:
            return "deleted_policy_skipped"
        if meta == HydrusMetadataState.NOT_REQUESTED:
            # Empty metadata or import-only path.
            if push.import_state == HydrusImportState.PREVIOUSLY_DELETED:
                return "deleted_no_duplicates"
            if push.import_state == HydrusImportState.LIVE:
                return "live"
            if push.import_state == HydrusImportState.NOT_REQUESTED:
                return "live"
        return None

    def sync_sidecars_to_hydrus(self, root: Path) -> Tuple[int, int]:
        """Push existing FurTag sidecars to Hydrus with resumable checkpoints.

        This is a migration/reconciliation pass: tag sidecars (txt or JSON)
        supply tags and URL sidecars supply source URLs. It deliberately does
        no booru lookup. Successful *and* terminal deleted outcomes are
        recorded as independent ``sidecar_sync`` metadata (BF-07) so the same
        dead content is not re-imported forever under the same Hydrus scope.

        Candidates are prepared in small batches. When the access key can
        search files, their local SHA-256s are checked against Hydrus first so
        files already present are tagged directly instead of being mirrored,
        hashed, and re-imported through ``add_files/add_file`` on every retry.
        """
        if not self.has_hydrus:
            return 0, 0
        # BF-13: stream candidates in Hydrus-sized batches instead of retaining
        # every path for a huge library. Total is unknown until the walk ends.
        attempted = successful = skipped = terminal_skipped = failed = 0
        discovered = 0
        scope_id = self._hydrus_scope_id()
        tag_deleted = bool(getattr(self, "hydrus_tag_deleted_duplicates", True))
        # Late import: furtag loads this mixin at import time.
        from furtag import LedgerManager, _is_junk_tag
        ledger_mgr = LedgerManager()
        prune = getattr(self, "prune_walk_dirs", None)

        def iter_batches() -> Iterable[List[Path]]:
            nonlocal discovered
            batch: List[Path] = []
            for dp, dirs, files in os.walk(root):
                if callable(prune):
                    prune(dirs)
                else:
                    dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                if self.cancelled():
                    break
                for name in sorted(files):
                    if name.startswith(".") or not self._media_kind(name):
                        continue
                    media = Path(dp) / name
                    if not self.has_sidecar(media):
                        continue
                    discovered += 1
                    batch.append(media)
                    if len(batch) >= HYDRUS_HASH_LOOKUP_BATCH:
                        yield batch
                        batch = []
            if batch:
                yield batch

        def prepare(media: Path) -> Tuple[
                Optional[os.stat_result], Set[str], Set[str], str,
                Optional[str], Optional[object], bool]:
            try:
                st = media.stat()
            except OSError as e:
                _notify(f"⚠️  Couldn't stat sidecar media {media.name}: {e}")
                return None, set(), set(), "", None, None, False
            tags, urls = self.read_sidecar_payload(media)
            tags = {tag for tag in tags if not _is_junk_tag(tag)}
            signature = self._sidecar_sync_signature(tags, urls)
            ledger = ledger_mgr.get(media.parent)
            if ledger.sidecar_sync_matches(
                    media.name, st.st_size, st.st_mtime, signature,
                    scope_id=scope_id,
                    tag_deleted_duplicates=tag_deleted,
                    mtime_ns=st.st_mtime_ns):
                return st, tags, urls, signature, None, ledger, True
            # Empty sidecars are a completed no-op. Do not read the entire media
            # file merely to checkpoint that they contain no metadata.
            sha256 = self._sha256_local(media) if (tags or urls) else None
            return st, tags, urls, signature, sha256, ledger, False

        def emit_progress(
                index: int, media: Path, state: str,
                *, final: bool = False, total: int = 0) -> None:
            self._emit(
                "sidecar_sync",
                message=(
                    f"{state} {index}"
                    + (f"/{total}" if total else f" · found {discovered}")
                    + f" · {media.name}"),
                index=index,
                total=total or discovered,
                current=str(media.relative_to(root)),
                sub=state,
                extra={
                    "checkpoint":
                        index == 1 or index % 25 == 0 or final,
                    "final": final,
                    "attempted": attempted,
                    "successful": successful,
                    "skipped": skipped,
                    "terminal_skipped": terminal_skipped,
                    "failed": failed,
                    "discovered": discovered,
                },
            )

        def checkpoint(
                ledger, media: Path, st: os.stat_result, signature: str,
                *, sha256: Optional[str] = None,
                disposition: str = "live",
                import_state: Optional[str] = None,
                complete: bool = True) -> None:
            ledger.record_sidecar_sync(
                media.name, st.st_size, st.st_mtime, signature,
                sha256=sha256,
                mtime_ns=st.st_mtime_ns,
                scope_id=scope_id,
                disposition=disposition,
                import_state=import_state,
                complete=complete,
                tag_deleted_duplicates=tag_deleted,
            )

        print("📤 Syncing sidecars to Hydrus…")
        workers = min(8, max(1, os.cpu_count() or 1))
        index = 0
        try:
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for batch in iter_batches():
                    if self.cancelled():
                        break
                    emit_progress(
                        max(1, index + 1), batch[0], "checking sidecars")
                    prepared = list(ex.map(prepare, batch))
                    local_hashes = {
                        sha256 for _st, _tags, _urls, _signature, sha256,
                        _ledger, was_synced in prepared
                        if sha256 is not None and not was_synced
                    }
                    current_hashes = self._hydrus_current_sha256s(
                        local_hashes)

                    for media, item in zip(batch, prepared):
                        index += 1
                        if self.cancelled():
                            break
                        (st, tags, urls, signature, sha256,
                         ledger, was_synced) = item
                        if st is None or ledger is None:
                            failed += 1
                            emit_progress(index, media, "failed")
                            continue
                        if was_synced:
                            skipped += 1
                            emit_progress(index, media, "already synced")
                            continue
                        if not tags and not urls:
                            checkpoint(
                                ledger, media, st, signature,
                                disposition="live")
                            successful += 1
                            emit_progress(index, media, "no metadata")
                            if successful % 25 == 0:
                                ledger_mgr.save_all()
                            continue

                        known_sha256 = None
                        if sha256 and (
                                not self.hydrus_import
                                or (current_hashes is not None
                                    and sha256 in current_hashes)):
                            known_sha256 = sha256

                        emit_progress(index, media, "syncing")
                        attempted += 1
                        push = self._hydrus_push_detailed(
                            media, tags, urls,
                            known_sha256=known_sha256)
                        disposition = self._sidecar_sync_disposition(push)
                        if disposition in (
                                "live", "deleted_tagged_duplicates"):
                            checkpoint(
                                ledger, media, st, signature,
                                sha256=push.sha256 or sha256,
                                disposition=disposition,
                                import_state=push.import_state.value)
                            successful += 1
                            emit_progress(index, media, "synced")
                            if successful % 25 == 0:
                                ledger_mgr.save_all()
                        elif disposition in (
                                "deleted_no_duplicates",
                                "deleted_policy_skipped"):
                            # Terminal under this scope/policy — not a failure.
                            checkpoint(
                                ledger, media, st, signature,
                                sha256=push.sha256 or sha256,
                                disposition=disposition,
                                import_state=push.import_state.value)
                            terminal_skipped += 1
                            emit_progress(
                                index, media,
                                "deleted in Hydrus (terminal)")
                            if (successful + terminal_skipped) % 25 == 0:
                                ledger_mgr.save_all()
                        else:
                            failed += 1
                            emit_progress(index, media, "failed")

                    if self.cancelled():
                        break
        finally:
            # A close/cancel or unexpected exception must retain every
            # successful reconciliation from this run.
            ledger_mgr.save_all()

        if self.cancelled():
            final_state = "cancelled"
            print("⏹️  Sidecar sync cancelled.")
        else:
            final_state = "complete"
        handled = successful + skipped + terminal_skipped + failed
        total = discovered or handled
        if discovered == 0 and not self.cancelled():
            print("📤 No FurTag sidecars found to sync to Hydrus.")
            return 0, 0
        self._emit(
            "sidecar_sync",
            message=(
                f"sidecar sync {final_state} · {successful} new · "
                f"{skipped} already synced · "
                f"{terminal_skipped} terminal deleted · {failed} failed"),
            index=handled,
            total=total,
            extra={
                "final": True,
                "checkpoint": True,
                "attempted": attempted,
                "successful": successful,
                "skipped": skipped,
                "terminal_skipped": terminal_skipped,
                "failed": failed,
                "discovered": discovered,
            },
        )
        print(
            f"✅ Sidecar sync: {successful} newly completed, "
            f"{skipped} already synced, "
            f"{terminal_skipped} terminal deleted, {failed} failed"
            f" ({discovered} candidate(s)); "
            "ledger checkpoints saved.")
        return attempted, failed


    def _hydrus_post(self, endpoint: str, body: dict, timeout: int) -> requests.Response:
        """POST to a Hydrus Client API endpoint with the standard headers."""
        return self.session.post(
            f"{self.hydrus_api_url}/{endpoint}",
            headers={**self._hydrus_headers(), "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )


    def _hydrus_add_file(self, media: Path) -> Optional[HydrusAddFileResult]:
        """POST /add_files/add_file by path. Returns (SHA-256 hex, import status)
        on success — status 1 = newly imported, 2 = already in the db."""
        try:
            r = self._hydrus_post("add_files/add_file", {"path": str(media.resolve())}, 120)
        except requests.RequestException as e:
            _notify(f"❌ Hydrus import request failed for {media.name}: {e}")
            return None

        if r.status_code != 200:
            _notify(f"⚠️  Hydrus import HTTP {r.status_code} for {media.name}: "
                   f"{r.text[:200]}")
            return None

        try:
            data = r.json()
        except ValueError:
            _notify(f"⚠️  Hydrus import returned non-JSON for {media.name}")
            return None

        status = data.get("status")
        h = data.get("hash") or ""
        note = (data.get("note") or "").strip()
        # 1 = imported, 2 = already in db — both give us a usable hash.
        # 3 is known-deleted. Keep its hash long enough to look up current
        # duplicate-group members, but never tag or cache this deleted record.
        if status in (1, 2) and h:
            return HydrusAddFileResult(h, status, note)
        if status == 3 and h:
            return HydrusAddFileResult(h, status, note)
        if status == 3:
            _notify(f"⚠️  Hydrus: {media.name} previously deleted"
                   + (f" ({note})" if note else "") + " — not tagging.")
            return None
        if status == 7:
            _notify(f"⚠️  Hydrus vetoed {media.name}"
                   + (f": {note}" if note else ""))
            return HydrusAddFileResult(h or None, status, note)
        _notify(f"⚠️  Hydrus import failed for {media.name} (status={status})"
               + (f": {note}" if note else ""))
        return None


    def _hydrus_add_tags(self, file_hash: str, tags: Set[str]) -> None:
        """POST /add_tags/add_tags — act like a downloader (don't override deletes)."""
        if not self.hydrus_tag_service_key:
            raise RuntimeError("Hydrus tag service is unresolved; refusing to "
                               "push tags to an unknown service.")
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

    def _hydrus_set_notes(
            self, file_hash: str, notes: Dict[str, str]) -> None:
        """Idempotently upsert source notes without touching unrelated notes."""
        cleaned = {str(name).strip(): str(text).strip()
                   for name, text in notes.items()
                   if str(name).strip() and str(text).strip()}
        if not cleaned:
            return
        body = {
            "hash": file_hash,
            "notes": cleaned,
            # Stable source-specific names make a rerun an update, not a
            # duplicate. False also avoids accidentally merging personal notes.
            "merge_cleverly": False,
        }
        r = self._hydrus_post("add_notes/set_notes", body, 30)
        if r.status_code != 200:
            raise RuntimeError(
                f"set_notes HTTP {r.status_code}: {r.text[:200]}")


    def _hydrus_get_url_info(self, url: str) -> Dict:
        """Return Hydrus's URL-class decision for one verified source URL."""
        try:
            r = self.session.get(
                f"{self.hydrus_api_url}/add_urls/get_url_info",
                headers=self._hydrus_headers(), params={"url": url}, timeout=30,
            )
        except requests.RequestException as e:
            raise RuntimeError(f"get_url_info request failed: {e}") from e
        if r.status_code != 200:
            raise RuntimeError(f"get_url_info HTTP {r.status_code}: "
                               f"{r.text[:200]}")
        try:
            info = r.json()
        except ValueError as e:
            raise RuntimeError("get_url_info returned non-JSON") from e
        if not isinstance(info, dict):
            raise RuntimeError("get_url_info returned an invalid payload")
        return info


    def _hydrus_add_url_for_enrichment(self, url: str) -> None:
        """Queue a recognised exact-match Post URL in Hydrus's downloader."""
        body = {
            "url": url,
            "destination_page_name":
                self.hydrus_exact_url_enrichment_page_name,
            "show_destination_page": False,
        }
        r = self._hydrus_post("add_urls/add_url", body, 30)
        if r.status_code != 200:
            raise RuntimeError(f"add_url HTTP {r.status_code}: {r.text[:200]}")


    def _hydrus_associate_urls(self, file_hash: str, urls: Set[str]) -> None:
        """POST /add_urls/associate_url."""
        body = {"hash": file_hash, "urls_to_add": sorted(urls)}
        r = self._hydrus_post("add_urls/associate_url", body, 30)
        if r.status_code != 200:
            raise RuntimeError(f"associate_url HTTP {r.status_code}: {r.text[:200]}")


    def _hydrus_add_to_page(self, kind: str, file_hash: str) -> None:
        """Accept a result hash without making page API traffic block a worker."""
        page = self.hydrus_result_pages.get(kind)
        if not page or not page.enabled or page.failed or not file_hash:
            return
        with self._hydrus_page_condition:
            if file_hash in page.seen:
                return
            if page.mode == "live" and page.limit and len(page.hashes) >= page.limit:
                return
            page.seen.add(file_hash)
            page.hashes.append(file_hash)
            if page.mode == "end_of_run":
                if page.limit and len(page.hashes) > page.limit:
                    del page.hashes[:-page.limit]
            else:
                page.pending.append(file_hash)
                if self.hydrus_live_page_update_interval == 0:
                    self._hydrus_page_condition.notify()


    def _hydrus_start_result_page_run(self) -> None:
        """Reset all page runtime state and start this scan's live publisher."""
        hy = self.settings.hydrus
        enabled_master = bool(
            self.has_hydrus and self.hydrus_can_manage_pages
            and hy.results_pages_enabled)
        with self._hydrus_page_condition:
            self._hydrus_page_failures.clear()
            self._hydrus_page_stop = False
            self._hydrus_page_run_active = True
            for page in self.hydrus_result_pages.values():
                page.reset(enabled=enabled_master and page.configured_enabled)
        self.hydrus_already_tagged_page_enabled = bool(
            enabled_master and hy.already_tagged_page_enabled)
        live_enabled = any(
            page.enabled and page.mode == "live"
            for page in self.hydrus_result_pages.values())
        self._hydrus_page_publisher = None
        if live_enabled:
            self._hydrus_page_publisher = threading.Thread(
                target=self._hydrus_page_publisher_loop,
                name="hydrus-page-publisher", daemon=True)
            self._hydrus_page_publisher.start()


    def _hydrus_page_publisher_loop(self) -> None:
        """Publish live page queues periodically until final synchronous drain."""
        interval = self.hydrus_live_page_update_interval
        while True:
            with self._hydrus_page_condition:
                if not self._hydrus_page_stop:
                    if interval == 0:
                        self._hydrus_page_condition.wait_for(
                            lambda: self._hydrus_page_stop or any(
                                page.pending for page in
                                self.hydrus_result_pages.values()))
                    else:
                        self._hydrus_page_condition.wait(timeout=interval)
                stopping = self._hydrus_page_stop
                batches = []
                for page in self.hydrus_result_pages.values():
                    if page.mode != "live" or not page.pending:
                        continue
                    batch = page.pending[:]
                    page.pending.clear()
                    batches.append((page, batch))
            for page, batch in batches:
                self._hydrus_publish_live_batch(page, batch)
            if stopping:
                return


    def _hydrus_publish_live_batch(
            self, page: HydrusResultPageState, hashes: List[str]) -> None:
        if not hashes or not page.enabled or page.failed:
            return
        try:
            if page.page_key is None:
                page.page_key = self._hydrus_create_hash_page(page.name, hashes)
                if not page.page_key:
                    raise RuntimeError("page creation returned no page key")
                return
            for start in range(0, len(hashes), HYDRUS_PAGE_BATCH):
                with self._hydrus_page_api_lock:
                    r = self._hydrus_post("manage_pages/add_files", {
                        "page_key": page.page_key,
                        "hashes": hashes[start:start + HYDRUS_PAGE_BATCH],
                    }, 30)
                if r.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            self._hydrus_disable_result_page(page, e)


    def _hydrus_disable_result_page(
            self, page: HydrusResultPageState, error: Exception) -> None:
        """Disable only one page and retain one warning for run finalization."""
        with self._hydrus_page_condition:
            if page.failed:
                return
            page.failed = True
            page.enabled = False
            page.pending.clear()
            self._hydrus_page_failures[page.name] = str(error)


    def _hydrus_create_hash_page(self, name: str, hashes: List[str]) -> Optional[str]:
        """Create one unfocused, hash-locked page and fill it in batches.

        The single page-creation path for every review page, so batching and
        error handling can't differ between them. Raises on failure to create
        the page; callers decide how to report it. A batch that fails after the
        page exists is reported and stops the fill, keeping what landed.
        """
        first, rest = hashes[:HYDRUS_PAGE_BATCH], hashes[HYDRUS_PAGE_BATCH:]
        with self._hydrus_page_api_lock:
            r = self._hydrus_post("manage_pages/new_page", {
                "page_type": 6,
                "page_name": name,
                "hashes": first,
                "system_hash_locked": True,
                "focus_page": False,
            }, 30)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            page_key = r.json()["page_key"]
            for start in range(0, len(rest), HYDRUS_PAGE_BATCH):
                r = self._hydrus_post("manage_pages/add_files", {
                    "page_key": page_key,
                    "hashes": rest[start:start + HYDRUS_PAGE_BATCH],
                }, 30)
                if r.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {r.status_code}: {r.text[:200]}")
        return page_key


    def _hydrus_flush_result_pages(self) -> None:
        """Compatibility helper: synchronously publish all uncreated pages."""
        if not self.has_hydrus:
            return
        for page in self.hydrus_result_pages.values():
            if not page.enabled or page.failed or not page.hashes or page.page_key:
                continue
            try:
                page.page_key = self._hydrus_create_hash_page(
                    page.name, list(page.hashes))
            except Exception as e:
                self._hydrus_disable_result_page(page, e)


    def _hydrus_finalize_result_page_run(self) -> None:
        """Drain live queues, create deferred pages, and stop the publisher."""
        if not self._hydrus_page_run_active:
            return
        with self._hydrus_page_condition:
            self._hydrus_page_stop = True
            self._hydrus_page_condition.notify_all()
        publisher = self._hydrus_page_publisher
        if publisher is not None:
            publisher.join()

        # Defensive synchronous drain for a run with no publisher, and for any
        # queue accepted immediately around shutdown.
        for page in self.hydrus_result_pages.values():
            if page.mode == "live":
                with self._hydrus_page_condition:
                    pending = page.pending[:]
                    page.pending.clear()
                self._hydrus_publish_live_batch(page, pending)
            elif page.enabled and not page.failed and page.hashes:
                try:
                    page.page_key = self._hydrus_create_hash_page(
                        page.name, list(page.hashes))
                except Exception as e:
                    self._hydrus_disable_result_page(page, e)

        if self._hydrus_page_failures:
            details = "; ".join(
                f"{name}: {error}" for name, error in
                self._hydrus_page_failures.items())
            _notify("⚠️  Some Hydrus review pages were disabled for this run "
                    f"({details}). Tagging and other pages continued.")
        self._hydrus_page_publisher = None
        self._hydrus_page_run_active = False


    @staticmethod
    def _unchanged_records(
            ledger_mgr: LedgerManager,
            match: Callable[[Dict], bool],
            verified: Set[str],
            require_ledger_file: bool = False,
    ) -> Iterator[Tuple[Path, "Ledger", str, os.stat_result, Dict]]:
        """Walk touched ledgers for records still matching their fingerprint.

        One definition of "an unchanged record of status X": select with *match*,
        then re-verify against the file's current size/mtime via `status_for`, so
        an edited or replaced file is never treated as still resolved. Yields
        ``(path, ledger, name, stat_result, record)``.
        """
        for ledger in ledger_mgr.touched():
            if require_ledger_file and not ledger.path.exists():
                continue
            for name, rec in ledger.records.items():
                if not isinstance(rec, dict) or not match(rec):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                if ledger.status_for(name, st.st_size, st.st_mtime) not in verified:
                    continue
                yield path, ledger, name, st, rec


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
        for path, ledger, name, st, rec in self._unchanged_records(
                ledger_mgr,
                lambda r: (
                    r.get("status") == "matched"
                    and not (
                        isinstance(r.get("hydrus_output"), dict)
                        and not r["hydrus_output"].get("complete"))),
                {"matched"}):
            # Records predating tagged_at sort oldest (fall to the tail).
            entries.append((path, ledger, name, st.st_size, st.st_mtime,
                            ledger.sha256_for(name, st.st_size, st.st_mtime),
                            rec.get("tagged_at") or 0.0))

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
            self._emit("status", track="perceptual",
                       sub=f"{self.hydrus_already_tagged_page_name} page · "
                           f"hashing {len(missing)} file(s)")
            workers = min(8, max(1, os.cpu_count() or 1))
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {ex.submit(self._sha256_local, entry[0]): entry
                           for entry in missing}
                for future in cf.as_completed(futures):
                    path, ledger, name, size, mtime, _, _ = futures[future]
                    try:
                        sha256 = future.result()
                    except Exception as e:
                        _notify(f"❌ SHA256 failed on {path.name}: {e}")
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

        try:
            self._hydrus_create_hash_page(
                self.hydrus_already_tagged_page_name, hashes)
        except (requests.RequestException, ValueError, KeyError, TypeError,
                RuntimeError) as e:
            with self._hydrus_page_condition:
                self._hydrus_page_failures.setdefault(
                    self.hydrus_already_tagged_page_name, str(e))
            self.hydrus_already_tagged_page_enabled = False
            return 0
        return len(hashes)


    @classmethod
    def _has_prior_matched_files(cls, ledger_mgr: LedgerManager) -> bool:
        """True only when an unchanged, valid matched ledger record exists."""
        return any(cls._unchanged_records(
            ledger_mgr,
            lambda r: (
                r.get("status") == "matched"
                and not (
                    isinstance(r.get("hydrus_output"), dict)
                    and not r["hydrus_output"].get("complete"))),
            {"matched"},
            require_ledger_file=True))


    def _hydrus_reconcile_prior_matches(
            self, ledger_mgr: LedgerManager,
            queued_items: Optional[List[FileItem]] = None,
    ) -> PriorMatchReconcileResult:
        """Retry only incomplete Hydrus match sinks from durable sidecars.

        A normal match writes tags/URLs to sidecars after its Hydrus attempt.
        When that attempt fails, ``write_results_detailed`` stores a compact
        resume context beside the incomplete ``hydrus_output`` checkpoint.
        This pre-pass selects only those ledger rows; unlike the broad manual
        sidecar-sync operation, it neither walks unrelated sidecars nor calls
        any source lookup service.

        Attempted paths are removed from the already-built search queue even
        when Hydrus is still down. That prevents a failed recovery attempt from
        immediately falling through to an unnecessary booru re-query in the
        same launch. Missing/deleted sidecars remain queued so a normal lookup
        can reconstruct their payload.
        """
        result = PriorMatchReconcileResult()
        if not self.has_hydrus:
            return result
        profile = self.search_profile_hash()

        def _needs_resume(rec: Dict) -> bool:
            checkpoint = rec.get("hydrus_output")
            if rec.get("status") == "hashed":
                # Compatibility for interrupted runs made before selective
                # checkpoints existed. The path-level sidecar check below
                # keeps ordinary unresolved hashes in the lookup pipeline.
                return True
            return bool(
                rec.get("status") == "matched"
                and rec.get("search_profile_hash") == profile
                and isinstance(checkpoint, dict)
                and not checkpoint.get("complete")
                and isinstance(
                    checkpoint.get("resume_from_sidecars"), dict))

        entries: List[Tuple[Path, Ledger, str, os.stat_result, Dict]] = []
        for ledger in ledger_mgr.touched():
            for name, rec in ledger.records.items():
                if not isinstance(rec, dict) or not _needs_resume(rec):
                    continue
                path = ledger.dir / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                # Verify bytes/path freshness directly. The shared resolved-row
                # iterator also applies direct-note backfill invalidation, but
                # pending Hydrus output must be resumed before that decision.
                if ledger._fresh_record(
                        name, st.st_size, st.st_mtime,
                        mtime_ns=st.st_mtime_ns) is None:
                    continue
                if rec.get("status") == "hashed" and not self.has_sidecar(path):
                    continue
                entries.append((path, ledger, name, st, rec))
        if entries:
            print(
                f"📥 Retrying Hydrus output for {len(entries)} previously "
                "tagged file(s)…")

        for done, (path, ledger, name, st, rec) in enumerate(entries, 1):
            if self.cancelled():
                break
            checkpoint = rec.get("hydrus_output") or {}
            resume = checkpoint.get("resume_from_sidecars") or {
                # Legacy hashed+sidecar recovery cannot reconstruct direct
                # notes or the original exact-URL enrichment decision, but it
                # can safely import and apply the durable tags/URLs without a
                # source lookup. Future failures persist the exact context.
                "requires_sidecar": True,
                "url_policy": UrlWritePolicy.ASSOCIATE_ONLY.value,
                "force_associate_urls": [],
                "notes": {},
            }
            requires_sidecar = bool(resume.get("requires_sidecar", True))
            if requires_sidecar and not self.has_sidecar(path):
                result.missing_payload += 1
                continue
            tags, urls = self.read_sidecar_payload(path)
            notes = {
                str(key): str(value)
                for key, value in (resume.get("notes") or {}).items()
                if str(key).strip() and str(value).strip()
            }
            if requires_sidecar and not (tags or urls):
                result.missing_payload += 1
                continue
            try:
                url_policy = UrlWritePolicy(
                    resume.get("url_policy")
                    or UrlWritePolicy.ASSOCIATE_ONLY.value)
            except (TypeError, ValueError):
                url_policy = UrlWritePolicy.ASSOCIATE_ONLY
            force_associate = {
                str(url) for url in
                (resume.get("force_associate_urls") or []) if url
            }
            self._emit(
                "status", track="hash",
                sub=f"Hydrus retry (tagged sidecar) "
                    f"{done}/{len(entries)} · {path.name}")
            result.attempted_paths.add(path.resolve())
            push = self._hydrus_push_detailed(
                path, tags, urls,
                known_sha256=(
                    checkpoint.get("sha256") or rec.get("sha256")),
                url_policy=url_policy,
                force_associate_urls=force_associate,
                notes=notes)
            new_checkpoint = push.to_ledger_checkpoint()
            if not push.complete:
                new_checkpoint["resume_from_sidecars"] = resume
            self.ledger_record(
                ledger,
                name, st.st_size, st.st_mtime, rec.get("md5"),
                "matched", list(rec.get("sources") or []),
                duplicate_of=str(rec.get("duplicate_of") or ""),
                sha256=push.sha256 or rec.get("sha256"),
                direct_notes_applied=(
                    self.direct_notes_effective() if push.complete else False),
                hydrus_output=new_checkpoint,
                review=(
                    rec.get("review")
                    if isinstance(rec.get("review"), dict) else None),
                mtime_ns=st.st_mtime_ns,
                tagged_at=rec.get("tagged_at"),
                stamp_tagged_at=False,
                metadata_version=rec.get("metadata_version", 0))
            if push.complete:
                result.completed_paths.add(path.resolve())
            else:
                result.failed += 1

        if queued_items is not None and result.attempted_paths:
            attempted = result.attempted_paths
            queued_items[:] = [
                item for item in queued_items
                if item.path.resolve() not in attempted]
        return result


    def _hydrus_import_prior_nomatches(
            self, ledger_mgr: LedgerManager,
            queued_items: Optional[List[FileItem]] = None,
    ) -> PriorNomatchReconcileResult:
        """Import unchanged old no-match files when unmatched import is on.

        Picks up legacy rows without SHA and rows whose nested
        ``unmatched_import`` checkpoint is incomplete (BF-02). Files completed
        here are removed from *queued_items* so a profile-invalidated nomatch
        is not immediately searched again by the already-built scan queue.
        """
        result = PriorNomatchReconcileResult()
        if not (self.has_hydrus and self.hydrus_import and
                self.hydrus_import_unmatched):
            return result
        current_scope = self._hydrus_scope_id()

        def _needs_import(rec: Dict) -> bool:
            if rec.get("status") != "nomatch":
                return False
            return not self.unmatched_import_is_complete(
                rec, required=True, scope_id=current_scope)

        entries: List[Tuple[
            Path, Ledger, str, int, float, int, Optional[str]]] = []
        for path, ledger, name, st, rec in self._unchanged_records(
                ledger_mgr, _needs_import, {"nomatch"}):
            entries.append((
                path, ledger, name, st.st_size, st.st_mtime, st.st_mtime_ns,
                rec.get("md5") if isinstance(rec, dict) else None))
        if entries:
            print(f"📥 Importing {len(entries)} prior no-match file(s) to Hydrus…")
        for done, (path, ledger, name, size, mtime, mtime_ns, md5) in enumerate(
                entries, 1):
            if self.cancelled():
                break
            # A per-file Hydrus round trip — without a status event the GUI's
            # progress cards sit frozen for the whole (possibly long) import.
            self._emit("status", track="hash",
                       sub=f"Hydrus import (prior no-match) "
                           f"{done}/{len(entries)} · {path.name}")
            outcome = self.write_unmatched_detailed(path)
            if outcome.complete:
                self.ledger_record(
                    ledger,
                    name, size, mtime, md5, "nomatch", [],
                    sha256=outcome.sha256,
                    unmatched_import=outcome.unmatched_import,
                    mtime_ns=mtime_ns)
                result.completed_paths.add(path.resolve())
                checkpoint = outcome.unmatched_import or {}
                import_state = checkpoint.get("import_state")
                if import_state == HydrusImportState.LIVE.value:
                    result.live += 1
                elif import_state == HydrusImportState.PREVIOUSLY_DELETED.value:
                    result.previously_deleted += 1
                elif import_state == HydrusImportState.VETOED.value:
                    result.vetoed += 1
                else:
                    result.other_terminal += 1
            else:
                # Persist the incomplete checkpoint even when no SHA was
                # returned; otherwise the ledger loses why the sink is open.
                if outcome.sha256:
                    ledger.cache_sha256(
                        name, size, mtime, outcome.sha256,
                        mtime_ns=mtime_ns)
                previous = ledger._fresh_record(
                    name, size, mtime, mtime_ns=mtime_ns) or {}
                sources = list(previous.get("sources") or [])
                self.ledger_record(
                    ledger,
                    name, size, mtime,
                    md5 or previous.get("md5"),
                    "nomatch", sources,
                    sha256=outcome.sha256,
                    unmatched_import=outcome.unmatched_import,
                    review=previous.get("review") if isinstance(
                        previous.get("review"), dict) else None,
                    mtime_ns=mtime_ns)
                result.failed += 1
        if queued_items is not None and result.completed_paths:
            completed = result.completed_paths
            queued_items[:] = [
                item for item in queued_items
                if item.path.resolve() not in completed]
        return result


    @staticmethod
    def _is_exact_hash_post_url(url: str) -> bool:
        """Compatibility wrapper — prefer :func:`is_enrichable_post_url`."""
        return is_enrichable_post_url(url)

    def _resolve_url_policy(
            self, url_policy: Optional[UrlWritePolicy] = None,
            exact_match: bool = False) -> UrlWritePolicy:
        """Map legacy ``exact_match`` onto :class:`UrlWritePolicy`."""
        if url_policy is not None:
            return url_policy
        if exact_match:
            return UrlWritePolicy.ENRICH_HASH_POSTS
        return UrlWritePolicy.ASSOCIATE_ONLY

    def _hydrus_push(
            self, media: Path, tags: Set[str], urls: Set[str],
            known_sha256: Optional[str] = None,
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
    ) -> Optional[str]:
        """Import (optional) + tag + route URLs. Returns SHA-256 or None.

        Thin wrapper over :meth:`_hydrus_push_detailed` for callers that only
        need the hash (scan pipeline, tests).
        """
        return self._hydrus_push_detailed(
            media, tags, urls, known_sha256=known_sha256,
            exact_match=exact_match, url_policy=url_policy,
            force_associate_urls=force_associate_urls, notes=notes).sha256

    def _hydrus_scope_id(self) -> Optional[str]:
        """Current non-secret Hydrus scope, or None if Hydrus is unbound."""
        from furtag_settings import hydrus_scope_id
        url = getattr(self, "hydrus_api_url", "") or ""
        if not url:
            return None
        settings = getattr(self, "settings", None)
        profile = ""
        if settings is not None:
            profile = getattr(
                getattr(settings, "hydrus", None), "hydrus_profile_uuid", "") or ""
        if not profile:
            profile = getattr(self, "hydrus_profile_uuid", "") or ""
        return hydrus_scope_id(profile, url)

    def _hydrus_push_detailed(
            self, media: Path, tags: Set[str], urls: Set[str],
            known_sha256: Optional[str] = None,
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
    ) -> HydrusPushResult:
        """Hydrus push with two-axis import/metadata outcomes.

        Safety: only *adds* content (never deletes files/tags/URLs). If import
        is on and the import is refused (previously deleted, vetoed, error),
        we abort bare-hash tagging of the deleted original — status 3 goes
        through the deleted-duplicate path when metadata was requested.

        For :attr:`UrlWritePolicy.ENRICH_HASH_POSTS`, parseable Post URLs are
        queued through Hydrus's URL downloader so installed parsers can add
        notes/descriptions. Other URLs are associated directly.
        *force_associate_urls* never enter the downloader (multi-file IB).
        """
        policy = self._resolve_url_policy(url_policy, exact_match)
        force_associate = {u for u in (force_associate_urls or set()) if u}
        scope = self._hydrus_scope_id()
        policy_hash = self.hydrus_output_policy_hash()
        with self._hydrus_lock:
            file_hash: Optional[str] = None
            import_status: Optional[int] = None
            try:
                if known_sha256:
                    file_hash, import_status = known_sha256, 2
                    import_state = HydrusImportState.LIVE
                elif self.hydrus_import:
                    added = self._hydrus_add_file(media)
                    if not added:
                        return HydrusPushResult(
                            scope_id=scope, policy_hash=policy_hash)
                    file_hash, import_status = added.sha256, added.status
                    if import_status == 7:
                        return HydrusPushResult(
                            sha256=file_hash,
                            import_state=HydrusImportState.VETOED,
                            metadata_state=HydrusMetadataState.NOT_REQUESTED,
                            scope_id=scope,
                            policy_hash=policy_hash,
                            reason=added.note or None)
                    if import_status == 3:
                        return self._hydrus_push_to_deleted_duplicates(
                            media, file_hash, tags, urls, url_policy=policy,
                            force_associate_urls=force_associate, notes=notes)
                    import_state = HydrusImportState.LIVE
                else:
                    file_hash = self._sha256_local(media)
                    if not file_hash:
                        _notify(f"❌ Hydrus: no hash for {media.name}; skipped push.")
                        return HydrusPushResult(
                            scope_id=scope, policy_hash=policy_hash)
                    import_status = 2
                    import_state = HydrusImportState.NOT_REQUESTED

                metadata_requested = bool(tags or urls or notes)
                if not metadata_requested:
                    return HydrusPushResult(
                        sha256=file_hash,
                        import_state=import_state,
                        metadata_state=HydrusMetadataState.NOT_REQUESTED,
                        scope_id=scope,
                        policy_hash=policy_hash)

                if tags:
                    self._hydrus_add_tags(file_hash, tags)
                # A missing permission disables that optional metadata channel
                # for this session; it is not a transient per-file failure.
                # Retrying every booru lookup forever cannot make the key gain
                # a permission. Actual attempted writes still remain retryable.
                notes_complete = (
                    not notes
                    or not getattr(self, "hydrus_direct_notes_enabled", True)
                    or not self.hydrus_can_edit_notes)
                if (notes and getattr(self, "hydrus_direct_notes_enabled", True)
                        and self.hydrus_can_edit_notes):
                    try:
                        self._hydrus_set_notes(file_hash, notes)
                        notes_complete = True
                    except Exception as e:
                        notes_complete = False
                        _notify(f"⚠️  Hydrus direct-note write failed for "
                               f"{media.name}; other metadata was kept ({e})")
                urls_complete = not urls or not self.hydrus_can_edit_urls
                if urls and self.hydrus_can_edit_urls:
                    urls_complete = self._hydrus_route_urls(
                        media, file_hash, urls, url_policy=policy,
                        force_associate_urls=force_associate)
                if import_status == 1:
                    self._hydrus_add_to_page("new", file_hash)
                elif tags or urls or notes:
                    self._hydrus_add_to_page("updated", file_hash)
                meta_ok = urls_complete and notes_complete
                return HydrusPushResult(
                    sha256=file_hash,
                    import_state=import_state,
                    metadata_state=(
                        HydrusMetadataState.APPLIED_ORIGINAL if meta_ok
                        else HydrusMetadataState.RETRYABLE_FAILURE),
                    scope_id=scope,
                    policy_hash=policy_hash)
            except Exception as e:
                _notify(f"❌ Hydrus push failed for {media.name}: {e}")
                # If import/hash resolution already succeeded, retain the hash
                # so an idempotent metadata retry can skip add_file next run.
                return HydrusPushResult(
                    sha256=file_hash,
                    import_state=(
                        HydrusImportState.LIVE if file_hash
                        else HydrusImportState.RETRYABLE_FAILURE),
                    metadata_state=HydrusMetadataState.RETRYABLE_FAILURE,
                    scope_id=scope,
                    policy_hash=policy_hash,
                    reason=str(e)[:240])

    def _hydrus_push_to_deleted_duplicates(
            self, media: Path, deleted_hash: str, tags: Set[str],
            urls: Set[str],
            url_policy: UrlWritePolicy = UrlWritePolicy.ASSOCIATE_ONLY,
            force_associate_urls: Optional[Set[str]] = None,
            notes: Optional[Dict[str, str]] = None,
    ) -> HydrusPushResult:
        """Handle import status 3 with two-axis outcomes.

        Always retains *deleted_hash*. Relationship lookup runs only when
        metadata was requested and deleted-dup tagging is enabled with
        permission. Missing permission is incomplete (not a permanent seal).
        Policy-disabled tagging is ``policy_skipped`` (not unscoped permanent).
        Only a successful empty relationship query yields
        ``no_duplicate_targets``.
        """
        scope = self._hydrus_scope_id()
        policy_hash = self.hydrus_output_policy_hash()
        base_deleted = HydrusPushResult(
            sha256=deleted_hash,
            import_state=HydrusImportState.PREVIOUSLY_DELETED,
            metadata_state=HydrusMetadataState.NOT_REQUESTED,
            scope_id=scope,
            policy_hash=policy_hash,
        )
        metadata_requested = bool(tags or urls or notes)
        if not metadata_requested:
            # Unmatched / empty push: import terminal, no relationship work.
            return base_deleted

        if not self.hydrus_tag_deleted_duplicates:
            # Until policy fingerprints exist, do not invent an unscoped
            # permanent seal — search may still record matched with this
            # checkpoint; enabling tagging later needs BF-03 to reopen.
            return HydrusPushResult(
                sha256=deleted_hash,
                import_state=HydrusImportState.PREVIOUSLY_DELETED,
                metadata_state=HydrusMetadataState.POLICY_SKIPPED,
                scope_id=scope,
                policy_hash=policy_hash,
            )

        if not self.hydrus_can_manage_relationships:
            if not getattr(self, "_warned_relationship_permission", False):
                self._warned_relationship_permission = True
                _notify(
                    "⚠️  Hydrus: deleted-file duplicate tagging needs the "
                    "'Manage File Relationships' permission — files stay "
                    "retryable until the key can query relationships.")
            return HydrusPushResult(
                sha256=deleted_hash,
                import_state=HydrusImportState.PREVIOUSLY_DELETED,
                metadata_state=HydrusMetadataState.PERMISSION_MISSING,
                scope_id=scope,
                policy_hash=policy_hash,
            )

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
                message = (
                    f"Hydrus: {media.name} was previously deleted; no "
                    "current duplicate-group members to tag.")
                aggregate = getattr(self, "_notify_repeated_info", None)
                if callable(aggregate):
                    aggregate(
                        "hydrus_deleted_no_targets",
                        "Hydrus deleted files with no current duplicates",
                        message)
                else:
                    _notify_info(message)
                return HydrusPushResult(
                    sha256=deleted_hash,
                    import_state=HydrusImportState.PREVIOUSLY_DELETED,
                    metadata_state=HydrusMetadataState.NO_DUPLICATE_TARGETS,
                    scope_id=scope,
                    policy_hash=policy_hash,
                )
            metadata_complete = True
            applied: List[str] = []
            for target_hash in targets:
                try:
                    if tags:
                        self._hydrus_add_tags(target_hash, tags)
                    if (notes and getattr(self, "hydrus_direct_notes_enabled", True)
                            and self.hydrus_can_edit_notes):
                        try:
                            self._hydrus_set_notes(target_hash, notes)
                        except Exception as e:
                            metadata_complete = False
                            _notify(
                                f"⚠️  Hydrus direct-note write failed for a "
                                f"duplicate of {media.name}; other metadata was "
                                f"kept ({e})")
                    if urls and self.hydrus_can_edit_urls:
                        if not self._hydrus_route_urls(
                                media, target_hash, urls, url_policy=url_policy,
                                force_associate_urls=force_associate_urls):
                            metadata_complete = False
                    self._hydrus_add_to_page("duplicates", target_hash)
                    applied.append(target_hash)
                except Exception as e:
                    metadata_complete = False
                    _notify(
                        f"⚠️  Hydrus: failed tagging duplicate of "
                        f"{media.name}: {e}")
            if applied:
                message = (
                    f"✅ Hydrus: {media.name} was deleted; tagged "
                    f"{len(applied)} current duplicate-group file(s).")
                aggregate = getattr(self, "_notify_repeated_info", None)
                if callable(aggregate):
                    aggregate(
                        "hydrus_deleted_tagged_targets",
                        "Hydrus deleted files with tagged current duplicates",
                        message)
                else:
                    _notify_info(message)
            return HydrusPushResult(
                sha256=deleted_hash,
                import_state=HydrusImportState.PREVIOUSLY_DELETED,
                metadata_state=(
                    HydrusMetadataState.APPLIED_DUPLICATES if metadata_complete
                    else HydrusMetadataState.RETRYABLE_FAILURE),
                target_hashes=tuple(applied),
                scope_id=scope,
                policy_hash=policy_hash,
            )
        except (requests.RequestException, ValueError, RuntimeError, TypeError) as e:
            _notify(f"⚠️  Hydrus: couldn't tag duplicate-group members for "
                   f"deleted {media.name}: {e}")
            return HydrusPushResult(
                sha256=deleted_hash,
                import_state=HydrusImportState.PREVIOUSLY_DELETED,
                metadata_state=HydrusMetadataState.RETRYABLE_FAILURE,
                scope_id=scope,
                policy_hash=policy_hash,
                reason=str(e)[:240],
            )

    def _hydrus_route_urls(
            self, media: Path, file_hash: str, urls: Set[str],
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
    ) -> bool:
        """Enrich safe hash-post URLs; associate everything else.

        A successful ``add_urls/add_url`` deliberately replaces
        ``associate_url`` for that URL. Associating first can make Hydrus regard
        the already-local file as fully handled and skip the page fetch that
        supplies notes/descriptions. Returns whether every URL was ultimately
        queued or associated.

        *force_associate_urls* are never queued for enrichment even when they
        match the hash-post URL pattern (multi-file InkBunny submissions).
        """
        policy = self._resolve_url_policy(url_policy, exact_match)
        remaining = set(urls)
        enrichable, associate = partition_urls(
            remaining, policy, force_associate=force_associate_urls)
        if (enrichable and self.hydrus_exact_url_enrichment
                and not self.cancelled()):
            for url in sorted(enrichable):
                try:
                    info = self._hydrus_get_url_info(url)
                    if info.get("url_type") != 0 or not info.get("can_parse"):
                        continue
                    self._hydrus_add_url_for_enrichment(url)
                    remaining.discard(url)
                except Exception as e:
                    _notify(f"⚠️  Hydrus metadata enrichment failed for "
                           f"{media.name}; associating URL normally ({e})")

        if remaining:
            try:
                self._hydrus_associate_urls(file_hash, remaining)
            except Exception as e:
                _notify(f"⚠️  Hydrus URL association failed for "
                       f"{media.name}: {e}")
                return False
        return True
