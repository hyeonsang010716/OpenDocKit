"""Headings of a PDF told from the body by weight, and by a size a little above the body."""

from io import BytesIO

import pytest

from opendockit import convert
from tests.test_pdf import _raw_pdf


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

    def test_a_sentence_a_little_larger_than_the_body_is_body_text(self):
        """A pull quote 1.15 times the body, and a sentence at the size of a subheading of the same band."""
        text = _text([(50, "Report", 20, "Times-Bold"), *_body(80, 10.5), (145, "Introduction to the Texts", 12.5, "Times-Roman"),
                      *_body(165, 10.5), (230, "Revenue grew in every region this year.", 12.5, "Times-Italic"), *_body(250, 10.5)])

        assert "## Introduction to the Texts" in text
        assert "\n\nRevenue grew in every region this year.\n\n" in text

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


class TestHeaderRows:
    @staticmethod
    def _row_page(titles: list[str], under: list[list[str]] | None = None, size: float = 14, pitch: float = 120) -> str:
        """Body text, then the bold titles side by side on one row `pitch` apart (1.4 times the body size by default), each
        with the lines of `under` (one row of them every 13pt; by default one line of text), then body text again."""
        under = under or [[f"text under {title.lower()}" for title in titles]]
        lines = [(y, text, size_, font, 72.0) for y, text, size_, font in [*_body(80), *_body(175 + 13 * len(under))]]
        for column, title in enumerate(titles):
            x = 72.0 + column * pitch
            lines.append((140, title, size, "Times-Bold", x))
            lines += [(158 + 13 * row, cells[column], 10, "Times-Roman", x) for row, cells in enumerate(under) if cells[column]]
        return convert(_pdf_at(lines), "a.pdf").sections[0].text

    def test_the_header_row_of_a_table_is_not_four_headings(self):
        """The header row of a table with three rows of short cells under it. A table with a single data row is not told
        from the panels of a slide, a line of text under each heading (an accepted gap of `_cell_rows_under`): this test had
        one line of text under each title until the four panels of a slide, which main kept as headings, were told from a
        table by the rows of cells under it."""
        text = self._row_page(["Service Stage", "Function", "Explanation", "Benefit"],
                              [["Intake", "Register", "Checks", "Faster"], ["Review", "Score", "", "Fairer"],
                               ["Payment", "Transfer", "Pays out", "Sooner"]])

        assert "#" not in text
        assert "Service Stage" in text and "Benefit" in text

    @pytest.mark.parametrize("size", [10, 12])
    def test_the_header_row_of_a_table_of_two_columns_is_no_heading(self, size):
        text = self._row_page(["Name", "Description"], [["alpha", "first letter"], ["beta", "second letter"]], size, 228)

        assert "#" not in text
        assert "Description" in text and "second letter" in text

    def test_three_panels_side_by_side_keep_their_headings(self):
        text = self._row_page(["Our Purpose", "Our Mission", "What We Do"])

        assert "# Our Purpose" in text and "# What We Do" in text

    @pytest.mark.parametrize("size", [10, 14])
    def test_headings_over_short_lines_of_prose_stay_headings(self, size):
        """Two headings 280pt apart, each over a column of short ragged prose at the same baselines as the other's."""
        full = ["The next step is to construct a model for the", "dynamics of the system as a whole and the idea",
                "which goes back to Boltzmann is that any state", "is likely to develop into a higher entropy."]
        narrow = ["Open Monday to Friday", "from nine to five and", "on Saturday mornings", "by appointment only."]
        items = [(x, 80 + index * 12.5, line, 10, "Times-Roman") for x in (40, 320) for index, line in enumerate(full)]
        items += [(40, 140, "Opening Hours", size, "Times-Bold"), (320, 140, "Our Location", size, "Times-Bold")]
        items += [(x, 140 + size * 1.6 + index * 12.5, line, 10, "Times-Roman") for x in (40, 320)
                  for index, line in enumerate(narrow)]
        text = _named_font_pdf(items)

        assert "# Opening Hours" in text and "# Our Location" in text

    def test_four_panels_of_a_slide_keep_their_headings(self):
        text = self._row_page(["Plan", "Build", "Test", "Ship"], [["Set goals", "Write code", "Check it", "Release"]])

        assert all(f"# {title}" in text for title in ["Plan", "Build", "Test", "Ship"])

    @pytest.mark.parametrize(("width", "words", "header"), [(200, "the text of the column", False), (60, "alpha", True)],
                             ids=["prose", "cells"])
    def test_lines_filling_their_column_under_headings_are_prose_not_cells(self, width, words, header):
        """Two headings of two columns on one row with the lines of their columns under them stay headings; with cells
        (narrower than half the column, under three words) under them they are the header of a table."""
        from opendockit.extractors._pdf_model import _Line
        from opendockit.extractors._pdf_paragraphs import _header_cells

        heads = [_Line(top=100, bottom=110, x0=x, x1=x + 40, text=text, size=10, bold=True)
                 for x, text in [(72, "Methods"), (300, "Results")]]
        under = [_Line(top=113 + 13 * row, bottom=123 + 13 * row, x0=x, x1=x + width, text=words, size=10)
                 for row in range(3) for x in (72, 300)]

        assert (_header_cells(heads + under, 10.0) == {id(line) for line in heads}) is header


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


class TestHeaderRowsTakeLinearTime:
    @pytest.mark.timing
    def test_a_row_of_a_thousand_pieces_is_looked_at_once(self):
        """The lines are grouped into rows once: comparing each line of a crowded row with the others, 400 bold pieces on one
        row took 0.8 seconds and 1,000 did not finish in two minutes."""
        from opendockit.extractors._pdf_model import _Line
        from opendockit.extractors._pdf_paragraphs import _header_cells
        from tests.timing import fastest

        def seconds(count: int) -> float:
            lines = [_Line(top=100, bottom=110, x0=10.0 * index, x1=10.0 * index + 8, text="Ab", size=14, bold=True)
                     for index in range(count)]
            return fastest(lambda: _header_cells(lines, 10.0))

        small, large = seconds(1_000), seconds(4_000)
        assert large < small * 8


class TestBoldSentences:
    """A bold line at the body size that is a sentence is emphasis in the text, not a heading."""

    def test_a_sentence_ending_in_a_number_is_no_heading(self):
        text = _text([*_body(80), (140, "Applications must be received by June 30.", 10, "Times-Bold"), *_body(156),
                      (220, "The full list of sites is given in Appendix C.", 10, "Times-Bold"), *_body(236)])

        assert "#" not in text
        assert "Applications must be received by June 30." in text and "given in Appendix C." in text

    @pytest.mark.parametrize("label", ["CHAPTER 1.", "Part IV.", "Section 2.3.", "Appendix C."])
    def test_a_numbered_label_with_a_full_stop_is_a_heading(self, label):
        text = _text([*_body(80), (140, label, 10, "Times-Bold"), *_body(156)])

        assert f"# {label}\n\nThe next step" in text


_KOREAN_BODY = [
    "본 계약은 갑과 을 사이의 용역 제공에 관한 기본적인 사항을 정하는 것을 목적으로",
    "하며 당사자는 신의에 따라 성실하게 이 계약을 이행하여야 하고 상대방의 권리를",
    "존중하여야 한다. 계약의 내용은 서면으로 변경할 수 있으며 변경된 내용은 즉시",
    "효력이 발생한다. 을은 갑의 사전 동의 없이 제3자에게 업무를 위탁할 수 없다.",
]


def _named_font_pdf(items: list[tuple[float, float, str, float, str]]) -> str:
    """The text of a one-page PDF drawing each (x, y from the top, text, size, font name) in a font of exactly that name (not
    embedded, Identity-H, a character's code is its code point, 500 units wide), so a Korean line can be set in a bold font
    name ('Batang-Bold') as Korean PDFs do."""
    names = list(dict.fromkeys(item[4] for item in items))
    highs = sorted({ord(char) >> 8 for item in items for char in item[2]})
    cmap = ("/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /U def 1 begincodespacerange <0000> <FFFF>"
            f" endcodespacerange {len(highs)} beginbfrange " + " ".join(f"<{h:02X}00> <{h:02X}FF> <{h:02X}00>" for h in highs)
            + " endbfrange endcmap CMapName currentdict /CMap defineresource pop end end").encode()
    content = "\n".join(f"BT /F{names.index(font)} {size} Tf {x} {842 - y} Td <{''.join(f'{ord(c):04X}' for c in text)}> Tj ET"
                         for x, y, text, size, font in items).encode()
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << %s >> >> >>"
                  % b" ".join(b"/F%d %d 0 R" % (index, 10 + 3 * index) for index in range(len(names))),
               4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
               5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)}
    for index, name in enumerate(names):
        base = 10 + 3 * index
        objects[base] = (b"<< /Type /Font /Subtype /Type0 /BaseFont /%s /Encoding /Identity-H /DescendantFonts [%d 0 R]"
                         b" /ToUnicode 5 0 R >>" % (name.encode(), base + 1))
        objects[base + 1] = (b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /%s /CIDSystemInfo << /Registry (Adobe)"
                             b" /Ordering (Identity) /Supplement 0 >> /FontDescriptor %d 0 R /DW 500 /CIDToGIDMap /Identity >>"
                             % (name.encode(), base + 2))
        objects[base + 2] = (b"<< /Type /FontDescriptor /FontName /%s /Flags 32 /FontBBox [0 -200 1000 900] /ItalicAngle 0"
                             b" /Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>" % name.encode())
    return convert(_raw_pdf(objects), "a.pdf").sections[0].text


def _named_body(top: float, font: str = "Batang", lines: list[str] = _KOREAN_BODY) -> list[tuple[float, float, str, float, str]]:
    return [(72, top + index * 12.5, line, 10, font) for index, line in enumerate(lines)]


class TestBoldSentenceEnds:
    """A bold sentence ending in '!', '?' or ':', holding two sentences, or ending like a Korean sentence is no heading; a
    short title ending so, and a Korean title, is."""

    @pytest.mark.parametrize("line", ["Do not discard this notice!", "Questions? Call 1-800-555-0100",
                                      "The following documents must be attached:", "The applicant must provide:",
                                      "Please note the following:"])
    def test_a_bold_sentence_with_another_end_is_no_heading(self, line):
        text = _text([*_body(80), (140, line, 10, "Times-Bold"), *_body(156)])

        assert "#" not in text and line in text

    @pytest.mark.parametrize("line", ["신청서는 반드시 기한 내에 제출하여야 합니다", "을은 갑의 사전 동의 없이 업무를 위탁할 수 없다"])
    def test_a_korean_sentence_without_a_full_stop_is_no_heading(self, line):
        text = _named_font_pdf([*_named_body(80), (72, 140, line, 10, "Batang-Bold"), *_named_body(156)])

        assert "#" not in text and line in text

    @pytest.mark.parametrize("line", ["서류는 기한 내 제출함", "본 계약은 무효임", "위와 같이 신청함", "다음과 같음", "제출 서류는 다음과 같음:", "외부 회의 참여함", "사업 계획 수립됨",
                                      "반드시 원본을 제출할 것", "기한 내 제출 바람", "협조하기 바람", "확인해 주세요", "서명하십시오",
                                      "반드시 확인하라", "결과를 얻는다", "제출하였음"])
    def test_a_korean_notice_is_no_heading(self, line):
        text = _named_font_pdf([*_named_body(80), (72, 140, line, 10, "Batang-Bold"), *_named_body(156)])

        assert "#" not in text and line in text

    @pytest.mark.parametrize("title", ["핵 잠수함", "미 해군 항공모함", "고객 건의함", "우편 사서함", "받은 편지함", "분석 프레임",
                                       "재난 대응 골든타임", "자바 런타임", "교수 정년퇴임", "회장 이취임", "사외이사 재선임",
                                       "운영위원회 정기모임", "기업의 사회적책임", "부가세 불포함"])
    def test_a_korean_compound_noun_ending_like_a_notice_is_a_heading(self, title):
        text = _named_font_pdf([*_named_body(80), (72, 140, title, 10, "Batang-Bold"), *_named_body(156)])

        assert f"# {title}\n\n본 계약은" in text

    @pytest.mark.parametrize("title", ["변화의 바람", "교육계에 부는 변화의 바람", "따뜻한 봄바람", "한류 바람"])
    def test_a_korean_title_about_the_wind_is_a_heading(self, title):
        text = _named_font_pdf([*_named_body(80), (72, 140, title, 10, "Batang-Bold"), *_named_body(156)])

        assert f"# {title}\n\n본 계약은" in text

    @pytest.mark.parametrize("title", ["꼭 알아야 할 것", "창업 전 알아야 할 것"])
    def test_a_korean_title_of_things_to_know_is_a_heading(self, title):
        text = _named_font_pdf([*_named_body(80), (72, 140, title, 10, "Batang-Bold"), *_named_body(156)])

        assert f"# {title}\n\n본 계약은" in text

    @pytest.mark.parametrize("title", ["안녕하세요", "어서 오세요", "사하라"])
    def test_a_korean_greeting_or_name_is_a_heading(self, title):
        text = _named_font_pdf([*_named_body(80), (72, 140, title, 10, "Batang-Bold"), *_named_body(156)])

        assert f"# {title}\n\n본 계약은" in text

    @pytest.mark.parametrize("title", ["제1조(목적)", "1. 사업 개요", "추진 배경", "개요", "필요", "다음", "정기 모임", "사회적 책임",
                                       "추진 현황 및 계획:", "1. 추진 경과 및 향후 계획:", "캐나다", "참고 자료", "향후 과제", "제5조(계약의 해지)", "왜 필요한가?", "100억원 투자 계획"])
    def test_a_korean_title_in_bold_is_a_heading(self, title):
        text = _named_font_pdf([*_named_body(80), (72, 140, title, 10, "Batang-Bold"), *_named_body(156)])

        assert f"# {title}\n\n본 계약은" in text

    @pytest.mark.parametrize("title", ["Why entropy?", "Read this first!", "Key findings", "1.1 Scope", "Requirements:",
                                       "Key findings:", "Chapter 3: Results", "Part 1: Getting Started:",
                                       "Summary of Key Findings:", "Terms of the Agreement:", "Risks and Mitigation Measures:",
                                       "Q&A Session with Panelists:", "Section 2. Payment Terms:", "1. Scope of Work:",
                                       "2.1 Data and Methods:", "III. Terms of Payment:"])
    def test_a_short_title_is_a_heading(self, title):
        text = _text([*_body(80), (140, title, 10, "Times-Bold"), *_body(156)])

        assert f"# {title}\n\nThe next step" in text

    def test_a_bold_sentence_closing_a_paragraph_keeps_it(self):
        text = _text([(80, _BODY[0], 10, "Times-Roman"), (92.5, "of time, simply because there are so many more of that kind.", 10,
                       "Times-Roman"), (105, "Do not discard this notice!", 10, "Times-Bold"), *_body(130)])

        assert "many more of that kind.\nDo not discard this notice!\n\nThe next step" in text


class TestBoldValues:
    """A total, a date line, an address or the name under a letter's closing set in bold at the body size is no heading."""

    @pytest.mark.parametrize("line", ["Total due USD 1,234.00", "March 3, 2024", "Q3 2024", "Acme Corp, PO Box 12, Springfield"])
    def test_a_bold_value_is_no_heading(self, line):
        text = _text([*_body(80), (140, line, 10, "Times-Bold"), *_body(156)])

        assert "#" not in text and line in text

    def test_a_bold_korean_total_is_no_heading(self):
        text = _named_font_pdf([*_named_body(80), (72, 140, "합계 금액 1,000,000원", 10, "Batang-Bold"), *_named_body(156)])

        assert "#" not in text and "합계 금액 1,000,000원" in text

    def test_the_name_under_the_closing_of_a_letter_is_no_heading(self):
        text = _named_font_pdf([*_named_body(80, "Times-Roman", _BODY), (72, 150, "Sincerely,", 10, "Times-Roman"),
                                (72, 190, "Jane Smith", 10, "Times-Bold"), (72, 202.5, "Director of Sales", 10, "Times-Roman")])

        assert "#" not in text and "Jane Smith" in text

    @pytest.mark.parametrize("title", ["Results for 2023", "Planning, design and construction", "5. The dynamics"])
    def test_a_title_with_a_number_or_commas_is_a_heading(self, title):
        text = _text([*_body(80), (140, title, 10, "Times-Bold"), *_body(156)])

        assert f"# {title}\n\nThe next step" in text


class TestStackedLabels:
    """Bold labels stacked in one column with their values beside them are a form, not a heading over two lines."""

    def test_stacked_bold_labels_with_values_beside_them_keep_their_lines(self):
        form = [(72, 150, "Name", 10, "Times-Bold"), (150, 150, "Kim", 10, "Times-Roman"), (250, 150, "Date", 10, "Times-Bold"),
                (330, 150, "2024", 10, "Times-Roman"), (72, 163, "Phone", 10, "Times-Bold"), (150, 163, "010", 10, "Times-Roman"),
                (250, 163, "Email", 10, "Times-Bold"), (330, 163, "a@b.c", 10, "Times-Roman")]
        text = _named_font_pdf([*_named_body(80, "Times-Roman", _BODY), *form, *_named_body(190, "Times-Roman", _BODY)])

        assert "#" not in text
        assert "Name\nPhone" in text and "Date\nEmail" in text

    @pytest.mark.parametrize(("labels", "values", "label_font", "value_font", "value_size"), [
        (["Name", "Phone"], ["Kim Minsu", "010-1234-5678"], "Times-Bold", "Times-Bold", 10),
        (["Name", "Phone"], ["Kim Minsu", "010-1234-5678"], "Times-Bold", "Times-Roman", 11),
        (["Invoice No.", "Due Date"], ["INV-2024-001", "March 3, 2024"], "Times-Bold", "Times-Bold", 10),
        (["성명", "연락처"], ["김민수", "010-1234-5678"], "Batang-Bold", "Batang-Bold", 10),
        (["신청인", "소속"], ["김민수", "기획조정실"], "Batang-Bold", "Batang-Bold", 10),
    ], ids=["bold-values", "larger-values", "invoice", "korean", "korean-names"])
    def test_stacked_labels_with_bold_or_larger_values_are_no_heading(self, labels, values, label_font, value_font, value_size):
        items = [*_named_body(80, "Times-Roman", _BODY)]
        for row, (label, value) in enumerate(zip(labels, values, strict=True)):
            items += [(72, 140 + 12.5 * row, label, 10, label_font), (250, 140 + 12.5 * row, value, value_size, value_font)]
        text = _named_font_pdf([*items, *_named_body(185, "Times-Roman", _BODY)])

        assert "#" not in text
        assert all(label in text and value in text for label, value in zip(labels, values, strict=True))

    def test_a_bold_heading_over_two_lines_stays_one_heading(self):
        text = _named_font_pdf([*_named_body(80, "Times-Roman", _BODY),
                                (72, 140, "A Very Long Heading That Is Set In Bold Over", 10, "Times-Bold"),
                                (72, 152.5, "Two Lines Of The Page", 10, "Times-Bold"), *_named_body(170, "Times-Roman", _BODY)])

        assert "# A Very Long Heading That Is Set In Bold Over Two Lines Of The Page\n\nThe next step" in text

    def test_a_large_title_beside_short_lines_stays_one_heading(self):
        """A cover title over two lines with 'Draft' and the date at the right of its lines: a heading by its size is no
        form label."""
        text = _named_font_pdf([(72, 100, "Annual Report on", 24, "Times-Bold"), (72, 130, "Water Quality", 24, "Times-Bold"),
                                (450, 100, "Draft", 10, "Times-Roman"), (450, 130, "May 2024", 10, "Times-Roman"),
                                *_named_body(200, "Times-Roman", _BODY)])

        assert "# Annual Report on Water Quality" in text

    def test_a_bold_heading_beside_margin_notes_stays_one_heading(self):
        text = _named_font_pdf([*_named_body(80, "Times-Roman", _BODY), (72, 140, "Annual Results of the", 10, "Times-Bold"),
                                (72, 152.5, "Regional Offices", 10, "Times-Bold"), (480, 140, "Page 3", 10, "Times-Roman"),
                                (480, 152.5, "Draft", 10, "Times-Roman"), *_named_body(170, "Times-Roman", _BODY)])

        assert "# Annual Results of the Regional Offices" in text

    @pytest.mark.parametrize("size", [10, 14])
    def test_two_line_headings_of_two_columns_on_the_same_rows_stay_headings(self, size):
        column = ["The next step is to construct a model for", "the dynamics of the system as a whole and",
                  "the idea which goes back to Boltzmann is", "that any state develops into a higher one."]
        items = [(x, top + index * 12.5, line, 10, "Times-Roman") for x in (40, 320) for top in (80, 176)
                 for index, line in enumerate(column)]
        items += [(40, 140, "Methods and", size, "Times-Bold"), (40, 140 + size * 1.2, "Materials", size, "Times-Bold"),
                  (320, 140, "Results and", size, "Times-Bold"), (320, 140 + size * 1.2, "Discussion", size, "Times-Bold")]
        text = _named_font_pdf(items)

        assert "# Methods and Materials" in text and "# Results and Discussion" in text

    def test_a_two_line_heading_beside_the_other_column_stays_one_heading(self):
        """The lines of the right column beside the heading are prose, not values."""
        left = ["The next step is to build a model of the whole", "system and the idea which goes back to the past",
                "is that any state is likely to develop into one"]
        right = [f"line {index} of the right column holds several words" for index in range(10)]
        text = _named_font_pdf([*[(60, 80 + index * 12.5, line, 9, "Times-Roman") for index, line in enumerate(left)],
                                (60, 130, "Results Of The Study Set", 9, "Times-Bold"), (60, 142.5, "Over Two Lines", 9, "Times-Bold"),
                                *[(60, 160 + index * 12.5, line, 9, "Times-Roman") for index, line in enumerate(left)],
                                *[(320, 80 + index * 12.5, line, 9, "Times-Roman") for index, line in enumerate(right)]])

        assert "# Results Of The Study Set Over Two Lines" in text
