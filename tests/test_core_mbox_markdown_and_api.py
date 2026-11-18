"""An mbox with many mails without closing boundaries, variants of data: URI pictures in MD, the warning for a very large picture
inside a document, convert called with its arguments in the wrong places."""

import asyncio
import base64
from io import BytesIO

from PIL import Image
import pytest

from opendockit import ConvertOptions, aconvert, convert, convert_file
from tests.timing import fastest


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
            return fastest(lambda: convert(data, "a.md"))

        small, large = seconds(20_000), seconds(160_000)

        assert large < 8 * small * 2 + 0.5


class TestMarkdownPicturesWithBracketsLineBreaksAndAnglesInTheirText:
    """CommonMark allows balanced brackets and single line breaks in alt text, and HTML a `>` inside a quoted attribute. The
    alt text field stopped at any `]` or line break and the `<img` fields at any `>`, so these pictures kept their base64 in
    the body, and `<img src="data:…" alt="Revenue > target">` lost its alt and left `target">` behind."""

    @pytest.mark.parametrize("markdown, alt", [
        ("![Figure [1]: sales](data:image/png;base64,{p})", "Figure [1]: sales"),
        ("![a long alt text that the editor\nwrapped onto two lines](data:image/png;base64,{p})",
         "a long alt text that the editor wrapped onto two lines"),
        ("![two\r\nlines](data:image/png;base64,{p})", "two lines"),
        ('<img alt="Revenue > target" src="data:image/png;base64,{p}">', "Revenue > target"),
        ('<img src="data:image/png;base64,{p}" alt="Revenue > target">', "Revenue > target"),
        ("<img src='data:image/png;base64,{p}' title='a > b' alt=빨강>", "빨강"),
    ])
    def test_picture_is_extracted(self, markdown, alt):
        data = f"# 제목\n\n{markdown.format(p=_png())}\n\n끝".encode()

        result = convert(data, "a.md")

        assert len(result.images) == 1
        assert "base64" not in result.text
        assert result.text == f"# 제목\n\n[Image: {alt}]\n\n끝"

    @pytest.mark.parametrize("markdown", [
        "<img alt='it's' src=\"data:image/png;base64,{p}\" title='x'>",
        '<img alt="5" screen" src="data:image/png;base64,{p}" width="100">',
    ])
    def test_img_with_a_stray_quote_before_src_is_extracted(self, markdown):
        """A quote opens an attribute value only right after `=`: a stray one took the `src` after it for part of a value,
        and the picture kept its base64 in the text."""
        result = convert(f"# 제목\n\n{markdown.format(p=_png())}\n\n끝".encode(), "a.md")

        assert len(result.images) == 1
        assert "base64" not in result.text and "끝" in result.text

    @pytest.mark.parametrize("markdown", [
        "![a\n\nb](data:image/png;base64,{p})",          # a blank line ends the paragraph, and the alt text with it
        "![a [b](data:image/png;base64,{p})",               # an unbalanced bracket is text, as before
    ])
    def test_what_is_not_one_picture_stays_as_before(self, markdown):
        data = markdown.format(p=_png()).encode()

        result = convert(data, "a.md")

        if "\n\n" in markdown:
            assert not result.images and "![a" in result.text
        else:
            assert len(result.images) == 1 and result.text == r"[Image: a \[b]"

    @pytest.mark.timing
    @pytest.mark.parametrize("piece", ["![[", "![a [b] ", "![a\n", '<img alt="a > b" ', '<img alt="', "<img alt='a > b' src=\"data:image/png;base64,A\" "])
    def test_many_unclosed_openers_take_linear_time(self, piece):
        def seconds(count: int) -> float:
            data = (piece * count + " data:image/png;base64,AAAA\n").encode()
            return fastest(lambda: convert(data, "a.md"))

        small, large = seconds(5_000), seconds(40_000)

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
