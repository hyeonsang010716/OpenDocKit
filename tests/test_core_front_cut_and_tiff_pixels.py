"""In the front-part verdict of a large HTML, the checks that decide whether to keep an element 'by looking at the whole element'
when it is cut off at the end of the front part; the total decoded pixels of a multi-page TIFF."""

from io import BytesIO

from PIL import Image
import pytest

from opendockit import ConvertOptions, convert


_BODY = "".join(f"<p>{'가' * 20}</p>" for _ in range(50))


class TestElementCutAtTheEndOfTheFront:
    """The front-part verdict (`html._extract_front`) assumes the text of the front part is within the full text. For an element
    cut off at the end of the front part, the checks that look at the whole element (number of svg labels, link ratio of a folded
    answer) differed from the full conversion, which blocked a document within the limit as 'exceeded' with text the full
    conversion does not have."""

    def test_svg_chart_cut_by_the_front_does_not_keep_labels_the_whole_page_drops(self):
        svg = "<svg>" + "".join(f"<text>{'1,000 ' * 100}</text>" for _ in range(30)) + "</svg>"
        data = f"<html><body>{_BODY}{svg}{_BODY}</body></html>".encode()
        whole = convert(data, "a.html", ConvertOptions(max_chars=None))

        result = convert(data, "a.html", ConvertOptions(max_chars=3000))

        assert "1,000" not in whole.text
        assert whole.char_count <= 3000
        assert not result.exceeded
        assert result.text == whole.text

    @pytest.mark.parametrize("extra", [0, 100, 250])
    def test_folded_menu_cut_by_the_front_is_not_kept_as_a_folded_answer(self, extra):
        menu = ("<button>전체 메뉴</button><div style='display:none'><p>" + "안내" * 600 + "</p>"
                + "".join(f"<a href='/m{i}'>메뉴항목{i}</a>" for i in range(300)) + "</div>")
        data = f"<html><body>{_BODY}{menu}{_BODY}</body></html>".encode()
        whole = convert(data, "a.html", ConvertOptions(max_chars=None))

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count + extra))

        assert "안내" not in whole.text
        assert not result.exceeded
        assert result.text == whole.text

    def test_small_svg_and_folded_answer_closed_inside_the_front_are_still_kept(self):
        """An element that is not cut (closed inside the front part) is judged as before — the front part alone exceeds the limit."""
        front = ("<svg><text>매출 120억</text></svg><a href='#'>질문</a><div style='display:none'>접어 둔 답</div>"
                 + _BODY * 4)
        data = f"<html><body>{front}<!--{'x' * 40000}-->{_BODY}</body></html>".encode()

        result = convert(data, "a.html", ConvertOptions(max_chars=1000))

        assert result.exceeded
        assert "매출 120억" in result.text
        assert "접어 둔 답" in result.text


def _tiff(pages: int, side: int = 200) -> bytes:
    frames = [Image.new("L", (side, side), 40 * index) for index in range(pages)]
    buffer = BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    return buffer.getvalue()


class TestMultiPageTiffPixelTotal:
    """A multi-page TIFF kept only the per-page decode limit, so a 650KB file of 100 black-and-white pages of 10,000×10,000 used
    9 seconds and 400MB (about 90 seconds for 1,000 pages). The total page pixels get a limit, and pages from the one that goes
    over it are not read, with a warning."""

    def test_pages_past_the_pixel_total_are_left_out_with_a_warning(self, monkeypatch):
        from opendockit import image as image_module

        monkeypatch.setattr(image_module, "_MAX_TIFF_PIXELS", 2 * 200 * 200)

        result = convert(_tiff(3), "scan.tif")

        assert len(result.images) == 2
        assert any("the first 2 page(s)" in warning for warning in result.warnings)

    def test_pages_within_the_pixel_total_are_all_read(self):
        result = convert(_tiff(3), "scan.tif")

        assert len(result.images) == 3
        assert result.warnings == []
