"""Credential storage for FurTag — keyring + environment variables.

Resolution order for each secret field:
1. Environment variable ``FURTAG_<KEY>`` (uppercase, e.g. FURTAG_E621_API_KEY)
2. OS keyring item under service ``org.furtag.FurTag``

Secrets never enter settings.json, progress events, or logs.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

KEYRING_SERVICE = "org.furtag.FurTag"

# Logical field name → (environment variable, keyring item name)
# keyring stores one secret per (service, username) pair; username is the field id.
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

    def keyring_status(self) -> Tuple[bool, str]:
        """Return (usable, message), including the last read error if any."""
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

    def get(self, field: str) -> str:
        """Resolve one field: env → keyring."""
        if field not in FIELD_MAP:
            return ""
        env_name, _ = FIELD_MAP[field]
        env_val = os.environ.get(env_name, "").strip()
        if env_val:
            return env_val
        try:
            import keyring
            val = keyring.get_password(self.service, field)
            return (val or "").strip()
        except Exception as e:
            self._keyring_error = str(e)
            return ""

    def set(self, field: str, value: str) -> None:
        if field not in FIELD_MAP:
            raise KeyError(field)
        value = (value or "").strip()
        if not value:
            self.delete(field)
            return
        import keyring
        keyring.set_password(self.service, field, value)

    def delete(self, field: str) -> None:
        if field not in FIELD_MAP:
            return
        try:
            import keyring
            keyring.delete_password(self.service, field)
        except Exception:
            pass

    def load_all(self) -> CredentialSnapshot:
        values = {f: self.get(f) for f in ALL_FIELDS}
        return CredentialSnapshot(values=values)

    def save_fields(self, updates: Dict[str, str]) -> List[str]:
        """Save non-empty updates; empty string removes. Returns error messages."""
        errors: List[str] = []
        for field, value in updates.items():
            if field not in FIELD_MAP:
                continue
            try:
                if value is None or str(value).strip() == "":
                    self.delete(field)
                else:
                    self.set(field, str(value))
            except Exception as e:
                errors.append(f"{field}: {e}")
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
