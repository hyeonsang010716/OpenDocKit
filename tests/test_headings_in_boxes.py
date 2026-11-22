"""A table of one column is a layout box, written as paragraphs; a heading among the paragraphs of its cells is a heading.

One rule for the formats that draw their tables with `markdown.TableGrid` (`TableGrid.unfold`): the text of a cell is its
paragraphs joined into lines, so a paragraph the author had put in a heading style came out as a line of text — in DOCX,
ODT and RTF alike, while the same box in HTML kept its headings. The Hangul formats, which tell a title by its size, are in
`test_hwp_size_headings.py::TestTitlesInBoxes`.
"""

import pytest

from opendockit import convert
from opendockit.markdown import CellHeading, TableGrid
from tests.test_odf import _odt
from tests.test_office import _docx_with_body, _paragraph
from tests.test_rtf import _STYLES, _row, _text


# One box in each format: a cell holding a title and a line, a cell holding a line, a subtitle and another line.
_CELLS = [[("Title in a box", 1), ("A line under the title.", 0)], [("Second cell.", 0), ("Sub in a box", 2), ("Its text.", 0)]]
_EXPECTED = ["# Title in a box", "A line under the title.", "Second cell.", "## Sub in a box", "Its text.", "After the box."]


def _docx_box(cells: list[list[tuple[str, int]]], columns: int = 1) -> str:
    def cell(paragraphs: list[tuple[str, int]]) -> str:
        return "<w:tc>" + "".join(_paragraph(text, style=f"Heading{level}" if level else None) for text, level in paragraphs) + "</w:tc>"
    extra = "<w:tc>" + _paragraph("beside") + "</w:tc>" if columns == 2 else ""
    return "<w:tbl>" + "".join(f"<w:tr>{cell(paragraphs)}{extra}</w:tr>" for paragraphs in cells) + "</w:tbl>"


def _odt_box(cells: list[list[tuple[str, int]]], columns: int = 1) -> str:
    def cell(paragraphs: list[tuple[str, int]]) -> str:
        return "<table:table-cell>" + "".join(
            f'<text:h text:outline-level="{level}">{text}</text:h>' if level else f"<text:p>{text}</text:p>"
            for text, level in paragraphs) + "</table:table-cell>"
    extra = "<table:table-cell><text:p>beside</text:p></table:table-cell>" if columns == 2 else ""
    return "<table:table>" + "".join(f"<table:table-row>{cell(paragraphs)}{extra}</table:table-row>" for paragraphs in cells) + "</table:table>"


def _rtf_box(cells: list[list[tuple[str, int]]], columns: int = 1) -> str:
    def cell(paragraphs: list[tuple[str, int]]) -> str:
        return r"\par".join(rf"\pard\intbl\s{level} {text}" if level else rf"\pard\intbl\s0 {text}" for text, level in paragraphs)
    return "".join(_row([cell(paragraphs)] + ["beside"] * (columns - 1)) for paragraphs in cells)


def _convert_box(kind: str, cells: list[list[tuple[str, int]]], columns: int = 1) -> str:
    if kind == "docx":
        return convert(_docx_with_body(_docx_box(cells, columns) + _paragraph("After the box.")), "a.docx").text
    if kind == "odt":
        return _odt(_odt_box(cells, columns) + "<text:p>After the box.</text:p>")
    return _text(_STYLES + _rtf_box(cells, columns) + r"\pard\s0 After the box.\par")


@pytest.mark.parametrize("kind", ["docx", "odt", "rtf"])
class TestHeadingsInBoxes:
    def test_a_heading_in_a_box_is_a_heading(self, kind):
        assert _convert_box(kind, _CELLS).split("\n\n") == _EXPECTED

    def test_a_box_without_a_heading_is_written_as_before(self, kind):
        """The paragraphs of a cell as lines, the cells as paragraphs."""
        text = _convert_box(kind, [[("First line.", 0), ("Second line.", 0)], [("Another cell.", 0)]])

        assert text == "First line.\nSecond line.\n\nAnother cell.\n\nAfter the box."

    def test_a_heading_in_a_cell_of_a_table_is_still_text(self, kind):
        text = _convert_box(kind, _CELLS, columns=2)

        assert "#" not in text
        assert "| Title in a box<br>A line under the title. | beside |" in text

    def test_a_cell_that_is_only_a_heading(self, kind):
        assert _convert_box(kind, [[("Only a title", 1)]]).split("\n\n") == ["# Only a title", "After the box."]

    @pytest.mark.parametrize("empty", [[], [("", 0)]])
    def test_an_empty_cell_beside_the_heading(self, kind, empty):
        """The title of a cover page stands between two empty cells. A cell without a paragraph raised, and the document
        was refused as corrupted."""
        text = _convert_box(kind, [empty, [("Title in a box", 1), ("A line under the title.", 0)], empty])

        assert text.split("\n\n") == ["# Title in a box", "A line under the title.", "After the box."]


class TestBoxesInsideOtherThings:
    def test_a_one_column_table_inside_a_cell_is_not_unfolded(self):
        """Only a table that is a block of the body is a box: one in a cell is drawn by the table around it."""
        inner = _docx_box([[("Inner title", 1), ("Inner line.", 0)]])
        table = f"<w:tbl><w:tr><w:tc>{inner}{_paragraph('')}</w:tc><w:tc>{_paragraph('beside')}</w:tc></w:tr></w:tbl>"

        text = convert(_docx_with_body(table + _paragraph("After the box.")), "a.docx").text

        assert "#" not in text and "Inner title" in text

    def test_a_table_inside_a_box_with_a_heading_is_kept(self):
        """The box is unfolded around its headings by the table itself, which holds the tables of its cells."""
        inner = f"<w:tbl><w:tr><w:tc>{_paragraph('a')}</w:tc><w:tc>{_paragraph('b')}</w:tc></w:tr><w:tr><w:tc>{_paragraph('c')}</w:tc><w:tc>{_paragraph('d')}</w:tc></w:tr></w:tbl>"
        box = f"<w:tbl><w:tr><w:tc>{_paragraph('Title', style='Heading1')}{_paragraph('Before.')}{inner}{_paragraph('Under.')}</w:tc></w:tr></w:tbl>"

        text = convert(_docx_with_body(box + _paragraph("After the box.")), "a.docx").text

        assert text.split("\n\n") == ["# Title", "Before.", "| a | b |\n| --- | --- |\n| c | d |", "Under.", "After the box."]


class TestUnfoldedBox:
    @staticmethod
    def _grid(*cells: list[str]) -> TableGrid:
        grid = TableGrid()
        for row, cell in enumerate(cells):
            grid.place(row, 0, "\n".join(cell))
        return grid

    def test_without_a_heading_what_was_written_is_kept(self):
        cells = [["one", "two"], ["three"]]
        grid = self._grid(*cells)

        assert grid.unfolded(grid.render(), cells) == ["one\ntwo\n\nthree"]

    def test_a_heading_is_a_block_and_the_text_around_it_is_written_as_a_cell(self):
        cells = [["above", CellHeading.of("Title", 2, "Title"), "under  it", "a | b"]]
        grid = self._grid(*cells)

        assert grid.unfolded(grid.render(), cells) == ["above", "## Title", "under it\na | b"]

    def test_headings_that_continue_each_other_are_one_title(self):
        first, second, third = (CellHeading.of(text, 1, text) for text in ("A title,", "set on two lines", "Another"))
        grid = self._grid([first, second, third])

        blocks = grid.unfold([[first, second, third]], lambda block: (block.level, block.title),
                             lambda block, following: following is second)

        assert blocks == ["# A title, set on two lines", "# Another"]

    def test_a_cell_without_a_paragraph_writes_nothing(self):
        heading = CellHeading.of("Title", 1, "Title")
        grid = self._grid([], [heading, "under it"], [])

        assert grid.unfolded(grid.render(), [[], [heading, "under it"], []]) == ["# Title", "under it"]

    def test_a_heading_taken_for_text_is_the_text_it_was(self):
        """Its value is the paragraph as the cell writes it: joined, sliced or cleaned, it is that text."""
        heading = CellHeading.of("- 1. Scope", 1, "1. Scope")

        assert "\n".join(["before", heading]) == "before\n- 1. Scope" and type(heading.strip()) is str
