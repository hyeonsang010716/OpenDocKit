"""The structure of a PDF is read the same whatever script it is written in: a letter is a letter of any script
(`_pdf_model._is_word_char`), not only a Latin one, a Hangul syllable or a Han character."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_model import _has_letter, _is_word_char, _word_chars
from tests.builder import pdf_of_glyphs


_OPTIONS = ConvertOptions(page_markers=False)
# script → (a title, a line of a paragraph)
_SCRIPTS = {
    "Cyrillic": ("Введение в проект", "Это первый абзац текста на русском языке о проекте"),
    "Greek": ("Εισαγωγή στο έργο", "Αυτή είναι η πρώτη παράγραφος κειμένου στα ελληνικά"),
    "kana": ("はじめに", "これはにほんごのさいしょのだんらくですのでよろしく"),
    "Armenian": ("Ներածություն նախագծին", "Սա հայերեն տեքստի առաջին պարբերությունն է նախագծի մասին"),
    "Georgian": ("შესავალი პროექტში", "ეს არის ქართული ტექსტის პირველი აბზაცი პროექტის შესახებ"),
    "Latin with accents": ("Élan général", "Voilà le premier paragraphe du texte français sur ce projet"),
}


def _body(line: str, top: float, rows: int = 4) -> list[tuple[float, float, float, str, bool]]:
    return [(72, top - 14 * row, 10, line, False) for row in range(rows)]


class TestLettersOfEveryScript:
    @pytest.mark.parametrize("script", _SCRIPTS)
    def test_a_line_set_larger_than_the_text_is_a_heading(self, script):
        """Asked for a Latin, Hangul or Han letter, the title of a Russian, Greek or Japanese document had no letter in it
        and was a paragraph like the others."""
        title, line = _SCRIPTS[script]

        result = convert(pdf_of_glyphs([(72, 760, 18, title, False), *_body(line, 730)]), "a.pdf", _OPTIONS)

        assert result.text.splitlines()[0] == f"# {title}"

    @pytest.mark.parametrize("script", _SCRIPTS)
    def test_a_short_bold_line_between_paragraphs_is_a_heading(self, script):
        """Whether a line is bold is asked of its letters and digits: a line without a Latin one was never bold."""
        title, line = _SCRIPTS[script]

        result = convert(pdf_of_glyphs([*_body(line, 760), (72, 690, 10, title, True), *_body(line, 665)]), "a.pdf", _OPTIONS)

        assert f"# {title}" in result.text.splitlines()

    @pytest.mark.parametrize("script", ["Cyrillic", "Greek", "kana"])
    def test_a_line_drawn_in_two_pieces_out_of_order_is_one_line(self, script):
        """The pieces a line is drawn in are joined where one starts at the end of the other, when both hold a letter:
        the start of a Japanese sentence drawn after its end stood under it as a paragraph of its own."""
        _, line = _SCRIPTS[script]
        cut = 12
        first_part_end = 72 + cut * 5.8
        runs = [(first_part_end, 760, 10, line[cut:], False), (72, 760, 10, line[:cut], False), *_body(line, 746, rows=3)]

        result = convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS)

        assert result.text.splitlines()[0] == line


class TestWordCharacters:
    @pytest.mark.parametrize("letter", ["a", "É", "Ж", "λ", "あ", "カ", "한", "漢", "ש", "ع", "क", "ก", "7", "٣", "७"])
    def test_a_letter_or_a_digit_of_any_script_is_one(self, letter):
        assert _is_word_char(letter)
        assert _word_chars(letter * 3) == 3

    @pytest.mark.parametrize("sign", ["¼", "²", "①", "Ⅳ", "•", "—", "%", " ", "", "\u0301"])
    def test_a_sign_or_a_number_that_is_no_digit_is_none(self, sign):
        """'¼' and 'ð' are what a formula's glyphs come out as when their font does not say what they are: counted as
        letters, the pieces of formulas were joined differently on two papers of the benchmark."""
        assert not _is_word_char(sign)
        assert _word_chars(sign) == 0

    def test_a_number_or_a_symbol_alone_holds_no_letter(self):
        assert not _has_letter("2024 — 12.5%")
        assert _has_letter("2024 год")
