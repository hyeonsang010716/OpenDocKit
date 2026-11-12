"""An mbox with many mails without closing boundaries, variants of data: URI pictures in MD, the warning for a very large picture
inside a document, convert called with its arguments in the wrong places."""

import asyncio
import base64
from io import BytesIO
import time

from PIL import Image
import pytest

from opendockit import ConvertOptions, aconvert, convert, convert_file


def _unclosed_mail(number: int) -> str:
    return (f"From a@b.c Mon Oct  2 10:00:00 2026\nFrom: a@b.c\nTo: x@y.z\nSubject: m{number}\n"
            f"Date: Mon, 2 Oct 2026 10:00:00 +0900\nMIME-Version: 1.0\n"
            f'Content-Type: multipart/mixed; boundary="X{number}"\n\n'
            f"--X{number}\nContent-Type: text/plain; charset=utf-8\n\n본문 {number}\n\n")


class TestMailboxOfMailsWithoutClosingBoundaries:
    """The whole archive was read as one mail (`mime_mail`) before the mbox was split, so the boundaries of mails without closing
    boundaries piled up between the mails. From the 33rd mail on, the whole archive was rejected as 'MIME parts nested deeper than
    32 levels'. Nesting is counted only within a single mail."""

    def test_forty_mails_without_closing_boundaries_are_all_read(self):
        data = "".join(_unclosed_mail(number) for number in range(40)).encode()

        result = convert(data, "box.mbox")

        assert "본문 0" in result.text
        assert "본문 39" in result.text
        assert result.text.count("## Message ") == 40


def _png(width: int = 200, height: int = 200) -> str:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class TestMarkdownDataUriVariants:
    """Only `![alt](data:… "title")` and a quoted `<img src="data:…">` were seen as pictures, so the base64 of single-quoted and
    parenthesized titles, `<…>` destinations, MIME parameters (`;charset=utf-8`) and an unquoted `src=data:…` stayed in the body as
    is (0 pictures, tens of thousands of characters per picture)."""

    @pytest.mark.parametrize("markdown, alt", [
        ("![구성도](data:image/png;base64,{p} '빨간 사각형')", "구성도"),
        ("![구성도](data:image/png;base64,{p} (빨간 사각형))", "구성도"),
        ("![구성도](<data:image/png;base64,{p}>)", "구성도"),
        ("![구성도](<data:image/png;base64,{p}> \"빨간 사각형\")", "구성도"),
        ("![구성도](data:image/png;name=a.png;base64,{p})", "구성도"),
        ("<img src=data:image/png;base64,{p} alt=빨강>", "빨강"),
        ("<img alt='빨강' src=data:image/png;base64,{p}>", "빨강"),
    ])
    def test_picture_is_extracted(self, markdown, alt):
        data = f"# 제목\n\n{markdown.format(p=_png())}\n\n끝".encode()

        result = convert(data, "a.md")

        assert len(result.images) == 1
        assert "base64" not in result.text
        assert result.text == f"# 제목\n\n[Image: {alt}]\n\n끝"

    @pytest.mark.parametrize("opening", ["![a](data:image/png;base64,", "![a](<data:image/png;base64,",
                                         "![a](data:image/png;charset=utf-8;base64,", "<img src=data:image/png;base64,"])
    @pytest.mark.timing
    def test_unclosed_data_uri_followed_by_long_whitespace_takes_linear_time(self, opening):
        def seconds(count: int) -> float:
            data = f"{opening}iVBORw0KGgo{' ' * count}x {opening}".encode()
            started = time.perf_counter()
            convert(data, "a.md")
            return time.perf_counter() - started

        small, large = seconds(20_000), seconds(160_000)

        assert large < 8 * small * 2 + 0.5


class TestTooLargePictureInsideADocument:
    """A picture over the decode limit (about 179 million pixels) was reported as 'WMF/EMF vectors, etc. or corrupted', so there was
    no way to tell that it was left out because of its size, not corruption."""

    def test_warning_says_the_picture_is_too_large(self, monkeypatch):
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 500_000)
        monkeypatch.setattr("opendockit.image._MAX_DECODE_PIXELS", 1_000_000)
        html = f'<p>본문</p><img alt="지도" src="data:image/png;base64,{_png(1500, 1000)}">'

        result = convert(html.encode(), "a.html")

        assert result.text == "본문\n\n[Image: 지도]"
        assert [warning for warning in result.warnings if "image" in warning] == [
            "1 image(s) are too large (decode limit about 179 million pixels) and were left as placeholder text."]


class TestArgumentsInTheWrongPlace:
    """Passing the options in the filename position, as in `convert(data, ConvertOptions(...))`, raised TypeError only after the
    conversion had run to the end (minutes for a large PDF). The types are checked before converting and reported right away."""

    def test_options_given_as_the_filename_is_rejected_before_converting(self, monkeypatch):
        monkeypatch.setattr("opendockit.converter.detect_format", lambda *_: pytest.fail("the conversion started"))

        with pytest.raises(TypeError) as error:
            convert(b"<p>a</p>", ConvertOptions())

        assert "options=" in str(error.value)

    def test_options_of_the_wrong_type_are_rejected(self):
        with pytest.raises(TypeError) as error:
            convert(b"<p>a</p>", "a.html", {"max_chars": 10})

        assert "ConvertOptions" in str(error.value)

    def test_convert_file_rejects_options_of_the_wrong_type(self, tmp_path):
        path = tmp_path / "a.html"
        path.write_bytes(b"<p>a</p>")

        with pytest.raises(TypeError):
            convert_file(path, {"max_chars": 10})

    def test_aconvert_rejects_options_given_as_the_filename(self):
        with pytest.raises(TypeError) as error:
            asyncio.run(aconvert(b"<p>a</p>", ConvertOptions()))

        assert "options=" in str(error.value)
