"""Paragraph boundaries in MD with Windows line breaks, the time to re-encode transparent pictures as PNG."""

import base64
import io

from PIL import Image
import pytest

from opendockit import convert


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), (5, 5, 5)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class TestInlineCodeAcrossParagraphsWithWindowsLineBreaks:
    """The round-45 paragraph boundary looked only at `\\n` blank lines, so in MD saved with CRLF or CR an unmatched backtick in
    an earlier paragraph paired with a backtick in a later paragraph. The data: URI picture between them stayed as inline code:
    0 pictures, and the base64 came out in the body as is (59 characters → 931 characters)."""

    @pytest.mark.parametrize("line_break", ["\r\n", "\r"])
    def test_unmatched_backtick_in_an_earlier_paragraph_does_not_hide_the_picture(self, line_break):
        markdown = f"키보드의 ` 키를 누르면 콘솔이 열립니다.\n\n![매출 차트](data:image/png;base64,{_png_base64()})\n\n그다음 `ls` 를 입력합니다."
        data = markdown.replace("\n", line_break).encode()

        result = convert(data, "guide.md")

        assert len(result.images) == 1
        assert "base64," not in result.text
        assert "`ls`" in result.text


class TestTransparentPictureEncoding:
    """Transparent pictures were rewritten with PNG `optimize=True`, which took most of the conversion time of picture-heavy PPTX
    and HWP (a deck with 122 pictures: 31.7 s → 10.4 s). The result is even slightly smaller without it, so nothing was gained.
    They are written with the default compression, without the compression-level search."""

    def test_transparent_picture_is_written_without_the_png_optimizer(self, monkeypatch):
        saved = []
        original_save = Image.Image.save

        def recording_save(image, fp, format=None, **params):
            saved.append((format, params))
            return original_save(image, fp, format, **params)

        monkeypatch.setattr(Image.Image, "save", recording_save)
        picture = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
        picture.paste((200, 30, 30, 255), (50, 50, 250, 250))
        buffer = io.BytesIO()
        original_save(picture, buffer, "PNG")

        result = convert(buffer.getvalue(), "logo.png")

        assert result.images[0].mime_type == "image/png"
        assert [params for format, params in saved if format == "PNG"]
        assert all(not params.get("optimize") for format, params in saved if format == "PNG")
