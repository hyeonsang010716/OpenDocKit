"""Backticks inside MD picture syntax and backticks paired across paragraphs, data: URI pictures that are not base64, a JPEG with
its end slightly cut off, the limit verdict on broken HTML."""

import base64
import io
import urllib.parse

from PIL import Image
import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, convert


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), (5, 5, 5)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _jpeg() -> bytes:
    picture = Image.new("RGB", (400, 300))
    picture.putdata([((x * 7) % 256, (y * 5) % 256, (x * y) % 256) for y in range(300) for x in range(400)])
    buffer = io.BytesIO()
    picture.save(buffer, "JPEG", quality=90)
    return buffer.getvalue()


class TestBackticksAroundMarkdownPictures:
    """The round-44 inline code check cut the text before the picture syntax was found, so a backtick inside the alt text, title or
    `<img alt>`, or backticks paired across paragraphs, split a picture in two. With 0 pictures the base64 stayed in the body and
    one picture used tens of thousands of characters."""

    @pytest.mark.parametrize("markdown", [
        "설정\n\n![`config.yaml` 편집 화면](data:image/png;base64,{png})\n\n끝",
        '설정\n\n<img alt="`ls` 실행 결과" src="data:image/png;base64,{png}">\n\n끝',
        '설정\n\n![결과](data:image/png;base64,{png} "`제목`")\n\n끝',
        "설정\n\n![` 하나만 있는 대체](data:image/png;base64,{png})\n\n그다음 `ls` 입력",
    ])
    def test_backtick_inside_the_picture_syntax_keeps_the_picture(self, markdown):
        data = markdown.format(png=_png_base64()).encode()

        result = convert(data, "guide.md")

        assert len(result.images) == 1
        assert "base64," not in result.text

    def test_unmatched_backtick_in_an_earlier_paragraph_does_not_hide_the_picture(self):
        markdown = f"키보드의 ` 키를 누르면 콘솔이 열립니다.\n\n![매출 차트](data:image/png;base64,{_png_base64()})\n\n그다음 `ls` 를 입력합니다."

        result = convert(markdown.encode(), "guide.md")

        assert len(result.images) == 1
        assert "base64," not in result.text
        assert "`ls`" in result.text

    def test_picture_inside_inline_code_on_two_lines_of_one_paragraph_stays_text(self):
        markdown = f"예시: `![a](data:image/png;base64,{_png_base64()})\n이렇게` 씁니다."

        result = convert(markdown.encode(), "guide.md")

        assert result.images == []
        assert "base64," in result.text


class TestMarkdownDataUriPicturesThatAreNotBase64:
    """A data: URI picture that is not base64, such as a percent-encoded SVG, stayed as its raw source in MD (140,000 characters).
    HTML leaves the same picture as its alt text."""

    def _svg_uri(self) -> str:
        svg = '<svg xmlns="http://www.w3.org/2000/svg"><text x="1" y="9">매출</text>' + '<rect width="1" height="1"/>' * 300 + "</svg>"
        return f"data:image/svg+xml,{urllib.parse.quote(svg)}"

    def test_markdown_picture_becomes_its_alt_text_like_in_html(self):
        uri = self._svg_uri()

        markdown = convert(f"앞 글\n\n![도표]({uri})\n\n뒤 글".encode(), "a.md")
        page = convert(f'<p>앞 글</p><img src="{uri}" alt="도표"><p>뒤 글</p>'.encode(), "a.html")

        assert markdown.text == page.text == "앞 글\n\n[Image: 도표]\n\n뒤 글"

    def test_html_picture_in_markdown_becomes_its_alt_text(self):
        result = convert(f'앞 글\n\n<img src="{self._svg_uri()}" alt="도표">\n\n뒤 글'.encode(), "a.md")

        assert result.text == "앞 글\n\n[Image: 도표]\n\n뒤 글"

    def test_angle_bracket_destination_and_title(self):
        result = convert(f'![도표](<{self._svg_uri()}> "제목")'.encode(), "a.md")

        assert result.text == "[Image: 도표]"

    def test_base64_svg_is_alt_text_without_a_warning_like_in_html(self):
        svg = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"><text>a</text></svg>').decode()

        result = convert(f"앞\n\n![도표](data:image/svg+xml;base64,{svg})\n\n뒤".encode(), "a.md")

        assert result.text == "앞\n\n[Image: 도표]\n\n뒤"
        assert result.warnings == []

    def test_picture_without_alt_text_leaves_nothing(self):
        result = convert(f"앞\n\n![]({self._svg_uri()})\n\n뒤".encode(), "a.md")

        assert result.text == "앞\n\n뒤"

    def test_non_base64_picture_inside_inline_code_stays_text(self):
        result = convert("예시: `![x](data:image/svg+xml,%3Csvg%3E)` 처럼 씁니다.".encode(), "a.md")

        assert "`![x](data:image/svg+xml,%3Csvg%3E)`" in result.text


class TestSlightlyTruncatedJpeg:
    """A JPEG missing only its end marker (EOI) or with its end slightly cut off was discarded whole (a standalone file was rejected
    as 'corrupted'). Browsers show such a picture too."""

    @pytest.mark.parametrize("keep", [lambda data: data[:-2], lambda data: data[:int(len(data) * 0.98)]])
    def test_truncated_jpeg_file_is_still_a_picture(self, keep):
        data = keep(_jpeg())

        result = convert(data, "photo.jpg")

        assert len(result.images) == 1

    def test_truncated_jpeg_inside_html_is_kept(self):
        encoded = base64.b64encode(_jpeg()[:-2]).decode()

        result = convert(f'<p>보고</p><img src="data:image/jpeg;base64,{encoded}" alt="차트">'.encode(), "a.html")

        assert len(result.images) == 1
        assert result.warnings == []

    @pytest.mark.parametrize("keep", [lambda data: data[:200], lambda data: data[:data.index(b"\xff\xda") + 40]])
    def test_jpeg_with_almost_no_picture_data_is_still_rejected(self, keep):
        data = keep(_jpeg())

        with pytest.raises(CorruptedDocumentError):
            convert(data, "photo.jpg")


class TestLimitOnBrokenHtml:
    """For broken HTML with many unclosed tags, reading the front part of a large document differed from the full parse, so the
    result was 'exceeded' although the limit equaled the full character count."""

    @pytest.mark.parametrize("page", [
        "<table><pre></summary><tr colspan=0># <ul rowspan=-1>​<details rowspan=-1>",
        "<table>1,234<h1 rowspan=-1><hr aria-hidden=true>가나다</li><td colspan=2><select hidden><aside rowspan=3>☑\n\n"
        "<table colspan=2>1,234é",
        "1. 1,234가나다<math open>- </label><col class='x'><span type=checkbox checked>",
        "<body>123</table><math colspan=0>☑​`x`|</select><dt src='data:image/png;base64,iVBORw0KGgo='></dl><mark start=5>",
    ])
    def test_limit_equal_to_the_full_length_is_not_exceeded(self, page):
        full = convert(page.encode(), "a.html", ConvertOptions(max_chars=None))

        limited = convert(page.encode(), "a.html", ConvertOptions(max_chars=full.char_count))

        assert limited.exceeded is False
        assert limited.text == full.text

    def test_text_after_math_left_open_to_the_end_is_kept(self):
        result = convert(b"<p>before</p><math>abc<p>after</p>", "a.html")

        assert "abc" in result.text
        assert "after" in result.text

    def test_text_beside_elements_inside_math_is_kept(self):
        result = convert("<p>값</p><math>- <span>a</span> b</math>".encode(), "a.html")

        assert result.text == "값\n\n$-ab$"
