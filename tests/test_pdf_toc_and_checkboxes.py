"""Page numbers at the end of the dot leaders of a PDF table of contents, and checkboxes drawn with lines."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


class TestDotLeaderPageNumbers:
    """A one-digit page number in a table of contents is further than twice the character width from the end of the dot leader, so pdfminer grouped
    it separately and the numbers came out in a bunch after the entries."""

    _ENTRIES = [(0, "Ⅰ. 사업 개요", 1), (1, "1. 추진 배경", 2), (1, "2. 사업 목표", 3), (0, "Ⅱ. 추진 계획", 5),
                (1, "1. 단계별 일정", 6), (2, "가. 인건비", 8), (0, "Ⅲ. 기대 효과", 11), (0, "붙임", 13)]

    def test_page_numbers_stay_at_the_end_of_their_dot_leaders(self):
        def draw(pdf, index):
            pdf.setFont(_FONT, 18)
            pdf.drawCentredString(297, 760, "목  차")
            y = 710.0
            for level, title, number in self._ENTRIES:
                pdf.setFont(_FONT, 11)
                x = 60 + level * 20
                pdf.drawString(x, y, title)
                start = x + pdf.stringWidth(title, _FONT, 11) + 5
                dots = int((490 - start) / pdf.stringWidth(".", _FONT, 11))
                pdf.drawString(start, y, "." * dots)
                pdf.drawRightString(515, y, str(number))
                y -= 24

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        lines = [line for line in result.text.splitlines() if line.strip()]
        for _, title, number in self._ENTRIES:
            line = next(line for line in lines if line.startswith(title))
            assert line == f"{title} … {number}", line
        assert not any(line.strip().isdigit() for line in lines)

    def test_dot_leaders_are_shortened_to_one_ellipsis(self):
        """Leaving the dot leaders (tab fill) of a table of contents as they are left more than 1,300 dots on one page, and the character count
        grew more than fivefold (1,696 chars) over the DOCX of the same document (325 chars). A dot leader is shortened to a single ellipsis (' …
        ')."""
        def draw(pdf, index):
            y = 760.0
            for level, title, number in self._ENTRIES:
                pdf.setFont(_FONT, 11)
                x = 60 + level * 20
                pdf.drawString(x, y, title)
                start = x + pdf.stringWidth(title, _FONT, 11) + 5
                pdf.drawString(start, y, "." * int((490 - start) / pdf.stringWidth(".", _FONT, 11)))
                pdf.drawRightString(515, y, str(number))
                y -= 24

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert [line for line in result.text.splitlines() if line] == [f"{title} … {number}" for _, title, number in self._ENTRIES]

    def test_long_dot_leaders_do_not_count_against_the_limit_before_they_are_shortened(self):
        """The running character count (`_countable_chars`) included the dot leaders before they were shortened, so a table-of-contents document
        that converts to 2,741 chars was rejected as exceeded at a limit of 8,223 chars. The running decision must not be larger than the character
        count of the full conversion."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 9)
            for row in range(45):
                pdf.drawString(72, 780 - row * 16, f"제{index * 45 + row + 1}절 세부 추진 과제 " + "." * 110 + f" {row + 3}")

        data = _korean_pdf(draw, pages=3)
        full = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        limited = convert(data, "a.pdf", ConvertOptions(page_markers=False, max_chars=full.char_count))

        assert not full.exceeded and not limited.exceeded
        assert limited.text == full.text

    @pytest.mark.parametrize(("text", "shortened"), [
        ("서론 . . . . . . . . 1", "서론 … 1"), ("서론·········1", "서론 … 1"), ("서론 ………… 1", "서론 … 1"),
        ("| 서론 .......... | 1 |", "| 서론 … | 1 |"), ("..........\n끝", "…\n끝"),
        ("그런데…… 말이다", "그런데…… 말이다"), ("1.2.3.4", "1.2.3.4"), ("다....", "다....")])
    def test_only_runs_of_leader_dots_are_shortened(self, text, shortened):
        """Leaders made of spaced dots, middle dots and ellipsis characters are shortened too, while an ellipsis inside prose ('……'), numbering and
        short runs of dots are left as they are."""
        from opendockit.extractors.pdf import _shortened_leaders

        result = _shortened_leaders(text)

        assert result == shortened

    def test_right_aligned_fees_stay_at_the_end_of_their_dot_leaders(self):
        fees = [("계좌 개설", "무료"), ("타행 이체(창구)", "3,000원"), ("타행 이체(인터넷)", "500원"), ("해외 송금", "10,000원")]

        def draw(pdf, index):
            y = 760.0
            for item, fee in fees:
                pdf.setFont(_FONT, 11)
                pdf.drawString(60, y, item)
                start = 60 + pdf.stringWidth(item, _FONT, 11) + 5
                dots = int((515 - pdf.stringWidth(fee, _FONT, 11) - 16 - start) / pdf.stringWidth(".", _FONT, 11))
                pdf.drawString(start, y, "." * dots)
                pdf.drawRightString(515, y, fee)
                y -= 22

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        for item, fee in fees:
            assert next(line for line in lines if line.startswith(item)) == f"{item} … {fee}"


def _box(pdf, x, y, mark=None):
    """A 9pt checkbox beside the text baseline y. `mark` is None (empty box), 'check' (a bent line), 'cross' (the two lines of an X) or 'fill' (a
    filled rectangle inside)."""
    pdf.rect(x, y - 1, 9, 9)
    if mark == "check":
        path = pdf.beginPath()
        path.moveTo(x + 1.5, y + 3.5)
        path.lineTo(x + 4, y + 1)
        path.lineTo(x + 8, y + 7)
        pdf.drawPath(path, stroke=1, fill=0)
    elif mark == "cross":
        pdf.line(x + 1.5, y + 0.5, x + 7.5, y + 6.5)
        pdf.line(x + 1.5, y + 6.5, x + 7.5, y + 0.5)
    elif mark == "fill":
        pdf.rect(x + 2, y + 1, 5, 5, stroke=0, fill=1)


class TestCheckboxesDrawnAsShapes:
    """When the checkboxes of a consent form are drawn with rectangles and lines there are no characters, so which option was chosen was lost. Only
    small square boxes right at the left of text are considered: ☑ is put before that text when there is a line or a filled rectangle inside, ☐
    when the box is empty."""

    @pytest.mark.parametrize("mark", ["check", "cross", "fill"])
    def test_ticked_and_empty_boxes_before_their_labels_are_marked(self, mark):
        def draw(pdf, index):
            pdf.drawString(60, 700, "위 개인정보 수집·이용에 동의하십니까?")
            _box(pdf, 330, 700, mark)
            pdf.drawString(343, 700, "동의함")
            _box(pdf, 400, 700)
            pdf.drawString(413, 700, "동의하지 않음")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☑ 동의함" in result.text
        assert "☐ 동의하지 않음" in result.text
        assert result.text.count("☑") == 1

    def test_squares_that_are_not_checkboxes_are_left_alone(self):
        """A row of character-cell boxes (abutting squares), a filled square bullet, a box far away from text and a large rectangle are not
        checkboxes."""
        def draw(pdf, index):
            pdf.drawString(60, 700, "주민등록번호")
            for column in range(6):
                pdf.rect(140 + column * 12, 699, 12, 12)
            pdf.drawString(220, 700, "앞자리")
            pdf.rect(60, 671, 6, 6, stroke=0, fill=1)
            pdf.drawString(72, 670, "칠한 글머리 항목")
            pdf.rect(60, 639, 9, 9)
            pdf.drawString(200, 640, "멀리 떨어진 글")
            pdf.rect(60, 560, 60, 60)
            pdf.drawString(130, 600, "큰 상자 옆 글")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☐" not in result.text
        assert "☑" not in result.text

    def test_boxes_inside_a_frame_around_the_question_are_still_marked(self):
        def draw(pdf, index):
            pdf.rect(50, 680, 480, 40)
            pdf.drawString(60, 700, "마케팅 정보 수신에 동의하십니까?")
            _box(pdf, 330, 700)
            pdf.drawString(343, 700, "동의함")
            _box(pdf, 400, 700, "check")
            pdf.drawString(413, 700, "동의하지 않음")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☐ 동의함" in result.text
        assert "☑ 동의하지 않음" in result.text


def test_dot_leaders_drawn_apart_from_the_titles_stay_on_one_line():
    """A table of contents drawn in the order entry text, page number, dot leader is grouped as three separate pieces. They must be joined into one
    line (not just the leader and the number)."""
    entries = [("제1장 서론", 3), ("1. 연구 배경", 3), ("2. 연구 목적", 5), ("제2장 현황 분석", 8)]

    def draw(pdf, index):
        y = 750.0
        for title, number in entries:
            pdf.setFont(_FONT, 11)
            pdf.drawString(80, y, title)
            pdf.drawRightString(500, y, str(number))
            start = 80 + pdf.stringWidth(title, _FONT, 11) + 5  # drawing the leader after the number makes pdfminer leave three pieces
            pdf.drawString(start, y, "." * int((481 - start) / pdf.stringWidth(".", _FONT, 11)))
            y -= 22

    result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

    lines = result.text.splitlines()
    for title, number in entries:
        line = next(line for line in lines if line.startswith(title))
        assert line == f"{title} … {number}", line


class TestCheckboxesBesideTheirLabels:
    """Boxes placed after their labels, forms with a slightly wide gap between box and label, and marks that overflow the box a little."""

    def test_boxes_after_their_labels_belong_to_the_label_on_their_left(self):
        """The boxes of '동의함 ☑ 동의하지 않음 ☐' were attached to the label on their right, which swapped the chosen option."""
        def draw(pdf, index):
            pdf.drawString(60, 740, "개인정보 수집·이용에 동의하십니까?")
            pdf.drawString(300, 712, "동의함")
            _box(pdf, 333, 712, "check")
            pdf.drawString(355, 712, "동의하지 않음")
            _box(pdf, 420, 712)
            pdf.drawString(60, 680, "마케팅 정보 수신에 동의하십니까?")
            pdf.drawString(300, 652, "동의함")
            _box(pdf, 333, 652)
            pdf.drawString(355, 652, "동의하지 않음")
            _box(pdf, 420, 652, "cross")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        text = result.text
        assert text.index("동의함 ☑") < text.index("동의하지 않음 ☐") < text.index("동의함 ☐") < text.index("동의하지 않음 ☑")
        assert text.count("☑") == 2
        assert text.count("☐") == 2

    def test_label_a_little_further_than_one_and_a_half_box_widths_still_gets_the_mark(self):
        """With 14pt between an 8pt box and its label (1.75x the box width) the choice silently disappeared."""
        def draw(pdf, index):
            pdf.drawString(60, 780, "희망 교육 과정을 선택하십시오.")
            for row, (label, ticked) in enumerate([("기초 과정", True), ("심화 과정", False), ("실무 과정", True)]):
                y = 750 - row * 20
                pdf.rect(70, y, 8, 8)
                if ticked:
                    pdf.rect(72, y + 2, 4, 4, stroke=0, fill=1)
                pdf.drawString(92, y + 0.5, label)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☑ 기초 과정" in result.text
        assert "☐ 심화 과정" in result.text
        assert "☑ 실무 과정" in result.text

    def test_mark_that_overflows_the_box_a_little_is_still_a_tick(self):
        """A hand-drawn X that goes 1.5pt beyond the corners of the box (outside the 1pt tolerance) made it an empty box."""
        def draw(pdf, index):
            pdf.drawString(60, 780, "개인정보 수집·이용에 동의하십니까?")
            pdf.rect(60, 710, 10, 10)
            pdf.line(58.5, 708.5, 71.5, 721.5)
            pdf.line(58.5, 721.5, 71.5, 708.5)
            pdf.drawString(75, 711, "동의함")
            pdf.rect(60, 690, 10, 10)
            pdf.drawString(75, 691, "동의하지 않음")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☑ 동의함" in result.text
        assert "☐ 동의하지 않음" in result.text

    def test_rule_running_through_a_box_is_not_a_tick(self):
        """A long line running across a box (underline, separator rule) is not a mark inside the box."""
        def draw(pdf, index):
            pdf.drawString(60, 780, "다음 중 해당하는 곳에 표시하십시오.")
            pdf.rect(60, 740, 10, 10)
            pdf.line(40, 745, 400, 745)
            pdf.drawString(75, 741, "해당 있음")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☐ 해당 있음" in result.text


class TestCheckboxesDrawnAsCurves:
    """Choice marks drawn as rounded boxes and circles (radio buttons). Only rectangles were considered, so which option was chosen was lost in
    PDFs printed from web and Word form controls."""

    @staticmethod
    def _tick(pdf, x, y):
        path = pdf.beginPath()
        path.moveTo(x + 2, y + 4)
        path.lineTo(x + 4.5, y + 1.5)
        path.lineTo(x + 8.5, y + 7.5)
        pdf.drawPath(path, stroke=1, fill=0)

    def test_rounded_boxes_and_radio_circles_are_marked(self):
        def draw(pdf, index):
            pdf.drawString(60, 780, "개인정보 수집·이용에 동의하십니까?")
            for x, label, ticked in [(60, "동의함", True), (160, "동의하지 않음", False)]:
                pdf.roundRect(x, 749, 10, 10, 2, stroke=1, fill=0)
                if ticked:
                    self._tick(pdf, x, 750)
                pdf.drawString(x + 14, 750, label)
            pdf.drawString(60, 720, "마케팅 정보 수신:")
            for x, label, chosen in [(170, "예", False), (220, "아니오", True)]:
                pdf.circle(x + 5, 724, 5, stroke=1, fill=0)
                if chosen:
                    pdf.circle(x + 5, 724, 2.5, stroke=0, fill=1)
                pdf.drawString(x + 14, 720, label)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☑ 동의함" in result.text
        assert "☐ 동의하지 않음" in result.text
        assert "☐ 예" in result.text
        assert "☑ 아니오" in result.text

    def test_browser_controls_drawn_over_a_white_backing_are_marked(self):
        """Chrome first paints a white backing for every control, then draws a ticked box as a white check inside a colour-filled rounded box and
        an unticked box as a grey outline only. Taking the white backing for a mark would turn empty boxes into ☑ too."""
        def control(pdf, x, y, ticked):
            pdf.setFillColorRGB(1, 1, 1)
            pdf.roundRect(x, y, 9.5, 9.5, 2, stroke=0, fill=1)
            if ticked:
                pdf.setFillColorRGB(0, 0.46, 1)
                pdf.roundRect(x - 0.1, y - 0.1, 9.7, 9.7, 2, stroke=0, fill=1)
                pdf.setStrokeColorRGB(1, 1, 1)
                self._tick(pdf, x - 0.5, y)
            else:
                pdf.setStrokeColorRGB(0.46, 0.46, 0.46)
                pdf.roundRect(x + 0.25, y + 0.25, 9, 9, 2, stroke=1, fill=0)
            pdf.setFillColorRGB(0, 0, 0)
            pdf.setStrokeColorRGB(0, 0, 0)

        def draw(pdf, index):
            pdf.drawString(60, 780, "개인정보 수집·이용 동의")
            control(pdf, 60, 749, True)
            pdf.drawString(74, 750, "동의함")
            control(pdf, 130, 749, False)
            pdf.drawString(144, 750, "동의하지 않음")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☑ 동의함" in result.text
        assert "☐ 동의하지 않음" in result.text

    def test_round_bullets_down_a_list_are_not_checkboxes(self):
        """Round bullets placed one per line (outlined or filled circles) are not choice marks. A curved box is considered only when it has a mark
        or there is another curved box on the same row."""
        def draw(pdf, index):
            for row, label in enumerate(["첫째 항목입니다", "둘째 항목입니다", "셋째 항목입니다"]):
                y = 760 - row * 20
                pdf.circle(65, y + 4, 3, stroke=1, fill=0)
                pdf.circle(65, y - 56, 3, stroke=0, fill=1)
                pdf.roundRect(60, y - 121, 8, 8, 2, stroke=1, fill=0)
                pdf.drawString(74, y, label)
                pdf.drawString(74, y - 60, label)
                pdf.drawString(74, y - 120, label)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "☐" not in result.text
        assert "☑" not in result.text


def test_section_numbers_set_apart_from_the_titles_of_a_table_of_contents_stay_on_their_lines():
    """In a table of contents whose chapter/section number column ('제1장', '1.1') is more than 3x the font size away from the entry text, the
    numbers were bunched separately and became '1.1\\n1.2'."""
    entries = [(0, "제1장", "서론", "1"), (1, "1.1", "연구 배경", "2"), (1, "1.2", "연구 목적", "5"), (0, "제2장", "이론적 배경", "9"),
               (1, "2.1", "선행 연구 검토", "10"), (0, "제3장", "결론", "103")]

    def draw(pdf, index):
        y = 740.0
        for level, number, title, page in entries:
            x = 60 + level * 20
            pdf.drawString(x, y, number)
            start = x + 50
            pdf.drawString(start, y, title)
            dots_start = start + pdf.stringWidth(title, _FONT, 10) + 6
            right = 535 - pdf.stringWidth(page, _FONT, 10)
            pdf.drawString(dots_start, y, "." * int((right - 8 - dots_start) / pdf.stringWidth(".", _FONT, 10)))
            pdf.drawString(right, y, page)
            y -= 22

    result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

    lines = [line for line in result.text.splitlines() if line.strip()]
    for _, number, title, page in entries:
        line = next(line for line in lines if title in line)
        assert line == f"{number} {title} … {page}", line


class TestBulletsDrawnAsCircles:
    """Chrome draws `<ul>` bullets not as characters but as 3pt curves (a filled circle at level 1, an outlined circle at level 2), so the bullets
    disappeared and the list hierarchy was flattened."""

    def test_filled_and_hollow_circle_bullets_become_bullet_characters(self):
        items = [(0, "CI 파이프라인이 모두 통과했는지 확인한다."), (1, "단위 테스트 커버리지 80% 이상"), (1, "보안 스캔 경고 0건"),
                 (0, "배포 공지를 채널에 올린다.")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10.5)
            pdf.drawString(72, 780, "1. 사전 점검")
            for number, (level, text) in enumerate(items):
                y = 760 - number * 16
                x = 92 + level * 30
                pdf.circle(x + 1.5, y + 3.5, 1.5, stroke=level, fill=1 - level)
                pdf.drawString(x + 11.5, y, text)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert ("• CI 파이프라인이 모두 통과했는지 확인한다.\n◦ 단위 테스트 커버리지 80% 이상\n◦ 보안 스캔 경고 0건\n"
                "• 배포 공지를 채널에 올린다.") in result.text

    def test_third_level_square_bullets_are_kept(self):
        """Chrome draws level-3 bullets as filled squares of the same size, so they disappeared in a check that looked at circles only."""
        items = [(0, "1단계: 현황 분석"), (1, "이해관계자 인터뷰"), (2, "내부 부서 12곳"), (2, "외부 협력사 5곳")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10.5)
            for number, (level, text) in enumerate(items):
                y = 760 - number * 16
                x = 92 + level * 30
                if level == 2:
                    pdf.rect(x, y + 2, 3.75, 3.75, stroke=0, fill=1)
                else:
                    pdf.circle(x + 1.5, y + 3.5, 1.5, stroke=level, fill=1 - level)
                pdf.drawString(x + 11.5, y, text)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "◦ 이해관계자 인터뷰\n▪ 내부 부서 12곳\n▪ 외부 협력사 5곳" in result.text

    def test_large_circle_next_to_a_label_is_not_a_bullet(self):
        """A circle as large as the line height (a radio button, for example) is not a bullet."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 10.5)
            pdf.circle(96, 763.5, 4.5, stroke=1, fill=0)
            pdf.drawString(104, 760, "예")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "◦" not in result.text and "•" not in result.text


class TestTableOfContentsWithoutLeaders:
    """In a table of contents without leaders, with only the page numbers aligned at the right edge, the entries indented by level have different
    left edges, so the 'label value' group was broken and the page numbers came out in a bunch after all the entries had been read."""

    _ENTRIES = [(0, "Ⅰ. 사업 개요", 1), (1, "1. 추진 배경", 2), (1, "2. 사업 목표", 4), (0, "Ⅱ. 현황 분석", 7),
                (1, "1. 시장 동향", 8), (1, "2. 경쟁사 분석", 11), (0, "Ⅲ. 추진 전략", 18), (0, "Ⅳ. 기대 효과", 27)]

    def test_indented_entries_keep_their_right_aligned_page_numbers(self):
        def draw(pdf, index):
            pdf.setFont(_FONT, 18)
            pdf.drawString(72, 770, "목 차")
            y = 720.0
            for level, title, number in self._ENTRIES:
                pdf.setFont(_FONT, 12 if level == 0 else 11)
                pdf.drawString(72 + level * 20, y, title)
                pdf.drawRightString(523, y, str(number))
                y -= 26

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        for _, title, number in self._ENTRIES:
            assert f"{title} {number}" in lines
