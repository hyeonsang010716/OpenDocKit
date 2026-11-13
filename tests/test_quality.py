"""Pins the conversion quality problems found during sample validation (samples/)."""

from datetime import date, datetime
from io import BytesIO

import pytest

from opendockit import convert
from opendockit.extractors import _pdf_ruled_tables
from opendockit.markdown import clean_text


def test_symbol_font_bullets_become_unicode_bullets():
    """Bullets in Word and LibreOffice PDFs are private-use-area characters (U+F0B7 etc.) of the Symbol and Wingdings fonts.
    Removing them made the list markers disappear."""
    text = " 항목\n 네모\n 화살표\n 체크\n 모르는 기호"

    result = clean_text(text)

    assert result == "• 항목\n▪ 네모\n➢ 화살표\n✓ 체크\n모르는 기호"


def _docx(build):
    from docx import Document

    document = Document()
    build(document)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_list_style_number_sets_nesting_level():
    """Word's 'List Bullet 2' style expresses the nesting level through the style name, not the level (ilvl)."""
    def build(document):
        document.add_paragraph("상위", style="List Bullet")
        document.add_paragraph("하위", style="List Bullet 2")
        document.add_paragraph("더 하위", style="List Bullet 3")
        document.add_paragraph("번호 하위", style="List Number 2")

    result = convert(_docx(build), "a.docx")

    assert result.text == "- 상위\n  - 하위\n    - 더 하위\n  1. 번호 하위"


def test_docx_nested_table_keeps_its_rows_and_columns():
    """It used to be flattened to '부서 / 인원<br>운영팀 / 3명'. The outer table is emitted as HTML with the inner table
    inside the cell."""
    def build(document):
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "담당"
        inner = table.cell(0, 1).add_table(rows=2, cols=2)
        for (row, col), value in {(0, 0): "부서", (0, 1): "인원", (1, 0): "운영팀", (1, 1): "3명"}.items():
            inner.cell(row, col).text = value

    result = convert(_docx(build), "a.docx")

    assert result.text == ("<table>\n<tr><th>담당</th><th><table><tr><th>부서</th><th>인원</th></tr>"
                           "<tr><td>운영팀</td><td>3명</td></tr></table></th></tr>\n</table>")


def _pdf(draw):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    def text(x, y, value, size=10.5):
        pdf.setFont("HYSMyeongJo-Medium", size)
        pdf.drawString(x, A4[1] - y, value)

    draw(text)
    pdf.save()
    return buffer.getvalue()


def test_pdf_heading_levels_follow_size_rank_and_skip_bullets():
    """Heading levels are assigned by the rank of the heading sizes within the document, not by an absolute ratio
    (22pt, 15pt → #, ##). A large-type line that starts with a bullet (a first-level slide bullet) is not a heading."""
    def draw(text):
        text(72, 80, "운영 현황 보고서", 22)
        text(72, 120, "1. 요약", 15)
        text(72, 145, "본문 문장입니다.")
        text(72, 165, "본문 둘째 문장입니다.")
        text(72, 200, "• 큰 글머리 항목", 15)
        text(72, 240, "2. 실적", 15)
        text(72, 265, "본문 셋째 문장입니다.")

    result = convert(_pdf(draw), "a.pdf")

    page = result.sections[0].text
    assert page.startswith("# 운영 현황 보고서\n\n## 1. 요약")
    assert "## 2. 실적" in page
    assert "• 큰 글머리 항목" in page and "# • 큰" not in page


def test_pdf_borderless_table_is_reconstructed():
    def draw(text):
        text(72, 80, "괘선 없는 표", 15)
        rows = [("모델", "개발사", "출시", "특징"), ("모델 A", "회사 가", "2026년 4월", "코드 작성"),
                ("모델 B", "회사 나", "2026년 6월", "긴 문맥")]
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                text(72 + col_index * 120, 120 + row_index * 22, value)
        text(72, 220, "표 아래 문단입니다.")

    result = convert(_pdf(draw), "a.pdf")

    page = result.sections[0].text
    assert page.startswith("# 괘선 없는 표"), "headings must be judged by the body font size measured before the table lines are taken out"
    assert ("| 모델 | 개발사 | 출시 | 특징 |\n| --- | --- | --- | --- |\n| 모델 A | 회사 가 | 2026년 4월 | 코드 작성 |\n"
            "| 모델 B | 회사 나 | 2026년 6월 | 긴 문맥 |") in page
    assert page.index("괘선 없는 표") < page.index("| 모델 |") < page.index("표 아래 문단입니다.")


def test_pdf_title_over_borderless_table_only_is_still_a_heading():
    """When a document has only a title and a table, measuring the body size after taking out the table lines made the
    title the reference size, so it was not detected as a heading."""
    def draw(text):
        text(72, 80, "괘선 없는 표", 15)
        for row_index, row in enumerate([("가", "나", "다"), ("1", "2", "3"), ("4", "5", "6")]):
            for col_index, value in enumerate(row):
                text(72 + col_index * 120, 120 + row_index * 22, value)

    result = convert(_pdf(draw), "a.pdf")

    assert result.sections[0].text.startswith("# 괘선 없는 표\n\n| 가 | 나 | 다 |")


def test_pdf_two_level_merged_header_is_flattened():
    """A two-level header common in Korean report PDFs ('1월', '2월' under '문의') is flattened into one row,
    '문의 1월 | 문의 2월'."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    top, height, columns = 700, 22, [72, 180, 260, 340, 420, 500]
    rows = [("부서", "문의", "", "해결률", ""), ("", "1월", "2월", "1월", "2월"), ("교육팀", "620", "700", "91%", "93%")]
    for index in range(len(rows) + 1):
        start = columns[1] if index == 1 else columns[0]
        pdf.line(start, top - index * height, columns[-1], top - index * height)
    for index, x in enumerate(columns):
        pdf.line(x, top - height if index in (2, 4) else top, x, top - height * len(rows))
    pdf.setFont("HYSMyeongJo-Medium", 10)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            if value:
                pdf.drawString(columns[col_index] + 6, top - (row_index + 1) * height + 7, value)
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert ("| 부서 | 문의 1월 | 문의 2월 | 해결률 1월 | 해결률 2월 |\n| --- | --- | --- | --- | --- |\n"
            "| 교육팀 | 620 | 700 | 91% | 93% |") in result.text


def test_pdf_table_without_outer_borders_keeps_its_first_and_last_columns():
    """Tables without left and right borders (horizontal lines across the full width, vertical lines only inside) are
    common in Korean reports. Only the columns between the vertical lines were detected as the table, so the first column
    (구분) and the last column (2025년) were scattered outside it and their values were left in the wrong places (the
    financial status table of a real document)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    top, height, columns = 700, 22, [72, 140, 200, 260, 320, 380]
    rows = [("구분", "2022년", "2023년", "2024년", "2025년"), ("매출", "10,442", "9,158", "18,071", "15,286"),
            ("영업이익", "719", "1,220", "4,113", "518")]
    for index in range(len(rows) + 1):
        pdf.line(columns[0], top - index * height, columns[-1], top - index * height)
    for x in columns[1:-1]:
        pdf.line(x, top, x, top - height * len(rows))
    pdf.setFont("HYSMyeongJo-Medium", 10)
    for row_index, row in enumerate(rows):
        for col_index, value in enumerate(row):
            pdf.drawString(columns[col_index] + 6, top - (row_index + 1) * height + 7, value)
    pdf.drawString(72, 560, "표 아래 문단입니다.")
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert result.text.endswith(
        "| 구분 | 2022년 | 2023년 | 2024년 | 2025년 |\n| --- | --- | --- | --- | --- |\n"
        "| 매출 | 10,442 | 9,158 | 18,071 | 15,286 |\n| 영업이익 | 719 | 1,220 | 4,113 | 518 |\n\n표 아래 문단입니다.")


def test_pdf_table_cell_keeps_spaces_between_words_in_small_fonts():
    """The tolerance for grouping the characters of a table cell into words was fixed at 3pt, so the word gap of a small
    font (10–11pt; 0.2 times the font size, about 2pt) was not seen as a space. Body lines were fine, but table cells alone
    were run together as "기반산업용비파괴검사솔루션개발" (some 60 places in 2 real documents)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(3):
        pdf.line(72, 700 - index * 24, 420, 700 - index * 24)
    for x in (72, 160, 420):
        pdf.line(x, 700, x, 652)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    pdf.drawString(80, 684, "구분")
    pdf.drawString(170, 684, "내용")
    pdf.drawString(80, 660, "목표")
    x = 170.0
    for word in ("산업용", "비파괴검사", "솔루션", "개발"):
        pdf.drawString(x, 660, word)
        x += len(word) * 10 + 2.0  # word gap 2pt = 0.2 times the font size, no space character
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert "| 목표 | 산업용 비파괴검사 솔루션 개발 |" in result.text


def test_pdf_table_cell_space_drawn_under_the_next_letter_does_not_split_the_word():
    """A document with tightened character spacing draws a space character and still draws the next character before the
    start of the space (a space with no advance). Laying the cell's characters out in x order put the space after the first
    character of the next word, giving '•새 로운', '측 정원리' (25 places in the tables of one manual)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(3):
        pdf.line(72, 700 - index * 24, 420, 700 - index * 24)
    for x in (72, 160, 420):
        pdf.line(x, 700, x, 652)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    pdf.drawString(80, 684, "구분")
    pdf.drawString(170, 684, "내용")
    pdf.drawString(80, 660, "방식")
    pdf.drawString(170, 660, "가)")
    pdf.drawString(190, 660, " ")  # draw a space, and
    pdf.drawString(189.67, 660, "새로운")  # the next character 0.33pt before the start of the space
    pdf.drawString(222, 660, "제품")
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert "| 방식 | 가) 새로운 제품 |" in result.text


def test_pdf_table_cell_space_overlapped_by_a_letter_that_still_advances_is_kept():
    """In a Latin font whose glyph boxes are wider than the advance, every character overlaps the next one. The space too is
    covered by more than half by the next character, but the next character starts after the start of the space (a space
    that advances). Telling no-advance spaces apart by the overlap width dropped this space, so 'CI / CD' became 'CI /CD'."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    space = pdfmetrics.stringWidth(" ", "HYSMyeongJo-Medium", 10)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(3):
        pdf.line(72, 700 - index * 24, 420, 700 - index * 24)
    for x in (72, 160, 420):
        pdf.line(x, 700, x, 652)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    pdf.drawString(80, 684, "구분")
    pdf.drawString(170, 684, "내용")
    pdf.drawString(80, 660, "과정")
    pdf.drawString(170, 660, "CI /")
    end = 170 + pdfmetrics.stringWidth("CI /", "HYSMyeongJo-Medium", 10)
    pdf.drawString(end, 660, " ")
    pdf.drawString(end + space * 0.3, 660, "CD")  # covers 70% of the space width but starts after the start of the space
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert "| 과정 | CI / CD |" in result.text


def _dotted_border_table_pdf(*, open_bottom: bool = False) -> bytes:
    """Table whose first column is merged vertically and whose second-column cell borders (vertical line, row dividers) are
    dotted. A dotted line is drawn as hundreds of short 0.4pt lines."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    top, height, columns = 700, 22, [72, 170, 330, 400]
    names = ["가나방송", "다라방송", "마바방송", "사아방송"]
    bottom = top - height * (len(names) + 1)

    def dotted(x0: float, y0: float, x1: float, y1: float) -> None:
        length = max(x1 - x0, y1 - y0)
        for step in range(int(length / 0.85)):
            offset = step * 0.85
            pdf.line(x0 + (offset if x1 > x0 else 0), y0 + (offset if y1 > y0 else 0),
                     x0 + (offset + 0.4 if x1 > x0 else 0), y0 + (offset + 0.4 if y1 > y0 else 0))

    pdf.line(columns[0], top, columns[-1], top)
    pdf.line(columns[0], top, columns[0], bottom)
    pdf.line(columns[-1], top, columns[-1], bottom)
    # A table that continues on the next page has no bottom border. The cells are not closed there.
    pdf.line(columns[2] if open_bottom else columns[0], bottom, columns[-1], bottom)
    pdf.line(columns[0], top - height, columns[-1], top - height)
    pdf.line(columns[2], top, columns[2], bottom)
    pdf.line(columns[1], top, columns[1], top - height)
    dotted(columns[1], bottom, columns[1], top - height)
    for index in range(2, len(names) + 1):
        dotted(columns[1], top - index * height, columns[2], top - index * height)
        pdf.line(columns[2], top - index * height, columns[3], top - index * height)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    for col, value in enumerate(("계열", "방송사업자명", "점유율")):
        pdf.drawString(columns[col] + 6, top - height + 7, value)
    pdf.drawString(columns[0] + 6, (top - height + bottom) / 2, "운영방송사")
    for index, name in enumerate(names, start=2):
        pdf.drawString(columns[1] + 6, top - index * height + 7, name)
        pdf.drawString(columns[2] + 6, top - index * height + 7, f"0.{index}1")
    pdf.save()
    return buffer.getvalue()


def test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell():
    """Discarding dotted cell borders merged the cells and broke the row correspondence between names and values (the
    broadcaster table of a government press release: 23 names gathered in one cell). The vertically merged cell (계열)
    repeats its value on every row, as in the other formats."""
    result = convert(_dotted_border_table_pdf(), "a.pdf")

    assert ("| 계열 | 방송사업자명 | 점유율 |\n| --- | --- | --- |\n| 운영방송사 | 가나방송 | 0.21 |\n| 운영방송사 | 다라방송 | 0.31 |\n"
            "| 운영방송사 | 마바방송 | 0.41 |\n| 운영방송사 | 사아방송 | 0.51 |") in result.text


def test_pdf_text_inside_a_table_area_is_never_dropped(monkeypatch):
    """Characters inside a table area are taken out of the body flow, but characters where table detection built no cell
    were held by no cell and silently disappeared (7 rows of "딜라이브…" in the same table). When the cells hold clearly fewer
    characters than the area contains, no table is made and the text stays in the body."""

    monkeypatch.setattr(_pdf_ruled_tables, "_TABLE_SETTINGS", {})  # reproduce the state in which dotted lines are not seen (pdfplumber defaults)

    result = convert(_dotted_border_table_pdf(open_bottom=True), "a.pdf")

    for name in ("가나방송", "다라방송", "마바방송", "사아방송", "운영방송사"):
        assert name in result.text


def test_pdf_two_short_columns_of_prose_are_not_a_table():
    """Only two pieces at the same height (two-column layout) or long lines are not taken for a table."""
    def draw(text):
        for index in range(4):
            text(72, 100 + index * 15, f"왼쪽 단의 {index + 1}번째 줄 본문이 이어집니다")
            text(320, 100 + index * 15, f"오른쪽 단의 {index + 1}번째 줄 본문이 이어집니다")

    result = convert(_pdf(draw), "a.pdf")

    assert "|" not in result.text


def _draw_words(text, x, y, words, size=16.0, advance=0.865, space=0.25, space_char=False):
    """Place each word separately. The actual advance width (advance) is narrower than the character width the font
    declares (1em), so the next word overlaps the one before."""
    for word in words:
        for piece in word.split("|"):  # "개발|하여": placed separately because the weight changes, but one word (no space between)
            text(x, y, piece + (" " if space_char and piece is word else ""), size)
            x += len(piece) * size * advance
        x += size * space


def test_pdf_words_placed_separately_without_space_characters_get_their_spaces_back():
    """Some pages of PDFs exported by PowerPoint place a separate text run per word and put in no space characters. The
    width of the fallback font is wider than the real one, so the words overlap and word spacing could not be found from
    the gaps ("자동차공장냉매주입라인을모사하는", some 10 pages in 2 real documents)."""
    def draw(text):
        _draw_words(text, 72, 100, ["자동차", "공장", "냉매", "주입", "라인을", "모사하는", "Off-line"])
        _draw_words(text, 72, 130, ["혼합냉매", "주입", "장비를", "개발|하여", "상용화", "추진", "검증|해"])
        _draw_words(text, 72, 160, ["최근", "환경", "규제", "및", "성능", "요건", "강화에", "따라"])

    result = convert(_pdf(draw), "a.pdf")

    page = result.sections[0].text
    assert "자동차 공장 냉매 주입 라인을 모사하는 Off-line" in page
    assert "혼합냉매 주입 장비를 개발하여 상용화 추진 검증해" in page, (
        "there is no space between the pieces of a word placed separately because the font changes")
    assert "최근 환경 규제 및 성능 요건 강화에 따라" in page


def test_pdf_overlapping_runs_keep_reading_order():
    """Sorting overlapping glyph boxes by x coordinate put the last character of one piece inside the next piece (in a table
    cell, "한국에너지기술연구원(KIER) 26.5% 지분" → "연구(K원IER) 26.5%지 분"). The order is corrected even for a font that has
    space characters."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(3):
        pdf.line(72, 700 - index * 24, 400, 700 - index * 24)
    for x in (72, 160, 400):
        pdf.line(x, 700, x, 652)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    # The page is corrected only if its body text is 'separately placed text' (evidence that the overlap comes from the font width).
    for row, words in enumerate((["수혜", "기업의", "전문", "시험", "역량을", "강화하고"], ["상생", "협력", "기반의", "구조를", "마련한다"],
                                 ["배터리", "성능", "평가", "장비를", "구축하여", "검증한다"])):
        x = 72.0
        for word in words:
            pdf.drawString(x, 780 - row * 20, word)
            x += len(word) * 8.65 + 2.5
    pdf.drawString(80, 684, "기업명")
    pdf.drawString(170, 684, "이트라이언")
    pdf.drawString(80, 660, "특징")
    x = 170.0
    for piece, width in (("에너지기술연구원", 8 * 8.65), ("(KIER) 26.5% ", 62.0), ("지분", 2 * 8.65), (" ", 2.5), ("보유", 0)):
        pdf.drawString(x, 660, piece)  # the start of each piece advances by the actual width only, narrower than the width the font declares (10pt)
        x += width
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert "| 특징 | 에너지기술연구원(KIER) 26.5% 지분 보유 |" in result.text


def test_pdf_cover_page_borrows_spacing_evidence_from_later_pages():
    """A page with little text, such as a cover or a table of contents, cannot gather the evidence of 'separately placed
    text' on its own. It borrows the character width confirmed on later pages of the same document. The cover comes first,
    so it is read again after the later pages have been read ("차세대냉매주입테스트장비구축지원")."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)

    def text(x, y, value, size=10.5):
        pdf.setFont("HYSMyeongJo-Medium", size)
        pdf.drawString(x, A4[1] - y, value)

    _draw_words(text, 72, 300, ["차세대", "냉매", "주입", "장비", "구축", "지원"], size=24.0)
    pdf.showPage()
    _draw_words(text, 72, 100, ["자동차", "공장", "냉매", "주입", "라인을", "모사하는", "장비를"])
    _draw_words(text, 72, 130, ["혼합냉매", "주입", "장비를", "개발하여", "상용화", "추진", "중이다"])
    _draw_words(text, 72, 160, ["최근", "환경", "규제", "및", "성능", "요건", "강화에", "따라"])
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    assert "차세대 냉매 주입 장비 구축 지원" in result.sections[0].text


def test_pdf_justified_text_with_varying_letter_spacing_is_left_alone():
    """A PDF exported from Hangul (HWP) stretches or shrinks the character spacing a little on each line because of
    justification, and marks word spacing by position only, with no space characters. Word-spacing recovery mistook this
    for 'separately placed words' and put a space after every character ("새 만 금 기 본 계 획", a government press release
    PDF)."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    lines = [(0.7, "새만금개발청은 군산새만금컨벤션센터에서 공청회를 개최했다고 밝혔다"),
             (-0.9, "이번 공청회는 지역주민과 전문가의 의견을 폭넓게 듣기위해 마련됐다"),
             (0.0, "기본계획의 변경을 추진중이며 핵심방향은 실행력강화와 전략산업육성이다"),
             (-0.6, "새만금위원회 심의를 거쳐 기본계획을 확정하고 단계별로 추진할 계획이다"),
             (0.4, "관계부처와 전문가 그리고 시민단체와 지역주민 등이 참석한 가운데 열렸다")]
    for index, (char_space, line) in enumerate(lines):
        text = pdf.beginText(72, 760 - index * 24)
        text.setFont("HYSMyeongJo-Medium", 12)
        text.setCharSpace(char_space)
        x = 72.0
        for word in line.split():
            text.setTextOrigin(x, 760 - index * 24)
            text.textOut(word)  # words are separated by position only, with no space character
            x += len(word) * (12 + char_space) + 5
        pdf.drawText(text)
    pdf.save()

    result = convert(buffer.getvalue(), "a.pdf")

    for _, line in lines:
        assert line in result.text


def test_pdf_with_tight_letter_spacing_does_not_get_extra_spaces():
    """In a normal document with tightened character spacing (every character overlaps) the overlaps are not word
    boundaries. If there are space characters, or the overlap is between all characters, the text is left alone."""
    def draw(text):
        _draw_words(text, 72, 100, ["자동차", "공장", "냉매", "주입", "라인을", "모사하는"], space_char=True, space=0.4)
        for index, char in enumerate("자간을좁힌문장은글자마다조금씩겹쳐서놓인다그래도한낱말이다"):
            text(72 + index * 13.8, 140, char, 16.0)

    result = convert(_pdf(draw), "a.pdf")

    page = result.sections[0].text
    assert "자동차 공장 냉매 주입 라인을 모사하는" in page
    assert "자간을좁힌문장은글자마다조금씩겹쳐서놓인다그래도한낱말이다" in page


class TestSpreadsheetNumberFormats:
    """Values are carried over as shown on screen in Excel (with the number format applied). If 108.2% goes out as 1.082,
    the LLM misreads the figure."""

    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (1.082, "0.0%", "108.2%"),
            (0.5, "0%", "50%"),
            (125000000, "#,##0", "125,000,000"),
            (1234.5, "#,##0.00", "1,234.50"),
            (3.14159, "0.00", "3.14"),
            (1200, '"₩"#,##0', "₩1,200"),
            (1200, "[$₩-412]#,##0", "₩1,200"),
            (1200.5, "$#,##0.00", "$1,200.50"),
            (5000, '#,##0"원"', "5,000원"),
            # The default "Accounting" format of Korean Excel. `_x` is a space as wide as x and `*x` fills with x, so neither may
            # appear in the value.
            (1200, '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-', "1,200"),
            (1200, '_(* #,##0_);_(* \\(#,##0\\);_(* "-"_);_(@_)', "1,200"),
            (1234.5, "_-₩* #,##0.00_-;\\-₩* #,##0.00_-", "₩1,234.50"),
            (1200, "#,##0_ ", "1,200"),
            (0.5, "# ?/?", "0.5"),  # fraction formats are not emulated; the raw value is used
            # A comma after the digit placeholders scales the display by thousands. Ignoring it makes the values of a
            # "단위: 천원" (unit: thousand won) table read 1,000 times too large.
            (1234567, "#,##0,", "1,235"),
            (1234567, '0.0,,"백만"', "1.2백만"),
            # 0 pads to the digit count (employee numbers, postal codes).
            (1234, "000000", "001234"),
            # A % escaped by quotes or a backslash is just a character, not a percentage. Multiplying by 100 turned "2.5%p"
            # into "250.0%%p".
            (2.5, '0.0"%p"', "2.5%p"),
            (2.5, "0.0\\%", "2.5%"),
            (0.025, '0.0%" 증가"', "2.5% 증가"),
            # Characters between digit placeholders (formats shaped like phone numbers, resident registration numbers, dates)
            # are inserted while the placeholders are filled from the right.
            (1012345678, "0##\\-####\\-####", "010-1234-5678"),
            (12345, "000\\-000", "012-345"),
            (20260331, "0000\\-00\\-00", "2026-03-31"),
            (5, "0\\/0", "0/5"),
            (-12345, '000"-"000', "-012-345"),
            # For conditional formats the choice of section is not emulated; the raw value is used. Using only the first section
            # broke phone numbers with the 02 area code.
            (212345678, "[<=9999999]###\\-####;\\(0##\\)\\ ###\\-####", "212345678"),
            (1234, '[>=1000]#,##0,"K";0', "1234"),
            (-5, "000", "-005"),
            (7.5, "00.0", "07.5"),
            (-42, "#,##0;[Red]-#,##0", "-42"),
            (0.1 + 0.2, "General", "0.3"),
            (12, "@", "12"),
            (1.5e-7, "0.00E+00", "1.5e-07"),
            (datetime(2026, 3, 30, 18, 0), "yyyy-mm-dd", "2026-03-30"),
            (datetime(2026, 3, 30, 18, 0), "yyyy-mm-dd hh:mm", "2026-03-30 18:00"),
            (datetime(2026, 3, 30, 18, 0), None, "2026-03-30 18:00:00"),
        ],
    )
    def test_formats_numbers_like_excel(self, value, number_format, expected):
        from opendockit.extractors._sheet_format import format_cell

        assert format_cell(value, number_format) == expected

    def test_xlsx_uses_cell_number_formats(self):
        import openpyxl

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["지점", "매출", "달성률", "마감"])
        sheet.append(["강남", 125000000, 1.082, date(2026, 3, 31)])
        sheet["B2"].number_format = "#,##0"
        sheet["C2"].number_format = "0.0%"
        sheet["D2"].number_format = "yyyy-mm-dd"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 강남 | 125,000,000 | 108.2% | 2026-03-31 |" in result.text


def test_html_multi_row_header_is_flattened_and_nav_removed():
    html = """<nav>홈 &gt; 안내</nav><aside>광고</aside><table>
    <thead><tr><th rowspan="2">과정</th><th colspan="2">요금</th></tr><tr><th>정가</th><th>할인가</th></tr></thead>
    <tbody><tr><td>파이썬</td><td>300</td><td>240</td></tr></tbody></table>"""

    result = convert(html.encode(), "a.html")

    assert result.text == "| 과정 | 요금 정가 | 요금 할인가 |\n| --- | --- | --- |\n| 파이썬 | 300 | 240 |"


def test_html_nested_list_wrapped_in_a_block_keeps_its_items_on_separate_lines():
    """When a nested list inside an item is wrapped in a div it is not a direct child, so it was not seen as a nested list
    and was merged into the item text as the single line "- 하위 1 - 하위 2"."""
    html = "<ul><li>상위<div><ul><li>하위 1</li><li>하위 2</li></ul></div></li><li>둘째</li></ul>"

    result = convert(html.encode(), "a.html")

    assert result.text == "- 상위\n  - 하위 1\n  - 하위 2\n- 둘째"


def test_html_empty_header_row_does_not_swallow_the_first_data_row():
    """An empty tr inside thead was counted as a header row too, but empty rows are left out of the table grid, so the first
    data row was cut off along with the header rows when they were taken off."""
    html = ("<table><thead><tr><th>A</th><th>B</th></tr><tr></tr></thead>"
            "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>")

    result = convert(html.encode(), "a.html")

    assert result.text == "| A | B |\n| --- | --- |\n| 1 | 2 |\n| 3 | 4 |"


def _pdf_canvas(draw):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("HYSMyeongJo-Medium", 10)
    draw(pdf)
    pdf.save()
    return buffer.getvalue()


def test_pdf_open_sided_table_with_two_level_header_is_kept_whole():
    """With a two-level header ('상반기', '하반기' under '2025년') in a table without left and right borders, the inner
    vertical lines have two different heights, so the table was found twice. The whole table was discarded because it
    contained the lower partial table, and '구분', '2025년', '비고' were scattered outside the table."""
    rows = [("구분", "2025년", "", "비고"), ("", "상반기", "하반기", ""), ("매출", "120", "150", "증가"),
            ("비용", "80", "90", "유지")]
    columns, top, height = [72, 172, 262, 352, 452], 700, 22

    def draw(pdf):
        for index in (0, 2, 3, 4):
            pdf.line(columns[0], top - index * height, columns[-1], top - index * height)
        pdf.line(columns[1], top - height, columns[3], top - height)
        for x in (columns[1], columns[3]):
            pdf.line(x, top, x, top - height * len(rows))
        pdf.line(columns[2], top - height, columns[2], top - height * len(rows))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                if value:
                    pdf.drawString(columns[col_index] + 5, top - (row_index + 1) * height + 7, value)

    result = convert(_pdf_canvas(draw), "a.pdf")

    assert ("| 구분 | 2025년 상반기 | 2025년 하반기 | 비고 |\n| --- | --- | --- | --- |\n"
            "| 매출 | 120 | 150 | 증가 |\n| 비용 | 80 | 90 | 유지 |") in result.text


def test_pdf_one_level_header_spanning_two_columns_keeps_the_first_data_row():
    """In a table with a one-level header whose '연락처' header cell spans two columns, the first data row below was taken
    for the second level of a two-level header and appended to the header ('이름 홍길동 | 연락처 010-…'). A row that has text
    of its own in a column that is not merged is data, not a sub-header."""
    rows = [("이름", "연락처", ""), ("홍길동", "010-1111-2222", "hong@a.kr"), ("김철수", "010-3333-4444", "kim@a.kr")]
    columns, top, height = [72, 172, 302, 452], 700, 20

    def draw(pdf):
        for index in range(len(rows) + 1):
            pdf.line(columns[0], top - index * height, columns[-1], top - index * height)
        for index, x in enumerate(columns):
            pdf.line(x, top - height if index == 2 else top, x, top - height * len(rows))
        for row_index, row in enumerate(rows):
            for col_index, value in enumerate(row):
                if value:
                    pdf.drawString(columns[col_index] + 4, top - (row_index + 1) * height + 6, value)

    result = convert(_pdf_canvas(draw), "a.pdf")

    assert ("| 이름 | 연락처 | |\n| --- | --- | --- |\n| 홍길동 | 010-1111-2222 | hong@a.kr |\n"
            "| 김철수 | 010-3333-4444 | kim@a.kr |") in result.text


def test_pdf_text_inside_form_xobjects_stays_where_it_is_drawn():
    """A flattened form puts the entered values in Form XObjects. pdfminer places the text inside an XObject after the
    other text of the page, so all the labels came first and the values were gathered at the end of the page. The text is
    moved to where it is drawn (after the label on the same line)."""
    fields = [("성명", "홍길동"), ("생년월일", "1990-01-02"), ("주소", "서울시 종로구")]

    def draw(pdf):
        pdf.drawString(72, 780, "입사 지원서")
        for index, (label, value) in enumerate(fields):
            y = 720 - index * 40
            pdf.drawString(72, y, label)
            pdf.beginForm(f"field{index}")
            pdf.setFont("HYSMyeongJo-Medium", 10)
            pdf.drawString(250, y, value)
            pdf.endForm()
            pdf.doForm(f"field{index}")
        pdf.drawString(72, 560, "위 내용이 사실임을 확인합니다.")

    result = convert(_pdf_canvas(draw), "a.pdf")

    words = ["입사 지원서", "성명", "홍길동", "생년월일", "1990-01-02", "주소", "서울시 종로구", "위 내용이 사실임을 확인합니다."]
    positions = [result.text.index(word) for word in words]
    assert positions == sorted(positions)


def test_pdf_side_by_side_form_xobjects_are_not_interleaved():
    """The position of an XObject block was decided against the lines of another block already inserted, so the right
    column, which starts at the same height, cut in right after the first line of the left column. The position is decided
    against text drawn directly on the page only."""
    def draw(pdf):
        pdf.drawString(72, 800, "머리글")
        for name, x in (("left", 72), ("right", 320)):
            pdf.beginForm(name)
            pdf.setFont("HYSMyeongJo-Medium", 10)
            for index in range(3):
                pdf.drawString(x, 700 - index * 14, f"{name} {index + 1}번째 줄")
            pdf.endForm()
            pdf.doForm(name)
        pdf.drawString(72, 60, "바닥글")

    result = convert(_pdf_canvas(draw), "a.pdf")

    assert "left 1번째 줄\nleft 2번째 줄\nleft 3번째 줄" in result.text
    assert "right 1번째 줄\nright 2번째 줄\nright 3번째 줄" in result.text
    assert result.text.index("머리글") < result.text.index("left 1") < result.text.index("바닥글")
    assert result.text.index("right 3") < result.text.index("바닥글"), (
        "it goes before the text below even when the page has no text of its own in the same column")
