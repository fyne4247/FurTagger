"""Hydrus database scan: selection, deleted-duplicate expansion, bookkeeping."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from furtag import HydrusScanReport, SourceMetadata, TagIntegrator
from furtag_settings import HydrusScanSettings, ScanSummary, Settings

from tests.test_fakes import FakeResponse, FakeSession

KEPT = "a" * 64
DELETED = "b" * 64
OTHER_KEPT = "c" * 64
KEPT_MD5 = "1" * 32
DELETED_MD5 = "2" * 32


def _integrator(session, settings=None):
    s = settings or Settings()
    s.output.hydrus_enabled = True
    ti = TagIntegrator(settings=s, session=session)
    ti.hydrus_api_url = "http://127.0.0.1:45869"
    ti.hydrus_access_key = "test-key"
    ti.hydrus_tag_service_key = "svc123"
    ti.has_hydrus = True
    ti.hydrus_can_search_files = True
    ti.hydrus_can_manage_relationships = True
    ti.hydrus_can_edit_urls = True
    ti.hydrus_can_edit_notes = True
    # e621 is the only hash source these tests exercise; the others would need
    # credentials to count as active.
    ti.has_e621 = True
    ti.enabled_e621 = True
    return ti


class TestScanPredicates(unittest.TestCase):
    def test_selection_settings_become_hydrus_predicates(self):
        ti = _integrator(FakeSession())
        scan = HydrusScanSettings(
            max_tag_count=5, tag_count_service="my tags", limit=250,
            inbox_only=True)
        predicates = ti.hydrus_scan_predicates(scan)

        self.assertIn("system:filetype = image", predicates)
        self.assertIn("system:number of tags (my tags) < 5", predicates)
        self.assertIn("system:inbox", predicates)
        self.assertIn("-furtag:scanned", predicates)
        # The cap is applied last so it counts files that passed every filter.
        self.assertEqual(predicates[-1], "system:limit = 250")

    def test_user_supplied_limit_is_not_duplicated(self):
        ti = _integrator(FakeSession())
        scan = HydrusScanSettings(
            limit=250, extra_predicates=["system:limit = 10"],
            images_only=False, skip_already_scanned=False)
        predicates = ti.hydrus_scan_predicates(scan)
        self.assertEqual(
            [p for p in predicates if "limit" in p], ["system:limit = 10"])

    def test_scanned_marker_is_omitted_when_not_marking(self):
        ti = _integrator(FakeSession())
        scan = HydrusScanSettings(mark_state_tags=False)
        self.assertNotIn(
            "-furtag:scanned", ti.hydrus_scan_predicates(scan))


class TestFileDomains(unittest.TestCase):
    def test_tag_repositories_are_not_offered_as_file_domains(self):
        session = FakeSession([
            ("GET", "get_services", FakeResponse(200, {
                "services_v2": [
                    # 0 is a tag repository, not a file domain — it appears in
                    # the same list and reads like one.
                    {"name": "public tag repository", "type": 0,
                     "service_key": "ptr"},
                    {"name": "my tags", "type": 5, "service_key": "tags"},
                    {"name": "remote files", "type": 1, "service_key": "repo"},
                    {"name": "trash", "type": 14, "service_key": "trash"},
                    {"name": "Furry Porn", "type": 2, "service_key": "local"},
                    {"name": "all local files", "type": 15,
                     "service_key": "alllocal"},
                    {"name": "all my files", "type": 21,
                     "service_key": "allmine"},
                ],
            })),
        ])
        ti = _integrator(session)
        self.assertEqual(
            ti.hydrus_file_services(),
            [("Furry Porn", "local"), ("all local files", "alllocal"),
             ("all my files", "allmine")])


class TestDuplicateExpansion(unittest.TestCase):
    def test_only_duplicates_are_followed_never_alternates(self):
        alternate = "d" * 64
        session = FakeSession([
            ("GET", "get_file_relationships", FakeResponse(200, {
                "file_relationships": {
                    KEPT: {"8": [KEPT, DELETED], "3": [alternate]},
                },
            })),
        ])
        ti = _integrator(session)
        members = ti.hydrus_duplicate_members([KEPT])

        # The file's own hash is dropped, and alternates never appear: they are
        # different artwork, so their booru tags would be wrong here.
        self.assertEqual(members, {KEPT: [DELETED]})

    def test_relationship_failure_does_not_abort_the_scan(self):
        session = FakeSession([
            ("GET", "get_file_relationships", FakeResponse(500, {}, "boom")),
        ])
        ti = _integrator(session)
        with patch.object(ti, "_notify_repeated_issue") as warned:
            self.assertEqual(ti.hydrus_duplicate_members([KEPT]), {})
        self.assertTrue(warned.called)

    def test_md5_map_covers_deleted_hashes(self):
        session = FakeSession([
            ("GET", "file_hashes", FakeResponse(200, {
                "hashes": {KEPT: KEPT_MD5, DELETED: DELETED_MD5},
            })),
        ])
        ti = _integrator(session)
        self.assertEqual(
            ti.hydrus_md5_map([KEPT, DELETED]),
            {KEPT: KEPT_MD5, DELETED: DELETED_MD5})


class TestScanRun(unittest.TestCase):
    """End-to-end runs with every booru lookup stubbed."""

    def _run(self, ti, scan, lookups):
        """Drive a scan, resolving each MD5 through *lookups*."""
        def fake_hash_lookup(service, md5):
            return lookups.get((service, md5), SourceMetadata())

        with patch.object(ti, "_hash_lookup", side_effect=fake_hash_lookup):
            return ti.scan_hydrus_db(scan, use_terminal_display=False)

    def _session(self, hashes, relationships=None, md5s=None):
        return FakeSession([
            ("GET", "search_files", lambda url, kw: FakeResponse(
                200, {"hashes": self._search_response(kw, hashes)})),
            ("GET", "get_file_relationships", FakeResponse(200, {
                "file_relationships": relationships or {}})),
            ("GET", "file_hashes", FakeResponse(200, {"hashes": md5s or {}})),
            ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ("POST", "add_urls/associate_url", FakeResponse(200, {})),
        ])

    @staticmethod
    def _search_response(kwargs, hashes):
        """Selection search returns candidates; currency checks return kept."""
        tags = json.loads(kwargs["params"]["tags"])
        if any("system:hash" in t for t in tags):
            # _hydrus_current_sha256s asking which group members still exist.
            return [h for h in hashes if h != DELETED]
        return hashes

    def test_deleted_duplicate_supplies_tags_for_the_kept_file(self):
        session = self._session(
            [KEPT],
            relationships={KEPT: {"8": [DELETED]}},
            md5s={KEPT: KEPT_MD5, DELETED: DELETED_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(
            mark_state_tags=False, write_report=False, limit=0)

        found = SourceMetadata(tags={"creator:someone"},
                               urls={"https://e621.net/posts/1"})
        summary = self._run(
            ti, scan, {("e621", DELETED_MD5): found})

        self.assertEqual(summary.tagged, 1)
        self.assertEqual(summary.matched_via_deleted_duplicate, 1)
        self.assertEqual(summary.deleted_duplicates_examined, 1)

        # The tags land on the kept file, never on the deleted one.
        tagged = [kw["json"] for method, url, kw in session.calls
                  if "add_tags" in url]
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["hash"], KEPT)
        self.assertEqual(
            tagged[0]["service_keys_to_tags"]["svc123"], ["creator:someone"])

    def test_on_miss_mode_skips_duplicates_when_the_kept_file_hits(self):
        session = self._session(
            [KEPT],
            relationships={KEPT: {"8": [DELETED]}},
            md5s={KEPT: KEPT_MD5, DELETED: DELETED_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(
            mark_state_tags=False, write_report=False, limit=0,
            deleted_duplicates_when="on_miss")

        seen = []

        def fake_hash_lookup(service, md5):
            seen.append(md5)
            if md5 == KEPT_MD5:
                return SourceMetadata(tags={"creator:known"})
            return SourceMetadata()

        with patch.object(ti, "_hash_lookup", side_effect=fake_hash_lookup):
            summary = ti.scan_hydrus_db(scan, use_terminal_display=False)

        self.assertEqual(summary.tagged, 1)
        self.assertEqual(summary.matched_via_deleted_duplicate, 0)
        self.assertNotIn(DELETED_MD5, seen)

    def test_a_current_duplicate_is_not_treated_as_deleted(self):
        session = self._session(
            [KEPT, OTHER_KEPT],
            relationships={KEPT: {"8": [OTHER_KEPT]}},
            md5s={KEPT: KEPT_MD5, OTHER_KEPT: "3" * 32})
        ti = _integrator(session)
        scan = HydrusScanSettings(
            mark_state_tags=False, write_report=False, limit=0)
        summary = self._run(ti, scan, {})
        self.assertEqual(summary.deleted_duplicates_examined, 0)

    def test_lookup_errors_leave_the_file_unmarked_for_a_retry(self):
        session = self._session([KEPT], md5s={KEPT: KEPT_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(write_report=False, limit=0,
                                  include_deleted_duplicates=False)

        def failing(service, md5):
            raise RuntimeError("e621 is down")

        with patch.object(ti, "_hash_lookup", side_effect=failing):
            summary = ti.scan_hydrus_db(scan, use_terminal_display=False)

        self.assertEqual(summary.errors, 1)
        self.assertEqual(summary.unmatched, 0)
        # No furtag:nomatch marker, so the next run's -furtag:scanned
        # predicate still selects this file.
        self.assertEqual(
            [kw["json"] for _, url, kw in session.calls if "add_tags" in url],
            [])

    def test_cancellation_mid_file_leaves_it_unmarked(self):
        session = self._session([KEPT], md5s={KEPT: KEPT_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(write_report=False, limit=0,
                                  include_deleted_duplicates=False)

        def cancel_then_miss(service, md5):
            ti.request_cancel()
            return SourceMetadata()

        with patch.object(ti, "_hash_lookup", side_effect=cancel_then_miss):
            summary = ti.scan_hydrus_db(scan, use_terminal_display=False)

        # A cancelled fan-out is not evidence of a miss, so nothing is sealed.
        self.assertEqual(summary.unmatched, 0)
        self.assertEqual(
            [kw["json"] for _, url, kw in session.calls if "add_tags" in url],
            [])

    def test_nomatch_is_marked_so_the_next_run_skips_it(self):
        session = self._session([KEPT], md5s={KEPT: KEPT_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(write_report=False, limit=0,
                                  include_deleted_duplicates=False)
        summary = self._run(ti, scan, {})

        self.assertEqual(summary.unmatched, 1)
        marked = [kw["json"] for _, url, kw in session.calls
                  if "add_tags" in url]
        self.assertEqual(len(marked), 1)
        self.assertEqual(
            sorted(marked[0]["service_keys_to_tags"]["svc123"]),
            ["furtag:nomatch", "furtag:scanned"])

    def test_results_reach_the_configured_hydrus_pages(self):
        session = self._session(
            [KEPT, OTHER_KEPT],
            relationships={KEPT: {"8": [DELETED]}},
            md5s={KEPT: KEPT_MD5, OTHER_KEPT: "4" * 32,
                  DELETED: DELETED_MD5})
        ti = _integrator(session)
        ti.hydrus_can_manage_pages = True
        for page in ti.hydrus_result_pages.values():
            page.enabled = True
        scan = HydrusScanSettings(
            mark_state_tags=False, write_report=False, limit=0)

        self._run(ti, scan, {
            ("e621", DELETED_MD5): SourceMetadata(tags={"creator:x"}),
            ("e621", "4" * 32): SourceMetadata(tags={"creator:y"}),
        })

        # A direct hit is "newly tagged"; one recovered from a deleted
        # duplicate belongs on the duplicate page, as in the folder pipeline.
        self.assertEqual(ti.hydrus_result_pages["updated"].hashes,
                         [OTHER_KEPT])
        self.assertEqual(ti.hydrus_result_pages["duplicates"].hashes, [KEPT])

    def test_dry_run_writes_nothing_to_hydrus(self):
        session = self._session(
            [KEPT],
            relationships={KEPT: {"8": [DELETED]}},
            md5s={KEPT: KEPT_MD5, DELETED: DELETED_MD5})
        ti = _integrator(session)
        scan = HydrusScanSettings(
            write_report=False, limit=0, dry_run=True)
        summary = self._run(
            ti, scan,
            {("e621", DELETED_MD5): SourceMetadata(tags={"creator:x"})})

        self.assertEqual(summary.tagged, 1)
        self.assertEqual(
            [url for method, url, _ in session.calls if method == "POST"], [])

    def test_missing_md5_is_skipped_not_counted_as_a_miss(self):
        session = self._session([KEPT], md5s={})
        ti = _integrator(session)
        scan = HydrusScanSettings(write_report=False, limit=0,
                                  include_deleted_duplicates=False)
        summary = self._run(ti, scan, {})
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.unmatched, 0)

    def test_scan_refuses_to_run_with_no_hash_sources(self):
        ti = _integrator(self._session([KEPT]))
        for name in ("e621", "inkbunny", "danbooru", "gelbooru"):
            setattr(ti.settings.sources, f"{name}_enabled", False)
        ti.settings.sources.e621_enabled = False
        with patch.object(ti, "enabled_hash_services", return_value=[]):
            with self.assertRaises(RuntimeError) as ctx:
                ti.scan_hydrus_db(
                    HydrusScanSettings(write_report=False),
                    use_terminal_display=False)
        self.assertIn("exact-hash sources", str(ctx.exception))


class TestScanReport(unittest.TestCase):
    def test_report_is_appended_per_file_and_summarised_at_the_end(self):
        scan = HydrusScanSettings(file_service_name="my files")
        with tempfile.TemporaryDirectory() as td:
            report = HydrusScanReport(scan, directory=Path(td))
            report.record(KEPT, "matched", md5=KEPT_MD5, sources=["e621"])
            report.record(DELETED, "nomatch", md5=DELETED_MD5)
            summary = ScanSummary(
                tagged=1, unmatched=1, total_items=2, stop_reason="completed")
            text_path = Path(report.finish(summary))

            lines = [json.loads(line)
                     for line in report.path.read_text().splitlines()]
            self.assertEqual(
                [entry["event"] for entry in lines],
                ["start", "file", "file", "end"])
            self.assertEqual(lines[-1]["counts"],
                             {"matched": 1, "nomatch": 1})
            body = text_path.read_text()
            self.assertIn("my files", body)
            self.assertIn("Tagged:    1", body)

    def test_a_cancelled_scan_still_leaves_readable_lines(self):
        with tempfile.TemporaryDirectory() as td:
            report = HydrusScanReport(
                HydrusScanSettings(), directory=Path(td))
            report.record(KEPT, "matched")
            # Nothing flushed at exit — every line is already on disk.
            lines = report.path.read_text().splitlines()
            self.assertEqual(len(lines), 2)
            report._close()


if __name__ == "__main__":
    unittest.main()
