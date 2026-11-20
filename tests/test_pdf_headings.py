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


class TestBoldHeadings:
    def test_a_bold_title_a_little_larger_than_the_body_is_a_heading(self):
        text = _text([*_body(80), (140, "5. The dynamics", 11, "Times-Bold"), *_body(156)])

        assert "# 5. The dynamics\n\nThe next step" in text

    def test_a_bold_title_at_the_body_size_and_spacing_is_split_from_the_text_below_it(self):
        """'Procedure:' sits at the usual line gap over the steps, so only its weight sets it apart."""
        steps = ["1. Prepare the yeast suspension and add seven grams of yeast to warm tap water.",
                 "2. Fill each tube with the solution and keep it upright for twenty minutes or so."]
        text = _text([*_body(80, 12), (80 + 4 * 15, "Procedure:", 12, "Times-Bold"), *_body(80 + 5 * 15, 12, lines=steps)])

        assert "# Procedure:\n\n1. Prepare the yeast" in text

    def test_a_bold_sentence_filling_the_line_keeps_its_paragraph(self):
        lines = [(80, _BODY[0], 10, "Times-Bold"), *_body(92.5, lines=_BODY[1:])]

        text = _text(lines)

        assert "#" not in text
        assert f"{_BODY[0]}\n{_BODY[1]}" in text

    def test_a_bold_caption_a_value_or_a_sentence_is_not_a_heading(self):
        text = _text([*_body(80), (140, "Figure 3. Sales by region", 10, "Times-Bold"), *_body(156),
                      (220, "2024", 10, "Times-Bold"), *_body(236), (300, "Please read this before you sign.", 10, "Times-Bold"),
                      *_body(316)])

        assert "#" not in text

    def test_a_document_set_in_bold_has_no_bold_headings(self):
        text = _text([*_body(80, font="Times-Bold"), (140, "The dynamics", 10, "Times-Bold"), *_body(156, font="Times-Bold")])

        assert "#" not in text

    def test_bold_headings_at_the_body_size_come_below_larger_headings(self):
        text = _text([(60, "Combinatorial Cosmology", 16, "Times-Bold"), *_body(90), (150, "5. The dynamics", 10.5, "Times-Bold"),
                      *_body(166)])

        assert text.startswith("# Combinatorial Cosmology")
        assert "## 5. The dynamics" in text

    def test_a_section_number_on_a_line_of_its_own_belongs_to_the_heading(self):
        text = _text([*_body(80), (140, "3.", 11, "Times-Bold"), (153, "Perspective of supply and demand", 11, "Times-Bold"),
                      (166, "balance of wood pellets", 11, "Times-Bold"), *_body(184)])

        assert "# 3. Perspective of supply and demand balance of wood pellets" in text

    def test_a_form_row_with_check_boxes_or_a_contents_row_is_not_a_heading(self):
        from reportlab.pdfbase.pdfmetrics import stringWidth

        row = "Lives at home: Yes"
        boxes = [(72 + stringWidth(row, "Times-Bold", 10) + 3, 140)]
        text = _text([*_body(80), (140, row, 10, "Times-Bold"), *_body(156), (220, "2.4. Results .......... 7", 10, "Times-Bold"),
                      *_body(236)], boxes)

        assert "☐" in text
        assert "#" not in text


class TestHeadingSizes:
    def test_a_subheading_a_fifth_larger_than_the_body_is_a_heading(self):
        text = _text([*_body(80, 10.5), (145, "Introduction to the Texts", 12.5, "Times-Roman"), *_body(165, 10.5)])

        assert "# Introduction to the Texts" in text

    def test_a_line_only_a_tenth_larger_is_body_text(self):
        text = _text([*_body(80, 10.5), (145, "Introduction to the Texts", 11.5, "Times-Roman"), *_body(165, 10.5)])

        assert "#" not in text

    def test_a_large_page_number_alone_is_not_a_heading(self):
        text = _text([(60, "12", 20, "Times-Roman"), *_body(90)])

        assert "# 12" not in text

    def test_a_large_value_of_an_infographic_is_not_a_heading(self):
        text = _text([*_body(80), (150, "56%", 24, "Times-Bold"), (175, "47698", 16, "Times-Roman"), *_body(200)])

        assert "#" not in text and "56%" in text


class TestBodyTextMarks:
    def test_a_body_line_starting_with_a_heading_or_quote_mark_stays_text(self):
        text = _text([*_body(80), (140, "# DNA replications", 10, "Times-Roman"), (170, "> 50 participants", 10, "Times-Roman"),
                      *_body(200)])

        assert "\\# DNA replications" in text
        assert "\\> 50 participants" in text


class TestHeaderRows:
    @staticmethod
    def _row_page(titles: list[str]) -> str:
        """Body text, then the bold titles side by side on one row at 1.4 times the body size, each with a line under it,
        then body text again."""
        lines = [(y, text, size, font, 72.0) for y, text, size, font in [*_body(80), *_body(190)]]
        for column, title in enumerate(titles):
            x = 72.0 + column * 120
            lines += [(140, title, 14, "Times-Bold", x), (158, f"text under {title.lower()}", 10, "Times-Roman", x)]
        return convert(_pdf_at(lines), "a.pdf").sections[0].text

    def test_the_header_row_of_a_table_is_not_four_headings(self):
        text = self._row_page(["Service Stage", "Function", "Explanation", "Benefit"])

        assert "#" not in text
        assert "Service Stage" in text and "Benefit" in text

    def test_three_panels_side_by_side_keep_their_headings(self):
        text = self._row_page(["Our Purpose", "Our Mission", "What We Do"])

        assert "# Our Purpose" in text and "# What We Do" in text


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


class TestFontNamesAndLabels:
    def test_a_demilight_line_is_not_bold(self):
        from opendockit.extractors._pdf_model import _is_bold_line

        def chars(font: str) -> list[dict]:
            return [{"text": letter, "fontname": font} for letter in "Heading"]

        assert not _is_bold_line(chars("ABCDEF+NotoSansCJKkr-DemiLight"))
        assert not _is_bold_line(chars("Academica-Regular"))
        assert _is_bold_line(chars("Garamond-Demi")) and _is_bold_line(chars("ABCDEF+NotoSansKR-Bold"))

    def test_a_bold_heading_about_a_week_is_not_a_note_label(self):
        from opendockit.extractors._pdf_model import _CAPTION_START

        assert not _CAPTION_START.match("주 52시간 근무제 개편") and not _CAPTION_START.match("주1회 점검 결과")
        assert _CAPTION_START.match("주: 단위 억원") and _CAPTION_START.match("주1) 잠정치")


class TestCaptionLabels:
    def test_a_word_beginning_like_a_label_is_no_figure_label(self):
        """A one-row box '| Tablet | iPad 10 units |' is a table, not a caption set in a box (`_is_caption_box`)."""
        from opendockit.extractors._pdf_model import _CAPTION_LABEL

        assert not any(_CAPTION_LABEL.fullmatch(word) for word in ("Tablet", "Maps", "Images", "Charts", "Photos"))
        assert all(_CAPTION_LABEL.fullmatch(label) for label in ("Diagram 4", "Figure A.1", "Table IV", "그림 2"))
