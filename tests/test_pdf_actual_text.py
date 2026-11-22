"""Glyphs that are not characters: the text a marked-content sequence says its glyphs stand for (`ActualText`,
`_pdf_pdfminer._stand_for`), and the marks a font sets on a letter as glyphs of their own."""

from io import BytesIO

import pdfplumber
import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_pdfminer
from opendockit.extractors._pdf_chars import _text_from_chars
from tests.builder import pdf_of_glyphs


_OPTIONS = ConvertOptions(page_markers=False)
_GLYPH = 5.8  # the width of a glyph of `pdf_of_glyphs` at size 10


def _line(*parts: str | list[str] | tuple, **how) -> str:
    """The text read from one line drawn part after part; a part given as (glyphs, text) is drawn inside a sequence that
    says the glyphs stand for the text."""
    runs, x = [], 72.0
    for part in parts:
        glyphs, *actual = part if isinstance(part, tuple) else (part,)
        runs.append((x, 760, 10, glyphs, False, *actual))
        x += sum(_GLYPH for glyph in glyphs if glyph not in how.get("no_width", ""))
    return convert(pdf_of_glyphs(runs, **how), "a.pdf", _OPTIONS).text


def _chars_as_converted(data: bytes) -> list[dict]:
    """The characters of the first page as a conversion reads them (the replaced parts of pdfminer act only inside one)."""
    token = _pdf_pdfminer._INFLATE_BUDGET.set(_pdf_pdfminer._InflateBudget(10**9))
    try:
        return pdfplumber.open(BytesIO(data), laparams=_pdf_pdfminer._LAPARAMS).pages[0].chars
    finally:
        _pdf_pdfminer._INFLATE_BUDGET.reset(token)


class TestActualText:
    def test_glyphs_take_the_text_their_sequence_says_they_stand_for(self):
        """The vowel sign of Hindi that is drawn before its consonant is a glyph without a name; the sequence around it
        and the consonant says 'ति'."""
        assert _line("प्राक", (["\x00", "त"], "ति"), "क") == "प्राकतिक"

    def test_one_glyph_without_a_name_takes_its_text(self):
        assert _line("3", (["\x00"], "1"), "4") == "314"

    def test_a_letter_named_by_another_form_takes_the_letter_that_was_written(self):
        assert _line(("ھ", "ه"), "abc") == "هabc"

    @pytest.mark.parametrize("unseen", ["\xad", "\u200c", "\u200b", " "])
    def test_a_text_with_nothing_to_see_changes_nothing(self, unseen):
        """InDesign writes a soft hyphen over the space glyph at each place a word may be broken."""
        assert _line("these", (" ", unseen), "distributions") == "these distributions"

    def test_the_text_of_a_whole_line_is_not_put_on_one_glyph(self):
        """A sequence of many glyphs is a line given another wording; its glyphs keep their places and their text."""
        assert _line(("Alpha Beta Gamma Delta", "Another wording of the line")) == "Alpha Beta Gamma Delta"

    def test_the_same_text_as_the_glyphs_changes_nothing(self):
        assert _line(("fi", "fi"), "ne") == "fine"

    def test_a_cluster_in_arabic_is_read_in_its_place(self):
        """The glyphs of a cluster are one unit of the line, whose text is in the order of reading."""
        assert _line("دج", (["\x00", "\x00"], "لا"), "ب") == "بلاجد"


class TestSequencesInsideEachOther:
    def test_the_characters_after_an_inner_sequence_belong_to_the_outer_one(self):
        """pdfplumber kept one MCID, so a `/Span` inside the content of a cell took the MCID away from the rest of the
        cell: those characters belonged to no cell of a tagged table."""
        data = pdf_of_glyphs([(72, 760, 10, "ab", False), (72 + 2 * _GLYPH, 760, 10, ["\x00"], False, "c"),
                              (72 + 3 * _GLYPH, 760, 10, "de", False)], around=(b"/P << /MCID 7 >> BDC\n", b"EMC\n"))
        chars = _chars_as_converted(data)

        assert [(char["text"], char["mcid"]) for char in chars] == [("a", 7), ("b", 7), ("c", 7), ("d", 7), ("e", 7)]


class TestMarksSetOnALetter:
    def test_marks_without_width_do_not_end_their_line(self):
        """Thai sets a vowel under a letter and a tone mark over both, each a glyph that moves nothing. pdfminer never
        put two such glyphs on one line, so the line ended between them and its pieces came out in another order."""
        text = "ตั้งอยู่ที่ตำแหน่ง"
        lines = [text, "นี่คือย่อหน้าแรกของข้อความ", "เป็นดาวบริวารถาวรดวงเดียว"]
        marks = "\u0e31ิีึืุู็่้"
        runs, y = [], 760
        for line in lines:
            runs.append((72, y, 10, line, False))
            y -= 16

        data = pdf_of_glyphs(runs, no_width=marks)

        assert convert(data, "a.pdf", _OPTIONS).text.splitlines() == lines
        # each mark is part of its letter: no glyph of marks alone is left to be a line of its own
        texts = [char["text"] for char in _chars_as_converted(data)]
        assert "ยู่" in texts and "ตั้" in texts
        assert not any(text in marks for text in texts)

    def test_a_mark_drawn_over_its_letter_does_not_put_a_space_after_it(self):
        """A mark with a width of its own, set back over its letter, ends before the letter does; the next letter then
        started 'far' from the last glyph and a space was written into the word."""
        runs = [(72, 760, 10, "ke", False), (72 + _GLYPH - 1, 760, 10, "\u0301", False), (72 + 2 * _GLYPH + 0.3, 760, 10, "p", False)]

        assert convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS).text == "k\xe9p"

    def test_a_mark_at_the_place_the_next_letter_starts_stands_on_the_letter_before(self):
        """In the cell of a table the characters are read from left to right, and a mark that starts where the next
        letter starts came after that letter: 'จนั' for 'จัน'."""
        def char(text: str, x0: float, x1: float) -> dict:
            return {"text": text, "x0": x0, "x1": x1, "width": x1 - x0, "height": 10.0, "top": 0.0, "bottom": 10.0}

        chars = [char("จ", 0, 6), char("น", 6.0, 12), char("\u0e31", 6.05, 6.05), char("ท", 12, 18)]

        assert _text_from_chars(chars) == "จันท"
