#!/usr/bin/env python3
"""Grandfather trustworthy local legacy matches into scoped ledger records.

This is deliberately conservative:

* only unchanged ``matched`` rows with no search profile/Hydrus checkpoint;
* only rows whose stored SHA-256, or MD5 resolved to SHA-256, is confirmed as
  *currently local* in the connected Hydrus database;
* no source tags, timestamps, or historical metadata are rewritten;
* changed ledgers are backed up beneath the scan root before atomic writes;
* old directory seals are removed so normal per-file completeness (including
  required sidecars) is still checked on the next scan.

Dry-run is the default. Pass ``--apply`` to write changes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from furtag import (  # noqa: E402
    LEDGER_FILE, LEDGER_METADATA_VERSION, Ledger, TagIntegrator,
)
from furtag_credentials import CredentialStore  # noqa: E402
from furtag_settings import (  # noqa: E402
    Settings, atomic_write_text, resolve_settings_path,
)

_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")
_MD5_RE = re.compile(r"[0-9a-fA-F]{32}")
_HYDRUS_BATCH = 256


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="scan root containing ledgers")
    parser.add_argument(
        "--settings", type=Path, default=resolve_settings_path(),
        help="non-secret FurTag settings.json path")
    parser.add_argument(
        "--expected-search-profile",
        help="abort unless the live effective profile has this digest")
    parser.add_argument(
        "--apply", action="store_true",
        help="write the migration (default is read-only dry-run)")
    return parser.parse_args()


def _load_settings(path: Path) -> Settings:
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"settings are not a JSON object: {path}")
    return Settings.from_dict(data)


def _fresh_legacy_match(
        directory: Path, name: str,
        rec: Dict) -> Tuple[bool, os.stat_result | None]:
    if rec.get("status") != "matched":
        return False, None
    if rec.get("search_profile_hash"):
        return False, None
    if isinstance(rec.get("hydrus_output"), dict):
        return False, None
    path = directory / name
    try:
        st = path.stat()
    except OSError:
        return False, None
    if rec.get("size") != st.st_size:
        return False, st
    stored_ns = rec.get("mtime_ns")
    try:
        if stored_ns is not None:
            return int(stored_ns) == st.st_mtime_ns, st
        fresh = (
            abs(float(rec.get("mtime", -1)) - st.st_mtime)
            <= Ledger.MTIME_EPS)
        return fresh, st
    except (TypeError, ValueError):
        return False, st


def _current_sha_by_md5(
        integrator: TagIntegrator, md5s: List[str]) -> Dict[str, str]:
    """Resolve MD5s to SHA-256s, retaining only current local Hydrus files."""
    resolved: Dict[str, str] = {}
    wanted = sorted({value.lower() for value in md5s})
    for offset in range(0, len(wanted), _HYDRUS_BATCH):
        batch = wanted[offset:offset + _HYDRUS_BATCH]
        try:
            mapping_r = integrator.session.get(
                f"{integrator.hydrus_api_url}/get_files/file_hashes",
                headers=integrator._hydrus_headers(),
                params={
                    "hashes": json.dumps(batch),
                    "source_hash_type": "md5",
                    "desired_hash_type": "sha256",
                },
                timeout=30,
            )
            search_r = integrator.session.get(
                f"{integrator.hydrus_api_url}/get_files/search_files",
                headers=integrator._hydrus_headers(),
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
            raw_mapping = mapping_r.json().get("hashes") or {}
            current = {
                value.lower() for value in (search_r.json().get("hashes") or [])
                if isinstance(value, str) and _SHA256_RE.fullmatch(value)
            }
        except Exception as exc:
            raise RuntimeError(
                "could not resolve legacy MD5s through Hydrus") from exc

        for md5, sha256 in raw_mapping.items():
            if not (isinstance(md5, str) and isinstance(sha256, str)):
                continue
            md5 = md5.lower()
            sha256 = sha256.lower()
            if sha256 in current:
                resolved[md5] = sha256
    return resolved


def main() -> int:
    args = _parse_args()
    root = args.root.expanduser().resolve()
    if not root.is_dir() or root.parent == root:
        raise SystemExit(f"refusing invalid/broad scan root: {root}")

    settings = _load_settings(args.settings.expanduser().resolve())
    integrator = TagIntegrator(settings=settings)
    integrator.load_credentials_from_store(CredentialStore())
    if not (integrator.has_hydrus and integrator.hydrus_can_search_files):
        raise SystemExit(
            "Hydrus must be connected with Search for and Fetch Files permission")

    search_profile = integrator.search_profile_hash()
    if (args.expected_search_profile
            and search_profile != args.expected_search_profile):
        raise SystemExit(
            "effective search profile differs from the expected scan profile: "
            f"{search_profile} != {args.expected_search_profile}")
    scope_id = integrator._hydrus_scope_id()
    policy_hash = integrator.hydrus_output_policy_hash()
    if not scope_id:
        raise SystemExit("Hydrus scope is unavailable; refusing migration")

    ledgers: Dict[Path, Dict] = {}
    candidates: List[
        Tuple[Path, str, Dict, os.stat_result, Optional[str]]] = []
    legacy_without_hash = 0
    metadata_incomplete = 0
    stale_or_missing = 0
    require_direct_notes = integrator.direct_notes_effective()
    for directory, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        if LEDGER_FILE not in files:
            continue
        ledger_path = Path(directory) / LEDGER_FILE
        try:
            data = json.loads(ledger_path.read_text("utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
            continue
        ledgers[ledger_path] = data
        for name, rec in data["records"].items():
            if not isinstance(rec, dict) or rec.get("status") != "matched":
                continue
            if rec.get("search_profile_hash") or isinstance(
                    rec.get("hydrus_output"), dict):
                continue
            if require_direct_notes and (
                    rec.get("metadata_version") != LEDGER_METADATA_VERSION
                    or rec.get("direct_notes_applied") is not True):
                metadata_incomplete += 1
                continue
            fresh, st = _fresh_legacy_match(Path(directory), name, rec)
            if not fresh or st is None:
                stale_or_missing += 1
                continue
            sha256 = rec.get("sha256")
            if isinstance(sha256, str) and _SHA256_RE.fullmatch(sha256):
                candidates.append(
                    (ledger_path, name, rec, st, sha256.lower()))
                continue
            md5 = rec.get("md5")
            if isinstance(md5, str) and _MD5_RE.fullmatch(md5):
                candidates.append((ledger_path, name, rec, st, None))
            else:
                legacy_without_hash += 1

    current_hashes = integrator._hydrus_current_sha256s(
        sha256 for _, _, _, _, sha256 in candidates if sha256)
    if current_hashes is None:
        raise SystemExit("could not verify current Hydrus hashes; no changes made")
    md5_candidates = [
        str(rec["md5"]).lower()
        for _, _, rec, _, sha256 in candidates if sha256 is None]
    try:
        md5_to_sha = _current_sha_by_md5(integrator, md5_candidates)
    except RuntimeError as exc:
        raise SystemExit(f"{exc}; no changes made") from exc

    eligible: List[Tuple[Path, str, Dict, os.stat_result, str]] = []
    for ledger_path, name, rec, st, stored_sha in candidates:
        verified_sha = stored_sha if stored_sha in current_hashes else None
        if stored_sha is None:
            verified_sha = md5_to_sha.get(str(rec.get("md5", "")).lower())
        if verified_sha:
            eligible.append((ledger_path, name, rec, st, verified_sha))
    not_current = len(candidates) - len(eligible)
    sidecar_complete = sum(
        1 for ledger_path, name, _, _, _ in eligible
        if integrator.has_sidecar(ledger_path.parent / name))

    print(f"Scan root: {root}")
    print(f"Search profile: {search_profile}")
    print(f"Hydrus scope: {scope_id}")
    print(f"Legacy matches eligible for scoped migration: {len(eligible):,}")
    print(f"  with a currently recognized sidecar: {sidecar_complete:,}")
    print(f"  missing required/current sidecar: {len(eligible) - sidecar_complete:,}")
    print(f"Legacy matches without usable SHA-256/MD5 (left open): "
          f"{legacy_without_hash:,}")
    print(f"Legacy matches needing metadata backfill (left open): "
          f"{metadata_incomplete:,}")
    print(f"Legacy hashes not current in Hydrus (left open): {not_current:,}")
    print(f"Stale or missing local files (left untouched): {stale_or_missing:,}")
    if not args.apply:
        print("DRY RUN: no files changed. Re-run with --apply to migrate.")
        return 0
    if not eligible:
        print("Nothing eligible; no files changed.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = root / f".furtag_legacy_migration_backup_{stamp}"
    changed_ledgers = sorted({row[0] for row in eligible})
    for ledger_path in changed_ledgers:
        relative = ledger_path.relative_to(root)
        backup_path = backup_root / relative
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ledger_path, backup_path)

    now = time.time()
    for ledger_path, name, rec, st, sha256 in eligible:
        rec["sha256"] = sha256
        rec["search_profile_hash"] = search_profile
        rec["mtime_ns"] = st.st_mtime_ns
        rec["legacy_local_migration"] = True
        rec["hydrus_output"] = {
            "scope_id": scope_id,
            "policy_hash": policy_hash,
            "import_state": "live",
            "metadata_state": "applied_original",
            "sha256": sha256,
            "target_hashes": [],
            "complete": True,
            "updated_at": now,
            "legacy_local_migration": True,
        }

    for ledger_path in changed_ledgers:
        data = ledgers[ledger_path]
        # Never upgrade a directory seal by assumption. The next scan performs
        # normal per-file checks (including required sidecars) and reseals only
        # directories that are genuinely complete under current policy.
        data.pop("dir_fingerprint", None)
        atomic_write_text(
            ledger_path,
            json.dumps(data, ensure_ascii=False, indent=0) + "\n")

    print(f"Migrated {len(eligible):,} match record(s) in "
          f"{len(changed_ledgers):,} ledger(s).")
    print(f"Backup: {backup_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
