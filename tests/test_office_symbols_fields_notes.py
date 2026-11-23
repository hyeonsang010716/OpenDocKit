"""Symbol-font characters in DOCX/PPTX, Word enclosed characters (EQ field), tables inside footnotes, resolving strikethrough
marks, hand-typed bullet lines in PPTX."""

import time

import pytest

from opendockit import ConvertOptions, convert
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


class TestSymbolsOfTheOtherSymbolFonts:
    """Wingdings 3 (arrows and triangles) and Webdings (pictures) had no table, so their numbers were handed to the shared
    mapping, which holds the Wingdings meaning of each: the down triangle before a fall came out as a box, the right triangle
    as a diamond, and the check and the cross of a yes / no table as nothing. The tables were read off the glyphs the fonts
    draw. A number a table does not hold is left out, not given the Wingdings glyph of that number."""

    @staticmethod
    def _sym(font: str, char: str) -> str:
        return f'<w:r><w:sym w:font="{font}" w:char="{char}"/></w:r>'

    def test_docx_symbols_of_wingdings_3_and_webdings(self):
        body = ("<w:p>" + _run("Trend ") + self._sym("Wingdings 3", "F071") + _run(" 1.1% ") + self._sym("Wingdings 3", "F070")
                + _run(" 3.2% ") + self._sym("Wingdings 3", "F075") + _run(" ") + self._sym("Wingdings 3", "0067") + "</w:p>"
                + "<w:p>" + _run("Export ") + self._sym("Webdings", "F072") + _run(" Import ") + self._sym("Webdings", "F061")
                + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "Trend ▼ 1.1% ▲ 3.2% ▶ →\n\nExport ✗ Import ✓"

    def test_docx_run_in_wingdings_3_maps_its_private_use_characters(self):
        body = "<w:p>" + _run("Sales ") + _run("\uf070", _font("Wingdings 3")) + _run(" Cost ") + _run("\uf071", _font("Wingdings 3")) + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "Sales ▲ Cost ▼"

    def test_number_the_table_does_not_hold_is_not_the_wingdings_glyph(self):
        """Webdings 6C is a compass, Wingdings 2 71 an ornament, Wingdings 3 F5 nothing: the Wingdings glyphs of those numbers
        (a disc, a box) are not what the document shows. The same numbers in Wingdings itself are read as before."""
        body = ("<w:p>" + _run("a") + self._sym("Webdings", "F06C") + self._sym("Wingdings 2", "F071")
                + self._sym("Wingdings 3", "F0F5") + _run("\uf06c\uf071", _font("Webdings")) + _run("b ")
                + self._sym("Wingdings", "F071") + _run("\uf06c", _font("Wingdings")) + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "ab ❑●"

    def test_pptx_symbol_typeface_of_wingdings_3_and_webdings(self):
        def symbol(typeface: str, text: str) -> str:
            return f'<a:r><a:rPr lang="en-US"><a:sym typeface="{typeface}" pitchFamily="2" charset="2"/></a:rPr><a:t>{text}</a:t></a:r>'

        def plain(text: str) -> str:
            return f'<a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r>'

        runs = (plain("Cost ") + symbol("Wingdings 3", "\uf071") + plain(" 1.1% Sales ") + symbol("Wingdings 3", "\uf070")
                + plain(" 3.2% ok ") + symbol("Webdings", "\uf061") + plain(" no ") + symbol("Webdings", "\uf072"))

        assert _slide_text(_pptx_with_runs(runs)) == "Cost ▼ 1.1% Sales ▲ 3.2% ok ✓ no ✗"

    def test_html_text_in_webdings_and_wingdings_3(self):
        page = ('<html><body><p>Done <span style="font-family:Webdings">a</span> Failed <font face="Webdings">r</font> '
                'Next <span style="font-family:Wingdings 3">g</span></p></body></html>')

        assert convert(page.encode("utf-8"), "a.html").text == "Done ✓ Failed ✗ Next →"


class TestSymbolFontCharactersInText:
    """Private-use-area characters stored as the text (w:t / a:t) of a symbol-font run. Word's Insert Symbol gives w:sym, but pasted
    text, other generators and PowerPoint (a:sym) store them as U+F0xx characters of a run with the font set. The common table
    had only □, so ☒ was removed and only □ remained, and the chosen item read the other way round."""

    def test_wingdings_run_in_docx_maps_its_private_use_characters_by_the_font(self):
        wing = _font("Wingdings")
        body = ("<w:p>" + _run("참석 여부: ") + _run("\uf0fd", wing) + _run(" 참석  ") + _run("\uf0a8", wing) + _run(" 불참")
                + "</w:p><w:p>" + _run("동의: ") + _run("\uf078", wing) + _run(" 예 ") + _run("\uf06f", wing) + _run(" 아니오")
                + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "참석 여부: ☒ 참석 □ 불참\n\n동의: ☒ 예 □ 아니오"

    def test_symbol_font_run_in_docx_uses_the_symbol_table(self):
        """B3 in Symbol is ≥. The same code is a different character in Wingdings, so it could not go into the common table and was
        removed."""
        body = "<w:p>" + _run("기준 ") + _run("\uf0b3", _font("Symbol")) + _run(" 80%") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "기준 ≥ 80%"

    def test_private_use_character_in_an_ordinary_font_run_keeps_the_generic_rule(self):
        """A private-use-area character in a run that is not in a symbol font is a glyph of that font, so its meaning is unknown. It
        is left to the common table (`clean_text`)."""
        body = "<w:p>" + _run("항목\uf078 끝", _font("맑은 고딕")) + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "항목 끝"

    def test_pptx_symbol_typeface_maps_private_use_characters(self):
        """PowerPoint's Insert Symbol gives a:rPr/a:sym typeface="Wingdings" + a U+F0xx character."""
        def symbol(text: str) -> str:
            return f'<a:r><a:rPr lang="ko-KR"><a:sym typeface="Wingdings" pitchFamily="2" charset="2"/></a:rPr><a:t>{text}</a:t></a:r>'

        def plain(text: str) -> str:
            return f'<a:r><a:rPr lang="ko-KR"/><a:t>{text}</a:t></a:r>'

        runs = (plain("참석 여부: ") + symbol("\uf0fd") + plain(" 참석  ") + symbol("\uf0a8") + plain(" 불참 / 동의: ")
                + symbol("\uf078") + plain(" 예 ") + symbol("\uf06f") + plain(" 아니오"))

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
                '<a:r><a:rPr lang="ko-KR"><a:sym typeface="Symbol"/></a:rPr><a:t>\uf070</a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:latin typeface="Arial"/><a:sym typeface="Symbol"/></a:rPr><a:t> (p)</a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs))

        assert body == "두께 5 μm, 원주율 π (p)"

    def test_pptx_letters_typed_after_an_inserted_symbol_stay_letters(self):
        """PowerPoint's Insert Symbol stores a U+F0xx character + a:sym, and text typed right after it inherits that a:sym but is
        drawn on screen in the Latin font. With a:sym taken as the font of ordinary letters, 'x+z = y+z for all numbers' in real
        PowerPoint 2010 lecture slides became 'ξ+ζ = ψ+ζ φορ αλλ νυμβερσ', and '(KPI A등급)' became '(ΚΠΙ Α등급)'."""
        symbol = '<a:rPr lang="ko-KR"><a:sym typeface="Symbol" pitchFamily="18" charset="2"/></a:rPr>'
        runs = (f'<a:r><a:rPr lang="ko-KR"/><a:t>목표: ROE </a:t></a:r><a:r>{symbol}<a:t>\uf0b3</a:t></a:r>'
                f'<a:r>{symbol}<a:t> 15% (KPI A등급), x</a:t></a:r><a:r>{symbol}<a:t>\uf0de</a:t></a:r>'
                f'<a:r>{symbol}<a:t>y+z for all</a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs))

        assert body == "목표: ROE ≥ 15% (KPI A등급), x⇒y+z for all"

    def test_pptx_latin_typeface_of_a_symbol_font_maps_too(self):
        runs = ('<a:r><a:rPr lang="ko-KR"/><a:t>확인 </a:t></a:r>'
                '<a:r><a:rPr lang="ko-KR"><a:latin typeface="Wingdings 2"/></a:rPr><a:t>\uf050</a:t></a:r>')

        body = _slide_text(_pptx_with_runs(runs, in_table=True))

        assert "확인 ✓" in body


def _eq_field(*instructions: str, result: str = "") -> str:
    """Complex field. The instruction is stored split across runs (in a Word enclosed character, the run of the outer shape carries
    its own size/position properties)."""
    runs = "".join(f'<w:r><w:instrText xml:space="preserve">{part}</w:instrText></w:r>' for part in instructions)
    separate = f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{_run(result)}' if result else ""
    return f'<w:r><w:fldChar w:fldCharType="begin"/></w:r>{runs}{separate}<w:r><w:fldChar w:fldCharType="end"/></w:r>'


class TestDocxSymbolsOfWord2016:
    """Word 2016 and later write an emoji or symbol inserted from the symbol dialog (or made by AutoCorrect from `:)`) as
    `w16se:symEx` in `mc:Choice`, the character itself only in the compatibility copy (`mc:Fallback`), which is never read: the
    emoji vanished, and a paragraph of only one with it. The character is read from `w16se:char`."""

    @staticmethod
    def _symbol(code: str) -> str:
        return ('<w:r><mc:AlternateContent><mc:Choice Requires="w16se" '
                'xmlns:w16se="http://schemas.microsoft.com/office/word/2015/wordml/symex">'
                f'<w16se:symEx w16se:font="Segoe UI Emoji" w16se:char="{code}"/></mc:Choice>'
                "<mc:Fallback><w:t>?</w:t></mc:Fallback></mc:AlternateContent></w:r>")

    def test_emoji_of_a_symbol_element_is_read(self):
        body = ("<w:p>" + _run("Thanks ") + self._symbol("1F60A") + _run(" see you") + "</w:p><w:p>" + _run("Rating:")
                + "</w:p><w:p>" + self._symbol("2605") + self._symbol("D800") + self._symbol("xyz") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "Thanks \U0001F60A see you\n\nRating:\n\n\u2605"


    def test_symbol_keeps_the_marks_of_its_run(self):
        """The character takes the bold and strikethrough of its run, as a `w:sym` does: added bare, it split the span of a
        bold or struck run, `**Great**😊 **job**`."""
        def run(text: str, properties: str) -> str:
            return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

        bold, struck = "<w:b/>", "<w:strike/>"
        symbol = self._symbol("1F60A")
        body = ("<w:p>" + run("Great", bold) + symbol.replace("<w:r>", f"<w:r><w:rPr>{bold}</w:rPr>") + run(" job", bold)
                + run(" plain", "") + "</w:p><w:p>" + run("old", struck) + symbol.replace("<w:r>", f"<w:r><w:rPr>{struck}</w:rPr>")
                + run("price", struck) + run(" new", "") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "**Great\U0001F60A job** plain\n\n~~old\U0001F60Aprice~~ new"


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


class TestDocxNoteReferenceWithoutContent:
    """A footnote reference whose note does not exist (no such id, no footnotes part) or has nothing in it left `[^1]` in the body
    with no `[^1]:` definition anywhere — a mark pointing nowhere, which the include_footnotes=False rule already removes."""

    def test_reference_to_a_missing_note_writes_no_mark(self):
        data = _docx_with_footnote("<w:p>" + _run("출처") + "</w:p>")
        data = builder.zip_with(data, edit={"word/document.xml": lambda xml: xml.replace(
            '<w:footnoteReference w:id="1"/></w:r>', '<w:footnoteReference w:id="1"/></w:r><w:r><w:footnoteReference w:id="9"/></w:r>')})

        result = convert(data, "a.docx")

        assert result.text == "배출량[^1]\n\n[^1]: 출처"

    def test_reference_without_a_footnotes_part_writes_no_mark(self):
        data = _docx_with_body("<w:p>" + _run("주장") + '<w:r><w:footnoteReference w:id="7"/></w:r>' + _run(" 끝") + "</w:p>")

        assert convert(data, "a.docx").text == "주장 끝"

    def test_empty_note_writes_no_mark_and_the_next_note_takes_the_first_number(self):
        data = _docx_with_footnote('<w:p><w:r><w:t xml:space="preserve">  </w:t></w:r></w:p>')
        notes = (f'<w:footnotes xmlns:w="{_WORD_NS}"><w:footnote w:id="1"><w:p/></w:footnote>'
                 f'<w:footnote w:id="2"><w:p>{_run("둘째 각주")}</w:p></w:footnote></w:footnotes>')
        data = builder.zip_with(data, edit={
            "word/footnotes.xml": lambda xml: notes,
            "word/document.xml": lambda xml: xml.replace(
                '<w:footnoteReference w:id="1"/></w:r>', '<w:footnoteReference w:id="1"/></w:r><w:r><w:footnoteReference w:id="2"/></w:r>')})

        assert convert(data, "a.docx").text == "배출량[^1]\n\n[^1]: 둘째 각주"


def _docx_with_custom_mark(kind: str, flag: str, note_start: str = "*", mark: str = "*") -> bytes:
    """Document whose body references note 1 of `kind` (footnote / endnote) with Word's Insert Footnote > Custom mark `mark`: the
    reference carries `w:customMarkFollows` = `flag` and the mark follows as text in the same run; the note starts with
    `note_start` as text in place of `w:footnoteRef`."""
    style = f'<w:rPr><w:rStyle w:val="{kind.capitalize()}Reference"/></w:rPr>'
    body = ("<w:p>" + _run("배출량") + f'<w:r>{style}<w:{kind}Reference w:customMarkFollows="{flag}" w:id="1"/><w:t>{mark}</w:t></w:r>'
            + _run(" 기준") + "</w:p>")
    note = (f'<w:{kind}s xmlns:w="{_WORD_NS}"><w:{kind} w:id="1"><w:p><w:r>{style}<w:t>{note_start}</w:t></w:r>{_run(" 추정치")}'
            f"</w:p></w:{kind}></w:{kind}s>")
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace(
            "</Relationships>", f'<Relationship Id="rIdNotes" Type="{_RELATIONSHIPS_NS}/{kind}s" Target="{kind}s.xml"/>'
                                "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace(
            "</Types>", f'<Override PartName="/word/{kind}s.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                        f'wordprocessingml.{kind}s+xml"/></Types>'),
    }, add={f"word/{kind}s.xml": note})


class TestDocxCustomNoteMark:
    """A footnote with a custom mark (Insert Footnote > Custom mark) is stored as `w:footnoteReference w:customMarkFollows="1"`
    followed by the mark as text in the same run, and the note starts with the mark as text where `w:footnoteRef` would be. The
    mark was written next to the generated label: 'Text[^1]* and more', '[^1]: * Estimated.'."""

    @pytest.mark.parametrize("flag", ["1", "true", "on"])
    def test_custom_footnote_mark_is_written_once_as_the_label(self, flag):
        result = convert(_docx_with_custom_mark("footnote", flag), "a.docx")

        assert result.text == "배출량[^1] 기준\n\n[^1]: 추정치"

    def test_custom_endnote_mark_is_written_once_as_the_label(self):
        result = convert(_docx_with_custom_mark("endnote", "1"), "a.docx")

        assert result.text == "배출량[^endnote1] 기준\n\n[^endnote1]: 추정치"

    @pytest.mark.parametrize("flag", ["0", "false", "off"])
    def test_text_after_an_ordinary_reference_stays(self, flag):
        result = convert(_docx_with_custom_mark("footnote", flag), "a.docx")

        assert result.text == "배출량[^1]* 기준\n\n[^1]: * 추정치"

    def test_custom_mark_is_left_out_with_the_reference_when_footnotes_are_not_included(self):
        result = convert(_docx_with_custom_mark("footnote", "1"), "a.docx", ConvertOptions(include_footnotes=False))

        assert result.text == "배출량 기준"

    def test_note_that_does_not_start_with_the_mark_keeps_its_text(self):
        result = convert(_docx_with_custom_mark("footnote", "1", note_start="주"), "a.docx")

        assert result.text == "배출량[^1] 기준\n\n[^1]: 주 추정치"

    def test_letter_mark_is_taken_off_a_note_only_as_a_word_of_its_own(self):
        """A letter mark is taken off the start of the note only where a word could end: a note that does not repeat the
        mark 'a' keeps the 'a' of 'about'."""
        repeated = convert(_docx_with_custom_mark("footnote", "1", note_start="a", mark="a"), "a.docx")
        not_repeated = convert(_docx_with_custom_mark("footnote", "1", note_start="about", mark="a"), "a.docx")

        assert repeated.text == "배출량[^1] 기준\n\n[^1]: 추정치"
        assert not_repeated.text == "배출량[^1] 기준\n\n[^1]: about 추정치"


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


class TestDocxFieldCharactersInHiddenRuns:
    """A hidden run (w:vanish) was skipped whole, field characters included. With only the separate hidden, the field stayed on
    the instruction side and its visible result was dropped as instruction text. Only the text of a hidden run is left out."""

    @staticmethod
    def _field_char(kind: str, hidden: bool) -> str:
        properties = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
        return f'<w:r>{properties}<w:fldChar w:fldCharType="{kind}"/></w:r>'

    @staticmethod
    def _instruction(text: str, hidden: bool) -> str:
        properties = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
        return f'<w:r>{properties}<w:instrText xml:space="preserve">{text}</w:instrText></w:r>'

    @pytest.mark.parametrize("hidden", [{"separate"}, {"begin", "instruction", "separate", "end"}, {"end"}, set()],
                             ids=["separate", "whole-code", "end", "none"])
    def test_visible_result_is_kept(self, hidden):
        body = ("<w:p>" + _run("Total: ") + self._field_char("begin", "begin" in hidden)
                + self._instruction(" =SUM(ABOVE) ", "instruction" in hidden) + self._field_char("separate", "separate" in hidden)
                + _run("1,200") + self._field_char("end", "end" in hidden) + _run(" won") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "Total: 1,200 won"

    def test_hidden_field_with_its_result_stays_hidden(self):
        hidden_result = "<w:r><w:rPr><w:vanish/></w:rPr><w:t>42</w:t></w:r>"
        body = ("<w:p>" + _run("앞") + self._field_char("begin", True) + self._instruction(" PAGE ", True)
                + self._field_char("separate", True) + hidden_result + self._field_char("end", True) + _run("뒤") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "앞뒤"

    def test_hidden_enclosed_character_field_writes_nothing(self):
        body = ("<w:p>" + _run("앞") + self._field_char("begin", True) + self._instruction(" eq \\o\\ac(○,21) ", True)
                + self._field_char("end", True) + _run("뒤") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "앞뒤"

    @pytest.mark.parametrize("hidden", [{"begin"}, {"instruction"}, {"end"}, {"begin", "instruction"}],
                             ids=["begin", "instruction", "end", "begin and instruction"])
    def test_equation_field_with_a_hidden_part_writes_nothing(self, hidden):
        """An EQ field without a result is drawn from its instruction. With the instruction of a hidden run gathered (for the
        link of a HYPERLINK field), a field hidden but for its end came out as '1/2'."""
        body = ("<w:p>" + _run("앞") + self._field_char("begin", "begin" in hidden)
                + self._instruction(" EQ \\f(1,2) ", "instruction" in hidden) + self._field_char("end", "end" in hidden)
                + _run("뒤") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "앞뒤"
        assert convert(_docx_with_body(body.replace("<w:rPr><w:vanish/></w:rPr>", "")), "a.docx").text == "앞1/2뒤"

    @pytest.mark.parametrize("hidden", [{"result"}, {"instruction", "result"}, {"separate", "result"},
                                        {"begin", "instruction", "separate", "result"}, {"separate"}],
                             ids=["result", "instruction and result", "separate and result", "all but the end", "separate"])
    def test_equation_field_with_a_hidden_result_writes_nothing(self, hidden):
        """A field shows its result; a result that is hidden shows nothing, and the instruction does not stand in for it."""
        result = '<w:r><w:rPr><w:vanish/></w:rPr><w:t>RESULT</w:t></w:r>' if "result" in hidden else ""
        body = ("<w:p>" + _run("앞") + self._field_char("begin", "begin" in hidden)
                + self._instruction(" EQ \\f(1,2) ", "instruction" in hidden) + self._field_char("separate", "separate" in hidden)
                + result + self._field_char("end", False) + _run("뒤") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "앞뒤"

    def test_visible_result_of_a_link_field_with_a_hidden_instruction_is_a_link(self):
        body = ("<w:p>" + _run("앞 ") + self._field_char("begin", False)
                + self._instruction(' HYPERLINK "https://example.com/x" ', True) + self._field_char("separate", False)
                + _run("자료") + self._field_char("end", False) + _run(" 뒤") + "</w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "앞 [자료](https://example.com/x) 뒤"

    def test_form_field_in_a_hidden_run_writes_no_value(self):
        """The checked state of a legacy form field is an attribute of its begin character: in a hidden run it is hidden text."""
        def box(properties: str) -> str:
            return (f'<w:p>{_run("앞")}<w:r>{properties}<w:fldChar w:fldCharType="begin"><w:ffData><w:checkBox>'
                    '<w:default w:val="1"/></w:checkBox></w:ffData></w:fldChar></w:r>'
                    + self._instruction(" FORMCHECKBOX ", bool(properties))
                    + f'<w:r>{properties}<w:fldChar w:fldCharType="end"/></w:r>{_run("뒤")}</w:p>')

        assert convert(_docx_with_body(box("<w:rPr><w:vanish/></w:rPr>")), "a.docx").text == "앞뒤"
        assert convert(_docx_with_body(box("")), "a.docx").text == "앞☒뒤"
