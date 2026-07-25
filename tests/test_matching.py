"""Matching thresholds, Fluffle review band, SauceNAO boundaries, source toggles."""

import unittest
from unittest.mock import MagicMock, patch

from furtag import TagIntegrator
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


if __name__ == "__main__":
    unittest.main()
