"""End-of-run recap: which tags are worth naming, and how elapsed time reads."""

import unittest

from furtag import LiveDisplay, format_elapsed, interesting_tags


def _names(counts, tagged=100, limit=20):
    return [tag for tag, _count in interesting_tags(counts, tagged, limit=limit)]


class TestInterestingTags(unittest.TestCase):
    def test_reported_scan_keeps_the_scene_and_drops_the_generics(self):
        # Counts stay under the "almost the whole run" cutoff so a miss here
        # is the denylist, not the frequency rule. These five are what a real
        # scan put at the top of the recap.
        counts = {
            "hi res": 40,
            "genitals": 38,
            "nude": 36,
            "clothed": 34,
            "open mouth": 32,
            "forest": 12,
            "collar": 9,
        }
        self.assertEqual(_names(counts), ["forest", "collar"])

    def test_issue_4_tags_and_their_underscore_forms(self):
        noise = (
            "animated gif", "animated_gif", "short playtime", "short_playtime",
            "genitals", "2d animation", "2d_animation", "penile", "Penile",
            "Hi Res", "hi_res", "open_mouth", "OPEN MOUTH",
        )
        for tag in noise:
            self.assertEqual(
                interesting_tags({tag: 10, "forest": 3}, tagged=30),
                [("forest", 3)],
                tag,
            )

    def test_compounds_gender_and_file_notes_drop_scene_tags_stay(self):
        counts = {
            "penile penetration": 20,
            "humanoid genitalia": 19,
            "male/female": 18,
            "cum inside": 17,
            "big breasts": 16,
            "topless": 15,
            "partially clothed": 14,
            "bodily fluids": 13,
            "english text": 12,
            "2024": 11,
            "1:1": 10,
            "artist:someone": 9,
            "ambiguous gender": 8,
            "sex toy": 7,
            "close-up": 6,
            "from_behind": 5,
            "digital media (artwork)": 4,
            "topwear": 3,
            "shirt": 8,
            "sketch": 7,
            "bed": 6,
            "watercolor (artwork)": 5,
            "wings": 4,
        }
        # Every count is below the frequency cutoff (tagged 40 → 26).
        self.assertEqual(
            _names(counts, tagged=40),
            ["shirt", "sketch", "bed", "watercolor (artwork)", "wings"],
        )

    def test_short_words_do_not_eat_unrelated_tags(self):
        kept = (
            "butterfly", "breastplate", "assembly", "cocktail", "orange",
            "analysis", "essex", "cumulus", "knotwork", "public",
        )
        counts = {tag: 3 for tag in kept}
        self.assertEqual(set(_names(counts, tagged=10)), set(kept))

    def test_a_tag_on_almost_the_whole_run_is_still_hidden(self):
        counts = {"forest": 90, "collar": 10}
        self.assertEqual(interesting_tags(counts, tagged=100), [("collar", 10)])

    def test_a_short_run_is_not_frequency_filtered(self):
        counts = {"forest": 3, "collar": 2}
        self.assertEqual(
            interesting_tags(counts, tagged=3),
            [("forest", 3), ("collar", 2)],
        )

    def test_tie_breaks_by_name_and_the_recap_stops_at_five(self):
        counts = {
            "zebra": 4, "apple": 4, "mango": 4,
            "pear": 3, "plum": 2, "fig": 1,
        }
        self.assertEqual(
            _names(counts, tagged=10, limit=5),
            ["apple", "mango", "zebra", "pear", "plum"],
        )

    def test_stored_spelling_is_what_the_recap_shows(self):
        self.assertEqual(
            interesting_tags({"Forest Clearing": 2}, tagged=4),
            [("Forest Clearing", 2)],
        )

    def test_nothing_interesting_is_an_empty_list(self):
        self.assertEqual(interesting_tags({}, tagged=10), [])
        self.assertEqual(
            interesting_tags({"hi res": 4, "nude": 3}, tagged=10), [])


class TestElapsedClock(unittest.TestCase):
    def test_always_hhmmss(self):
        self.assertEqual(format_elapsed(0), "00:00:00")
        self.assertEqual(format_elapsed(5), "00:00:05")
        self.assertEqual(format_elapsed(65), "00:01:05")
        self.assertEqual(format_elapsed(83), "00:01:23")
        self.assertEqual(format_elapsed(3600), "01:00:00")
        self.assertEqual(format_elapsed(3661), "01:01:01")
        self.assertEqual(format_elapsed(360000), "100:00:00")

    def test_truncates_and_clamps(self):
        self.assertEqual(format_elapsed(59.9), "00:00:59")
        self.assertEqual(format_elapsed(-4), "00:00:00")

    def test_terminal_clock_uses_the_same_form(self):
        self.assertEqual(LiveDisplay._fmt(65), "00:01:05")
        self.assertEqual(LiveDisplay._fmt(3661), format_elapsed(3661))


if __name__ == "__main__":
    unittest.main()
