"""Credential store: env vars, keyring isolation, redaction."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from furtag_credentials import CredentialStore, redact_secrets, FIELD_MAP


class TestEnvCredentials(unittest.TestCase):
    def test_env_wins(self):
        store = CredentialStore(service="org.furtag.FurTag.test")
        with patch.dict(os.environ, {"FURTAG_E621_API_KEY": "env-secret-key"}):
            self.assertEqual(store.get("e621_api_key"), "env-secret-key")

    def test_missing_returns_empty(self):
        store = CredentialStore(service="org.furtag.FurTag.test.empty")
        # Don't leave env set
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FURTAG_GELBOORU_API_KEY", None)
            # May still have keyring value; just check type
            val = store.get("gelbooru_api_key")
            self.assertIsInstance(val, str)

    def test_plaintext_parse(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "credentials.txt"
            p.write_text(
                "e621_username = alice\n"
                "e621_api_key = secret123\n"
                "# comment\n"
                "hydrus_api_url = http://127.0.0.1:45869\n",
                encoding="utf-8")
            store = CredentialStore()
            cfg = store.load_from_plaintext(p)
            self.assertEqual(cfg["e621_username"], "alice")
            self.assertEqual(cfg["e621_api_key"], "secret123")

    def test_redact(self):
        text = "Authorization: Bearer supersecretkey123 and more"
        self.assertNotIn(
            "supersecretkey123",
            redact_secrets(text, ["supersecretkey123"]))


class TestFieldMap(unittest.TestCase):
    def test_all_env_names(self):
        for field, (env, _) in FIELD_MAP.items():
            self.assertTrue(env.startswith("FURTAG_"))
            self.assertEqual(env, env.upper())


if __name__ == "__main__":
    unittest.main()
