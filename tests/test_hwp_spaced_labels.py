"""Letter-spaced labels of Hangul forms (`성   명`, `담   당   자`) are written without the spaces between their syllables
(`_hangul.unspaced_label`). In 34 real HWP/HWPX forms the labels `성 명`, `주 소`, `담 당 자`, `기 관 명` came out spaced, so a
search for `성명` or `담당자` did not find them. Only syllables spaced wider than a word gap are joined: one space between
one-syllable words is ordinary Korean as often (`수 및 양`, the choices `남 여`), and joining it cannot be undone."""

import pytest

from opendockit import convert
from opendockit.extractors._hangul import unspaced_label
from tests import builder


def _cell(row: int, col: int, text: str) -> str:
    return (f"<hp:tc><hp:subList><hp:p><hp:run><hp:t>{text}</hp:t></hp:run></hp:p></hp:subList>"
            f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')


def _form(*rows: tuple[str, str]) -> bytes:
    body = (f'<hp:p><hp:run><hp:tbl rowCnt="{len(rows)}" colCnt="2">'
            + "".join(f"<hp:tr>{_cell(row, 0, label)}{_cell(row, 1, value)}</hp:tr>" for row, (label, value) in enumerate(rows))
            + "</hp:tbl></hp:run></hp:p>")
    return builder.hwpx_bytes(body)


class TestSpacedLabels:
    def test_labels_of_a_form_are_written_without_their_spaces(self):
        text = convert(_form(("기  관  명", "한국정보원"), ("성   명", "홍길동"), ("담   당   자", "김철수")), "a.hwpx").text

        assert text.splitlines() == ["| 기관명 | 한국정보원 |", "| --- | --- |", "| 성명 | 홍길동 |", "| 담당자 | 김철수 |"]

    def test_tab_or_ideographic_space_keeps_a_label_apart_from_its_value(self):
        body = ('<hp:p><hp:run><hp:t>기   관   명<hp:tab/>서울시청</hp:t></hp:run></hp:p>'
                '<hp:p><hp:run><hp:t>대   표   자　홍  길  동</hp:t></hp:run></hp:p>')

        # (the shared cleaning writes the tab and the ideographic space as a space)
        assert convert(builder.hwpx_bytes(body), "a.hwpx").text.split("\n\n") == ["기관명 서울시청", "대표자 홍길동"]
        assert unspaced_label("성   명\t홍 길 동") == "성명\t홍 길 동"

    def test_bold_label_is_joined_too(self):
        """Header cells are often bold throughout; their marks are dropped as for any whole cell, and the label is joined."""
        head = '<hh:charProperties itemCnt="1"><hh:charPr id="1" height="1000"><hh:bold/></hh:charPr></hh:charProperties>'
        body = ('<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>'
                + _cell(0, 0, "기   관   명").replace("<hp:run>", '<hp:run charPrIDRef="1">') + _cell(0, 1, "한국정보원")
                + "</hp:tr></hp:tbl></hp:run></hp:p>")
        data = builder.zip_with(builder.hwpx_bytes(body), edit={
            "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", head + "</hh:refList>")})

        assert convert(data, "a.hwpx").text.splitlines()[0] == "| 기관명 | 한국정보원 |"

    def test_blank_date_of_a_form_keeps_its_spaces(self):
        text = convert(_form(("작성일", "2024 년   월   일"), ("시간", "시   분"), ("날짜", "년   월   일")), "a.hwpx").text

        assert "2024 년 월 일" in text and "시 분" in text and "| 년 월 일 |" in text

    def test_words_and_choices_spaced_by_one_space_stay_apart(self):
        """One space between one-syllable words: words of a sentence, choices of a cell, a label and its value. Joining them
        (`할수있는일`, `남여`, `성명홍길동`) could not be undone."""
        lines = ("할 수 있는 일", "이 법 시행 전", "수 및 양", "그 외 몇 가지", "약 3 억 원", "제 1 장 총 칙", "기 관 명")
        cells = ("성 명 홍 길 동", "남 여"), ("평 가", "상 중 하"), ("요 일", "월 화 수 목 금"), ("유 무", "찬 반")
        body = "".join(f"<hp:p><hp:run><hp:t>{line}</hp:t></hp:run></hp:p>" for line in lines)

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text.split("\n\n") == list(lines)
        table = convert(_form(*cells), "a.hwpx").text
        assert all(f"| {cell} |" in table for row in cells for cell in row)

    @pytest.mark.parametrize(("line", "expected"), [
        ("담   당   자", "담당자"),
        ("주        소", "주소"),
        ("목   차", "목차"),
        ("□ 추   진   배   경", "□ 추진배경"),
        ("13. 농  업  기  술  센  터", "13. 농업기술센터"),
        ("기 관 명", "기 관 명"),           # one space: as `수 및 양`
        ("성 명", "성 명"),
        ("성   명  홍 길 동", "성   명  홍 길 동"),   # gaps of different widths: a label and its value
        ("성   명   홍   길   동   님   께", "성   명   홍   길   동   님   께"),   # more than six syllables
        ("실   습   생 인적사항", "실   습   생 인적사항"),
        ("년   월   일", "년   월   일"),
        ("성   명   (인)", "성   명   (인)"),
        ("제   1   장", "제   1   장"),
        ("A   B   C", "A   B   C"),
    ])
    def test_which_lines_are_spaced_labels(self, line, expected):
        assert unspaced_label(line) == expected

    def test_spaced_title_paragraph_is_joined(self):
        text = convert(builder.hwpx_bytes("<hp:p><hp:run><hp:t>목   차</hp:t></hp:run></hp:p>"
                                          "<hp:p><hp:run><hp:t>보 도 자 료</hp:t></hp:run></hp:p>"), "a.hwpx").text

        assert text == "목차\n\n보 도 자 료"
