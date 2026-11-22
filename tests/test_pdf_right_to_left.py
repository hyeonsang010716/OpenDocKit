"""Hebrew and Arabic in a PDF (`_pdf_bidi`): a page holds glyphs at places, from left to right the text backwards, and
the reader brings each line back to the order it is read in."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_bidi import _PAGE_READING, _logical, _page_reading, _Reading, _visual_units
from opendockit.extractors._pdf_chars import _text_from_chars
from tests.builder import pdf_of_glyphs


_OPTIONS = ConvertOptions(page_markers=False)
_GLYPH = 5.8  # the width of a glyph of `pdf_of_glyphs` at size 10


def _page(*lines: str | list[str], producer: str | None = None) -> str:
    """The text read from a page whose lines are drawn from left to right as given, one under the other."""
    runs = [(72, 760 - 16 * number, 10, line, False) for number, line in enumerate(lines)]
    return convert(pdf_of_glyphs(runs, producer=producer), "a.pdf", _OPTIONS).text


@pytest.fixture
def reading():
    """Sets what a page says to the lines read by `_logical` in a test, and takes it back."""
    tokens = []

    def set_reading(**said) -> None:
        tokens.append(_PAGE_READING.set(_Reading(**said)))

    yield set_reading
    for token in reversed(tokens):
        _PAGE_READING.reset(token)


class TestLinesAreReadInTheirDirection:
    @pytest.mark.parametrize(("on_the_page", "read"), [
        ("םלוע םולש", "שלום עולם"),
        ("ملاعلاب ابحرم", "مرحبا بالعالم"),
        ("دراو ںیم ودرا", "اردو میں وارد"),
    ])
    def test_a_line_is_read_from_its_right_end(self, on_the_page, read):
        """Read from left to right, as every line was, the text was backwards letter by letter."""
        assert _page(on_the_page) == read

    def test_numbers_and_latin_words_inside_the_line_are_read_from_their_left_end(self):
        assert _page("2030 ماعل Windows 10 ماظن") == "نظام Windows 10 لعام 2030"

    def test_a_number_with_a_sign_or_a_point_stays_one_number(self):
        assert _page("12.5% הלע ריחמה") == "המחיר עלה 12.5%"
        assert _page("1,234 ددعب 25/9/2015 يف") == "في 25/9/2015 بعدد 1,234"

    def test_a_range_of_numbers_in_arabic_text_is_read_from_its_right_end(self):
        """After Arabic letters a number is an Arabic number, which a hyphen does not join to the next: the two years
        stand apart, the first to the right, as the algorithm lays them out. Hebrew keeps the range as one number."""
        assert _page("2030-2015 ةرتفلل") == "للفترة 2015-2030"
        assert _page("2015-2030 הפוקתל") == "לתקופה 2015-2030"

    def test_a_glyph_of_several_letters_is_kept_as_it_is_named(self):
        """The ligature of lam and alef is one glyph named 'لا', in the order of reading. Turned round letter by letter
        with the line, the word came out as 'سالم'."""
        assert _page(["م", "لا", "س"]) == "سلام"

    def test_a_letter_named_by_its_form_is_the_letter(self):
        """A browser names the shape a letter is drawn in at its place in the word (U+FEAE, U+FEE4, U+FED7)."""
        assert _page("\ufeae\ufee4\ufed7") == "قمر"

    def test_the_stroke_that_stretches_a_word_is_no_part_of_it(self):
        assert _page("ةدـحتملا ممــألا") == "الأمم المتحدة"

    def test_a_large_line_is_a_heading(self):
        runs = [(72, 760, 18, "םלוע םולש", False), *((72, 730 - 14 * row, 10, "הקידבל תירבעב טסקט והז", False) for row in range(4))]

        result = convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS)

        assert result.text.splitlines()[0] == "# שלום עולם"
        assert result.text.splitlines()[2] == "זהו טקסט בעברית לבדיקה"

    def test_a_line_without_such_letters_is_left_as_it_stands(self):
        assert _page("A/RES/70/1*", "םלוע םולש") == "A/RES/70/1*\nשלום עולם"


class TestTheEndsOfALine:
    def test_the_full_stop_at_the_left_end_closes_the_line(self):
        assert _page(".םלוע םולש") == "שלום עולם."

    def test_arabic_typed_into_a_line_from_left_to_right_keeps_its_full_stop_and_the_order_of_its_parts(self):
        """In a paragraph laid out from left to right the Arabic words are turned round where they stand: the full stop
        stays at the right end and the Latin word between the two halves of the sentence stays between them."""
        assert _page("ةملكو صن اذه English طسولا يف.") == "هذا نص وكلمة English في الوسط."

    def test_the_lines_of_a_page_that_do_not_show_their_direction_follow_those_that_do(self):
        """'English' to the left of two Arabic words is the last word of a line from right to left and the first of one
        from left to right. The page says which."""
        assert _page("English نيميلا نم", ".راسيلا ىلإ").splitlines()[0] == "من اليمين English"
        assert _page("English نيميلا نم", "راسيلا ىلإ.").splitlines()[0] == "English من اليمين"


class TestBrackets:
    def test_a_bracket_named_as_written_and_one_named_as_drawn_read_the_same(self):
        """Word and LibreOffice name the bracket that was typed; a browser names the one that is drawn, its mirror image
        inside right-to-left text. Whichever closes its pairs is the reading."""
        assert _page(".הקידבל )םיירגוסב( טסקט") == "טקסט (בסוגריים) לבדיקה."
        assert _page(".הקידבל (םיירגוסב) טסקט") == "טקסט (בסוגריים) לבדיקה."

    def test_a_bracket_alone_on_its_line_goes_by_the_page(self):
        """A bracket opened on one line and closed on the next says nothing by itself."""
        as_drawn = _page(".הקידבל (םיירגוסב) טסקט", "לודג רפסמ) דוע")
        as_written = _page(".הקידבל )םיירגוסב( טסקט", "לודג רפסמ( דוע")

        assert as_drawn.splitlines()[1] == as_written.splitlines()[1] == "עוד (מספר גדול"


class TestNumbersOfWord:
    def test_two_numbers_with_a_slash_in_arabic_text_made_by_word_are_read_from_the_right(self):
        """Word sets '70/1' in Arabic text as two numbers, the first to the right: '1/70' on the page. Another program
        keeps the number as one, and so it is read."""
        assert _page("1/70 رارقلا", producer="Microsoft Word 2010") == "القرار 70/1"
        assert _page("70/1 رارقلا", producer="LibreOffice 26.2") == "القرار 70/1"

    def test_a_number_after_latin_words_is_not_touched(self):
        assert _page("A/CONF.219/7 ةقيثولا", producer="Microsoft Word 2010") == "الوثيقة A/CONF.219/7"


class TestLinesDrawnRunByRun:
    def test_the_runs_of_a_line_drawn_from_the_right_are_one_line(self):
        """Word draws a line of Arabic run by run from its right end, each run to the left of the one before. Each was
        a line of its own, and the words of the line came out as paragraphs in the order of the page."""
        words = ["دنبلا", "15", "لودج", "لامعألا"]  # as drawn: from the right end of the line, each from left to right
        runs, right = [], 400.0
        for word in words:
            left = right - len(word) * _GLYPH
            runs.append((left, 760, 10, word, False))
            right = left - _GLYPH * 0.3
        runs += [(72, 740 - 14 * row, 10, "ممألا ةدحتملا ةماعلا ةيعمجلا", False) for row in range(3)]

        result = convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS)

        assert result.text.splitlines()[0] == "البند 15 جدول الأعمال"

    def test_text_drawn_letter_by_letter_from_the_right_is_read_the_same(self):
        """Drawn in the order of reading, each letter to the left of the one before, the line is the same line."""
        letters = "שלום"
        runs = [(300 - index * _GLYPH, 760, 10, letter, False) for index, letter in enumerate(letters)]

        assert convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS).text == "שלום"


class TestListNumbers:
    def test_the_number_to_the_right_of_its_item_stands_before_it(self):
        """The number of a list item is set apart to the right of its text: '.1' on the page, with no letter that would
        say how to read it, and after its item when a row is read from the left."""
        runs = []
        for row, (number, item) in enumerate([(".1", ".המדקה :ןושארה ףיעסה"), (".2", ".תואצות :ינשה ףיעסה")]):
            y = 760 - 16 * row
            runs += [(400 - len(item) * _GLYPH, y, 10, item, False), (412, y, 10, number, False)]

        result = convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS)

        assert result.text.splitlines() == ["1. הסעיף הראשון: הקדמה.", "2. הסעיף השני: תוצאות."]

    def test_a_number_alone_on_a_page_read_from_the_left_is_left_alone(self):
        assert ".5" in _page(".5", "The value is small.")


def _chars(units: list[tuple[str, float, float]]) -> list[dict]:
    return [{"text": text, "x0": x0, "x1": x1, "width": x1 - x0, "height": 10.0, "top": 0.0, "bottom": 10.0} for text, x0, x1 in units]


class TestGlyphsOfALine:
    def test_a_mark_belongs_to_the_letter_under_its_middle(self):
        """The damma over 'ي' in 'يُقصد' is a glyph of its own, set over its letter: it follows the letter it stands
        over, whichever of the two starts further left."""
        letters = [("د", 0, 6), ("ص", 6, 12), ("ق", 12, 18), ("ي", 18, 24)]

        over_the_middle = _text_from_chars(_chars([*letters, ("\u064f", 20, 22)]))
        before_its_letter = _text_from_chars(_chars([*letters, ("\u064f", 17.5, 19.5)]))

        assert over_the_middle == before_its_letter == "يُقصد"

    def test_a_space_glyph_drawn_under_the_next_letter_is_no_space(self):
        """A browser draws the zero-width non-joiner of a Persian word as the space glyph of its font, without moving on:
        'می\u200cشود' is one word."""
        chars = _chars([("د", 0, 6), ("و", 6, 12), ("ش", 12, 18), (" ", 18, 21), ("ی", 18, 24), ("م", 24, 30)])

        assert _text_from_chars(chars) == "میشود"

    def test_a_space_glyph_between_two_words_is_a_space(self):
        chars = _chars([("ب", 0, 6), (" ", 6, 9), ("ا", 9, 15)])

        assert _text_from_chars(chars) == "ا ب"

    def test_a_line_without_such_letters_is_joined_as_it_was(self):
        assert _visual_units(_chars([("a", 0, 6), ("b", 6, 12)]), 0.1) == ["a", "b"]
        assert _logical(["a", "b", " ", "1", "."]) == "ab 1."


class TestWhatThePageSays:
    def test_a_page_without_such_letters_says_nothing(self):
        assert _page_reading(_chars([("a", 0, 6), (".", 6, 12)]), 0.1) == _Reading()

    def test_most_letters_say_the_direction_and_a_full_stop_says_it_first(self):
        hebrew = [("ם", 6, 12), ("ו", 12, 18), ("ל", 18, 24), ("ש", 24, 30)]

        assert _page_reading(_chars(hebrew), 0.1).leftwards is True
        assert _page_reading(_chars([*hebrew, (".", 30, 36)]), 0.1).leftwards is False
        assert _page_reading(_chars([(".", 0, 6), *hebrew]), 0.1).leftwards is True

    def test_a_word_of_hebrew_among_latin_words_is_turned_where_it_stands(self, reading):
        reading()

        assert _logical(list("The word םולש means peace.")) == "The word שלום means peace."

    def test_the_page_decides_for_a_line_that_does_not(self, reading):
        reading(leftwards=True)
        assert _logical(list("PDF ץבוק")) == "קובץ PDF"

        reading(leftwards=False)
        assert _logical(list("PDF ץבוק")) == "PDF קובץ"
