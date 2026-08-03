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
    normalize_recent_scan_paths,
    remember_scan_path,
    hydrus_scope_id,
    normalize_hydrus_api_origin,
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

    def test_unknown_placeholder_rejected(self):
        with self.assertRaises(SidecarPatternError):
            validate_sidecar_pattern("{name}{ext}.{foo}.txt")

    def test_bad_patterns_fall_back_on_load(self):
        """CLI never calls the preflight, so load() itself must sanitize."""
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text(json.dumps({"output": {
                "sidecar_tag_filename": "{name}{ext}.{foo}",
                "sidecar_url_filename": "../{name}{ext}.urls.txt",
                "sidecar_json_filename": "",
            }}), encoding="utf-8")
            s = SettingsStore(path).load()
            self.assertEqual(s.output.sidecar_tag_filename, "{name}{ext}.txt")
            self.assertEqual(s.output.sidecar_url_filename,
                             "{name}{ext}.urls.txt")
            self.assertEqual(s.output.sidecar_json_filename, "{name}{ext}.json")
            # And rendering with the sanitized pattern cannot raise/escape.
            self.assertEqual(
                render_sidecar_name(s.output.sidecar_tag_filename,
                                    Path("/tmp/cat.jpg")), "cat.jpg.txt")

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
        self.assertEqual(s.hydrus.duplicate_tagged_page_name,
                         "FurTag Duplicate Tagged")
        self.assertTrue(s.hydrus.direct_source_notes)
        self.assertFalse(s.hydrus.exact_url_enrichment)
        self.assertEqual(s.hydrus.exact_url_enrichment_page_name,
                         "FurTag Metadata")
        self.assertEqual(s.history.recent_scan_paths, [])

    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            store = SettingsStore(path)
            s = Settings()
            s.matching.saucenao_min_similarity = 75.0
            s.sources.e621_enabled = False
            s.hydrus.duplicate_tagged_page_name = "Dupe Review"
            s.hydrus.direct_source_notes = False
            s.hydrus.exact_url_enrichment = True
            s.hydrus.exact_url_enrichment_page_name = "Source Metadata"
            s.history.recent_scan_paths = ["/Volumes/Art", "/tmp/archive"]
            store.save(s)
            loaded = store.load()
            self.assertEqual(loaded.matching.saucenao_min_similarity, 75.0)
            self.assertFalse(loaded.sources.e621_enabled)
            self.assertEqual(loaded.hydrus.duplicate_tagged_page_name,
                             "Dupe Review")
            self.assertFalse(loaded.hydrus.direct_source_notes)
            self.assertTrue(loaded.hydrus.exact_url_enrichment)
            self.assertEqual(loaded.hydrus.exact_url_enrichment_page_name,
                             "Source Metadata")
            self.assertEqual(
                loaded.history.recent_scan_paths,
                [str(Path("/Volumes/Art").resolve(strict=False)),
                 str(Path("/tmp/archive").resolve(strict=False))])

    def test_v1_migrates_from_downloader_scraping_to_direct_notes(self):
        loaded = Settings.from_dict({
            "version": 1,
            "hydrus": {"exact_url_enrichment": True},
        })
        self.assertTrue(loaded.hydrus.direct_source_notes)
        self.assertFalse(loaded.hydrus.exact_url_enrichment)

    def test_current_version_respects_legacy_enrichment_choice(self):
        loaded = Settings.from_dict({
            "version": 2,
            "hydrus": {
                "direct_source_notes": True,
                "exact_url_enrichment": True,
            },
        })
        self.assertTrue(loaded.hydrus.direct_source_notes)
        self.assertTrue(loaded.hydrus.exact_url_enrichment)

    def test_v2_page_settings_migrate_to_independent_live_pages(self):
        loaded = Settings.from_dict({
            "version": 2,
            "hydrus": {
                "results_pages_enabled": True,
                "new_imports_page_name": "Fresh",
                "newly_tagged_page_name": "Updated",
                "duplicate_tagged_page_name": "Duplicates",
                "already_tagged_page_name": "History",
                "build_already_tagged_page": True,
                "result_page_limit": 37,
            },
        })
        h = loaded.hydrus
        self.assertEqual(h.new_imports_page_name, "Fresh")
        self.assertEqual(h.newly_tagged_page_name, "Updated")
        self.assertEqual(h.duplicate_tagged_page_name, "Duplicates")
        self.assertEqual(h.already_tagged_page_name, "History")
        self.assertTrue(h.already_tagged_page_enabled)
        for prefix in ("new_imports", "newly_tagged", "duplicate_tagged"):
            self.assertEqual(getattr(h, f"{prefix}_page_limit"), 37)
            self.assertEqual(getattr(h, f"{prefix}_page_mode"), "live")
        self.assertEqual(h.already_tagged_page_limit, 37)

    def test_page_settings_normalize_invalid_values(self):
        loaded = Settings.from_dict({
            "version": 3,
            "hydrus": {
                "new_imports_page_limit": -4,
                "new_imports_page_mode": "rolling",
                "newly_tagged_page_limit": "bad",
                "duplicate_tagged_page_mode": None,
                "already_tagged_page_limit": -1,
                "live_page_update_interval": 999,
            },
        })
        h = loaded.hydrus
        self.assertEqual(h.new_imports_page_limit, 0)
        self.assertEqual(h.new_imports_page_mode, "live")
        self.assertEqual(h.newly_tagged_page_limit, 0)
        self.assertEqual(h.duplicate_tagged_page_mode, "live")
        self.assertEqual(h.already_tagged_page_limit, 0)
        self.assertEqual(h.live_page_update_interval, 60)

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

    def test_recent_paths_are_absolute_deduped_and_bounded(self):
        base = Path(tempfile.gettempdir()).resolve()
        path_a = str(base / "A")
        paths = [path_a, path_a + "/", "", "relative/path", 42]
        self.assertEqual(normalize_recent_scan_paths(paths), [path_a])
        many = [str(base / str(i)) for i in range(20)]
        self.assertEqual(len(normalize_recent_scan_paths(many)), 12)

    def test_remember_scan_path_moves_existing_path_to_front(self):
        base = Path(tempfile.gettempdir()).resolve()
        first, second = str(base / "first"), str(base / "second")
        remembered = remember_scan_path([first, second], second)
        self.assertEqual(remembered, [second, first])

    def test_hydrus_profile_uuid_generated_on_load(self):
        s = Settings.from_dict({})
        self.assertTrue(s.hydrus.hydrus_profile_uuid)
        again = Settings.from_dict({
            "hydrus": {"hydrus_profile_uuid": s.hydrus.hydrus_profile_uuid},
        })
        self.assertEqual(
            again.hydrus.hydrus_profile_uuid, s.hydrus.hydrus_profile_uuid)

    def test_store_persists_generated_hydrus_profile_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            path.write_text('{"version": 2}\n', encoding="utf-8")
            store = SettingsStore(path)
            first = store.load().hydrus.hydrus_profile_uuid
            second = store.load().hydrus.hydrus_profile_uuid
            self.assertTrue(first)
            self.assertEqual(second, first)
            persisted = json.loads(path.read_text("utf-8"))
            self.assertEqual(
                persisted["hydrus"]["hydrus_profile_uuid"], first)

    def test_new_store_persists_nonblank_hydrus_profile_uuid(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "settings.json"
            store = SettingsStore(path)
            first = store.load().hydrus.hydrus_profile_uuid
            self.assertTrue(first)
            self.assertTrue(path.exists())
            self.assertEqual(
                store.load().hydrus.hydrus_profile_uuid, first)

    def test_hydrus_scope_id_ignores_path_and_key(self):
        uid = "11111111-1111-1111-1111-111111111111"
        a = hydrus_scope_id(uid, "http://127.0.0.1:45869/")
        b = hydrus_scope_id(uid, "http://127.0.0.1:45869")
        self.assertEqual(a, b)
        self.assertEqual(
            normalize_hydrus_api_origin("https://Host.Example:1234/foo"),
            "https://host.example:1234")
        self.assertNotEqual(
            hydrus_scope_id(uid, "http://127.0.0.1:45869"),
            hydrus_scope_id(uid, "http://127.0.0.1:45870"))


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
