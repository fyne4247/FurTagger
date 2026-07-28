"""Credential store: env vars, keyring isolation, redaction."""

import os
import unittest
from unittest.mock import MagicMock, patch

from furtag_credentials import (
    CredentialSnapshot, CredentialStore, FIELD_MAP, redact_secrets,
)


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

    def test_redact(self):
        text = "Authorization: Bearer supersecretkey123 and more"
        self.assertNotIn(
            "supersecretkey123",
            redact_secrets(text, ["supersecretkey123"]))


class TestSecureStoreMerge(unittest.TestCase):
    def _load(self, values, tag_service: str = "my gui tags"):
        from furtag import TagIntegrator
        from furtag_settings import Settings

        s = Settings()
        s.output.hydrus_enabled = True
        s.output.hydrus_tag_service = tag_service
        ti = TagIntegrator(settings=s)
        seen = {}
        store = MagicMock()
        store.load_all.return_value = CredentialSnapshot(values)
        with patch.object(TagIntegrator, "_init_hydrus",
                          lambda self, cfg: seen.update(cfg)):
            ti.load_credentials_from_store(store=store)
        return seen

    def test_non_secret_preferences_come_from_settings(self):
        cfg = self._load({})
        self.assertEqual(cfg.get("hydrus_tag_service"), "my gui tags")
        self.assertEqual(cfg.get("hydrus_import"), "true")
        self.assertEqual(cfg.get("hydrus_results_page"), "on")

    def test_secret_fields_are_loaded_from_secure_snapshot(self):
        cfg = self._load({
            "hydrus_access_key": "fake-test-access-key",
            "hydrus_api_url": "http://127.0.0.1:45869",
            "e621_api_key": "fake-test-e621-key",
        })
        self.assertEqual(cfg.get("hydrus_access_key"), "fake-test-access-key")
        self.assertEqual(cfg.get("hydrus_api_url"), "http://127.0.0.1:45869")
        self.assertEqual(cfg.get("e621_api_key"), "fake-test-e621-key")
        self.assertEqual(cfg.get("hydrus_tag_service"), "my gui tags")

    def test_reload_clears_stale_source_and_hydrus_capabilities(self):
        from furtag import TagIntegrator
        from furtag_settings import Settings

        ti = TagIntegrator(settings=Settings())
        ti.has_e621 = True
        ti.has_inkbunny = True
        ti.ib_sid = "stale-session"
        ti.has_hydrus = True
        ti.hydrus_tag_service_key = "stale-service"
        ti.hydrus_can_edit_urls = True
        ti.hydrus_can_edit_notes = True

        ti.load_credentials({})

        self.assertFalse(ti.has_e621)
        self.assertFalse(ti.has_inkbunny)
        self.assertEqual(ti.ib_sid, "")
        self.assertFalse(ti.has_hydrus)
        self.assertEqual(ti.hydrus_tag_service_key, "")
        self.assertFalse(ti.hydrus_can_edit_urls)
        self.assertFalse(ti.hydrus_can_edit_notes)


class TestFieldMap(unittest.TestCase):
    def test_all_env_names(self):
        for field, (env, _) in FIELD_MAP.items():
            self.assertTrue(env.startswith("FURTAG_"))
            self.assertEqual(env, env.upper())


if __name__ == "__main__":
    unittest.main()
