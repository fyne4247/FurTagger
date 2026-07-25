"""Ledger skip rules, pending_review semantics, fingerprint sealing."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from furtag import Ledger, LedgerManager, TagIntegrator, RESOLVED_LEDGER_STATUSES
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

    def test_md5_cache_reuse(self):
        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            led = Ledger(d)
            led.cache_md5("c.gif", 10, 1.0, "deadbeef")
            self.assertEqual(led.md5_for("c.gif", 10, 1.0), "deadbeef")
            self.assertEqual(led.status_for("c.gif", 10, 1.0), "hashed")


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
            self.assertTrue((root / "duplicates.log").exists())


if __name__ == "__main__":
    unittest.main()
