"""Tables read from the clipping path Hangul's own PDF export sets around each table cell (`_pdf_clip_tables`)."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_clip_tables
from opendockit.extractors._pdf_clip_tables import _cell_groups
from tests.builder import pdf_of_glyphs


_OPTIONS = ConvertOptions(page_markers=False)
_HANCOM = "Hancom PDF 1.3.0.550"
_BODY = (72, 780, 10, "The cells below are drawn one by one.", False)


def _cells(*cells: tuple[float, float, float, float, str | list[str]]) -> tuple[list[tuple], dict[int, tuple]]:
    """The runs and clips of cells given as (x0, top, x1, bottom, text) with `top` and `bottom` counted from the top of
    the page; a text given as a list is one line under the other. An empty text is an empty cell."""
    runs, clips = [_BODY], {}
    for x0, top, x1, bottom, text in cells:
        lines = text if isinstance(text, list) else [text]
        for index, line in enumerate(lines):
            clips[len(runs)] = (x0, 842 - bottom, x1, 842 - top)
            runs.append((x0 + 4, 842 - top - 12 - 12 * index, 10, line, False))
    return runs, clips


def _read(cells: tuple, producer: str | None = _HANCOM, **how) -> str:
    runs, clips = _cells(*cells)
    return convert(pdf_of_glyphs(runs, producer=producer, clips=clips, **how), "a.pdf", _OPTIONS).text


_GRID = ((72, 100, 172, 120, "Name"), (172, 100, 272, 120, "Role"), (272, 100, 400, 120, "Phone"),
         (72, 120, 172, 140, "Kim"), (172, 120, 272, 140, "Lead"), (272, 120, 400, 140, "1234"),
         (72, 140, 172, 160, "Lee"), (172, 140, 272, 160, ""), (272, 140, 400, 160, "5678"))


class TestTablesFromCellClips:
    def test_the_boxes_are_the_cells_of_a_table_no_rule_shows(self):
        """No border is drawn at all; the cells are where Hangul clipped them."""
        text = _read(_GRID)

        assert "| Name | Role | Phone |" in text
        assert "| Kim | Lead | 1234 |" in text
        assert "| Lee | | 5678 |" in text

    def test_only_a_document_hangul_wrote_is_read_so(self):
        """Another program clips for other reasons (Word clips every run of text, a browser every block): the same page
        is read as it is without its clips."""
        runs, clips = _cells(*_GRID)
        for producer in (None, "Microsoft Word for Microsoft 365", "Skia/PDF m158"):
            assert (convert(pdf_of_glyphs(runs, producer=producer, clips=clips), "a.pdf", _OPTIONS).text
                    == convert(pdf_of_glyphs(runs, producer=producer), "a.pdf", _OPTIONS).text)

    def test_a_cell_over_two_columns_is_written_with_its_span(self):
        cells = ((72, 100, 272, 120, "Contact"), (272, 100, 400, 120, "Phone"),
                 (72, 120, 172, 140, "Kim"), (172, 120, 272, 140, "Lead"), (272, 120, 400, 140, "1234"))
        text = _read(cells)

        assert '<th colspan="2">Contact</th>' in text
        assert "<td>Kim</td><td>Lead</td><td>1234</td>" in text
        assert "colspan" not in _read(cells, producer=None)  # nothing drawn on the page says so

    def test_a_cell_over_two_rows_is_written_on_both(self):
        text = _read(((72, 100, 172, 140, "Team"), (172, 100, 272, 120, "Kim"), (272, 100, 400, 120, "1234"),
                      (172, 120, 272, 140, "Lee"), (272, 120, 400, 140, "5678")))

        assert "| Team | Kim | 1234 |" in text
        assert "| Team | Lee | 5678 |" in text

    def test_one_row_of_boxes_is_a_line_of_the_document(self):
        """The label and the date of a press release stand in a table of one row: a line, as Hangul's reader writes it."""
        text = _read(((72, 100, 172, 120, "Release"), (172, 100, 400, 120, "at once")))

        assert "|" not in text
        assert "Release" in text and "at once" in text

    def test_one_row_with_a_cell_of_two_lines_stays_a_table(self):
        text = _read(((72, 100, 172, 140, "Release"), (172, 100, 400, 140, ["online at ten", "print the next day"])))

        assert "| Release | online at ten" in text

    def test_boxes_that_frame_text_are_that_text(self):
        """A box with its title in a tab above it: no two boxes hold text side by side."""
        text = _read(((72, 100, 172, 120, ""), (172, 100, 300, 120, "Summary"), (72, 120, 400, 160, "What the release says.")))

        assert "|" not in text and "<table" not in text
        assert "Summary" in text and "What the release says." in text

    def test_a_turned_page_is_read_without_its_clips(self):
        runs, clips = _cells(*_GRID)

        assert (convert(pdf_of_glyphs(runs, producer=_HANCOM, clips=clips, rotate=90), "a.pdf", _OPTIONS).text
                == convert(pdf_of_glyphs(runs, producer=_HANCOM, rotate=90), "a.pdf", _OPTIONS).text)


def _char(x: float, top: float, text: str = "a") -> dict:
    return {"x0": x, "x1": x + 5, "top": top, "bottom": top + 10, "text": text}


class TestGroupsOfBoxes:
    def test_a_box_set_into_the_row_above_starts_another_table(self):
        """The row of dates of a press release is set a fraction of a point into the title box above it."""
        title = [(60, 80, 190, 117.35), (190, 80, 380, 117.35), (380, 80, 536, 117.35)]
        dates = [(250, 117.0, 300, 145), (300, 117.0, 380, 145), (380, 117.0, 420, 145), (420, 117.0, 536, 145),
                 (250, 145, 300, 170), (300, 145, 380, 170), (380, 145, 420, 170), (420, 145, 536, 170)]
        chars = [_char(box[0] + 2, box[1] + 2) for box in (*title, *dates)]

        groups = _cell_groups(title + dates, chars, [])

        assert [len(group) for group in groups] == [8]  # the title row alone is one row of boxes: a line
        assert set(groups[0]) == set(dates)

    def test_the_clip_of_a_picture_is_no_cell(self):
        grid = [(72, 100, 172, 120), (172, 100, 272, 120), (72, 120, 172, 140), (172, 120, 272, 140)]
        picture = (272, 100, 372, 140)  # beside the table, edge to edge
        chars = [_char(box[0] + 2, box[1] + 2) for box in grid]

        assert _cell_groups([*grid, picture], chars, [(272.4, 100.2, 371.8, 139.9)]) == [sorted(grid, key=lambda box: (box[1], box[0]))]
        assert len(_cell_groups([*grid, picture], chars, [])[0]) == 5

    def test_the_cells_of_a_table_inside_a_cell_are_a_table_of_their_own(self):
        outer = [(72, 100, 172, 200), (172, 100, 400, 200), (72, 200, 172, 240), (172, 200, 400, 240)]
        inner = [(180, 110, 280, 150), (280, 110, 390, 150), (180, 150, 280, 190), (280, 150, 390, 190)]
        chars = [_char(box[0] + 2, box[1] + 2) for box in (outer[0], outer[2], outer[3], *inner)]

        groups = _cell_groups(outer + inner, chars, [])

        assert sorted(len(group) for group in groups) == [4, 4]
        assert set(groups[0]) == set(outer) and set(groups[1]) == set(inner)

    def test_a_page_of_too_many_boxes_is_left_to_its_rules(self, monkeypatch):
        grid = [(72 + 20 * column, 100 + 12 * row, 92 + 20 * column, 112 + 12 * row) for row in range(6) for column in range(6)]
        chars = [_char(box[0] + 2, box[1] + 1) for box in grid]
        assert len(_cell_groups(grid, chars, [])) == 1

        monkeypatch.setattr(_pdf_clip_tables, "_MAX_COMPARISONS", 50)

        assert _cell_groups(grid, chars, []) == []

    @pytest.mark.parametrize("count", [1, _pdf_clip_tables._MAX_BOXES + 1])
    def test_one_box_or_more_than_the_bound_is_no_table(self, count):
        runs = [_BODY] + [(72, 700 - index % 60 * 10, 6, "x", False) for index in range(count)]
        clips = {index + 1: (72 + index % 7 * 0.5, 100 + index * 0.9, 300, 101.5 + index * 0.9) for index in range(count)}

        assert (convert(pdf_of_glyphs(runs, producer=_HANCOM, clips=clips), "a.pdf", _OPTIONS).text
                == convert(pdf_of_glyphs(runs, producer=_HANCOM), "a.pdf", _OPTIONS).text)
