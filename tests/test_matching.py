"""Matching thresholds, Fluffle review band, SauceNAO boundaries, source toggles."""

import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from furtag import TagIntegrator, RetryableLookupError, _is_pdf_page_render
from furtag_review import PendingReview
from furtag_settings import Settings


class TestSourceToggles(unittest.TestCase):
    def test_available_vs_enabled(self):
        ti = TagIntegrator(settings=Settings())
        ti.has_e621 = True
        ti.has_inkbunny = True
        ti.has_danbooru = False
        ti.has_gelbooru = False
        ti.has_saucenao = True
        ti.enabled_e621 = False
        ti.enabled_inkbunny = True
        ti.enabled_saucenao = True
        ti.enabled_fluffle = True
        self.assertEqual(ti.enabled_hash_services(), ["inkbunny"])
        self.assertTrue(ti.source_active("inkbunny"))
        self.assertFalse(ti.source_active("e621"))  # disabled by user
        self.assertFalse(ti.source_active("danbooru"))  # unavailable
        status = ti.source_status_map()
        self.assertEqual(status["e621"], "disabled")
        self.assertEqual(status["danbooru"], "unavailable")
        self.assertEqual(status["inkbunny"], "active")

    def test_disable_all_hash(self):
        s = Settings()
        s.sources.e621_enabled = False
        s.sources.inkbunny_enabled = False
        s.sources.danbooru_enabled = False
        s.sources.gelbooru_enabled = False
        ti = TagIntegrator(settings=s)
        ti.has_e621 = ti.has_inkbunny = ti.has_danbooru = ti.has_gelbooru = True
        ti._apply_source_toggles()
        self.assertEqual(ti.enabled_hash_services(), [])


class TestFluffleMatching(unittest.TestCase):
    def _payload(self, results):
        return {"results": results}

    def test_exact_accepted(self):
        ti = TagIntegrator(settings=Settings())
        j = self._payload([{
            "match": "exact",
            "platform": "e621",
            "location": "https://e621.net/posts/123",
            "credits": [{"name": "Artist"}],
        }])
        tags, urls, md5, pid, review = ti.find_best_exact_match(j)
        self.assertIn("creator:Artist", tags)
        self.assertIn("https://e621.net/posts/123", urls)
        self.assertEqual(pid, "123")
        self.assertIsNone(review)

    def test_non_e621_post_key_is_not_used_for_e621_enrichment(self):
        ti = TagIntegrator(settings=Settings())
        location = (
            "https://bsky.app/profile/did:plc:example/"
            "post/3m73uwpmv422d"
        )
        j = self._payload([{
            "match": "exact",
            "platform": "bluesky",
            "location": location,
            "credits": [],
        }])
        _tags, urls, _md5, pid, review = ti.find_best_exact_match(j)
        self.assertIn(location, urls)
        self.assertEqual(pid, "")
        self.assertIsNone(review)

    def test_e621_post_id_paths_still_parse(self):
        ti = TagIntegrator(settings=Settings())
        self.assertEqual(
            ti._post_id_from_url("https://e621.net/posts/456?foo=bar"),
            "456")
        self.assertEqual(
            ti._post_id_from_url("https://e621.net/post/show/789"),
            "789")
        self.assertEqual(
            ti._post_id_from_url("https://danbooru.donmai.us/posts/456"),
            "")

    def test_tossup_e621_auto(self):
        ti = TagIntegrator(settings=Settings())
        ti.fluffle_tossup_e621 = True
        j = self._payload([{
            "match": "tossUp",
            "platform": "e621",
            "location": "https://e621.net/posts/99",
            "credits": [],
        }])
        tags, urls, _, pid, review = ti.find_best_exact_match(j)
        self.assertTrue(tags or urls)
        self.assertEqual(pid, "99")
        self.assertIsNone(review)

    def test_tossup_non_e621_discarded_by_default(self):
        ti = TagIntegrator(settings=Settings())
        ti.fluffle_review_mode = "off"
        j = self._payload([{
            "match": "tossUp",
            "platform": "furaffinity",
            "location": "https://www.furaffinity.net/view/1",
            "credits": [{"name": "Someone"}],
        }])
        tags, urls, _, _, review = ti.find_best_exact_match(j)
        self.assertFalse(tags or urls)
        self.assertIsNone(review)

    def test_tossup_non_e621_queued_in_review_mode(self):
        ti = TagIntegrator(settings=Settings())
        ti.fluffle_review_mode = "tossups"
        j = self._payload([{
            "match": "tossUp",
            "platform": "furaffinity",
            "location": "https://www.furaffinity.net/view/1",
            "credits": [{"name": "Someone"}],
        }])
        tags, urls, _, _, review = ti.find_best_exact_match(j)
        self.assertFalse(tags or urls)
        self.assertIsNotNone(review)
        self.assertEqual(review["match"], "tossUp")

    def test_alternative_review_band(self):
        ti = TagIntegrator(settings=Settings())
        ti.fluffle_review_mode = "tossups_alternatives"
        j = self._payload([{
            "match": "alternative",
            "platform": "e621",
            "location": "https://e621.net/posts/5",
            "credits": [],
        }])
        _, _, _, _, review = ti.find_best_exact_match(j)
        self.assertIsNotNone(review)


def _result(match, platform, num, creator=None):
    """Small fake Fluffle result. `location` doubles as the identity marker."""
    loc = {
        "e621": f"https://e621.net/posts/{num}",
        "furaffinity": f"https://www.furaffinity.net/view/{num}",
        "twitter": f"https://twitter.com/x/status/{num}",
    }[platform]
    return {"match": match, "platform": platform, "location": loc,
            "credits": [{"name": creator}] if creator else []}


class TestFlufflePriority(unittest.TestCase):
    """The accept/reject matrix of find_best_exact_match().

    A wrong pick here injects wrong creator:/character: tags into Hydrus, so
    every class/platform/setting combination that matters is pinned down.
    """

    def _ti(self, accepted=None, tossup_e621=True, review_mode="off"):
        ti = TagIntegrator(settings=Settings())
        ti.fluffle_accepted_matches = list(accepted or ["exact"])
        ti.fluffle_tossup_e621 = tossup_e621
        ti.fluffle_review_mode = review_mode
        return ti

    def _pick(self, ti, results):
        """(chosen location or None, review location or None)."""
        _, urls, _, _, review = ti.find_best_exact_match({"results": results})
        chosen = next(iter(urls)) if urls else None
        return chosen, (review or {}).get("location")

    # ── defaults ─────────────────────────────────────────────────────────

    def test_default_settings_are_the_documented_ones(self):
        ti = TagIntegrator(settings=Settings())
        self.assertEqual(ti.fluffle_accepted_matches, ["exact"])
        self.assertTrue(ti.fluffle_tossup_e621)
        self.assertEqual(ti.fluffle_review_mode, "off")

    def test_alternative_never_beats_a_later_exact(self):
        """BUG: an early `alternative` used to outrank a genuine `exact`."""
        results = [_result("alternative", "e621", 1),
                   _result("exact", "furaffinity", 2)]
        for accepted in (["exact"], ["exact", "alternative"]):
            with self.subTest(accepted=accepted):
                chosen, review = self._pick(self._ti(accepted), results)
                self.assertEqual(chosen,
                                 "https://www.furaffinity.net/view/2")
                self.assertIsNone(review)

    def test_unlikely_never_beats_a_later_exact(self):
        results = [_result("unlikely", "e621", 1),
                   _result("exact", "furaffinity", 2)]
        chosen, _ = self._pick(self._ti(["exact", "unlikely"]), results)
        self.assertEqual(chosen, "https://www.furaffinity.net/view/2")

    def test_exact_e621_beats_exact_other_in_either_order(self):
        e621 = _result("exact", "e621", 7)
        other = _result("exact", "furaffinity", 8)
        for results in ([e621, other], [other, e621]):
            with self.subTest(order=[r["platform"] for r in results]):
                chosen, _ = self._pick(self._ti(), results)
                self.assertEqual(chosen, "https://e621.net/posts/7")

    def test_exact_beats_tossup_in_either_order(self):
        exact = _result("exact", "furaffinity", 3)
        tossup = _result("tossUp", "e621", 4)
        for results in ([tossup, exact], [exact, tossup]):
            with self.subTest(order=[r["match"] for r in results]):
                chosen, _ = self._pick(self._ti(), results)
                self.assertEqual(chosen,
                                 "https://www.furaffinity.net/view/3")

    def test_tossup_e621_accepted_by_default(self):
        chosen, review = self._pick(
            self._ti(), [_result("tossUp", "e621", 9)])
        self.assertEqual(chosen, "https://e621.net/posts/9")
        self.assertIsNone(review)

    def test_tossup_non_e621_rejected_under_e621_gate(self):
        for platform in ("furaffinity", "twitter"):
            with self.subTest(platform=platform):
                chosen, review = self._pick(
                    self._ti(), [_result("tossUp", platform, 5)])
                self.assertIsNone(chosen)
                self.assertIsNone(review)

    def test_tossup_beats_alternative_and_unlikely(self):
        results = [_result("alternative", "e621", 1),
                   _result("unlikely", "e621", 2),
                   _result("tossUp", "e621", 3)]
        chosen, _ = self._pick(
            self._ti(["exact", "alternative", "unlikely"]), results)
        self.assertEqual(chosen, "https://e621.net/posts/3")

    def test_alternative_beats_unlikely_when_both_opted_in(self):
        results = [_result("unlikely", "furaffinity", 1),
                   _result("alternative", "furaffinity", 2)]
        chosen, _ = self._pick(
            self._ti(["alternative", "unlikely"]), results)
        self.assertEqual(chosen, "https://www.furaffinity.net/view/2")

    def test_alternative_and_unlikely_rejected_by_default(self):
        for match in ("alternative", "unlikely"):
            for platform in ("e621", "furaffinity"):
                with self.subTest(match=match, platform=platform):
                    chosen, review = self._pick(
                        self._ti(), [_result(match, platform, 1)])
                    self.assertIsNone(chosen)
                    self.assertIsNone(review)

    def test_empty_accepted_list_falls_back_to_exact(self):
        chosen, _ = self._pick(self._ti([]), [_result("exact", "e621", 1)])
        self.assertEqual(chosen, "https://e621.net/posts/1")
        chosen, _ = self._pick(self._ti([]),
                               [_result("alternative", "e621", 1)])
        self.assertIsNone(chosen)

    def test_unknown_match_class_is_rejected(self):
        chosen, review = self._pick(
            self._ti(), [_result("bogus", "e621", 1)])
        self.assertIsNone(chosen)
        self.assertIsNone(review)

    # ── opt-in classes ───────────────────────────────────────────────────

    def test_tossup_opt_in_without_gate_accepts_any_platform(self):
        chosen, _ = self._pick(
            self._ti(["exact", "tossUp"], tossup_e621=False),
            [_result("tossUp", "furaffinity", 6)])
        self.assertEqual(chosen, "https://www.furaffinity.net/view/6")

    def test_tossup_opt_in_with_gate_still_rejects_non_e621(self):
        chosen, _ = self._pick(
            self._ti(["exact", "tossUp"], tossup_e621=True),
            [_result("tossUp", "furaffinity", 6)])
        self.assertIsNone(chosen)

    def test_alternative_opt_in_accepts_when_nothing_better(self):
        chosen, _ = self._pick(self._ti(["exact", "alternative"]),
                               [_result("alternative", "furaffinity", 4)])
        self.assertEqual(chosen, "https://www.furaffinity.net/view/4")

    def test_alternative_e621_preferred_over_alternative_other(self):
        results = [_result("alternative", "furaffinity", 1),
                   _result("alternative", "e621", 2)]
        chosen, _ = self._pick(self._ti(["alternative"]), results)
        self.assertEqual(chosen, "https://e621.net/posts/2")

    # ── review routing ───────────────────────────────────────────────────

    def test_review_mode_tossups_routes_tossup_not_alternative(self):
        ti = self._ti(review_mode="tossups")
        _, review = self._pick(ti, [_result("tossUp", "furaffinity", 1)])
        self.assertEqual(review, "https://www.furaffinity.net/view/1")
        _, review = self._pick(ti, [_result("alternative", "furaffinity", 2)])
        self.assertIsNone(review)

    def test_review_mode_tossups_alternatives_routes_both(self):
        ti = self._ti(review_mode="tossups_alternatives")
        _, review = self._pick(ti, [_result("tossUp", "furaffinity", 1)])
        self.assertEqual(review, "https://www.furaffinity.net/view/1")
        _, review = self._pick(ti, [_result("alternative", "e621", 2)])
        self.assertEqual(review, "https://e621.net/posts/2")

    def test_review_never_offered_for_exact_or_unlikely(self):
        ti = self._ti(accepted=[], review_mode="tossups_alternatives")
        ti.fluffle_accepted_matches = ["tossUp"]   # exact deliberately off
        _, review = self._pick(ti, [_result("exact", "furaffinity", 1)])
        self.assertIsNone(review)
        _, review = self._pick(ti, [_result("unlikely", "furaffinity", 2)])
        self.assertIsNone(review)

    def test_auto_accept_wins_over_review_candidate(self):
        ti = self._ti(review_mode="tossups_alternatives")
        results = [_result("alternative", "furaffinity", 1),
                   _result("exact", "e621", 2)]
        chosen, review = self._pick(ti, results)
        self.assertEqual(chosen, "https://e621.net/posts/2")
        self.assertIsNone(review)

    def test_review_prefers_higher_confidence_class(self):
        ti = self._ti(review_mode="tossups_alternatives")
        results = [_result("alternative", "e621", 1),
                   _result("tossUp", "furaffinity", 2)]
        _, review = self._pick(ti, results)
        self.assertEqual(review, "https://www.furaffinity.net/view/2")

    def test_opted_in_class_is_not_also_sent_to_review(self):
        ti = self._ti(["exact", "alternative"],
                      review_mode="tossups_alternatives")
        chosen, review = self._pick(ti, [_result("alternative", "e621", 1)])
        self.assertEqual(chosen, "https://e621.net/posts/1")
        self.assertIsNone(review)

    # ── malformed input ──────────────────────────────────────────────────

    def test_malformed_results_are_survivable(self):
        ti = self._ti()
        for payload in ({}, {"results": None}, {"results": []},
                        {"results": "nope"}, {"results": [None, 42]}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    ti.find_best_exact_match(payload),
                    (set(), set(), "", "", None))

    def test_missing_platform_falls_back_to_location(self):
        ti = self._ti()
        r = {"match": "tossUp", "location": "https://e621.net/posts/11",
             "credits": []}
        tags, urls, _, pid, _ = ti.find_best_exact_match({"results": [r]})
        self.assertEqual(pid, "11")   # recognised as e621 via the URL


class TestPdfPageDetection(unittest.TestCase):
    """Only real rendered PDF pages may collect comic:/page: tags."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _page(self, comic="My Comic", page=2, pdf=True):
        (self.root / f"{comic}.pdf").write_bytes(b"%PDF-1.4\n") if pdf else None
        d = self.root / comic
        d.mkdir(exist_ok=True)
        p = d / f"{comic} PAGE{page}.PNG"
        p.write_bytes(b"\x89PNG")
        return p

    def test_real_rendered_page_detected(self):
        self.assertTrue(_is_pdf_page_render(self._page()))

    def test_ordinary_png_in_a_folder_is_not_a_page(self):
        d = self.root / "Vacation2024"
        d.mkdir()
        p = d / "cat.png"
        p.write_bytes(b"\x89PNG")
        self.assertFalse(_is_pdf_page_render(p))

    def test_page_named_png_without_source_pdf_is_not_a_page(self):
        self.assertFalse(_is_pdf_page_render(self._page(pdf=False)))

    def test_page_of_a_different_pdf_is_not_a_page(self):
        (self.root / "Other.pdf").write_bytes(b"%PDF-1.4\n")
        d = self.root / "Other"
        d.mkdir()
        p = d / "Unrelated PAGE1.PNG"
        p.write_bytes(b"\x89PNG")
        self.assertFalse(_is_pdf_page_render(p))

    def test_resolve_pending_review_skips_comic_tags_for_plain_png(self):
        d = self.root / "Vacation2024"
        d.mkdir()
        p = d / "cat.png"
        p.write_bytes(b"\x89PNG")
        tags = self._approve(p)
        self.assertFalse([t for t in tags if t.startswith("comic:")])
        self.assertFalse([t for t in tags if t.startswith("page:")])

    def test_resolve_pending_review_keeps_comic_tags_for_real_page(self):
        tags = self._approve(self._page())
        self.assertIn("comic:My Comic", tags)
        self.assertIn("page:2", tags)

    def _approve(self, path):
        ti = TagIntegrator(settings=Settings())
        ti.has_e621 = ti.enabled_e621 = False
        st = path.stat()
        pending = PendingReview.create(
            path=str(path), relpath=path.name, size=st.st_size,
            mtime=st.st_mtime, md5="0" * 32, match_class="tossUp",
            platform="furaffinity", fluffle_tags=["creator:Someone"],
            fluffle_urls=["https://example.invalid/1"])
        from furtag import WriteOutcome
        with patch.object(
                ti, "write_results_detailed",
                return_value=WriteOutcome(None, True)) as writer:
            self.assertTrue(ti.resolve_pending_review(pending, True,
                                                      root=self.root))
        return writer.call_args[0][1]


class TestPdfRenderResume(unittest.TestCase):
    def test_partial_png_without_completion_manifest_is_re_rendered(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "comic.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            out_dir = root / "comic"
            out_dir.mkdir()
            (out_dir / "comic PAGE1.PNG").write_bytes(b"partial")
            ti = TagIntegrator(settings=Settings())
            with patch("furtag._import_fitz", return_value=MagicMock()):
                _page_dirs, jobs = ti.plan_pdf_renders(root)
            self.assertEqual(jobs, [pdf])

    def test_completed_render_requires_every_manifest_page(self):
        from furtag import PDF_COMPLETE_FILE

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pdf = root / "comic.pdf"
            pdf.write_bytes(b"%PDF-1.4\n")
            st = pdf.stat()
            out_dir = root / "comic"
            out_dir.mkdir()
            page1 = out_dir / "comic PAGE1.PNG"
            page2 = out_dir / "comic PAGE2.PNG"
            page1.write_bytes(b"one")
            page2.write_bytes(b"two")
            (out_dir / PDF_COMPLETE_FILE).write_text(json.dumps({
                "version": 1,
                "source": {"size": st.st_size, "mtime_ns": st.st_mtime_ns},
                "dpi": 300,
                "pages": [page1.name, page2.name],
            }), encoding="utf-8")
            ti = TagIntegrator(settings=Settings())
            with patch("furtag._import_fitz", return_value=MagicMock()):
                _page_dirs, jobs = ti.plan_pdf_renders(root)
                self.assertEqual(jobs, [])
                page2.unlink()
                _page_dirs, jobs = ti.plan_pdf_renders(root)
            self.assertEqual(jobs, [pdf])


class TestSauceNAOThresholds(unittest.TestCase):
    def test_instance_thresholds_used(self):
        s = Settings()
        s.matching.saucenao_min_similarity = 70.0
        s.matching.saucenao_auth_similarity = 85.0
        ti = TagIntegrator(settings=s)
        self.assertEqual(ti.saucenao_min_similarity, 70.0)
        self.assertEqual(ti.saucenao_auth_similarity, 85.0)

    def test_auth_boundary(self):
        ti = TagIntegrator(settings=Settings())
        ti.has_e621 = True
        ti.enabled_e621 = True
        # Exactly at auth threshold should qualify
        j = {
            "results": [{
                "header": {"similarity": str(ti.saucenao_auth_similarity)},
                "data": {"e621_id": 42},
            }]
        }
        svc, pid = ti._saucenao_best_authoritative(j, ti.saucenao_auth_similarity)
        self.assertEqual(svc, "e621")
        self.assertEqual(pid, "42")
        # Just below should not
        j["results"][0]["header"]["similarity"] = str(
            ti.saucenao_auth_similarity - 0.01)
        svc2, pid2 = ti._saucenao_best_authoritative(
            j, ti.saucenao_auth_similarity)
        self.assertIsNone(svc2)


class TestSauceNAOQuota(unittest.TestCase):
    def test_daily_exhaustion(self):
        ti = TagIntegrator(settings=Settings())
        ti.has_saucenao = True
        ti.enabled_saucenao = True
        self.assertFalse(ti.saucenao_exhausted)
        ti._saucenao_check_quota({"long_remaining": 0, "short_remaining": 1,
                                   "short_limit": 4})
        self.assertTrue(ti.saucenao_exhausted)
        # Second call does not re-notify messily
        ti._saucenao_check_quota({"long_remaining": 0})
        self.assertTrue(ti.saucenao_exhausted)

    def test_second_consecutive_429_disables_session(self):
        ti = TagIntegrator(settings=Settings())
        ti.has_saucenao = True
        ti.enabled_saucenao = True
        ti._prepare_thumb = MagicMock(return_value=MagicMock())
        ti.pace["saucenao"].wait = MagicMock()
        ti.pace["saucenao"].backoff = MagicMock()
        first = MagicMock(status_code=429)
        first.headers = {"Retry-After": "45"}
        second = MagicMock(status_code=429)
        second.headers = {}
        ti.session.post = MagicMock(side_effect=[first, second])

        with patch("furtag.notify") as notice:
            with self.assertRaises(RetryableLookupError):
                ti.saucenao_search(Path("first.png"))
            self.assertFalse(ti.saucenao_exhausted)
            ti.pace["saucenao"].backoff.assert_called_once_with(45.0)

            with self.assertRaises(RetryableLookupError):
                ti.saucenao_search(Path("second.png"))
            self.assertTrue(ti.saucenao_exhausted)
            self.assertEqual(ti.session.post.call_count, 2)

            # Once disabled, later files never spend another API request.
            with self.assertRaises(RetryableLookupError):
                ti.saucenao_search(Path("third.png"))
            self.assertEqual(ti.session.post.call_count, 2)

        messages = [call.args[0] for call in notice.call_args_list]
        self.assertTrue(any("another 429 will disable it" in m
                            for m in messages))
        self.assertTrue(any("repeatedly returned HTTP 429" in m
                            for m in messages))


class TestJunkTags(unittest.TestCase):
    def test_keywording_policy_filtered(self):
        from furtag import _is_junk_tag
        self.assertTrue(_is_junk_tag("keywording policy"))
        self.assertTrue(_is_junk_tag("Keywording_Policy"))
        self.assertTrue(_is_junk_tag("keyword policy"))
        self.assertTrue(_is_junk_tag("inkbunny keywording policy"))
        self.assertFalse(_is_junk_tag("fox"))
        self.assertFalse(_is_junk_tag("creator:scampdog"))

    def test_unknown_artist_still_filtered(self):
        from furtag import _is_junk_tag
        self.assertTrue(_is_junk_tag("unknown artist"))
        self.assertTrue(_is_junk_tag("creator:anonymous"))


class TestPdfMetaTags(unittest.TestCase):
    def test_base_tags_from_meta_file(self):
        from furtag import TagIntegrator, PDF_META_FILE
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Book.pdf").write_bytes(b"%PDF")
            d = root / "Book"
            d.mkdir()
            (d / PDF_META_FILE).write_text(
                '{"comic": "Renamed Comic", "creator": "Someone"}\n',
                encoding="utf-8")
            page = d / "Book PAGE3.PNG"
            page.write_bytes(b"\x89PNG")
            tags = TagIntegrator._pdf_page_base_tags(page)
            self.assertIn("comic:Renamed Comic", tags)
            self.assertIn("creator:Someone", tags)
            self.assertIn("page:3", tags)

    def test_base_tags_fall_back_to_folder_name(self):
        from furtag import TagIntegrator
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "Folder Comic.pdf").write_bytes(b"%PDF")
            d = root / "Folder Comic"
            d.mkdir()
            page = d / "Folder Comic PAGE1.PNG"
            page.write_bytes(b"\x89PNG")
            tags = TagIntegrator._pdf_page_base_tags(page)
            self.assertIn("comic:Folder Comic", tags)
            self.assertIn("page:1", tags)
            self.assertFalse(any(t.startswith("creator:") for t in tags))

    def test_normalize_pdf_meta_defaults(self):
        from furtag import _normalize_pdf_meta
        self.assertEqual(
            _normalize_pdf_meta("", "  artist  ", "Stem"),
            {"comic": "Stem", "creator": "artist"})
        self.assertEqual(
            _normalize_pdf_meta("My Comic", "", "Stem"),
            {"comic": "My Comic"})


class TestInkBunnyMultiFile(unittest.TestCase):
    """Multi-file IB submissions must not enter Hydrus's URL downloader."""

    def test_file_count_from_pagecount_and_files(self):
        from furtag import TagIntegrator
        self.assertEqual(TagIntegrator._inkbunny_file_count({"pagecount": 20}), 20)
        self.assertEqual(TagIntegrator._inkbunny_file_count({"pagecount": "3"}), 3)
        self.assertEqual(
            TagIntegrator._inkbunny_file_count({"files": [{}, {}, {}]}), 3)
        # files[] wins over a stale pagecount when both are present
        self.assertEqual(
            TagIntegrator._inkbunny_file_count(
                {"pagecount": 1, "files": [{}, {}]}), 2)
        self.assertEqual(TagIntegrator._inkbunny_file_count({}), 1)

    def test_multi_file_submission_url_is_force_associated(self):
        from furtag import TagIntegrator
        from furtag_settings import Settings
        from tests.test_fakes import FakeResponse, FakeSession

        multi_url = "https://inkbunny.net/s/999"
        single_url = "https://inkbunny.net/s/111"
        session = FakeSession([
            ("GET", "inkbunny.net/api_submissions.php", FakeResponse(200, {
                "submissions": [
                    {
                        "submission_id": 999,
                        "username": "artist",
                        "pagecount": 20,
                        "keywords": [{"keyword_name": "fox"}],
                    },
                    {
                        "submission_id": 111,
                        "username": "solo",
                        "pagecount": 1,
                        "keywords": [{"keyword_name": "wolf"}],
                    },
                ],
            })),
        ])
        ti = TagIntegrator(settings=Settings(), session=session)
        ti.has_inkbunny = True
        ti.ib_sid = "test-sid"
        # Avoid real rate-limit sleeps in unit tests.
        ti.pace["inkbunny"].wait = lambda: None

        tags, urls, force = ti._inkbunny_submission_tags(["999", "111"])
        self.assertIn("site:inkbunny", tags)
        self.assertIn("creator:artist", tags)
        self.assertIn("fox", tags)
        self.assertIn("wolf", tags)
        self.assertEqual(urls, {multi_url, single_url})
        self.assertEqual(force, {multi_url})


class TestUrlPartitionForceAssociate(unittest.TestCase):
    def test_force_associate_blocks_enrichment(self):
        from furtag_urls import UrlWritePolicy, partition_urls

        ib = "https://inkbunny.net/s/42"
        e6 = "https://e621.net/posts/99"
        enrich, associate = partition_urls(
            {ib, e6},
            UrlWritePolicy.ENRICH_HASH_POSTS,
            force_associate={ib},
        )
        self.assertEqual(enrich, {e6})
        self.assertEqual(associate, {ib})

    def test_single_file_ib_still_enrichable(self):
        from furtag_urls import UrlWritePolicy, partition_urls

        ib = "https://inkbunny.net/s/42"
        enrich, associate = partition_urls(
            {ib}, UrlWritePolicy.ENRICH_HASH_POSTS)
        self.assertEqual(enrich, {ib})
        self.assertEqual(associate, set())


if __name__ == "__main__":
    unittest.main()
