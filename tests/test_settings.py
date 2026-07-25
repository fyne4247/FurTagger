"""Unit tests for settings, sidecar patterns, and preflight validation."""

import json
import tempfile
import unittest
from pathlib import Path

from furtag_settings import (
    Settings,
    SettingsStore,
    SidecarPatternError,
    validate_run_preflight,
    validate_sidecar_pattern,
    render_sidecar_name,
    DEFAULT_SAUCENAO_MIN_SIMILARITY,
    DEFAULT_SAUCENAO_AUTH_SIMILARITY,
)


class TestSidecarPatterns(unittest.TestCase):
    def test_default_patterns_ok(self):
        validate_sidecar_pattern("{name}{ext}.txt")
        validate_sidecar_pattern("{name}{ext}.urls.txt")
        validate_sidecar_pattern("{name}{ext}.json", for_json=True)

    def test_missing_ext_rejected(self):
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("{name}.txt")

    def test_path_sep_rejected(self):
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("{name}/{ext}.txt")
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("..\\{name}{ext}.txt")

    def test_media_self_rejected(self):
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("{name}{ext}")

    def test_empty_rejected(self):
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("")

    def test_render(self):
        p = Path("/tmp/cat.jpg")
        self.assertEqual(
            render_sidecar_name("{name}{ext}.txt", p), "cat.jpg.txt")
        self.assertEqual(
            render_sidecar_name("{name}{ext}.urls.txt", p), "cat.jpg.urls.txt")


class TestSettingsStore(unittest.TestCase):
    def test_defaults_match_baseline(self):
        s = Settings()
        self.assertEqual(s.matching.saucenao_min_similarity,
                         DEFAULT_SAUCENAO_MIN_SIMILARITY)
        self.assertEqual(s.matching.saucenao_auth_similarity,
                         DEFAULT_SAUCENAO_AUTH_SIMILARITY)
        self.assertEqual(s.matching.fluffle_review_mode, "off")
        self.assertTrue(s.matching.fluffle_tossup_e621_only)
        self.assertEqual(s.matching.fluffle_accepted_matches, ["exact"])
        self.assertFalse(s.output.sidecars_enabled)
        self.assertTrue(s.output.hydrus_enabled)
        self.assertEqual(s.output.sidecar_format, "txt")

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            store = SettingsStore(path)
            s = Settings()
            s.matching.saucenao_min_similarity = 75.0
            s.sources.e621_enabled = False
            store.save(s)
            loaded = store.load()
            self.assertEqual(loaded.matching.saucenao_min_similarity, 75.0)
            self.assertFalse(loaded.sources.e621_enabled)

    def test_forward_compat_unknown_keys(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({
                "version": 99,
                "output": {"hydrus_enabled": False, "future_key": 1},
                "mystery": True,
            }), encoding="utf-8")
            loaded = SettingsStore(path).load()
            self.assertFalse(loaded.output.hydrus_enabled)
            # Missing sections filled with defaults
            self.assertTrue(loaded.sources.e621_enabled)

    def test_auth_clamped_to_min(self):
        s = Settings.from_dict({
            "matching": {
                "saucenao_min_similarity": 90,
                "saucenao_auth_similarity": 70,
            }
        })
        self.assertGreaterEqual(
            s.matching.saucenao_auth_similarity,
            s.matching.saucenao_min_similarity)


class TestPreflight(unittest.TestCase):
    def test_both_sinks_off(self):
        s = Settings()
        s.output.hydrus_enabled = False
        s.output.sidecars_enabled = False
        errs = validate_run_preflight(
            s, hydrus_available=False, any_source_available=True)
        self.assertTrue(any("output sink" in e.lower() or "sidecars" in e.lower()
                            for e in errs))

    def test_defaults_without_hydrus_still_ok(self):
        """Classic fallback: hydrus wanted but unavailable → write sidecars."""
        s = Settings()  # hydrus_enabled=True, sidecars_enabled=False
        errs = validate_run_preflight(
            s, hydrus_available=False, any_source_available=True)
        self.assertEqual(errs, [])

    def test_all_sources_disabled(self):
        s = Settings()
        s.sources.e621_enabled = False
        s.sources.inkbunny_enabled = False
        s.sources.danbooru_enabled = False
        s.sources.gelbooru_enabled = False
        s.sources.fluffle_enabled = False
        s.sources.saucenao_enabled = False
        errs = validate_run_preflight(
            s, hydrus_available=True, any_source_available=True)
        self.assertTrue(any("source" in e.lower() for e in errs))

    def test_defaults_ok(self):
        s = Settings()
        errs = validate_run_preflight(
            s, hydrus_available=True, any_source_available=True)
        self.assertEqual(errs, [])


if __name__ == "__main__":
    unittest.main()
