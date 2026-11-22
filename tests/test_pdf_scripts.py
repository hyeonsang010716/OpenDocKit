"""The structure of a PDF is read the same whatever script it is written in: a letter is a letter of any script
(`_pdf_model._is_word_char`), not only a Latin one, a Hangul syllable or a Han character."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_model import _has_letter, _is_word_char, _runs_on, _word_chars
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


class TestTitlesInCornerBrackets:
    @pytest.mark.parametrize("title", ["『世界人権宣言』", "「개인정보 보호법」"])
    def test_a_title_set_in_corner_brackets_is_a_heading(self, title):
        """Corner brackets mark the name of a work or a law. Taken for the quotes of a saying, they kept the title of a
        document from being its heading."""
        body = "これはにほんごのさいしょのだんらくですのでよろしく"

        result = convert(pdf_of_glyphs([(72, 760, 18, title, False), *_body(body, 730)]), "a.pdf", _OPTIONS)

        assert result.text.splitlines()[0] == f"# {title}"

    def test_a_saying_in_quotes_is_still_no_heading(self):
        body = "これはにほんごのさいしょのだんらくですのでよろしく"
        saying = chr(0x201C) + "みんなでそだつしょくば" + chr(0x201D)

        result = convert(pdf_of_glyphs([(72, 760, 18, saying, False), *_body(body, 730)]), "a.pdf", _OPTIONS)

        assert "#" not in result.text


class TestTablesOfEveryScript:
    """A table without rules whose first column is a number: the number is set in a Latin font and stands at another
    height than the cells in the font of the script beside it."""

    _INTRO = "यह एक लंबा अनुच्छेद है जो तालिका से पहले आता है"

    def _table(self, cells: list[tuple[str, str]], *, step: float = 4.6, third: float = 0.6, pitch: float = 24,
               wrapped: dict[int, str] | None = None) -> str:
        """The page of a table of three columns — 1, 2, 3 …, then the two texts of each row — whose numbers stand `step`
        higher than the cells and whose third column stands `third` lower than the second, the rows `pitch` apart;
        `wrapped` gives rows a second line in the third column, a line (12) under the first."""
        runs = [(72, 790 - 14 * line, 10, self._INTRO, False) for line in range(3)]
        y = 720.0
        for number, (second, last) in enumerate(cells, 1):
            runs += [(72, y + step, 10, str(number), False), (200, y, 10, second, False), (330, y - third, 10, last, False)]
            if wrapped and number in wrapped:
                y -= 12
                runs.append((330, y - third, 10, wrapped[number], False))
            y -= pitch
        return convert(pdf_of_glyphs(runs), "a.pdf", _OPTIONS).text

    @pytest.mark.parametrize("script", ["hindi", "thai", "hebrew"])
    def test_the_number_of_a_row_stands_in_its_row(self, script):
        """The fragments of a row at heights out of step were one row only between Hangul or Han and other text. The
        numbers beside Hindi, Thai, Bengali or Tamil cells were rows of their own, and no table was found."""
        cells = {"hindi": [("अनुच्छेद", "सभी मनुष्यों को"), ("धारा", "सभी को इस"), ("भाग", "प्रत्येक व्यक्ति"), ("खंड", "कोई भी नहीं")],
                 "thai": [("ข้อหนึ่ง", "มนุษย์ทั้งหลาย"), ("ข้อสอง", "ทุกคนย่อมมี"), ("ข้อสาม", "คนทุกคนมี"), ("ข้อสี่", "บุคคลใดจะ")],
                 "hebrew": [("ףיעס", "םדא ינב לכ"), ("קרפ", "םדא לכ"), ("קלח", "שיא לכ"), ("תוא", "דחא ףא")]}[script]

        table = [line for line in self._table(cells).splitlines() if line.startswith("|")]

        assert len(table) == 5
        assert all(line.count("|") == 4 for line in table)
        assert [line.split("|")[1].strip() for line in (table[0], *table[2:])] == ["1", "2", "3", "4"]

    @pytest.mark.parametrize(("step", "found"), [(5.5, True), (6.5, False)])
    def test_how_far_out_of_step_the_number_may_stand(self, step, found):
        """Bengali beside a number stands 0.56 of the size lower, which leaves 0.44 of it shared; a number that shares
        a third of its height with a line is as likely the number of the line above or below."""
        cells = [("अनुच्छेद", "सभी मनुष्यों को"), ("धारा", "सभी को इस"), ("भाग", "प्रत्येक व्यक्ति"), ("खंड", "कोई भी नहीं")]

        assert ("| 1 |" in self._table(cells, step=step)) is found

    def test_a_cell_in_a_script_without_spaces_is_seen_to_end(self):
        """A line of Japanese is broken after any character, so what had no room on the line above is one character and
        not the 'word' a line without spaces is: taken for a word, no cell was ever seen to end, and a table whose
        cells wrap with no more room between two rows than between two lines was not found."""
        cells = [("第１条", "すべての人間は生まれな"), ("第２条", "すべて人は人種皮膚の色"), ("第３条", "すべての人は生命"), ("第４条", "何人も奴隷にされ又は")]

        text = self._table(cells, pitch=12, wrapped={1: "がらにして", 2: "性言語", 4: "苦役に"})
        table = [line for line in text.splitlines() if line.startswith("|")]

        # the lines of a cell are joined by nothing: the line was broken inside running text
        assert table == ["| 1 | 第１条 | すべての人間は生まれながらにして |", "| --- | --- | --- |", "| 2 | 第２条 | すべて人は人種皮膚の色性言語 |",
                         "| 3 | 第３条 | すべての人は生命 |", "| 4 | 第４条 | 何人も奴隷にされ又は苦役に |"]

    def test_latin_fragments_at_heights_out_of_step_are_still_rows_of_their_own(self):
        """Without a letter of another script the heights say what they said: the pieces of a formula out of step are
        not the cells of a row."""
        cells = [("Article", "All human beings"), ("Section", "Everyone is"), ("Part", "Each person"), ("Clause", "No one shall")]

        assert "| 1 |" not in self._table(cells)

    def test_a_header_of_words_of_one_letter_is_a_header(self):
        """'제 1 조' holds two words and no two letters side by side. A table whose cells wrap, with no more room between
        two rows than between the lines of a cell, needs a header with a word in each column but one — and a word was
        two letters."""
        cells = [("제 1 조", "모든 인간은"), ("제 2 조", "모든 사람은 인종"), ("제 3 조", "모든 사람은 생명과"), ("제 4 조", "어느 누구도 노예")]

        text = self._table(cells, pitch=12, wrapped={2: "피부색", 3: "신체의"})
        table = [line for line in text.splitlines() if line.startswith("|")]

        assert table == ["| 1 | 제 1 조 | 모든 인간은 |", "| --- | --- | --- |", "| 2 | 제 2 조 | 모든 사람은 인종 피부색 |",
                         "| 3 | 제 3 조 | 모든 사람은 생명과 신체의 |", "| 4 | 제 4 조 | 어느 누구도 노예 |"]


class TestLinesOfRunningText:
    @pytest.mark.parametrize(("above", "below"), [("すべて人は、人種、皮", "膚の色"), ("人人生而自由", "在尊严"), ("บุคคลใด ๆ จะถูกยึดเป็น", "ทาส"), ("មានសិទ្ធិ", "ឱ្យគេ")])
    def test_a_line_of_a_script_without_spaces_goes_on_into_the_next(self, above, below):
        """The lines of a cell are joined by a space; in Japanese, Chinese, Thai or Khmer that put a space into a word
        or between two words that have none."""
        assert _runs_on(above, below)

    @pytest.mark.parametrize(("above", "below"), [("the inherent", "dignity"), ("모든 사람은", "인종"), ("第１条", "Article"), ("Article", "第１条"), ("", "膚")])
    def test_any_other_line_is_set_apart_from_the_next_by_a_space(self, above, below):
        assert not _runs_on(above, below)


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
