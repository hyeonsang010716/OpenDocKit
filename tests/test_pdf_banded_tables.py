"""Tables drawn with horizontal rules only (`_pdf_banded_tables.py`)."""

from io import BytesIO

from opendockit import ConvertOptions, convert


_PLAIN = ConvertOptions(page_markers=False)
_BODY = ["The samples were prepared as described in the previous section of this report.",
         "Each of them was measured three times on the same day by the same person.",
         "The mean of the three measurements is given in the table for every sample."]


def _pdf(rules: list[float], rows: list[tuple[float, list[tuple[float, str]]]], *, left: float = 72, right: float = 320,
         caption: str | None = None, body: bool = True) -> bytes:
    """A page with horizontal rules at the heights `rules` (from the top) and `rows` of (height, [(x, text), …])."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    pdf.setFont("Helvetica", 10)
    if body:
        for index, line in enumerate(_BODY):
            pdf.drawString(72, 780 - 14 * index, line)
    if caption:
        pdf.drawString(left, 842 - (rules[0] - 16), caption)
    pdf.setLineWidth(0.5)
    for top in rules:
        pdf.line(left, 842 - top, right, 842 - top)
    for top, cells in rows:
        for x, text in cells:
            pdf.drawString(x, 842 - top, text)
    if body:
        for index, line in enumerate(_BODY):
            pdf.drawString(72, 842 - (rules[-1] + 60 + 14 * index), line)
    pdf.save()
    return buffer.getvalue()


def _rows(texts: list[list[str]], top: float, xs: tuple[float, ...] = (76, 180, 260)) -> list[tuple[float, list[tuple[float, str]]]]:
    return [(top + 16 * index, [(x, text) for x, text in zip(xs, row, strict=False) if text]) for index, row in enumerate(texts)]


class TestRuleBandTables:
    def test_three_rules_make_a_table_with_the_first_band_as_its_header(self):
        rows = _rows([["Sample", "Mass", "Loss"]], 214) + _rows([["kaolinite", "10", "2"], ["illite", "30", "5"], ["humus", "200", "9"]], 236)

        text = convert(_pdf([200, 222, 280], rows), "a.pdf", _PLAIN).text

        assert "| Sample | Mass | Loss |\n| --- | --- | --- |\n| kaolinite | 10 | 2 |\n| illite | 30 | 5 |\n| humus | 200 | 9 |" in text
        assert text.count("kaolinite") == 1 and "previous section" in text and "for every sample." in text

    def test_a_cell_left_empty_keeps_its_column(self):
        rows = _rows([["Added cation", "Settling rate"]], 214, (76, 200)) + _rows([["K+"], ["Na+"], ["Ca2+"]], 236, (76, 200))

        text = convert(_pdf([200, 222, 280], rows), "a.pdf", _PLAIN).text

        assert "| Added cation | Settling rate |\n| --- | --- |\n| K+ | |\n| Na+ | |\n| Ca2+ | |" in text

    def test_two_rules_make_a_table_only_under_a_caption(self):
        rows = _rows([["Sample", "Mass"], ["kaolinite", "10"], ["illite", "30"], ["humus", "200"]], 214, (76, 220))

        with_caption = convert(_pdf([200, 270], rows, caption="Table 13.4. Typical values."), "a.pdf", _PLAIN).text
        without = convert(_pdf([200, 270], rows, caption="Typical values of the samples."), "a.pdf", _PLAIN).text

        assert "| Sample | Mass |\n| --- | --- |\n| kaolinite | 10 |" in with_caption
        assert "| --- |" not in without and "kaolinite" in without

    def test_a_paragraph_between_rules_stays_a_paragraph(self):
        lines = [(214 + 14 * index, [(76, line[:52])]) for index, line in enumerate(["Abstract", *_BODY, *_BODY])]

        text = convert(_pdf([200, 222, 320], lines, body=False), "a.pdf", _PLAIN).text

        assert "|" not in text and "Abstract" in text

    def test_a_list_of_contents_between_rules_is_not_a_table(self):
        rows = _rows([["Contents", "4"]], 214, (76, 300)) + _rows([["Legal Framework", "6"], ["Administration", "11"], ["Media Freedom", "25"]], 236, (76, 300))

        text = convert(_pdf([200, 222, 280], rows), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and "Media Freedom" in text

    def test_the_value_labels_of_a_chart_between_its_grid_lines_are_not_a_table(self):
        rows = [(214, [(80, "81"), (110, "76")]), (236, [(170, "56")]), (258, [(230, "47")]), (280, [(140, "21")]), (302, [(200, "9")])]

        text = convert(_pdf([200, 222, 244, 266, 288, 310], rows), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and "81" in text and "47" in text

    def test_rules_as_wide_as_the_page_frame_are_not_a_table(self):
        rows = _rows([["Report", "2024"]], 60, (76, 400)) + _rows([["Alpha", "one"], ["Beta", "two"]], 400, (76, 400))

        text = convert(_pdf([70, 790], rows, left=60, right=540, caption="Table 1. Frame.", body=False), "a.pdf", _PLAIN).text

        assert "| --- |" not in text

    def test_one_column_between_rules_stays_lines(self):
        rows = _rows([["Filtered Task Name"]], 214, (76,)) + _rows([["task228_arc_easy"], ["hellaswag:1.1.0"], ["cot_gsm8k"]], 236, (76,))

        text = convert(_pdf([200, 222, 280], rows, caption="Table 8: Task names."), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and text.count("hellaswag:1.1.0") == 1

    def test_boxes_of_one_width_under_each_other_are_not_a_table(self):
        """The box around the masthead of a press release and the box under it: three rules of one width, with sides."""
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(595, 842))
        pdf.setLineWidth(0.4)
        for top, bottom in ((75, 110), (170, 300)):
            for x in (60, 536):
                pdf.line(x, 842 - top, x, 842 - bottom)
            pdf.line(60, 842 - top, 536, 842 - top)
        pdf.line(60, 842 - 110, 536, 842 - 110)
        pdf.setFont("Helvetica", 12)
        pdf.drawString(260, 842 - 98, "Press release")
        pdf.drawString(80, 842 - 130, "Release time")
        pdf.drawString(300, 842 - 130, "Friday 11:00")
        pdf.setFont("Helvetica-Bold", 22)
        pdf.drawString(80, 842 - 205, "Smart farming goes abroad")
        pdf.setFont("Helvetica", 12)
        for index in range(6):
            pdf.drawString(80, 842 - 340 - 16 * index, f"Body line {index} of the release, long enough to be a sentence of its text.")
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf", _PLAIN).text

        assert "| --- |" not in text and "# Smart farming goes abroad" in text
