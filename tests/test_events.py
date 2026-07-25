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
import unittest
from unittest import mock
from pathlib import Path

import furtag
from furtag import (
    FileItem, LiveDisplay, TagIntegrator, notify, set_active_observer,
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
    return RunOptions(import_unmatched=False, result_page_limit=0,
                      build_already_tagged_page=False, sync_sidecars=False,
                      pdf_dpi=None)


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
        # begin_phase keeps the richer CLI labels (service lists intact).
        labels = {e.track: e.phase for e in rec.kinds("begin_phase")}
        self.assertIn("e621", labels["hash"])
        self.assertIn("Fluffle", labels["perceptual"])

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
            ({"creator:someone"}, {"https://e621.net/posts/1"})
            if service == "e621" else (set(), set()))
        rec = RecordingObserver()
        ti._observer = rec
        item = FileItem(path=Path("/tmp/x.png"), relpath="x.png", size=1,
                        mtime=0.0, kind="image", md5="0" * 32)
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            tags, urls, sources = ti.hash_tier(item, ex)
        self.assertEqual(sources, ["e621"])
        statuses = rec.kinds("status", "hash")
        self.assertTrue(statuses, "no hash ticker status events")
        self.assertTrue(all(e.sub.startswith("hash ▸ ") for e in statuses))
        final = statuses[-1].extra["hash_state"]
        self.assertEqual(final, {"e621": "hit", "danbooru": "miss"})
        self.assertIn("e621 ✓", statuses[-1].sub)


class TestUrlEnrichmentBoundary(unittest.TestCase):
    """Only byte-exact hash-tier results may enter Hydrus's downloader."""

    def _capture_writes(self, ti):
        exact_flags = []

        def capture(media, tags, urls, known_sha256=None, exact_match=False):
            exact_flags.append(exact_match)
            return "a" * 64

        ti.write_results = capture
        return exact_flags

    def test_hash_tier_write_is_marked_exact(self):
        d = _make_pngs(1)
        self.addCleanup(shutil.rmtree, d, True)
        s = _offline_settings()
        s.sources.e621_enabled = True
        ti = TagIntegrator(settings=s)
        ti.has_e621 = True
        ti._hash_lookup = lambda service, md5: (
            {"creator:exact"}, {"https://e621.net/posts/1"})
        exact_flags = self._capture_writes(ti)

        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)

        self.assertEqual(exact_flags, [True])

    def test_perceptual_write_is_not_marked_exact(self):
        d = _make_pngs(1)
        self.addCleanup(shutil.rmtree, d, True)
        ti = TagIntegrator(settings=_offline_settings())
        ti.perceptual_tier = lambda item: (
            {"creator:fuzzy"}, {"https://www.furaffinity.net/view/1"},
            ["fluffle"], None)
        exact_flags = self._capture_writes(ti)

        with contextlib.redirect_stdout(io.StringIO()):
            ti.run(d, options=_run_options(), observer=RecordingObserver(),
                   use_terminal_display=False)

        self.assertEqual(exact_flags, [False])


if __name__ == "__main__":
    unittest.main()
