"""Audited defects on the text side of the PDF extractor: scans that grew with the square of a page's annotations, spaces,
shapes or faint characters, the character limit that did not see sideways text and form values, FreeText labels and clipped
pictures on pages whose MediaBox does not start at 0,0, characters at positions that are not finite numbers, and re-reading
past the time budget."""

from io import BytesIO
import time
import zlib

import pdfplumber
import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_artifacts, _pdf_chars, _pdf_forms, _pdf_marks
from opendockit.extractors import pdf as pdf_module
from opendockit.extractors._pdf_artifacts import _view_box
from opendockit.extractors._pdf_figures import _figure_boxes
from opendockit.extractors._pdf_model import _Line
from tests.test_audit_pdf_head import _flate, _one_page, _stream
from tests.test_pdf import _raw_pdf
from tests.timing import allowed


_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _pages(count: int, content: bytes, page_extra: bytes = b"", objects: dict[int, bytes] | None = None) -> bytes:
    """A PDF of `count` pages that all draw the content stream object `content` (objects 10 onwards are the pages)."""
    kids = b" ".join(b"%d 0 R" % (10 + index) for index in range(count))
    page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> %s >>"
            % page_extra)
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, count),
                     4: content, 5: _FONT, **(objects or {}), **{10 + index: page for index in range(count)}})


def _object_stream_pdf(content: bytes, annotations: bytes) -> bytes:
    """One page whose `/Annots` array (object 6) sits compressed in an object stream, with a cross-reference stream."""
    header = b"6 0 "
    packed = zlib.compress(header + annotations, 9)
    plain = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
             3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> "
                b"/Annots 6 0 R >>",
             4: content, 5: _FONT,
             7: b"<< /Type /ObjStm /N 1 /First %d /Length %d /Filter /FlateDecode >>\nstream\n" % (len(header), len(packed))
                + packed + b"\nendstream"}
    data, offsets = b"%PDF-1.7\n", {}
    for number in sorted(plain):
        offsets[number] = len(data)
        data += b"%d 0 obj\n%s\nendobj\n" % (number, plain[number])
    xref = len(data)
    rows = [b"\x00\x00\x00\x00\x00\xff\xff"]
    for number in range(1, 9):
        if number == 6:
            rows.append(b"\x02" + (7).to_bytes(4, "big") + b"\x00\x00")
        else:
            rows.append(b"\x01" + (xref if number == 8 else offsets[number]).to_bytes(4, "big") + b"\x00\x00")
    table = b"".join(rows)
    return data + (b"8 0 obj\n<< /Type /XRef /Size 9 /W [1 4 2] /Root 1 0 R /Length %d >>\nstream\n" % len(table) + table
                   + b"\nendstream\nendobj\nstartxref\n%d\n%%%%EOF\n" % xref)


class TestManyFreeTextAnnotations:
    """Every FreeText annotation scanned every body line of its page for a label, so a 38KB PDF with 200,000 identical
    annotations over 1,800 lines took 52 seconds inside one page, where the time budget never looks. The lines are indexed by
    height, and a page looks at a bounded number of candidate lines in all."""

    @staticmethod
    def _page(annotations: int, lines: int) -> bytes:
        body = b"BT /F1 6 Tf " + b"".join(b"1 0 0 1 %d %d Tm (L%d:) Tj " % (20 + (index % 8) * 70, 830 - (index // 8) * 7, index)
                                         for index in range(lines)) + b"ET"
        array = b"[" + b"<</Subtype/FreeText/Rect[500 0 590 842]/Contents(v)>>" * annotations + b"]"
        return _object_stream_pdf(_flate(body), array)

    def test_labels_are_found_through_the_index(self):
        lines = [_Line(top=100 + row * 20, bottom=112 + row * 20, x0=72, x1=110, text=f"Label {row}:", size=10) for row in range(50)]
        lines.append(_Line(top=0, bottom=800, x0=0, x1=20, text="tall margin", size=10))
        beside = _pdf_forms._LinesBeside(lines)

        assert beside.label(130, 300, 312).text == "Label 10:"
        assert beside.label(60, 300, 312).text == "tall margin"
        assert beside.label(10, 300, 312) is None
        assert beside.label(130, 2000, 2012) is None

    def test_labels_stop_once_the_search_work_is_spent(self, monkeypatch):
        monkeypatch.setattr(_pdf_forms, "_LABEL_SEARCH_WORK", 3)
        lines = [_Line(top=100, bottom=112, x0=72, x1=110, text="Name:", size=10)]
        beside = _pdf_forms._LinesBeside(lines)

        assert [beside.label(130, 100, 112) is lines[0] for _ in range(4)] == [True, True, True, False]

    def test_a_page_of_many_annotations_looks_at_a_bounded_number_of_lines(self, monkeypatch):
        # Counted rather than timed: the work spent on labels, not the machine's speed, is what grew with annotations × lines
        monkeypatch.setattr(_pdf_forms, "_LABEL_SEARCH_WORK", 200_000)
        searches = []

        class Counted(_pdf_forms._LinesBeside):
            def __init__(self, lines: list[_Line]) -> None:
                super().__init__(lines)
                searches.append(self)

        monkeypatch.setattr(_pdf_forms, "_LinesBeside", Counted)

        result = convert(self._page(5_000, 1_800), "a.pdf", ConvertOptions(max_chars=None))

        assert len(searches) == 1 and 0 <= searches[0]._work < 1_800  # 200,000 - work left; the full scan looked at 9 million
        values = [line for line in result.sections[0].text.splitlines() if line.endswith("v")]
        assert len(values) == 5_000  # every value comes out; the first ones with their label
        assert values[0].endswith(": v") and values[-1] == "v"


class TestFreeTextLabelsOnShiftedAndRotatedPages:
    """The FreeText box was measured from the paper's top edge in the inverted MediaBox pdfplumber keeps, so on a page whose
    MediaBox does not start at 0,0 the box missed its label and the value came out alone."""

    @staticmethod
    def _page(mediabox: bytes, label_x: int, label_y: int, rect: bytes, rotate: int = 0, matrix: bytes = b"1 0 0 1") -> str:
        body = b"BT /F1 12 Tf %s %d %d Tm (Name:) Tj ET BT /F1 12 Tf %s %d %d Tm (Body text here) Tj ET" % (
            matrix, label_x, label_y, matrix, label_x, label_y - 400 if rotate != 180 else label_y + 400)
        annots = b"/Rotate %d /Annots [<< /Type /Annot /Subtype /FreeText /Rect [%s] /Contents (Hong) >>]" % (rotate, rect)
        return convert(_one_page(_stream(body), mediabox=mediabox, page_extra=annots), "a.pdf").text

    @pytest.mark.parametrize("mediabox", [b"[0 0 595 842]", b"[100 0 695 842]", b"[0 100 595 942]", b"[0 -100 595 842]"])
    def test_label_is_found_when_the_media_box_does_not_start_at_the_origin(self, mediabox):
        assert self._page(mediabox, 172, 800, b"215 798 300 814").endswith("Name: Hong")

    def test_label_is_found_on_a_rotated_page(self):
        # Text drawn upside down on a page rotated by 180 degrees reads upright; the box is to the right of the label as shown
        text = self._page(b"[0 0 595 842]", 523, 42, b"295 28 475 44", rotate=180, matrix=b"-1 0 0 -1")

        assert text.endswith("Name: Hong")


class TestLongLetterSpacedLine:
    """Splitting a line at its gaps scanned every space character of the line for every gap and every piece, so one 659-byte
    line of 16,000 letter-spaced words took 36 seconds."""

    @pytest.mark.timing
    def test_a_long_line_of_spaced_letters_is_split_in_about_linear_time(self):
        count = 16_000
        body = b"BT /F1 2 Tf 0.6 Tc 10 400 Td (" + b"a " * count + b") Tj ET"
        data = _one_page(_flate(body), mediabox=b"[0 0 %d 842]" % (count * 3 + 100))

        started = time.perf_counter()
        result = convert(data, "a.pdf")

        assert time.perf_counter() - started < allowed(6)  # 36 seconds before
        assert result.sections[0].text.count("a") == count

    def test_parts_are_the_same_as_scanning_every_space(self):
        def char(text: str, x0: float, width: float) -> dict:
            return {"text": text, "x0": x0, "x1": x0 + width, "width": width, "height": 10, "size": 10}

        chars = [char("1", 0, 5), char(" ", 5, 2), char("2", 7, 5), char("3", 20, 5), char(" ", 25, 2), char("a", 40, 5),
                 char("b", 45, 5), char(" ", 33, 1)]

        assert _pdf_chars._gap_parts(chars, "1 2 3 ab", 10, 0.6) == ((0, 12, "1 2"), (20, 25, "3"), (40, 50, "ab"))


class TestLimitCountsSidewaysTextAndFormValues:
    """The running character count left out text read sideways and the values of form fields, though both are written at the
    end of the page, so a document whose text was all rotated by 90 degrees was analysed page after page far beyond the limit
    (200 pages under a 5,000-character limit ran into the 60-second time budget)."""

    def test_sideways_text_counts_towards_the_limit_while_reading(self):
        lines = b"".join(b"0 1 -1 0 %d 60 Tm (Sideways line number %d with some words in it for the count) Tj " % (40 + index * 14, index)
                         for index in range(20))
        data = _pages(30, _flate(b"BT /F1 10 Tf " + lines + b"ET"))

        result = convert(data, "a.pdf", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert len(result.sections) <= 3  # the pages after the one that went over are not read (all 30 were)

    def test_form_values_count_towards_the_limit_while_reading(self):
        widgets = b" ".join(b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (field%d) /V (%s) /Rect [72 %d 300 %d] >>"
                            % (index, b"value " * 20, 100 + index * 20, 112 + index * 20) for index in range(30))
        data = _pages(30, _stream(b"BT /F1 10 Tf 72 800 Td (Form) Tj ET"), page_extra=b"/Annots [%s]" % widgets)

        result = convert(data, "a.pdf", ConvertOptions(max_chars=5_000))

        assert result.exceeded
        assert len(result.sections) <= 3  # the pages after the one that went over are not read (all 30 were)


class TestManyCurveBoxes:
    """Grouping check boxes drawn as curves compared every shape with every group, and every group scanned every other group
    and every possible check mark, so a page of 5,000 small circles (166KB) took 35 seconds."""

    @staticmethod
    def _circle(x: float, y: float, radius: float = 4.0) -> bytes:
        k = 0.55 * radius
        return (b"%g %g m %g %g %g %g %g %g c %g %g %g %g %g %g c %g %g %g %g %g %g c %g %g %g %g %g %g c h S\n"
                % (x + radius, y, x + radius, y + k, x + k, y + radius, x, y + radius, x - k, y + radius, x - radius, y + k,
                   x - radius, y, x - radius, y - k, x - k, y - radius, x, y - radius, x + k, y - radius, x + radius, y - k,
                   x + radius, y))

    @pytest.mark.timing
    def test_many_small_circles_are_grouped_in_about_linear_time(self):
        side = 71
        body = b"".join(self._circle(20 + (index % side) * 10, 20 + (index // side) * 10) for index in range(5_000))
        data = _one_page(_flate(body + b"BT /F1 10 Tf 30 830 Td (Hello world) Tj ET\n"), mediabox=b"[0 0 750 842]")

        started = time.perf_counter()
        result = convert(data, "a.pdf")

        assert time.perf_counter() - started < allowed(8)  # 35 seconds before
        assert "Hello world" in result.text

    def test_curve_boxes_beside_labels_are_still_marked(self):
        def curve(x0: float, top: float, filled: bool = False) -> dict:
            return {"object_type": "curve", "x0": x0, "x1": x0 + 8, "top": top, "bottom": top + 8, "width": 8, "height": 8,
                    "pts": [(x0, top), (x0 + 8, top), (x0 + 8, top + 8), (x0, top)], "stroke": True, "fill": filled}

        class Page:
            curves = [curve(60, 100), curve(160, 100), curve(162, 102, True) | {"x1": 166, "bottom": 106, "width": 4, "height": 4}]
            rects, lines, chars = [], [], []

        lines = [_Line(top=98, bottom=110, x0=72, x1=120, text="Agree", size=10),
                 _Line(top=98, bottom=110, x0=172, x1=230, text="Disagree", size=10)]
        _pdf_marks._mark_checkboxes(Page, lines, [])

        assert [line.text for line in lines] == ["☐ Agree", "☑ Disagree"]


class TestManyFaintCharacters:
    """Every group of faint large characters (grouped by line, size and exact colour) scanned every character of the page for
    an overlap, so a 41KB page of 5,000 faint characters in slightly different greys took 13 seconds, and 6,000 separate groups
    over 18,000 characters 20 seconds."""

    @staticmethod
    def _char(text: str, x0: float, top: float, size: float, colour: tuple) -> dict:
        return {"text": text, "x0": x0, "x1": x0 + size * 0.6, "top": top, "bottom": top + size, "size": size,
                "non_stroking_color": colour, "matrix": (1, 0, 0, 1, 0, 0)}

    @pytest.mark.timing
    @pytest.mark.parametrize(("size", "step", "bound"), [
        (6, (0, 7), 1.5),  # 20 seconds when every group scanned every character
        (70, (50, 80), 1.5),  # 15 seconds at 6,000 (22 seconds at 18,000) when characters over 64pt were kept aside
        (300, (200, 320), 1.5),
    ])
    def test_many_separate_faint_groups_are_checked_in_about_linear_time(self, size, step, bound):
        count = 6_000
        chars = [*(self._char("x", index % 500, index // 500 * 2, 1, (0,)) for index in range(3 * count)),
                 *(self._char("W", 10 + index % 50 * step[0], 1_000 + (index // 50 if step[0] else index) * step[1], size,
                              (0.7 + index * 0.25 / count,)) for index in range(count))]

        started = time.perf_counter()
        faint = _pdf_artifacts._faint_watermark(chars, 3)

        assert time.perf_counter() - started < allowed(bound)
        assert faint == set()

    @pytest.mark.timing
    def test_large_faint_characters_over_body_text_are_found_in_about_linear_time(self):
        count = 6_000
        chars = [*(self._char("x", index % 500, index // 500 * 2, 1, (0,)) for index in range(3 * count)),
                 *(self._char("W", index % 50 * 10, index // 50 % 20 * 2, 70, (0.7 + index * 0.25 / count,))
                   for index in range(count))]

        started = time.perf_counter()
        faint = _pdf_artifacts._faint_watermark(chars, 3)

        assert time.perf_counter() - started < allowed(1)  # a variant: every group overlaps body text at once
        assert len(faint) == count

    def test_faint_large_text_over_body_text_is_still_found(self):
        def char(text: str, x0: float, top: float, size: float, colour: tuple) -> dict:
            return {"text": text, "x0": x0, "x1": x0 + size * 0.6, "top": top, "bottom": top + size, "size": size,
                    "non_stroking_color": colour, "matrix": (1, 0, 0, 1, 0, 0)}

        draft = [char(letter, 100 + index * 40, 300, 60, (0.85,)) for index, letter in enumerate("DRAFT")]
        body = [char("a", 150, 330, 10, (0,)), char("b", 400, 700, 10, (0,))]
        cover = [char("7", 100, 50, 60, (0.9,))]  # faint and large but over nothing

        assert _pdf_artifacts._faint_watermark([*draft, *body, *cover], 30) == {id(item) for item in draft}


class TestCharactersAtNonFinitePositions:
    """A text matrix overflowing to infinity puts characters at NaN positions. Rounding such a piece's position raised
    ValueError and the whole document was reported corrupted; with that fixed, every copy still came out as a body line 'x',
    and copies at a NaN position were never capped (NaN is unequal even to itself), so 20,000 of them took 39 seconds."""

    @staticmethod
    def _page(copies: int) -> bytes:
        huge = b"1" + b"0" * 400 + b".0"
        return _one_page(_flate(b"BT /F1 10 Tf " + huge + b" 0 0 1 0 0 Tm " + b"(x) Tj " * copies
                                + b"ET BT /F1 10 Tf 72 700 Td (Hello) Tj ET"))

    def test_the_page_is_read_without_the_characters_drawn_nowhere(self):
        result = convert(self._page(3), "a.pdf")

        assert result.sections[0].text == "Hello"

    @pytest.mark.timing
    def test_copies_at_a_nan_position_are_capped(self):
        started = time.perf_counter()
        result = convert(self._page(20_000), "a.pdf")

        assert time.perf_counter() - started < allowed(4)  # 39 seconds before
        assert result.sections[0].text == "Hello"


class TestClipWithOffsetMediaBox:
    """The clip regions are recorded in pdfminer's page coordinates while pdfplumber moves x by the MediaBox's left edge, so
    on a page whose MediaBox does not start at x = 0 no clip matched its picture and the clipping was silently lost."""

    @pytest.mark.parametrize(("mediabox", "dx", "dy", "expected"), [
        (b"[0 0 595 842]", 0, 0, (150, 342, 350, 542)),
        (b"[100 0 695 842]", 100, 0, (250, 342, 450, 542)),
        (b"[0 100 595 942]", 0, 100, (150, 242, 350, 442)),
        (b"[-50 0 545 842]", -50, 0, (100, 342, 300, 542)),
    ])
    def test_picture_is_cut_to_its_clip(self, mediabox, dx, dy, expected):
        image = b"BI /W 2 /H 2 /CS /G /BPC 8 ID \x00\xff\xff\x00 EI"
        body = (b"1 0 0 1 %d %d cm q 150 300 200 200 re W n q 500 0 0 500 50 200 cm " % (dx, dy) + image
                + b" Q Q BT /F1 12 Tf 72 100 Td (caption) Tj ET")
        page = pdfplumber.open(BytesIO(_one_page(_stream(body), mediabox=mediabox))).pages[0]

        assert [tuple(round(value) for value in box[:4]) for box in _figure_boxes(page, _view_box(page))] == [expected]


class TestPendingPagesAfterTheTimeBudget:
    """A page that could not determine the width of separately placed text is read again once a later page has. That second
    reading ignored the time budget, so after the budget had sent the remaining pages to plain extraction every pending page was
    still analysed once more."""

    def test_pending_pages_are_not_read_again_once_the_budget_is_spent(self, monkeypatch):
        clock = [0.0]
        calls = []
        read_page = pdf_module._read_page

        def read(page, placement_fit=None):
            calls.append(page.page_number)
            result = read_page(page, placement_fit)
            if page.page_number == 1:
                result.placement_pending = True
            else:
                result.placement_fit = (0.9, 0.25)
                clock[0] = 1_000.0  # the budget is spent while page 2 is read
            return result

        monkeypatch.setattr(pdf_module, "_read_page", read)
        monkeypatch.setattr(pdf_module.time, "perf_counter", lambda: clock[0])

        result = convert(_pages(2, _stream(b"BT /F1 12 Tf 72 700 Td (Hello world) Tj ET")), "a.pdf",
                         ConvertOptions(pdf_time_budget=10))

        assert calls == [1, 2]
        assert result.text.count("Hello world") == 2

    def test_pending_pages_are_read_again_within_the_budget(self, monkeypatch):
        calls = []
        read_page = pdf_module._read_page

        def read(page, placement_fit=None):
            calls.append(page.page_number)
            result = read_page(page, placement_fit)
            if page.page_number == 1 and placement_fit is None:
                result.placement_pending = True
            elif page.page_number == 2:
                result.placement_fit = (0.9, 0.25)
            return result

        monkeypatch.setattr(pdf_module, "_read_page", read)

        convert(_pages(2, _stream(b"BT /F1 12 Tf 72 700 Td (Hello world) Tj ET")), "a.pdf")

        assert calls == [1, 2, 1]
