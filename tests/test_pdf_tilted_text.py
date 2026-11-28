"""Text set at an angle within a page — neither along the page nor turned a quarter — is read along its own direction."""

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_artifacts
from tests.test_pdf import _FONT, _korean_pdf


def _body(pdf: object, lines: int = 5) -> None:
    pdf.setFont("Helvetica", 11)
    for line in range(lines):
        pdf.drawString(72, 760 - line * 15, f"Body line {line + 1} of the page, set along it as usual.")


def _aslant(pdf: object, x: float, y: float, angle: float, text: str, size: float = 9, font: str = "Helvetica", grey: float = 0.0) -> None:
    pdf.saveState()
    pdf.translate(x, y)
    pdf.rotate(angle)
    pdf.setFillGray(grey)
    pdf.setFont(font, size)
    pdf.drawString(0, 0, text)
    pdf.restoreState()


def _text(draw: object, pages: int = 1, **options: object) -> str:
    return convert(_korean_pdf(draw, pages=pages), "a.pdf", ConvertOptions(page_markers=False, **options)).text


class TestTextSetAtAnAngle:
    """Such text was dropped from the page with no warning: the heads of the columns of a table set aslant (27 words of
    a real table, one of them in the output), the names of the streets of a map, a slogan across a corner."""

    def test_heads_of_columns_set_aslant_are_read_from_left_to_right(self):
        heads = ["Stockholm culture", "Stockholm teaching", "Country culture", "Abroad, all of them"]

        def draw(pdf, index):
            _body(pdf)
            for column, head in enumerate(heads):
                _aslant(pdf, 150 + column * 60, 500, 45, head)

        lines = _text(draw).split("\n")

        assert [line for line in lines if line in heads] == heads

    def test_the_body_is_as_it_was(self):
        def draw_body(pdf, index):
            _body(pdf)

        def draw_both(pdf, index):
            _body(pdf)
            _aslant(pdf, 150, 400, 30, "A slogan across the corner")

        assert _text(draw_both) == _text(draw_body) + "\n\nA slogan across the corner"

    @pytest.mark.parametrize("angle", [8, 30, 45, 60, 80, 100, 135, 200, -20, -45, -70, -120])
    def test_any_direction_is_read_along_itself(self, angle):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 300, 400, angle, "Words read in their own order")

        assert "Words read in their own order" in _text(draw).split("\n")

    def test_lines_of_one_direction_that_follow_each_other_are_one_paragraph(self):
        def draw(pdf, index):
            _body(pdf)
            pdf.saveState()
            pdf.translate(150, 300)
            pdf.rotate(20)
            pdf.setFont("Helvetica", 10)
            for line, text in enumerate(("First line of a note", "its second line", "and its third")):
                pdf.drawString(0, -line * 12, text)
            pdf.restoreState()
            _aslant(pdf, 350, 500, -30, "Another note elsewhere")

        text = _text(draw)

        assert "First line of a note\nits second line\nand its third" in text
        assert "\n\nAnother note elsewhere" in text

    def test_text_in_korean_is_read(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 200, 400, 45, "서울 문화 예술", font=_FONT)

        assert "서울 문화 예술" in _text(draw)

    def test_two_runs_on_one_line_far_apart_are_two(self):
        def draw(pdf, index):
            _body(pdf)
            pdf.saveState()
            pdf.translate(100, 300)
            pdf.rotate(15)
            pdf.setFont("Helvetica", 10)
            pdf.drawString(0, 0, "Left label")
            pdf.drawString(250, 0, "Right label")
            pdf.restoreState()

        lines = _text(draw).split("\n")

        assert "Left label" in lines and "Right label" in lines


class TestReadAsItIsWritten:
    """What an independent review found the first form writing wrong."""

    def test_text_of_a_negative_size_is_read_the_way_its_glyphs_advance(self):
        """With a negative size or scaling the glyphs advance against the text matrix: it read `redro ni daer sdroW`."""
        from tests.test_pdf_clipped_text import _page

        def tilted(before: bytes) -> str:
            content = (b"BT /F1 11 Tf 1 0 0 1 72 760 Tm (Body line of the page, set along it as usual.) Tj ET "
                       b"BT " + before + b" 0.866 0.5 -0.5 0.866 300 400 Tm (Words read in order) Tj ET")
            return convert(_page(content), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "Words read in order" in tilted(b"/F1 9 Tf")
        assert "Words read in order" in tilted(b"/F1 -9 Tf")
        assert "Words read in order" in tilted(b"/F1 9 Tf -100 Tz")

    def test_two_blocks_side_by_side_are_two_paragraphs(self):
        """A line joined only the line read just before it, which was the other block's: every line was a paragraph
        and the two blocks came out line about."""
        def draw(pdf, index):
            _body(pdf)
            pdf.saveState()
            pdf.translate(120, 250)
            pdf.rotate(8)
            pdf.setFont("Helvetica", 10)
            for line in range(6):
                pdf.drawString(0, 200 - line * 13, f"Left line {line + 1} of this block.")
                pdf.drawString(220, 200 - line * 13, f"Right line {line + 1} of this block.")
            pdf.restoreState()

        text = _text(draw)
        left = "\n".join(f"Left line {line + 1} of this block." for line in range(6))
        right = "\n".join(f"Right line {line + 1} of this block." for line in range(6))

        assert f"{left}\n\n{right}" in text

    @pytest.mark.parametrize("spacing", [0.5, 1.5, 6])
    def test_a_label_set_with_wide_character_spacing_keeps_its_words(self, spacing):
        """Every gap of such a label is wide: it read `Wo rds read in o rde r`, and set wider still was not written."""
        def draw(pdf, index):
            _body(pdf)
            pdf.saveState()
            pdf.translate(150, 300)
            pdf.rotate(30)
            wide = pdf.beginText(0, 0)
            wide.setFont("Helvetica", 9)
            wide.setCharSpace(spacing)
            wide.textOut("Words read in order")
            pdf.drawText(wide)
            pdf.restoreState()

        assert "Words read in order" in _text(draw).split("\n")

    @pytest.mark.parametrize("angle, apart", [(45, 60), (45, 14), (-45, 60), (-45, 14), (60, 14), (-60, 20)])
    def test_heads_rising_or_falling_close_or_far_are_read_from_left_to_right(self, angle, apart):
        heads = ["Stockholm culture", "Stockholm teaching", "Country culture", "Abroad, all of them"]

        def draw(pdf, index):
            _body(pdf)
            for column, head in enumerate(heads):
                _aslant(pdf, 150 + column * apart, 500, angle, head)

        assert [line for line in _text(draw).split("\n") if line in heads] == heads

    def test_a_stamp_set_all_over_one_page_is_written_once(self):
        def draw(pdf, index):
            _body(pdf)
            for row in range(4):
                for column in range(3):
                    _aslant(pdf, 80 + column * 170, 150 + row * 120, 30, "Do not copy", size=10)

        assert _text(draw).count("Do not copy") == 1

    def test_a_string_of_a_font_without_widths_is_not_read(self):
        """pdfminer draws the whole string on one spot: a line of eighty letters came out as `word an`."""
        from tests.test_pdf_clipped_text import _page

        content = (b"BT /F1 11 Tf 1 0 0 1 72 760 Tm (Body line of the page, set along it as usual.) Tj ET "
                   b"BT /F1 9 Tf 0.866 0.5 -0.5 0.866 200 300 Tm (Tilted words of a font without widths) Tj ET")
        unmeasured = b"<< /Type /Font /Subtype /TrueType /BaseFont /Foo >>"
        measured = convert(_page(b"BT /F1 9 Tf 0.866 0.5 -0.5 0.866 200 300 Tm (Tilted words of a font with widths) Tj ET"), "a.pdf")

        assert "Tilted words of a font with widths" in measured.text
        assert _pdf_artifacts._tilted_paragraphs([{"text": letter, "x0": 100.0, "x1": 100.0, "top": 500.0, "bottom": 510.0, "size": 10.0,
                                                   "adv": 0.0, "matrix": (0.866, 0.5, -0.5, 0.866, 100.0, 500.0)} for letter in "Label"]) == []
        assert convert(_page(content, font=unmeasured), "a.pdf") is not None   # and nothing raises

    def test_a_page_of_tilted_text_alone_is_left_to_the_other_reader(self):
        """A page laid askew, a scan with its text layer: pdfium reads it whole and in its order, as before. Read along
        its direction it lost the lines that stand twice on purpose and broke its paragraphs."""
        def draw(pdf, index):
            pdf.saveState()
            pdf.translate(120, 100)
            pdf.rotate(8)
            pdf.setFont("Helvetica", 11)
            for line, text in enumerate(("Form of the office", "Name of the applicant", "Signature", "Name of the witness", "Signature")):
                pdf.drawString(0, 600 - line * 20, text)
            pdf.restoreState()

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("Signature") == 2 and "Name of the witness" in result.text
        assert any("simple extraction" in warning for warning in result.warnings)

    def test_the_gate_is_other_text_of_any_kind(self, monkeypatch):
        """Lines, a table or text turned a quarter: beside any of them slanted text is read; alone it is not."""
        from opendockit.extractors import pdf as pdf_module

        def tilted_of(draw: object) -> list[str]:
            read = []
            read_page = pdf_module._read_page

            def noting(page: object, *args: object) -> object:
                found = read_page(page, *args)
                read.append(found.tilted)
                return found

            monkeypatch.setattr(pdf_module, "_read_page", noting)
            convert(_korean_pdf(draw), "a.pdf")
            monkeypatch.setattr(pdf_module, "_read_page", read_page)
            return read[0]

        def label(pdf: object) -> None:
            _aslant(pdf, 300, 300, 30, "Set at an angle")

        def with_table(pdf, index):
            for row in range(3):
                for column in range(3):
                    pdf.rect(100 + column * 100, 600 - row * 30, 100, 30)
                    pdf.drawString(105 + column * 100, 610 - row * 30, f"cell {row}{column}")
            label(pdf)

        assert tilted_of(lambda pdf, index: label(pdf)) == []
        assert tilted_of(lambda pdf, index: (_body(pdf, 1), label(pdf))) == ["Set at an angle"]
        assert tilted_of(lambda pdf, index: (_aslant(pdf, 40, 300, 90, "Turned a quarter in the margin", size=12), label(pdf))) == ["Set at an angle"]
        assert tilted_of(with_table) == ["Set at an angle"]

    @pytest.mark.parametrize("size", [11, 14, 17, 18, 19, 20, 24])
    def test_a_label_larger_than_the_text_is_written_whole_or_not_at_all(self, size):
        """Large slanted characters are a watermark's and are removed one by one, by the height of their box, and the
        box of a turned glyph is as high as the glyph is wide: the wide letters of a slogan went and the rest was
        written, `elco e to the Su er arket`."""
        def draw(pdf, index):
            _body(pdf, 8)
            _aslant(pdf, 120, 300, 45, "Welcome to the Summer Market", size=size)

        written = [line for line in _text(draw).split("\n") if "Body line" not in line and line]

        assert written in (["Welcome to the Summer Market"], [])
        assert written or size > 16

    def test_a_label_that_stands_a_few_times_on_a_page_is_written_each_time(self):
        def draw(pdf, index):
            _body(pdf)
            for copy in range(4):
                _aslant(pdf, 100 + copy * 110, 300, 30, "Twenty at least")

        assert _text(draw).count("Twenty at least") == 4

    def test_a_mark_without_an_advance_is_read_with_its_letter(self):
        """A vowel or tone mark, a combining accent: left out as a glyph of a font without widths, `taxi` read `tai`."""
        run = TestTiltedParagraphs()._run("taxi exit")
        for char in run:
            if char["text"] == "x":
                char["adv"] = 0.0

        assert _pdf_artifacts._tilted_paragraphs(run) == ["taxi exit"]

    def test_letters_set_apart_one_by_one_are_not_one_word(self):
        """Wide gaps are character spacing when they are small against the letters or the piece shows its words besides."""
        unit = TestTiltedParagraphs()

        def spaced(text: str, gap: float) -> list[str]:
            import math
            chars, x, top = [], 100.0, 500.0
            for letter in text:
                if letter != " ":
                    chars.append(unit._char(letter, x, top))
                step = 5 + gap if letter != " " else 8
                x, top = x + step * math.cos(math.radians(30)), top - step * math.sin(math.radians(30))
            return _pdf_artifacts._tilted_paragraphs(chars)

        assert spaced("Wordsreadinorder", 3.0) == ["Wordsreadinorder"]          # small against the size of 10
        assert spaced("Words read in order", 7.0) == ["Words read in order"]    # wide, and the words show
        assert spaced("ABCDEFG", 7.0) == []                                      # letters apart: no two together

    def test_an_indented_first_line_belongs_to_its_paragraph(self):
        def draw(pdf, index):
            _body(pdf)
            pdf.saveState()
            pdf.translate(150, 300)
            pdf.rotate(12)
            pdf.setFont("Helvetica", 10)
            pdf.drawString(18, 0, "A note whose first line is set in,")
            pdf.drawString(0, -12, "its second line and its third are not,")
            pdf.drawString(0, -24, "and all three are one paragraph.")
            pdf.restoreState()

        assert "A note whose first line is set in,\nits second line and its third are not,\nand all three are one paragraph." in _text(draw)

    def test_tilted_text_is_written_after_text_turned_a_quarter(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 300, 500, 30, "Set at an angle")
            _aslant(pdf, 40, 300, 90, "Turned a quarter in the margin", size=12)

        text = _text(draw)

        assert text.index("Turned a quarter in the margin") < text.index("Set at an angle")

    def test_a_label_part_of_which_a_ruled_cell_holds_is_left_as_it_was(self):
        def draw(table: bool) -> object:
            def drawing(pdf, index):
                _body(pdf)
                if table:
                    for row in range(4):
                        for column in range(3):
                            pdf.rect(100 + column * 100, 400 - row * 30, 100, 30)
                            pdf.drawString(105 + column * 100, 410 - row * 30, f"cell {row}{column}")
                _aslant(pdf, 380, 415, 30, "Label running out of the table")
            return drawing

        assert "Label running out of the table" in _text(draw(False)).split("\n")
        assert "Label running out of the table" not in _text(draw(True)).split("\n")


class TestWhatIsNotRead:
    def test_a_large_watermark_is_not_read(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 150, 300, 45, "CONFIDENTIAL", size=60)

        assert "CONFIDENTIAL" not in _text(draw)

    def test_a_pale_watermark_of_the_size_of_the_text_is_not_read(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 150, 300, 45, "Sample copy", size=12, grey=0.85)
            _aslant(pdf, 150, 200, 45, "Dark label", size=12, grey=0.3)

        text = _text(draw)

        assert "Sample copy" not in text and "Dark label" in text

    def test_letters_along_a_curve_are_not_read_in_pieces(self):
        """Each letter of such a text has a direction of its own; by twos and threes they shared one, and came out as
        `ndell` and `cent Dr`."""
        def draw(pdf, index):
            _body(pdf)
            for step, letter in enumerate("Crescent Drive"):
                _aslant(pdf, 150 + step * 7, 400 + step * (step * 0.2), 10 + (step // 3) * 4, letter, size=10)

        text = _text(draw)

        assert not any(piece in text for piece in ("Cre", "sce", "nt ", "Dri", "ive"))

    def test_numbers_alone_are_not_read(self):
        """The heights of the contours of a map, the measures of a drawing."""
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 150, 400, 30, "250")
            _aslant(pdf, 250, 400, 30, "12.5 - 300")
            _aslant(pdf, 350, 400, 30, "Hill 250")

        lines = _text(draw).split("\n")

        assert "Hill 250" in lines and "250" not in lines and "12.5 - 300" not in lines

    def test_a_piece_of_two_characters_is_not_read(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 150, 400, 30, "No")
            _aslant(pdf, 350, 400, 30, "Yes")

        lines = _text(draw).split("\n")

        assert "Yes" in lines and "No" not in lines

    def test_text_turned_a_quarter_is_read_as_before(self):
        def draw(pdf, index):
            _body(pdf)
            _aslant(pdf, 40, 300, 90, "arXiv:2609.12345v1 [cs.LG] 30 Sep 2026", size=14)

        assert "arXiv:2609.12345v1 [cs.LG] 30 Sep 2026" in _text(draw)


class TestStampOnEveryPage:
    def _stamped(self, pdf: object, index: int) -> None:
        _body(pdf)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 600, f"This is page {index + 1} of the report.")
        _aslant(pdf, 400, 300, 30, "Draft for review", size=11)
        if index == 1:
            _aslant(pdf, 200, 300, 30, "Only on this page", size=11)

    def test_a_stamp_on_every_page_is_written_once(self):
        text = _text(self._stamped, pages=4)

        assert text.count("Draft for review") == 1 and text.count("Only on this page") == 1
        assert text.index("Draft for review") < text.index("This is page 2 of the report.")

    def test_every_copy_stays_when_repeated_lines_are_kept(self):
        assert _text(self._stamped, pages=4, remove_repeated_page_lines=False).count("Draft for review") == 4

    def test_a_page_left_with_nothing_but_the_stamp_keeps_it(self):
        from opendockit.extractors import pdf as pdf_module
        from opendockit.extractors._pdf_model import _Line, _Page

        def page(number: int, lines: int) -> _Page:
            return _Page(number=number, width=595.0, height=842.0, tables=[], has_images=False, view=(0.0, 0.0, 595.0, 842.0),
                         lines=[_Line(top=100.0, bottom=110.0, x0=72.0, x1=300.0, text="Body", size=10.0)] * lines,
                         tilted=["Draft for review"])

        pages = [page(1, 1), page(2, 0), page(3, 1), page(4, 1)]
        pdf_module._tilted_once(pages)

        assert [found.tilted for found in pages] == [["Draft for review"], ["Draft for review"], [], []]

    def test_a_stamp_on_fewer_than_half_of_the_pages_stays_on_each(self):
        def draw(pdf, index):
            _body(pdf)
            if index in (0, 3, 6):
                _aslant(pdf, 400, 300, 30, "Draft for review", size=11)

        assert _text(draw, pages=8).count("Draft for review") == 3

    def test_on_two_pages_both_copies_stay(self):
        assert _text(self._stamped, pages=2).count("Draft for review") == 2


class TestTiltedParagraphs:
    @staticmethod
    def _char(text: str, x: float, top: float, angle: float = 30.0, size: float = 10.0, **more: object) -> dict:
        import math
        turn = math.radians(angle)
        return {"text": text, "x0": x, "x1": x + 5, "top": top, "bottom": top + size, "size": size, "adv": 5.0,
                "matrix": (math.cos(turn), math.sin(turn), -math.sin(turn), math.cos(turn), x, top), **more}

    def _run(self, text: str, x: float = 100.0, top: float = 500.0, angle: float = 30.0, **more: object) -> list[dict]:
        import math
        turn = math.radians(angle)
        return [self._char(letter, x + step * 5 * math.cos(turn), top - step * 5 * math.sin(turn), angle, **more)
                for step, letter in enumerate(text)]

    def test_a_run_is_read(self):
        assert _pdf_artifacts._tilted_paragraphs(self._run("Label")) == ["Label"]

    def test_characters_at_no_place_are_left_out_and_raise_nothing(self):
        chars = self._run("Label")
        chars.append(self._char("x", float("nan"), 500.0))
        chars.append(self._char("y", 100.0, float("inf")))
        chars.append({**self._char("z", 100.0, 500.0), "matrix": (float("nan"), 1.0, 0.0, 0.0, 0.0, 0.0)})
        chars.append({**self._char("w", 100.0, 500.0), "size": 0.0})
        chars.append({**self._char("v", 100.0, 500.0), "matrix": None, "size": float("inf")})

        assert _pdf_artifacts._tilted_paragraphs(chars) == ["Label"]

    def test_letters_written_from_right_to_left_are_put_into_the_order_of_reading(self):
        """A writer sets Hebrew glyph by glyph from the left: along the page it was read `\u05e9\u05dc\u05d5\u05dd`, aslant backwards."""
        drawn = "\u05dd\u05dc\u05d5\u05e2 \u05dd\u05d5\u05dc\u05e9"   # the glyphs of `shalom olam` as they stand from the left

        assert _pdf_artifacts._tilted_paragraphs(self._run(drawn)) == ["\u05e9\u05dc\u05d5\u05dd \u05e2\u05d5\u05dc\u05dd"]

    def test_a_character_at_no_place_does_not_spoil_the_line_it_would_stand_in(self):
        chars = self._run("Label")
        chars.insert(2, {**self._char("x", 105.0, 495.0), "x0": float("nan"), "x1": float("nan")})
        chars.insert(3, {**self._char("y", 105.0, 495.0), "adv": float("nan")})

        assert _pdf_artifacts._tilted_paragraphs(chars) == ["Label"]

    def test_paragraphs_are_written_from_the_top_down_and_rows_from_left_to_right(self):
        steep = [*self._run("Lower", x=100.0, top=600.0, angle=45.0), *self._run("Upperright", x=300.0, top=200.0, angle=45.0),
                 *self._run("Upperleft", x=100.0, top=200.0, angle=45.0), *self._run("Middle", x=200.0, top=400.0, angle=45.0)]

        assert _pdf_artifacts._tilted_paragraphs(steep) == ["Upperleft", "Upperright", "Middle", "Lower"]

    def test_the_direction_of_a_group_is_that_of_its_middle(self):
        """Characters within the span of one direction are turned by the direction of the middle one."""
        chars = [*self._run("Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", angle=30.0),
                 *self._run("Bbb", x=100.0, top=300.0, angle=31.4)]

        assert sorted(_pdf_artifacts._tilted_paragraphs(chars)) == ["Aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "Bbb"]

    def test_a_piece_a_table_cell_holds_part_of_is_left_to_the_cell(self):
        run = self._run("Crescent")

        assert _pdf_artifacts._tilted_paragraphs(run[4:], run[:4]) == []
        assert _pdf_artifacts._tilted_paragraphs(self._run("Other", top=300.0), run) == ["Other"]
        assert _pdf_artifacts._tilted_paragraphs([], run) == []

    def test_a_page_of_more_tilted_characters_than_the_bound_is_left_as_it_was(self, monkeypatch):
        monkeypatch.setattr(_pdf_artifacts, "_TILTED_MAX_CHARS", 4)

        assert _pdf_artifacts._tilted_paragraphs(self._run("Label")) == []

    def test_directions_are_told_apart_from_the_first_of_each(self):
        """The labels of a map stand at every angle: taken neighbour by neighbour they were all one direction, and two
        roads that cross were read as one line of both."""
        import math

        def through(text: str, angle: float, x: float = 300.0, top: float = 400.0) -> list[dict]:
            back = 5 * (len(text) // 2)   # the middle of the label lies on (x, top)
            return self._run(text, x=x - back * math.cos(math.radians(angle)), top=top + back * math.sin(math.radians(angle)), angle=angle)

        chars = through("Firstroadofthemap", 10.0) + through("Otherroadcrossing", 16.0)
        for step, angle in enumerate((11.2, 12.4, 13.6, 14.8)):   # roads elsewhere, at the angles between
            chars += through(f"Elsewhere{'abcd'[step]}", angle, x=100.0 + step * 120, top=100.0)

        read = _pdf_artifacts._tilted_paragraphs(chars)

        assert sorted(read) == ["Elsewherea", "Elsewhereb", "Elsewherec", "Elsewhered", "Firstroadofthemap", "Otherroadcrossing"]

    def test_pieces_of_a_text_set_along_a_curve_are_not_read(self):
        """Each letter drawn after the last, beside it and turned a little against it."""
        import math

        def along(text: str, turn: float) -> list[dict]:
            chars, x, top, angle = [], 100.0, 500.0, 10.0
            for number, letter in enumerate(text):
                if number and number % 4 == 0:
                    angle += turn
                chars.append(self._char(letter, x, top, angle))
                x, top = x + 5 * math.cos(math.radians(angle)), top - 5 * math.sin(math.radians(angle))
            return chars

        assert _pdf_artifacts._tilted_paragraphs(along("CrescentDrive", 0.0)) == ["CrescentDrive"]
        assert _pdf_artifacts._tilted_paragraphs(along("CrescentDrive", 6.0)) == []
        # a turn as wide as that is another text, not the next letter
        assert sorted(_pdf_artifacts._tilted_paragraphs(along("CrescentDriv", 40.0))) == ["Cres", "Driv", "cent"]
