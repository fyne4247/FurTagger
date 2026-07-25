#!/usr/bin/env python3
"""PRIVATE one-time migration: credentials.txt → OS keyring.

Run explicitly on the owner's machine only. Do NOT ship in release builds,
document for end users, or leave wired into the GUI/CLI entry points.

Usage (from repo root):
    .venv/bin/python scripts/migrate_credentials.py
    .venv/bin/python scripts/migrate_credentials.py --credentials /path/to/credentials.txt
    .venv/bin/python scripts/migrate_credentials.py --verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from furtag_credentials import CredentialStore, ALL_FIELDS, SECRET_FIELDS


def main() -> int:
    ap = argparse.ArgumentParser(description="Migrate credentials.txt → keyring")
    ap.add_argument(
        "--credentials",
        type=Path,
        default=_ROOT / "credentials.txt",
        help="Path to legacy credentials.txt",
    )
    ap.add_argument(
        "--verify",
        action="store_true",
        help="Print which fields are present in keyring/env (secrets redacted)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without writing",
    )
    args = ap.parse_args()
    store = CredentialStore()

    if args.verify:
        snap = store.load_all()
        print("Resolved credential fields (secrets redacted):")
        for f in ALL_FIELDS:
            val = snap.get(f)
            if not val:
                print(f"  {f}: (empty)")
            elif f in SECRET_FIELDS:
                print(f"  {f}: *** ({len(val)} chars)")
            else:
                print(f"  {f}: {val}")
        usable, msg = store.keyring_status()
        print(f"Keyring: {msg}" if usable else f"Keyring unavailable: {msg}")
        return 0

    if not args.credentials.exists():
        print(f"No file at {args.credentials}")
        return 1

    cfg = store.load_from_plaintext(args.credentials)
    known = {k: v for k, v in cfg.items() if k in ALL_FIELDS and v}
    # aliases
    if "hydrus_api_url" not in known and cfg.get("hydrus_url"):
        known["hydrus_api_url"] = cfg["hydrus_url"]
    if "hydrus_access_key" not in known and cfg.get("hydrus_api_key"):
        known["hydrus_access_key"] = cfg["hydrus_api_key"]

    print(f"Found {len(known)} field(s) in {args.credentials.name}:")
    for k in known:
        if k in SECRET_FIELDS:
            print(f"  {k}: ***")
        else:
            print(f"  {k}: {known[k]}")

    if args.dry_run:
        print("Dry run — nothing written.")
        return 0

    n, errors = store.migrate_from_plaintext(args.credentials)
    print(f"Imported {n} field(s) into keyring.")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  {e}")
        return 1
    print("Verify with: python scripts/migrate_credentials.py --verify")
    print("After verifying, delete credentials.txt and remove this helper "
          "before any distributable build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
