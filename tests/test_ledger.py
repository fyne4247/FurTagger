"""Ledger skip rules, pending_review semantics, fingerprint sealing."""

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from furtag import (
    FileItem, LEDGER_METADATA_VERSION, Ledger, LedgerManager, TagIntegrator,
    RESOLVED_LEDGER_STATUSES,
)
from furtag_settings import Settings
from furtag_review import ReviewQueue, PendingReview


class TestLedgerSkip(unittest.TestCase):
    def test_index_prunes_hidden_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            visible = root / "visible.jpg"
            visible.write_bytes(b"visible")
            protected = root / ".DocumentRevisions-V100"
            protected.mkdir()
            (protected / "revision.jpg").write_bytes(b"hidden")

            items, _ = TagIntegrator(settings=Settings()).index(
                root, LedgerManager(), set())

            self.assertEqual([item.path for item in items], [visible])

    def test_status_for_matched(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            led = Ledger(d)
            led.record("a.jpg", 100, 1.0, "md5a", "matched", ["e621"])
            self.assertEqual(led.status_for("a.jpg", 100, 1.0), "matched")
            self.assertIsNone(led.status_for("a.jpg", 101, 1.0))  # size change
            self.assertIsNone(led.status_for("a.jpg", 100, 2.0))  # mtime change

    def test_old_resolved_record_is_retried_for_metadata_backfill(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.records["old.jpg"] = {
                "size": 100,
                "mtime": 1.0,
                "md5": "abc",
                "status": "matched",
                "sources": ["e621"],
                "metadata_version": LEDGER_METADATA_VERSION - 1,
            }
            self.assertIsNone(led.status_for("old.jpg", 100, 1.0))
            # The expensive disk hash remains reusable during the backfill.
            self.assertEqual(led.md5_for("old.jpg", 100, 1.0), "abc")

    def test_direct_note_backfill_waits_until_capability_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record(
                "offline.jpg", 100, 1.0, "abc", "matched", ["e621"],
                direct_notes_applied=False)
            self.assertEqual(
                led.status_for(
                    "offline.jpg", 100, 1.0,
                    require_direct_notes=False),
                "matched")
            self.assertIsNone(
                led.status_for(
                    "offline.jpg", 100, 1.0,
                    require_direct_notes=True))

    def test_sidecar_does_not_hide_deferred_direct_note_backfill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "offline.jpg"
            media.write_bytes(b"image")
            st = media.stat()
            settings = Settings()
            settings.output.hydrus_enabled = False
            settings.output.sidecars_enabled = True
            ti = TagIntegrator(settings=settings)
            ti._write_sidecar_results(media, {"creator:test"}, set())
            led = Ledger(root)
            ti.ledger_record(
                led,
                media.name, st.st_size, st.st_mtime, "abc",
                "matched", ["e621"], direct_notes_applied=False)
            led.mark_dir_complete(
                1, st.st_size,
                ti._directory_manifest(root, {media.name: st}),
                direct_notes_applied=False)
            led.save()

            # Sidecar/offline mode accepts the resolved record without wasting
            # source calls. Once note capability appears, both the directory
            # fingerprint and per-file sidecar skip reopen it.
            self.assertEqual(
                ti.index(root, LedgerManager(), set())[0], [])
            ti.has_hydrus = True
            ti.hydrus_can_edit_notes = True
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [media])

    def test_unscoped_legacy_hydrus_deleted_revalidates_once(self):
        """Old deletion seals cannot cross an unknown Hydrus database scope."""
        self.assertIn("hydrus_deleted", RESOLVED_LEDGER_STATUSES)
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "deleted.png"
            media.write_bytes(b"deleted-bytes")
            st = media.stat()
            led = Ledger(root)
            # Simulate a pre-migration ledger row still on disk.
            led.records[media.name] = {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "md5": "md5del",
                "status": "hydrus_deleted",
                "sources": ["e621"],
                "metadata_version": LEDGER_METADATA_VERSION,
            }
            led.save()

            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [media])
            self.assertEqual(
                led.status_for(media.name, st.st_size, st.st_mtime),
                "hydrus_deleted")

    def test_record_rewrites_hydrus_deleted_to_matched_plus_checkpoint(self):
        """New writers never persist top-level hydrus_deleted."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record(
                "a.jpg", 100, 1.0, "md5a", "hydrus_deleted", ["e621"],
                sha256="d" * 64)
            rec = led.records["a.jpg"]
            self.assertEqual(rec["status"], "matched")
            self.assertEqual(
                rec["hydrus_output"]["metadata_state"], "no_duplicate_targets")
            self.assertEqual(rec["sha256"], "d" * 64)

    def test_matched_with_hydrus_output_checkpoint_is_skipped(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "gone.png"
            media.write_bytes(b"gone-bytes")
            st = media.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led,
                media.name, st.st_size, st.st_mtime, "md5g", "matched",
                ["e621"], sha256="d" * 64,
                hydrus_output={
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "previously_deleted",
                    "metadata_state": "no_duplicate_targets",
                    "sha256": "d" * 64,
                    "target_hashes": [],
                    "complete": True,
                })
            led.save()
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual(items, [])

    def test_incomplete_unmatched_import_blocks_directory_seal(self):
        """BF-02: required unmatched import pending → no dir fingerprint seal."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"fake-jpeg-bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_import = True
            ti.hydrus_import_unmatched = True
            led = Ledger(root)
            ti.ledger_record(
                led,
                "x.jpg", st.st_size, st.st_mtime, "abc", "nomatch", [],
                unmatched_import={
                    "requested": True,
                    "complete": False,
                    "import_state": "retryable_failure",
                    "metadata_state": "not_requested",
                })
            led.save()
            mgr = LedgerManager()
            items, cand = ti.index(root, mgr, set())
            # Search is resolved (nomatch) so index skips re-search.
            self.assertEqual(items, [])
            ti.finalize_dir_fingerprints(cand, set(), mgr, root=root)
            self.assertIsNone(mgr.get(root).dir_count)

    def test_complete_unmatched_import_allows_directory_seal(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"fake-jpeg-bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_import = True
            ti.hydrus_import_unmatched = True
            led = Ledger(root)
            ti.ledger_record(
                led,
                "x.jpg", st.st_size, st.st_mtime, "abc", "nomatch", [],
                sha256="a" * 64,
                unmatched_import={
                    "requested": True,
                    "complete": True,
                    "import_state": "live",
                    "metadata_state": "not_requested",
                    "sha256": "a" * 64,
                })
            led.save()
            mgr = LedgerManager()
            items, cand = ti.index(root, mgr, set())
            self.assertEqual(items, [])
            ti.finalize_dir_fingerprints(cand, set(), mgr, root=root)
            self.assertEqual(mgr.get(root).dir_count, 1)

    def test_not_requested_checkpoint_reopens_when_import_becomes_required(self):
        ti = TagIntegrator(settings=Settings())
        rec = {
            "status": "nomatch",
            "unmatched_import": {
                "requested": False,
                "complete": True,
                "scope_id": None,
            },
        }
        self.assertFalse(
            ti.unmatched_import_is_complete(rec, required=True))

    def test_reject_review_persists_decision_when_import_incomplete(self):
        """Reject clears the interactive queue even if Hydrus import fails."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "a.jpg"
            media.write_bytes(b"img")
            st = media.stat()
            pending = PendingReview.create(
                path=str(media.resolve()), relpath="a.jpg",
                size=st.st_size, mtime=st.st_mtime, md5="0" * 32)
            rq = ReviewQueue(root)
            rq.add(pending)
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_import = True
            ti.hydrus_import_unmatched = True
            ti._review_queue = rq
            from furtag import WriteOutcome
            with patch.object(
                    ti, "write_unmatched_detailed",
                    return_value=WriteOutcome(
                        None, False, ledger_status="nomatch",
                        unmatched_import={
                            "requested": True,
                            "complete": False,
                            "import_state": "retryable_failure",
                        })):
                self.assertTrue(
                    ti.resolve_pending_review(pending, approve=False,
                                              root=root))
            self.assertEqual(len(rq), 0)
            led = Ledger(root)
            led.load()
            rec = led.records["a.jpg"]
            self.assertEqual(rec["status"], "nomatch")
            self.assertEqual(rec["review"]["decision"], "rejected")
            self.assertFalse(rec["review"]["output_complete"])
            self.assertFalse(rec["unmatched_import"]["complete"])

    def test_pending_review_not_resolved(self):
        self.assertNotIn("pending_review", RESOLVED_LEDGER_STATUSES)
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            led = Ledger(d)
            led.record("b.png", 50, 1.0, "md5b", "pending_review", ["pending_review"])
            self.assertEqual(led.status_for("b.png", 50, 1.0), "pending_review")
            # status_for returns it, but index treats it as unresolved

    def test_fingerprint_blocked_by_pending(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"fake-jpeg-bytes")
            st = img.stat()
            led = Ledger(root)
            led.record("x.jpg", st.st_size, st.st_mtime, "abc",
                       "pending_review", ["pending_review"])
            led.save()

            ti = TagIntegrator(settings=Settings())
            mgr = LedgerManager()
            items, cand = ti.index(root, mgr, set())
            # pending_review must remain eligible
            self.assertEqual(len(items), 1)
            ti.finalize_dir_fingerprints(cand, set(), mgr)
            led2 = mgr.get(root)
            # Must NOT be sealed while pending_review
            self.assertIsNone(led2.dir_count)

    def test_enabling_source_invalidates_old_nomatch_profile(self):
        """BF-03: search_profile_hash change reopens prior nomatch rows."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.settings.sources.e621_enabled = False
            ti.enabled_e621 = False
            led = Ledger(root)
            ti.ledger_record(
                led, "x.jpg", st.st_size, st.st_mtime, "abc", "nomatch", [])
            led.save()
            # Same profile → still skipped.
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual(items, [])
            # Enable e621 → profile changes → re-queue.
            ti.settings.sources.e621_enabled = True
            ti.enabled_e621 = True
            items2, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([i.path for i in items2], [img])

    def test_sidecar_does_not_hide_stale_search_profile(self):
        """A sidecar satisfies its sink, not a now-stale search decision."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            Path(str(img) + ".txt").write_text(
                "creator:test\n", encoding="utf-8")
            st = img.stat()
            settings = Settings()
            settings.sources.e621_enabled = False
            ti = TagIntegrator(settings=settings)
            led = Ledger(root)
            ti.ledger_record(
                led, img.name, st.st_size, st.st_mtime,
                "a" * 32, "nomatch", [])
            led.save()

            ti.settings.sources.e621_enabled = True
            ti.enabled_e621 = True
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [img])

    def test_sidecar_does_not_hide_unresolved_hashed_record(self):
        """A sidecar written before a failed Hydrus sink must not seal work."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            Path(str(img) + ".txt").write_text(
                "creator:test\n", encoding="utf-8")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = True
            led = Ledger(root)
            led.cache_md5(img.name, st.st_size, st.st_mtime, "a" * 32)
            led.save()

            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [img])

    def test_deleted_policy_change_reopens_normal_scan_checkpoint(self):
        """Normal scan output, not only sidecar sync, is policy scoped."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            st = img.stat()
            settings = Settings()
            settings.output.hydrus_tag_deleted_duplicates = False
            ti = TagIntegrator(settings=settings)
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led, img.name, st.st_size, st.st_mtime,
                "a" * 32, "matched", ["e621"],
                hydrus_output={
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "previously_deleted",
                    "metadata_state": "policy_skipped",
                    "complete": True,
                })
            led.mark_dir_complete(
                1, st.st_size,
                ti._directory_manifest(root, {img.name: st}),
                direct_notes_applied=False,
                search_profile_hash=ti.search_profile_hash(),
                sidecars_required=False,
                sidecar_format=ti.sidecar_format_key(),
                output_policy_hash=ti.output_policy_hash())
            led.save()

            ti.hydrus_tag_deleted_duplicates = True
            ti.settings.output.hydrus_tag_deleted_duplicates = True
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [img])

    def test_hydrus_scope_change_reopens_normal_scan_checkpoint(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_api_url = "http://127.0.0.1:45869"
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led, img.name, st.st_size, st.st_mtime,
                "a" * 32, "matched", ["e621"],
                hydrus_output={
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "live",
                    "metadata_state": "applied_original",
                    "complete": True,
                })
            led.save()

            ti.settings.hydrus.hydrus_profile_uuid = "replacement-db"
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [img])

    def test_sidecars_required_invalidates_hydrus_only_dir_seal(self):
        """BF-04: seal without sidecars does not wholesale-skip when required."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led, "x.jpg", st.st_size, st.st_mtime, "abc", "matched",
                ["e621"], hydrus_output={
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "live",
                    "metadata_state": "applied_original",
                    "complete": True,
                })
            led.mark_dir_complete(
                1, st.st_size,
                ti._directory_manifest(root, {"x.jpg": st}),
                direct_notes_applied=False,
                search_profile_hash=ti.search_profile_hash(),
                sidecars_required=False,
                sidecar_format="txt")
            led.save()
            # Still no sidecars required → wholesale skip.
            items, _ = ti.index(root, LedgerManager(), set())
            self.assertEqual(items, [])
            # Enable sidecars → old seal must not apply.
            ti.hydrus_also_sidecars = True
            items2, cand = ti.index(root, LedgerManager(), set())
            self.assertEqual([i.path for i in items2], [img])
            self.assertIn(root, cand)

    def test_missing_sidecar_prevents_reseal_after_cancel_path(self):
        """BF-05: finalize will not seal when required sidecar is missing."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = True
            led = Ledger(root)
            ti.ledger_record(
                led, "x.jpg", st.st_size, st.st_mtime, "abc", "matched",
                ["e621"])
            led.save()
            mgr = LedgerManager()
            # Index sees missing sidecar → needs work; candidate dir present.
            items, cand = ti.index(root, mgr, set())
            self.assertEqual([i.path for i in items], [img])
            # Finalize without writing sidecar must not seal.
            ti.finalize_dir_fingerprints(cand, set(), mgr, root=root)
            self.assertIsNone(mgr.get(root).dir_count)

    def test_fingerprint_seals_when_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"fake-jpeg-bytes")
            st = img.stat()
            ti = TagIntegrator(settings=Settings())
            led = Ledger(root)
            ti.ledger_record(
                led, "x.jpg", st.st_size, st.st_mtime, "abc",
                "matched", ["e621"])
            led.save()

            ti._write_sidecar_results(img, {"creator:test"}, set())
            mgr = LedgerManager()
            items, cand = ti.index(root, mgr, set())
            self.assertEqual(len(items), 0)  # already matched
            # candidate_dirs empty when whole-folder skip... actually matched
            # file still walks dir. finalize seals if complete.
            if not cand:
                cand = {root}
            ti.finalize_dir_fingerprints(cand, set(), mgr)
            led2 = mgr.get(root)
            self.assertIsNotNone(led2.dir_count)

    def test_directory_manifest_detects_same_size_rename(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            original = root / "a.jpg"
            original.write_bytes(b"same-size")
            st = original.stat()
            ti = TagIntegrator(settings=Settings())
            led = Ledger(root)
            led.record(
                original.name, st.st_size, st.st_mtime, "abc",
                "matched", ["e621"])
            stats = {original.name: st}
            manifest = ti._directory_manifest(root, stats)
            led.mark_dir_complete(1, st.st_size, manifest)
            led.save()

            original.rename(root / "b.jpg")
            items, _candidate_dirs = ti.index(
                root, LedgerManager(), set())
            self.assertEqual([item.path.name for item in items], ["b.jpg"])

    def test_missing_active_sidecar_reopens_matched_file(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "x.jpg"
            media.write_bytes(b"image")
            st = media.stat()
            settings = Settings()
            settings.output.hydrus_enabled = False
            settings.output.sidecars_enabled = True
            ti = TagIntegrator(settings=settings)
            ti._write_sidecar_results(media, {"creator:test"}, set())
            led = Ledger(root)
            led.record(
                media.name, st.st_size, st.st_mtime, "abc",
                "matched", ["e621"])
            led.save()

            ti.tag_sidecar_path(media).unlink()
            items, _candidate_dirs = ti.index(
                root, LedgerManager(), set())
            self.assertEqual([item.path for item in items], [media])

    def test_md5_cache_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            led = Ledger(d)
            led.cache_md5("c.gif", 10, 1.0, "deadbeef")
            self.assertEqual(led.md5_for("c.gif", 10, 1.0), "deadbeef")
            self.assertEqual(led.status_for("c.gif", 10, 1.0), "hashed")

    def test_sidecar_sync_checkpoint_is_independent_of_scan_status(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            led = Ledger(d)
            led.record_sidecar_sync(
                "sidecar.jpg", 100, 1.0, "signature-a", "sha-a")
            self.assertTrue(led.sidecar_sync_matches(
                "sidecar.jpg", 100, 1.0, "signature-a"))
            self.assertEqual(
                led.status_for("sidecar.jpg", 100, 1.0), "sidecar_only")
            self.assertNotIn("sidecar_only", RESOLVED_LEDGER_STATUSES)

            # A later online match for the same bytes must keep the sync
            # checkpoint while replacing only the ordinary scan status.
            led.record(
                "sidecar.jpg", 100, 1.0, "md5-a", "matched", ["e621"])
            self.assertEqual(
                led.status_for("sidecar.jpg", 100, 1.0), "matched")
            self.assertTrue(led.sidecar_sync_matches(
                "sidecar.jpg", 100, 1.0, "signature-a"))

            # Replaced media invalidates the checkpoint automatically.
            self.assertFalse(led.sidecar_sync_matches(
                "sidecar.jpg", 101, 1.0, "signature-a"))

    def test_legacy_sidecar_sync_does_not_cross_hydrus_scope(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.records["sidecar.jpg"] = {
                "size": 100,
                "mtime": 1.0,
                "status": "sidecar_only",
                "sources": [],
                "sidecar_sync": {"signature": "signature-a"},
            }
            self.assertFalse(led.sidecar_sync_matches(
                "sidecar.jpg", 100, 1.0, "signature-a",
                scope_id="replacement-hydrus"))


class TestReviewQueue(unittest.TestCase):
    def test_persist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rq = ReviewQueue(root)
            p = PendingReview.create(
                path=str(root / "a.jpg"), relpath="a.jpg",
                size=1, mtime=1.0, match_class="tossUp",
                platform="e621", location="https://e621.net/posts/1")
            rq.add(p)
            rq2 = ReviewQueue(root)
            rq2.load()
            self.assertEqual(len(rq2), 1)
            self.assertEqual(rq2.list_items()[0].match_class, "tossUp")
            rq2.remove(p.id)
            self.assertEqual(len(rq2), 0)


class TestCliReviewLoopSurvivesSourceFailure(unittest.TestCase):
    """A raising source must defer the item, not kill the CLI.

    ``resolve_pending_review`` reaches e621, which raises RetryableLookupError
    on a 401 rather than reporting a clean miss. ``_cli_review_loop`` is called
    inside a try that only catches KeyboardInterrupt, so an escaping error would
    end the whole CLI with a traceback and drop the rest of the queue.
    """

    def _run_loop(self, answer, resolve):
        import furtag as ft
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            rq = ReviewQueue(root)
            for name in ("a.jpg", "b.jpg"):
                rq.add(PendingReview.create(
                    path=str(root / name), relpath=name, size=1, mtime=1.0,
                    match_class="tossUp", platform="e621",
                    location="https://e621.net/posts/1"))
            ti = MagicMock()
            ti._review_queue = rq
            ti.resolve_pending_review.side_effect = resolve

            real_isatty = ft.sys.stdin.isatty
            ft.sys.stdin.isatty = lambda: True
            try:
                with patch("builtins.input", lambda *_a, **_k: answer):
                    ft._cli_review_loop(ti, root)
            finally:
                ft.sys.stdin.isatty = real_isatty
            return ti.resolve_pending_review.call_count

    def test_retryable_error_defers_and_keeps_going(self):
        from furtag import RetryableLookupError
        calls = self._run_loop(
            "a", RetryableLookupError("e621 authentication rejected"))
        # Both items attempted: the loop must not abort on the first failure.
        self.assertEqual(calls, 2)

    def test_normal_approval_still_resolves(self):
        self.assertEqual(self._run_loop("a", lambda *a, **k: True), 2)


class TestLedgerSha256(unittest.TestCase):
    """A cached Hydrus SHA-256 must survive a later record() that has no hash
    of its own (sidecar-only mode / unmatched with importing off), but must
    never be inherited by a file whose bytes changed."""

    def test_sha256_preserved_when_record_passes_none(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record("a.jpg", 100, 1.0, "md5a", "matched", ["e621"])
            led.cache_sha256("a.jpg", 100, 1.0, "sha-aaa")
            self.assertEqual(led.sha256_for("a.jpg", 100, 1.0), "sha-aaa")
            # Same file recorded again with no fresh hash (write_results → None)
            led.record("a.jpg", 100, 1.0, "md5a", "matched", ["e621", "danbooru"])
            self.assertEqual(led.sha256_for("a.jpg", 100, 1.0), "sha-aaa")
            self.assertEqual(led.records["a.jpg"]["sha256"], "sha-aaa")

    def test_fresh_sha256_still_wins(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record("a.jpg", 100, 1.0, "md5a", "matched", [], sha256="sha-old")
            led.record("a.jpg", 100, 1.0, "md5a", "matched", [], sha256="sha-new")
            self.assertEqual(led.records["a.jpg"]["sha256"], "sha-new")

    def test_sha256_dropped_when_size_changed(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record("a.jpg", 100, 1.0, "md5a", "matched", [], sha256="sha-aaa")
            # File was replaced: different size → the old hash describes other bytes
            led.record("a.jpg", 250, 1.0, "md5b", "nomatch", [])
            self.assertNotIn("sha256", led.records["a.jpg"])
            self.assertIsNone(led.sha256_for("a.jpg", 250, 1.0))

    def test_sha256_dropped_when_mtime_changed(self):
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record("a.jpg", 100, 1.0, "md5a", "matched", [], sha256="sha-aaa")
            led.record("a.jpg", 100, 99.0, "md5b", "nomatch", [])
            self.assertNotIn("sha256", led.records["a.jpg"])
            self.assertIsNone(led.sha256_for("a.jpg", 100, 99.0))


class TestDedup(unittest.TestCase):
    def test_exact_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "a.jpg"
            b = root / "b.jpg"
            data = b"identical-bytes-for-md5"
            a.write_bytes(data)
            b.write_bytes(data)
            ti = TagIntegrator(settings=Settings())
            mgr = LedgerManager()
            items, _ = ti.index(root, mgr, set())
            ti.hash_all(items)
            survivors, n_dup, groups = ti.deduplicate(root, items, mgr)
            self.assertEqual(n_dup, 1)
            self.assertEqual(len(survivors), 1)
            duplicate = next(iter(groups.values()))[0]
            self.assertEqual(
                duplicate.ledger.status_for(
                    duplicate.path.name, duplicate.size, duplicate.mtime),
                "duplicate_pending")
            self.assertFalse((root / "duplicates.log").exists())
            ti._resolve_duplicate_nomatches(
                root, survivors[0], [duplicate])
            ti._write_duplicates_log(root, mgr)
            self.assertTrue((root / "duplicates.log").exists())

    def test_prior_hydrus_deleted_seeds_canonical_and_propagates(self):
        """BF-08: legacy hydrus_deleted prior seal fans out to same-MD5 copies."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = b"identical-deleted-content"
            a = root / "a.jpg"
            b = root / "b.jpg"
            a.write_bytes(data)
            b.write_bytes(data)
            st_a = a.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            # Prior seal on a only (legacy top-level status).
            led = Ledger(root)
            led.records[a.name] = {
                "size": st_a.st_size,
                "mtime": st_a.st_mtime,
                "md5": hashlib.md5(data).hexdigest(),
                "status": "hydrus_deleted",
                "sources": ["e621"],
                "sha256": "d" * 64,
                "metadata_version": LEDGER_METADATA_VERSION,
                "hydrus_output": {
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "previously_deleted",
                    "metadata_state": "no_duplicate_targets",
                    "sha256": "d" * 64,
                    "target_hashes": [],
                    "complete": True,
                },
            }
            led.save()

            mgr = LedgerManager()
            items, _ = ti.index(root, mgr, set())
            # a is legacy-resolved; b still needs work
            self.assertEqual([i.path.name for i in items], ["b.jpg"])
            ti.hash_all(items)
            survivors, n_dup, groups = ti.deduplicate(root, items, mgr)
            self.assertEqual(n_dup, 1)
            self.assertEqual(survivors, [])  # only copy was pending against a
            copied = ti._propagate_prior_duplicate_groups(root, groups, mgr)
            self.assertEqual(copied, 1)
            led2 = mgr.get(root)
            rec_b = led2.records[b.name]
            self.assertEqual(rec_b["status"], "matched")
            self.assertEqual(
                rec_b["hydrus_output"]["metadata_state"],
                "no_duplicate_targets")
            self.assertEqual(rec_b["sha256"], "d" * 64)

    def test_prior_matched_with_nested_deleted_output_propagates(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = b"same-bytes-nested"
            a = root / "canon.jpg"
            b = root / "copy.jpg"
            a.write_bytes(data)
            b.write_bytes(data)
            st = a.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led, a.name, st.st_size, st.st_mtime,
                hashlib.md5(data).hexdigest(), "matched", ["e621"],
                sha256="d" * 64,
                hydrus_output={
                    "scope_id": ti._hydrus_scope_id(),
                    "policy_hash": ti.hydrus_output_policy_hash(),
                    "import_state": "previously_deleted",
                    "metadata_state": "no_duplicate_targets",
                    "sha256": "d" * 64,
                    "target_hashes": [],
                    "complete": True,
                })
            led.save()
            mgr = LedgerManager()
            items, _ = ti.index(root, mgr, set())
            ti.hash_all(items)
            survivors, n_dup, groups = ti.deduplicate(root, items, mgr)
            self.assertEqual(n_dup, 1)
            self.assertEqual(survivors, [])
            self.assertEqual(
                ti._propagate_prior_duplicate_groups(root, groups, mgr), 1)
            rec_b = mgr.get(root).records[b.name]
            self.assertEqual(rec_b["status"], "matched")
            self.assertEqual(
                rec_b["hydrus_output"]["import_state"], "previously_deleted")

    def test_prior_nomatch_resolves_new_duplicate_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data = b"prior-clean-nomatch"
            canonical = root / "a.jpg"
            duplicate = root / "b.jpg"
            canonical.write_bytes(data)
            duplicate.write_bytes(data)
            st = canonical.stat()
            ti = TagIntegrator(settings=Settings())
            ti.has_hydrus = True
            ti.hydrus_also_sidecars = False
            led = Ledger(root)
            ti.ledger_record(
                led, canonical.name, st.st_size, st.st_mtime,
                hashlib.md5(data).hexdigest(), "nomatch", [])
            led.save()

            mgr = LedgerManager()
            items, _ = ti.index(root, mgr, set())
            ti.hash_all(items)
            survivors, count, groups = ti.deduplicate(root, items, mgr)
            self.assertEqual(survivors, [])
            self.assertEqual(count, 1)
            ti._propagate_prior_duplicate_groups(root, groups, mgr)
            self.assertEqual(
                mgr.get(root).records[duplicate.name]["status"], "duplicate")
            self.assertEqual(groups, {})


class TestHardeningPRF(unittest.TestCase):
    def test_matched_rewrite_preserves_tagged_at(self):
        """BF-11: idempotent matched rewrites keep original tagged_at."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record("a.jpg", 100, 1.0, "md5", "matched", ["e621"])
            first = led.records["a.jpg"]["tagged_at"]
            time.sleep(0.01)
            led.record("a.jpg", 100, 1.0, "md5", "matched", ["e621", "danbooru"])
            self.assertEqual(led.records["a.jpg"]["tagged_at"], first)

    def test_mirror_write_does_not_invent_tagged_at(self):
        """BF-11: historical mirrors stay historical when time is unknown."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record(
                "copy.jpg", 100, 1.0, "md5", "matched", ["e621"],
                stamp_tagged_at=False, metadata_version=0)
            self.assertNotIn("tagged_at", led.records["copy.jpg"])
            self.assertEqual(led.records["copy.jpg"]["metadata_version"], 0)

    def test_metadata_version_does_not_reopen_nomatch(self):
        """BF-15: notes metadata version is matched-only."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.records["n.jpg"] = {
                "size": 10,
                "mtime": 1.0,
                "md5": "abc",
                "status": "nomatch",
                "sources": [],
                "metadata_version": LEDGER_METADATA_VERSION - 1,
                "search_profile_hash": "x",
            }
            # Without profile check, old nomatch still resolves.
            self.assertEqual(led.status_for("n.jpg", 10, 1.0), "nomatch")
            # Matched with notes sources still reopens on version mismatch.
            led.records["m.jpg"] = {
                "size": 10,
                "mtime": 1.0,
                "md5": "def",
                "status": "matched",
                "sources": ["e621"],
                "metadata_version": LEDGER_METADATA_VERSION - 1,
                "direct_notes_applied": True,
            }
            self.assertIsNone(led.status_for("m.jpg", 10, 1.0))

    def test_mtime_ns_mismatch_invalidates_legacy_float_match(self):
        """BF-16: ns fingerprint wins when both sides provide it."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record(
                "a.jpg", 100, 1.0, "md5", "matched", ["e621"],
                mtime_ns=1_000_000_000)
            self.assertEqual(
                led.status_for("a.jpg", 100, 1.0, mtime_ns=1_000_000_000),
                "matched")
            self.assertIsNone(
                led.status_for("a.jpg", 100, 1.0, mtime_ns=1_000_000_001))
            # Legacy float-only lookup still works against ns-stored rows.
            self.assertEqual(led.status_for("a.jpg", 100, 1.0), "matched")

    def test_unreadable_reopens_on_decoder_profile_change(self):
        """BF-17: decoder_profile gate on unreadable seals."""
        with tempfile.TemporaryDirectory() as td:
            led = Ledger(Path(td))
            led.record(
                "bad.jpg", 10, 1.0, "md5", "unreadable", [],
                decoder_profile="v1;pillow=9.0.0",
                unreadable_reason="decode failed")
            self.assertEqual(
                led.status_for(
                    "bad.jpg", 10, 1.0, decoder_profile="v1;pillow=9.0.0"),
                "unreadable")
            self.assertIsNone(
                led.status_for(
                    "bad.jpg", 10, 1.0, decoder_profile="v1;pillow=10.0.0"))

    def test_normal_pipeline_record_upgrades_to_mtime_ns(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "a.jpg"
            media.write_bytes(b"bytes")
            st = media.stat()
            ti = TagIntegrator(settings=Settings())
            led = Ledger(root)
            ti.ledger_record(
                led, media.name, st.st_size, st.st_mtime,
                "a" * 32, "nomatch", [])
            self.assertEqual(
                led.records[media.name]["mtime_ns"], st.st_mtime_ns)

    def test_local_hash_failure_stays_retryable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "a.mp4"
            media.write_bytes(b"bytes")
            st = media.stat()
            led = Ledger(root)
            item = FileItem(
                media, media.name, st.st_size, st.st_mtime, "video",
                ledger=led, mtime_ns=st.st_mtime_ns)
            ti = TagIntegrator(settings=Settings())
            with patch.object(ti, "_md5_local", return_value=None):
                ti.hash_all([item])
            self.assertIn("local_hash", item.lookup_errors)
            self.assertNotIn(media.name, led.records)


class TestSidecarSyncTraversal(unittest.TestCase):
    def test_hidden_directory_sidecars_are_not_synced(self):
        """BF-10: sidecar sync must not enter dot-directories."""
        from tests.test_fakes import FakeSession, FakeResponse, _hydrus_ti

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hidden = root / ".hidden"
            hidden.mkdir()
            media = hidden / "secret.jpg"
            media.write_bytes(b"secret")
            Path(str(media) + ".txt").write_text(
                "creator:test\n", encoding="utf-8")
            visible = root / "visible.jpg"
            visible.write_bytes(b"visible")
            Path(str(visible) + ".txt").write_text(
                "creator:other\n", encoding="utf-8")

            session = FakeSession([
                ("POST", "add_files/add_file", FakeResponse(200, {
                    "status": 1, "hash": "a" * 64,
                })),
                ("POST", "add_tags/add_tags", FakeResponse(200, {})),
            ])
            ti = _hydrus_ti(session)
            attempted, failed = ti.sync_sidecars_to_hydrus(root)
            # Only the visible file is a candidate.
            self.assertEqual(attempted, 1)
            self.assertEqual(failed, 0)
            add_paths = [
                (kwargs.get("json") or {}).get("path")
                for method, url, kwargs in session.calls
                if method == "POST" and "add_file" in url
            ]
            self.assertEqual(len(add_paths), 1)
            self.assertIn("visible.jpg", add_paths[0])
            self.assertNotIn(".hidden", add_paths[0])


if __name__ == "__main__":
    unittest.main()
