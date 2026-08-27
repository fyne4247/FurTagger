"""Credential storage for FurTag — keyring + environment variables.

Resolution order for each secret field:
1. Environment variable ``FURTAG_<KEY>`` (uppercase, e.g. FURTAG_E621_API_KEY)
2. OS keyring item under service ``org.furtag.FurTag``

Setting ``FURTAG_DISABLE_KEYRING=1`` skips step 2 entirely, so no keychain
access ever happens; see ``keyring_disabled``.

Secrets never enter settings.json, progress events, or logs.

All fields live in a *single* keyring item (account ``credentials_v1``) holding
a JSON object. Earlier versions used one item per field, which on macOS meant
one authorization prompt per field — granting "Always Allow" on one item left
the rest still queued. ``migrate_legacy_items`` folds those old items into the
blob; see ``packaging/README.md`` for why the app must be code-signed with a
stable identity for that authorization to persist across rebuilds.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

KEYRING_SERVICE = "org.furtag.FurTag"

# Single keyring account holding the JSON blob of every field.
BLOB_ACCOUNT = "credentials_v1"

# Set to 1/true/yes to bypass the OS keyring completely and resolve every field
# from FURTAG_* environment variables alone. This exists for running from source
# on macOS: an ad-hoc-signed Python has no stable code identity, so the keychain
# cannot remember an "Always Allow" grant and re-prompts on every launch. See
# .env.example and packaging/README.md.
DISABLE_KEYRING_ENV = "FURTAG_DISABLE_KEYRING"


def keyring_disabled() -> bool:
    """True when the user has opted out of the OS keyring entirely."""
    return os.environ.get(DISABLE_KEYRING_ENV, "").strip().lower() in {
        "1", "true", "yes", "on"}


# Where the credential file lives when the keyring is bypassed. FurTag.command
# sources this file before launching, so values reach the app as FURTAG_*
# variables; writing it back is what makes the Credentials dialog work with the
# keyring off. Override for packaged builds that run outside the project dir.
ENV_FILE_ENV = "FURTAG_ENV_FILE"

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?(FURTAG_[A-Z0-9_]+)\s*=")


def env_file_path() -> Path:
    """Absolute path of the .env file the keyring-disabled backend writes.

    Source checkouts keep it beside the code, which is where ``FurTag.command``
    sources it from. A frozen build cannot: ``__file__`` there points inside a
    read-only temp extraction dir, so it uses the same user config directory
    that settings.json lives in.
    """
    override = os.environ.get(ENV_FILE_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    if getattr(sys, "frozen", False):
        try:
            from platformdirs import user_config_dir
            base = Path(user_config_dir("FurTag", "FurTag"))
        except ImportError:
            base = Path.home() / ".config" / "FurTag"
        base.mkdir(parents=True, exist_ok=True)
        return base / ".env"
    return Path(__file__).resolve().parent / ".env"


def _shell_quote(value: str) -> str:
    """Quote a value so `. ./.env` reproduces it byte for byte."""
    if value and not re.search(r"""[\s#"'$`\\!*?~<>|&;()\[\]{}]""", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"

# Windows Credential Locker caps a credential blob at CRED_MAX_CREDENTIAL_BLOB_SIZE
# (2560 bytes, stored as UTF-16). Refuse to write anything close to that ceiling
# rather than letting the OS truncate or reject the item silently.
MAX_BLOB_CHARS = 1200

# Logical field name → (environment variable, legacy per-field keyring account)
# The legacy account name is retained so pre-consolidation items can be migrated.
FIELD_MAP = {
    "e621_username": ("FURTAG_E621_USERNAME", "e621_username"),
    "e621_api_key": ("FURTAG_E621_API_KEY", "e621_api_key"),
    "inkbunny_username": ("FURTAG_INKBUNNY_USERNAME", "inkbunny_username"),
    "inkbunny_password": ("FURTAG_INKBUNNY_PASSWORD", "inkbunny_password"),
    "danbooru_username": ("FURTAG_DANBOORU_USERNAME", "danbooru_username"),
    "danbooru_api_key": ("FURTAG_DANBOORU_API_KEY", "danbooru_api_key"),
    "gelbooru_user_id": ("FURTAG_GELBOORU_USER_ID", "gelbooru_user_id"),
    "gelbooru_api_key": ("FURTAG_GELBOORU_API_KEY", "gelbooru_api_key"),
    "sauce_nao_api_key": ("FURTAG_SAUCE_NAO_API_KEY", "sauce_nao_api_key"),
    "hydrus_api_url": ("FURTAG_HYDRUS_API_URL", "hydrus_api_url"),
    "hydrus_access_key": ("FURTAG_HYDRUS_ACCESS_KEY", "hydrus_access_key"),
}

# Fields that are secrets (must never be logged).
SECRET_FIELDS = {
    "e621_api_key", "inkbunny_password", "danbooru_api_key",
    "gelbooru_api_key", "sauce_nao_api_key", "hydrus_access_key",
}

# Non-secret companion fields stored in keyring too for convenience (usernames).
ALL_FIELDS = list(FIELD_MAP.keys())


@dataclass
class CredentialSnapshot:
    """Plain dict of resolved credentials (in-memory only)."""
    values: Dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default) or default

    def as_cfg(self) -> Dict[str, str]:
        """Lower-case keys consumed by TagIntegrator."""
        return {k.lower(): v for k, v in self.values.items() if v}


class CredentialStore:
    """Keyring + env-var credential backend."""

    def __init__(self, service: str = KEYRING_SERVICE) -> None:
        self.service = service
        self._keyring_error: Optional[str] = None
        # Cached blob so a load_all() costs one keyring read, not one per field.
        self._blob: Optional[Dict[str, str]] = None

    # ── keyring plumbing ─────────────────────────────────────────────────

    def keyring_status(self) -> Tuple[bool, str]:
        """Return (usable, message), including the last read error if any."""
        if keyring_disabled():
            return False, (f"Keyring disabled ({DISABLE_KEYRING_ENV}=1); "
                           "credentials come from FURTAG_* variables")
        try:
            import keyring
            backend = keyring.get_keyring()
            name = type(backend).__name__
            if "fail" in name.lower() or "null" in name.lower():
                return False, f"No usable keyring backend ({name})"
            if self._keyring_error:
                return True, f"Keyring: {name} (last error: {self._keyring_error})"
            return True, f"Keyring: {name}"
        except Exception as e:
            return False, f"Keyring unavailable: {e}"

    def refresh(self) -> None:
        """Drop the cached blob so the next read hits the keyring again."""
        self._blob = None

    def _read_blob(self) -> Dict[str, str]:
        """Load (and cache) the consolidated JSON item. Never raises."""
        if self._blob is not None:
            return self._blob
        if keyring_disabled():
            self._blob = {
                f: v for f in FIELD_MAP
                if (v := os.environ.get(FIELD_MAP[f][0], "").strip())}
            return self._blob
        blob: Dict[str, str] = {}
        try:
            import keyring
            raw = keyring.get_password(self.service, BLOB_ACCOUNT)
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    blob = {k: str(v) for k, v in parsed.items()
                            if k in FIELD_MAP and v}
        except json.JSONDecodeError as e:
            # A corrupt item must not wedge the app — env vars still resolve.
            self._keyring_error = f"corrupt credential item: {e}"
        except Exception as e:
            self._keyring_error = str(e)
        self._blob = blob
        return blob

    def _write_blob(self, blob: Dict[str, str]) -> None:
        """Persist the blob, but only when it differs from what is stored.

        keyring's macOS backend implements a write as delete-then-add, which
        discards the item's access-control list along with any "Always Allow"
        the user granted. Skipping no-op writes keeps that authorization alive.
        """
        clean = {k: v for k, v in blob.items() if k in FIELD_MAP and v}
        if clean == self._read_blob():
            return
        if keyring_disabled():
            self._write_env_file(clean)
            return
        import keyring
        if not clean:
            self._blob = {}
            try:
                keyring.delete_password(self.service, BLOB_ACCOUNT)
            except Exception:
                pass
            return
        raw = json.dumps(clean, separators=(",", ":"), sort_keys=True)
        if len(raw) > MAX_BLOB_CHARS:
            raise ValueError(
                f"credentials exceed the {MAX_BLOB_CHARS}-character keyring "
                f"limit ({len(raw)} used); shorten or unset a field")
        keyring.set_password(self.service, BLOB_ACCOUNT, raw)
        self._blob = clean

    def _write_env_file(self, clean: Dict[str, str]) -> None:
        """Rewrite the .env file in place, then update the live environment.

        Existing comments, ordering, and any non-FurTag lines are preserved, so
        the file stays the readable thing the user may also edit by hand. Fields
        with no value are kept as empty assignments rather than deleted — that
        documents what is available to set.
        """
        path = env_file_path()
        wanted = {FIELD_MAP[f][0]: clean.get(f, "") for f in FIELD_MAP}

        try:
            existing = path.read_text().splitlines()
        except FileNotFoundError:
            existing = [
                "# FurTag credentials — written by the Credentials dialog.",
                "# Gitignored. Never commit or share this file.",
                "",
                f"{DISABLE_KEYRING_ENV}=1",
                "",
            ]
        except OSError as e:
            raise ValueError(f"could not read {path}: {e}") from e

        seen, out = set(), []
        for line in existing:
            m = _ENV_LINE.match(line)
            if m and m.group(1) in wanted:
                name = m.group(1)
                seen.add(name)
                out.append(f"{name}={_shell_quote(wanted[name])}")
            else:
                out.append(line)
        for name, value in wanted.items():
            if name not in seen:
                out.append(f"{name}={_shell_quote(value)}")

        tmp = path.with_name(path.name + ".tmp")
        try:
            # Create the temp file already private — never widen, even briefly.
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write("\n".join(out).rstrip("\n") + "\n")
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except OSError as e:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise ValueError(f"could not write {path}: {e}") from e

        # The launcher only sources .env at startup, so the running process
        # needs its environment updated for the new values to take effect now.
        for name, value in wanted.items():
            if value:
                os.environ[name] = value
            else:
                os.environ.pop(name, None)
        self._blob = dict(clean)

    # ── field access ─────────────────────────────────────────────────────

    def get(self, field: str) -> str:
        """Resolve one field: env → keyring."""
        if field not in FIELD_MAP:
            return ""
        env_name, _ = FIELD_MAP[field]
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
        return self._read_blob().get(field, "").strip()

    def set(self, field: str, value: str) -> None:
        if field not in FIELD_MAP:
            raise KeyError(field)
        value = (value or "").strip()
        if not value:
            self.delete(field)
            return
        blob = dict(self._read_blob())
        blob[field] = value
        self._write_blob(blob)

    def delete(self, field: str) -> None:
        if field not in FIELD_MAP:
            return
        blob = dict(self._read_blob())
        if blob.pop(field, None) is None:
            return
        try:
            self._write_blob(blob)
        except Exception:
            pass

    def load_all(self) -> CredentialSnapshot:
        values = {f: self.get(f) for f in ALL_FIELDS}
        return CredentialSnapshot(values=values)

    def save_fields(self, updates: Dict[str, str]) -> List[str]:
        """Save non-empty updates; empty string removes. Returns error messages.

        All fields are applied to the blob together so a Save costs at most one
        keyring write — and no write at all when nothing actually changed.
        """
        errors: List[str] = []
        blob = dict(self._read_blob())
        for field, value in updates.items():
            if field not in FIELD_MAP:
                continue
            text = "" if value is None else str(value).strip()
            if text:
                blob[field] = text
            else:
                blob.pop(field, None)
        try:
            self._write_blob(blob)
        except Exception as e:
            errors.append(str(e))
        return errors

    def delete_all(self) -> None:
        """Remove every stored credential, consolidated and legacy alike."""
        try:
            self._write_blob({})
        except Exception:
            pass
        if keyring_disabled():
            return
        self._delete_legacy_items()

    # ── migration from the pre-consolidation layout ──────────────────────

    def legacy_fields(self) -> List[str]:
        """Field names that still have a legacy per-field keyring item.

        On macOS this probes item *attributes* via the `security` CLI, which
        does not read the secret and so does not raise an authorization prompt.
        Elsewhere the backends prompt-free, so a plain read is fine.
        """
        found: List[str] = []
        if keyring_disabled():
            return found
        if sys.platform == "darwin":
            for name in ALL_FIELDS:
                try:
                    rc = subprocess.run(
                        ["security", "find-generic-password",
                         "-s", self.service, "-a", name],
                        capture_output=True, timeout=10).returncode
                except (OSError, subprocess.SubprocessError):
                    return []
                if rc == 0:
                    found.append(name)
            return found
        try:
            import keyring
            for name in ALL_FIELDS:
                if keyring.get_password(self.service, name):
                    found.append(name)
        except Exception as e:
            self._keyring_error = str(e)
            return []
        return found

    def needs_migration(self) -> bool:
        """True when legacy items exist and the consolidated item does not."""
        if keyring_disabled():
            return False
        if self._read_blob():
            return False
        return bool(self.legacy_fields())

    def migrate_legacy_items(self) -> Tuple[int, List[str]]:
        """Fold legacy per-field items into the blob, then delete them.

        Returns (fields_migrated, errors). Reading the legacy items is what
        triggers macOS's one-time burst of authorization prompts; the blob this
        writes is created by — and therefore automatically trusted by — the
        running app.
        """
        errors: List[str] = []
        names = self.legacy_fields()
        if not names:
            return 0, errors

        migrated: Dict[str, str] = {}
        try:
            import keyring
        except Exception as e:
            return 0, [f"keyring unavailable: {e}"]

        for name in names:
            try:
                val = (keyring.get_password(self.service, name) or "").strip()
            except Exception as e:
                errors.append(f"{name}: {e}")
                continue
            if val:
                migrated[name] = val

        if migrated:
            blob = dict(self._read_blob())
            # Anything already in the blob wins; it is the newer value.
            for k, v in migrated.items():
                blob.setdefault(k, v)
            try:
                self._write_blob(blob)
            except Exception as e:
                # Leave the legacy items in place so nothing is lost.
                return 0, errors + [f"could not write consolidated item: {e}"]

        errors.extend(self._delete_legacy_items())
        return len(migrated), errors

    def _delete_legacy_items(self) -> List[str]:
        errors: List[str] = []
        try:
            import keyring
        except Exception:
            return errors
        for name in ALL_FIELDS:
            try:
                keyring.delete_password(self.service, name)
            except Exception:
                # Absent items raise; that is the normal case, not an error.
                pass
        return errors


def redact_secrets(text: str, secrets: Optional[List[str]] = None) -> str:
    """Best-effort redaction of known secret substrings from log text."""
    if not text:
        return text
    out = text
    for s in secrets or []:
        if s and len(s) >= 4 and s in out:
            out = out.replace(s, "***")
    return out
