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
from pathlib import Path
from typing import (
    Callable, Dict, Iterable, Iterator, List, Optional, Set, Tuple, TYPE_CHECKING,
)

import requests

from furtag_urls import UrlWritePolicy, is_enrichable_post_url, partition_urls

if TYPE_CHECKING:
    from furtag import FileItem, Ledger, LedgerManager

# Keep batch sizes next to the API that consumes them.
HYDRUS_HASH_LOOKUP_BATCH = 256  # well below the Client API's 2 MB GET limit
HYDRUS_PAGE_BATCH = 256         # hashes per manage_pages call
HYDRUS_RELATIONSHIP_DUPLICATES = "8"  # Hydrus duplicate-status enum; "3" = alternates


def _truthy(val: str, default: bool = False) -> bool:
    if val is None or str(val).strip() == "":
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "y", "on")


def _notify(message: str) -> None:
    """Forward to furtag.notify without importing it at module load time."""
    from furtag import notify
    notify(message)


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
        try:
            self.hydrus_results_page_limit = max(
                0, int((cfg.get("hydrus_results_page_limit") or "0").strip()))
        except ValueError:
            _notify("⚠️  Invalid hydrus_results_page_limit; using unlimited.")
            self.hydrus_results_page_limit = 0
        page_setting = cfg.get("hydrus_results_page", "on").strip()
        page_requested = page_setting.lower() not in {"", "0", "false", "no", "off"}
        for key, cfg_key in (("new", "hydrus_new_imports_page"),
                             ("updated", "hydrus_newly_tagged_page"),
                             ("duplicates", "hydrus_duplicate_tagged_page")):
            name = cfg.get(cfg_key, "").strip()
            if name:
                self.hydrus_result_pages[key]["name"] = name
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
                _notify(f"‼️  Hydrus API rejected access key (HTTP {r.status_code}) – "
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
                _notify("⚠️  Hydrus URLs disabled – access key needs the "
                       "'Import and Edit URLs' permission; tags still work.")
            if not self.hydrus_can_search_files:
                _notify("⚠️  Hydrus hash cache disabled – access key needs "
                       "'Search for and Fetch Files'; imports still work.")
            if (self.hydrus_tag_deleted_duplicates and
                    not self.hydrus_can_manage_relationships):
                _notify("⚠️  Deleted-file duplicate tagging disabled – access key needs "
                       "'Manage File Relationships'.")
            for page in self.hydrus_result_pages.values():
                page["enabled"] = page_requested and can_manage_pages
            self.hydrus_already_tagged_page_enabled = (
                old_page_requested and can_manage_pages)
            if (page_requested or old_page_requested) and not can_manage_pages:
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


    def sync_sidecars_to_hydrus(self, root: Path) -> Tuple[int, int]:
        """Push existing FurTag sidecars to Hydrus with resumable checkpoints.

        This is a migration/reconciliation pass: tag sidecars (txt or JSON)
        supply tags and URL sidecars supply source URLs. It deliberately does
        no booru lookup. Successful syncs are recorded as independent
        ``sidecar_sync`` metadata in each directory ledger; normal scan status
        (matched/nomatch/etc.) is preserved and unaffected.

        Candidates are prepared in small batches. When the access key can
        search files, their local SHA-256s are checked against Hydrus first so
        files already present are tagged directly instead of being mirrored,
        hashed, and re-imported through ``add_files/add_file`` on every retry.
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
        total = len(candidates)
        attempted = successful = skipped = failed = 0
        # Late import: furtag loads this mixin at import time.
        from furtag import LedgerManager, _is_junk_tag
        ledger_mgr = LedgerManager()

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
                    media.name, st.st_size, st.st_mtime, signature):
                return st, tags, urls, signature, None, ledger, True
            # Empty sidecars are a completed no-op. Do not read the entire media
            # file merely to checkpoint that they contain no metadata.
            sha256 = self._sha256_local(media) if (tags or urls) else None
            return st, tags, urls, signature, sha256, ledger, False

        def emit_progress(
                index: int, media: Path, state: str,
                *, final: bool = False) -> None:
            self._emit(
                "sidecar_sync",
                message=f"{state} {index}/{total} · {media.name}",
                index=index,
                total=total,
                current=str(media.relative_to(root)),
                sub=state,
                extra={
                    "checkpoint":
                        index == 1 or index % 25 == 0 or index == total,
                    "final": final,
                    "attempted": attempted,
                    "successful": successful,
                    "skipped": skipped,
                    "failed": failed,
                },
            )

        # Sidecar reads and local hashes are disk-bound and independent. Work
        # one Hydrus-sized batch ahead rather than retaining every payload/hash
        # for a potentially huge library.
        workers = min(8, max(1, os.cpu_count() or 1))
        try:
            with cf.ThreadPoolExecutor(max_workers=workers) as ex:
                for offset in range(0, total, HYDRUS_HASH_LOOKUP_BATCH):
                    if self.cancelled():
                        break
                    batch = candidates[
                        offset:offset + HYDRUS_HASH_LOOKUP_BATCH]
                    emit_progress(
                        offset + 1, batch[0], "checking sidecars")
                    prepared = list(ex.map(prepare, batch))
                    local_hashes = {
                        sha256 for _st, _tags, _urls, _signature, sha256,
                        _ledger, was_synced in prepared
                        if sha256 is not None and not was_synced
                    }
                    current_hashes = self._hydrus_current_sha256s(
                        local_hashes)

                    for batch_index, (media, item) in enumerate(
                            zip(batch, prepared), start=1):
                        index = offset + batch_index
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
                            ledger.record_sidecar_sync(
                                media.name, st.st_size, st.st_mtime,
                                signature)
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
                        file_hash, complete = self._hydrus_push_detailed(
                            media, tags, urls,
                            known_sha256=known_sha256)
                        if complete:
                            ledger.record_sidecar_sync(
                                media.name, st.st_size, st.st_mtime,
                                signature, sha256=file_hash or sha256)
                            successful += 1
                            emit_progress(index, media, "synced")
                            if successful % 25 == 0:
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
        self._emit(
            "sidecar_sync",
            message=(
                f"sidecar sync {final_state} · {successful} new · "
                f"{skipped} already synced · {failed} failed"),
            index=min(total, successful + skipped + failed),
            total=total,
            extra={
                "final": True,
                "checkpoint": True,
                "attempted": attempted,
                "successful": successful,
                "skipped": skipped,
                "failed": failed,
            },
        )
        print(
            f"✅ Sidecar sync: {successful} newly completed, "
            f"{skipped} already synced, {failed} failed; "
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


    def _hydrus_add_file(self, media: Path) -> Optional[Tuple[str, int]]:
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
            return h, status
        if status == 3 and h:
            return h, status
        if status == 3:
            _notify(f"⚠️  Hydrus: {media.name} previously deleted"
                   + (f" ({note})" if note else "") + " — not tagging.")
            return None
        if status == 7:
            _notify(f"⚠️  Hydrus vetoed {media.name}"
                   + (f": {note}" if note else ""))
            return None
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


    def _hydrus_create_hash_page(self, name: str, hashes: List[str]) -> Optional[str]:
        """Create one unfocused, hash-locked page and fill it in batches.

        The single page-creation path for every review page, so batching and
        error handling can't differ between them. Raises on failure to create
        the page; callers decide how to report it. A batch that fails after the
        page exists is reported and stops the fill, keeping what landed.
        """
        first, rest = hashes[:HYDRUS_PAGE_BATCH], hashes[HYDRUS_PAGE_BATCH:]
        with self._hydrus_lock:
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
                    _notify(f"⚠️  Hydrus stopped filling '{name}' page "
                           f"(HTTP {r.status_code}).")
                    break
        return page_key


    def _hydrus_flush_result_pages(self) -> None:
        """Create this run's hash-locked result pages from their rolling lists."""
        if not self.has_hydrus:
            return
        for page in self.hydrus_result_pages.values():
            if not page["enabled"] or not page["hashes"]:
                continue
            name = page["name"]
            try:
                self._hydrus_create_hash_page(name, page["hashes"])
            except Exception as e:
                page["enabled"] = False
                _notify(f"⚠️  Hydrus '{name}' page unavailable ({e}); "
                       "continuing without it.")


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
                ledger_mgr, lambda r: r.get("status") == "matched", {"matched"}):
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
            _notify(f"⚠️  Hydrus Already Tagged page failed: {e}")
            return 0
        return len(hashes)


    @classmethod
    def _has_prior_matched_files(cls, ledger_mgr: LedgerManager) -> bool:
        """True only when an unchanged, valid matched ledger record exists."""
        return any(cls._unchanged_records(
            ledger_mgr, lambda r: r.get("status") == "matched", {"matched"},
            require_ledger_file=True))


    def _hydrus_import_prior_nomatches(self, ledger_mgr: LedgerManager) -> int:
        """Import unchanged old no-match files once when the run toggle is on."""
        if not (self.has_hydrus and self.hydrus_import and
                self.hydrus_import_unmatched):
            return 0
        entries: List[Tuple[Path, Ledger, str, int, float]] = []
        for path, ledger, name, st, _rec in self._unchanged_records(
                ledger_mgr,
                lambda r: r.get("status") == "nomatch" and not r.get("sha256"),
                {"nomatch"}):
            entries.append((path, ledger, name, st.st_size, st.st_mtime))
        if entries:
            print(f"📥 Importing {len(entries)} prior no-match file(s) to Hydrus…")
        imported = 0
        for done, (path, ledger, name, size, mtime) in enumerate(entries, 1):
            # A per-file Hydrus round trip — without a status event the GUI's
            # progress cards sit frozen for the whole (possibly long) import.
            self._emit("status", track="hash",
                       sub=f"Hydrus import (prior no-match) "
                           f"{done}/{len(entries)} · {path.name}")
            sha = self.write_unmatched(path)
            if sha:
                ledger.cache_sha256(name, size, mtime, sha)
                imported += 1
        return imported


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
    ) -> Optional[str]:
        """Import (optional) + tag + route URLs. Returns SHA-256 or None.

        Thin wrapper over :meth:`_hydrus_push_detailed` for callers that only
        need the hash (scan pipeline, tests).
        """
        file_hash, _complete = self._hydrus_push_detailed(
            media, tags, urls, known_sha256=known_sha256,
            exact_match=exact_match, url_policy=url_policy,
            force_associate_urls=force_associate_urls)
        return file_hash

    def _hydrus_push_detailed(
            self, media: Path, tags: Set[str], urls: Set[str],
            known_sha256: Optional[str] = None,
            exact_match: bool = False,
            url_policy: Optional[UrlWritePolicy] = None,
            force_associate_urls: Optional[Set[str]] = None,
    ) -> Tuple[Optional[str], bool]:
        """Hydrus push plus whether every requested metadata write completed.

        Safety: only *adds* content (never deletes files/tags/URLs). If import
        is on and the import is refused (previously deleted, vetoed, error),
        we abort the whole push — we do NOT fall through to bare-hash tagging.

        For :attr:`UrlWritePolicy.ENRICH_HASH_POSTS`, parseable Post URLs are
        queued through Hydrus's URL downloader so installed parsers can add
        notes/descriptions. Other URLs are associated directly.
        *force_associate_urls* never enter the downloader (multi-file IB).
        """
        policy = self._resolve_url_policy(url_policy, exact_match)
        force_associate = {u for u in (force_associate_urls or set()) if u}
        with self._hydrus_lock:
            try:
                if known_sha256:
                    file_hash, import_status = known_sha256, 2
                elif self.hydrus_import:
                    added = self._hydrus_add_file(media)
                    if not added:
                        return None, False
                    file_hash, import_status = added
                    if import_status == 3:
                        complete = self._hydrus_push_to_deleted_duplicates(
                            media, file_hash, tags, urls, url_policy=policy,
                            force_associate_urls=force_associate)
                        if urls and not self.hydrus_can_edit_urls:
                            complete = False
                        return None, complete
                else:
                    file_hash = self._sha256_local(media)
                    if not file_hash:
                        _notify(f"❌ Hydrus: no hash for {media.name}; skipped push.")
                        return None, False
                    import_status = 2

                if tags:
                    self._hydrus_add_tags(file_hash, tags)
                urls_complete = not urls
                if urls and self.hydrus_can_edit_urls:
                    urls_complete = self._hydrus_route_urls(
                        media, file_hash, urls, url_policy=policy,
                        force_associate_urls=force_associate)
                if import_status == 1:
                    self._hydrus_add_to_page("new", file_hash)
                elif tags or urls:
                    self._hydrus_add_to_page("updated", file_hash)
                return file_hash, urls_complete
            except Exception as e:
                _notify(f"❌ Hydrus push failed for {media.name}: {e}")
                return None, False

    def _hydrus_push_to_deleted_duplicates(
            self, media: Path, deleted_hash: str, tags: Set[str],
            urls: Set[str],
            url_policy: UrlWritePolicy = UrlWritePolicy.ASSOCIATE_ONLY,
            force_associate_urls: Optional[Set[str]] = None,
    ) -> bool:
        """Tag only current members of a deleted file's Hydrus duplicate group.

        Uses the same URL routing policy as a normal push so exact hash-tier
        post URLs can still enrich notes/descriptions on surviving members.
        """
        if not tags and not urls:
            return False
        if not (self.hydrus_tag_deleted_duplicates and
                self.hydrus_can_manage_relationships):
            _notify(f"⚠️  Hydrus: {media.name} was previously deleted — not tagging.")
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
                _notify(f"⚠️  Hydrus: {media.name} was previously deleted; no "
                       "current duplicate-group members to tag.")
                return False
            metadata_complete = True
            for target_hash in targets:
                if tags:
                    self._hydrus_add_tags(target_hash, tags)
                if urls and self.hydrus_can_edit_urls:
                    if not self._hydrus_route_urls(
                            media, target_hash, urls, url_policy=url_policy,
                            force_associate_urls=force_associate_urls):
                        metadata_complete = False
                self._hydrus_add_to_page("duplicates", target_hash)
            _notify(f"✅ Hydrus: {media.name} was deleted; tagged {len(targets)} "
                   "current duplicate-group file(s).")
            return metadata_complete
        except (requests.RequestException, ValueError, RuntimeError, TypeError) as e:
            _notify(f"⚠️  Hydrus: couldn't tag duplicate-group members for "
                   f"deleted {media.name}: {e}")
            return False

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

