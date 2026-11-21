"""Korean words a line break cut in two (`_pdf_paragraphs._breaks_inside_a_word`): Hangul (HWP) breaks a line after any
syllable and its PDF draws the space a line was broken at, so a line that ends without one was broken inside a word."""

from io import BytesIO

from opendockit import ConvertOptions, convert


_PLAIN = ConvertOptions(page_markers=False)
_FONT, _SIZE, _RIGHT = "HYSMyeongJo-Medium", 11, 520.0
# The lines of one paragraph as a justified column draws them: a line broken at a space ends in that space
_LINES = ["산림청은 이틀간 강원특별자치도 속초시에서 지방정부와 산림",
          "청의 산지관리 업무 담당자가 참석한 가운데 워크숍을 열었고 ",
          "정책의 추진 성과를 공유하며 현장 중심의 제도개선 방안과 지 ",
          "원 방안을 논의하기 위해 산지관리 담당자들이 함께 모였으며 ",
          "지속가능한 발전 방안을 찾았다고 밝혔다."]


def _pdf(lines: list[str], *, ragged: bool = False) -> bytes:
    """A page whose `lines` all end at the right end of the column, the space a line ends in standing past it — or, with
    `ragged`, all start at the left end and end where their text does."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    pdf.setFont(_FONT, _SIZE)
    for index, line in enumerate(lines):
        if ragged or index == len(lines) - 1:
            pdf.drawString(520.0 - 30 * _SIZE, 760 - index * 16, line)
        else:
            pdf.drawRightString(_RIGHT + pdfmetrics.stringWidth(" ", _FONT, _SIZE) * line.endswith(" "), 760 - index * 16, line)
    pdf.save()
    return buffer.getvalue()


class TestWordsBrokenAtLineEnds:
    def test_a_line_that_ends_without_the_space_the_others_end_in_was_broken_inside_a_word(self):
        text = convert(_pdf(_LINES), "a.pdf", _PLAIN).text

        assert "지방정부와 산림청의 산지관리" in text
        # a line broken at a space is a line break still: Markdown reads it as the space that stood there
        assert "워크숍을 열었고\n정책의 추진" in text and "방안과 지\n원 방안을" in text and "모였으며\n지속가능한" in text

    def test_a_document_that_draws_no_space_at_a_line_end_says_nothing_of_its_breaks(self):
        """Chrome and Word end every line with its last letter, so the same line break is left as it is."""
        text = convert(_pdf([line.rstrip() for line in _LINES]), "a.pdf", _PLAIN).text

        assert "지방정부와 산림\n청의 산지관리" in text

    def test_a_line_that_stops_short_of_the_column_ended_a_paragraph(self):
        """The lines of an address end where their text does, without a space, and are no halves of words."""
        lines = [*_LINES[1:4], "산림청 산지정책과 담당자 김영곤", "대전광역시 서구 청사로 정부대전청사"]
        text = convert(_pdf(lines, ragged=True), "a.pdf", _PLAIN).text

        assert "김영곤\n대전광역시" in text

    def test_a_break_between_a_number_and_hangul_stays(self):
        """Only Hangul on both sides is known to be one word: a number at the end of a line may end a word or not."""
        lines = [_LINES[0][:-2] + "모두 340", "명이 참석한 가운데 산지관리 업무 담당자 워크숍을 열었고 ", *_LINES[2:]]
        text = convert(_pdf(lines), "a.pdf", _PLAIN).text

        assert "모두 340\n명이 참석한" in text
