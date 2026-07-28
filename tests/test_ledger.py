"""Ledger skip rules, pending_review semantics, fingerprint sealing."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from furtag import (
    LEDGER_METADATA_VERSION, Ledger, LedgerManager, TagIntegrator,
    RESOLVED_LEDGER_STATUSES,
)
from furtag_settings import Settings
from furtag_review import ReviewQueue, PendingReview


class TestLedgerSkip(unittest.TestCase):
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
            led.record(
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

    def test_fingerprint_seals_when_resolved(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            img = root / "x.jpg"
            img.write_bytes(b"fake-jpeg-bytes")
            st = img.stat()
            led = Ledger(root)
            led.record("x.jpg", st.st_size, st.st_mtime, "abc",
                       "matched", ["e621"])
            led.save()

            ti = TagIntegrator(settings=Settings())
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


if __name__ == "__main__":
    unittest.main()
