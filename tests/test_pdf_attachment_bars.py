"""The bar an attachment of a Korean official document opens with (`_pdf_paragraphs._attachment_bars`): a label and a
title in a box of one row, a point or two larger than the body. HWP and HWPX write it as a heading of level 2
(`test_hwp_one_row_tables.py`), and so does the PDF where the page shows it."""

from io import BytesIO

from opendockit import ConvertOptions, convert


_PLAIN = ConvertOptions(page_markers=False)
_FONT = "HYSMyeongJo-Medium"
_BODY = ["행사는 산림청이 주관하며 전국의 산지관리 담당자가 참석한다.", "참석자는 행사 전날까지 등록을 마쳐야 하며 자료는 현장에서 배부한다.",
         "문의는 산지정책과로 하고 세부 일정은 추후 다시 안내한다."]


def _pdf(*pages: list[tuple[float, float, int, str]], boxed: tuple[float, float, float] | None = None) -> bytes:
    """Pages of pieces of text (x, y, size, text). `boxed` (left, middle, right) draws a box of two cells around the first
    line of the first page."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    for number, pieces in enumerate(pages):
        if boxed and number == 0:
            top = pieces[0][1]
            pdf.rect(boxed[0], top - 8, boxed[2] - boxed[0], 28)
            pdf.line(boxed[1], top - 8, boxed[1], top + 20)
        for x, y, size, text in pieces:
            pdf.setFont(_FONT, size)
            pdf.drawString(x, y, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _body(top: float) -> list[tuple[float, float, int, str]]:
    return [(72, top - 18 * index, 11, line) for index, line in enumerate(_BODY)]


_FIRST_PAGE = _body(760) + _body(680)


class TestAttachmentBars:
    def test_a_bar_at_the_top_of_its_page_is_a_heading(self):
        text = convert(_pdf(_FIRST_PAGE, [(72, 780, 13, "붙임 1"), (140, 780, 13, "행사 개요"), *_body(740)]), "a.pdf", _PLAIN).text

        assert "\n\n## 붙임 1 행사 개요\n\n행사는 산림청이" in text

    def test_a_bar_further_down_its_page_is_one_by_the_label_standing_apart(self):
        page = [*_body(780), (72, 700, 13, "참 고 2"), (150, 700, 13, "확산방지를 위한 안내문"), *_body(660)]
        text = convert(_pdf(_FIRST_PAGE, page), "a.pdf", _PLAIN).text

        assert "\n\n## 참 고 2 확산방지를 위한 안내문\n\n" in text

    def test_a_bar_drawn_as_a_box_of_two_cells_is_a_heading_not_a_table(self):
        page = [(80, 780, 13, "붙임 2"), (160, 780, 13, "현장 사진"), *_body(730)]
        text = convert(_pdf(page, _FIRST_PAGE, boxed=(72, 140, 520)), "a.pdf", _PLAIN).text

        assert text.startswith("## 붙임 2 현장 사진\n\n") and "|" not in text

    def test_the_list_of_attachments_in_the_body_is_body_text(self):
        """At the body size, with a mark after the label — and one set larger is still a list."""
        for size in (11, 13):
            page = [*_body(780), (72, 700, size, "붙임 1. 행사 개요 1부."), (72, 680, size, "붙임 : 행사 사진. 끝.")]
            text = convert(_pdf(_FIRST_PAGE, page), "a.pdf", _PLAIN).text

            assert "## 붙임" not in text and "붙임 1. 행사 개요 1부." in text

    def test_a_sentence_that_starts_with_the_word_is_body_text(self):
        page = [*_body(780), (72, 700, 11, "참고 자료는 누리집에서 내려받을 수 있다.")]
        text = convert(_pdf(_FIRST_PAGE, page), "a.pdf", _PLAIN).text

        assert "#" not in text

    def test_a_label_at_the_body_size_is_no_bar(self):
        text = convert(_pdf(_FIRST_PAGE, [(72, 780, 11, "붙임 1"), (140, 780, 11, "행사 개요"), *_body(740)]), "a.pdf", _PLAIN).text

        assert "## 붙임" not in text

    def test_a_label_with_its_title_beside_it_in_another_size_is_one_heading(self):
        """The title is a paragraph of its own by its size, two lines of it, and still the title of the label it stands beside."""
        page = [*_body(780), (72, 700, 13, "붙임1"), (140, 706, 15, "기후변화에 관한 정부 간 협의체"), (140, 688, 15, "제7차 평가보고서 개요"),
                *_body(640)]
        text = convert(_pdf(_FIRST_PAGE, page), "a.pdf", _PLAIN).text

        assert "\n\n## 붙임1 기후변화에 관한 정부 간 협의체 제7차 평가보고서 개요\n\n" in text
        assert text.count("기후변화에 관한") == 1
