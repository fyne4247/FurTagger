"""Sidecar format detection, dual-format read, legacy recognition."""

import json
import tempfile
import unittest
from pathlib import Path

from furtag import TagIntegrator, _nuke_candidates, perform_nuke
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


class TestSelectiveNuke(unittest.TestCase):
    def _tree(self, root: Path):
        (root / ".furtag_ledger.json").write_text("{}", encoding="utf-8")
        (root / "duplicates.log").write_text("report", encoding="utf-8")
        (root / "image.jpg").write_bytes(b"img")
        (root / "image.jpg.txt").write_text(
            "creator:alice\n", encoding="utf-8")
        pdf = root / "comic.pdf"
        pdf.write_bytes(b"pdf")
        page_dir = root / "comic"
        page_dir.mkdir()
        page = page_dir / "comic PAGE1.PNG"
        page.write_bytes(b"png")
        return page

    def test_can_remove_only_ledgers_and_reports(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = self._tree(root)

            removed, failures = perform_nuke(
                root,
                include_pdf_pages=False,
                include_ledgers_reports=True,
                include_sidecars=False,
                settings=Settings(),
            )

            self.assertEqual(removed, 2)
            self.assertEqual(failures, [])
            self.assertFalse((root / ".furtag_ledger.json").exists())
            self.assertFalse((root / "duplicates.log").exists())
            self.assertTrue((root / "image.jpg.txt").exists())
            self.assertTrue(page.exists())

    def test_can_remove_only_sidecars(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = self._tree(root)

            removed, failures = perform_nuke(
                root,
                include_pdf_pages=False,
                include_ledgers_reports=False,
                include_sidecars=True,
                settings=Settings(),
            )

            self.assertEqual(removed, 1)
            self.assertEqual(failures, [])
            self.assertTrue((root / ".furtag_ledger.json").exists())
            self.assertTrue((root / "duplicates.log").exists())
            self.assertFalse((root / "image.jpg.txt").exists())
            self.assertTrue(page.exists())

    def test_can_remove_only_rendered_pdf_pages(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            page = self._tree(root)

            removed, failures = perform_nuke(
                root,
                include_pdf_pages=True,
                include_ledgers_reports=False,
                include_sidecars=False,
                settings=Settings(),
            )

            self.assertEqual(removed, 1)
            self.assertEqual(failures, [])
            self.assertTrue((root / ".furtag_ledger.json").exists())
            self.assertTrue((root / "duplicates.log").exists())
            self.assertTrue((root / "image.jpg.txt").exists())
            self.assertFalse(page.exists())
            self.assertFalse(page.parent.exists())


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


class TestForeignTxtNotOurSidecar(unittest.TestCase):
    """`<media>.<ext>.txt` is also a hand-written note / gallery-dl tag dump.

    Reset deletes with ``unlink()`` — no trash, no undo — so a name match alone
    must never mark a `.txt` deletable, exactly as for the JSON branch. The bias
    is toward keeping: a missed sidecar is clutter, a wrong delete is gone.
    """

    def _candidates(self, root: Path, settings=None):
        _, sidecars = _nuke_candidates(root, settings or Settings())
        return {p.name for p in sidecars}

    def test_prose_opening_with_a_namespace_word_is_not_deletable(self):
        """A notes file may legitimately start ``title:...``.

        Spaces have to be allowed inside a namespaced value (SauceNAO writes
        real titles), so the namespace check alone would claim this file. A
        spaced *un-namespaced* line is prose and must disqualify the file.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "notes.jpg").write_bytes(b"img")
            (root / "notes.jpg.txt").write_text(
                "title:my great idea\n"
                "and then some rambling prose line here\n",
                encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_namespaced_value_with_spaces_is_still_ours(self):
        """The prose guard must not reject genuine spaced values."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "s.jpg").write_bytes(b"img")
            (root / "s.jpg.txt").write_text(
                "title:Some Comic Name\ncreator:alice\nfur\n", encoding="utf-8")
            self.assertEqual(self._candidates(root), {"s.jpg.txt"})

    def test_written_tag_and_url_sidecars_are_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "real.jpg"
            media.write_bytes(b"img")
            ti = TagIntegrator(settings=Settings())
            ti._write_sidecar_results(
                media, {"creator:alice", "solo", "anthro"},
                {"https://e621.net/posts/1"})
            self.assertEqual(self._candidates(root),
                             {"real.jpg.txt", "real.jpg.urls.txt"})

    def test_pdf_page_base_tag_sidecar_is_deletable(self):
        # convert_pdf writes only comic:/page: (and maybe creator:) base tags.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "comic PAGE1.PNG").write_bytes(b"png")
            (root / "comic PAGE1.PNG.txt").write_text(
                "comic:my comic\npage:1\n", encoding="utf-8")
            self.assertEqual(self._candidates(root), {"comic PAGE1.PNG.txt"})

    def test_handwritten_note_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "commission.png").write_bytes(b"img")
            (root / "commission.png.txt").write_text(
                "Paid $50 on the 3rd, revisions due Friday.\n"
                "Client wants the background swapped out.\n", encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_gallerydl_write_tags_output_is_not_deletable(self):
        # gallery-dl --write-tags: one bare tag per line, no FurTag namespaces.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "post.jpg").write_bytes(b"img")
            (root / "post.jpg.txt").write_text(
                "fox\nsolo\nmale\ndigital_media_(artwork)\n", encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_stable_diffusion_prompt_file_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "render.png").write_bytes(b"img")
            (root / "render.png.txt").write_text(
                "masterpiece, best quality, anthro wolf, forest background\n"
                "Negative prompt: blurry, extra limbs\n"
                "Steps: 30, Sampler: DPM++ 2M, CFG scale: 7, Seed: 12345\n",
                encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_empty_txt_is_not_deletable(self):
        # FurTag never writes an empty text sidecar, so an empty one isn't ours.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "e.jpg").write_bytes(b"img")
            (root / "e.jpg.txt").write_text("", encoding="utf-8")
            (root / "w.jpg").write_bytes(b"img")
            (root / "w.jpg.txt").write_text("\n  \n\n", encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_non_utf8_txt_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "b.jpg").write_bytes(b"img")
            (root / "b.jpg.txt").write_bytes(b"creator:\xff\xfe caf\xe9\n")
            self.assertEqual(self._candidates(root), set())

    def test_oversized_txt_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "big.jpg").write_bytes(b"img")
            (root / "big.jpg.txt").write_text(
                "creator:alice\n" + "solo\n" * 300_000, encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_urls_sidecar_with_non_url_lines_is_not_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "u.jpg").write_bytes(b"img")
            (root / "u.jpg.urls.txt").write_text(
                "https://e621.net/posts/1\nremember to check this one\n",
                encoding="utf-8")
            self.assertEqual(self._candidates(root), set())

    def test_urls_sidecar_of_only_urls_is_deletable(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "v.jpg").write_bytes(b"img")
            (root / "v.jpg.urls.txt").write_text(
                "https://e621.net/posts/1\nhttp://example.com/a?b=c\n",
                encoding="utf-8")
            self.assertEqual(self._candidates(root), {"v.jpg.urls.txt"})

    def test_perform_nuke_removes_ours_and_spares_theirs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for name in ("mine.jpg", "theirs.jpg"):
                (root / name).write_bytes(b"img")
            (root / "mine.jpg.txt").write_text(
                "creator:alice\nsolo\n", encoding="utf-8")
            (root / "theirs.jpg.txt").write_text(
                "My own notes about this picture, kept for years.\n",
                encoding="utf-8")

            removed, failures = perform_nuke(
                root, include_pdf_pages=False, include_ledgers_reports=False,
                include_sidecars=True, settings=Settings())

            self.assertEqual((removed, failures), (1, []))
            self.assertFalse((root / "mine.jpg.txt").exists())
            self.assertTrue((root / "theirs.jpg.txt").exists())


if __name__ == "__main__":
    unittest.main()
