"""PDF lines with widened character spacing (Tc): pdfminer put a space after every character, splitting numbers and words."""


import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


def _spaced_lines(spacing: float, lines: list[tuple[float, float, str]], size: float = 10) -> str:
    """Text of a page on which each (x, y, text) line is written with a character spacing of `spacing` pt."""
    def draw(pdf, index):
        for x, y, text in lines:
            spaced = pdf.beginText(x, y)
            spaced.setFont(_FONT, size)
            spaced.setCharSpace(spacing)
            spaced.textOut(text)
            pdf.drawText(spaced)

    return convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text


class TestWideCharacterSpacing:
    """When the character spacing exceeded 10% of the font size it tripped pdfminer's word-gap threshold (word_margin 0.1)
    and the text became '1 , 2 3 4'."""

    def test_line_with_uniform_wide_spacing_keeps_its_words(self):
        text = _spaced_lines(1.5, [(50, 780, "총 사업비는 1,234,567천원이며 기간은 2025.01.01~2025.12.31이다.")])

        assert "총 사업비는 1,234,567천원이며 기간은 2025.01.01~2025.12.31이다." in text

    def test_borderless_table_cells_with_wide_spacing_keep_their_numbers(self):
        rows = [("인건비", "45,600", "3%"), ("운영비", "12,300", "10%"), ("합계", "57,900", "17%")]
        lines = [(x, 700 - index * 16, cell) for index, row in enumerate(rows) for x, cell in zip((50, 250, 400), row, strict=True)]

        text = _spaced_lines(1.5, lines)

        assert "| 인건비 | 45,600 | 3% |" in text and "| 합계 | 57,900 | 17% |" in text

    def test_line_with_ordinary_spacing_is_unchanged(self):
        assert "총 사업비는 1,234,567천원이다." in _spaced_lines(0.3, [(50, 780, "총 사업비는 1,234,567천원이다.")])

    def test_a_word_in_synthetic_italic_does_not_keep_its_line_spaced_out(self):
        """A word in synthetic italic (an oblique text matrix, how LibreOffice and Word slant a Korean font) has its character
        boxes widened by the slant, so its gaps were negative, and the whole letter-spaced line stayed spaced out
        ('매 출 을 보 고 서 현 황 크게 것이다 향 후 현 황')."""
        def draw(pdf, index):
            x = 72
            for words, slanted in [("매출을 보고서 현황 ", False), ("크게 것이다 ", True), ("향후 현황", False)]:
                run = pdf.beginText(x, 700)
                if slanted:
                    run.setTextTransform(1, 0, 0.35, 1, x, 700)
                run.setFont(_FONT, 9)
                run.setCharSpace(2)
                run.textOut(words)
                pdf.drawText(run)
                x += pdf.stringWidth(words, _FONT, 9) + 2 * len(words)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "매출을 보고서 현황 크게 것이다 향후 현황"


def _grid_text(rows: list[tuple[float, float, str]], *, pitch: float = 5.42, size: float = 9.0, font: str = "Helvetica",
               typed: tuple[tuple[float, float, str], ...] = ()) -> str:
    """The text of a page on which each (x, y, text) row is drawn one character to a cell of `pitch`, the middle of the
    character in the middle of its cell, as a report made for a printer of fixed pitch is drawn in a proportional font.
    The rows of `typed` are written as usual, each character behind the one before."""
    from io import BytesIO

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    if font == _FONT:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(612, 792))
    pdf.setFont(font, size)
    for x, y, text in rows:
        for index, letter in enumerate(text):
            if letter != " ":
                pdf.drawCentredString(x + index * pitch, y, letter)
    for x, y, text in typed:
        pdf.drawString(x, y, text)
    pdf.showPage()
    pdf.save()
    return convert(buffer.getvalue(), "a.pdf", ConvertOptions(page_markers=False)).text


class TestTextSetOnAGrid:
    """The statement of a bank, made for a printer of fixed pitch and drawn in Helvetica: the middle of every letter stands
    on a grid of 5.42pt at 9pt. The letters are narrower than their cells, and a space was read after every one of them:
    'A c c o u n t No T y p e o f A c c o u n t', '8 , 9 2 3 . 0 7'."""

    _ROWS = [(40, 700, "Account No      Type of Account     Current Balance"),
             (40, 688, "735720          COMMERCIAL CHECKING        8,923.07    20"), (40, 664, "Previous Balance        11,180.08")]

    def test_words_end_where_a_cell_is_empty_and_nowhere_else(self):
        text = _grid_text(self._ROWS)

        for piece in ("Account No", "Type of Account", "Current Balance", "735720", "COMMERCIAL CHECKING", "8,923.07",
                      "Previous Balance", "11,180.08"):
            assert piece in text.split("\n"), piece

    def test_short_piece_of_the_page_is_read_on_the_grid_of_its_lines(self):
        """'20' alone shows no grid; its two digits stand a cell apart on the grid of the page."""
        assert "20" in _grid_text(self._ROWS).split("\n")
        assert "2 0" not in _grid_text(self._ROWS)

    def test_text_of_a_fixed_pitch_font_is_as_before(self):
        """Courier fills its cells: no space is read between its letters, and nothing is done."""
        text = _grid_text([(40, 700, "Account No      Type of Account")], pitch=5.4, font="Courier")

        assert "Account No" in text and "Type of Account" in text

    def test_letters_a_cell_wider_than_their_size_apart_stay_apart(self):
        """Wider apart than the size of the letters, a row of letters is the cells of a row, not a word."""
        text = _grid_text([(40, 700, "ABCDEFGHIJ")], pitch=14.0)

        assert "ABCDEFGHIJ" not in text

    def test_ordinary_text_stands_on_no_grid(self):
        from io import BytesIO

        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(612, 792))
        pdf.setFont("Helvetica", 9)
        pdf.drawString(40, 700, "An ordinary line of a report, set in a proportional font as usual.")
        pdf.showPage()
        pdf.save()

        assert "An ordinary line of a report, set in a proportional font as usual." in convert(buffer.getvalue(), "a.pdf").text

    def test_line_with_a_letter_off_the_grid_is_left_as_it_was_read(self):
        """One letter half a cell out of step: the line is no line of the grid."""
        on = _grid_text([(40, 700, "Account Numbers")])
        off = _grid_text([(40, 700, "Account"), (40 + 8 * 5.42 + 2.7, 700, "Numbers")])

        assert "Account Numbers" in on.split("\n")
        assert "Account Numbers" not in off and "Account" not in off.split() and "Numbers" not in off.split()

    def test_digits_written_with_spaces_on_a_page_with_a_grid_keep_their_spaces(self):
        """The spaces of such a line are characters of the line: it reads as it is written, also beside lines on a grid."""
        text = _grid_text(self._ROWS, typed=((40, 600, "1 2 3 4 5 6 7 8 9"), (40, 580, "A list of rooms: 1 2 3 4 5 6 7 8 9")))

        assert "1 2 3 4 5 6 7 8 9" in text.split("\n") and "A list of rooms: 1 2 3 4 5 6 7 8 9" in text.split("\n")
        assert "Account No" in text.split("\n")

    def test_digit_in_a_cell_of_text_set_solid_is_not_set_apart(self):
        """East Asian text is set on a grid of its character size, and a digit of half that width stands in the middle of
        a cell of its own: the annual report of a company read 'no. 1' with a space on either side of the 1 in a title
        that has no space character there."""
        text = _grid_text([(40, 700, "공고제1호비경상손익항목의구분"), (40, 680, "가치가4만원보다낮은임대자산")], pitch=9.0, font=_FONT)

        assert "공고제1호비경상손익항목의구분" in text and "가치가4만원보다낮은임대자산" in text

    def test_page_without_a_long_line_on_a_grid_is_as_before(self):
        """Seven letters show no grid: fewer than the eight a line has to have."""
        assert "Account" not in _grid_text([(40, 700, "Account")]).split("\n")
        assert "Accounts" in _grid_text([(40, 700, "Accounts")]).split("\n")


class TestGridOfALine:
    """`_fixed_pitches`, `_fixed_pitch_text` and `_cells_apart` on characters given by their places."""

    @staticmethod
    def _chars(text: str, *, pitch: float = 5.42, size: float = 9.0, width: float = 4.0, start: float = 40.0,
               own_spaces: bool = True, laid_over: float | None = None, lower: float = 0.6):
        """One character to a cell, `width` wide and centred, the space characters of the line (2.2 wide) in cells of their
        own unless `own_spaces` is off; with `laid_over`, a row of space characters over the line whose grid stands that
        far to the right of the letters' and whose baseline is `lower` under theirs."""
        chars = [{"text": letter, "x0": start + index * pitch - (width if letter != " " else 2.2) / 2,
                  "x1": start + index * pitch + (width if letter != " " else 2.2) / 2, "size": size,
                  "width": width if letter != " " else 2.2, "height": size, "top": 314.0, "bottom": 323.0}
                 for index, letter in enumerate(text) if letter != " " or own_spaces]
        if laid_over is not None:
            chars += [{"text": " ", "x0": start + laid_over + index * pitch - 1.1, "x1": start + laid_over + index * pitch + 1.1,
                       "size": size, "width": 2.2, "height": size, "top": 314.0 + lower, "bottom": 323.0 + lower}
                      for index in range(len(text))]
        return chars

    @staticmethod
    def _typed(text: str, *, size: float = 9.0, digit: float = 5.0, space: float = 2.5):
        """Text written as usual, each character behind the one before: digits of one width, a space character between."""
        chars, x = [], 40.0
        for letter in text:
            width = space if letter == " " else digit
            chars.append({"text": letter, "x0": x, "x1": x + width, "size": size, "width": width, "height": size, "top": 314.0,
                          "bottom": 323.0})
            x += width
        return chars

    def test_cells_apart_is_a_whole_number_of_pitches_or_nothing(self):
        from opendockit.extractors._pdf_chars import _cells_apart

        assert [_cells_apart(step, 5.42) for step in (5.42, 5.9, 4.9, 10.84, 16.3, 5.97, 4.87, 8.1, 0.0, -5.42)] == [1, 1, 1, 2, 3, 0, 0, 0, 0, 0]
        assert _cells_apart(float("nan"), 5.42) == 0 and _cells_apart(5.42, 0.0) == 0 and _cells_apart(float("inf"), 5.42) == 0

    def test_grid_of_a_page_is_the_pitch_of_a_long_line_of_narrow_letters(self):
        from opendockit.extractors._pdf_chars import _fixed_pitches

        assert _fixed_pitches([self._chars("Account No  Type")]) == [(pytest.approx(5.42), 9.0)]
        assert _fixed_pitches([self._chars("Account No  Type"), self._chars("Previous Balance")]) == [(pytest.approx(5.42), 9.0)]

    @pytest.mark.parametrize(("chars", "why"), [
        ({"text": "Account"}, "seven letters"),
        ({"text": "Acc oun t"}, "seven letters and two space characters"),
        ({"text": "Account No  Type", "width": 5.2, "own_spaces": False}, "letters that fill their cells, with empty cells between words"),
        ({"text": "Account No  Type", "width": 5.2}, "letters that fill their cells: no space is read between them"),
        ({"text": "Account No  Type", "pitch": 9.5}, "a pitch wider than the size"),
        ({"text": "Account No  Type", "pitch": 2.5, "width": 1.0}, "a pitch under three tenths of the size"),
        ({"text": "A c c o u n t N o T"}, "no two letters in neighbouring cells"),
        ({"text": "Ac c o u n t N o T y", "own_spaces": False}, "one step of a single cell, not four"),
    ], ids=lambda value: value if isinstance(value, str) else None)
    def test_line_that_shows_no_grid(self, chars, why):
        from opendockit.extractors._pdf_chars import _fixed_pitches

        assert _fixed_pitches([self._chars(**chars)]) == [], why

    def test_letters_drawn_over_each_other_or_at_no_place_show_no_grid_and_raise_nothing(self):
        """A word in fake bold is drawn four times at one place; a place that is no number came from a broken matrix."""
        from opendockit.extractors._pdf_chars import _fixed_pitch_text, _fixed_pitches

        over = self._chars("Account No  Type") * 2
        broken = self._chars("Account No  Type")
        broken[3]["x0"] = float("nan")
        behind = self._chars("Account No  Type")  # a wide letter whose middle lies right of the middle of the next one
        behind[-2]["x1"] += 12
        late = self._chars("Account No") + self._chars("Type of Acc", start=40 + 12 * 5.42 + 2.7)  # half a cell out of step
        for chars in (over, broken, behind, late, [], self._chars("    ")):
            assert _fixed_pitches([chars]) == []
            assert _fixed_pitch_text(chars, [(5.42, 9.0)]) is None

        no_bottom = self._chars("Account No  Type")
        no_bottom[1]["bottom"] = float("nan")
        _fixed_pitches([no_bottom])
        _fixed_pitch_text(no_bottom, [(5.42, 9.0)])

    def test_space_characters_of_a_row_laid_over_the_line_are_no_word_gaps(self):
        """The statement lays a row of space characters over each line, on a grid 1.2pt beside that of the letters and a
        baseline 0.6pt under theirs: one falls between every two letters."""
        from opendockit.extractors._pdf_chars import _fixed_pitch_text, _fixed_pitches

        chars = self._chars("Account No  Type of Account", laid_over=1.2)

        assert _fixed_pitches([chars]) == [(pytest.approx(5.42), 9.0)]
        assert _fixed_pitch_text(chars, [(5.42, 9.0)]) == "Account No Type of Account"

    def test_space_characters_on_the_baseline_of_the_letters_are_the_line_s_own(self):
        """The same blanks on the baseline of the letters, between them and off their grid, are characters of the line:
        with them it stands on no grid, and nothing is done."""
        from opendockit.extractors._pdf_chars import _fixed_pitch_text, _fixed_pitches

        chars = self._chars("Account No  Type of Account", laid_over=1.2, lower=0.0)
        near = self._chars("Account No  Type of Account", laid_over=1.2, lower=0.05)
        apart = self._chars("Account No  Type of Account", laid_over=1.2, lower=0.2)

        assert _fixed_pitches([chars]) == [] and _fixed_pitch_text(chars, [(5.42, 9.0)]) is None
        assert _fixed_pitches([near]) == [] and _fixed_pitch_text(near, [(5.42, 9.0)]) is None
        assert _fixed_pitch_text(apart, [(5.42, 9.0)]) == "Account No Type of Account"  # a fifth of a point under: another row

    def test_digits_written_with_a_space_between_each_two_keep_their_spaces(self):
        """Digits of one width with a space character between stand on a grid too, of half their distance, a space in
        every other cell: no two digits are neighbours, so the line names no grid, and on a page that has one it reads
        as it is written."""
        from opendockit.extractors._pdf_chars import _fixed_pitch_text, _fixed_pitches

        chars = self._typed("1 2 3 4 5 6 7 8 9")

        assert _fixed_pitches([chars]) == []
        assert _fixed_pitch_text(chars, [(3.75, 9.0)]) == "1 2 3 4 5 6 7 8 9"
        assert _fixed_pitch_text(chars, [(7.5, 9.0)]) is None  # the grid of the digits alone is none: the spaces are the line's

    def test_empty_cell_without_a_space_character_is_a_word_gap(self):
        from opendockit.extractors._pdf_chars import _fixed_pitch_text, _fixed_pitches

        chars = self._chars("Account No  Type of Account", own_spaces=False)

        assert _fixed_pitches([chars]) == [(pytest.approx(5.42), 9.0)]
        assert _fixed_pitch_text(chars, [(5.42, 9.0)]) == "Account No Type of Account"

    def test_text_of_a_line_on_the_grid_and_of_one_that_is_not(self):
        from opendockit.extractors._pdf_chars import _fixed_pitch_text

        grid = [(5.42, 9.0)]
        assert _fixed_pitch_text(self._chars("8,923.07    20"), grid) == "8,923.07 20"
        assert _fixed_pitch_text(self._chars("20"), grid) == "20"
        assert _fixed_pitch_text(self._chars("2"), grid) is None
        assert _fixed_pitch_text(self._chars("20"), []) is None
        assert _fixed_pitch_text(self._chars("20", size=12.0), grid) is None  # another size is another text
        assert _fixed_pitch_text(self._chars("20", pitch=7.0), grid) is None
        assert _fixed_pitch_text(self._chars("20", pitch=5.42 * 2), grid) == "2 0"
