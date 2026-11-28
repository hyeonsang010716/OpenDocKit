"""The character spacing (`Tc`) comes after every glyph, the last of a string too."""

from io import BytesIO
from types import SimpleNamespace

import pdfplumber
import pytest

from opendockit import convert
from opendockit.extractors import _pdf_pdfminer
from tests.test_pdf_clipped_text import _page


_BODY = b" BT /F1 12 Tf 1 0 0 1 72 650 Tm (A line of body text to set the measure of the page by.) Tj ET"
_A = 12 * 0.556  # the width of Helvetica's `a`, `b`, `c` and `d` at 12pt


def _line(text: bytes) -> str:
    """The first line of a page that draws `text` from (72, 700) in Helvetica 12pt, over a line of body text."""
    return convert(_page(b"BT /F1 12 Tf 1 0 0 1 72 700 Tm " + text + b" ET" + _BODY), "a.pdf").text.split("\n")[2]


def _places(text: bytes) -> dict[str, float]:
    """Where pdfminer, as the reader changes it, puts the left edge of each letter of `text` drawn from x = 72."""
    with pdfplumber.open(BytesIO(_page(b"BT /F1 12 Tf 1 0 0 1 72 700 Tm " + text + b" ET"))) as pdf:
        return {char["text"]: char["x0"] for char in pdf.pages[0].chars}


class TestSpacingAfterTheLastGlyphOfAString:
    """pdfminer adds the spacing before every glyph but the first of a string, so a string ends one spacing short and
    the next string of the line starts that much short of where it is drawn. With a spacing of 1.5pt the strings of an
    amount ran into each other and the reader, which takes the gaps of a line for its measure, wrote
    `$ 33 6 , 7 8 5`; on real pages `$ 3 36,785`, `availab le`, `I S BN`."""

    def test_an_amount_drawn_in_two_strings_is_read_whole(self):
        assert _line(b"1.5 Tc ($ 3) Tj (36,785) Tj") == "$ 336,785"

    def test_a_word_drawn_in_three_strings_is_read_whole(self):
        assert _line(b"3 Tc (pay) Tj (ment) Tj") == "payment"
        assert _line(b"6 Tc (GAZE) Tj (TA) Tj") == "GAZETA"

    def test_a_string_starts_one_spacing_after_the_glyph_before_it(self):
        places = _places(b"5 Tc (ab) Tj (c) Tj")

        assert places["b"] == pytest.approx(72 + _A + 5)
        assert places["c"] == pytest.approx(72 + 2 * (_A + 5))

    def test_every_operator_that_shows_text_ends_with_the_spacing(self):
        assert _places(b"5 Tc [(a) 0 (b)] TJ (c) Tj")["c"] == pytest.approx(72 + 2 * (_A + 5))
        assert _places(b"5 Tc (a) Tj 0 Tc (c) Tj")["c"] == pytest.approx(72 + _A + 5)   # the spacing it was drawn with
        assert _places(b"5 Tc (a) Tj -5 Tc (b) Tj (c) Tj")["c"] == pytest.approx(72 + 2 * _A)

    def test_the_spacing_is_scaled_across_with_the_text(self):
        assert _places(b"50 Tz 6 Tc (a) Tj (c) Tj")["c"] == pytest.approx(72 + _A / 2 + 3)

    def test_a_negative_spacing_sets_the_next_string_back(self):
        assert _places(b"-2 Tc (ab) Tj (c) Tj")["c"] == pytest.approx(72 + 2 * (_A - 2))

    def test_a_string_placed_anew_starts_where_it_is_put(self):
        places = _places(b"5 Tc (a) Tj 1 0 0 1 300 700 Tm (c) Tj 40 0 Td (d) Tj")

        assert places["c"] == pytest.approx(300) and places["d"] == pytest.approx(340)

    def test_a_string_without_a_glyph_adds_no_spacing(self):
        assert _places(b"5 Tc (a) Tj () Tj [] TJ [100] TJ (c) Tj")["c"] == pytest.approx(72 + _A + 5 - 1.2)

    def test_without_spacing_nothing_moves(self):
        assert _places(b"(ab) Tj (c) Tj")["c"] == pytest.approx(72 + 2 * _A)


class TestNumberBeforeTheFirstGlyph:
    """pdfminer takes a number that comes before the first glyph of a `TJ` array for a glyph drawn, and adds the
    spacing before that first glyph: the whole string stood one spacing on."""

    def test_the_first_glyph_stands_where_the_number_puts_it(self):
        places = _places(b"5 Tc [-1000 (ab)] TJ (c) Tj")

        assert places["a"] == pytest.approx(72 + 12)
        assert places["b"] == pytest.approx(72 + 12 + _A + 5)
        assert places["c"] == pytest.approx(72 + 12 + 2 * (_A + 5))

    def test_a_number_after_a_glyph_is_as_before(self):
        assert _places(b"5 Tc [(a) -1000 (c)] TJ")["c"] == pytest.approx(72 + _A + 5 + 12)


class TestSpacingLeftOut:
    def test_what_is_left_out_and_whether_a_number_leads(self):
        one_byte = SimpleNamespace(decode=lambda data: iter(data))
        two_bytes = SimpleNamespace(decode=lambda data: iter(range(len(data) // 2)))   # a font of two-byte codes

        def left_out(seq: object, charspace: object = 5.0, scaling: object = 100.0, font: object = one_byte) -> tuple[float, bool]:
            return _pdf_pdfminer._spacing_left_out(SimpleNamespace(charspace=charspace, scaling=scaling, font=font), seq)

        assert left_out([-20.5, b"ab"]) == (5.0, True)                                           # most numbers are no whole numbers
        assert left_out([b"a"], font=two_bytes) == (0.0, False)                                   # one byte of two is no glyph
        assert left_out([b"a", -20, b"ab"], font=two_bytes) == (5.0, True)

        assert left_out([b"ab"]) == (5.0, False) and left_out([-20, b"ab"]) == (5.0, True) and left_out([b"a", -20, b"b"]) == (5.0, False)
        assert left_out([b"", -20, b"b"]) == (5.0, True) and left_out([b"ab"], scaling=50.0) == (2.5, False)
        assert left_out([]) == (0.0, False) and left_out([b""]) == (0.0, False) and left_out([-20]) == (0.0, False)
        assert left_out([b"ab"], charspace=0.0) == (0.0, False) and left_out([b"ab"], font=None) == (0.0, False)
        assert left_out([b"ab"], charspace=float("inf")) == (0.0, False) and left_out([b"ab"], charspace="wide") == (0.0, False)
        assert left_out(None) == (0.0, False) and left_out([b"ab", object()]) == (5.0, False)

    def test_a_string_pdfminer_cannot_place(self):
        def placed(**state: object) -> bool:
            font = SimpleNamespace(is_multibyte=lambda: state.pop("multibyte", False))
            return _pdf_pdfminer._placed(SimpleNamespace(**{"charspace": 0.0, "scaling": 100.0, "wordspace": 0.0, "font": font, **state}))

        assert placed() and placed(charspace=3.0, wordspace=2.0)
        assert not placed(charspace=float("inf")) and not placed(charspace=float("nan")) and not placed(scaling=float("inf"))
        assert not placed(charspace="wide") and not _pdf_pdfminer._placed(object())
        multibyte = SimpleNamespace(is_multibyte=lambda: True)
        assert not placed(wordspace=2.0, font=multibyte) and placed(wordspace=0.0, font=multibyte)

    def test_a_font_written_downwards_moves_on_along_its_height(self):
        across = SimpleNamespace(linematrix=(10.0, 20.0), font=SimpleNamespace(is_vertical=lambda: False))
        downwards = SimpleNamespace(linematrix=(10.0, 20.0), font=SimpleNamespace(is_vertical=lambda: True))

        _pdf_pdfminer._move_on(across, 5.0)
        _pdf_pdfminer._move_on(downwards, 5.0)

        assert across.linematrix == (15.0, 20.0) and downwards.linematrix == (10.0, 25.0)
