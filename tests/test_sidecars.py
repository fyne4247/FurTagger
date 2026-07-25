"""Sidecar format detection, dual-format read, legacy recognition."""

import json
import tempfile
import unittest
from pathlib import Path

from furtag import TagIntegrator
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


if __name__ == "__main__":
    unittest.main()
