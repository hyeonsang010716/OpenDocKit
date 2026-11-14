"""Assembly (blank lines in a code block of a section that has a picture) and image files (very large pictures, animated AVIF)."""

import base64
from io import BytesIO

from PIL import Image
import pytest

from opendockit import ConvertOptions, DocumentFormat, convert
from opendockit.exceptions import DocumentTooLargeError
from tests import builder


_CODE = "<pre>def a():\n    pass\n\n\n\ndef b():\n    pass</pre>"
_PICTURE = '<p>그림</p><img alt="도식" src="data:image/png;base64,' + base64.b64encode(builder.png_bytes()).decode() + '">'


class TestBlankLinesInCodeNextToAPicture:
    """For a section with a picture, assembly (`_render_template`) collapsed blank lines again across the whole section, so the
    consecutive blank lines inside a code block that `clean_text` had preserved shrank to one (without a picture they stayed).
    Lines inside a fence are not collapsed."""

    def test_blank_lines_inside_a_code_block_are_kept_when_the_section_has_a_picture(self):
        data = (_CODE + _PICTURE).encode()

        result = convert(data, "a.html")

        assert result.text == "```\ndef a():\n    pass\n\n\n\ndef b():\n    pass\n```\n\n그림\n\n[Image: 도식]"

    def test_blank_lines_outside_code_blocks_are_still_collapsed_around_a_picture(self):
        data = ("<p>앞 문단 " + _PICTURE.removeprefix("<p>그림</p>") + "</p><p>뒤</p>").encode()

        result = convert(data, "a.html")

        assert result.text == "앞 문단\n\n[Image: 도식]\n\n뒤"

    def test_limit_is_exact_for_a_section_with_a_code_block_and_a_picture(self):
        data = (_CODE + _PICTURE).encode()
        length = convert(data, "a.html").char_count

        results = [convert(data, "a.html", options=ConvertOptions(max_chars=limit)) for limit in (length - 1, length)]

        assert [result.exceeded for result in results] == [True, False]


def _jpeg(width: int, height: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 180, 160)).save(buffer, format="JPEG", quality=50)
    return buffer.getvalue()


class TestVeryLargePictures:
    """Valid photos (panoramas, large scans) over Pillow's decompression-bomb limit (about 179 million pixels) were rejected as a
    "corrupted image". A JPEG is read reduced to 1/2–1/8 at decode time (draft); if it is still over the limit, or the format
    cannot be reduced, it is reported as a "document too large". The tests lower both limits to take the same path with small
    pictures."""

    @pytest.fixture(autouse=True)
    def _small_pixel_limits(self, monkeypatch):
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 500_000)
        monkeypatch.setattr("opendockit.image._MAX_DECODE_PIXELS", 1_000_000)

    def test_large_jpeg_is_read_at_a_reduced_scale(self):
        data = _jpeg(3200, 2400)  # 7.68 million pixels: over twice the Pillow limit (1 million)

        result = convert(data, "panorama.jpg", options=ConvertOptions(image_max_side=512))

        assert result.format is DocumentFormat.IMAGE
        assert [(image.width, image.height) for image in result.images] == [(512, 384)]

    def test_jpeg_too_large_even_at_one_eighth_is_rejected_as_too_large(self):
        data = _jpeg(9000, 1000)  # at 1/8 it is 1125×125 → 140 thousand pixels, but the limit is lowered so that it is over

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr("opendockit.image._MAX_DECODE_PIXELS", 100_000)
            with pytest.raises(DocumentTooLargeError) as error:
                convert(data, "strip.jpg")

        assert "The image is too large" in str(error.value)

    def test_large_png_is_rejected_as_too_large_not_corrupted(self):
        buffer = BytesIO()
        Image.new("L", (1500, 1000), 200).save(buffer, format="PNG")

        with pytest.raises(DocumentTooLargeError) as error:
            convert(buffer.getvalue(), "scan.png")

        assert "The image is too large" in str(error.value)

    def test_too_large_picture_inside_a_document_is_reported_as_unreadable(self):
        buffer = BytesIO()
        Image.new("L", (1500, 1000), 200).save(buffer, format="PNG")
        html = f'<p>본문</p><img alt="지도" src="data:image/png;base64,{base64.b64encode(buffer.getvalue()).decode()}">'

        result = convert(html.encode(), "a.html")

        assert result.text == "본문\n\n[Image: 지도]"
        assert any("1 image(s)" in warning for warning in result.warnings)


class TestAnimatedAvif:
    def test_animated_avif_is_an_image_and_its_first_frame_is_read(self):
        """An animated AVIF (brand `avis`) was not detected as an image and was rejected as an "unsupported binary file". Like a GIF,
        only the first frame is read."""
        frames = [Image.new("RGB", (200, 200), color) for color in [(255, 0, 0), (0, 0, 255)]]
        buffer = BytesIO()
        frames[0].save(buffer, format="AVIF", save_all=True, append_images=frames[1:], duration=100)

        result = convert(buffer.getvalue(), "anim.avif")

        assert buffer.getvalue()[8:12] == b"avis"
        assert result.format is DocumentFormat.IMAGE
        assert [(image.width, image.height) for image in result.images] == [(200, 200)]


def _tall_picture(width: int = 860, height: int = 18000) -> bytes:
    """A very tall picture, like a product detail page (horizontal stripes, so every tile has content)."""
    image = Image.new("RGB", (width, height), "white")
    for top in range(0, height, 40):
        image.paste((0, 0, 0), (20, top, width - 20, top + 12))
    buffer = BytesIO()
    image.save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


class TestTallPictures:
    """A picture with an aspect ratio over 20:1 was taken for a divider and dropped from the document, so shopping-mall detail
    pages (860×18000) and long screen captures vanished without a warning. An uploaded picture was downscaled to a long side of
    2048, becoming 98×2048 with unreadable text. A picture whose short side is large is kept as content, and a very long one is
    also cut into tiles of a readable width (`DocumentImage.tiles`) that are sent to the describer together."""

    def test_tall_picture_in_a_document_is_kept(self):
        data = ('<p>상세</p><img src="data:image/jpeg;base64,' + base64.b64encode(_tall_picture()).decode() + '"><p>끝</p>').encode()

        result = convert(data, "a.html")

        assert result.text == "상세\n\n[Image]\n\n끝"
        assert len(result.images) == 1

    def test_thin_divider_in_a_document_is_still_dropped(self):
        data = ('<p>상세</p><img src="data:image/png;base64,' + base64.b64encode(builder.png_bytes(3000, 120)).decode()
                + '"><p>끝</p>').encode()

        result = convert(data, "a.html")

        assert result.text == "상세\n\n끝"
        assert result.images == []

    @pytest.mark.parametrize("size", [(860, 18000), (18000, 860)])
    def test_very_long_picture_is_also_cut_into_readable_tiles(self, size):
        result = convert(_tall_picture(*size), "detail.jpg")

        image = result.images[0]
        tiles = [Image.open(BytesIO(tile)) for tile in image.tiles]
        assert 2 <= len(tiles) <= 8
        assert all(min(tile.size) >= 600 and max(tile.size) <= 2048 for tile in tiles)  # downscaled whole it would be 98
        assert sum(max(tile.size) for tile in tiles) >= 18000 * 0.9 * min(tiles[0].size) / 860

    def test_ordinary_picture_has_no_tiles(self):
        result = convert(builder.png_bytes(4000, 3000), "photo.png")

        assert result.images[0].tiles == ()

    def test_openai_describer_sends_every_tile_in_order(self):
        class FakeChatModel:
            messages = None

            async def ainvoke(self, messages):
                from langchain_core.messages import AIMessage

                self.messages = messages
                return AIMessage(content="상세페이지")

        import asyncio

        from opendockit import OpenAIImageDescriber

        image = convert(_tall_picture(), "detail.jpg").images[0]
        model = FakeChatModel()

        asyncio.run(OpenAIImageDescriber(model).describe(image))

        parts = model.messages[0].content
        urls = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
        assert len(urls) == len(image.tiles)
        assert urls[0].endswith(base64.b64encode(image.tiles[0]).decode())
        assert "tile(s)" in parts[0]["text"]


class TestDecorativeAnswersNearTheLimit:
    """A picture judged decorative (SKIP, no alt text) is erased and adds nothing, so it must not use up the headroom of the
    limit check during description. Counted as 3 characters, it made `describe_images` assemble the whole document again every
    few pictures when the text was just under the limit — quadratic in the number of pictures, on the event loop."""

    def test_skip_answers_do_not_make_description_assemble_the_document_again(self, monkeypatch):
        import asyncio

        from opendockit import describe
        from opendockit.describe import describe_images

        parts = [f"<p>paragraph {index}</p><img src='data:image/png;base64,"
                 + base64.b64encode(builder.png_bytes(width=200 + index, seed=index % 20)).decode() + "'>" for index in range(200)]
        html = ("<html><body>" + "".join(parts) + "</body></html>").encode()
        body = len(convert(html, "a.html", ConvertOptions(max_chars=None, extract_images=False)).text)
        result = convert(html, "a.html", ConvertOptions(max_chars=body + 10))
        assert len(result.images) == 200

        class Skip:
            async def describe(self, image):
                return "SKIP"

        renders = []
        original = describe.render
        monkeypatch.setattr(describe, "render", lambda result: renders.append(1) or original(result))

        described = asyncio.run(describe_images(result, Skip()))

        assert not described.exceeded and described.char_count == body
        assert all(image.decorative for image in described.images)
        assert len(renders) <= 3, "the floor once at the start and the final assembly; not once every few pictures"
