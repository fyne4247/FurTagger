"""Oversized-source thumbnailing: ImageMagick hand-off and permanent skips."""

import subprocess
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import furtag
from furtag import RetryableMediaError, TagIntegrator, THUMB_SOURCE_MAX_PIXELS
from furtag_settings import Settings


def _png_bytes(size=(64, 48)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", size, "green").save(buf, "PNG")
    return buf.getvalue()


class TestOversizedThumbnails(unittest.TestCase):
    def setUp(self):
        self.ti = TagIntegrator(settings=Settings())
        self.tmp = tempfile.TemporaryDirectory()
        self.img = Path(self.tmp.name) / "huge.png"
        Image.new("RGB", (32, 32), "red").save(self.img)
        # Report a pixel count past the in-process ceiling without allocating it.
        side = int(THUMB_SOURCE_MAX_PIXELS ** 0.5) + 1000
        self.size_patch = patch.object(
            Image.Image, "size", property(lambda _self: (side, side)))

    def tearDown(self):
        self.tmp.cleanup()

    def test_oversized_source_is_handed_to_imagemagick(self):
        # Built before the size patch, which would otherwise corrupt the save.
        prepared = BytesIO(_png_bytes())
        with self.size_patch, patch.object(
                TagIntegrator, "_prepare_thumb_external",
                return_value=prepared) as ext:
            buf = self.ti._prepare_thumb(self.img)
        ext.assert_called_once()
        self.assertEqual(Image.open(buf).size, (64, 48))

    def test_oversized_source_without_imagemagick_is_a_permanent_skip(self):
        # None (unusable) rather than RetryableMediaError, so the file is not
        # re-attempted on every scan forever.
        with self.size_patch, patch.object(
                TagIntegrator, "_prepare_thumb_external", return_value=None):
            self.assertIsNone(self.ti._prepare_thumb(self.img))

    def test_normal_source_never_shells_out(self):
        with patch.object(TagIntegrator, "_prepare_thumb_external") as ext:
            buf = self.ti._prepare_thumb(self.img)
        ext.assert_not_called()
        self.assertIsNotNone(buf)


class TestPrepareThumbExternal(unittest.TestCase):
    def setUp(self):
        self.ti = TagIntegrator(settings=Settings())
        self.img = Path("/nonexistent/huge.png")
        furtag._MAGICK_CMD = ["magick"]
        furtag._MAGICK_MISSING_NOTIFIED = False

    def tearDown(self):
        furtag._MAGICK_CMD = "unset"
        furtag._MAGICK_MISSING_NOTIFIED = False

    def test_returns_buffer_on_success(self):
        done = subprocess.CompletedProcess([], 0, _png_bytes(), b"")
        with patch("subprocess.run", return_value=done) as run:
            buf = self.ti._prepare_thumb_external(self.img)
        self.assertEqual(Image.open(buf).size, (64, 48))
        self.assertIn("-thumbnail", run.call_args[0][0])

    def test_nonzero_exit_is_retryable(self):
        done = subprocess.CompletedProcess([], 1, b"", b"magick: no decode")
        with patch("subprocess.run", return_value=done):
            with self.assertRaises(RetryableMediaError):
                self.ti._prepare_thumb_external(self.img)

    def test_timeout_is_retryable(self):
        with patch("subprocess.run",
                   side_effect=subprocess.TimeoutExpired("magick", 180)):
            with self.assertRaises(RetryableMediaError):
                self.ti._prepare_thumb_external(self.img)

    def test_launch_error_is_retryable(self):
        with patch("subprocess.run", side_effect=OSError("process unavailable")):
            with self.assertRaises(RetryableMediaError):
                self.ti._prepare_thumb_external(self.img)

    def test_missing_binary_returns_none_and_warns_once(self):
        furtag._MAGICK_CMD = None
        with patch("furtag.notify") as notice:
            self.assertIsNone(self.ti._prepare_thumb_external(self.img))
            self.assertIsNone(self.ti._prepare_thumb_external(self.img))
        notice.assert_called_once()

    def test_decoder_profile_silently_probes_missing_binary(self):
        furtag._MAGICK_CMD = "unset"
        with patch("furtag.shutil.which", return_value=None), \
                patch("furtag.notify") as notice:
            profile = self.ti.decoder_profile()
            self.assertIn("magick=no", profile)
            notice.assert_not_called()
            self.assertIsNone(self.ti._prepare_thumb_external(self.img))
            self.assertIsNone(self.ti._prepare_thumb_external(self.img))
        notice.assert_called_once()


if __name__ == "__main__":
    unittest.main()
