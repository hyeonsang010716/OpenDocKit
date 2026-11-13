"""PDF conversion: reading order, tables, paragraphs, headings, removal of repeated header lines."""

import pytest

from opendockit import ConversionError, ConvertOptions, SectionKind, convert
from tests import builder


@pytest.fixture(scope="module")
def result():
    return convert(builder.pdf_bytes(pages=3, two_columns=True), "보고서.pdf")


class TestPdf:
    def test_one_section_per_page_with_page_markers(self, result):
        assert [section.kind for section in result.sections] == [SectionKind.PAGE] * 3
        assert result.text.startswith("<!-- Page 1 -->")
        assert result.metadata["page_count"] == "3"

    def test_removes_header_and_page_numbers_repeated_on_every_page(self, result):
        assert "사내 보고서 머리글" not in result.text
        assert "- 1 -" not in result.text

    def test_marks_larger_font_as_heading(self, result):
        assert "# 제1장 개요" in result.sections[0].text

    def test_groups_lines_into_paragraphs_by_line_gap(self, result):
        """Line breaks inside a paragraph are not joined with a space — so that Korean wrapped per character is not split mid-word."""
        assert "첫 문단의 첫 줄입니다\n첫 문단의 둘째 줄입니다\n\n둘째 문단입니다" in result.sections[0].text

    def test_extracts_ruled_table_without_duplicating_its_text(self, result):
        page = result.sections[0].text
        assert "| 품목 | 수량 | 단가 |\n| --- | --- | --- |\n| 연필 | 3 | 500 |" in page
        assert page.count("연필") == 1

    def test_reads_two_column_layout_column_by_column(self, result):
        """Sorting by the y coordinate alone interleaves the lines of the left and right columns one by one."""
        page = result.sections[1].text
        last_left = page.index("왼쪽 단 5번째")
        first_right = page.index("오른쪽 단 1번째")
        assert last_left < first_right

    def test_max_pages_truncates_with_warning(self):
        result = convert(builder.pdf_bytes(pages=3), options=ConvertOptions(max_pages=1))

        assert len(result.sections) == 1
        assert any("only the first 1 were converted" in warning for warning in result.warnings)

    def test_stops_reading_pages_once_the_character_limit_is_exceeded(self, monkeypatch):
        """PDF page analysis is slow. Pages after the one where the limit is exceeded are not read."""
        from opendockit.extractors import pdf as pdf_module

        original, calls = pdf_module._read_page, []

        def counting(page, *args):
            calls.append(page.page_number)
            return original(page, *args)

        monkeypatch.setattr(pdf_module, "_read_page", counting)

        result = convert(builder.pdf_bytes(pages=6), "a.pdf", ConvertOptions(max_chars=60))

        assert result.exceeded
        assert len(calls) < 6

    def test_does_not_render_images_when_already_exceeded(self):
        """Rendering the pictures of a document already over the limit is a waste (it leads on to image decoding and vision model calls)."""
        result = convert(builder.pdf_with_images(), "a.pdf", ConvertOptions(max_chars=5))

        assert result.exceeded
        assert result.images == []

    def test_page_markers_can_be_disabled(self):
        result = convert(builder.pdf_bytes(pages=1), options=ConvertOptions(page_markers=False))

        assert "<!--" not in result.text


class TestPdfiumFallback:
    """A PDF that pdfminer cannot read is read with pdfium instead.

    Real case: on a certificate PDF AES-encrypted with an empty password, pdfminer failed with 'Invalid IV size (0)' and pdfium read it.
    """

    def test_page_that_pdfminer_cannot_parse_falls_back_to_pdfium_text(self, monkeypatch):
        from pdfplumber.utils.exceptions import PdfminerException

        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def flaky(page, *args):
            if page.page_number == 2:
                raise PdfminerException(ValueError("Invalid IV size (0) for CBC."))
            return original(page, *args)

        monkeypatch.setattr(pdf_module, "_read_page", flaky)

        result = convert(builder.pdf_bytes(pages=3), "cert.pdf")

        assert "제2장 개요" in result.sections[1].text
        assert "첫 문단의 첫 줄입니다" in result.sections[1].text
        assert any("simple extraction" in warning and "2" in warning for warning in result.warnings)

    def test_page_where_pdfminer_finds_no_text_uses_pdfium_text(self, monkeypatch):
        """Real case: pdfium read an 18-page PDF in which pdfminer silently failed to resolve the fonts and saw 0 characters."""
        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def textless(page, *args):
            read = original(page, *args)
            read.lines, read.tables = [], []
            return read

        monkeypatch.setattr(pdf_module, "_read_page", textless)

        result = convert(builder.pdf_bytes(pages=1), "a.pdf")

        assert "첫 문단의 첫 줄입니다" in result.sections[0].text

    def test_time_budget_reads_remaining_pages_with_pdfium(self):
        """A PDF with many vector shapes took over 2 s per page with pdfminer (184 s for 102 pages). Once the budget is exceeded the rest
        is read with fast simple extraction."""
        result = convert(builder.pdf_bytes(pages=3), "a.pdf", ConvertOptions(pdf_time_budget=0.0))

        assert "둘째 문단입니다" in result.sections[2].text
        assert any("time budget" in warning and "remaining 3 page(s)" in warning for warning in result.warnings)
        assert not any("failed" in warning for warning in result.warnings)

    def test_document_pdfminer_cannot_open_stops_reading_pages_once_the_character_limit_is_exceeded(self, monkeypatch):
        """When pdfminer could not open a 200-page document with 300,000 characters per page, the pdfium fallback path read every page
        with no character limit (101 pages in 122 s). The normal path stops at page 2."""
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException

        from opendockit.extractors import pdf as pdf_module

        def broken(*args, **kwargs):
            raise PdfminerException(ValueError("broken xref"))

        opened = []
        original = pdf_module._pdfium_page
        monkeypatch.setattr(pdfplumber, "open", broken)
        monkeypatch.setattr(pdf_module, "_pdfium_page", lambda document, index: opened.append(index) or original(document, index))

        result = convert(builder.pdf_bytes(pages=20), "a.pdf", ConvertOptions(max_chars=30))

        assert result.exceeded
        assert len(opened) < 3

    def test_document_pdfminer_cannot_open_falls_back_to_pdfium(self, monkeypatch):
        import pdfplumber
        from pdfplumber.utils.exceptions import PdfminerException

        def broken(*args, **kwargs):
            raise PdfminerException(ValueError("broken xref"))

        monkeypatch.setattr(pdfplumber, "open", broken)

        result = convert(builder.pdf_bytes(pages=2), "a.pdf")

        assert len(result.sections) == 2
        assert "둘째 문단입니다" in result.sections[0].text
        assert any("simple extraction" in warning for warning in result.warnings)

    @pytest.mark.parametrize("attribute", [b"/CropBox /Foo", b"/Rotate /Foo", b"/CropBox [0 0 595]", b"/MediaBox 8 0 R"])
    def test_document_with_a_malformed_page_attribute_is_read_with_pdfium(self, attribute):
        """When a page attribute of one page is malformed (a name, a box of 3 numbers, a MediaBox pointing at itself), pdfplumber raised
        TypeError/IndexError while building the page list, and the whole document, which pdfium reads, was rejected as a 'corrupted
        document'."""
        body = b"BT /F1 12 Tf 72 700 Td (Page %d body text) Tj ET"
        font = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R %s >>" % font,
                         4: b"<< /Length 47 >>\nstream\n%s\nendstream" % (body % 1),
                         5: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] %s /Contents 6 0 R %s >>" % (attribute, font),
                         6: b"<< /Length 47 >>\nstream\n%s\nendstream" % (body % 2), 8: b"8 0 R"})

        result = convert(data, "a.pdf")

        assert [section.text for section in result.sections] == ["Page 1 body text", "Page 2 body text"]
        assert any("simple extraction" in warning for warning in result.warnings)

    def test_document_whose_cross_reference_offsets_are_all_wrong_is_read_with_pdfium(self):
        """When every offset in the cross-reference table is off (startxref is right), pdfplumber returned 0 pages without an exception
        and the result was empty. pdfium finds the objects again and reads the 3 pages."""
        import re

        shifted = re.sub(rb"(\d{10})( 00000 n)", lambda match: b"%010d" % (int(match.group(1)) + 7) + match.group(2),
                         builder.pdf_bytes(pages=3))

        result = convert(shifted, "a.pdf")

        assert len(result.sections) == 3
        assert "둘째 문단입니다" in result.sections[2].text
        assert any("simple extraction" in warning for warning in result.warnings)


def test_password_protected_pdf_is_rejected_as_encrypted():
    """Even with the pdfium fallback path, a password-protected document must be reported as 'encrypted', not 'corrupted'."""
    from io import BytesIO

    from reportlab.pdfgen import canvas

    from opendockit import EncryptedDocumentError

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, encrypt="secret")
    pdf.drawString(72, 700, "hidden")
    pdf.save()

    with pytest.raises(EncryptedDocumentError):
        convert(buffer.getvalue(), "locked.pdf")


def test_concurrent_pdf_conversion_in_threads_does_not_crash():
    """pdfium is not thread-safe. Converting several PDFs at once in aconvert's default executor (a thread pool) killed the process
    with SIGTRAP. Run in a subprocess so that the crash does not take the test runner down with it."""
    import subprocess
    import sys

    script = (
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from opendockit import convert\n"
        "from tests import builder\n"
        "data = builder.pdf_with_images()\n"
        "with ThreadPoolExecutor(8) as pool:\n"
        "    results = list(pool.map(lambda _: convert(data, 'a.pdf'), range(24)))\n"
        "assert all(len(result.images) == 2 for result in results)\n"
        "print('ok')\n"
    )

    completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=300)

    assert completed.returncode == 0, completed.stderr[-500:]
    assert completed.stdout.strip() == "ok"


@pytest.mark.timing
def test_page_of_thousands_of_scattered_text_pieces_is_read_without_box_grouping():
    """pdfminer's text-box grouping (boxes_flow) uses time and memory quadratic in the number of text boxes, so a single page of 4,700
    scattered characters used 54 s and 3.2 GB (scatter-plot labels, drawings). A page with very many text pieces is read without
    grouping, with a warning."""
    from io import BytesIO
    import random
    import time

    from reportlab.pdfgen import canvas

    rng = random.Random(1)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(1200, 1600))
    pdf.setFont("Helvetica", 6)
    for _ in range(2_500):
        pdf.drawString(rng.uniform(10, 1180), rng.uniform(40, 1590), "x")
    pdf.setFont("Helvetica", 10)
    pdf.drawString(20, 20, "caption of the scatter plot")
    pdf.save()

    started = time.perf_counter()
    result = convert(buffer.getvalue(), "a.pdf")

    assert time.perf_counter() - started < 5, "before the fix this took 13 s and 1 GB"
    assert "caption of the scatter plot" in result.text
    assert any("reading-order" in warning for warning in result.warnings)


def _korean_pdf(draw, pages: int = 1, encrypt: object = None) -> bytes:
    """A PDF whose A4 pages are drawn with `draw(canvas, page index from 0)`. The font is a Korean CID font (HYSMyeongJo-Medium).
    `encrypt` is reportlab's `StandardEncryption` (None for no encryption)."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4, encrypt=encrypt)
    for index in range(pages):
        pdf.setFont("HYSMyeongJo-Medium", 10)
        draw(pdf, index)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


_FONT = "HYSMyeongJo-Medium"


class TestRepeatedMarginsAndBorderlessTables:
    def test_repeated_margin_removal_keeps_rows_of_a_borderless_table_spanning_pages(self):
        """In a borderless table spanning several pages the header row ('성명|지급액|지급일') repeats on every page, and the amount and
        date of the bottom row are the same on every page once the digits are removed. Looking for repeats piece by piece erased the
        header row and left only the name in the last row."""
        names = ["김민수", "이영희", "박철수", "최지우", "정하늘", "강바다", "조은비"]

        def draw(pdf, index):
            y = 742.0
            pdf.drawString(72, y, "성명")
            pdf.drawString(250, y, "지급액")
            pdf.drawString(400, y, "지급일")
            for row in range(42):
                y -= 16
                pdf.drawString(72, y, names[(index * 3 + row) % len(names)] + str(row))
                pdf.drawRightString(300, y, f"{index + 1},{row:03d},000")
                pdf.drawString(400, y, f"2025-0{index + 1}-{row % 28 + 1:02d}")

        result = convert(_korean_pdf(draw, pages=4), "a.pdf")

        for section in result.sections:
            assert "| 성명 | 지급액 | 지급일 |" in section.text
        last = names[(3 * 3 + 41) % len(names)] + "41"
        assert f"| {last} | 4,041,000 | 2025-04-14 |" in result.sections[3].text

    def test_book_footers_alternating_by_page_side_inside_the_crop_box_are_removed(self):
        """A book PDF for print has different footers on even and odd pages ('2 자유 문화' / '해적질 3') and none on the first page of a
        chapter, so neither footer reaches half of all pages. The footers are in the margin inside the trim line (CropBox), which was
        outside the margin measured on the paper (MediaBox). With the margin measured on the visible area, a line repeated on at least
        half of the pages of one parity is removed ('# FREE CULTURE' on 137 of the 352 pages of a real book PDF)."""
        def draw(pdf, index):
            pdf.drawString(72, 700, f"{index + 1}쪽 본문 첫 줄입니다.")
            pdf.drawString(72, 400, f"{index + 1}쪽 본문 가운데 줄입니다.")
            if index in (0, 5):
                return  # first page of a chapter
            pdf.drawString(72, 122, f"{index + 1} 자유 문화" if index % 2 else f"해적질 {index + 1}")

        data = _korean_pdf(draw, pages=10).replace(b"/MediaBox [ ", b"/CropBox [ 36 60 559 800 ] /MediaBox [ ")

        result = convert(data, "a.pdf")

        assert "자유 문화" not in result.text
        assert "해적질" not in result.text
        assert result.text.count("본문 첫 줄입니다.") == 10
        assert result.text.count("본문 가운데 줄입니다.") == 10

    def test_batched_four_page_forms_keep_their_titles_and_signature_lines(self):
        """In a PDF that binds 3 copies of a 4-page contract, the title (page 1) is always on an odd page and the signature block
        (page 4) always on an even page, so with a threshold of half of one parity they were removed as repeated lines (3/12 overall).
        The per-parity threshold is at least 2/3 of those pages — book and journal headers are at 78–100%."""
        def draw(pdf, index):
            if index % 4 == 0:
                pdf.drawString(220, 760, "표준 근로계약서")
            for line in range(12):
                pdf.drawString(72, 680 - line * 30, f"{index + 1}쪽 본문 {line + 1}번째 줄입니다.")
            if index % 4 == 3:
                pdf.drawString(72, 120, "위 계약 내용을 확인하고 서명합니다.")
                pdf.drawString(72, 95, "사업주 (서명)        근로자 (서명)")

        result = convert(_korean_pdf(draw, pages=12), "a.pdf", ConvertOptions(extract_images=False))

        assert result.text.count("표준 근로계약서") == 3
        assert result.text.count("확인하고 서명합니다") == 3
        assert result.text.count("사업주") == 3

    def test_margin_line_on_a_minority_of_one_page_side_is_kept(self):
        """Even when counted per parity, a line on fewer than half of those pages is not a repeated line."""
        def draw(pdf, index):
            pdf.drawString(72, 700, f"{index + 1}쪽 본문입니다.")
            if index in (0, 2):
                pdf.drawString(72, 30, "검토 필요")

        result = convert(_korean_pdf(draw, pages=10), "a.pdf")

        assert result.text.count("검토 필요") == 2

    def test_margin_lines_with_a_count_that_differs_per_page_are_kept(self):
        """'소계 12건', '소계 7건', '소계 19건' (subtotal of N items) in the bottom margin of each page were removed on every page by the
        digit-insensitive comparison. Digits are ignored only in tokens shaped like a page number ('3', '3/10', '3쪽', 'p.3',
        '2024-01-05') — a value with a unit attached ('12건') is compared literally. A running header that mixes a date and a page
        number ('2024-01-05 보고서 3쪽') and page numbers are still removed."""
        counts = [12, 7, 19]

        def draw(pdf, index):
            pdf.drawString(72, 812, f"2024-01-05 보고서 {index + 1}쪽")
            for line in range(5):
                pdf.drawString(72, 700 - line * 30, f"{index + 1}쪽 본문 {line + 1}번째 줄입니다.")
            pdf.drawString(72, 72, f"소계 {counts[index]}건")
            pdf.drawString(300, 40, f"- {index + 1} -")

        result = convert(_korean_pdf(draw, pages=3), "a.pdf")

        assert all(f"소계 {count}건" in result.text for count in counts)
        assert "보고서" not in result.text
        assert "- 1 -" not in result.text and "- 2 -" not in result.text

    def test_margin_lines_with_amounts_or_rates_that_differ_per_page_are_kept(self):
        """'전기이월 잔액: 1,000,000', '실지급액: 3,450,000', '증감률 6.8%' and '소계 1,234' in the top and bottom margins of each page
        leave only punctuation once the digits are removed, so they were taken for page-number tokens and removed on every page. A token
        with a thousands comma, a decimal point, % or a currency sign is a value. A running header that mixes a dotted date and a page
        number ('2024.01.05 보고서 3쪽') and page numbers are still removed."""
        amounts = ["1,000,000", "3,450,000", "2,980,000", "4,010,000"]

        def draw(pdf, index):
            pdf.drawString(72, 812, f"2024.01.05 보고서 {index + 1}쪽")
            pdf.drawString(72, 790, f"전기이월 잔액: {amounts[index]}")
            for line in range(5):
                pdf.drawString(72, 700 - line * 30, f"{index + 1}쪽 본문 {line + 1}번째 줄입니다.")
            pdf.drawString(72, 74, f"소계 {index + 1},234")
            pdf.drawString(72, 62, f"이번 쪽 증감률 {index + 5}.{index * 3}%")
            pdf.drawString(72, 50, f"실지급액: {amounts[-1 - index]}")
            pdf.drawString(300, 30, f"- {index + 1} -")

        result = convert(_korean_pdf(draw, pages=4), "a.pdf")

        for index, section in enumerate(result.sections):
            assert f"전기이월 잔액: {amounts[index]}" in section.text
            assert f"소계 {index + 1},234" in section.text
            assert f"이번 쪽 증감률 {index + 5}.{index * 3}%" in section.text
            assert f"실지급액: {amounts[-1 - index]}" in section.text
        assert "보고서" not in result.text
        assert "- 2 -" not in result.text

    def test_running_footer_with_page_number_beside_it_is_still_removed(self):
        """In a footer with two pieces at the same height ('대외 공개 금지   3 / 4') only the digits of the page-number piece differ.
        It is removed when looked at as a whole line too."""
        def draw(pdf, index):
            pdf.drawString(72, 700, f"{index + 1}쪽 본문 내용입니다.")
            pdf.drawString(72, 30, "대외 공개 금지")
            pdf.drawRightString(520, 30, f"{index + 1} / 4")

        result = convert(_korean_pdf(draw, pages=4), "a.pdf")

        assert "대외 공개 금지" not in result.text
        assert "/ 4" not in result.text
        assert "3쪽 본문 내용입니다." in result.text

    def test_caption_lines_attached_to_a_ruled_table_are_not_removed_as_repeated_lines(self):
        """In a document where a ruled table takes up most of the page (financial statements) only a few body lines remain outside the
        table cells, so '(단위: 백만원)' right above the table and the note right below it fell into the top and bottom two rows of the
        page and were removed as lines repeated on every page. Page numbers in the margin are still removed."""
        columns, top, height = [72, 272, 392, 512], 690, 20

        def draw(pdf, index):
            pdf.drawString(72, 740, f"{index + 1}. 재무제표 {'가나다라'[index]}")
            pdf.drawString(72, 722, "제 12 기 2025년 9월 30일 현재")
            pdf.drawString(72, 704, "(단위: 백만원)")
            rows = [["과목", "당기", "전기"], *([f"항목 {index}-{row}", f"{row * 137 + index:,}", f"{row * 91 + index:,}"] for row in range(1, 24))]
            for line in range(len(rows) + 1):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)
            pdf.drawString(72, top - height * len(rows) - 16, "※ 별첨 주석은 본 재무제표의 일부입니다.")
            pdf.drawCentredString(297, 30, f"- {index + 1} -")

        result = convert(_korean_pdf(draw, pages=4), "a.pdf")

        for section in result.sections:
            assert "제 12 기 2025년 9월 30일 현재\n(단위: 백만원)\n\n| 과목 | 당기 | 전기 |" in section.text
            assert section.text.endswith("※ 별첨 주석은 본 재무제표의 일부입니다.")

    def test_repeated_header_outside_the_margin_area_is_still_removed(self):
        """A slide-style PDF has its header outside the margin (top and bottom 10%). The top and bottom two rows of a page are removed
        when they repeat, even outside the margin."""
        def draw(pdf, index):
            pdf.drawString(72, 740, "2025년 사업 계획 보고")
            for line in range(6):
                pdf.drawString(72, 660 - line * 16, f"{index + 1}쪽의 {line + 1}번째 본문 줄입니다.")
            pdf.drawString(72, 120, "한빛테크 경영기획팀")

        result = convert(_korean_pdf(draw, pages=4), "a.pdf")

        assert "사업 계획 보고" not in result.text
        assert "경영기획팀" not in result.text
        assert "3쪽의 1번째 본문 줄입니다." in result.text

    def test_label_value_rows_in_the_margin_area_keep_their_values(self):
        """In a document whose page numbers are bare numbers, the value (a bare number) of a 'label  value' row reaching down into the
        bottom margin area was removed as a page number ('해지 건수 5' → '해지 건수'). When rows differing only in their value were on
        every page, the whole row was removed. Page numbers and headers are still removed."""
        pairs = [("계약 건수", "128"), ("미수금 건수", "17"), ("해지 건수", "5")]

        def draw(pdf, index):
            pdf.drawString(57, 802, "주식회사 한빛테크")
            for line in range(38):
                pdf.drawString(57, 742 - line * 17, f"{index + 1}-{line + 1}. 본문 문장이 쪽 아래까지 이어진다.")
            for line, (label, value) in enumerate(pairs, start=38):
                pdf.drawString(57, 742 - line * 17, label)
                pdf.drawString(200, 742 - line * 17, value if index == 2 else str(index + line))
            pdf.drawCentredString(297, 30, str(index + 1))

        result = convert(_korean_pdf(draw, pages=6), "a.pdf")

        assert result.sections[2].text.endswith("계약 건수 128\n미수금 건수 17\n해지 건수 5")
        assert result.sections[0].text.endswith("계약 건수 38\n미수금 건수 39\n해지 건수 40")
        assert "한빛테크" not in result.text

    def test_large_page_titles_that_differ_only_in_a_number_are_kept(self):
        """Page titles in the top margin band ('2025년 3월 영업 실적') differ only in a number and were all removed as repeated lines. A
        line in a font larger than the body must match literally to be a repeat. A running header at body size or smaller ('월간 보고
        3쪽') and page numbers are still removed even when the digits differ."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 9)
            pdf.drawRightString(540, 815, f"영업본부 월간 보고 {index + 1}쪽")
            pdf.setFont(_FONT, 16)
            pdf.drawString(60, 780, f"2025년 {index + 1}월 영업 실적")
            pdf.setFont(_FONT, 10)
            for line in range(5):
                pdf.drawString(60, 720 - line * 16, f"{index + 1}월의 {line + 1}번째 본문 문장이다.")
            pdf.drawCentredString(297, 30, f"- {index + 1} -")

        result = convert(_korean_pdf(draw, pages=5), "a.pdf")

        for index, section in enumerate(result.sections):
            assert f"2025년 {index + 1}월 영업 실적" in section.text
        assert "월간 보고" not in result.text
        assert "- 3 -" not in result.text

    def test_large_running_header_with_the_same_text_on_every_page_is_still_removed(self):
        """Even in a font larger than the body, a header with the same text on every page and a page number in a large font are repeated
        lines."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawString(60, 790, "2025년 사업 계획서")
            pdf.drawCentredString(297, 30, f"- {index + 1} -")
            pdf.setFont(_FONT, 10)
            for line in range(5):
                pdf.drawString(60, 720 - line * 16, f"{index + 1}쪽의 {line + 1}번째 본문 문장이다.")

        result = convert(_korean_pdf(draw, pages=5), "a.pdf")

        assert "사업 계획서" not in result.text
        assert "- 3 -" not in result.text
        assert "3쪽의 1번째 본문 문장이다." in result.text

    @pytest.mark.parametrize("values", [["김담당", "이담당", "박담당"], [f"{number * 7919 % 500000:,}" for number in range(1, 39)]])
    def test_values_of_a_one_column_list_at_the_page_edges_are_not_removed_as_repeated_lines(self, values):
        """A column pushed onto the following pages because it overflowed the print width (a single column of the person in charge) has
        the same top and bottom values on every page, so the values of the edge rows were removed as repeated lines (402 of 6,000). In
        a numeric column the values in the margin area were removed as lines differing only in digits (page-number shape). When rows of
        the same shape also appear several times at the same position in the body of that page, they are data, not a header. Page
        numbers are still removed."""
        def draw(pdf, index):
            for row in range(39):
                pdf.drawRightString(120, 800 - row * 20, values[row % len(values)])
            pdf.drawCentredString(297, 15, f"- {index + 1} -")

        result = convert(_korean_pdf(draw, pages=6), "a.pdf")

        for section in result.sections:
            assert section.text.split("\n") == [values[row % len(values)] for row in range(39)]

    def test_borderless_table_row_is_grouped_when_fonts_put_the_cells_at_different_heights(self):
        """A generator that aligns table cells to the top puts the cells of one row 4.5pt apart when Korean and digits use different
        fonts (11pt font, values from a real document). Rows were grouped only by a top-coordinate difference of 2.5pt, so '사원' and
        '3,200,000' became different rows and the table scattered column by column."""
        rows = [["직급", "기본급", "직책수당", "연차일수"], ["사원", "3,200,000", "0", "15"], ["대리", "3,900,000", "100,000", "16"],
                ["과장", "4,700,000", "200,000", "18"], ["부장", "6,100,000", "500,000", "22"]]

        def draw(pdf, index):
            pdf.setFont(_FONT, 11)
            pdf.drawString(72, 760, "직급별 급여 기준은 다음과 같다.")
            for number, row in enumerate(rows):
                for x, value in zip((95, 203, 311, 419), row, strict=True):
                    raised = 4.5 if number and x != 95 else 0.0
                    pdf.drawString(x, 720 - number * 16 + raised, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ("| 직급 | 기본급 | 직책수당 | 연차일수 |\n| --- | --- | --- | --- |\n| 사원 | 3,200,000 | 0 | 15 |\n"
                "| 대리 | 3,900,000 | 100,000 | 16 |") in result.text
        assert "| 부장 | 6,100,000 | 500,000 | 22 |" in result.text

    def test_borderless_table_split_into_half_rows_by_font_heights_is_regrouped(self):
        """A table whose numeric cells (Latin font) sit 3.4pt higher and whose cells starting with a digit ('1차') sit 1.1pt higher
        (values from a real LibreOffice PDF). Grouping by the top coordinate makes '1차·date' and '담당 부서·대상' different rows, yet
        those half rows also formed a table, so the second search, which regroups by overlap, never ran. Half of the cells of every
        row were empty."""
        rows = [["차수", "담당 부서", "대상", "예정일"], ["1차", "정보보안팀", "본사", "2025-10-31"],
                ["2차", "인프라팀", "데이터센터", "2025-11-30"], ["3차", "구매팀", "협력사", "2025-12-15"],
                ["4차", "인사팀", "지사", "2026-01-31"]]

        def draw(pdf, index):
            pdf.drawString(72, 780, "점검 일정은 다음과 같다.")
            for number, row in enumerate(rows):
                for x, value, raised in zip((72, 171, 270, 369), row, (1.1, 0.0, 0.0, 3.4), strict=True):
                    pdf.drawString(x, 740 - number * 20 + (raised if number else 0.0), value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ("| 차수 | 담당 부서 | 대상 | 예정일 |\n| --- | --- | --- | --- |\n| 1차 | 정보보안팀 | 본사 | 2025-10-31 |\n"
                "| 2차 | 인프라팀 | 데이터센터 | 2025-11-30 |") in result.text
        assert "| 4차 | 인사팀 | 지사 | 2026-01-31 |" in result.text

    @pytest.mark.parametrize("rows, expected", [
        ([["구분", "1분기", "2분기", "합계"],
          ["매출", "1,234,567,890", "2,345,678,901", "3,580,246,791"],
          ["비용", "", "45,000,000", "45,000,000"],
          ["순이익", "1,234,567,890", "2,300,678,901", "3,535,246,791"]],
         "| 구분 | 1분기 | 2분기 | 합계 |\n| --- | --- | --- | --- |\n"
         "| 매출 | 1,234,567,890 | 2,345,678,901 | 3,580,246,791 |\n| 비용 | | 45,000,000 | 45,000,000 |"),
        ([["구분", "전년", "당년", "증감"],
          ["매출", "1,200", "1,500", "300"],
          ["특별이익", "", "12,345,678,900", "12,345,678,900"],
          ["순이익", "900", "1,100", "200"]],
         "| 특별이익 | | 12,345,678,900 | 12,345,678,900 |"),
    ])
    def test_borderless_table_with_right_aligned_numbers_wider_than_headers(self, rows, expected):
        """A table whose headers and numbers are aligned to the right edge. When a number is wider than its header, its centre crosses
        into the range of the column on the left, so the header fell off the table or the value landed one column to the left. Columns
        are decided by left/right edge alignment and horizontal overlap."""
        def draw(pdf, index):
            pdf.drawString(72, 780, "부문별 실적입니다. 단위는 원입니다.")
            for number, row in enumerate(rows):
                y = 740 - number * 18
                pdf.drawString(72, y, row[0])
                for right, value in zip((300, 420, 540), row[1:], strict=True):
                    if value:
                        pdf.drawRightString(right, y, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert expected in result.text


class TestBorderlessTableWithWrappedCell:
    """When the text of a cell in a borderless table wrapped onto a second line, the wrapped line became a one-piece row and the table
    broke there. When fewer than 3 rows remained they became neither a table nor 'table-like rows', and the values scattered one per
    line ('Ⅷ. 법인세비용차감전순이익(손실)' in a real income statement)."""

    _ROWS = [("Ⅰ. 매출액", "4", "52,341,220", "48,120,551"), ("Ⅱ. 매출원가", "5", "(38,112,004)", "(34,887,120)"),
             ("Ⅲ. 매출총이익", "", "14,229,215", "13,233,431"), ("Ⅳ. 영업외비용", "7", "(1,894,330)", "(512,004)"),
             (("Ⅴ. 법인세비용차감전", "순이익(손실)"), "", "910,775", "2,884,695"), ("Ⅵ. 법인세비용", "8", "(1,120,400)", "(611,203)"),
             ("Ⅶ. 당기순이익(손실)", "", "(209,624)", "2,273,492")]

    @classmethod
    def _draw(cls, pdf, rows, pitch=24, wrap=12, top=740):
        for x, head in zip((80, 220, 330, 450), ("과목", "주석", "제15기", "제14기"), strict=True):
            pdf.drawString(x, top, head)
        y = top
        for subject, note, current, previous in rows:
            y -= pitch
            parts = subject if isinstance(subject, tuple) else (subject,)
            for number, part in enumerate(parts):
                pdf.drawString(80, y - number * wrap, part)
            pdf.drawString(225, y, note)
            pdf.drawRightString(390, y, current)
            pdf.drawRightString(510, y, previous)
            y -= (len(parts) - 1) * wrap

    def test_wrapped_cell_text_stays_in_its_cell_and_the_table_is_not_split(self):
        def draw(pdf, index):
            self._draw(pdf, self._ROWS)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("| --- | --- | --- | --- |") == 1
        assert "| Ⅴ. 법인세비용차감전<br>순이익(손실) | | 910,775 | 2,884,695 |\n| Ⅵ. 법인세비용 | 8 |" in result.text
        assert "| Ⅶ. 당기순이익(손실) | | (209,624) | 2,273,492 |" in result.text

    def test_single_piece_row_at_the_ordinary_row_spacing_still_ends_the_table(self):
        """A one-piece line at the ordinary row spacing (a note below the table) is not the continuation of a cell's text. It is not
        put in the table."""
        def draw(pdf, index):
            self._draw(pdf, self._ROWS[:4])
            pdf.drawString(80, 620, "주석 참조")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "주석 참조" not in result.text.split("| Ⅳ. 영업외비용")[1].split("\n")[0]
        assert result.text.rstrip().endswith("주석 참조")

    @pytest.mark.parametrize("note", ["* 잠정치", "출처: 인사팀", "(9월 말)"])
    def test_short_note_right_under_the_last_row_is_not_joined_to_its_cell(self, note):
        """A short note written right below the table at the first column was read as the wrapped line of the first cell of the last
        row, giving '인사팀<br>* 잠정치'. A wrapped line is joined to the cell only when more rows of the table follow below it."""
        def draw(pdf, index):
            y = 740
            for row in [["부서", "정원", "현원", "결원"], ["기획팀", "12", "11", "1"], ["총무팀", "8", "8", "0"],
                        ["인사팀", "6", "5", "1"]]:
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 100, y, value)
                y -= 26
            pdf.drawString(72, y + 26 - 14, note)
            pdf.drawString(72, y - 30, "위 표와 같이 기획팀과 인사팀에 각 1명의 결원이 있어 충원이 필요하다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 인사팀 | 6 | 5 | 1 |\n\n" + note + "\n" in result.text

    def test_cell_wrapped_over_three_lines_stays_in_its_cell(self):
        rows = [*self._ROWS[:4], (("Ⅴ. 법인세비용", "차감전", "순이익(손실)"), "", "910,775", "2,884,695"), *self._ROWS[5:]]

        def draw(pdf, index):
            self._draw(pdf, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("| --- | --- | --- | --- |") == 1
        assert "| Ⅴ. 법인세비용<br>차감전<br>순이익(손실) | | 910,775 | 2,884,695 |\n| Ⅵ. 법인세비용 | 8 |" in result.text


class TestBorderlessTableEnd:
    def test_headings_of_two_columns_below_a_table_are_not_a_table_row(self):
        """In a two-column band below a table, the two pieces of the section headings (larger characters) of the left and right columns
        were attached as the last row of the table, giving '| 3. 결과 | | 4. 결론 |'. A row after the first data row is taken in only
        when its character size matches the previous row."""
        def draw(pdf, index):
            y = 700
            for row in [["지역", "2023", "2024", "2025"], ["서울", "100", "104", "109"], ["부산", "80", "79", "77"],
                        ["대구", "60", "61", "60"]]:
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 110, y, value)
                y -= 16
            y -= 20
            for x, heading, tag in ((50, "3. 결과", "L"), (307, "4. 결론", "R")):
                pdf.setFont(_FONT, 12)
                pdf.drawString(x, y, heading)
                pdf.setFont(_FONT, 10)
                for number in range(4):
                    pdf.drawString(x, y - 18 - number * 13, f"[{tag}] 이 단의 {number + 1}번째 본문 줄은 단 폭을 채운다")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 대구 | 60 | 61 | 60 |\n\n" in result.text
        assert "| 3. 결과" not in result.text and "3. 결과" in result.text

    def test_full_rows_of_varying_font_size_stay_in_the_table(self):
        """In an OCR text layer the character size wobbles from row to row. A row with every cell filled stays in the table even at a
        different size (the price table of an OCR'd book was cut)."""
        def draw(pdf, index):
            for number, (row, size) in enumerate([(["모델", "입력", "출력"], 10), (["mini", "$0.15", "$0.60"], 10),
                                                  (["base", "$2.50", "$10.00"], 10), (["large", "$5.00", "$15.00"], 12.5),
                                                  (["max", "$30.00", "$60.00"], 9)]):
                pdf.setFont(_FONT, size)
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 120, 700 - number * 20, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| large | $5.00 | $15.00 |\n| max | $30.00 | $60.00 |" in result.text

    _STATEMENT = [("자산", "", "", ""), ("Ⅰ. 유동자산", "", "12,345,678", "11,234,567"), ("현금및현금성자산", "4,5", "1,234,567", "987,654"),
                  ("매출채권", "6,7", "5,432,100", "4,987,600"), ("자산총계", "", "21,111,111", "20,246,913"),
                  ("부채", "", "", ""), ("Ⅰ. 유동부채", "", "3,456,789", "3,111,222"), ("매입채무", "", "1,111,111", "999,999"),
                  ("단기차입금", "12", "2,345,678", "2,111,223"), ("부채총계", "", "8,456,789", "9,111,222")]

    def test_section_rows_without_values_stay_in_the_table(self):
        """A borderless balance sheet with valueless section rows ('자산', '부채') broke at every section row, so the header fell apart
        into one paragraph per cell and the amounts of a subtotal row with an empty note cell ('Ⅰ. 유동자산') scattered to the end of the
        page (the shape of an audit report). A short section row that sits at the table's row spacing and only in the first column is
        a row of the table when the next row continues the table."""
        def draw(pdf, index):
            for x, head in ((150, "과 목"), (262, "주석"), (370, "제 12 (당) 기"), (490, "제 11 (전) 기")):
                pdf.drawCentredString(x, 745, head)
            for number, (name, note, current, previous) in enumerate(self._STATEMENT, start=1):
                y = 745 - number * 18
                pdf.drawString(60, y, name)
                pdf.drawCentredString(262, y, note)
                pdf.drawRightString(420, y, current)
                pdf.drawRightString(540, y, previous)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("|\n| --- ") == 1
        assert ("| 과 목 | 주석 | 제 12 (당) 기 | 제 11 (전) 기 |\n| --- | --- | --- | --- |\n| 자산 | | | |\n"
                "| Ⅰ. 유동자산 | | 12,345,678 | 11,234,567 |") in result.text
        assert "| 자산총계 | | 21,111,111 | 20,246,913 |\n| 부채 | | | |\n| Ⅰ. 유동부채 | | 3,456,789 | 3,111,222 |" in result.text

    @pytest.mark.parametrize("label_size, data_rows", [(13, 2), (10, 1)])
    def test_heading_or_lone_label_between_table_like_rows_is_not_a_section_row(self, label_size, data_rows):
        """A subheading in larger characters is not a section row, and section rows do not count toward the minimum number of table
        rows. In a document of formulas, subheadings ('절댓값 부등식', '수학적 귀납법 구조') between rows whose cells happened to line
        up were pulled in as rows of a table (real PDF)."""
        def draw(pdf, index):
            rows = [["구간", "하한", "상한"], None, *[["가", "1", "5"], ["나", "6", "9"]][:data_rows]]
            for number, row in enumerate(rows):
                y = 700 - number * 18
                if row is None:
                    pdf.setFont(_FONT, label_size)
                    pdf.drawString(72, y, "소제목")
                    pdf.setFont(_FONT, 10)
                    continue
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 110, y, value)
            for number in range(3):
                pdf.drawString(72, 560 - number * 14, f"본문 {number + 1}번째 줄은 표 모양 행 아래에 이어지는 설명 문장이다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 소제목" not in result.text and "소제목" in result.text

    def test_short_line_set_apart_between_two_tables_is_not_a_section_row(self):
        """A short line set apart between tables by more than the row spacing (the caption of the next table) is not a section row. The
        two tables are separate."""
        def draw(pdf, index):
            rows = [["지역", "2023", "2024"], ["서울", "100", "104"], ["부산", "80", "79"], None,
                    ["품목", "1분기", "2분기"], ["철강", "51", "53"], ["화학", "40", "44"]]
            y = 720
            for row in rows:
                if row is None:
                    pdf.drawString(72, y - 8, "표 2. 품목별")  # caption set 8pt further apart than the row spacing, above and below
                    y -= 32
                    continue
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 110, y, value)
                y -= 16

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("|\n| --- ") == 2
        assert "| 표 2. 품목별" not in result.text

    @pytest.mark.parametrize("between", ["<표 2> 부서별 인원", "주: 2025년은 잠정치", "부서별로는 다음과 같다."])
    def test_short_line_at_row_spacing_before_the_header_of_a_next_table_is_not_a_section_row(self, between):
        """In a document with tight paragraph spacing, a short line (caption, note, sentence) placed at exactly the row spacing between
        two borderless tables became a section row, so the two tables merged into one and the header of the next table became a data
        row (LibreOffice). After a row with values, a next row without a single value is the header of a new table."""
        def draw(pdf, index):
            rows = [["지역", "인원", "예산", "비고"], ["서울", "12", "3,400", "증원"], ["부산", "8", "2,100", "유지"], None,
                    ["부서", "1분기", "2분기", "3분기"], ["기획", "10", "12", "14"], ["총무", "5", "6", "7"]]
            for number, row in enumerate(rows):
                y = 720 - number * 16
                if row is None:
                    pdf.drawString(72, y, between)
                    continue
                for column, value in enumerate(row):
                    pdf.drawString(72 + column * 110, y, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("|\n| --- ") == 2
        assert f"| {between}" not in result.text and between in result.text
        assert "| 부서 | 1분기 | 2분기 | 3분기 |\n| --- |" in result.text


class TestBorderlessTableInOneColumn:
    """A borderless table (or one with only three horizontal rules: top, below the header, bottom) inside one column of a two-column
    page. Grouping rows from lines at the same height across the whole page slipped a body line of the other column into every row,
    and the table scattered into one paragraph per cell."""

    _ROWS = [["구분", "2024", "2025", "증감"], ["대기업", "42%", "58%", "+16%p"], ["중견기업", "18%", "31%", "+13%p"],
             ["중소기업", "7%", "12%", "+5%p"]]
    _TEXT = ("생성형 인공지능의 기업 도입이 빠르게 늘고 있다. 국내 중견기업의 도입률은 2024년 18%에서 2025년 31%로 상승하였으며, "
             "주요 활용 분야는 문서 작성, 고객 응대, 코드 생성 순이었다. ")

    def _story(self, rules: bool, paragraphs_after: int):
        def make(font):
            from reportlab.lib import colors
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.platypus import Paragraph, Spacer, Table, TableStyle

            body = ParagraphStyle("본문", fontName=font, fontSize=9.5, leading=14, wordWrap="CJK")
            style = [("FONT", (0, 0), (-1, -1), font, 9)]
            if rules:
                style += [("LINEABOVE", (0, 0), (-1, 0), 1, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black),
                          ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black)]
            table = Table(self._ROWS, colWidths=[60, 45, 45, 45])
            table.setStyle(TableStyle(style))
            story = [Paragraph(self._TEXT * 2, body), Spacer(1, 6), Paragraph("<표 1> 기업 규모별 도입률", body), table, Spacer(1, 6)]
            for number in range(paragraphs_after):
                story += [Paragraph(f"({number + 1}번 문단) " + self._TEXT * 2, body), Spacer(1, 6)]
            return story

        return make

    @pytest.mark.parametrize("rules", [False, True])
    def test_table_in_the_left_column_beside_the_right_column_text_is_a_table(self, rules):
        data = builder.pdf_two_column_story(self._story(rules, paragraphs_after=8))

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 구분 | 2024 | 2025 | 증감 |\n| --- | --- | --- | --- |\n| 대기업 | 42% | 58% | +16%p |\n"
                "| 중견기업 | 18% | 31% | +13%p |\n| 중소기업 | 7% | 12% | +5%p |") in result.text
        assert result.text.index("<표 1>") < result.text.index("| 구분 |") < result.text.index("(1번 문단)")

    def test_table_in_a_column_with_nothing_beside_it_is_still_a_table(self):
        data = builder.pdf_two_column_story(self._story(rules=False, paragraphs_after=2))

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert "| 구분 | 2024 | 2025 | 증감 |\n| --- | --- | --- | --- |\n| 대기업 | 42% | 58% | +16%p |" in result.text

    @pytest.mark.parametrize("lines", [("생성형 인공지능 도입 실태 조사", "홍길동, 김철수 (한국대학교 경영학과)"),
                                       ("그림 2. 제안하는 문서 변환 파이프라인의 전체 구조",)])
    def test_table_in_a_column_under_a_centred_title_or_caption_is_a_table(self, lines):
        """The centred title and author lines (or a figure caption) in a single-column frame above the two columns straddle the page
        centre and were counted as right-column lines, so the end of the left column passed the start of the right column, two-column
        detection turned off and the table scattered into one paragraph per cell."""
        def header(font):
            from reportlab.lib.enums import TA_CENTER
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.platypus import Paragraph

            style = ParagraphStyle("가운데", fontName=font, fontSize=11, leading=16, alignment=TA_CENTER)
            return [Paragraph(line, style) for line in lines]

        data = builder.pdf_two_column_story(self._story(rules=False, paragraphs_after=8), header=header)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 구분 | 2024 | 2025 | 증감 |\n| --- | --- | --- | --- |\n| 대기업 | 42% | 58% | +16%p |\n"
                "| 중견기업 | 18% | 31% | +13%p |\n| 중소기업 | 7% | 12% | +5%p |") in result.text

    def test_table_in_a_column_below_a_figure_caption_in_the_middle_of_the_page_is_a_table(self):
        """With a figure across both columns and a centred caption in the middle of the page, the caption is inside the height range of
        the lines of both columns, so the page is two-column only when measured again without the lines straddling the page centre."""
        sentence, other = "본 연구에서는 다양한 형식의 문서를 변환하는", "실험 결과 제안 방법은 정확도가 향상되었다"

        def draw(pdf, index):
            pdf.setFont(_FONT, 9)
            for number in range(10):
                pdf.drawString(50, 790 - number * 13, f"{sentence} 위{number + 1}")
                pdf.drawString(310, 790 - number * 13, f"{other} 위{number + 1}")
            pdf.setFillGray(0.8)
            pdf.rect(80, 500, 435, 150, fill=1, stroke=0)
            pdf.setFillGray(0)
            pdf.drawCentredString(297.6, 485, "그림 2. 제안하는 문서 변환 파이프라인의 전체 구조")
            for row, values in enumerate(self._ROWS):
                for x, value in zip((50, 120, 175, 230), values, strict=True):
                    pdf.drawString(x, 455 - row * 14, value)
            for number in range(25):
                pdf.drawString(50, 390 - number * 13, f"{sentence} 아래{number + 1}")
            for number in range(30):
                pdf.drawString(310, 455 - number * 13, f"{other} 밑{number + 1}")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 구분 | 2024 | 2025 | 증감 |\n| --- | --- | --- | --- |\n| 대기업 | 42% | 58% | +16%p |\n"
                "| 중견기업 | 18% | 31% | +13%p |\n| 중소기업 | 7% | 12% | +5%p |") in result.text

    def test_table_across_both_columns_of_a_two_column_page_stays_whole(self):
        """A single-column table across both columns has cells at the positions of both columns in every row, so it is not taken for
        the tables of two columns mixed together (it is not cut into two half tables)."""
        sentence, other = "본 연구에서는 다양한 형식의 문서를 변환하는", "실험 결과 제안 방법은 정확도가 향상되었다"
        rows = [["형식", "건수", "평균", "최대", "오류", "비율"], ["PDF", "320", "1.2", "8.4", "12", "3.8%"],
                ["DOCX", "210", "0.3", "1.1", "3", "1.4%"], ["HWP", "150", "0.5", "2.0", "5", "3.3%"]]

        def draw(pdf, index):
            pdf.setFont(_FONT, 9)
            for number in range(12):
                pdf.drawString(50, 790 - number * 13, f"{sentence} 위{number + 1}")
                pdf.drawString(310, 790 - number * 13, f"{other} 위{number + 1}")
            for row, values in enumerate(rows):
                for x, value in zip((50, 140, 230, 320, 410, 500), values, strict=True):
                    pdf.drawString(x, 610 - row * 14, value)
            for number in range(20):
                pdf.drawString(50, 540 - number * 13, f"{sentence} 아래{number + 1}")
                pdf.drawString(310, 540 - number * 13, f"{other} 밑{number + 1}")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 형식 | 건수 | 평균 | 최대 | 오류 | 비율 |\n| --- | --- | --- | --- | --- | --- |\n"
                "| PDF | 320 | 1.2 | 8.4 | 12 | 3.8% |\n| DOCX | 210 | 0.3 | 1.1 | 3 | 1.4% |\n"
                "| HWP | 150 | 0.5 | 2.0 | 5 | 3.3% |") in result.text

    def test_tables_in_both_columns_under_a_centred_title_keep_their_own_rows(self):
        """With one table in each column and a centred title above, two-column detection turned off and the rows of the two tables at
        the same height were mixed into one line."""
        sentence, other = "본 연구에서는 다양한 형식의 문서를 변환하는", "실험 결과 제안 방법은 정확도가 향상되었다"

        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawCentredString(297.6, 782, "한국어 문서 변환 품질 평가 연구")
            pdf.setFont(_FONT, 10)
            pdf.drawCentredString(297.6, 757, "홍길동, 김철수 (한국대학교 컴퓨터공학과)")
            pdf.setFont(_FONT, 9)
            y = 722
            for number in range(8):
                pdf.drawString(50, y - number * 13, f"{sentence} 왼{number + 1}")
            for row, values in enumerate([["데이터셋", "기존", "제안", "향상"], ["공문", "71.2", "88.5", "17.3"],
                                          ["논문", "65.0", "84.1", "19.1"], ["견적서", "80.3", "91.7", "11.4"]]):
                for x, value in zip((50, 120, 175, 230), values, strict=True):
                    pdf.drawString(x, 600 - row * 14, value)
            for number in range(20):
                pdf.drawString(50, 535 - number * 13, f"{sentence} 아래{number + 1}")
            for number in range(10):
                pdf.drawString(310, y - number * 13, f"{other} 오{number + 1}")
            for row, values in enumerate([["형식", "평균", "최대", "건수"], ["PDF", "1.2", "8.4", "320"],
                                          ["DOCX", "0.3", "1.1", "210"], ["HWP", "0.5", "2.0", "150"]]):
                for x, value in zip((310, 390, 450, 500), values, strict=True):
                    pdf.drawString(x, 586 - row * 14, value)
            for number in range(20):
                pdf.drawString(310, 521 - number * 13, f"{other} 밑{number + 1}")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 데이터셋 | 기존 | 제안 | 향상 |\n| --- | --- | --- | --- |\n| 공문 | 71.2 | 88.5 | 17.3 |\n"
                "| 논문 | 65.0 | 84.1 | 19.1 |\n| 견적서 | 80.3 | 91.7 | 11.4 |") in result.text
        assert ("| 형식 | 평균 | 최대 | 건수 |\n| --- | --- | --- | --- |\n| PDF | 1.2 | 8.4 | 320 |\n"
                "| DOCX | 0.3 | 1.1 | 210 |\n| HWP | 0.5 | 2.0 | 150 |") in result.text

    def test_paragraph_ends_above_a_table_with_a_wide_note_column_are_not_two_columns(self):
        """On a single-column page, when the last lines of paragraphs (25–50% of the page width) are on the left and the wide '비고'
        (remarks) cells of a table are on the right, not at the same height, the page was judged two-column and the '비고' column fell
        off the table. The lines of two columns must be beside each other (in the same height range). Every data row has a wide cell,
        so no table is found (the 'short piece' criterion for borderless tables, deliberate) and the rows come out as row lines — the
        values of a row stay together on one line."""
        paragraphs = [("반도체 부문은 고대역폭 메모리 수요 증가에 힘입어 전년 대비 매출이 크게 늘었다. 특히 서버용 제품의",
                       "판매 비중이 작년보다 크게 확대되었다."),
                      ("디스플레이 부문은 중국 업체와의 가격 경쟁 심화로 매출이 감소하였으며, 수익성 개선을 위한 원가",
                       "절감 활동을 전사적으로 추진 중이다."),
                      ("모바일 부문은 신제품 출시 효과로 출하량이 소폭 증가하였으나 평균 판매 단가는 하락하여 전체 매출은",
                       "전년과 비슷한 수준을 유지하고 있다."),
                      ("가전 부문은 프리미엄 제품 판매 확대로 이익률이 개선되었으며, 해외 신규 시장 진출을 위한 유통망을",
                       "현지 법인을 중심으로 지속적으로 확대해 나가고 있다."),
                      ("기타 부문은 신사업 투자 확대로 비용이 늘었으나 장기적인 성장 기반을 마련하는 데 의의가 있으며",
                       "내년에는 흑자 전환을 기대하고 있다.")]
        rows = [["구분", "2024", "2025", "증감", "비고"], ["반도체", "120", "158", "+38", "HBM 판매 확대 및 서버용 수요 급증 영향"],
                ["디스플레이", "81", "74", "-7", "중국 업체 가격 경쟁 심화, 원가 절감 추진"],
                ["모바일", "56", "56", "0", "신제품 출시 효과로 출하량 소폭 증가함"],
                ["가전", "40", "45", "+5", "프리미엄 제품 확대로 이익률 크게 개선"],
                ["기타", "10", "8", "-2", "신사업 투자 비용 증가로 이익 감소함"]]

        def draw(pdf, index):
            y = 757
            for first, second in paragraphs:
                pdf.drawString(50, y, first)
                pdf.drawString(50, y - 15, second)
                y -= 40
            for row, values in enumerate(rows):
                for x, value in zip((50, 130, 190, 250, 330), values, strict=True):
                    pdf.drawString(x, y - 20 - row * 18, value)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "반도체 120 158 +38 HBM 판매 확대 및 서버용 수요 급증 영향\n" in result.text
        assert result.text.endswith("기타 10 8 -2 신사업 투자 비용 증가로 이익 감소함")


class TestBorderlessTableWithWideCells:
    """Rows in which one cell (the item name) of a borderless table exceeds 25% of the page width. The 'short piece' criterion
    (conservative, deliberate) is left as is; a table found with it is extended upward, or rows that cannot form a table are output
    row by row."""

    _ITEMS = [("무선 블루투스 키보드 (한/영 각인, 블랙) 저소음", "12", "45,000"), ("27인치 QHD 모니터 IPS 패널 75Hz 피벗", "8", "289,000"),
              ("USB-C 멀티 허브", "20", "32,000"), ("인체공학 메쉬 사무용 의자 (헤드레스트 포함)", "15", "178,000"),
              ("A4 복사용지", "40", "24,500"), ("레이저 프린터 토너", "6", "98,000"), ("회의실용 스피커폰", "3", "215,000"),
              ("노트북 거치대", "25", "27,000")]

    @staticmethod
    def _draw_rows(pdf, items, top=760):
        for x, head in zip((60, 110, 380, 460), ("번호", "품명", "수량", "단가"), strict=True):
            pdf.drawString(x, top, head)
        for number, (name, quantity, price) in enumerate(items, start=1):
            y = top - number * 16
            pdf.drawString(60, y, str(number))
            pdf.drawString(110, y, name)
            pdf.drawRightString(400, y, quantity)
            pdf.drawRightString(500, y, price)

    def test_table_found_among_short_rows_starts_at_the_header_above_the_wide_rows(self):
        """The table broke at the rows with a wide item name, so the first part scattered column by column and the latter part, made of
        short rows only, became a table whose header was a data row from the middle ('5 | A4 복사용지')."""
        def draw(pdf, index):
            self._draw_rows(pdf, self._ITEMS)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text.startswith(
            "| 번호 | 품명 | 수량 | 단가 |\n| --- | --- | --- | --- |\n| 1 | 무선 블루투스 키보드 (한/영 각인, 블랙) 저소음 | 12 | 45,000 |\n")
        assert "| 5 | A4 복사용지 | 40 | 24,500 |\n| 6 | 레이저 프린터 토너 | 6 | 98,000 |" in result.text
        assert result.text.count("| --- | --- | --- | --- |") == 1

    def test_rows_that_cannot_form_a_table_are_read_row_by_row(self):
        """When the item name is wide in every row, no table is formed. Even so, the number, item name, quantity and unit price do not
        scatter column by column; each row is one line."""
        items = [("무선 블루투스 키보드 (한/영 각인, 블랙)", "12", "45,000"), ("27인치 고해상도 모니터 광시야각 패널 피벗 지원", "8", "289,000"),
                 ("고속 충전 멀티 허브 7포트 알루미늄 본체", "20", "32,000"), ("인체공학 메쉬 사무용 의자 (헤드레스트 포함)", "15", "178,000")]

        def draw(pdf, index):
            pdf.drawString(60, 790, "아래는 이번 분기에 구매한 물품의 내역이다.")
            self._draw_rows(pdf, items)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ("번호 품명 수량 단가\n1 무선 블루투스 키보드 (한/영 각인, 블랙) 12 45,000\n2 27인치 고해상도 모니터 광시야각 패널 피벗 지원 8 289,000\n"
                "3 고속 충전 멀티 허브 7포트 알루미늄 본체 20 32,000\n4 인체공학 메쉬 사무용 의자 (헤드레스트 포함) 15 178,000") in result.text

    def test_short_pieces_beside_a_column_of_prose_are_not_joined_across_the_columns(self):
        """Short pieces in the left column (formulas and their numbers) at the same height as body lines of the right column are not
        joined into rows when the body column continues above and below."""
        def draw(pdf, index):
            for number in range(9):
                y = 760 - number * 14
                if 3 <= number <= 5:
                    pdf.drawString(72, y, f"합계{number} = 단가 × 수량")
                    pdf.drawString(240, y, f"(식 {number})")
                else:
                    pdf.drawString(72, y, f"왼쪽 단 {number + 1}번째 줄 본문입니다 길게 씁니다")
                pdf.drawString(300, y, f"오른쪽 단 {number + 1}번째 줄 본문이며 길게 이어집니다")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ") 오른쪽" not in result.text
        assert "오른쪽 단 4번째 줄 본문이며 길게 이어집니다\n오른쪽 단 5번째 줄 본문이며 길게 이어집니다" in result.text

    def test_side_note_beside_part_of_a_two_column_list_is_not_joined_into_its_rows(self):
        """A notice box beside a few rows of a '제N조 제목' (Article N, title) list became the third piece of those rows and slipped in
        between the list rows one line at a time, like '제2조 정의 이 규정은 2026년…'. When the rows above and below the group continue
        in the same columns without a wide piece (the list continues outside the box), they are not joined."""
        note = ["이 규정은 2026년 1월 1일부터 시행하며", "시행 전에 접수된 신청은 종전 규정에", "따라 처리한다 다만 신청인이 원하면",
                "개정 규정을 적용할 수 있다고 본다"]

        def draw(pdf, index):
            for number, title in enumerate(["목적", "정의", "적용 범위", "위원회 구성", "위원의 임기", "회의 소집", "의결 방법", "보칙"]):
                y = 740 - number * 18
                pdf.drawString(50, y, f"제{number + 1}조")
                pdf.drawString(110, y, title)
                if 1 <= number <= 4:
                    pdf.drawString(300, y, note[number - 1])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(note) in result.text
        assert "제2조 정의" not in result.text

    @staticmethod
    def _draw_two_rows(pdf, rows, xs, right=()):
        """Draw `rows` at the cell starts `xs` (a column index in `right` is right-aligned to its `xs` as the cell end)."""
        pdf.drawString(60, 760, "3. 변경 적용일 및 금액 한도는 다음과 같습니다.")
        for number, row in enumerate(rows):
            for column, (x, value) in enumerate(zip(xs, row, strict=True)):
                (pdf.drawRightString if column in right else pdf.drawString)(x, 735 - number * 25, value)
        pdf.drawString(60, 670, "4. 문의사항은 재무팀(02-111-2222)으로 연락 바랍니다.")

    @pytest.mark.parametrize("rows, xs, right", [
        ([["적용일", "2025. 11. 1.", "월 한도", "5억원"], ["유예기간", "2025. 12. 31.", "건당 한도", "1억원"]], (66, 174, 282, 390), ()),
        ([["자본총계", "100,657,210,113", "88,664,122,783"], ["부채와자본총계", "150,769,210,113", "139,114,122,783"]],
         (66, 409, 517), (1, 2))])
    def test_two_aligned_rows_of_short_pieces_are_read_row_by_row(self, rows, xs, right):
        """A borderless 2-row table ('적용일 | date | limit' in an official letter, the last two rows of a financial statement carried
        over to the next page) falls short of the minimum number of table rows and scattered column by column ('적용일 / 유예기간 /
        2025. 11. 1. / …'). Two rows whose cells line up in every column are read one line per row."""
        def draw(pdf, index):
            self._draw_two_rows(pdf, rows, xs, right)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(" ".join(row) for row in rows) in result.text

    def test_two_line_labels_of_boxes_side_by_side_are_left_as_read(self):
        """Labels wrapped onto two lines in each of several boxes side by side ('임상 진행 약물 / 추가 적응증 발굴' in a diagram) have
        cells that line up in every column, but the rows are only a line spacing apart. Joining them into rows split the two lines of
        each box (real presentation PDF)."""
        labels = [("임상 1상 전 최적", "적응증 발굴"), ("임상 진행 약물", "추가 적응증 발굴"), ("시판 약물", "병용 약제 발굴")]

        def draw(pdf, index):
            for number, lines in enumerate(labels):
                for row, text in enumerate(lines):
                    pdf.drawCentredString(130 + number * 170, 735 - row * 12, text)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "임상 진행 약물" in result.text
        assert "임상 1상 전 최적 임상 진행 약물" not in result.text

    def test_two_rows_beside_a_column_continuing_above_them_are_left_as_read(self):
        """In a two-column table of contents, an entry of the right column ('4-1 필사하자 140') at the same height as two entries of the
        left column does not make two rows of a table. When the left column continues above and below the two rows at the same left
        edge, they are not joined into rows (real OCR'd book PDF)."""
        def draw(pdf, index):
            for number in range(5):
                pdf.drawString(60, 735 - number * 20, f"4-{number + 1} 장 제목 {number + 1}번째 항목")
            for number in range(2):
                pdf.drawString(320, 695 - number * 20, f"5-{number + 1} 요약하기")
                pdf.drawRightString(500, 695 - number * 20, str(140 + number))

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "항목 5-1 요약하기" not in result.text
        assert "4-2 장 제목 2번째 항목\n4-3 장 제목 3번째 항목" in result.text

    def test_two_rows_of_short_pieces_out_of_line_are_left_as_read(self):
        """Two rows whose cells do not line up in every column (short pieces placed staggered) are not table-shaped. They are not
        joined into rows."""
        def draw(pdf, index):
            for number, (xs, row) in enumerate([((66, 210, 380), ("서울 본사", "부산 지사", "대구 지사")),
                                                ((120, 290, 450), ("02-111-2222", "051-222-3333", "053-333-4444"))]):
                for x, value in zip(xs, row, strict=True):
                    pdf.drawString(x, 735 - number * 25, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "서울 본사 부산 지사" not in result.text
        assert "02-111-2222 051-222-3333" not in result.text


def test_photo_collage_filling_the_page_keeps_its_photos():
    """Four photos filling the page 2×2 without gaps (site photos, each with a caption text) were merged as 'adjoining images that
    cover the page' into a background, and since the page has more than 100 characters all the photos vanished. Adjoining images are
    merged into one background only when they are strip-shaped (every piece fills the whole width or height of the merged box) — the
    case of a scanner storing a page as horizontal strips."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.utils import ImageReader

    from opendockit import ImageKind

    width, height = landscape(A4)
    names = ["서울 공장", "부산 물류센터", "대전 연구소", "광주 사업장"]

    def draw(pdf, index):
        pdf.setPageSize((width, height))
        for number, name in enumerate(names):
            x, y = number % 2 * width / 2, (1 - number // 2) * height / 2
            pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(600, 400, seed=number))), x, y, width / 2, height / 2)
            pdf.drawString(x + 20, y + 30, f"{name} — 2025년 준공, 연면적 12,000㎡, 상시 근로자 350명 근무")

    result = convert(_korean_pdf(draw), "a.pdf")

    assert [image.kind for image in result.images] == [ImageKind.FIGURE] * 4
    assert all(name in result.text for name in names)


def test_background_strips_with_pieces_laid_inside_them_stay_one_background():
    """On a designed page with a left-half piece laid over each page-wide background strip (real guidebook PDF), the pieces inside the
    strips made it look not strip-shaped, and the 10 background pieces became pictures. A piece lying inside another piece is left
    out of the strip-shape check."""
    from io import BytesIO

    from reportlab.lib.utils import ImageReader

    def draw(pdf, index):
        for strip in range(5):
            y = 841.89 - (strip + 1) * 841.89 / 5
            pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(600, 170, seed=strip))), 0, y, 595.28, 841.89 / 5)
            pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(320, 170, seed=10 + strip))), 0, y, 323, 841.89 / 5)
        for row in range(12):
            pdf.drawString(72, 760 - row * 50, f"{row + 1}번째 본문 줄: 업무에 생성형 AI 를 활용하는 방법을 설명합니다.")

    result = convert(_korean_pdf(draw), "a.pdf")

    assert result.images == []
    assert result.text.count("활용하는 방법을 설명합니다.") == 12


class TestDrawingArtifacts:
    @pytest.mark.parametrize("mark, font", [("대외비", _FONT), ("CONFIDENTIAL", "Helvetica")])
    def test_diagonal_watermark_is_not_body_text_or_heading(self, mark, font):
        """A watermark drawn at 45 degrees on every page slipped into the end of the body character by character ('비\\n\\n외\\n\\n대')
        and, being large, took the top heading level."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 18)
            pdf.drawString(72, 780, f"{index + 1}. 사업 개요")
            pdf.setFont(_FONT, 11)
            for line in range(4):
                pdf.drawString(72, 750 - line * 15, f"본문 {line + 1}번째 줄입니다. 사업 내용을 설명합니다.")
            pdf.saveState()
            pdf.translate(300, 420)
            pdf.rotate(45)
            pdf.setFillGray(0.85)
            pdf.setFont(font, 60)
            pdf.drawCentredString(0, 0, mark)
            pdf.restoreState()

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "# 1. 사업 개요" in result.text
        for char in set(mark):
            assert char not in result.text.replace("CONFIDENTIAL", "")
        assert "CONFIDENTIAL" not in result.text
        assert result.text.count("번째 줄입니다") == 8

    @pytest.mark.parametrize("rotation", [90, 180, 270])
    def test_rotated_page_with_an_off_centre_crop_box_keeps_its_visible_text(self, rotation):
        """On a rotated page pdfplumber flips the CropBox using the height before rotation, so visible characters were judged to be
        outside the trim line and removed. On a rotated page the trim-line filter and the visible-area margins are not used (the
        previous behaviour)."""
        def draw(pdf, index):
            pdf.setFont("Helvetica", 8)
            for x in range(110, 440, 90):
                for y in range(110, 720, 60):
                    pdf.drawString(x, y, f"p{x}q{y}")

        data = _korean_pdf(draw).replace(b"/Rotate 0", b"/Rotate %d" % rotation).replace(
            b"/MediaBox [ ", b"/CropBox [ 85 97 445 727 ] /MediaBox [ ")
        assert b"/Rotate %d" % rotation in data

        result = convert(data, "a.pdf")

        missing = [(x, y) for x in range(110, 440, 90) for y in range(110, 720, 60) if f"p{x}q{y}" not in result.text]
        assert missing == []

    def test_text_outside_the_crop_box_is_not_read(self):
        """A PDF for print keeps printer's marks outside the trim line (outside the CropBox): '….qxd 2/10/04 3:46 PM Page 7',
        '4TH PASS PAGES', proof line numbers. They are not visible on screen, yet they slipped into the body on every page and became
        broken tables (350 pages of a real book PDF). The CropBox is placed asymmetrically top and bottom so that a flipped coordinate
        system makes the test fail: the visible area is 42–642pt from the top."""
        def draw(pdf, index):
            pdf.setFont("Helvetica", 7)
            pdf.drawString(40, 830, "14773_01_1-174_r9jm.qxd 2/10/04 3:51 PM Page 17")
            pdf.drawString(575, 700, "1")
            pdf.drawString(300, 120, "4TH PASS PAGES")
            pdf.setFont(_FONT, 11)
            pdf.drawString(72, 760, "보이는 영역 맨 위의 본문입니다.")
            pdf.drawString(72, 300, "보이는 영역 아래쪽의 본문입니다.")

        data = _korean_pdf(draw).replace(b"/MediaBox [ ", b"/CropBox [ 36 200 559 800 ] /MediaBox [ ")
        assert b"/CropBox [ 36 200" in data

        result = convert(data, "a.pdf")

        assert "보이는 영역 맨 위의 본문입니다." in result.text
        assert "보이는 영역 아래쪽의 본문입니다." in result.text
        assert "qxd" not in result.text
        assert "PASS" not in result.text
        assert "\n1\n" not in f"\n{result.text}\n"

    def test_page_whose_text_lies_entirely_outside_the_crop_box_stays_empty(self):
        """A page left with 0 lines after the characters outside the visible area were removed was taken for a 'page with 0 characters'
        and swapped for pdfium simple extraction, so the hidden text came back and the warning said 'structure analysis failed'. A
        page from which characters were removed is not swapped."""
        def draw(pdf, index):
            pdf.drawString(72, 760, "재단선 밖에 남은 교정 메모입니다.")
            pdf.drawString(72, 300, "이 줄도 보이는 영역 밖입니다.")

        data = _korean_pdf(draw).replace(b"/MediaBox [ ", b"/CropBox [ 300 500 595 842 ] /MediaBox [ ")
        assert b"/CropBox [ 300 500" in data

        result = convert(data, "a.pdf")

        assert "교정 메모" not in result.text and "보이는 영역 밖" not in result.text
        assert not any("Structure analysis failed" in warning for warning in result.warnings)

    def test_small_tilted_labels_inside_a_ruled_table_are_kept(self):
        """The slanted tick labels of a chart axis are read as they are from the cells detected as a table. Only characters much larger
        than the page's text are taken for a watermark."""
        def draw(pdf, index):
            pdf.drawString(72, 780, "분기별 추이 차트입니다.")
            top = 700
            for row in range(3):
                pdf.line(72, top - row * 40, 372, top - row * 40)
            for x in (72, 172, 272, 372):
                pdf.line(x, top, x, top - 80)
            for column, label in enumerate(["Q1", "Q2", "Q3"]):
                pdf.drawString(80 + column * 100, top - 15, f"항목{column + 1}")
                pdf.saveState()
                pdf.translate(90 + column * 100, top - 70)
                pdf.rotate(30)
                pdf.setFont("Helvetica", 7)
                pdf.drawString(0, 0, label)
                pdf.restoreState()

        result = convert(_korean_pdf(draw), "a.pdf")

        for label in ("Q1", "Q2", "Q3"):
            assert label in result.text

    def test_fake_bold_drawn_twice_is_read_once(self):
        """'Fake bold': the same text drawn twice, shifted by 0.3–0.4pt, instead of using a bold font. The title came out twice and the
        table header became '항항목목'."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            for shift in (0, 0.4):
                pdf.drawString(72 + shift, 780, "사업 추진 계획")
            pdf.setFont(_FONT, 10)
            pdf.drawString(72, 750, "본문 첫 줄입니다. 내용이 이어집니다.")
            for shift in (0, 0.3):
                pdf.drawString(72 + shift, 735, "총 예산 1,500")
            top = 700
            for row in range(4):
                pdf.line(72, top - row * 20, 372, top - row * 20)
            for x in (72, 172, 272, 372):
                pdf.line(x, top, x, top - 60)
            for row, cells in enumerate([["항목", "수량", "금액"], ["가", "1", "100"], ["나", "2", "200"]]):
                for column, value in enumerate(cells):
                    for shift in ((0, 0.35) if row == 0 else (0,)):
                        pdf.drawString(76 + column * 100 + shift, top - row * 20 - 14, value)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.count("사업 추진 계획") == 1
        assert result.text.count("총 예산 1,500") == 1
        assert "| 항목 | 수량 | 금액 |" in result.text

    def test_shadow_drawn_in_another_colour_slightly_offset_is_read_once(self):
        """A shadow effect (the same text drawn first in grey, shifted by 1.2–3pt) exceeds the fake-bold threshold of 1pt and came out
        twice on every line. The same character drawn again in another colour within 0.15 times the character size is a copy. The same
        characters written side by side in the same colour are left alone."""
        shadows = [(20, 1.5, -1.5), (20, 2.5, -2.5), (24, 3, 0), (12, 1.2, -1.2), (16, 0, -2)]

        def draw(pdf, index):
            for number, (size, dx, dy) in enumerate(shadows):
                pdf.setFont(_FONT, size)
                pdf.setFillColorRGB(0.7, 0.7, 0.7)
                pdf.drawString(60 + dx, 760 - number * 60 + dy, f"경영 전략 발표 {number + 1}")
                pdf.setFillColorRGB(0, 0, 0)
                pdf.drawString(60, 760 - number * 60, f"경영 전략 발표 {number + 1}")
            pdf.setFont(_FONT, 10)
            pdf.drawString(60, 420, "본문 문장입니다. 그림자 제목 아래 일반 문단이 이어집니다.")
            pdf.drawString(60, 400, "llll IIII 1111 ....")

        result = convert(_korean_pdf(draw), "a.pdf")

        for number in range(len(shadows)):
            assert result.text.count(f"경영 전략 발표 {number + 1}") == 1
        assert "llll IIII 1111 ...." in result.text

    @pytest.mark.parametrize("hidden", [b"/F1 0 Tf 72 700 Td", b"/F1 12 Tf 0 0 0 0 72 700 Tm", b"/F1 0 Tf 300 500 Td"],
                             ids=["size-0-on-the-line", "degenerate-matrix", "size-0-alone"])
    def test_text_drawn_at_zero_size_is_not_read(self, hidden):
        """Text drawn at character size 0 (or with a text matrix that makes the size 0) is not on screen. At the same position as a
        visible line it slipped into that line character by character, giving 'Htzerosix ello world'."""
        body = b"BT %s (zero size text) Tj ET BT /F1 12 Tf 72 700 Td (Hello world body text) Tj ET" % hidden
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
                            b" /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>",
                         4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(body), body)})

        result = convert(data, "a.pdf")

        assert result.sections[0].text == "Hello world body text"

    def test_page_with_huge_number_of_drawing_objects_is_read_with_pdfium(self, monkeypatch):
        """One page of a paper's figure had 240,000 Form XObjects, so pdfminer used 65 s and 4.6 GB on that single page (with 280 text
        pieces). The drawing objects (including those inside forms) are counted with pdfium first, and when there are too many the
        page is read with simple extraction, with a warning.
        A document of the real size takes long to build, so the limit is lowered for the check."""
        from opendockit.extractors import pdf as pdf_module

        def draw(pdf, index):
            pdf.drawString(72, 780, f"{index + 1}쪽 그림 설명입니다.")
            if index == 1:
                pdf.beginForm("dot")
                pdf.rect(0, 0, 1, 1, fill=1)
                pdf.endForm()
                for number in range(60):
                    pdf.saveState()
                    pdf.translate(72 + number * 5, 400)
                    pdf.doForm("dot")
                    pdf.restoreState()

        original, read = pdf_module._read_page, []

        def recording(page, *args):
            read.append(page.page_number)
            return original(page, *args)

        monkeypatch.setattr(pdf_module, "_read_page", recording)
        monkeypatch.setattr(pdf_module, "_MAX_PAGE_OBJECTS", 50)

        result = convert(_korean_pdf(draw, pages=3), "a.pdf")

        assert read == [1, 3]
        assert "2쪽 그림 설명입니다." in result.sections[1].text
        assert any("drawing elements" in warning and "(2)" in warning for warning in result.warnings)


class TestFlowJoins:
    def test_two_column_label_value_rows_keep_each_pair_on_one_line(self):
        """A two-column 'label  value' layout was split by pdfminer into a group of labels and a group of values, separating the pairs
        like '연락처\\n주소\\n\\n010-…\\n서울…'."""
        pairs = [("성명", "홍길동"), ("생년월일", "1990년 1월 1일"), ("연락처", "010-1234-5678"),
                 ("주소", "서울특별시 중구 세종대로 110"), ("최종학력", "한국대학교 경영학과 졸업")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawString(72, 790, "인적 사항")
            pdf.setFont(_FONT, 11)
            for number, (label, value) in enumerate(pairs):
                pdf.drawString(72, 760 - number * 18, label)
                pdf.drawString(160, 760 - number * 18, value)
            pdf.setFont(_FONT, 16)
            pdf.drawString(72, 650, "경력 사항")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(f"{label} {value}" for label, value in pairs) in result.text
        assert "|" not in result.text

    @pytest.mark.parametrize("pairs", [
        [("수신", "수신자 참조"), ("(경유)", ""), ("제목", "2025년 하반기 안전점검 실시 계획 알림")],
        [("수신", "한빛시 각 부서장"), ("참조", "총무과장"), ("(경유)", ""), ("제목", "2025년 하반기 안전점검 실시 계획 알림")],
    ])
    def test_label_row_with_an_empty_value_between_label_value_rows_keeps_the_pairs(self, pairs):
        """In the head of an official letter, '수신 / (경유) / 제목', the valueless '(경유)' row cut the group of 'label value' rows (at
        least 2 consecutive rows are needed), which split into a group of labels and a group of values
        ('수신\\n(경유)\\n제목\\n\\n수신자 참조\\n\\n2025년 …'). A label-only row between rows that are pairs is put in the group."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 20)
            pdf.drawCentredString(297, 772, "한 빛 시 청")
            pdf.setFont(_FONT, 11)
            for number, (label, value) in enumerate(pairs):
                pdf.drawString(60, 722 - number * 22, label)
                pdf.drawString(120, 722 - number * 22, value)
            bottom = 722 - len(pairs) * 22
            pdf.line(60, bottom + 8, 535, bottom + 8)
            pdf.drawString(60, bottom - 14, "1. 위 호와 관련하여 2025년 하반기 시설물 안전점검을 아래와 같이 실시하오니 협조하여 주시기 바랍니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(f"{label} {value}".strip() for label, value in pairs) in result.text

    @pytest.mark.parametrize("long_row", [0, 2, 4])
    def test_label_value_rows_with_one_value_longer_than_half_the_page_keep_the_pairs(self, long_row):
        """When one value is longer than half the page width, that row cut the 'label value' group, and both cut groups were discarded
        because of the value-column piece of the neighbouring row, so the labels and the values split apart entirely. The values were
        pushed even past the next section heading ('2. 추진 배경')."""
        values = ["스마트 공장 고도화 지원 사업", "2025. 3. 1. ~ 2026. 2. 28.", "1,200백만원", "한국산업진흥원", "㈜가나다테크"]
        values[long_row] = "1,200백만원 (국비 600백만원, 지방비 240백만원, 자부담 360백만원)"
        labels = ["사업명", "사업 기간", "총 사업비", "주관 기관", "참여 기관"]

        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 782, "1. 사업 개요")
            pdf.setFont(_FONT, 10)
            for number, (label, value) in enumerate(zip(labels, values, strict=True)):
                pdf.drawString(70, 752 - number * 21, label)
                pdf.drawString(170, 752 - number * 21, value)
            pdf.drawString(60, 637, "2. 추진 배경")
            pdf.drawString(60, 617, "최근 제조업 경쟁력 강화를 위한 디지털 전환 요구가 커지고 있다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(f"{label} {value}" for label, value in zip(labels, values, strict=True)) in result.text
        assert result.text.index(values[-1]) < result.text.index("2. 추진 배경")

    @pytest.mark.parametrize("value_x", [150, 311])
    def test_wrapped_value_lines_stay_under_their_label(self, value_x):
        """A value wrapped at the cell width (a borderless two-column table of LibreOffice or Word) has its next line alone in the value
        column, and the 'label value' group was discarded. A wrapped line is put below the value line of its label. LibreOffice split
        the column widths in half so the value column started at the page centre (311pt) — when a 'label value' row comes again after
        a value-only wrapped line, they are joined even though the columns are far apart."""
        rows = [("사업명", ["스마트 공장 고도화 지원 사업"]),
                ("총 사업비", ["1,200백만원 (국비 600백만원, 지방비 240백만원, 자부", "담 360백만원)"]),
                ("지원 대상", ["공고일 현재 본사 또는 공장이 경기도에 소재한 중소", "제조기업으로서 최근 3년 이내 스마트 공장 구축 이력",
                              "이 있는 기업"]),
                ("주관 기관", ["한국산업진흥원"])]

        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 782, "1. 사업 개요")
            pdf.setFont(_FONT, 10)
            y = 752
            for label, lines in rows:
                pdf.drawString(62, y, label)
                for line in lines:
                    pdf.drawString(value_x, y, line)
                    y -= 13
                y -= 6
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, y - 20, "2. 추진 배경")

        result = convert(_korean_pdf(draw), "a.pdf")

        for label, lines in rows:
            assert "\n".join([f"{label} {lines[0]}", *lines[1:]]) in result.text
        assert result.text.index("주관 기관 한국산업진흥원") < result.text.index("2. 추진 배경")

    def test_two_columns_of_short_lines_are_not_joined_through_a_line_only_on_the_right(self):
        """In two-column text made of short lines, a right-column line at the spot where a left-column paragraph ended looks the same as
        a wrapped value line. A group with long labels (over 15% of the page width) does not take wrapped value lines, so the two
        columns are not joined row by row."""
        def draw(pdf, index):
            for number in range(4):
                pdf.drawString(60, 760 - number * 14, f"왼쪽 첫 문단 {number + 1}번째 줄 본문")
            for number in range(9):
                pdf.drawString(310, 760 - number * 14, f"오른쪽 단 {number + 1}번째 줄 본문")
            for number in range(4):
                pdf.drawString(60, 690 - number * 14, f"왼쪽 둘째 문단 {number + 1}번째 줄 본문")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert not any("왼쪽" in line and "오른쪽" in line for line in result.text.splitlines())

    def test_two_column_prose_with_a_short_list_on_the_left_is_not_joined(self):
        """In a two-column layout, a short list in the left column at the same height as right-column lines is not joined across the
        columns."""
        def draw(pdf, index):
            for number in range(8):
                y = 760 - number * 14
                left = f"- 항목 {number + 1} 세부 설명입니다" if 2 <= number <= 5 else f"왼쪽 단 {number + 1}번째 줄 본문입니다 길게"
                pdf.drawString(72, y, left)
                pdf.drawString(270, y, f"오른쪽 단 {number + 1}번째 줄 본문입니다")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "설명입니다 오른쪽" not in result.text
        assert result.text.index("- 항목 6") < result.text.index("오른쪽 단 1번째")

    def test_single_character_beside_a_long_line_is_not_joined(self):
        """An identifier printed vertically in the left margin of a paper ('arXiv:…') comes out as one-character lines. They are not
        joined to the body line beside them as 'label value'."""
        def draw(pdf, index):
            for number, char in enumerate("vx"):
                pdf.drawString(30, 700 - number * 14, char)
                pdf.drawString(72, 700 - number * 14, f"본문 {number + 1}번째 줄은 쪽 폭 대부분을 차지하는 긴 문장으로 이어집니다 끝")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "v 본문" not in result.text
        assert "본문 1번째 줄은 쪽 폭 대부분을 차지하는 긴 문장으로 이어집니다 끝\n본문 2번째" in result.text

    def test_letter_spaced_title_is_one_line(self):
        """pdfminer split a title with wide letter spacing into one line per character, giving a run of one-character paragraphs
        ('제\\n\\n안\\n\\n서')."""
        def draw(pdf, index):
            for x, y, size, spacing, value in ((100, 700, 28, 70, "제안서"), (100, 640, 20, 45, "사업계획")):
                text = pdf.beginText(x, y)
                text.setFont(_FONT, size)
                text.setCharSpace(spacing)
                text.textOut(value)
                pdf.drawText(text)
            text = pdf.beginText(72, 560)
            text.setFont(_FONT, 11)
            text.setCharSpace(0)
            text.textOut("본문 문단입니다. 이 문서는 제안 내용을 담습니다.")
            pdf.drawText(text)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "# 제안서" in result.text
        assert "## 사업계획" in result.text


class TestWidelySpacedColumns:
    """At a line spacing of 150% or more pdfminer makes a text box per line and pairs the horizontally nearest lines first, so two
    side-by-side columns were mixed line by line (130% and 140% are fine: one text box per column). The columns are read one at a
    time whatever the line spacing."""

    @staticmethod
    def _columns(pdf, pitch, offset=0.0, rows=12, top=700):
        for number in range(rows):
            pdf.drawString(60, top - number * pitch, f"왼쪽 단 {number + 1}번째 줄: 품질 방침과 고객 만족 설명")
            pdf.drawString(320, top - offset - number * pitch, f"오른쪽 단 {number + 1}번째 줄: 교육 일정과 신청 안내")

    @pytest.mark.parametrize("pitch, offset", [(13, 0), (15, 0), (16, 0), (16, 5), (18, 5), (20, 0), (30, 0)])
    def test_two_columns_are_read_column_by_column_at_any_line_spacing(self, pitch, offset):
        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawString(60, 740, "사내 소식지")
            pdf.setFont(_FONT, 10)
            self._columns(pdf, pitch, offset)

        result = convert(_korean_pdf(draw), "a.pdf")

        lines = [line.split("번째 줄")[0] for line in result.text.splitlines() if "번째 줄" in line]
        assert lines == [f"{side} 단 {number}" for side in ("왼쪽", "오른쪽") for number in range(1, 13)]

    def test_side_by_side_text_boxes_keep_their_items_under_their_own_heading(self):
        """Side-by-side text boxes in a presentation ('현재', '개선') have wide gaps between items, so the left and right items came out
        alternately."""
        boxes = [(43, "현재(As-Is)", ["종이 품의서를 출력해 대면 결재한다", "결재 완료까지 평균 5일이 걸린다", "진행 상황은 전화로만 확인한다"]),
                 (330, "개선(To-Be)", ["전자결재 시스템에서 모바일로 승인", "결재 완료까지 평균 1일이 걸린다", "진행 상황은 알림으로 자동 통지"])]

        def draw(pdf, index):
            for x, head, items in boxes:
                pdf.setFont(_FONT, 16)
                pdf.drawString(x, 700, head)
                pdf.setFont(_FONT, 12)
                for number, item in enumerate(items):
                    pdf.drawString(x, 665 - number * 30, f"• {item}")

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for _, head, items in boxes for text in (head, *items)]
        assert order == sorted(order)

    def test_line_across_both_columns_separates_the_columns_above_from_those_below(self):
        """When a line crosses both columns (a full-width subheading), the two columns above it are read first, then that line, then
        the two columns below."""
        def draw(pdf, index):
            self._columns(pdf, 18, rows=4, top=740)
            pdf.drawString(60, 650, "이하 내용은 다음 분기 계획이며 두 단에 걸쳐 쓴 안내 문장으로 쪽 폭 대부분을 차지합니다.")
            for number in range(4):
                pdf.drawString(60, 620 - number * 18, f"아래 왼쪽 {number + 1}번째 줄: 생산 계획과 설비 점검 일정")
                pdf.drawString(320, 620 - number * 18, f"아래 오른쪽 {number + 1}번째 줄: 채용 계획과 면접 일정")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 4번째", "오른쪽 단 1번째", "오른쪽 단 4번째", "이하 내용은", "아래 왼쪽 4번째", "아래 오른쪽 1번째"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_rows_of_a_short_piece_beside_a_long_line_are_still_read_row_by_row(self):
        """A row with a long line beside a short piece such as an article number is not two side-by-side columns. The row-by-row order
        is not changed."""
        clauses = [f"사용자는 {number}번 화면에서 조회 조건을 입력하고 결과를 내려받을 수 있어야 하며 권한을 확인한다" for number in range(1, 6)]

        def draw(pdf, index):
            for number, clause in enumerate(clauses):
                pdf.drawString(60, 700 - number * 30, f"제{number + 1}조")
                pdf.drawString(110, 700 - number * 30, clause)

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for number, clause in enumerate(clauses) for text in (f"제{number + 1}조", clause)]
        assert order == sorted(order)

    def test_two_columns_with_wide_paragraph_spacing_are_read_column_by_column(self):
        """In two columns with about one line of space after each paragraph, pdfminer makes a text box per paragraph, and the paragraphs
        of the right column (including the rest of the sentence carried over from the end of the left column) slipped in between the
        paragraphs of the left column. The flow order (left column → right column → next page) is the order the text was written in."""
        from io import BytesIO

        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate, Paragraph

        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
        sentences = ["시장 규모는 매년 꾸준히 증가하고 있다.", "주요 경쟁사는 가격 인하 전략을 택하였다.",
                     "당사의 점유율은 전년보다 소폭 상승하였다.", "원자재 수급 불안이 지속되고 있다.",
                     "정부 정책 변화에 따른 영향도 고려해야 한다.", "해외 시장 진출은 내년 상반기로 예정되어 있다."]
        counts = [2, 5, 1, 3, 1, 4, 4, 4, 4, 2, 1, 4, 1, 4, 4, 5, 1, 4, 3, 2, 5, 1, 3, 1, 1, 1, 5, 1]
        texts = [f"({number + 1}번 문단) " + " ".join(sentences[(number + k) % 6] for k in range(count))
                 for number, count in enumerate(counts)]
        style = ParagraphStyle("본문", fontName=_FONT, fontSize=10, leading=14, spaceAfter=14, wordWrap="CJK")
        width, height = A4
        column = (width - 120) / 2
        buffer = BytesIO()
        document = BaseDocTemplate(buffer, pagesize=A4)
        document.addPageTemplates([PageTemplate(frames=[Frame(50, 50, column, height - 100),
                                                        Frame(70 + column, 50, column, height - 100)])])
        document.build([Paragraph(text, style) for text in texts])

        result = convert(buffer.getvalue(), "a.pdf", ConvertOptions(page_markers=False))

        assert "".join(result.text.split()) == "".join("".join(texts).split())

    def test_left_aligned_text_below_both_columns_stays_after_the_right_column(self, monkeypatch):
        """Text set out below the two columns ('자주 묻는 질문') is not the continuation of the left column even though it is right
        below it. On a page pdfminer read as left column → right column → text below, the text below was pulled up in front of the
        right column (real notice PDF)."""
        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def read_in_column_blocks(page, *args):
            read = original(page, *args)
            read.lines.sort(key=lambda line: (not line.text.startswith(("왼쪽", "오른쪽")), line.text.startswith("오른쪽"),
                                              line.top))  # left column, right column, text below
            return read

        monkeypatch.setattr(pdf_module, "_read_page", read_in_column_blocks)

        def draw(pdf, index):
            for number in range(5):
                pdf.drawString(80, 700 - number * 15, f"왼쪽 단 {number + 1}번째 줄: 개인 계정 생성 팝업 안내")
                pdf.drawString(330, 700 - number * 15, f"오른쪽 단 {number + 1}번째 줄: 계정 권한 변경 안내")
            pdf.setFont(_FONT, 13)
            pdf.drawString(40, 620, "자주 묻는 질문")
            pdf.drawString(40, 600, "개인 계정을 꼭 만들어야 하나요?")

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for text in ("왼쪽 단 5번째", "오른쪽 단 1번째", "오른쪽 단 5번째", "자주 묻는 질문")]
        assert order == sorted(order)

    @staticmethod
    def _paragraph_columns(pdf):
        """Two side-by-side columns of three paragraphs each (6pt between paragraphs). Both columns end at the same height."""
        for side, x in (("왼쪽", 57), ("오른쪽", 309)):
            y = 730
            for paragraph in range(3):
                for number in range(5):
                    pdf.drawString(x, y, f"{side} 단 {paragraph + 1}번 문단 {number + 1}번째 줄 본문 내용입니다")
                    y -= 11.7
                y -= 6

    @pytest.mark.parametrize("below", [
        [("3. 결론", 15), ("결론 문단은 두 단 아래에 쪽 폭 전체로 이어지는 문장이며 오른쪽 끝까지 길게 쓴 본문 줄입니다 하나 둘 셋", 10),
         ("결론 문단의 둘째 줄입니다.", 10)],
        [("문의: 기획팀 02-123-4567", 10)]])
    def test_short_line_below_both_columns_stays_after_the_right_column(self, below):
        """A short line below the two columns (a subheading one column wide, a contact line) is not the continuation of the left column
        even though it is right below it. It was attached to the left column and came out before the right column ('3. 결론' below a
        LibreOffice two-column section, the shape of a real report)."""
        def draw(pdf, index):
            self._paragraph_columns(pdf)
            for number, (text, size) in enumerate(below):
                pdf.setFont(_FONT, size)
                pdf.drawString(57, 520 - number * 14 - (8 if number else 0), text)

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 3번 문단 5번째", "오른쪽 단 1번 문단 1번째", "오른쪽 단 3번 문단 5번째", *(text[:8] for text, _ in below)]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_last_line_of_the_left_column_below_the_end_of_the_right_column_stays_in_the_left_column(self, monkeypatch):
        """In a presentation with wide line spacing, the continuation line of the last item of the left column (an indented line in
        parentheses) is not text below the two columns even though it sits lower than the right column. It is indented from the left
        edge of the column body, and the line spacing is the ordinary one inside the column (real agenda PDF)."""
        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def read_column_by_column(page, *args):
            read = original(page, *args)
            read.lines.sort(key=lambda line: (line.x0 > 300, line.top))  # left column → right column
            return read

        monkeypatch.setattr(pdf_module, "_read_page", read_column_by_column)
        left = ["• 연구개발 인프라 현황과 시험 설비 보유 내역", "- 항온항습 챔버와 충방전 시험기 각 2대 보유",
                "• 대전 환경과학기술원과 인접한 입지를 확보함", "- 타 외부시험소 대비 실시간 시험 모니터링 가능",
                "  (이노테스; 울산, KTL; 진주/천안 등)"]
        right = ["[주요 실적] 배터리 열관리 시험 수행 실적 요약", "- 열폭주 평가와 충방전 평가 수행 실적 다수",
                 "- 냉각판 성능 평가 및 누설 시험 수행 실적", "- 모듈 단위 열 확산 시험 수행 실적 보유"]

        def draw(pdf, index):
            for number, text in enumerate(left):
                pdf.drawString(60 + (12 if text.startswith(" ") else 0), 700 - number * 22, text.strip())
            for number, text in enumerate(right):
                pdf.drawString(320, 700 - number * 22, text)

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text.strip()[:12]) for text in (*left, *right)]
        assert order == sorted(order)

    @staticmethod
    def _read_in_order(monkeypatch, key):
        """Make it as if pdfminer had given the lines of the page in `key` order."""
        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def read_in_order(page, *args):
            read = original(page, *args)
            read.lines.sort(key=key)
            return read

        monkeypatch.setattr(pdf_module, "_read_page", read_in_order)

    @pytest.mark.parametrize("summary", [False, True])
    def test_right_column_read_before_the_left_column_is_put_after_it(self, monkeypatch, summary):
        """When pdfminer gives the group of the right column before the group of the left column, the right column came out first (the
        left column that came back had no earlier left column to continue, so it was seen as a break). The same holds when a
        single-column summary paragraph above puts its short last line in front of the left column (reportlab two-column paper)."""
        self._read_in_order(monkeypatch, lambda line: (line.top >= 100, line.x0 < 300, line.top))  # summary, right column, left column

        def draw(pdf, index):
            if summary:
                pdf.drawString(57, 800, "요약: 본 보고서는 두 단으로 쓴 본문 위에 쪽 폭 전체로 이어지는 요약 문단을 둔 문서이며")
                pdf.drawString(57, 786, "그 마지막 줄은 짧다.")
            self._paragraph_columns(pdf)

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 1번 문단 1번째", "왼쪽 단 3번 문단 5번째", "오른쪽 단 1번 문단 1번째", "오른쪽 단 3번 문단 5번째"]
        order = [result.text.index(mark) for mark in (["그 마지막 줄은"] if summary else []) + marks]
        assert order == sorted(order)

    def test_left_column_continuing_below_its_table_is_not_taken_for_text_below_both_columns(self, monkeypatch):
        """When there is a table in the middle of the left column and the right column ends at the height of the left text above the
        table, the caption below the table and the rest of the left column looked like 'text below the two columns' and were pushed
        behind the right column (the last page of a Word two-column paper). The table is counted as part of the left column when the
        bottom ends of the two columns are measured."""
        self._read_in_order(monkeypatch, lambda line: (line.x0 > 300, line.top))  # left column → right column

        def draw(pdf, index):
            for side, x in (("왼쪽", 57), ("오른쪽", 309)):
                for number in range(8):
                    pdf.drawString(x, 760 - number * 14 - number // 4 * 14, f"{side} 단 {number + 1}번째 줄 본문 내용입니다 하나")
            TestBlockPlacement._grid(pdf, [57, 140, 210, 280], 620, 20, [["모델", "정밀도", "재현율"], ["기준", "0.68", "0.74"]])
            pdf.drawString(57, 560, "표 1. 모델별 성능 비교")
            for number in range(6):
                pdf.drawString(57, 530 - number * 14, f"왼쪽 단 {number + 9}번째 줄 본문 내용입니다 하나")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 8번째", "| 모델 |", "표 1.", "왼쪽 단 14번째", "오른쪽 단 1번째", "오른쪽 단 8번째"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_heading_and_paragraph_closing_the_longer_left_column_stay_in_the_left_column(self, monkeypatch):
        """On a page whose left column is longer, the subheading and paragraph of the left column lying below the end of the right
        column looked like 'text below the two columns' and were pushed behind the right column. When the lines below continue while
        filling the column width, it is text of the left column."""
        self._read_in_order(monkeypatch, lambda line: (line.x0 < 300 < line.x1, line.x0 > 300, line.top))

        def draw(pdf, index):
            for side, x in (("왼쪽", 57), ("오른쪽", 309)):
                for number in range(8):
                    pdf.drawString(x, 760 - number * 14 - number // 4 * 8, f"{side} 단 {number + 1}번째 줄 본문 내용입니다 하나")
            pdf.setFont(_FONT, 13)
            pdf.drawString(57, 620, "3. 마무리")
            pdf.setFont(_FONT, 10)
            for number in range(2):
                pdf.drawString(57, 602 - number * 14, f"왼쪽 단 {number + 9}번째 줄 본문 내용입니다 하나")
            pdf.drawString(57, 560, "두 단 아래의 맺음 문장은 쪽 폭 전체로 이어지며 오른쪽 단 자리까지 길게 쓴 본문 줄입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 8번째", "3. 마무리", "왼쪽 단 10번째", "오른쪽 단 1번째", "오른쪽 단 8번째", "두 단 아래의 맺음"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_left_column_text_set_apart_below_the_right_column_read_first_comes_after_it(self, monkeypatch):
        """When pdfminer gives the right column first and the group of the left column runs on into column-wide text set well apart
        below the two columns, that text came out before the right column. A left-column group beside the right column is read in two
        parts, cut where it breaks (gap > line height × 3) past the bottom end of the right column."""
        self._read_in_order(monkeypatch, lambda line: (line.x0 < 300, line.top))  # right column → left column

        def draw(pdf, index):
            for side, x in (("왼쪽", 57), ("오른쪽", 309)):
                for number in range(8):
                    pdf.drawString(x, 760 - number * 14 - number // 4 * 8, f"{side} 단 {number + 1}번째 줄 본문 내용입니다 하나")
            for number in range(3):
                pdf.drawString(57, 600 - number * 14, f"아래쪽 {number + 1}번째 줄은 왼쪽 단 폭으로 이어 쓴 본문입니다")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 1번째", "왼쪽 단 8번째", "오른쪽 단 1번째", "오른쪽 단 8번째", "아래쪽 1번째", "아래쪽 3번째"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_left_column_continuing_below_its_table_beside_the_right_column_read_first_stays_before_it(self, monkeypatch):
        """In the same order as above (right column first), left-column text continuing below a table in the middle of the left column
        is not broken-off text. The gap is measured with the table counted as the preceding line — the caption below the table and
        the rest of the left column were pushed behind the right column (the second page of a two-column paper)."""
        self._read_in_order(monkeypatch, lambda line: (line.x0 < 300, line.top))  # right column → left column

        def draw(pdf, index):
            for number in range(8):
                pdf.drawString(57, 760 - number * 14 - number // 4 * 8, f"왼쪽 단 {number + 1}번째 줄 본문 내용입니다 하나")
            for number in range(6):
                pdf.drawString(309, 760 - number * 14, f"오른쪽 단 {number + 1}번째 줄 본문 내용입니다 하나")
            TestBlockPlacement._grid(pdf, [57, 140, 210, 280], 620, 20, [["모델", "정밀도", "재현율"], ["기준", "0.68", "0.74"]])
            pdf.drawString(57, 560, "표 1. 모델별 성능 비교")
            for number in range(4):
                pdf.drawString(57, 530 - number * 14, f"왼쪽 단 {number + 9}번째 줄 본문 내용입니다 하나")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 8번째", "| 모델 |", "표 1.", "왼쪽 단 12번째", "오른쪽 단 1번째", "오른쪽 단 6번째"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    @pytest.mark.parametrize("extra", [16, 21])
    def test_paragraphs_starting_level_in_both_columns_of_one_section_are_read_column_by_column(self, monkeypatch, extra):
        """In one two-column section whose paragraphs are 2–3 line heights apart, when the paragraphs of the two columns start at the
        same height, that spot has the same shape as the boundary of two two-column sections stacked one above the other with no
        crossing line. Cutting a new section at such a spot would read this page alternately paragraph by paragraph — reading a page
        whose two stacked sections are at most 3 line heights apart as one section is a known accepted gap."""
        pitch = 4 * 14 + extra
        self._read_in_order(monkeypatch, lambda line: (int((line.top - 60) // pitch), line.x0 > 300, line.top))  # alternating paragraphs

        def draw(pdf, index):
            for side, x in (("왼쪽", 57), ("오른쪽", 309)):
                for paragraph in range(3):
                    for number in range(4):
                        y = 760 - paragraph * pitch - number * 14
                        pdf.drawString(x, y, f"{side} {paragraph + 1}문단 {number + 1}번째 줄 본문 내용입니다 하나")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = [f"{side} {paragraph}문단 1번째" for side in ("왼쪽", "오른쪽") for paragraph in (1, 2, 3)]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_short_last_line_of_a_summary_above_two_columns_does_not_hide_the_columns(self, monkeypatch):
        """The short last line of a single-column summary paragraph above the two columns (a line slightly wider than a column, passing
        the start of the right column) was counted as a left-column line, so column detection turned off and the two columns, read
        alternately paragraph by paragraph, stayed mixed (reportlab and Word two-column papers)."""
        def interleaved(line):
            if line.top < 100:
                return (0, 0, 0, line.top)
            paragraph = int(line.text.split("번 문단")[0][-1])
            return (1, paragraph, line.x0 > 300, line.top)  # left paragraph 1, right paragraph 1, left paragraph 2 …

        self._read_in_order(monkeypatch, interleaved)

        def draw(pdf, index):
            pdf.drawString(57, 780, "요약: 본 보고서는 두 단으로 쓴 본문 위에 쪽 폭 전체로 이어지는 요약 문단을 둔 문서이며")
            pdf.drawString(57, 766, "그 마지막 줄은 오른쪽 단이 시작하는 자리를 조금 넘는 길이다.")
            self._paragraph_columns(pdf)

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["그 마지막 줄은", "왼쪽 단 1번 문단 1번째", "왼쪽 단 3번 문단 5번째", "오른쪽 단 1번 문단 1번째", "오른쪽 단 3번 문단 5번째"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_rows_of_a_condition_beside_its_indicator_are_still_read_row_by_row(self):
        """A layout like '조건 | 해당 지표' (condition | indicator), where each row pairs a sentence on the left with a short value on
        the right, is not two side-by-side columns. The row order is kept."""
        rows = [("제품이나 서비스를 취약계층에게 할인하여 판매한다면", "제품 서비스 성과 1. 할인 제공"),
                ("기존 시장에 없는 제품이나 서비스를 생산하여 제공한다면", "제품 서비스 성과 1. 신규 제공"),
                ("통제된 시장에서 품질을 높이려고 비용을 투입한다면", "제품 서비스 성과 2. 품질 제고 비용 투입"),
                ("취약계층을 직접 고용한다면", "내부 공정 성과 1. 직접 고용"),
                ("직업 교육을 통해 취업과 이직을 지원한다면", "내부 공정 성과 2. 경과성 일자리"),
                ("소규모 농가의 직거래 유통을 확대한다면", "외부 공정 성과 1. 소농 거래"),
                ("해외 현지 생산자와 공정하게 거래한다면", "외부 공정 성과 3. 공정무역 비농산물 거래 확대")]

        def draw(pdf, index):
            for number, (condition, indicator) in enumerate(rows):
                pdf.drawString(70, 700 - number * 24, condition)
                pdf.drawString(350, 700 - number * 24, indicator)

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for row in rows for text in row]
        assert order == sorted(order)

    @staticmethod
    def _short_columns(pdf, heading_size=12, closing=None):
        """Two short columns starting with a subheading (3 lines per column) and, below them, a conclusion line across both columns.
        With `closing`, the left column is two lines longer and that text (a 14pt single-column heading) is put above the conclusion
        line."""
        left = ["국내 매출은 전년 대비 8% 증가하였다 수도권 신", "규 매장 12곳이 개점하였고 온라인 매출 비중이"]
        if closing:
            left += ["35%까지 높아졌다 물류센터 자동화로 배송 기간", "이 하루 단축되었고 고객 만족도는 4.6점으로"]
        columns = [(57, "4.1 국내", [*left, "역대 최고를 기록하였다." if closing else "35%까지 높아졌다."]),
                   (309, "4.2 해외", ["해외 매출은 15% 성장하였다 베트남 법인이 흑", "자 전환하였으며 미국 법인은 유통망 재편으로",
                                      "수익성이 개선되었다."])]
        for x, heading, lines in columns:
            pdf.setFont(_FONT, heading_size)
            pdf.drawString(x, 740, heading)
            pdf.setFont(_FONT, 10)
            for number, text in enumerate(lines):
                pdf.drawString(x, 718 - number * 16, text)
        bottom = 718 - len(columns[0][2]) * 16
        if closing:
            pdf.setFont(_FONT, 14)
            pdf.drawString(57, bottom - 20, closing)
            pdf.setFont(_FONT, 10)
            bottom -= 26
        pdf.drawString(57, bottom - 20, "결론: 2026년에는 해외 비중을 40%까지 확대하고 국내는 수익성 중심으로 매장을 재편하는 계획이다.")
        return columns

    @pytest.mark.parametrize("order", ["as_read", "headings_first"])
    def test_short_two_columns_under_level_headings_are_read_column_by_column(self, monkeypatch, order):
        """A short two-column section with at most 4 wide lines per column falls short of column detection (5 wide lines), so the
        subheadings of the two columns came out side by side and then the lines of the two columns alternated one by one ('4.1 국내 |
        4.2 해외' in LibreOffice and reportlab reports). When a pair of subheadings sits at the same height at the top of the two
        columns, short two columns are read column by column too."""
        if order == "headings_first":  # page where pdfminer gives the subheading pair first, then left column, right column, crossing line
            self._read_in_order(monkeypatch, lambda line: (line.x0 < 300 < line.x1, line.size < 11, line.x0 > 300, line.top))
        columns = []

        def draw(pdf, index):
            columns.extend(self._short_columns(pdf))

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = [text[:10] for _, heading, lines in columns for text in (heading, *lines)] + ["결론: 2026년"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_heading_below_short_two_columns_with_the_longer_left_column_comes_after_the_right_column(self):
        """When the left column of short two columns is longer, the single-column heading below ('5. 결론') looked like the continuation
        of the left column and came out before the right column. In short two columns found from a pair of subheadings, among the left
        lines below the end of the right column, the text below the two columns starts at the first line as large as those subheadings."""
        columns = []

        def draw(pdf, index):
            columns.extend(self._short_columns(pdf, closing="5. 결론"))

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = [text[:10] for _, heading, lines in columns for text in (heading, *lines)] + ["5. 결론", "결론: 2026년"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    def test_short_two_columns_under_body_size_headings_are_left_as_read(self):
        """Subheadings no larger than the body are not taken as evidence of short two columns (deliberate, conservative — they cannot be
        told apart from the header row of a '조건 | 지표' list paired row by row). Known accepted gap: such short two columns come out
        alternating line by line, as before."""
        def draw(pdf, index):
            self._short_columns(pdf, heading_size=10)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.index("국내 매출은") < result.text.index("해외 매출은") < result.text.index("규 매장 12곳이")

    def test_rows_of_a_condition_beside_its_indicator_under_a_larger_header_row_are_still_read_row_by_row(self):
        """Even when the header row ('조건 | 해당 지표') is larger than the body, a list pairing a sentence on the left with a value on
        the right in every row is not short two columns. They are short two columns only when at least half of the lines of each
        column are wrapped lines filling the column width."""
        rows = [("제품이나 서비스를 취약계층에게 할인하여 판매한다면", "제품 서비스 성과 1. 할인 제공"),
                ("기존 시장에 없는 제품이나 서비스를 생산하여 제공한다면", "제품 서비스 성과 1. 신규 제공"),
                ("통제된 시장에서 품질을 높이려고 비용을 투입한다면", "제품 서비스 성과 2. 품질 제고 비용 투입"),
                ("직업 교육을 통해 취업과 이직을 지원한다면", "내부 공정 성과 2. 경과성 일자리")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 12)
            pdf.drawString(70, 730, "조건")
            pdf.drawString(350, 730, "해당 지표")
            pdf.setFont(_FONT, 10)
            for number, (condition, indicator) in enumerate(rows):
                pdf.drawString(70, 700 - number * 24, condition)
                pdf.drawString(350, 700 - number * 24, indicator)

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for row in rows for text in row]
        assert order == sorted(order)

    @pytest.mark.parametrize("right_rows", [8, 10])
    def test_one_line_paragraph_under_a_heading_below_both_columns_comes_after_the_right_column(self, monkeypatch, right_rows):
        """A single-column subheading and a one-line paragraph below the two columns looked like 'text continuing the left column' when
        that one line happened to be as long as the column width, and were pulled up in front of the right column (below a LibreOffice
        two-column section). It is the continuation of the left column only when a line filling the column width wraps onto the next
        line at that same column width."""
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfbase.pdfmetrics import stringWidth

        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))  # measured below, before `_korean_pdf` registers it
        self._read_in_order(monkeypatch, lambda line: (line.x0 < 300 < line.x1, line.x0 > 300, line.top))
        body = "왼쪽 단 {}번째 줄 본문 내용입니다 하나"
        tail = "맺음 문단은 한 줄이며 단 폭만큼 길게 쓴 줄입니다."
        assert stringWidth(body.format(1), _FONT, 10) - 10 < stringWidth(tail, _FONT, 10) < 309 - 57

        def draw(pdf, index):
            for side, x, rows in (("왼쪽", 57, 8), ("오른쪽", 309, right_rows)):
                for number in range(rows):
                    pdf.drawString(x, 760 - number * 14 - number // 4 * 8, f"{side} 단 {number + 1}번째 줄 본문 내용입니다 하나")
            pdf.setFont(_FONT, 13)
            pdf.drawString(57, 760 - right_rows * 14 - 30, "3. 마무리")
            pdf.setFont(_FONT, 10)
            pdf.drawString(57, 760 - right_rows * 14 - 50, tail)

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["왼쪽 단 8번째", "오른쪽 단 1번째", f"오른쪽 단 {right_rows}번째", "3. 마무리", "맺음 문단은"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    @pytest.mark.parametrize("pitch, below", [(16, "paragraph"), (16, "short"), (20, "paragraph")])
    def test_heading_below_widely_spaced_columns_read_line_by_line_comes_after_the_right_column(self, pitch, below):
        """A single-column heading and short paragraph below two columns with a line spacing of 160% or more (a page read mixed line by
        line and put back by `_read_columns_in_turn`) are lines on the left-column side, so they were attached to the left column and
        came out before the right column. The 'text below the two columns' check of the paragraph-level path is used on the line-level
        path too."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(57, 770, "Ⅳ. 시장 분석")
            pdf.setFont(_FONT, 10)
            for number in range(8):
                pdf.drawString(57, 740 - number * pitch, f"[왼쪽 {number + 1}] 국내 시장은 연평균 12% 성장")
                pdf.drawString(309, 740 - number * pitch, f"[오른쪽 {number + 1}] 해외 시장은 규모가 약 40조")
            bottom = 740 - 8 * pitch - 20
            pdf.setFont(_FONT, 14)
            pdf.drawString(57, bottom, "Ⅴ. 결론")
            pdf.setFont(_FONT, 10)
            if below == "short":
                pdf.drawString(57, bottom - 26, "[맺음] 국내 점유율 확대가 우선이다.")
            else:
                pdf.drawString(57, bottom - 26, "[맺음] 이상을 종합하면 국내 시장은 성장성이, 해외 시장은 규모가 강점이므로 단계적으로 진출한다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["Ⅳ. 시장 분석", "[왼쪽 1]", "[왼쪽 8]", "[오른쪽 1]", "[오른쪽 8]", "Ⅴ. 결론", "[맺음]"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)

    @pytest.mark.parametrize("left_rows", [3, 4])
    def test_heading_below_short_headed_columns_read_line_by_line_comes_after_the_right_column(self, left_rows):
        """When short two columns under a pair of subheadings are put back by the line-level path too, the single-column heading below
        them ('5. 결론') comes after the right column. On a page whose left column is one line longer, too, the text below the two
        columns starts at the first line as large as the subheadings (`_heading_below_headed_columns`)."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 13)
            pdf.drawString(57, 770, "4.1 국내")
            pdf.drawString(309, 770, "4.2 해외")
            pdf.setFont(_FONT, 10)
            for number in range(left_rows):
                pdf.drawString(57, 750 - number * 15, f"[왼쪽 {number + 1}] 국내 시장은 연평균 12% 성장하고")
            for number in range(3):
                pdf.drawString(309, 750 - number * 15, f"[오른쪽 {number + 1}] 해외 시장은 규모가 약 40조 원")
            bottom = 750 - left_rows * 15 - 20
            pdf.setFont(_FONT, 13)
            pdf.drawString(57, bottom, "5. 결론")
            pdf.setFont(_FONT, 10)
            pdf.drawString(57, bottom - 20, "[맺음] 이상을 종합하면 국내 시장은 성장성이, 해외 시장은 규모가 강점이므로 단계적으로 진출한다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        marks = ["4.1 국내", "[왼쪽 1]", f"[왼쪽 {left_rows}]", "4.2 해외", "[오른쪽 1]", "[오른쪽 3]", "5. 결론", "[맺음]"]
        order = [result.text.index(mark) for mark in marks]
        assert order == sorted(order)


class TestRightAlignedLinesBetweenLines:
    """On a page where left lines and right-aligned lines alternate from top to bottom (a chat transcript, agenda items with
    right-aligned presenters, right-aligned ※ notes between lines) the meaning is in the top-to-bottom order, but pdfminer's text-box
    flow took them for columns and read all the left lines and then the right lines."""

    @staticmethod
    def _draw(pdf, lines, top=740, pitch=20, size=10):
        from reportlab.pdfbase.pdfmetrics import stringWidth

        pdf.setFont(_FONT, size)
        for number, (side, text) in enumerate(lines):
            y = top - number * pitch
            if side == "L":
                pdf.drawString(72, y, text)
            else:
                pdf.drawString(523 - stringWidth(text, _FONT, size), y, text)

    @pytest.mark.parametrize("pitch", [14, 20, 26])
    def test_chat_transcript_is_read_top_to_bottom(self, pitch):
        lines = [("L", "안녕하세요 지난번 견적서 관련해서 문의드립니다"), ("R", "네 안녕하세요 어떤 부분이 궁금하신가요"),
                 ("L", "수량을 100개에서 200개로 늘리면 단가가 어떻게 되나요"), ("R", "200개면 개당 4,500원으로 해 드릴 수 있습니다"),
                 ("L", "그럼 납기는 언제까지 가능한가요 다음 달 초요"), ("R", "다음 달 5일까지 납품 가능합니다 확인 부탁드립니다")]

        result = convert(_korean_pdf(lambda pdf, index: self._draw(pdf, lines, pitch=pitch)), "a.pdf")

        positions = [result.text.index(text) for _, text in lines]
        assert positions == sorted(positions)

    def test_agenda_items_keep_their_right_aligned_presenters_below_them(self):
        lines = [line for number, (item, who) in enumerate([("개회 선언", "사회: 기획팀장 홍길동"), ("경과 보고", "보고: 총무팀장 김철수"),
                                                           ("예산안 심의", "설명: 예산팀장 이영희"), ("폐회", "사회: 기획팀 대리 최영수")],
                                                          start=1)
                 for line in (("L", f"{number}. {item} 순서에 따라 진행합니다"), ("R", who))]

        result = convert(_korean_pdf(lambda pdf, index: self._draw(pdf, lines, pitch=17)), "a.pdf")

        positions = [result.text.index(text) for _, text in lines]
        assert positions == sorted(positions)

    def test_right_aligned_notes_between_left_lines_stay_where_they_are(self):
        lines = [("L", "참석: 기획팀 총무팀 인사팀 재무팀 각 1명"), ("R", "※ 화상 참석자는 별도 명단으로 관리함"),
                 ("L", "안건 1: 2027년도 예산 편성 방향 확정"), ("L", "안건 2: 하반기 인력 충원 계획 검토"),
                 ("R", "※ 안건 2는 인사팀 요청으로 추가됨"), ("L", "결정: 안건 1 원안 의결 안건 2 보류함")]

        result = convert(_korean_pdf(lambda pdf, index: self._draw(pdf, lines, pitch=18)), "a.pdf")

        positions = [result.text.index(text) for _, text in lines]
        assert positions == sorted(positions)

    def test_left_aligned_columns_offset_by_half_a_line_are_still_read_column_by_column(self):
        """When the right group is left-aligned (a column), the lines are not mixed top to bottom even though no line shares a row (two
        side-by-side text boxes)."""
        def draw(pdf, index):
            for number in range(6):
                pdf.drawString(60, 700 - number * 30, f"왼쪽 상자 {number + 1}번째 항목: 품질 방침 설명")
                pdf.drawString(320, 685 - number * 30, f"오른쪽 상자 {number + 1}번째 항목: 교육 일정 안내")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.text.index("왼쪽 상자 6번째") < result.text.index("오른쪽 상자 1번째")

    def test_left_aligned_list_under_a_centred_heading_is_not_taken_for_right_aligned_lines(self, monkeypatch):
        """The table of contents on a cover (left-aligned entries under a centred '【목차】') is not right-aligned lines even when the
        right end of an entry lines up with the heading. Only the heading has a ragged left edge. The lines were mixed top to bottom
        and the team name and date slipped in between the contents entries (real agenda PDF)."""
        from opendockit.extractors import pdf as pdf_module

        original = pdf_module._read_page

        def read_left_block_first(page, *args):
            read = original(page, *args)
            read.lines.sort(key=lambda line: (line.x0 > 300, line.top))  # left of the cover → contents (the flow of the real PDF)
            return read

        monkeypatch.setattr(pdf_module, "_read_page", read_left_block_first)
        items = [("I.", "사업 과제 개요"), ("II.", "수혜 기업 개요"), ("III.", "기금 집행 목적")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 20)
            pdf.drawString(66, 630, "냉매 주입 테스트 장비 구축 지원")
            pdf.setFont(_FONT, 16)
            pdf.drawString(89, 425, "HVAC사업팀")
            pdf.drawString(89, 390, "2026. 05.")
            pdf.setFont(_FONT, 12)
            for number, (mark, item) in enumerate(items):
                pdf.drawString(398, 495 - number * 21.6, mark)
                pdf.drawString(420, 495 - number * 21.6, item)
            right = 420 + pdf.stringWidth(items[0][1], _FONT, 12)
            pdf.setFont(_FONT, 14)
            pdf.drawString(right - pdf.stringWidth("【목차】", _FONT, 14), 527, "【목차】")

        result = convert(_korean_pdf(draw), "a.pdf")

        order = [result.text.index(text) for text in ("HVAC사업팀", "2026. 05.", "【목차】", "I. 사업 과제 개요", "III. 기금 집행 목적")]
        assert order == sorted(order)


class TestInfoStrings:
    """Decoding the title bytes of the document information (Info). The standard is UTF-16BE or PDFDocEncoding, but some generators
    put a Korean title in as raw UTF-8 or EUC-KR bytes, and reading it as PDFDocEncoding gave 'íŁœê¸• ì€œëª©'."""

    @staticmethod
    def _pdf_titled(raw: bytes) -> bytes:
        from io import BytesIO

        from reportlab.pdfgen import canvas

        placeholder = "A" * len(raw)  # swapped in at the same length, so the positions in the cross-reference table stay the same
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setTitle(placeholder)
        pdf.drawString(72, 700, "hello")
        pdf.save()
        return buffer.getvalue().replace(f"({placeholder})".encode(), b"(" + raw + b")")

    @pytest.mark.parametrize("raw, title", [
        ("한글 제목".encode(), "한글 제목"),
        ("한글 제목".encode("euc_kr"), "한글 제목"),
        (b"Caf\xe9 report", "Café report"),
        (b"Opie\x9bka", "Opiełka"),  # ł (0x9B) in PDFDocEncoding. Read as a CP949 extension character it gives 'Opie쌽a'
    ])
    def test_info_title_is_decoded_from_its_bytes(self, raw, title):
        data = self._pdf_titled(raw)

        result = convert(data, "a.pdf")

        assert result.metadata["title"] == title


class TestSidewaysText:
    """Text drawn rotated 90 degrees within a page (an identifier in the margin of a paper, a table laid sideways on a portrait page)
    was turned by pdfminer into one line per character, so flipped one-character lines like '6\\n2\\n0\\n2' slipped into the middle
    of a body paragraph or became a table of single characters."""

    def test_margin_stamp_is_one_line_and_does_not_split_the_body(self):
        def draw(pdf, index):
            for line in range(6):
                pdf.drawString(90, 700 - line * 14, f"본문 {line + 1}번째 줄입니다. 내용이 이어집니다.")
            pdf.saveState()
            pdf.translate(40, 560)
            pdf.rotate(90)
            pdf.setFont("Helvetica", 14)
            pdf.drawString(0, 0, "arXiv:2609.12345v1 [cs.LG] 30 Sep 2026")
            pdf.restoreState()

        result = convert(_korean_pdf(draw), "a.pdf")

        body = "\n".join(f"본문 {line + 1}번째 줄입니다. 내용이 이어집니다." for line in range(6))
        assert body in result.text
        assert "arXiv:2609.12345v1 [cs.LG] 30 Sep 2026" in result.text

    @pytest.mark.parametrize("angle, origin", [(90, (500, 100)), (-90, (100, 740))])
    def test_page_of_sideways_lines_is_read_line_by_line_in_order(self, angle, origin):
        def draw(pdf, index):
            pdf.translate(*origin)
            pdf.rotate(angle)
            pdf.setFont("Helvetica", 12)
            for line in range(6):
                pdf.drawString(0, -line * 18, f"Sideways line {line} with several words")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "\n".join(f"Sideways line {line} with several words" for line in range(6)) in result.text
        assert "|" not in result.text
        assert not result.warnings


class TestLinesDrawnInPieces:
    """Some generators draw one line in pieces, in an order different from the visible one (LibreOffice when it outputs Hangul, digits
    and spaces in different fonts). pdfminer groups into one line only characters that are neighbours in drawing order, so every
    piece became a line and the paragraph was shuffled piece by piece."""

    _LINES = [("본 보고서는 2026년 3분기 사업 운영 실적과 ", "4분기 계획을 정리한 것이다. 3분기"),
              ("매출은 전년 동기 대비 12.4% 증가한 ", "482억 원이며, 영업이익은 37억"),
              ("원으로 영업이익률 7.7%를 ", "기록하였다.")]

    def _draw_pieces(self, pdf, x, top, gap=0.0):
        """Draw the head piece of each of `_LINES` first, line by line, then the tail pieces at the end of their head piece (+`gap`)."""
        for index, (head, _) in enumerate(self._LINES):
            pdf.drawString(x, top - index * 14, head)
        for index, (head, tail) in enumerate(self._LINES):
            pdf.drawString(x + pdf.stringWidth(head, _FONT, 10) + gap, top - index * 14, tail)

    def test_line_pieces_that_touch_on_one_baseline_are_read_as_one_line(self):
        def draw(pdf, index):
            pdf.drawString(72, 740, "1. 개요")
            self._draw_pieces(pdf, 72, 700)
            pdf.drawString(72, 630, "다음 문단은 한 번에 그려진 줄입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("1. 개요\n\n" + "\n".join(head + tail for head, tail in self._LINES)
                                           + "\n\n다음 문단은 한 번에 그려진 줄입니다.")

    def test_page_with_more_lines_than_box_grouping_allows_is_not_grouped_twice(self, monkeypatch):
        """A page whose count estimated from pdfium chunks is within the limit but whose pdfminer lines exceed it (fuzzing sample:
        estimate under 2,000, 2,098 lines) grouped the text boxes on the first read and grouped them again after joining the pieces,
        spending the quadratic time twice (48 s for a 20 KB page). When the lines exceed the limit at regrouping, they are not grouped."""
        from pdfminer.layout import LTLayoutContainer

        from opendockit.extractors import pdf as pdf_module

        def draw(pdf, index):
            self._draw_pieces(pdf, 72, 700)

        calls = []
        group_textboxes = LTLayoutContainer.group_textboxes
        monkeypatch.setattr(LTLayoutContainer, "group_textboxes", lambda *args: calls.append(1) or group_textboxes(*args))
        monkeypatch.setattr(pdf_module, "_text_line_pieces", lambda rects: 0)
        monkeypatch.setattr(pdf_module, "_MAX_GROUPED_TEXT_PIECES", 2)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert len(calls) == 1, "before the fix the text boxes were grouped once more after the pieces were joined"
        assert result.sections[0].text == "\n".join(head + tail for head, tail in self._LINES)

    def test_pieces_apart_on_the_same_row_are_left_as_they_are(self):
        """Pieces that do not touch (the next column, the next cell of a table) are not one line. They are not joined; each piece stays
        a line."""
        def draw(pdf, index):
            self._draw_pieces(pdf, 72, 700, gap=30)

        result = convert(_korean_pdf(draw), "a.pdf")

        lines = result.sections[0].text.split("\n")
        assert all(head.strip() in lines and tail in lines for head, tail in self._LINES)

    def test_piece_ending_with_a_footnote_mark_is_joined_to_the_next_piece(self):
        """A piece ending with a footnote number (a small raised digit) was not joined to the next piece, because the two touching
        characters differ in size and baseline, and the paragraph was shuffled piece by piece ('102,110¹' followed on the next line by
        '백만원으로 …'). The measurement uses the ordinary character before the footnote number."""
        lines = [("2025년 상반기 연결 매출액은 102,110", "1", "백만원으로 전년 동기 대비 증가하였으며,"),
                 ("영업이익은 8,870", "2", "백만원을 기록하였다. 부채비율은 135"),
                 ("%로 전년 말 대비 소폭 상승하였다. 해외 매출 비중은 42.5", "3", "%로 확대되었다.")]

        def draw(pdf, index):
            pdf.drawString(72, 740, "1. 재무 현황")
            for number, (head, mark, _) in enumerate(lines):
                pdf.setFont(_FONT, 10)
                pdf.drawString(72, 700 - number * 14, head)
                pdf.setFont(_FONT, 5.8)
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10), 704.2 - number * 14, mark)
            pdf.setFont(_FONT, 10)
            for number, (head, mark, tail) in enumerate(lines):
                x = 72 + pdf.stringWidth(head, _FONT, 10) + pdf.stringWidth(mark, _FONT, 5.8)
                pdf.drawString(x, 700 - number * 14, tail)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == "1. 재무 현황\n\n" + "\n".join(f"{head}^{mark}{tail}" for head, mark, tail in lines)

    def test_footnote_mark_drawn_as_its_own_piece_joins_both_neighbours(self):
        """A superscript footnote number ('1)', 0.8 times the body) that LibreOffice outputs in a variable font is a piece separate from
        both the text before it and the text after it, so the paragraph split in three around the number and one line was pushed to
        the end of the page."""
        lines = [("국내 반도체 수출액은 전년 대비 43.9% 증가하였다.", "1)", " 특히 메모리 수요가 늘었다."),
                 ("시스템 반도체 역시 소폭 증가하였다.", "2)", " 다만 중국 의존도가 높다.")]

        def draw(pdf, index):
            for step in range(3):  # drawn separately in the order: text before → number → text after
                for number, (head, mark, tail) in enumerate(lines):
                    y = 700 - number * 16
                    x = 72 + pdf.stringWidth(head, _FONT, 10.5) + (pdf.stringWidth(mark, _FONT, 8.405) if step == 2 else 0)
                    pdf.setFont(_FONT, 8.405 if step == 1 else 10.5)  # the size 0.8 times the body, with a decimal rounding error
                    pdf.drawString(72 if step == 0 else x, y + (4 if step == 1 else 0), (head, mark, tail)[step])

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "\n".join(f"{head}^{{{mark}}}{tail}" for head, mark, tail in lines)

    def test_one_symbol_drawn_later_in_a_fallback_font_joins_its_line(self):
        """A single symbol drawn later in a fallback font ('₩', '¹') has no word character and was not taken for a piece to join, and
        since the bottom of the character box differs from font to font the baselines looked off as well, so the sentence split into
        three paragraphs '(' / '₩' / '23,100,000, …'. The baseline is measured at the character origin."""
        lines = [("합계금액: 일금 이천삼백십만원정 (", "\u00b9", "23,100,000, VAT 포함)"), ("김연구", "\u00b2", ", 이교수 외 2인")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            pdf.drawString(72, 740, "1. 견적 요약")
            for number, (head, _, _) in enumerate(lines):
                pdf.drawString(72, 700 - number * 14, head)
            for number, (head, mark, tail) in enumerate(lines):
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10) + pdf.stringWidth(mark, "Courier", 10), 700 - number * 14, tail)
            pdf.setFont("Courier", 10)  # the bottom (descent) differs from the Korean font
            for number, (head, mark, _) in enumerate(lines):
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10), 700 - number * 14, mark)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == "1. 견적 요약\n\n" + "\n".join(f"{head}{mark}{tail}" for head, mark, tail in lines)

    def test_piece_after_a_separately_drawn_space_joins_its_line(self):
        """LibreOffice draws the space before a highlight separately, in another font and on another baseline, leaving a gap of one
        space width (3pt) between the pieces before and after. A piece holding only a space could not be a piece to join and the gap
        exceeded the touching tolerance, so the line split in two and the line order of the paragraph became 'line 1 left → line 2 →
        line 1 right'."""
        lines = [("본 사업의 감축 효과는 산식 기여도", "반영 결과를 기준으로 산정하며"), ("재생용기 투입량과 회수율", "80%를 곱해 구한다."),
                 ("나머지 값은 시장평균", "자료를 쓴다.")]

        def draw(pdf, index):
            pdf.drawString(72, 740, "1. 산정 방법")
            for number, (head, _) in enumerate(lines):
                pdf.drawString(72, 700 - number * 14, head)
            pdf.setFont("Helvetica", 10)
            for number, (head, _) in enumerate(lines):
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10), 701 - number * 14, " ")
            pdf.setFont(_FONT, 10)
            for number, (head, tail) in enumerate(lines):
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10) + pdf.stringWidth(" ", "Helvetica", 10), 700 - number * 14, tail)
            pdf.drawString(72, 630, "다음 문단은 한 번에 그려진 줄입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("1. 산정 방법\n\n" + "\n".join(f"{head} {tail}" for head, tail in lines)
                                           + "\n\n다음 문단은 한 번에 그려진 줄입니다.")

    def test_fallback_font_piece_next_to_a_space_joins_its_line(self):
        """A Chinese character that LibreOffice draws later in a fallback font ('先', '中') sat 2.3pt after the space ending the piece
        before it and 0.7pt before the space starting the piece after it, so it was not joined and the sentence split into three
        paragraphs '크레딧' / '先' / '매입 후'. With a space character in between, pieces are joined up to one space width apart."""
        lines = [("인증을 받은 사업을 대상으로 잠재 감축 크레딧 ", "先", " 매입 후 투자자에게 판매한다."),
                 ("밀폐 공간 작업 ", "中", " 질식사고는 연 평균 사망사고 발생률이 높다.")]

        def draw(pdf, index):
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.cidfonts import UnicodeCIDFont

            pdfmetrics.registerFont(UnicodeCIDFont("HYGothic-Medium"))
            pdf.drawString(72, 740, "1. 사업 개요")
            for number, (head, _, _) in enumerate(lines):
                pdf.drawString(72, 700 - number * 14, head)
            for number, (head, mark, tail) in enumerate(lines):
                x = 72 + pdf.stringWidth(head, _FONT, 10) + 2.3 + pdf.stringWidth(mark, "HYGothic-Medium", 10) + 0.7
                pdf.drawString(x, 700 - number * 14, tail)
            pdf.setFont("HYGothic-Medium", 10)
            for number, (head, mark, _) in enumerate(lines):
                pdf.drawString(72 + pdf.stringWidth(head, _FONT, 10) + 2.3, 700 - number * 14, mark)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == "1. 사업 개요\n\n" + "\n".join(f"{head}{mark}{tail}" for head, mark, tail in lines)

    def test_pieces_of_one_baseline_drawn_font_by_font_that_overlap_are_read_as_one_line(self):
        """Cairo-style generators draw all the Hangul runs of a line first in one font and the digit runs later in another font.
        pdfminer made several pieces whose x ranges interleave ('보호법」제 조 제 조', '15 , 17 , 제 조'), which became heading pieces.
        Pieces on the same baseline whose x ranges overlap have their characters put back in x order and are read as one line."""
        lines = ["개인정보 보호법 제15조, 제17조, 제22조 및 제24조에 따라 아래와 같이 동의를 받습니다.",
                 "국세기본법 제81조의13 제1항 제7호의 과세정보 12건과 7건을 2024년 3월 5일에 확인하였다.",
                 "위 사항을 확인하였습니다."]

        result = convert(builder.pdf_with_font_runs(lines), "a.pdf")

        assert result.sections[0].text == "\n".join(lines)


def test_dash_that_arrives_as_a_soft_hyphen_is_kept_as_a_hyphen():
    """The '–' of a Korean CID font that is not embedded (UniKS-UCS2-H) comes out of pdfminer as U+00AD (soft hyphen), and the result
    clean-up deleted it as an invisible character, so the numbers ran together ('2025–2026' → '20252026'). It is a drawn character,
    so it is kept as '-'. The same goes for table cells."""
    def draw(pdf, index):
        pdf.drawString(72, 740, "사업기간: 2025–2026년 (24개월)")
        _draw_cells(pdf, [72, 200, 330], 700, 20, [["구분", "기간"], ["1차", "2025–2026년"]])
        for y in (700, 680, 660):
            pdf.line(72, y, 330, y)
        for x in (72, 200, 330):
            pdf.line(x, 700, x, 660)

    text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

    assert "사업기간: 2025-2026년 (24개월)" in text
    assert "| 1차 | 2025-2026년 |" in text


@pytest.mark.parametrize("strength", [40, 128], ids=["rc4", "aes"])
def test_copy_protected_pdf_with_a_non_embedded_korean_cid_font_keeps_its_text(strength):
    """In a PDF with only an owner password (copy protection) reportlab writes the CIDSystemInfo strings of the font dictionary
    unencrypted. pdfminer 'decrypted' them and could not find the mapping table with the garbled Ordering, so every character became
    `(cid:n)` and the page was left with an empty table and a single warning."""
    from reportlab.lib.pdfencrypt import StandardEncryption

    def draw(pdf, index):
        pdf.drawString(72, 760, "당사는 2025년 매출액 1조 4,567억원을 기록하였습니다.")
        _draw_cells(pdf, [72, 200, 330], 720, 20, [["구분", "금액"], ["매출액", "1,456,789"]])
        for y in (720, 700, 680):
            pdf.line(72, y, 330, y)
        for x in (72, 200, 330):
            pdf.line(x, 720, x, 680)

    encrypt = StandardEncryption("", ownerPassword="own", canCopy=0, strength=strength)
    result = convert(_korean_pdf(draw, encrypt=encrypt), "a.pdf")

    assert "당사는 2025년 매출액 1조 4,567억원을 기록하였습니다." in result.sections[0].text
    assert "| 매출액 | 1,456,789 |" in result.sections[0].text
    assert result.warnings == []


def test_wrongly_decrypted_font_info_that_happens_to_be_printable_is_still_repaired(monkeypatch):
    """The repair above recognised broken font info by 'not printable ASCII'. AES uses a random IV, and now and then the wrongly
    decrypted bytes were all printable (`b'"w#"Q'`, `b'(|>%Ue'`): about 1 conversion in 400 of the same document lost its whole
    text. The bytes pdfminer hands back are replaced here by that printable garbage so the case is reproduced every time."""
    from pdfminer.pdftypes import resolve1 as real_resolve
    from reportlab.lib.pdfencrypt import StandardEncryption

    from opendockit.extractors import pdf as extractor

    def draw(pdf, index):
        pdf.drawString(72, 760, "당사는 2025년 매출액 1조 4,567억원을 기록하였습니다.")

    def resolve_with_printable_garbage(value, *args, **kwargs):
        resolved = real_resolve(value, *args, **kwargs)
        if isinstance(resolved, dict) and "Ordering" in resolved and resolved.get("Ordering") != b"Korea1":
            resolved["Registry"], resolved["Ordering"] = b'"w#"Q', b"(|>%Ue"
        return resolved

    monkeypatch.setattr(extractor, "resolve1", resolve_with_printable_garbage)
    encrypt = StandardEncryption("", ownerPassword="own", canCopy=0, strength=128)

    result = convert(_korean_pdf(draw, encrypt=encrypt), "a.pdf")

    assert "당사는 2025년 매출액 1조 4,567억원을 기록하였습니다." in result.sections[0].text
    assert result.warnings == []


class TestListMarkers:
    """A PDF exported with LibreOffice from a Word list (List Number, List Bullet) places the numbers and bullets apart from the item
    text, so a paragraph of numbers only ('1.\\n\\n2.\\n\\n3.') and the item text came apart. The numbers of the footnote area did
    the same ('1\\n2\\n3\\n\\nK-IFRS …')."""

    _ITEMS = [["모든 임직원은 업무용 노트북에 회사가 지정한 보안 프로그램(백신, DLP)을 설치하여야", "하며, 임의로 삭제하거나 비활성화하여서는 아니 된다."],
              ["외부 저장매체(USB, 외장하드 등)의 사용은 원칙적으로 금지한다. 다만 업무상 불가피한", "경우 소속 부서장의 승인을 받아 등록한 매체에 한하여 사용할 수 있다."],
              ["개인정보가 포함된 파일은 반드시 암호화하여 저장하고, 외부로 전송할 때에는", "사내 보안 메일 시스템을 이용한다."]]

    def _draw_list(self, pdf, markers, x=72, top=735, indent=18, pitch=16.5):
        """Draw all the numbers first and the item text afterwards (the same order as LibreOffice)."""
        tops = [top - pitch * sum(len(item) for item in self._ITEMS[:number]) for number in range(len(self._ITEMS))]
        for marker, item_top in zip(markers, tops, strict=True):
            pdf.drawString(x, item_top, marker)
        for item, item_top in zip(self._ITEMS, tops, strict=True):
            for number, line in enumerate(item):
                pdf.drawString(x + indent, item_top - pitch * number, line)

    @pytest.mark.parametrize("markers", [["1.", "2.", "3."], ["가)", "나)", "다)"]])
    def test_list_numbers_drawn_apart_from_their_items_lead_their_items(self, markers):
        def draw(pdf, index):
            pdf.drawString(72, 760, "다음은 주요 개정 사항이다.")
            self._draw_list(pdf, markers)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == "다음은 주요 개정 사항이다.\n\n" + "\n".join(
            f"{marker} {first}\n{second}" for marker, (first, second) in zip(markers, self._ITEMS, strict=True))

    def test_footnote_numbers_lead_their_footnotes(self):
        notes = ["K-IFRS 연결 기준, 미감사 수치.", "일회성 비용 1,200백만원 제외.", "2025년 6월 말 기준."]

        def draw(pdf, index):
            pdf.drawString(72, 760, "본문 문단입니다. 각주는 쪽 아래에 있습니다.")
            for number in range(len(notes)):
                pdf.drawString(57, 120 - number * 15.75, str(number + 1))
            for number, note in enumerate(notes):
                pdf.drawString(74, 120 - number * 15.75, note)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text.endswith("\n\n" + "\n".join(f"{number} {note}" for number, note in enumerate(notes, start=1)))

    def test_numbers_beside_lines_that_do_not_start_at_their_height_are_left_alone(self):
        """When no line starts at the height of the number (figure ticks, numbers between lines), nothing is joined."""
        def draw(pdf, index):
            for number in range(3):
                pdf.drawString(57, 700 - number * 40, str(number + 1))
                pdf.drawString(74, 680 - number * 40, f"{number + 1}번 눈금 아래 설명 줄입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert all(f"{number} {number}번" not in result.text for number in range(1, 4))

    @pytest.mark.parametrize("bullet", ["", "", "", "", "", ""])
    def test_symbol_font_bullets_are_lone_bullets(self, bullet):
        """Symbol and Wingdings bullets come out as private-use-area characters (U+F0B7 and so on). When not taken for bullets they came
        apart from the item text. reportlab's CID fonts cannot draw these characters, so only the detection is checked."""
        from opendockit.extractors import pdf as pdf_module

        matched = (pdf_module._LONE_BULLET.match(bullet), pdf_module._BULLET_START.match(bullet + " 항목"))

        assert all(matched)


class TestSuperscriptByPosition:
    """A PDF has no information saying that a character is a script. A character smaller than the other characters of its line, with
    its baseline shifted up or down, is taken for a superscript or subscript."""

    @staticmethod
    def _draw_line(pdf, y, pieces):
        """pieces: (text, font size, baseline shift), drawn one after another from the left."""
        x = 72.0
        for text, size, rise in pieces:
            pdf.setFont("Helvetica", size)
            pdf.drawString(x, y + rise, text)
            x += pdf.stringWidth(text, "Helvetica", size)

    def test_small_raised_and_lowered_characters_are_marked(self):
        def draw(pdf, index):
            self._draw_line(pdf, 700, [("The area is 5x10", 11, 0), ("6", 7, 4), (" m", 11, 0), ("2", 7, 4), (" and CO", 11, 0),
                                       ("2", 7, -2.5), (" stays, 10", 11, 0), ("-3", 7, 4), (" too.", 11, 0)])
            self._draw_line(pdf, 680, [("An ordinary second line of body text follows here.", 11, 0)])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "The area is 5x10^6 m^2 and CO_2 stays, 10^{-3} too." in result.text
        assert "An ordinary second line of body text follows here." in result.text

    def test_browser_scripts_five_sixths_the_size_and_shifted_far_are_marked(self):
        """The sup and sub printed by a browser (Chrome) are 5/6 of the character size (0.833 times) with the baseline shifted by 0.37
        and 0.3 em. They missed the 0.8-times criterion, so 10⁶ was read as '106'."""
        def draw(pdf, index):
            self._draw_line(pdf, 700, [("Capacity is 10", 11, 0), ("6", 9.16, 4.05), (" per day, error 10", 11, 0), ("-3", 9.16, 4.05),
                                       (" and CO", 11, 0), ("2", 9.16, -3.3), (" over 1,250 m", 11, 0), ("2", 9.16, 4.05), (".", 11, 0)])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "Capacity is 10^6 per day, error 10^{-3} and CO_2 over 1,250 m^2." in result.text

    def test_slightly_smaller_text_shifted_only_a_little_is_not_marked(self):
        """A character over 0.8 times the size is a script only when its baseline is shifted a lot. Slightly smaller text raised a
        little (design emphasis) is left as it is."""
        def draw(pdf, index):
            self._draw_line(pdf, 700, [("Total amount 12,345", 11, 0), (" KRW", 9.16, 2.4), (" as of today", 11, 0)])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "Total amount 12,345 KRW as of today" in result.text

    def test_smaller_text_on_the_same_baseline_is_not_marked(self):
        def draw(pdf, index):
            self._draw_line(pdf, 700, [("Total amount 12,345", 11, 0), (" won (tax included)", 7, 0), (" as of today", 11, 0)])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "Total amount 12,345 won (tax included) as of today" in result.text

    def test_small_raised_korean_words_are_not_marked(self):
        """Design documents align the small text beside a title and raised units to the top. By position alone they are superscripts,
        giving '12,345^{백만원}'. A run containing Hangul or Chinese characters is not an exponent or a chemical formula, so it is not
        marked."""
        def draw(pdf, index):
            x = 72.0
            for text, size, rise in [("총 사업비 12,345", 14, 0), ("백만원", 8, 4.5), (", 면적 1,250m", 14, 0), ("2", 8, 4.5)]:
                pdf.setFont(_FONT, size)
                pdf.drawString(x, 700 + rise, text)
                x += pdf.stringWidth(text, _FONT, size)
            pdf.setFont(_FONT, 11)
            pdf.drawString(72, 670, "본문 줄입니다. 내용이 이어집니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "총 사업비 12,345백만원, 면적 1,250m^2" in result.text

    def test_footnote_mark_on_a_tightly_spaced_line_is_marked_not_merged_into_the_number(self):
        """At a line spacing of 100% (the LibreOffice default) the bottom of a line and the top of the next line are less than 1pt
        apart. Picking the characters of a line by top coordinate ±1pt mixed in characters of the next line and the script check gave
        up, so the footnote number stuck to the number ('부채비율은 135^1%' → '1351%')."""
        def draw(pdf, index):
            x = 72.0
            for text, size, rise in [("상반기 부채비율은 135", 10, 0), ("1", 5.8, 4.2), ("%이다. 이는 차입금 증가에 따른 것이다.", 10, 0)]:
                pdf.setFont(_FONT, size)
                pdf.drawString(x, 700 + rise, text)
                x += pdf.stringWidth(text, _FONT, size)
            pdf.setFont(_FONT, 10)
            pdf.drawString(72, 689, "하반기에는 영업현금흐름 개선에 따라 점진적으로 낮아질 것으로 전망된다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "상반기 부채비율은 135^1%이다. 이는 차입금 증가에 따른 것이다.\n하반기에는" in result.text

    def test_words_placed_separately_on_lines_as_tall_as_the_font_keep_their_lines(self):
        """On a page where every word is placed separately and there are no spaces (a target of spacing restoration), when the line
        spacing equals the character size the characters of the next line were mixed into the line's text and the characters of two
        lines were woven into one ('자혼합동냉차매 공 주장입 …')."""
        lines = [["자동차", "공장", "냉매", "주입", "라인을", "모사하는", "장비"],
                 ["혼합냉매", "주입", "장비를", "개발하여", "상용화", "추진", "검증해"],
                 ["최근", "환경", "규제", "및", "성능", "요건", "강화에", "따라"]]

        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            for number, words in enumerate(lines):
                x = 72.0
                for word in words:
                    pdf.drawString(x, 700 - number * 16, word)
                    x += len(word) * 16 * 0.865 + 16 * 0.25  # advance narrower than the width the font states (1em): the words overlap

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == "\n".join(" ".join(words) for words in lines)


def _draw_cells(pdf, columns: list[float], top: float, height: float, rows: list[list[str]]) -> None:
    """Draw only the text of the table cells (each test draws its own rules). `columns` holds the x of the column borders, the row
    height is `height`."""
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            pdf.drawString(columns[column_index] + 6, top - (row_index + 1) * height + 7, value)


class TestRuledTables:
    """Ruled-table detection: a table filling the page, the various shapes of tables without left and right borders."""

    def test_grid_filling_the_whole_page_is_a_table_not_a_page_frame(self):
        """A table over 80% of the page area was discarded as a 'page frame' whatever its number of rows and columns, so the cell text
        of a requirements table filling the page scattered column by column. Only a frame box with hardly any cells is discarded."""
        columns, top, height = [20, 100, 200, 575], 822, 20
        rows = [["번호", "요구사항", "설명"],
                *([f"REQ-{index:03d}", f"요구사항 {index}", f"사용자는 {index}번 화면에서 조회 조건을 입력하고 결과를 내려받을 수 있어야 한다."]
                  for index in range(1, 40))]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 번호 | 요구사항 | 설명 |\n| --- | --- | --- |\n| REQ-001 | 요구사항 1 | 사용자는 1번 화면에서" in result.text
        assert "| REQ-039 | 요구사항 39 | 사용자는 39번 화면에서" in result.text

    def test_page_frame_split_into_two_boxes_is_still_layout(self):
        """A frame around the whole page divided into boxes by a single vertical line (side menu + body) is not a grid. The text is
        read as body text."""
        def draw(pdf, index):
            pdf.rect(20, 20, 555, 802)
            pdf.line(160, 20, 160, 822)
            pdf.drawString(30, 780, "옆 메뉴 안내")
            pdf.drawString(180, 780, "본문 첫 문장입니다.")
            pdf.drawString(180, 760, "본문 둘째 문장입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "|" not in result.text
        assert "본문 첫 문장입니다.\n본문 둘째 문장입니다." in result.text

    def test_ruled_table_overdrawn_with_glyph_outlines_keeps_its_columns(self):
        """A document that draws the same text again over the text layer as filled polygons (glyph outlines) has the vertical edges of
        the strokes densely lined up across the whole table width. pdfplumber chained nearby vertical lines together and gathered the
        cell border lines into one position, so the columns collapsed and the cell text split like '세부내 | 용' (the 'Baseline types'
        table of a design-tool guidebook)."""
        columns, top, height = [70, 230, 470], 700, 24
        rows = [["구분", "세부내용"], ["시장 평균", "가중 평균 대비"], ["법적 기준", "규제 기준 대비"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)
            for line in range(len(rows)):
                y = top - (line + 1) * height + 7
                for x in range(74, 466, 2):  # one stroke: a filled polygon 0.8pt wide
                    path = pdf.beginPath()
                    path.moveTo(x, y)
                    path.lineTo(x + 0.8, y)
                    path.lineTo(x + 0.8, y + 6)
                    path.lineTo(x, y + 7)
                    path.close()
                    pdf.drawPath(path, stroke=0, fill=1)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("| 구분 | 세부내용 |\n| --- | --- |\n| 시장 평균 | 가중 평균 대비 |\n"
                                           "| 법적 기준 | 규제 기준 대비 |")

    def test_open_sided_table_found_from_two_rule_widths_is_kept_once(self):
        """When the ends of the horizontal rules of an open-sided table are off by about 1pt, the width groups split in two and the
        same table was found twice. Each of the two tables saw the other as 'a box containing another table' and both were discarded,
        so the two-column definition table scattered line by line ('총배출량 | 순배출량' in a design-tool guidebook)."""
        columns, top, height = [70, 200, 470], 700, 22
        rows = [["구분", "정의"], ["총배출량", "실제로 배출한 총량"], ["순배출량", "감축량을 차감한 배출량"], ["기준", "환경부 명세서"],
                ["감축 수단", "직접감축, 자가발전"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                right = columns[-1] + (0.7 if line % 2 else 0)  # horizontal rules with ends 0.7pt apart: two width groups of 3 each
                pdf.line(columns[0], top - line * height, right, top - line * height)
            pdf.line(columns[1], top, columns[1], top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("| 구분 | 정의 |\n| --- | --- |\n| 총배출량 | 실제로 배출한 총량 |\n"
                                           "| 순배출량 | 감축량을 차감한 배출량 |\n| 기준 | 환경부 명세서 |\n"
                                           "| 감축 수단 | 직접감축, 자가발전 |")

    _OPEN_COLUMNS = [70, 170, 270, 370, 470]

    def test_open_sided_table_whose_column_rules_are_drawn_row_by_row_keeps_its_outer_columns(self):
        """When the inner vertical rules are drawn broken at every row (0.25pt gaps), one vertical rule spans a single row only, so it
        does not contain 3 or more horizontal rules and the open-sided table was not found. Only the columns between the vertical
        rules became a table and the '구분' and '순위' columns scattered as lines outside the table."""
        columns, top, height = self._OPEN_COLUMNS, 700, 22
        rows = [["구분", "방식", "내용", "순위"], ["가격", "일반시장", "평균 가격", "1"], ["비용", "대체방식", "대체 비용", "2"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns[1:-1]:
                for line in range(len(rows)):
                    pdf.line(x, top - line * height - 0.12, x, top - (line + 1) * height + 0.12)
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("| 구분 | 방식 | 내용 | 순위 |\n| --- | --- | --- | --- |\n"
                                           "| 가격 | 일반시장 | 평균 가격 | 1 |\n| 비용 | 대체방식 | 대체 비용 | 2 |")

    def test_open_sided_table_with_white_filled_header_cells_keeps_its_outer_columns(self):
        """With a borderless white-filled rectangle under each header cell, their outer edges looked like the left and right borders of
        the header row, so the horizontal rules drawn broken at every cell did not connect and the table looked like a 'closed table'.
        The first column (vertically merged) and the last column were empty and their text scattered outside the table (a real manual
        <표 1-1>)."""
        columns, top, height = self._OPEN_COLUMNS, 700, 22
        rows = [["구분", "방식", "내용", "순위"], ["가격", "일반시장", "평균 가격", "1"], ["", "공급의사", "공급 가격", "2"],
                ["비용", "대체방식", "대체 비용", "3"], ["", "추가투입", "추가 원가", "4"]]

        def draw(pdf, index):
            pdf.setFillColorRGB(1, 1, 1)
            for left, right in zip(columns, columns[1:], strict=False):
                pdf.rect(left, top - height, right - left, height, stroke=0, fill=1)
            pdf.setFillColorRGB(0, 0, 0)
            for line in range(len(rows) + 1):
                start = columns[1] if line in (2, 4) else columns[0]  # horizontal rules that avoid the merged cells of the first column
                for left, right in zip(columns, columns[1:], strict=False):
                    if left >= start:
                        pdf.line(left, top - line * height, right, top - line * height)
            for x in columns[1:-1]:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == (
            "| 구분 | 방식 | 내용 | 순위 |\n| --- | --- | --- | --- |\n| 가격 | 일반시장 | 평균 가격 | 1 |\n"
            "| 가격 | 공급의사 | 공급 가격 | 2 |\n| 비용 | 대체방식 | 대체 비용 | 3 |\n| 비용 | 추가투입 | 추가 원가 | 4 |")

    def test_open_sided_table_whose_rules_are_drawn_cell_by_cell_keeps_its_outer_columns(self):
        """Some generators draw the horizontal rules of a table without left and right borders broken at every cell. Grouping the
        horizontal rules by (start, end) leaves only cell-wide lines, so the full-width table was not found, only the columns between
        the vertical rules became a table and the '구분' and '2025년' columns scattered."""
        columns, top, height = self._OPEN_COLUMNS, 700, 22
        rows = [["구분", "2023년", "2024년", "2025년"], ["매출액", "1,020", "1,142", "1,284"], ["영업이익", "70", "84", "97"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                for left, right in zip(columns, columns[1:], strict=False):
                    pdf.line(left, top - line * height, right, top - line * height)
            for x in columns[1:-1]:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == ("| 구분 | 2023년 | 2024년 | 2025년 |\n| --- | --- | --- | --- |\n"
                                           "| 매출액 | 1,020 | 1,142 | 1,284 |\n| 영업이익 | 70 | 84 | 97 |")

    def test_open_sided_table_with_vertically_merged_first_column_is_kept_whole(self):
        """When the first column (category) is vertically merged, the horizontal rules have two widths (the full width, and the width
        that avoids the merged cell). A partial table was found separately from the narrow rules and the whole table containing it
        was discarded, so the category column went outside the table."""
        columns, top, height = self._OPEN_COLUMNS, 700, 22
        rows = [["사업부", "항목", "2024년", "2025년"], ["센서", "매출", "612", "701"], ["", "영업이익", "48", "58"],
                ["", "인원", "120", "131"], ["모듈", "매출", "388", "421"], ["", "영업이익", "27", "29"], ["", "인원", "85", "88"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0] if line in (0, 1, 4, 7) else columns[1], top - line * height, columns[-1], top - line * height)
            for x in columns[1:-1]:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == (
            "| 사업부 | 항목 | 2024년 | 2025년 |\n| --- | --- | --- | --- |\n| 센서 | 매출 | 612 | 701 |\n"
            "| 센서 | 영업이익 | 48 | 58 |\n| 센서 | 인원 | 120 | 131 |\n| 모듈 | 매출 | 388 | 421 |\n"
            "| 모듈 | 영업이익 | 27 | 29 |\n| 모듈 | 인원 | 85 | 88 |")

    def test_closed_table_with_merged_first_column_under_a_two_column_header_is_kept_whole(self):
        """In a table closed on all four sides whose first column is vertically merged and whose header '구분' spans columns 1–2, the
        vertical rule between columns 1 and 2 is missing in the header row, so the horizontal rules avoiding the merged cells looked
        like 'a table open on the left'. A partial table from column 2 on was found separately and the whole table containing it was
        discarded, so the category column ('자산', '부채', '자본') came out as lines outside the table."""
        columns, top, height = self._OPEN_COLUMNS, 700, 22
        rows = [["구분", "", "2024년", "2025년"], ["자산", "유동자산", "100", "110"], ["", "비유동자산", "300", "320"],
                ["부채", "유동부채", "50", "55"], ["", "비유동부채", "120", "118"], ["자본", "자본금", "200", "200"],
                ["", "이익잉여금", "30", "57"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0] if line % 2 or line == 0 else columns[1], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top - height if x == columns[1] else top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text == (
            "| 구분 | | 2024년 | 2025년 |\n| --- | --- | --- | --- |\n| 자산 | 유동자산 | 100 | 110 |\n"
            "| 자산 | 비유동자산 | 300 | 320 |\n| 부채 | 유동부채 | 50 | 55 |\n| 부채 | 비유동부채 | 120 | 118 |\n"
            "| 자본 | 자본금 | 200 | 200 |\n| 자본 | 이익잉여금 | 30 | 57 |")

    def test_vertical_merge_is_repeated_in_every_row_when_another_row_merges_columns(self):
        """The rows and columns in which to repeat a vertically merged value are found by centre coordinates, but merged cells inflated
        the row and column boxes (the '수입' cell spanning three rows became the height of the first row, and the '합계' cell spanning
        two columns became the width of the first column), so the centres were off. The category of the third row was empty."""
        columns, top, height = [70, 150, 270, 370, 470], 700, 22
        rows = [["구분", "세부", "1분기", "2분기"], ["수입", "매출", "100", "110"], ["", "기타", "5", "7"], ["", "이자", "1", "2"],
                ["합계", "", "106", "119"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[1] if line in (2, 3) else columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * (len(rows) - 1 if x == columns[1] else len(rows)))
            _draw_cells(pdf, columns, top, height, rows)

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ("| 수입 | 매출 | 100 | 110 |\n| 수입 | 기타 | 5 | 7 |\n| 수입 | 이자 | 1 | 2 |\n| 합계 | | 106 | 119 |"
                in result.sections[0].text)

    def test_text_right_outside_a_cell_on_the_same_line_as_the_cell_text_is_kept(self):
        """A note mark right outside the right border of a table ('주1)', '원') is so close to the cell text that pdfminer groups it
        into one line with the cell text. The centre of the line is inside the cell, so the whole line was taken out of the body, and
        the cell text holds only the characters inside the cell, so the mark vanished."""
        columns, top, height = [60, 200, 330], 742, 20
        rows = [["항목", "금액(부가세 제외)"], ["서버 임차료", "12,000,000"], ["소프트웨어 사용료", "8,500,000"], ["유지보수 인건비", "24,000,000"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * len(rows))
            for number, (row, mark) in enumerate(zip(rows, ["", "주1)", "주2)", "원"], strict=True)):
                pdf.drawString(columns[0] + 4, top - number * height - 14, row[0])
                pdf.drawRightString(columns[2] - 3, top - number * height - 14, row[1])
                pdf.drawString(columns[2] + 3, top - number * height - 14, mark)
            pdf.drawString(60, top - 110, "주1) 3년 약정 기준. 주2) 연간 구독.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 서버 임차료 | 12,000,000 |\n| 소프트웨어 사용료 | 8,500,000 |\n| 유지보수 인건비 | 24,000,000 |" in result.text
        assert result.text.count("주1)") == 2
        assert result.text.count("주2)") == 2
        assert "\n원\n" in result.text

    def test_text_rotated_inside_a_cell_is_read_in_its_own_direction(self):
        """A category name written rotated 90 degrees in a vertically merged cell ('기계 설비') was read top to bottom like horizontal
        text and came out reversed ('비설<br>계기')."""
        columns, top, height = [70, 100, 220, 320], 700, 24
        rows = [["", "점검 항목", "기준"], ["", "유압 펌프 압력", "150 bar"], ["", "냉각수 온도", "25 ℃ 이하"], ["", "벨트 장력", "40 N"],
                ["", "절연 저항", "1 MΩ 이상"], ["", "접지 저항", "10 Ω 이하"]]

        def draw(pdf, index):
            for line in range(len(rows) + 1):
                pdf.line(columns[0] if line in (0, 1, 4, 6) else columns[1], top - line * height, columns[-1], top - line * height)
            for x in columns:
                pdf.line(x, top, x, top - height * len(rows))
            _draw_cells(pdf, columns, top, height, rows)
            for bottom_row, text in ((4, "기계 설비"), (6, "전기")):
                pdf.saveState()
                pdf.translate(columns[0] + 19, top - bottom_row * height + 6)
                pdf.rotate(90)
                pdf.drawString(0, 0, text)
                pdf.restoreState()

        result = convert(_korean_pdf(draw), "a.pdf")

        assert ("| 기계 설비 | 유압 펌프 압력 | 150 bar |\n| 기계 설비 | 냉각수 온도 | 25 ℃ 이하 |\n| 기계 설비 | 벨트 장력 | 40 N |\n"
                "| 전기 | 절연 저항 | 1 MΩ 이상 |\n| 전기 | 접지 저항 | 10 Ω 이하 |") in result.text


class TestBlockPlacement:
    """Tables and pictures are placed not by height alone but within the paragraph flow of the column they sit in."""

    @staticmethod
    def _grid(pdf, columns, top, height, rows):
        for line in range(len(rows) + 1):
            pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
        for x in columns:
            pdf.line(x, top, x, top - height * len(rows))
        _draw_cells(pdf, columns, top, height, rows)

    @staticmethod
    def _column(pdf, x, name, lines):
        """A column of consecutive four-line paragraphs. `lines` holds the numbers of the lines to draw (from 0)."""
        for line in lines:
            pdf.drawString(x, 760 - line * 14 - line // 4 * 14, f"{name} 단의 {line + 1}번째 줄 본문이 이어집니다.")

    def test_table_in_the_right_column_is_not_inserted_into_the_left_column(self):
        """On a two-column page the table of the right column slipped in before the first paragraph starting lower than the table (a
        paragraph of the left column)."""
        rows = [["항목", "대상", "기한"], ["보안 교육", "전 직원", "10월 6일"], ["장비 점검", "관리자", "10월 13일"]]

        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(24))
            self._column(pdf, 320, "오른쪽", [*range(4), *range(12, 24)])
            self._grid(pdf, [320, 400, 470, 540], 700, 20, rows)

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        table = "| 항목 | 대상 | 기한 |\n| --- | --- | --- |\n| 보안 교육 | 전 직원 | 10월 6일 |\n| 장비 점검 | 관리자 | 10월 13일 |"
        assert f"오른쪽 단의 4번째 줄 본문이 이어집니다.\n\n{table}\n\n오른쪽 단의 13번째 줄 본문이 이어집니다." in text
        assert text.index("왼쪽 단의 24번째") < text.index("오른쪽 단의 1번째")

    def test_table_at_the_bottom_of_the_left_column_stays_in_the_left_column(self):
        """On a two-column page the table at the very bottom of the left column has no paragraph below it and was attached to the end
        of the page (after the body of the right column). It is put after the last paragraph of its own column."""
        rows = [["지역", "목표", "실적"], ["서울", "100", "98"], ["부산", "60", "61"]]

        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(8))
            self._column(pdf, 320, "오른쪽", range(12))
            self._grid(pdf, [57, 130, 200, 270], 600, 20, rows)

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        table = "| 지역 | 목표 | 실적 |\n| --- | --- | --- |\n| 서울 | 100 | 98 |\n| 부산 | 60 | 61 |"
        assert f"왼쪽 단의 8번째 줄 본문이 이어집니다.\n\n{table}\n\n오른쪽 단의 1번째" in text

    def test_table_split_from_the_bottom_of_the_left_column_reads_before_its_rest(self):
        """In a table starting at the very bottom of the left column and carried over to the top of the right column, when the body of
        the right column ends above the table and the page number is below, the left part was attached after the body of the right
        column (before the page number) and read after the carried-over row ('합계')."""
        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(16))
            self._grid(pdf, [57, 130, 200, 270], 420, 20, [["지역", "목표", "실적"], ["서울", "100", "98"]])
            self._grid(pdf, [320, 393, 463, 533], 780, 20, [["합계", "240", "244"]])
            for line in range(8):
                pdf.drawString(320, 740 - line * 14 - line // 4 * 14, f"오른쪽 단의 {line + 1}번째 줄 본문이 이어집니다.")
            pdf.drawString(520, 40, "- 1 -")

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        order = [text.index(part) for part in ("왼쪽 단의 16번째", "| 지역 |", "| 합계 |", "오른쪽 단의 1번째", "- 1 -")]
        assert order == sorted(order)

    def test_table_in_the_right_column_is_not_inserted_before_a_short_heading_of_the_left_column(self):
        """A short subheading of the left column is less than half the table width and looked like 'narrow text beside the table', so
        the table lower in the right column slipped in before that subheading. When the left edge of the subheading lines up with the
        body of the left column, it is another column."""
        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(12))
            pdf.setFont(_FONT, 13)
            pdf.drawString(57, 538, "2. 관련 연구")
            pdf.setFont(_FONT, 10)
            for line in range(4):
                pdf.drawString(57, 520 - line * 14, f"왼쪽 단의 {line + 13}번째 줄 본문이 이어집니다.")
            self._column(pdf, 320, "오른쪽", range(8))
            self._grid(pdf, [320, 400, 470, 540], 560, 20, [["구분", "적용 전", "적용 후"], ["불량률", "3.2%", "1.1%"]])

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert "왼쪽 단의 12번째 줄 본문이 이어집니다.\n\n# 2. 관련 연구\n\n왼쪽 단의 13번째" in text
        assert text.index("오른쪽 단의 8번째") < text.index("| 구분 |")

    def test_label_beside_a_table_comes_before_it(self):
        """A narrow label placed vertically centred to the left of a table ('AS-IS', 'TO-BE') starts lower than the top of the table,
        so it was pushed behind the table and read like the title of the next table. A label is put before its own table."""
        rows = {"AS-IS": [["단계", "담당"], ["접수", "민원팀"], ["승인", "팀장"]],
                "TO-BE": [["단계", "담당"], ["온라인 접수", "시스템"], ["승인", "팀장"]]}

        def draw(pdf, index):
            pdf.drawString(57, 780, "3. 업무 프로세스 개선안")
            top = 740
            for label, table in rows.items():
                self._grid(pdf, [200, 330, 460], top, 22, table)
                pdf.setFont(_FONT, 14)
                pdf.drawString(70, top - 33 - 5, label)
                pdf.setFont(_FONT, 10)
                top -= 22 * 3 + 40
            pdf.drawString(57, top, "개선 후 처리 기간이 줄었다.")

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert text.index("AS-IS") < text.index("| 접수 |") < text.index("TO-BE") < text.index("| 온라인 접수 |")
        assert text.index("| 온라인 접수 |") < text.index("개선 후 처리 기간이 줄었다.")

    def test_wide_paragraph_left_of_a_table_is_not_taken_for_its_label(self):
        """Wide body text beside a table (another column) is not a label. Even at the height of the table it stays where it used to
        be."""
        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(8))
            self._column(pdf, 320, "오른쪽", range(4))
            self._grid(pdf, [320, 430, 540], 690, 20, [["항목", "값"], ["가", "1"], ["나", "2"]])

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert text.index("왼쪽 단의 8번째") < text.index("오른쪽 단의 1번째") < text.index("| 항목 |")

    def test_tables_at_the_end_of_a_column_keep_their_order(self):
        """When two tables follow the last paragraph of a column, the upper table comes first."""
        def draw(pdf, index):
            self._column(pdf, 57, "왼쪽", range(24))
            self._column(pdf, 320, "오른쪽", range(8))
            self._grid(pdf, [320, 430, 540], 620, 20, [["첫째 표", "값"], ["가", "1"]])
            self._grid(pdf, [320, 430, 540], 560, 20, [["둘째 표", "값"], ["나", "2"]])

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert ("오른쪽 단의 8번째 줄 본문이 이어집니다.\n\n| 첫째 표 | 값 |\n| --- | --- |\n| 가 | 1 |\n\n"
                "| 둘째 표 | 값 |\n| --- | --- |\n| 나 | 2 |") in text

    def test_centred_narrow_table_on_a_one_column_page_stays_under_its_heading(self):
        """On a single-column page, a left-aligned paragraph below a centred narrow table looked like 'the body of another column', so
        every table was pulled up to right after the lead paragraph at the top of the page and separated from its subheading. When
        the only paragraphs overlapping the table are page-wide paragraphs that also overlap that paragraph, there is one column."""
        teams = [("1. 영업1팀", "1,200", "110%"), ("2. 영업2팀", "900", "75%"), ("3. 영업3팀", "3,000", "150%")]

        def draw(pdf, index):
            pdf.drawString(57, 780, "이번 분기 부서별 실적을 아래와 같이 보고합니다. 금액 단위는 백만원이며 부가세 별도")
            pdf.drawString(57, 766, "기준입니다.")
            for number, (name, amount, rate) in enumerate(teams):
                top = 730 - number * 150
                pdf.drawString(57, top, name)
                self._grid(pdf, [220, 300, 380], top - 14, 20, [["구분", "금액"], ["매출", amount]])
                pdf.drawString(57, top - 80, f"목표 대비 {rate} 달성")

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        for name, amount, rate in teams:
            assert f"{name}\n\n| 구분 | 금액 |\n| --- | --- |\n| 매출 | {amount} |\n\n목표 대비 {rate} 달성" in text

    def test_table_beside_a_list_on_a_one_column_page_stays_before_the_list(self):
        """A page with a table placed beside text (a slide) does have separate columns, unlike the case above. In height order the
        table would slip in between the list items."""
        def draw(pdf, index):
            pdf.drawString(57, 780, "도입 효과 요약: 공정 자동화 이후 불량률과 처리 시간이 모두 줄었습니다. 세부 수치는 오른쪽 표.")
            for number in range(4):
                pdf.drawString(57, 700 - number * 30, f"- 개선 항목 {number + 1}번: 공정 자동화 효과")
            self._grid(pdf, [330, 430, 540], 690, 20, [["구분", "값"], ["불량률", "1.1%"], ["처리 시간", "3분"]])

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert text.index("| 구분 | 값 |") < text.index("- 개선 항목 1번")


class TestFormFields:
    """The values entered in a fillable form (AcroForm) are not in the page content but in widget annotations."""

    def test_filled_form_values_are_appended_to_their_page(self):
        """The entered values were dropped without even a warning, leaving only the labels ('성명', '소속'). The values are appended
        to the end of their page as 'name: value', in top-to-bottom order. A checkbox shows on or off, and empty text fields and push
        buttons are not included."""
        def draw(pdf, index):
            pdf.drawString(70, 760, "출장 신청서")
            form = pdf.acroForm
            for row, (name, value) in enumerate([("Name", "Hong Gildong"), ("Department", "Sales Team 2"), ("Memo", "")]):
                form.textfield(name=name, value=value, x=160, y=700 - row * 34, width=280, height=22, fontName="Helvetica")
            form.checkbox(name="agree", tooltip="규정 확인", checked=True, x=70, y=560, size=14)
            form.checkbox(name="newsletter", checked=False, x=70, y=530, size=14)
            form.radio(name="trip", value="domestic", selected=True, x=70, y=500, size=14)
            form.radio(name="trip", value="overseas", selected=False, x=170, y=500, size=14)
            pdf.drawString(70, 460, "양식 아래 본문입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert result.sections[0].text.endswith(
            "양식 아래 본문입니다.\n\nName: Hong Gildong\nDepartment: Sales Team 2\n규정 확인: ☑\nnewsletter: ☐\ntrip: domestic")
        assert result.warnings == []

    def test_xfa_form_without_readable_values_is_reported(self):
        """In an XFA form the content is in XML and the page holds only a 'Please wait...' notice. The notice alone came out as a
        'success'."""
        objects = [b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [] /XFA 5 0 R >> >>",
                   b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
                   b" /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>",
                   b"<< /Length 44 >>\nstream\nBT /F1 12 Tf 72 700 Td (Please wait...) Tj ET\nendstream",
                   b"<< /Length 6 >>\nstream\n<xdp/>\nendstream"]
        data, offsets = b"%PDF-1.7\n", []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(data))
            data += b"%d 0 obj\n%s\nendobj\n" % (number, body)
        xref = len(data)
        data += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        data += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
        data += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)

        result = convert(data, "a.pdf")

        assert "Please wait..." in result.text
        assert any("XFA" in warning for warning in result.warnings)


class TestUndecodableContentStreams:
    """pdfminer only logs a content stream whose compression is broken and returns an empty page. When the content stream of a page
    with no text, pictures or vectors at all failed to decode, it is reported — a real file in which all 30 pages were like that
    gave nothing but 'No text was extracted.'"""

    _TEXT = (b"<< /Length 46 >>\nstream\nBT /F1 12 Tf 72 700 Td (Readable page) Tj ET\nendstream")

    def _pdf(self, second: bytes) -> bytes:
        font = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
        return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>",
                         2: b"<< /Type /Pages /Kids [3 0 R 5 0 R] /Count 2 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R " + font + b" >>",
                         4: self._TEXT,
                         5: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 6 0 R " + font + b" >>",
                         6: second})

    def test_page_whose_content_stream_cannot_be_decoded_is_reported(self):
        garbage = b"\x78\x9c" + b"\xff\x00garbage" * 20
        data = self._pdf(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(garbage) + garbage + b"\nendstream")

        result = convert(data, "a.pdf")

        assert "Readable page" in result.sections[0].text
        assert any("could not be decoded" in warning and "1 PDF page(s) (2)" in warning for warning in result.warnings)

    def test_blank_page_with_an_empty_content_stream_is_not_reported(self):
        data = self._pdf(b"<< /Length 0 >>\nstream\n\nendstream")

        result = convert(data, "a.pdf")

        assert "Readable page" in result.sections[0].text
        assert not any("could not be decoded" in warning for warning in result.warnings)


class TestDeterministicLayout:
    """pdfminer's text-box grouping (LTLayoutContainer.group_textboxes) decides which of two pairs at the same distance to group first
    by id() (the memory address), so the reading order and character count of 13 out of 30 table-heavy public paper PDFs differed
    from run to run even within the same process (the character-limit check wobbled too). Ties are broken by first-seen order (the
    replacement of pdfminer.layout.id in extractors/pdf.py)."""

    # 7 text boxes (x, y, width, height) with distance ties, whose reading order depends on the grouping order — found with reversed ids.
    _BOXES = [(50, 0, 30, 10), (150, 14, 30, 10), (50, 28, 30, 10), (0, 28, 30, 10), (150, 0, 30, 10), (50, 14, 30, 10),
              (100, 28, 30, 10)]

    @staticmethod
    def _reading_order(allocation: list[int], padding: int) -> tuple[int, ...]:
        import pdfminer.layout as layout

        from opendockit.extractors import pdf as extractor

        laparams = layout.LAParams(**extractor._LAPARAMS)
        made, keep = {}, []
        for number in allocation:  # same list order; only the order of creation in memory and the objects in between change (scattered addresses)
            keep.extend(object() for _ in range(padding))
            box = layout.LTTextBoxHorizontal()
            x, y, width, height = TestDeterministicLayout._BOXES[number]
            box.set_bbox((x, y, x + width, y + height))
            box.number = number
            made[number] = box
        boxes = [made[number] for number in range(7)]
        groups = layout.LTPage(1, (0, 0, 600, 800)).group_textboxes(laparams, boxes)
        assigner = layout.IndexAssigner()
        for group in groups:
            group.analyze(laparams)
            assigner.run(group)
        return tuple(box.number for box in sorted(boxes, key=lambda box: box.index))

    def test_text_box_grouping_does_not_depend_on_memory_addresses(self):
        import random

        rng = random.Random(7)
        allocations = [rng.sample(range(7), 7) for _ in range(40)]

        orders = {self._reading_order(allocation, padding=index % 5) for index, allocation in enumerate(allocations)}

        assert len(orders) == 1, orders


def _raw_pdf(objects: dict[int, bytes], info: int | None = None) -> bytes:
    """Hand-build a PDF from object number → body (without `obj`/`endobj`). The cross-reference table is made by counting positions."""
    data, offsets = b"%PDF-1.7\n", {}

    for number in sorted(objects):
        offsets[number] = len(data)
        data += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])

    xref, size = len(data), max(objects) + 1
    data += b"xref\n0 %d\n0000000000 65535 f \n" % size
    data += b"".join(b"%010d 00000 n \n" % offsets[number] if number in offsets else b"0000000000 65535 f \n"
                     for number in range(1, size))
    trailer = b"/Size %d /Root 1 0 R" % size + (b" /Info %d 0 R" % info if info else b"")
    return data + b"trailer\n<< %s >>\nstartxref\n%d\n%%%%EOF\n" % (trailer, xref)


class _Hung(BaseException):
    """A conversion that did not finish in time. It is a BaseException so that the conversion boundary (`except Exception`) does not
    catch it and turn it into a 'corrupted document'."""


def _convert_within(data: bytes, seconds: float):
    """_Hung when it does not finish within `seconds`. SIGALRM cuts it off so that an endless loop does not stall the test runner. It
    can hang again in the clean-up code after being cut off (pdfplumber's close rebuilds the page list), so it is cut off again every
    second."""
    import signal

    def interrupt(signum, frame):
        raise _Hung

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds, 1.0)
    try:
        return convert(data, "a.pdf")
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="platform without SIGALRM")
class TestSelfReferencingObjects:
    """An indirect object that points at itself (or at each other), like `7 0 obj 7 0 R endobj`. pdfminer's `resolve1` follows
    references until the value is no longer a reference, so the conversion never finished wherever it was — document information,
    form annotations or page resources (one process hangs)."""

    _BODY = b"BT /F1 12 Tf 72 700 Td (Hello world body text) Tj ET"
    _FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    @classmethod
    def _document(cls, page: bytes = b"", catalog: bytes = b"", extra: dict[int, bytes] | None = None,
                  info: int | None = None) -> bytes:
        """A single-page document. The defaults of the page dictionary (`/MediaBox`, `/Contents`, `/Resources`) are replaced when
        `page` gives the same key."""
        entries = {b"/MediaBox": b"[0 0 595 842]", b"/Contents": b"4 0 R", b"/Resources": b"<< /Font << /F1 5 0 R >> >>"}

        for key in entries:
            if page.startswith(key):
                entries[key], page = page[len(key):], b""

        attributes = b" ".join(key + b" " + value.strip() for key, value in entries.items())
        objects = {1: b"<< /Type /Catalog /Pages 2 0 R %s >>" % catalog, 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   3: b"<< /Type /Page /Parent 2 0 R %s %s >>" % (attributes, page),
                   4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cls._BODY), cls._BODY), 5: cls._FONT}
        return _raw_pdf({**objects, **(extra or {})}, info=info)

    @pytest.mark.parametrize("info", [b"<< /Title 7 0 R >>", b"<< /Author 7 0 R >>", b"<< /Producer 7 0 R >>"])
    @pytest.mark.parametrize("chain", [{7: b"7 0 R"}, {7: b"8 0 R", 8: b"7 0 R"}], ids=["self", "pair"])
    @pytest.mark.timing
    def test_document_info_entry_that_refers_to_itself(self, info, chain):
        data = self._document(extra={6: info, **chain}, info=6)

        result = _convert_within(data, 10)

        assert "Hello world body text" in result.text
        assert "title" not in result.metadata and "author" not in result.metadata

    @pytest.mark.parametrize("page, catalog, extra", [
        (b"/Annots 6 0 R", b"", {6: b"6 0 R"}),
        (b"/Annots [6 0 R]", b"", {6: b"<< /Type /Annot /Subtype /Widget /Rect [72 600 200 620] /T (name) /FT /Tx /V 7 0 R >>",
                                   7: b"7 0 R"}),
        (b"/Annots [6 0 R]", b"", {6: b"<< /Type /Annot /Subtype /Widget /Rect 7 0 R /T (name) /FT /Tx /V (v) >>", 7: b"7 0 R"}),
        (b"", b"/AcroForm 6 0 R", {6: b"6 0 R"}),
    ], ids=["annots", "field-value", "widget-rect", "acroform"])
    @pytest.mark.timing
    def test_form_entries_that_refer_to_themselves(self, page, catalog, extra):
        data = self._document(page=page, catalog=catalog, extra=extra)

        result = _convert_within(data, 10)

        assert "Hello world body text" in result.text

    @pytest.mark.parametrize("page", [b"/MediaBox 6 0 R", b"/Contents 6 0 R", b"/Resources 6 0 R",
                                      b"/Resources << /Font 6 0 R >>", b"/Resources << /Font << /F1 6 0 R >> >>"],
                             ids=["mediabox", "contents", "resources", "font-dict", "font"])
    @pytest.mark.timing
    def test_page_entries_that_refer_to_themselves_end_without_hanging(self, page):
        """It must also end on the paths where pdfminer reads the page (pdfpage, font resolution). It reads as much as can be read or
        reports a corrupted document."""
        data = self._document(page=page, extra={6: b"6 0 R"})

        try:
            result = _convert_within(data, 10)
        except ConversionError:
            return

        assert isinstance(result.text, str)


class TestCellsMarkedOnlyByFill:
    """A ruled table whose empty cells are marked only by a fill colour (a Gantt-chart schedule). The cells have no text, so the marks
    vanished. Only an empty cell mostly filled with a chromatic or dark colour, while other cells of the same row are not filled,
    is output as ■. Header-row shading, stripes, a whole-row background and whole-column shading are not marks."""

    _ROWS = [["추진 내용", "1월", "2월", "3월", "4월", "5월", "6월"], ["요구사항 분석", "", "", "", "", "", ""],
             ["시스템 설계", "", "", "", "", "", ""], ["개발 및 시험", "", "", "", "", "", ""], ["시범 운영", "", "", "", "", "", ""]]

    def _table_pdf(self, rows, style):
        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.platypus import Table, TableStyle

            table = Table(rows, colWidths=[120] + [55] * (len(rows[0]) - 1))
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                                       *style]))
            _, height = table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 50, 760 - height)

        return _korean_pdf(draw)

    @pytest.mark.parametrize("colour", ["#4472C4", "#7F7F7F"], ids=["blue", "dark-grey"])
    def test_empty_cells_filled_with_a_colour_are_marked(self, colour):
        from reportlab.lib import colors

        bars = [("BACKGROUND", (first, row), (last, row), colors.HexColor(colour))
                for row, (first, last) in {1: (1, 2), 2: (2, 3), 3: (3, 5), 4: (6, 6)}.items()]
        data = self._table_pdf(self._ROWS, bars)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 추진 내용 | 1월 | 2월 | 3월 | 4월 | 5월 | 6월 |\n| --- | --- | --- | --- | --- | --- | --- |\n"
                "| 요구사항 분석 | ■ | ■ | | | | |\n| 시스템 설계 | | ■ | ■ | | | |\n"
                "| 개발 및 시험 | | | ■ | ■ | ■ | |\n| 시범 운영 | | | | | | ■ |") in result.text

    @pytest.mark.parametrize("kind", ["header", "zebra", "whole-table", "first-column", "white", "light-grey",
                                      "scattered"])
    def test_shading_that_is_not_a_mark_leaves_empty_cells_empty(self, kind):
        """Header-row shading (including empty header cells, only some cells), row stripes, a whole-table background, shading of the
        whole first column (including empty category cells), a white fill, light grey ('cells not to be filled in') and scattered
        highlighted cells with no run of adjacent filled cells are not marks."""
        from reportlab.lib import colors

        rows = [["", *self._ROWS[0][1:]], *self._ROWS[1:3], ["", "", "", "", "", "", ""], *self._ROWS[3:]]
        blue = colors.HexColor("#4472C4")
        style = {"header": [("BACKGROUND", (0, 0), (3, 0), blue)],
                 "zebra": [("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#C6E0B4")])],
                 "whole-table": [("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#FFF2CC"))],
                 "first-column": [("BACKGROUND", (0, 0), (0, -1), blue)],
                 "white": [("BACKGROUND", (1, 1), (2, 2), colors.white)],
                 "light-grey": [("BACKGROUND", (1, 1), (2, 2), colors.HexColor("#D9D9D9"))],
                 "scattered": [("BACKGROUND", (column, row), (column, row), colors.HexColor("#FFF2CC"))
                               for row, column in ((1, 2), (2, 4), (4, 1), (5, 6))]}[kind]
        data = self._table_pdf(rows, style)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert "| 요구사항 분석 | | | | | | |" in result.text
        assert "■" not in result.text


class TestHorizontalWatermark:
    """A large light-grey watermark written horizontally ('DRAFT', 6 times the body) slipped in as a paragraph between table cells
    and body text."""

    def _page(self, mark_gray: float, mark_size: float = 60, over_text: bool = True, alpha: float = 1.0):
        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawString(72, 790, "3. 사업부별 실적 분석")
            pdf.setFont(_FONT, 10)
            for line in range(30):
                pdf.drawString(72, 760 - line * 15, f"{line + 1}번째 본문 줄: 이번 분기에는 주요 고객사의 발주가 늘었다.")
            pdf.setFillGray(mark_gray)
            pdf.setFillAlpha(alpha)
            pdf.setFont("Helvetica-Bold", mark_size)
            pdf.drawCentredString(297, 420 if over_text else 120, "DRAFT")

        return convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

    def test_light_large_horizontal_watermark_over_the_text_is_dropped(self):
        text = self._page(0.85)

        assert "DRAFT" not in text
        assert text.count("번째 본문 줄") == 30

    def test_dark_large_text_is_kept(self):
        assert "DRAFT" in self._page(0.0)

    def test_light_large_text_on_its_own_is_kept(self):
        """Large light text that does not overlap the body (a decorative number on a cover or slide) is not a watermark."""
        assert "DRAFT" in self._page(0.85, over_text=False)

    def test_black_watermark_made_faint_by_transparency_is_dropped(self):
        """`color: rgba(0,0,0,0.08)` in browser printing puts a fill opacity (ExtGState `/ca`) on black characters. By colour alone it
        is black text, so it slipped in between table cells and body text as 'S AM P LE'."""
        text = self._page(0.0, alpha=0.08)

        assert "DRAFT" not in text
        assert text.count("번째 본문 줄") == 30

    def test_half_transparent_dark_text_is_kept(self):
        assert "DRAFT" in self._page(0.0, alpha=0.6)
