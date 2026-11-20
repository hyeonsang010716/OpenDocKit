"""Headings of a PDF told from the body by weight, and by a size a little above the body."""

from io import BytesIO

from opendockit import convert


_BODY = [
    "The next step is to construct a model for the dynamics of the system as a whole, and",
    "the idea, which essentially goes back to Boltzmann, is that any given state at any",
    "given time is extremely likely to develop into a state with a higher entropy at the",
    "next moment of time, simply because there are so many more states of that kind.",
]


def _pdf(lines: list[tuple[float, str, float, str]], boxes: list[tuple[float, float]] = ()) -> bytes:
    """A one-page PDF drawing each (y from the top, text, size, font) at the left margin, and an 8pt check box at each
    (x, y from the top)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for y, text, size, font in lines:
        pdf.setFont(font, size)
        pdf.drawString(72, A4[1] - y, text)
    for x, y in boxes:
        pdf.rect(x, A4[1] - y, 8, 8)
    pdf.save()
    return buffer.getvalue()


def _body(top: float, size: float = 10, font: str = "Times-Roman", lines: list[str] = _BODY) -> list[tuple[float, str, float, str]]:
    return [(top + index * size * 1.25, line, size, font) for index, line in enumerate(lines)]


def _text(lines: list[tuple[float, str, float, str]], boxes: list[tuple[float, float]] = ()) -> str:
    return convert(_pdf(lines, boxes), "a.pdf").sections[0].text


class TestHeadingSizes:
    def test_paragraphs_a_little_larger_than_a_small_body_are_body_text(self):
        rows = [f"Row {index} of the table data with several words in a smaller size for the report" for index in range(30)]
        text = _text([*_body(60, 9, lines=rows), (420, "This short paragraph is written in the normal text size of the report.", 10.5,
                      "Times-Roman")])

        assert "#" not in text

    def test_a_line_only_a_tenth_larger_is_body_text(self):
        text = _text([*_body(80, 10.5), (145, "Introduction to the Texts", 11.5, "Times-Roman"), *_body(165, 10.5)])

        assert "#" not in text

    def test_a_large_page_number_alone_is_not_a_heading(self):
        text = _text([(60, "12", 20, "Times-Roman"), *_body(90)])

        assert "# 12" not in text

    def test_a_large_year_is_a_heading(self):
        """The years of a timeline set large; a bold year at the body size stays a value."""
        text = _text([(50, "Company History", 20, "Times-Bold"), (90, "2023", 14, "Times-Bold"), *_body(110),
                      (170, "2024", 14, "Times-Bold"), *_body(190), (250, "2025", 10, "Times-Bold"), *_body(266)])

        assert "## 2023\n\nThe next step" in text and "## 2024\n\nThe next step" in text
        assert "# 2025" not in text

    def test_a_large_value_of_an_infographic_is_not_a_heading(self):
        text = _text([*_body(80), (150, "56%", 24, "Times-Bold"), (175, "47698", 16, "Times-Roman"), *_body(200)])

        assert "#" not in text and "56%" in text


class TestBodyTextMarks:
    def test_a_body_line_starting_with_a_heading_or_quote_mark_stays_text(self):
        text = _text([*_body(80), (140, "# DNA replications", 10, "Times-Roman"), (170, "> 50 participants", 10, "Times-Roman"),
                      *_body(200)])

        assert "\\# DNA replications" in text
        assert "\\> 50 participants" in text


def _pdf_at(lines: list[tuple[float, str, float, str, float]]) -> bytes:
    """A one-page PDF drawing each (y from the top, text, size, font, x)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for y, text, size, font, x in lines:
        pdf.setFont(font, size)
        pdf.drawString(x, A4[1] - y, text)
    pdf.save()
    return buffer.getvalue()


class TestCaptionLabels:
    def test_a_word_beginning_like_a_label_is_no_figure_label(self):
        """A one-row box '| Tablet | iPad 10 units |' is a table, not a caption set in a box (`_is_caption_box`)."""
        from opendockit.extractors._pdf_model import _CAPTION_LABEL

        assert not any(_CAPTION_LABEL.fullmatch(word) for word in ("Tablet", "Maps", "Images", "Charts", "Photos"))
        assert all(_CAPTION_LABEL.fullmatch(label) for label in ("Diagram 4", "Figure A.1", "Table IV", "그림 2"))
