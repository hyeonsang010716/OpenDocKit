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
            chars += through("Elsewhere", angle, x=100.0 + step * 120, top=100.0)

        read = _pdf_artifacts._tilted_paragraphs(chars)

        assert "Firstroadofthemap" in read and "Otherroadcrossing" in read and read.count("Elsewhere") == 4

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
