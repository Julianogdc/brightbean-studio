"""Tests for the Pillow seam in ``apps.media_library.services``.

These paths had no coverage at all, which is uncomfortable given what they do:
they are the only place the worker decodes attacker-supplied pixel data, and an
unbounded decode is what put the Heroku worker over its 512 MB quota. The cases
below pin the two properties that matter — the decode is bounded, and the
thumbnail still looks right for every format we accept.
"""

import io

from django.test import SimpleTestCase, override_settings
from PIL import Image

from apps.media_library.services import (
    ImageTooLargeError,
    apply_image_edits,
    extract_image_metadata,
    generate_image_thumbnail,
    open_image,
)


def _encode(img, fmt):
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    buf.seek(0)
    return buf


def _jpeg(width=1200, height=800, mode="RGB"):
    return _encode(Image.new(mode, (width, height), (120, 30, 200)), "JPEG")


def _alpha_png(width=600, height=400):
    return _encode(Image.new("RGBA", (width, height), (10, 200, 90, 128)), "PNG")


def _palette_png(width=600, height=400):
    return _encode(Image.new("RGB", (width, height), (200, 40, 40)).convert("P"), "PNG")


class OpenImageGuardTest(SimpleTestCase):
    def test_rejects_an_image_over_the_pixel_limit(self):
        # 600x400 = 240_000 px, so a limit just under it must reject.
        with (
            override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=200_000),
            self.assertRaises(ImageTooLargeError),
            open_image(_alpha_png()),
        ):
            pass

    def test_error_names_the_dimensions_and_the_limit(self):
        with (
            override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=200_000),
            self.assertRaises(ImageTooLargeError) as ctx,
            open_image(_alpha_png()),
        ):
            pass
        message = str(ctx.exception)
        assert "600x400" in message
        assert "megapixels" in message

    def test_allows_an_image_under_the_limit(self):
        with override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=1_000_000), open_image(_alpha_png()) as img:
            assert img.size == (600, 400)

    def test_draft_lets_a_large_jpeg_through_on_its_reduced_size(self):
        """A JPEG's header dimensions are not what it costs us to decode.

        The decoder downscales during the read, so judging a JPEG on its full
        size would refuse a file that never allocates that much. 2400x1600 is
        3.84M px, but drafted for a 400x400 thumbnail it decodes far smaller.
        """
        with (
            override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=1_000_000),
            open_image(_jpeg(2400, 1600), draft_size=(400, 400)) as img,
        ):
            assert img.width * img.height <= 1_000_000

    def test_a_png_of_the_same_size_is_rejected(self):
        """PNG has no draft support, so it really does decode full-size."""
        with (
            override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=1_000_000),
            self.assertRaises(ImageTooLargeError),
            open_image(_alpha_png(2400, 1600), draft_size=(400, 400)),
        ):
            pass

    def test_does_not_close_a_caller_supplied_file_object(self):
        """``_process_image`` opens the same FieldFile twice, in sequence."""
        handle = _alpha_png()
        with open_image(handle):
            pass
        assert not handle.closed
        with open_image(handle) as img:
            assert img.size == (600, 400)


class GenerateImageThumbnailTest(SimpleTestCase):
    def test_rgb_jpeg(self):
        thumb = generate_image_thumbnail(_jpeg())
        assert thumb is not None
        with Image.open(io.BytesIO(thumb.read())) as out:
            assert out.format == "JPEG"
            assert out.mode == "RGB"
            assert max(out.size) <= 400

    def test_alpha_png_is_flattened_onto_white(self):
        thumb = generate_image_thumbnail(_alpha_png())
        assert thumb is not None
        with Image.open(io.BytesIO(thumb.read())) as out:
            assert out.mode == "RGB"
            assert max(out.size) <= 400

    def test_palette_png(self):
        """Mode "P" is promoted to RGBA before the resize.

        Pillow forces NEAREST resampling on palette images, so skipping the
        promotion visibly degrades the thumbnail.
        """
        thumb = generate_image_thumbnail(_palette_png())
        assert thumb is not None
        with Image.open(io.BytesIO(thumb.read())) as out:
            assert out.mode == "RGB"
            assert max(out.size) <= 400

    def test_cmyk_jpeg(self):
        source = _encode(Image.new("CMYK", (1200, 800), (10, 20, 30, 40)), "JPEG")
        thumb = generate_image_thumbnail(source)
        assert thumb is not None
        with Image.open(io.BytesIO(thumb.read())) as out:
            assert out.mode == "RGB"

    def test_preserves_aspect_ratio(self):
        thumb = generate_image_thumbnail(_jpeg(1200, 400))
        with Image.open(io.BytesIO(thumb.read())) as out:
            assert out.size == (400, 133)

    def test_returns_none_over_the_limit_rather_than_raising(self):
        """``_process_image`` treats a falsy thumbnail as "skip it"."""
        with override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=200_000):
            assert generate_image_thumbnail(_alpha_png()) is None

    def test_returns_none_on_a_file_that_is_not_an_image(self):
        assert generate_image_thumbnail(io.BytesIO(b"not an image at all")) is None


class ExtractImageMetadataTest(SimpleTestCase):
    def test_reports_real_dimensions_not_drafted_ones(self):
        assert extract_image_metadata(_jpeg(2400, 1600)) == {"width": 2400, "height": 1600}

    def test_returns_empty_over_the_limit(self):
        with override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=200_000):
            assert extract_image_metadata(_alpha_png()) == {}

    def test_returns_empty_on_unreadable_input(self):
        assert extract_image_metadata(io.BytesIO(b"nope")) == {}


class ApplyImageEditsTest(SimpleTestCase):
    def test_no_operations_still_returns_a_file(self):
        """Regression: the save used to sit outside the open context.

        With no operations the working image IS the opened one, so leaving the
        context first closed the file pointer out from under ``save()``.
        """
        edited, size = apply_image_edits(_jpeg(800, 600), {})
        assert size == (800, 600)
        assert edited.size > 0

    def test_crop(self):
        edited, size = apply_image_edits(_jpeg(800, 600), {"crop": {"x": 10, "y": 20, "width": 100, "height": 50}})
        assert size == (100, 50)
        with Image.open(io.BytesIO(edited.read())) as out:
            assert out.size == (100, 50)

    def test_rotate_expands(self):
        _, size = apply_image_edits(_jpeg(800, 600), {"rotate": 90})
        assert size == (600, 800)

    def test_resize(self):
        _, size = apply_image_edits(_jpeg(800, 600), {"resize": {"width": 320, "height": 240}})
        assert size == (320, 240)

    def test_alpha_source_is_written_as_png(self):
        edited, _ = apply_image_edits(_alpha_png(), {"rotate": 180})
        assert edited.name.endswith(".png")

    def test_raises_over_the_limit(self):
        """Unlike the thumbnail path, this propagates: the edit has failed."""
        with override_settings(MEDIA_LIBRARY_MAX_IMAGE_PIXELS=200_000), self.assertRaises(ImageTooLargeError):
            apply_image_edits(_alpha_png(), {"rotate": 90})
