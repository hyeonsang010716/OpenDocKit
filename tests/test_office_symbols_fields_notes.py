"""Symbol-font characters in DOCX/PPTX, Word enclosed characters (EQ field), tables inside footnotes, resolving strikethrough
marks, hand-typed bullet lines in PPTX."""

import time

import pytest

from opendockit import convert
from opendockit.extractors._inline import mark_strike, resolve_strikes
from tests import builder
from tests.test_office import (
    _docx_with_body,
    _pptx_with_paragraphs,
    _pptx_with_runs,
    _slide_body,
)


def _run(text: str, properties: str = "") -> str:
    return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def _font(name: str) -> str:
    return f'<w:rFonts w:ascii="{name}" w:hAnsi="{name}"/>'


def _slide_text(data: bytes) -> str:
    return convert(data, "a.pptx").text.split("\n\n", 1)[1]


class TestSymbolFontCharactersInText:
    """Private-use-area characters stored as the text (w:t / a:t) of a symbol-font run. Word's Insert Symbol gives w:sym, but pasted
    text, other generators and PowerPoint (a:sym) store them as U+F0xx characters of a run with the font set. The common table
    had only □, so ☒ was removed and only □ remained, and the chosen item read the other way round."""

    def test_wingdings_run_in_docx_maps_its_private_use_characters_by_the_font(self):
        wing = _font("Wingdings")
        body = ("<w:p>" + _run("참석 여부: ") + _run("", wing) + _run(" 참석  ") + _run("", wing) + _run(" 불참")
                + "</w:p><w:p>" + _run("동의: ") + _run("", wing) + _run(" 예 ") + _run("", wing) + _run(" 아니오")
                + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "참석 여부: ☒ 참석 □ 불참\n\n동의: ☒ 예 □ 아니오"

    def test_symbol_font_run_in_docx_uses_the_symbol_table(self):
        """B3 in Symbol is ≥. The same code is a different character in Wingdings, so it could not go into the common table and was
        removed."""
        body = "<w:p>" + _run("기준 ") + _run("", _font("Symbol")) + _run(" 80%") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "기준 ≥ 80%"

    def test_private_use_character_in_an_ordinary_font_run_keeps_the_generic_rule(self):
        """A private-use-area character in a run that is not in a symbol font is a glyph of that font, so its meaning is unknown. It
        is left to the common table (`clean_text`)."""
        body = "<w:p>" + _run("항목 끝", _font("맑은 고딕")) + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "항목 끝"

    def test_pptx_symbol_typeface_maps_private_use_characters(self):
        """PowerPoint's Insert Symbol gives a:rPr/a:sym typeface="Wingdings" + a U+F0xx character."""
        def symbol(text: str) -> str:
            return f'<a:r><a:rPr lang="ko-KR"><a:sym typeface="Wingdings" pitchFamily="2" charset="2"/></a:rPr><a:t>{text}</a:t></a:r>'

        def plain(text: str) -> str:
            return f'<a:r><a:rPr lang="ko-KR"/><a:t>{text}</a:t></a:r>'

        runs = (plain("참석 여부: ") + symbol("") + plain(" 참석  ") + symbol("") + plain(" 불참 / 동의: ")
                + symbol("") + plain(" 예 ") + symbol("") + plain(" 아니오"))

        body = _slide_text(_pptx_with_runs(runs))

        assert body == "참석 여부: ☒ 참석 □ 불참 / 동의: ☒ 예 □ 아니오"

    def test_plain_letters_in_a_symbol_font_run_in_docx_become_the_symbols_they_show(self):
        """Ordinary letters switched to the Symbol font (m, a, ³) show on screen as μ, α, ≥. Only private-use-area characters were
        mapped, giving '10 mm, a ³ 30' (Adobe Symbol encoding). Hangul in the same run is drawn in the East Asian font, so it stays."""
        symbol = _font("Symbol")
        body = ("<w:p>" + _run("입자 크기 10 ") + _run("m", symbol) + _run("m, 각도 ") + _run("a", symbol) + _run(" ")
                + _run("³", symbol) + _run(" 30, ") + _run("D각", symbol) + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "입자 크기 10 μm, 각도 α ≥ 30, Δ각"

    def test_plain_letters_follow_the_font_of_their_own_range_in_docx(self):
        """ASCII characters are drawn in the w:ascii font, other Latin-1 characters in the w:hAnsi font. A run whose East Asian font
        alone is Symbol is not changed."""
        body = ("<w:p>" + _run("a³", '<w:rFonts w:ascii="Arial" w:hAnsi="Symbol"/>') + _run(" ")
                + _run("a³", '<w:rFonts w:ascii="Symbol" w:hAnsi="Arial"/>') + _run(" ")
                + _run("ab", '<w:rFonts w:eastAsia="Symbol"/>') + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "a≥ α³ ab"

    def test_plain_letters_in_a_wingdings_run_are_left_alone(self):
        """No mapping for ordinary letters in Wingdings was added (only some meaningful symbols are in the table). The letters stay
        as they are."""
        body = "<w:p>" + _run("abc", _font("Wingdings")) + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "abc"

    def test_pptx_plain_letters_follow_the_latin_typeface_only(self):
        """The font of ordinary letters is a:latin. Only the letters of a run whose a:latin is Symbol are read as Adobe Symbol codes."""
        runs = ('<a:r><a:rPr lang="ko-KR"/><a:t>두께 5 </a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:latin typeface="Symbol"/></a:rPr><a:t>m</a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"/><a:t>m, 원주율 </a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:sym typeface="Symbol"/></a:rPr><a:t></a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:latin typeface="Arial"/><a:sym typeface="Symbol"/></a:rPr><a:t> (p)</a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs))

        assert body == "두께 5 μm, 원주율 π (p)"

    def test_pptx_letters_typed_after_an_inserted_symbol_stay_letters(self):
        """PowerPoint's Insert Symbol stores a U+F0xx character + a:sym, and text typed right after it inherits that a:sym but is
        drawn on screen in the Latin font. With a:sym taken as the font of ordinary letters, 'x+z = y+z for all numbers' in real
        PowerPoint 2010 lecture slides became 'ξ+ζ = ψ+ζ φορ αλλ νυμβερσ', and '(KPI A등급)' became '(ΚΠΙ Α등급)'."""
        symbol = '<a:rPr lang="ko-KR"><a:sym typeface="Symbol" pitchFamily="18" charset="2"/></a:rPr>'
        runs = (f'<a:r><a:rPr lang="ko-KR"/><a:t>목표: ROE </a:t></a:r><a:r>{symbol}<a:t></a:t></a:r>'
                f'<a:r>{symbol}<a:t> 15% (KPI A등급), x</a:t></a:r><a:r>{symbol}<a:t></a:t></a:r>'
                f'<a:r>{symbol}<a:t>y+z for all</a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs))

        assert body == "목표: ROE ≥ 15% (KPI A등급), x⇒y+z for all"

    def test_pptx_latin_typeface_of_a_symbol_font_maps_too(self):
        runs = ('<a:r><a:rPr lang="ko-KR"/><a:t>확인 </a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:latin typeface="Wingdings 2"/></a:rPr><a:t></a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs, in_table=True))

        assert "확인 ✓" in body


def _eq_field(*instructions: str, result: str = "") -> str:
    """Complex field. The instruction is stored split across runs (in a Word enclosed character, the run of the outer shape carries
    its own size/position properties)."""
    runs = "".join(f'<w:r><w:instrText xml:space="preserve">{part}</w:instrText></w:r>' for part in instructions)
    separate = f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{_run(result)}' if result else ""
    return f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>{runs}{separate}<w:r><w:fldChar w:fldCharType="end"/></w:r>'


class TestDocxEnclosedCharacters:
    """A Word 'enclosed character' (Home → Enclose Characters) is stored as the EQ field `eq \\o\\ac(○,21)` with no result run. The
    instruction was not read, so '제㉑조' became '제조'. **Checked only with synthetic XML, without a real Word file.**"""

    def test_circled_number_field_becomes_a_circled_number(self):
        body = ("<w:p>" + _run("제") + _eq_field("eq \\o\\ac(", "○", ",21)") + _run("조 (보칙)") + "</w:p><w:p>"
                + _eq_field("EQ \\o\\ac(○,3)") + _run(" ") + _eq_field("eq \\o\\ac(◯,50)") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "제㉑조 (보칙)\n\n③ ㊿"

    def test_check_overstruck_on_a_box_becomes_a_checked_box(self):
        body = ("<w:p>" + _run("검토유형: ") + _eq_field("eq \\o(□,✓)") + _run(" 신규지표  ") + _eq_field("eq \\o\\ac(□,×)")
                + _run(" 기존지표") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "검토유형: ☑ 신규지표 ☒ 기존지표"

    def test_shrunken_text_and_semicolon_separator_are_read(self):
        """An enclosed character with 'shrink text' has its inner text wrapped in `\\s\\up 1(…)`, and a locale whose list separator
        is ';' separates the arguments with ';'."""
        body = "<w:p>" + _eq_field("eq \\o\\ac(○,\\s\\up 1(가))") + _eq_field("eq \\o\\ac(○;\\s\\up -1(12))") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "㉮⑫"

    def test_enclosure_without_a_matching_character_keeps_shape_and_text(self):
        body = "<w:p>" + _eq_field("eq \\o\\ac(○,주)") + _run(" ") + _eq_field("eq \\o\\ac(□,확인)") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "(주) [확인]"

    def test_unknown_equation_form_keeps_its_inner_text(self):
        body = ("<w:p>" + _run("비율 ") + _eq_field("eq \\f(1,2)") + _run(", 강조 ") + _eq_field("eq \\x\\to(검토)")
                + _run(", 배열 ") + _eq_field("eq \\a\\al\\co2(가,나,다,라)") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "비율 1/2, 강조 검토, 배열 가 나 다 라"

    def test_stored_result_is_used_instead_of_the_instruction(self):
        """When there is a result run, that is the on-screen text. Reading the instruction too would give the same text twice."""
        body = "<w:p>" + _eq_field("eq \\o\\ac(○,3)", result="③") + _run(" 항") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "③ 항"

    def test_simple_field_without_a_result_is_read_too(self):
        body = '<w:p><w:fldSimple w:instr=" eq \\o\\ac(○,7)"/>' + _run(" 항목") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "⑦ 항목"

    def test_deeply_nested_instruction_keeps_its_text_without_recursing(self):
        """The instruction is text written in the file, so its parenthesis nesting is not trusted. When it is deep the structure is
        not parsed and only the text is kept (recursion limit)."""
        body = "<w:p>" + _eq_field("eq " + "\\s\\up 1(" * 5_000 + "가" + ")" * 5_000) + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "가"

    def test_other_fields_without_a_result_still_give_nothing(self):
        body = "<w:p>" + _run("쪽 ") + _eq_field("PAGE") + _run(" 끝") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "쪽 끝"


_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class TestDocxFieldWithoutAnEnd:
    """A field in a paragraph with only begin and an instruction but no separate/end (a corrupted file) treated all the following
    text as instruction, so the rest of the paragraph vanished. The text after the instruction of a field still unclosed at the
    end of the paragraph is restored as visible text."""

    def test_text_after_an_unclosed_field_is_kept(self):
        body = ("<w:p>" + _run("앞글 ") + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                '<w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r>' + _run("끝없는필드") + _run(" 뒷글") + "</w:p>"
                "<w:p>" + _run("다음 문단") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert "앞글 끝없는필드 뒷글\n\n다음 문단" in result.text
        assert "PAGE" not in result.text

    def test_closed_field_nested_in_an_unclosed_one_stays_hidden(self):
        """The result of a closed inner field is part of the outer instruction, not text (the same rule as for a closed field)."""
        body = ('<w:p><w:r><w:fldChar w:fldCharType="begin"/></w:r>'
                '<w:r><w:instrText xml:space="preserve"> IF </w:instrText></w:r>'
                + _eq_field(" NUMPAGES ", result="4") + _run(" 뒷글") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "뒷글"


def _docx_with_footnote(note_body: str) -> bytes:
    notes = (f'<w:footnotes xmlns:w="{_WORD_NS}"><w:footnote w:id="1">{note_body}</w:footnote></w:footnotes>')
    return builder.zip_with(_docx_with_body("<w:p>" + _run("배출량") + '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>'), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace(
            "</Relationships>", f'<Relationship Id="rIdNotes" Type="{_RELATIONSHIPS_NS}/footnotes" Target="footnotes.xml"/>'
                                "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace(
            "</Types>", '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                        'wordprocessingml.footnotes+xml"/></Types>'),
    }, add={"word/footnotes.xml": notes})


def _word_table(rows: list[list[str]]) -> str:
    return "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc><w:p>{_run(cell)}</w:p></w:tc>" for cell in row) + "</w:tr>"
                               for row in rows) + "</w:tbl>"


class TestDocxTableInsideFootnote:
    def test_table_in_a_footnote_keeps_cell_and_row_boundaries(self):
        """Cell paragraphs of a table in a footnote were joined with spaces, giving '출처: 환경부 구분 값 A 10'. A footnote is one
        line, so cells are joined with ' / ' and rows with '; '."""
        data = _docx_with_footnote("<w:p>" + _run("출처: 환경부") + "</w:p>"
                                   + _word_table([["구분", "값"], ["A", "10"], ["B", ""]]))

        result = convert(data, "a.docx")

        assert result.text == "배출량[^1]\n\n[^1]: 출처: 환경부 구분 / 값; A / 10; B"

    def test_paragraphs_in_a_footnote_cell_stay_in_that_cell(self):
        data = _docx_with_footnote(_word_table([["연도", "값"]]).replace(
            f"<w:p>{_run('값')}</w:p>", f"<w:p>{_run('값')}</w:p><w:p>{_run('(톤)')}</w:p>"))

        result = convert(data, "a.docx")

        assert result.text.endswith("[^1]: 연도 / 값 (톤)")


class TestStrikeResolutionIsLinear:
    @pytest.mark.timing
    def test_many_struck_runs_on_one_line_resolve_in_linear_time(self):
        """The line-start check walked back to the start of the line for every mark (rfind + strip), so 40,000 pairs on one line
        took about 2 s (quadratic time)."""
        text = "".join(mark_strike("삭제") + " 유지 " for _ in range(40_000))

        started = time.perf_counter()
        resolved = resolve_strikes(text)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.5
        assert resolved.count("~~") == 80_000

    @pytest.mark.parametrize(("pieces", "expected"), [
        ([" ", ("- 1차", True)], " - ~~1차~~"),
        (["앞 ", ("- 1차", True)], "앞 ~~- 1차~~"),
        ([(" ", True), ("- 1차", True)], " - ~~1차~~"),
        ([("\t", True), " ", ("- 1차", True)], "\t - ~~1차~~"),
        (["가\n  ", ("• 항목\n- 둘", True), (" 셋", True)], "가\n  • ~~항목~~\n- ~~둘 셋~~"),
        ([("가", True), "\n", ("- 나", True)], "~~가~~\n- ~~나~~"),
    ])
    def test_line_start_rule_is_unchanged(self, pieces, expected):
        """The bullet is put outside the mark only at the start of a line (only whitespace before it). Even if the preceding struck
        span is whitespace only, once there was a mark it is not the start of the line."""
        text = "".join(mark_strike(piece[0]) if isinstance(piece, tuple) else piece for piece in pieces)

        resolved = resolve_strikes(text)

        assert resolved == expected


class TestPptxHandTypedLinesInsideAnItem:
    def test_dotted_date_line_is_not_a_numbered_sub_item(self):
        """'12. 31. 기준 완료' is a date (month. day.). '12. ' was read as a number and the line was indented as a sub-item."""
        data = _pptx_with_paragraphs([("1단계 착수\v12. 31. 기준 완료\v3. 2.5% 증가", 0, "")], placeholder=True)

        body = _slide_body(data)

        assert body == "- 1단계 착수\n12. 31. 기준 완료\n  3. 2.5% 증가"

    def test_parenthesized_number_line_is_a_sub_item_with_a_list_mark(self):
        """A '3) ' line is a sub-item, but only indentation was added and `clean_text` removed it (list lines are only '-', '*',
        '1.'). It is written in the sub-number form '- 3) ' (`_lists.list_item_prefix`)."""
        data = _pptx_with_paragraphs([("1단계 착수\v3) 예산 확정", 0, "")], placeholder=True)

        body = _slide_body(data)

        assert body == "- 1단계 착수\n  - 3) 예산 확정"
