"""Sidecar format detection, dual-format read, legacy recognition."""

import json
import tempfile
import unittest
from pathlib import Path

from furtag import TagIntegrator, _nuke_candidates
from furtag_settings import Settings


class TestSidecarIO(unittest.TestCase):
    def test_legacy_txt_recognized_when_json_format(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "cat.jpg"
            media.write_bytes(b"img")
            # Legacy sidecar
            (root / "cat.jpg.txt").write_text("creator:alice\n", encoding="utf-8")
            s = Settings()
            s.output.sidecar_format = "json"
            ti = TagIntegrator(settings=s)
            self.assertTrue(ti.has_sidecar(media))
            tags, urls = ti.read_sidecar_payload(media)
            self.assertIn("creator:alice", tags)

    def test_json_write_and_read(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "dog.png"
            media.write_bytes(b"img")
            s = Settings()
            s.output.sidecar_format = "json"
            ti = TagIntegrator(settings=s)
            ti._write_sidecar_results(
                media, {"creator:bob", "character:rex"},
                {"https://example.com/1"})
            self.assertTrue(ti.has_sidecar(media))
            tags, urls = ti.read_sidecar_payload(media)
            self.assertIn("creator:bob", tags)
            self.assertIn("character:rex", tags)
            self.assertIn("https://example.com/1", urls)
            # Append merges
            ti._write_sidecar_results(media, {"species:canine"}, set())
            tags2, _ = ti.read_sidecar_payload(media)
            self.assertIn("species:canine", tags2)
            self.assertIn("creator:bob", tags2)

    def test_txt_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "bird.webp"
            media.write_bytes(b"img")
            ti = TagIntegrator(settings=Settings())
            ti._write_sidecar_results(
                media, {"creator:carol"}, {"https://e621.net/posts/9"})
            self.assertTrue(ti.tag_sidecar_path(media).exists())
            self.assertTrue(ti.url_sidecar_path(media).exists())
            tags, urls = ti.read_sidecar_payload(media)
            self.assertIn("creator:carol", tags)
            self.assertIn("https://e621.net/posts/9", urls)

    def test_switching_to_json_does_not_lose_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "fox.jpg"
            media.write_bytes(b"img")
            ti_txt = TagIntegrator(settings=Settings())
            ti_txt._write_sidecar_results(media, {"creator:old"}, set())
            s = Settings()
            s.output.sidecar_format = "json"
            ti_json = TagIntegrator(settings=s)
            # Index skip: has_sidecar must still be true
            self.assertTrue(ti_json.has_sidecar(media))
            items, _ = ti_json.index(root, __import__("furtag").LedgerManager(), set())
            # Already has sidecar → not in to-process list
            self.assertEqual(len(items), 0)


class TestEmptyResultsNeverWriteSidecar(unittest.TestCase):
    """An all-junk result must not leave a permanent 'already tagged' marker."""

    def test_json_empty_payload_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "empty.jpg"
            media.write_bytes(b"img")
            s = Settings()
            s.output.sidecar_format = "json"
            ti = TagIntegrator(settings=s)
            ti._write_sidecar_results(media, set(), set())
            self.assertFalse(ti.json_sidecar_path(media).exists())
            self.assertFalse(ti.has_sidecar(media))

    def test_json_empty_payload_does_not_clobber_existing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "kept.jpg"
            media.write_bytes(b"img")
            s = Settings()
            s.output.sidecar_format = "json"
            ti = TagIntegrator(settings=s)
            ti._write_sidecar_results(media, {"creator:dana"}, set())
            ti._write_sidecar_results(media, set(), set())
            tags, _ = ti.read_sidecar_payload(media)
            self.assertIn("creator:dana", tags)

    def test_txt_empty_payload_not_written(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "empty.png"
            media.write_bytes(b"img")
            ti = TagIntegrator(settings=Settings())
            ti._write_sidecar_results(media, set(), set())
            self.assertFalse(ti.tag_sidecar_path(media).exists())
            self.assertFalse(ti.url_sidecar_path(media).exists())


class TestNukeCandidateClassification(unittest.TestCase):
    """Reset must never delete JSON that FurTag did not write (e.g. gallery-dl)."""

    def _candidates(self, root: Path, settings: Settings):
        _, sidecars = _nuke_candidates(root, settings)
        return {p.name for p in sidecars}

    def test_gallerydl_metadata_json_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "image.jpg").write_bytes(b"img")
            # gallery-dl's default metadata sidecar: same name shape, other body
            (root / "image.jpg.json").write_text(json.dumps({
                "category": "e621", "id": 123, "filename": "image",
                "tags": ["fox", "solo"], "extension": "jpg",
            }), encoding="utf-8")
            names = self._candidates(root, Settings())
            self.assertNotIn("image.jpg.json", names)

    def test_furtag_json_sidecar_is_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "real.png").write_bytes(b"img")
            s = Settings()
            s.output.sidecar_format = "json"
            ti = TagIntegrator(settings=s)
            ti._write_sidecar_results(
                Path(root / "real.png"), {"creator:eve"},
                {"https://e621.net/posts/1"})
            names = self._candidates(root, s)
            self.assertIn("real.png.json", names)

    def test_txt_sidecars_still_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.jpg").write_bytes(b"img")
            (root / "a.jpg.txt").write_text("creator:f\n", encoding="utf-8")
            (root / "a.jpg.urls.txt").write_text("https://x/1\n", encoding="utf-8")
            (root / "notes.txt").write_text("mine\n", encoding="utf-8")
            names = self._candidates(root, Settings())
            self.assertEqual(names, {"a.jpg.txt", "a.jpg.urls.txt"})

    def test_custom_json_pattern_recognized(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "b.jpg").write_bytes(b"img")
            s = Settings()
            s.output.sidecar_format = "json"
            s.output.sidecar_json_filename = "{name}{ext}.furtag.json"
            ti = TagIntegrator(settings=s)
            ti._write_sidecar_results(root / "b.jpg", {"creator:g"}, set())
            self.assertTrue((root / "b.jpg.furtag.json").exists())
            names = self._candidates(root, s)
            self.assertIn("b.jpg.furtag.json", names)

    def test_non_json_object_json_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "c.jpg").write_bytes(b"img")
            (root / "c.jpg.json").write_text("[1, 2, 3]", encoding="utf-8")
            (root / "d.jpg").write_bytes(b"img")
            (root / "d.jpg.json").write_text("not json at all", encoding="utf-8")
            names = self._candidates(root, Settings())
            self.assertEqual(names, set())


class TestForeignJsonNotOurSidecar(unittest.TestCase):
    """`<media>.<ext>.json` is also gallery-dl's default metadata name.

    Treating a foreign file as ours has two distinct consequences, and both had
    to be fixed: Reset would delete it, and — the subtler one — `has_sidecar`
    would silently exclude the media from every future scan.
    """

    GALLERY_DL = {"category": "twitter", "id": 123, "tags": ["foreign_tag"]}
    FURTAG = {"tags": ["creator:real"], "urls": ["https://e621.net/posts/1"]}

    def _tree(self, td):
        root = Path(td)
        for name, payload in (("p0.png", self.GALLERY_DL), ("p1.png", self.FURTAG)):
            (root / name).write_bytes(b"img")
            (root / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        return root, TagIntegrator(settings=Settings())

    def test_foreign_json_does_not_mark_media_as_having_a_sidecar(self):
        with tempfile.TemporaryDirectory() as td:
            root, ti = self._tree(td)
            self.assertFalse(ti.has_sidecar(root / "p0.png"))
            self.assertTrue(ti.has_sidecar(root / "p1.png"))

    def test_foreign_json_tags_are_never_ingested(self):
        with tempfile.TemporaryDirectory() as td:
            root, ti = self._tree(td)
            self.assertEqual(ti.read_sidecar_payload(root / "p0.png"), (set(), set()))
            tags, urls = ti.read_sidecar_payload(root / "p1.png")
            self.assertEqual(tags, {"creator:real"})
            self.assertEqual(urls, {"https://e621.net/posts/1"})

    def test_media_beside_foreign_json_is_still_indexed(self):
        with tempfile.TemporaryDirectory() as td:
            root, ti = self._tree(td)
            items = {p.path.name for p in ti.discover(root)["items"]}
            self.assertIn("p0.png", items)      # gallery-dl neighbour ignored
            self.assertNotIn("p1.png", items)   # real FurTag sidecar → skipped

    def test_foreign_json_is_not_deletable_by_reset(self):
        with tempfile.TemporaryDirectory() as td:
            root, _ = self._tree(td)
            _, sidecars = _nuke_candidates(root, Settings())
            names = {p.name for p in sidecars}
            self.assertNotIn("p0.png.json", names)
            self.assertIn("p1.png.json", names)


if __name__ == "__main__":
    unittest.main()
