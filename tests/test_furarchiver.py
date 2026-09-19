import tempfile
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image

from furtag import (
    HashTierResult,
    Ledger,
    PerceptualTierResult,
    SourceMetadata,
    TagIntegrator,
    WriteOutcome,
    _filename_names_artist,
    _read_furarchiver_marker,
)
from furtag_events import NullObserver
from furtag_settings import Settings


def _archive_settings() -> Settings:
    settings = Settings()
    settings.output.hydrus_enabled = False
    settings.output.sidecars_enabled = True
    settings.output.write_folder_json_report = False
    settings.pdf.pdf_enabled = False
    for name in ("e621", "inkbunny", "danbooru", "gelbooru",
                 "fluffle", "saucenao"):
        setattr(settings.sources, f"{name}_enabled", False)
    return settings


class TestFurArchiverMetadata(unittest.TestCase):
    def _make_archive(self, root: Path, artist: str = "atimon") -> None:
        (root / "_readme.txt").write_text(
            "Downloaded from furarchiver.net\n\n"
            "Download date: Mon, 07 Sep 2026 14:09:55 GMT\n"
            f"Artist: {artist}\n"
            "Files: 2 (including descriptions)\n",
            encoding="utf-8")

    def test_marker_requires_furarchiver_header_and_artist(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            archive = _read_furarchiver_marker(root)
            self.assertIsNotNone(archive)
            self.assertEqual(archive.artist, "atimon")

            (root / "_readme.txt").write_text(
                "Artist: atimon\n", encoding="utf-8")
            self.assertIsNone(_read_furarchiver_marker(root))

    def test_artist_match_uses_filename_tokens(self):
        self.assertTrue(_filename_names_artist(
            "1638741080.atimon_portrait_fa.png", "atimon"))
        self.assertTrue(_filename_names_artist(
            "123.foo_bar-picture.jpg", "Foo Bar"))
        self.assertFalse(_filename_names_artist(
            "123.notatimon_portrait.png", "atimon"))

    def test_archive_adds_tags_and_unique_html_description(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            media = root / "123.atimon_picture.png"
            media.write_bytes(b"image")
            Path(str(media) + ".html").write_text(
                "<html><pre>Hello &amp; goodbye.<br>Second line.</pre></html>",
                encoding="utf-8")
            ti = TagIntegrator(_archive_settings())
            ti._begin_furarchiver_scan(root)
            metadata = SourceMetadata()

            self.assertTrue(ti._merge_furarchiver_metadata(metadata, media))
            self.assertEqual(metadata.tags, {
                "creator:atimon", "site:furarchiver", "site:furaffinity"})
            self.assertEqual(metadata.notes, {
                "furaffinity description":
                    "Hello & goodbye.\nSecond line."})

    def test_sibling_descriptions_directory_is_supported(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root, artist="s1m")
            images = root / "Images"
            descriptions = root / "Descriptions"
            images.mkdir()
            descriptions.mkdir()
            media = images / "1659830416.s1m_beastars-trio2.png"
            media.write_bytes(b"image")
            (descriptions / f"{media.name}.html").write_text(
                "<html><pre>It's Haru's turn.</pre></html>",
                encoding="utf-8")
            ti = TagIntegrator(_archive_settings())
            ti._begin_furarchiver_scan(root)
            metadata = SourceMetadata()

            self.assertTrue(ti._merge_furarchiver_metadata(metadata, media))
            self.assertEqual(metadata.tags, {
                "creator:s1m", "site:furarchiver", "site:furaffinity"})
            self.assertEqual(metadata.notes, {
                "furaffinity description": "It's Haru's turn."})

    def test_exact_description_from_another_source_is_not_duplicated(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            media = root / "123.atimon_picture.png"
            media.write_bytes(b"image")
            Path(str(media) + ".html").write_text(
                "<pre>Same description</pre>", encoding="utf-8")
            ti = TagIntegrator(_archive_settings())
            ti._begin_furarchiver_scan(root)
            metadata = SourceMetadata(notes={"e621 description":
                                             "Same description"})

            ti._merge_furarchiver_metadata(metadata, media)

            self.assertEqual(metadata.notes, {
                "e621 description": "Same description"})

    def test_nonmatching_filename_gets_description_but_not_archive_tags(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            media = root / "wandered_in.png"
            media.write_bytes(b"image")
            Path(str(media) + ".html").write_text(
                "<pre>Local description</pre>", encoding="utf-8")
            ti = TagIntegrator(_archive_settings())
            ti._begin_furarchiver_scan(root)
            metadata = SourceMetadata()

            ti._merge_furarchiver_metadata(metadata, media)

            self.assertEqual(metadata.tags, set())
            self.assertEqual(list(metadata.notes.values()), [
                "Local description"])

    def test_old_resolved_row_reopens_once_for_archive_backfill(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            media = root / "123.atimon_picture.png"
            media.write_bytes(b"image")
            st = media.stat()
            ledger = Ledger(root)
            ledger.record(
                media.name, st.st_size, st.st_mtime, "abc", "nomatch", [],
                mtime_ns=st.st_mtime_ns)
            ti = TagIntegrator(_archive_settings())
            ti._begin_furarchiver_scan(root)

            self.assertFalse(ti.local_path_complete(
                media, ledger, st, root=root))

    def test_no_search_hit_still_writes_archive_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._make_archive(root)
            media = root / "123.atimon_picture.png"
            Image.new("RGB", (8, 8), "purple").save(media)
            Path(str(media) + ".html").write_text(
                "<pre>Archived description</pre>", encoding="utf-8")
            ti = TagIntegrator(_archive_settings())
            ti.hash_tier = mock.MagicMock(return_value=
                HashTierResult(SourceMetadata(), []))
            ti.perceptual_tier = mock.MagicMock(return_value=
                PerceptualTierResult(SourceMetadata(), [], None))
            written = []

            def capture(path, tags, urls, known_sha256=None, **kwargs):
                written.append((path, set(tags), dict(kwargs.get("notes") or {})))
                return WriteOutcome(None, True, hydrus_complete=True,
                                    sidecar_complete=True)

            ti.write_results_detailed = capture
            summary = ti.run(
                root, observer=NullObserver(), use_terminal_display=False)

            self.assertEqual(len(written), 1)
            self.assertEqual(summary.tagged, 1)
            self.assertEqual(summary.source_hits["furarchiver"], 1)
            self.assertEqual(written[0][1], {
                "creator:atimon", "site:furarchiver", "site:furaffinity"})
            self.assertEqual(list(written[0][2].values()), [
                "Archived description"])


if __name__ == "__main__":
    unittest.main()
