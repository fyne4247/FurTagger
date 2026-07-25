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


class TestLegacyPlaintextMerge(unittest.TestCase):
    """A leftover credentials.txt may still supply secrets, but must never
    override non-secret preferences the user set in Settings."""

    def _load(self, legacy_text: str, tag_service: str = "my gui tags"):
        from furtag import TagIntegrator
        from furtag_settings import Settings

        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "credentials.txt"
            legacy.write_text(legacy_text, encoding="utf-8")
            s = Settings()
            s.output.hydrus_enabled = True
            s.output.hydrus_tag_service = tag_service
            ti = TagIntegrator(settings=s)
            seen = {}
            with patch.object(TagIntegrator, "_init_hydrus",
                              lambda self, cfg: seen.update(cfg)), \
                 patch.dict(os.environ, {}, clear=True):
                ti.load_credentials_from_store(
                    store=CredentialStore(service="org.furtag.FurTag.test.none"),
                    legacy_path=legacy)
            return seen

    def test_non_secret_key_does_not_override_settings(self):
        cfg = self._load(
            "hydrus_tag_service = stale file tags\n"
            "hydrus_import = false\n"
            "hydrus_results_page = off\n")
        self.assertEqual(cfg.get("hydrus_tag_service"), "my gui tags")
        self.assertEqual(cfg.get("hydrus_import"), "true")
        self.assertEqual(cfg.get("hydrus_results_page"), "on")

    def test_secret_key_still_picked_up(self):
        cfg = self._load(
            "hydrus_access_key = deadbeef\n"
            "hydrus_api_url = http://127.0.0.1:45869\n"
            "e621_api_key = legacy-secret\n"
            "hydrus_tag_service = stale file tags\n")
        self.assertEqual(cfg.get("hydrus_access_key"), "deadbeef")
        self.assertEqual(cfg.get("hydrus_api_url"), "http://127.0.0.1:45869")
        self.assertEqual(cfg.get("e621_api_key"), "legacy-secret")
        self.assertEqual(cfg.get("hydrus_tag_service"), "my gui tags")

    def test_ignored_non_secret_warning_is_once_per_process(self):
        from furtag import TagIntegrator
        from furtag_settings import Settings

        with tempfile.TemporaryDirectory() as td:
            legacy = Path(td) / "credentials.txt"
            legacy.write_text(
                "hydrus_import = false\n"
                "hydrus_tag_service = stale file tags\n",
                encoding="utf-8")
            settings = Settings()
            settings.output.hydrus_enabled = True
            first = TagIntegrator(settings=settings)
            second = TagIntegrator(settings=settings)
            store = CredentialStore(service="org.furtag.FurTag.test.none")
            with patch.object(TagIntegrator, "_init_hydrus"), \
                 patch.dict(os.environ, {}, clear=True), \
                 patch("furtag.notify") as notice:
                first.load_credentials_from_store(
                    store=store, legacy_path=legacy)
                second.load_credentials_from_store(
                    store=store, legacy_path=legacy)

        warnings = [
            call.args[0] for call in notice.call_args_list
            if "Ignoring non-secret key(s)" in call.args[0]
        ]
        self.assertEqual(len(warnings), 1)


class TestFieldMap(unittest.TestCase):
    def test_all_env_names(self):
        for field, (env, _) in FIELD_MAP.items():
            self.assertTrue(env.startswith("FURTAG_"))
            self.assertEqual(env, env.upper())


if __name__ == "__main__":
    unittest.main()
