"""Tables set without any rule whose cells wrap (`_pdf_gutter_tables.py`): the lines of a cell follow each other at the
distance of any other line, and nothing but the next line stands between two rows."""

from io import BytesIO

from opendockit import ConvertOptions, convert


_PLAIN = ConvertOptions(page_markers=False)
_INTRO = "The organisations that were accredited as observers are listed in the table below, with their numbers."
_AFTER = "The list was published by the committee at the end of April and has not been changed since then."


def _pdf(rows: list[list[str]], lefts: tuple[float, ...], widths: tuple[float, ...], *, leading: float = 12) -> bytes:
    """A page with a line of text, a table whose cells are wrapped to their `widths` and set from `lefts` with no space
    between its rows, and a line of text."""
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    pdf.setFont("Helvetica", 10)
    pdf.drawString(72, 770, _INTRO)
    top = 740.0
    for row in rows:
        cells = [simpleSplit(cell, "Helvetica", 10, width) for cell, width in zip(row, widths, strict=True)]
        for left, lines in zip(lefts, cells, strict=True):
            for index, line in enumerate(lines):
                pdf.drawString(left, top - leading * index, line)
        top -= leading * max(len(lines) for lines in cells)
    pdf.drawString(72, top - 24, _AFTER)
    pdf.save()
    return buffer.getvalue()


_ROWS = [["No.", "Name of organization", "Number of accredited observers"],
         ["1", "Union of Youth Federations of Cambodia (UYFC)", "17,266"],
         ["2", "Cambodian Women for Peace and Development", "9,835"],
         ["3", "Our Friends Association", "27"],
         ["4", "Traditional and Modern Mental Health Organization", "15"]]
_LEFTS, _WIDTHS = (72.0, 200.0, 340.0), (110.0, 120.0, 110.0)


class TestWrappedCells:
    def test_the_lines_of_a_cell_are_one_cell(self):
        text = convert(_pdf(_ROWS, _LEFTS, _WIDTHS), "a.pdf", _PLAIN).text

        assert ("| No. | Name of organization | Number of accredited observers |\n| --- | --- | --- |\n"
                "| 1 | Union of Youth Federations of Cambodia (UYFC) | 17,266 |\n"
                "| 2 | Cambodian Women for Peace and Development | 9,835 |\n"
                "| 3 | Our Friends Association | 27 |\n"
                "| 4 | Traditional and Modern Mental Health Organization | 15 |") in text
        assert _INTRO in text and _AFTER in text and text.count("UYFC") == 1

    def test_a_row_with_a_cell_left_empty_is_a_row_not_a_wrapped_line(self):
        """'Total' under the names has text in fewer columns than the row above, but it had room on the line above."""
        text = convert(_pdf([*_ROWS, ["", "Total", "27,143"]], _LEFTS, _WIDTHS), "a.pdf", _PLAIN).text

        assert "| 4 | Traditional and Modern Mental Health Organization | 15 |\n| | Total | 27,143 |" in text

    def test_a_value_under_a_value_is_the_next_row(self):
        """A table that thins out to the right: '5' under '4' and '8.93%' under '12.49%' are cells of the next row."""
        rows = [["Year", "Recovery rate of the asset class", "Unadjusted basis", "Depreciation"],
                ["3", "14.81%", "19.20%", "17.49%"], ["4", "7.41%", "11.52%", "12.49%"], ["5", "", "11.52%", "8.93%"],
                ["6", "", "5.76%", "8.93%"], ["7", "", "", "8.93%"]]

        text = convert(_pdf(rows, (72.0, 130.0, 250.0, 370.0), (40.0, 90.0, 100.0, 100.0)), "a.pdf", _PLAIN).text

        assert "| 4 | 7.41% | 11.52% | 12.49% |\n| 5 | | 11.52% | 8.93% |\n| 6 | | 5.76% | 8.93% |\n| 7 | | | 8.93% |" in text


class TestWhatIsLeftAlone:
    def test_columns_of_prose_with_one_ending_early_are_not_a_table(self):
        """The lines of each column go on under each other: no cell is seen to end above a row."""
        prose = ["It is a truth universally acknowledged, that a single man in possession of a good fortune, must be in want "
                 "of a wife. However little known the feelings or views of such a man may be on his first entering.",
                 "Call me Ishmael. Some years ago, never mind how long precisely, having little or no money in my purse.",
                 "It was the best of times, it was the worst of times, it was the age of wisdom, it was the age of foolishness, "
                 "it was the epoch of belief, it was the epoch of incredulity, it was the season of Light."]

        text = convert(_pdf([prose], (72.0, 232.0, 392.0), (140.0, 140.0, 140.0)), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and "Call me Ishmael." in text

    def test_a_table_of_two_columns_is_left_to_the_other_searches(self):
        rows = [["Name of organization", "Seats"], ["Union of Youth Federations of Cambodia (UYFC)", "17"],
                ["Cambodian Women for Peace and Development", "9"], ["Our Friends Association", "2"]]

        text = convert(_pdf(rows, (72.0, 260.0), (120.0, 60.0)), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and "Our Friends Association" in text

    def test_a_table_whose_cells_do_not_wrap_is_read_as_before(self):
        """Nothing wraps: the borderless search reads it, with its own rules."""
        rows = [["Item", "Quantity", "Price"], ["Apples", "12", "3,200"], ["Pears", "7", "4,100"], ["Grapes", "3", "9,800"]]
        from opendockit.extractors import _pdf_gutter_tables

        made = []
        original = _pdf_gutter_tables._gutter_table
        _pdf_gutter_tables._gutter_table = lambda block: made.append(original(block)) or made[-1]
        try:
            text = convert(_pdf(rows, _LEFTS, _WIDTHS, leading=16), "a.pdf", _PLAIN).text
        finally:
            _pdf_gutter_tables._gutter_table = original

        assert "| Item | Quantity | Price |\n| --- | --- | --- |\n| Apples | 12 | 3,200 |" in text
        assert not any(made)
