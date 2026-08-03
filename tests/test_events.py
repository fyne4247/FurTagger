"""Observer contract: the engine emits events, frontends render them.

Covers the two invariants that used to be broken by the duplicated
`disp.*` + `observer.emit(...)` progress calls:

* a progress point is written exactly ONCE (so `grow` can't double the
  perceptual total), and
* `notify()` reaches the active observer (so a GUI issue pane can populate).
"""

import concurrent.futures as cf
import io
import contextlib
import shutil
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

import furtag
from furtag import (
    FileItem, Ledger, LiveDisplay, TagIntegrator, WriteOutcome, notify,
    set_active_observer,
)
from furtag_events import NullObserver, RunEvent, TerminalObserver
from furtag_settings import RunOptions, Settings


class RecordingObserver:
    def __init__(self):
        self.events = []

    def emit(self, event: RunEvent) -> None:
        self.events.append(event)

    def kinds(self, kind, track=None):
        return [e for e in self.events
                if e.kind == kind and (track is None or e.track == track)]


def _offline_settings():
    s = Settings()
    s.output.hydrus_enabled = False
    s.output.sidecars_enabled = True
    for name in ("e621", "inkbunny", "danbooru", "gelbooru",
                 "fluffle", "saucenao"):
        setattr(s.sources, f"{name}_enabled", False)
    s.pdf.pdf_enabled = False
    return s


def _run_options():
    return RunOptions(import_unmatched=False, sync_sidecars=False, pdf_dpi=None)


def _make_pngs(count):
    from PIL import Image
    d = Path(tempfile.mkdtemp(prefix="furtag-events-"))
    for i in range(count):
        Image.new("RGB", (16 + i, 16), (i * 17 % 255, 40, 60)).save(
            d / f"img{i}.png")
    return d


class TestNotifyRouting(unittest.TestCase):
    def setUp(self):
        self._prev = set_active_observer(None)
        self.addCleanup(lambda: set_active_observer(self._prev))

    def test_notify_emits_issue_event_to_active_observer(self):
        rec = RecordingObserver()
        set_active_observer(rec)
        notify("⚠️  something went sideways")
        self.assertEqual([e.kind for e in rec.events], ["issue"])
        self.assertEqual(rec.events[0].message, "⚠️  something went sideways")

    def test_notify_without_observer_prints(self):
        set_active_observer(None)   # default display-less TerminalObserver
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            notify("plain fallback")
        self.assertIn("plain fallback", buf.getvalue())

    def test_notify_is_swallowed_by_null_observer(self):
        set_active_observer(NullObserver())
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            notify("dropped")
        self.assertEqual(buf.getvalue(), "")

    def test_terminal_observer_routes_issue_into_live_display(self):
        disp = LiveDisplay()
        disp.tty = True
        disp.begin_phase("hash", "Phase · hash lookups", 1)
        set_active_observer(TerminalObserver(disp))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            notify("❌ e621 failed on a.png: boom")
        # Rendered into the panel's rolling issue history, not a raw print.
        self.assertEqual(disp._issue_total, 1)
        self.assertIn("Recent issues", buf.getvalue())

    def test_engine_warnings_reach_a_gui_style_observer(self):
        """A GUI passes its own observer; notify() from a worker must land there."""
        d = _make_pngs(2)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        real = ti.perceptual_tier

        def boom(item):
            if item.path.name == "img0.png":
                raise RuntimeError("synthetic failure")
            return real(item)

        ti.perceptual_tier = boom
        rec = RecordingObserver()
        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=rec,
                   use_terminal_display=False)
        issues = [e.message for e in rec.kinds("issue")]
        self.assertTrue(any("synthetic failure" in m for m in issues), issues)


class TestGrowTotalNotDoubleCounted(unittest.TestCase):
    def test_terminal_observer_grow_counts_once(self):
        disp = LiveDisplay()
        disp.tty = False
        obs = TerminalObserver(disp)
        with contextlib.redirect_stdout(io.StringIO()):
            obs.emit(RunEvent(kind="begin_phase", track="perceptual",
                              phase="Phase · perceptual", total=0,
                              extra={"growing": True}))
            for _ in range(5):
                obs.emit(RunEvent(kind="grow", track="perceptual"))
        self.assertEqual(disp.tracks["perceptual"].total, 5)

    def test_grow_honours_explicit_by(self):
        disp = LiveDisplay()
        disp.tty = False
        obs = TerminalObserver(disp)
        obs.emit(RunEvent(kind="grow", track="perceptual", extra={"by": 3}))
        self.assertEqual(disp.tracks["perceptual"].total, 3)

    def test_terminal_display_keeps_running_source_totals(self):
        disp = LiveDisplay()
        disp.tty = False
        obs = TerminalObserver(disp)
        with contextlib.redirect_stdout(io.StringIO()):
            obs.emit(RunEvent(
                kind="finish_file", track="hash", result="matched",
                source_hits={"e621": 4, "inkbunny": 2}))
        self.assertEqual(disp.source_hits["e621"], 4)
        self.assertEqual(disp.source_hits["inkbunny"], 2)
        self.assertEqual(disp.source_hits["danbooru"], 0)

    def test_run_perceptual_total_matches_files_processed(self):
        """Regression: the total used to be 2× the file count, because both
        `disp.grow(...)` and a `grow` event fired at the same site."""
        n = 4
        d = _make_pngs(n)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        rec = RecordingObserver()
        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=rec,
                   use_terminal_display=False)

        begin = rec.kinds("begin_phase", "perceptual")[0]
        grown = sum(int(e.extra.get("by") or 1)
                    for e in rec.kinds("grow", "perceptual"))
        finished = rec.kinds("finish_file", "perceptual")
        self.assertEqual(begin.total, 0)             # no PDF pages seeded
        self.assertEqual(grown, n)                   # exactly one grow per file
        self.assertEqual(begin.total + grown, len(finished))

    def test_terminal_run_bar_total_is_exact(self):
        """Same check through the real terminal path: LiveDisplay is driven
        only by TerminalObserver, so its total equals the file count."""
        n = 3
        d = _make_pngs(n)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        seen = {}

        real_grow = LiveDisplay.grow

        def spy(self, track, by=1):
            seen[track] = seen.get(track, 0) + by
            return real_grow(self, track, by)

        with mock.patch.object(LiveDisplay, "grow", spy):
            with contextlib.redirect_stdout(io.StringIO()):
                ti.run(d, options=_run_options(), use_terminal_display=True)
        self.assertEqual(seen.get("perceptual"), n)


class TestEventStream(unittest.TestCase):
    def test_finish_events_carry_running_source_totals(self):
        d = _make_pngs(2)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        ti.hash_tier = lambda _item, _executor: (
            {"creator:test"}, set(), ["e621", "danbooru"], set())
        rec = RecordingObserver()

        with contextlib.redirect_stdout(io.StringIO()):
            summary = ti.run(
                d, options=_run_options(), observer=rec,
                use_terminal_display=False)

        hits = [
            event.source_hits for event in rec.kinds("finish_file", "hash")
            if event.source_hits]
        self.assertEqual(len(hits), 2)
        self.assertEqual(hits[0]["e621"], 1)
        self.assertEqual(hits[0]["danbooru"], 1)
        self.assertEqual(hits[1]["e621"], 2)
        self.assertEqual(hits[1]["danbooru"], 2)
        self.assertEqual(summary.source_hits["e621"], 2)
        self.assertEqual(summary.source_hits["danbooru"], 2)

    def test_expected_stream_for_an_offline_run(self):
        n = 3
        d = _make_pngs(n)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        rec = RecordingObserver()
        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=rec,
                   use_terminal_display=False)

        kinds = [e.kind for e in rec.events]
        self.assertEqual(kinds.count("begin_phase"), 2)
        self.assertEqual(len(rec.kinds("start_file", "hash")), n)
        self.assertEqual(len(rec.kinds("finish_file", "hash")), n)
        self.assertEqual(len(rec.kinds("start_file", "perceptual")), n)
        self.assertEqual(len(rec.kinds("finish_file", "perceptual")), n)
        self.assertEqual(len(rec.kinds("freeze_total", "perceptual")), 1)
        self.assertEqual(kinds.count("close_display"), 1)
        # The local-hash pre-pass reports itself now (the GUI used to see nothing).
        self.assertTrue(any("local hash" in e.sub
                            for e in rec.kinds("status", "perceptual")))
        # Phase labels reflect the configured pipeline without advertising
        # sources the user explicitly disabled.
        labels = {e.track: e.phase for e in rec.kinds("begin_phase")}
        self.assertIn("none enabled", labels["hash"])
        self.assertIn("none enabled", labels["perceptual"])
        self.assertNotIn("SauceNAO", labels["perceptual"])

    def test_engine_never_touches_a_display_directly(self):
        """`_run_pipeline` must not hold a LiveDisplay reference any more."""
        import inspect
        src = inspect.getsource(TagIntegrator._run_pipeline)
        self.assertNotIn("disp.", src)
        self.assertNotIn("LiveDisplay(", src)

    def test_observer_is_restored_after_a_run(self):
        d = _make_pngs(1)
        self.addCleanup(shutil.rmtree, d, True)
        before = furtag._active_observer
        ti = TagIntegrator(settings=_offline_settings())
        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)
        self.assertIs(furtag._active_observer, before)
        self.assertIsNone(furtag._display)


class TestHashTickerReachesObserver(unittest.TestCase):
    def test_per_site_ticker_emitted_as_status_events(self):
        s = _offline_settings()
        s.sources.e621_enabled = True
        s.sources.danbooru_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = ti.has_danbooru = True
        ti._hash_lookup = lambda service, md5: (
            ({"creator:someone"}, {"https://e621.net/posts/1"}, set())
            if service == "e621" else (set(), set(), set()))
        rec = RecordingObserver()
        ti._observer = rec
        item = FileItem(path=Path("/tmp/x.png"), relpath="x.png", size=1,
                        mtime=0.0, kind="image", md5="0" * 32)
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            tags, urls, sources, force_assoc = ti.hash_tier(item, ex)
        self.assertEqual(force_assoc, set())
        self.assertEqual(sources, ["e621"])
        statuses = rec.kinds("status", "hash")
        self.assertTrue(statuses, "no hash ticker status events")
        self.assertTrue(all(e.sub.startswith("hash ▸ ") for e in statuses))
        final = statuses[-1].extra["hash_state"]
        self.assertEqual(final, {"e621": "hit", "danbooru": "miss"})
        self.assertIn("e621 ✓", statuses[-1].sub)

    def test_source_exception_is_remembered_as_retryable(self):
        s = _offline_settings()
        s.sources.e621_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = True

        def fail(_service, _md5):
            raise OSError("temporary CA bundle failure")

        ti._hash_lookup = fail
        ti._observer = RecordingObserver()
        item = FileItem(path=Path("/tmp/x.mp4"), relpath="x.mp4", size=1,
                        mtime=0.0, kind="video", md5="0" * 32)
        with cf.ThreadPoolExecutor(max_workers=1) as ex, \
             mock.patch("furtag.notify"):
            tags, urls, sources, force_assoc = ti.hash_tier(item, ex)
        self.assertFalse(tags or urls or sources or force_assoc)
        self.assertEqual(item.lookup_errors, {"e621"})

    def test_missing_ca_bundle_stops_the_scan_once(self):
        s = _offline_settings()
        s.sources.e621_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = True

        def fail(_service, _md5):
            raise OSError(
                "Could not find a suitable TLS CA certificate bundle, "
                "invalid path: /moved/.venv/certifi/cacert.pem")

        ti._hash_lookup = fail
        ti._observer = RecordingObserver()
        item = FileItem(path=Path("/tmp/x.mp4"), relpath="x.mp4", size=1,
                        mtime=0.0, kind="video", md5="0" * 32)
        with cf.ThreadPoolExecutor(max_workers=1) as ex, \
             mock.patch("furtag.notify") as notice:
            ti.hash_tier(item, ex)
        self.assertTrue(ti.cancelled())
        self.assertEqual(notice.call_count, 1)
        self.assertIn("stopping safely", notice.call_args.args[0])


class TestTransientLookupFailures(unittest.TestCase):
    def _run_with_hash_failure(self, suffix):
        d = Path(tempfile.mkdtemp(prefix="furtag-retry-"))
        self.addCleanup(shutil.rmtree, d, True)
        media = d / f"media{suffix}"
        media.write_bytes(b"not-real-media")
        s = _offline_settings()
        s.sources.e621_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = True

        def fail(_service, _md5):
            raise OSError("temporary CA bundle failure")

        ti._hash_lookup = fail
        if suffix == ".png":
            ti.perceptual_tier = lambda _item: (
                set(), set(), [], None)
        rec = RecordingObserver()
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("furtag.notify"):
            summary = ti.run(
                d, options=_run_options(), observer=rec,
                use_terminal_display=False)
        ledger = furtag.Ledger(d)
        ledger.load()
        return media, ledger, rec, summary

    def test_hash_only_video_failure_is_not_checkpointed_as_nomatch(self):
        media, ledger, rec, summary = self._run_with_hash_failure(".mp4")
        stat = media.stat()
        self.assertEqual(
            ledger.status_for(media.name, stat.st_size, stat.st_mtime),
            "hashed")
        self.assertEqual(summary.unmatched, 0)
        events = rec.kinds("finish_file", "hash")
        self.assertTrue(events[-1].extra["retryable"])

    def test_image_with_hash_failure_skips_slow_perceptual_and_stays_retryable(self):
        media, ledger, rec, summary = self._run_with_hash_failure(".png")
        stat = media.stat()
        self.assertEqual(
            ledger.status_for(media.name, stat.st_size, stat.st_mtime),
            "hashed")
        self.assertEqual(summary.unmatched, 0)
        events = rec.kinds("finish_file", "hash")
        self.assertTrue(events[-1].extra["retryable"])
        self.assertEqual(rec.kinds("start_file", "perceptual"), [])

    def test_incomplete_output_is_not_checkpointed_as_matched(self):
        root = _make_pngs(1)
        self.addCleanup(shutil.rmtree, root, True)
        settings = _offline_settings()
        settings.sources.e621_enabled = True
        ti = TagIntegrator(settings=settings)
        ti.has_e621 = True
        ti._hash_lookup = lambda service, md5: (
            {"creator:exact"}, {"https://e621.net/posts/1"}, set())
        ti.write_results_detailed = lambda *args, **kwargs: WriteOutcome(
            None, False)

        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(root, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)

        media = next(root.glob("*.png"))
        st = media.stat()
        ledger = Ledger(root)
        ledger.load()
        self.assertNotEqual(
            ledger.status_for(media.name, st.st_size, st.st_mtime),
            "matched")

    def test_partial_hash_hit_waits_for_failed_additive_source(self):
        root = Path(tempfile.mkdtemp(prefix="furtag-partial-hit-"))
        self.addCleanup(shutil.rmtree, root, True)
        media = root / "media.mp4"
        media.write_bytes(b"not-real-video")
        settings = _offline_settings()
        settings.sources.e621_enabled = True
        settings.sources.danbooru_enabled = True
        ti = TagIntegrator(settings=settings)
        ti.has_e621 = ti.has_danbooru = True

        def lookup(service, _md5):
            if service == "e621":
                return {"creator:exact"}, {"https://e621.net/posts/1"}, set()
            raise OSError("temporary Danbooru failure")

        ti._hash_lookup = lookup
        ti.write_results_detailed = mock.MagicMock()
        rec = RecordingObserver()
        with contextlib.redirect_stdout(io.StringIO()), \
             mock.patch("furtag.notify"):
            summary = ti.run(
                root, options=_run_options(), observer=rec,
                use_terminal_display=False)

        ledger = Ledger(root)
        ledger.load()
        st = media.stat()
        self.assertEqual(
            ledger.status_for(media.name, st.st_size, st.st_mtime),
            "hashed")
        self.assertEqual(summary.tagged, 0)
        ti.write_results_detailed.assert_not_called()
        event = rec.kinds("finish_file", "hash")[-1]
        self.assertTrue(event.extra["retryable"])
        self.assertEqual(event.extra["failed_sources"], ["danbooru"])

    def test_pre_set_cancellation_is_not_cleared(self):
        root = _make_pngs(1)
        self.addCleanup(shutil.rmtree, root, True)
        cancel = threading.Event()
        cancel.set()
        ti = TagIntegrator(settings=_offline_settings())
        with contextlib.redirect_stdout(io.StringIO()):
            summary = ti.run(
                root, options=_run_options(), observer=RecordingObserver(),
                cancel_event=cancel, use_terminal_display=False)
        self.assertTrue(cancel.is_set())
        self.assertTrue(summary.cancelled)


class TestUrlEnrichmentBoundary(unittest.TestCase):
    """Only byte-exact hash-tier results may enter Hydrus's downloader."""

    def _capture_writes(self, ti):
        from furtag_urls import UrlWritePolicy
        policies = []

        def capture(media, tags, urls, known_sha256=None, exact_match=False,
                    url_policy=None, force_associate_urls=None, notes=None):
            if url_policy is None:
                url_policy = (
                    UrlWritePolicy.ENRICH_HASH_POSTS if exact_match
                    else UrlWritePolicy.ASSOCIATE_ONLY)
            policies.append(url_policy)
            return WriteOutcome("a" * 64, True)

        ti.write_results_detailed = capture
        return policies

    def test_hash_tier_write_is_marked_exact(self):
        from furtag_urls import UrlWritePolicy
        d = _make_pngs(1)
        self.addCleanup(shutil.rmtree, d, True)
        s = _offline_settings()
        s.sources.e621_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = True
        ti._hash_lookup = lambda service, md5: (
            {"creator:exact"}, {"https://e621.net/posts/1"}, set())
        policies = self._capture_writes(ti)

        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)

        self.assertEqual(policies, [UrlWritePolicy.ENRICH_HASH_POSTS])

    def test_perceptual_write_is_not_marked_exact(self):
        from furtag_urls import UrlWritePolicy
        d = _make_pngs(1)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        ti.perceptual_tier = lambda item: (
            {"creator:fuzzy"}, {"https://www.furaffinity.net/view/1"},
            ["fluffle"], None)
        policies = self._capture_writes(ti)

        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)

        self.assertEqual(policies, [UrlWritePolicy.ASSOCIATE_ONLY])

if __name__ == "__main__":
    unittest.main()
