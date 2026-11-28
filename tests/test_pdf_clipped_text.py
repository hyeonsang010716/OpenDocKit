"""Text drawn outside the clipping path in force is not on the page, and is not read."""

from types import SimpleNamespace

import pytest

from opendockit import convert
from opendockit.extractors import _pdf_pdfminer
from opendockit.extractors import pdf as pdf_module
from tests.test_pdf import _raw_pdf


_FONT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n" % (len(body), extra) + body + b"\nendstream"


def _form(body: bytes, box: bytes = b"[0 0 100 30]", extra: bytes = b"") -> bytes:
    return _stream(body, b"/Type /XObject /Subtype /Form /BBox %s /Resources << /Font << /F1 5 0 R >> >> %s" % (box, extra))


def _page(content: bytes, *, page: bytes = b"/MediaBox [0 0 595 842]", forms: dict[bytes, bytes] | None = None,
          font: bytes = _FONT) -> bytes:
    """A one-page PDF drawing `content` with `font` (Helvetica) as /F1 and each of `forms` (name: object) as a form XObject."""
    forms = forms or {}
    named = b" ".join(b"/%s %d 0 R" % (name, 6 + index) for index, name in enumerate(forms))
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: b"<< /Type /Page /Parent 2 0 R %s /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> /XObject << %s >> >> >>"
                        % (page, named),
                     4: _stream(content), 5: font, **{6 + index: body for index, body in enumerate(forms.values())}})


def _at(x: float, y: float, text: bytes, before: bytes = b"") -> bytes:
    return b"BT /F1 12 Tf %s 1 0 0 1 %d %d Tm (%s) Tj ET\n" % (before, x, y, text)


def _lines(content: bytes, **page: object) -> list[str]:
    text = convert(_page(content, **page), "a.pdf").text
    return [line for line in text.splitlines() if line and not line.startswith("<!--")]


def _words(content: bytes, **page: object) -> list[str]:
    return " ".join(_lines(content, **page)).split()


class TestTextOutsideItsClip:
    """A spreadsheet draws the whole of a long cell and clips it to the cell; a layout program leaves the text of a
    frame that was cropped away in the stream. pdfminer reads `W` and does nothing, so the hidden end of one cell lay
    among the letters of the next, and text nobody sees was written as the words of the document."""

    def test_the_end_of_a_line_that_runs_past_its_clip_is_left_out(self):
        content = b"q 72 690 100 30 re W n " + _at(72, 700, b"Shown part and the hidden end of it runs on") + b"Q"

        assert _lines(content) == ["Shown part and the"]

    def test_a_word_drawn_outside_the_clip_is_left_out_and_one_inside_is_kept(self):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]

    def test_the_clip_ends_where_the_graphics_state_is_restored(self):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"Shown") + b"Q " + _at(300, 700, b"After")

        assert _words(content) == ["Shown", "After"]

    def test_a_clip_set_without_a_saved_state_holds_to_the_end_of_the_page(self):
        content = b"72 690 100 30 re W n " + _at(80, 700, b"Shown") + _at(80, 600, b"Hidden")

        assert _words(content) == ["Shown"]

    def test_a_clip_inside_a_clip_is_cut_to_both(self):
        content = (b"q 72 690 300 30 re W n q 200 600 300 200 re W n "
                   + _at(80, 700, b"First") + _at(220, 700, b"Both") + _at(420, 700, b"Second") + b"Q "
                   + _at(80, 700, b"Outer") + b"Q")

        assert _words(content) == ["Both", "Outer"] or _lines(content) == ["Outer", "Both"]

    def test_the_hidden_end_of_one_cell_no_longer_lies_among_the_letters_of_the_next(self):
        cells = (b"q 72 690 60 20 re W n " + _at(74, 695, b"Name of the holder") + b"Q "
                 b"q 132 690 100 20 re W n " + _at(134, 695, b"ADAUTO") + b"Q")

        (line,) = _lines(cells)

        assert line.split()[-1] == "ADAUTO" and "holder" not in line

    def test_a_line_drawn_once_for_each_band_of_the_page_is_read_once(self):
        """A printer driver draws the page in bands: the whole line each time, clipped to one band."""
        line = _at(72, 700, b"One line of the page drawn in two bands")
        content = b"q 0 0 150 842 re W n " + line + b"Q q 150 0 445 842 re W n " + line + b"Q"

        assert _lines(content) == ["One line of the page drawn in two bands"]

    def test_a_clip_painted_as_it_is_set_clips_too(self):
        content = b"q 1 g 72 690 100 30 re W f 0 g " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]

    def test_the_even_odd_clip_is_followed_too(self):
        content = b"q 72 690 100 30 re W* n " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]

    def test_a_clip_is_measured_where_the_matrix_puts_it(self):
        content = b"q 2 0 0 2 0 0 cm 36 345 50 15 re W n 1 0 0 1 0 0 cm " + _at(40, 350, b"Shown") + _at(150, 350, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]

    def test_text_in_the_box_of_a_clip_that_is_no_rectangle_is_kept(self):
        """The box of the path is never smaller than the clip: text beside a triangle, inside its box, stays."""
        content = b"q 72 690 m 272 690 l 72 720 l h W n " + _at(200, 705, b"Beside") + _at(400, 705, b"Hidden") + b"Q"

        assert _words(content) == ["Beside"]

    def test_the_control_points_of_a_curve_are_part_of_the_box(self):
        content = b"q 72 690 m 72 760 300 760 300 690 c h W n " + _at(80, 745, b"Above the curve") + _at(400, 700, b"Hidden") + b"Q"

        assert _words(content) == ["Above", "the", "curve"]

    def test_a_letter_is_kept_while_any_part_of_its_box_is_inside(self):
        """The ink of a glyph may be a small part of its box (a hyphen clipped to its stroke): no share is asked for."""
        assert _words(b"q 72 690 1 30 re W n " + _at(72, 700, b"W") + b"Q") == ["W"]
        assert _words(b"q 174 703 2 1 re W n " + _at(172, 700, b"-") + b"Q") == ["-"]
        assert _words(b"q 60 690 10 30 re W n " + _at(72, 700, b"W") + b"Q") == []

    def test_a_line_cut_by_the_edge_of_its_frame_is_kept(self):
        """The last line of a frame, cut by its lower edge: the tops of its letters show and it is read."""
        shown = b"q 72 696 400 100 re W n " + _at(72, 700, b"Body of the frame") + _at(72, 690, b"and its last line") + b"Q"
        below = b"q 72 699 400 100 re W n " + _at(72, 700, b"Body of the frame") + _at(72, 680, b"and its last line") + b"Q"

        assert _words(shown) == "Body of the frame and its last line".split()
        assert _words(below) == "Body of the frame".split()

    def test_a_glyph_drawn_far_from_its_box_is_kept_by_the_reach_of_its_font(self):
        """A font of large delimiters has a descent of three times its size: pdfminer sets the box of each character
        that far under the baseline, and the glyph, drawn at the baseline, shows through a clip its box is far from."""
        deep = (b"<< /Type /Font /Subtype /Type1 /BaseFont /Delimiters /FirstChar 65 /LastChar 122 /Widths [%s] "
                b"/FontDescriptor << /Type /FontDescriptor /FontName /Delimiters /Flags 4 /FontBBox [0 -3009 1032 959] "
                b"/Ascent 959 /Descent -3009 /CapHeight 700 /ItalicAngle 0 /StemV 80 >> >>") % b" ".join([b"600"] * 58)
        at_the_baseline = b"q 70 695 40 14 re W n " + _at(72, 700, b"Sum") + b"Q " + _at(72, 600, b"Body")
        far_above = b"q 70 760 40 14 re W n " + _at(72, 700, b"Sum") + b"Q " + _at(72, 600, b"Body")

        assert _words(at_the_baseline, font=deep) == ["Sum", "Body"]
        assert _words(far_above, font=deep) == ["Body"]
        assert _words(b"q 70 720 40 14 re W n " + _at(72, 700, b"Sum") + b"Q " + _at(72, 600, b"Body")) == ["Body"]

    def test_a_character_is_judged_by_its_box_and_the_reach_of_its_font(self):
        state = SimpleNamespace(**{_pdf_pdfminer._CLIP: (72.0, 690.0, 172.0, 720.0)})
        plain = SimpleNamespace(get_descent=lambda: -0.2, bbox=(0, -200, 1000, 900), vscale=0.001)
        deep = SimpleNamespace(get_descent=lambda: -3.0, bbox=(0, -3000, 1000, 950), vscale=0.001)
        boxless = SimpleNamespace(get_descent=lambda: -0.2, bbox=None, vscale=0.001)

        def outside(font: object, x: float, y: float, width: float = 6.0, size: float = 10.0) -> bool:
            low = y + font.get_descent() * size
            char = SimpleNamespace(graphicstate=state, x0=x, x1=x + width, y0=low, y1=low + size, adv=width, matrix=(1, 0, 0, 1, x, y))
            return _pdf_pdfminer._outside_its_clip(char, object(), font, size, 0.0)

        assert not outside(plain, 80.0, 700.0) and outside(plain, 300.0, 700.0) and outside(plain, 80.0, 600.0)
        # a font without widths: the glyph is drawn somewhere within a size of where the character stands
        assert not outside(plain, 80.0, 700.0, width=0.0) and outside(plain, 300.0, 700.0, width=0.0)
        assert not outside(plain, 65.0, 700.0, width=0.0) and not outside(plain, 178.0, 700.0, width=0.0)
        assert outside(plain, 60.0, 700.0, width=0.0) and outside(plain, 184.0, 700.0, width=0.0)
        assert not outside(plain, 171.0, 700.0) and outside(plain, 173.0, 700.0)                         # across: the box alone
        assert outside(plain, 80.0, 725.0) and not outside(deep, 80.0, 725.0)       # the box is under the clip, the glyph is not
        assert outside(deep, 80.0, 760.0) and outside(boxless, 80.0, 725.0)
        assert not outside(plain, float("nan"), 700.0) and not outside(plain, 80.0, float("nan"))

    def test_invisible_text_is_kept(self):
        """The text layer of a scan is drawn in the invisible mode: it is never shown, and a clip says nothing of it."""
        content = b"q 72 690 100 30 re W n " + _at(300, 700, b"Recognised", b"3 Tr") + b"Q"

        assert _words(content) == ["Recognised"]

    def test_text_after_invisible_text_is_judged_by_its_own_mode(self):
        content = b"q 72 690 100 30 re W n " + _at(300, 700, b"Recognised", b"3 Tr") + _at(300, 650, b"Hidden", b"0 Tr") + b"Q"

        assert _words(content) == ["Recognised"]

    def test_a_path_that_is_not_there_clips_nothing(self):
        content = b"q W n " + _at(80, 700, b"Shown") + b"Q"

        assert _words(content) == ["Shown"]

    def test_a_path_that_cannot_be_placed_clips_nothing(self):
        """Numbers past what a float holds give a box that is no box: the clip is left as it was."""
        content = b"q 1e39 0 0 1e39 0 0 cm 0 0 10 10 re W n Q q 1e39 0 0 1 0 0 cm 0 0 10 10 re W n 1e-39 0 0 1 0 0 cm Q " + _at(80, 700, b"Shown")

        assert _words(content) == ["Shown"]

    def test_a_clip_far_from_the_page_hides_the_text(self):
        content = b"q 1e30 1e30 10 10 re W n " + _at(80, 700, b"Hidden") + b"Q " + _at(80, 600, b"Shown")

        assert _words(content) == ["Shown"]

    def test_clips_that_leave_nothing_between_them_hide_the_text(self):
        content = b"q 0 0 100 842 re W n 300 0 100 842 re W n " + _at(50, 700, b"Hidden") + _at(320, 700, b"Hidden") + b"Q " + _at(80, 600, b"Shown")

        assert _words(content) == ["Shown"]

    def test_a_page_of_hidden_text_alone_is_not_handed_to_the_other_reader(self):
        """A page pdfminer reads nothing on is read by pdfium when pdfium finds text: it would bring the hidden text back."""
        result = convert(_page(b"q 72 690 100 30 re W n " + _at(300, 700, b"Hidden words only on this page") + b"Q"), "a.pdf")

        assert "Hidden" not in result.text
        assert not any("simple extraction" in warning for warning in result.warnings)

    def test_hidden_text_is_not_what_an_undecoded_font_left_unread(self, monkeypatch):
        """In a document with a font pdfminer cannot decode, a page pdfium reads clearly more of is read by pdfium.
        The characters dropped for their clip are counted with those pdfminer read: they are not text it missed."""
        read_page_from = pdf_module._read_page_from

        def with_an_undecoded_font(*args: object) -> object:
            page = read_page_from(*args)
            _pdf_pdfminer._INFLATE_BUDGET.get().undecoded_fonts = 1
            return page

        monkeypatch.setattr(pdf_module, "_read_page_from", with_an_undecoded_font)
        hidden = b"".join(_at(300, 700 - 14 * row, b"Hidden words of a cropped frame") for row in range(8))
        content = b"q 72 100 100 700 re W n " + _at(80, 700, b"Shown") + hidden + b"Q"

        result = convert(_page(content), "a.pdf")

        assert "Shown" in result.text and "Hidden" not in result.text
        assert not any("simple extraction" in warning for warning in result.warnings)

    def test_every_page_starts_without_a_clip(self):
        data = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            4: _stream(b"72 690 100 30 re W n " + _at(80, 700, b"First page")), 5: _FONT,
            6: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 7 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            7: _stream(_at(300, 500, b"Second page"))})

        text = convert(data, "a.pdf").text

        assert "First page" in text and "Second page" in text


def _drawn(monkeypatch: pytest.MonkeyPatch, content: bytes) -> str:
    """The characters of the page as the reader is given them, spaces too, in the order they were drawn."""
    seen: list[str] = []
    read_page = pdf_module._read_page

    def noting(page: object, *args: object) -> object:
        seen.append("".join(char["text"] for char in page.chars))
        return read_page(page, *args)

    monkeypatch.setattr(pdf_module, "_read_page", noting)
    convert(_page(content), "a.pdf")
    return seen[0]


class TestSpacesOutsideTheClip:
    """A space puts no ink on the page and a clip says nothing of it: it goes only with the glyphs it stands among."""

    def test_the_space_that_ends_a_line_past_the_edge_of_its_cell_is_kept(self, monkeypatch):
        """Hangul draws the space that ends a line past the edge of the cell: it tells that the next line starts a word."""
        content = b"q 72 690 33 30 re W n " + _at(72, 700, b"Shown ") + b"Q"

        assert _drawn(monkeypatch, content) == "Shown "

    def test_the_spaces_of_a_hidden_end_go_with_it(self, monkeypatch):
        content = b"q 72 690 60 30 re W n " + _at(72, 700, b"Shown and a hidden end") + b"Q"

        assert _drawn(monkeypatch, content) == "Shown and "

    def test_the_spaces_before_a_hidden_word_go_with_it(self, monkeypatch):
        content = b"q 72 690 60 30 re W n " + _at(72, 700, b"Shown") + _at(300, 700, b"  hidden words ") + b"Q"

        assert _drawn(monkeypatch, content) == "Shown"

    def test_the_spaces_before_a_shown_word_stay(self, monkeypatch):
        content = b"q 72 690 60 30 re W n " + _at(60, 700, b"  Shown") + b"Q"

        assert _drawn(monkeypatch, content) == "  Shown"

    def test_a_string_of_spaces_alone_stays(self, monkeypatch):
        content = b"q 72 690 60 30 re W n " + _at(72, 700, b"Shown") + _at(300, 700, b"   ") + b"Q"

        assert _drawn(monkeypatch, content) == "Shown   "

    def test_each_string_is_judged_by_its_own_glyphs(self, monkeypatch):
        content = b"q 72 690 60 30 re W n " + _at(300, 700, b"hidden") + _at(72, 700, b"Shown ") + _at(300, 650, b" hidden") + b"Q"

        assert _drawn(monkeypatch, content) == "Shown "

    def test_a_dropped_space_is_not_counted_as_text_that_was_dropped(self, monkeypatch):
        counted = []
        read_page = pdf_module._read_page

        def noting(page: object, *args: object) -> object:
            read = read_page(page, *args)
            counted.append(read.clipped_chars)
            return read

        monkeypatch.setattr(pdf_module, "_read_page", noting)
        convert(_page(b"q 72 690 60 30 re W n " + _at(72, 700, b"Shown") + _at(300, 700, b" two words ") + b"Q"), "a.pdf")

        assert counted == [8]


class TestTheClipIsThePathAsItIsEnded:
    """`W` marks the path being built; the clip is that path as it stands when the operator ending it comes."""

    def test_what_is_added_to_the_path_after_the_operator_is_part_of_the_clip(self):
        content = b"q 72 690 100 30 re W 290 690 100 30 re n " + _at(80, 700, b"First") + _at(300, 700, b"Second") + _at(80, 600, b"Hidden") + b"Q"

        assert _words(content) == ["First", "Second"]

    def test_a_path_that_is_never_ended_clips_nothing(self):
        content = b"q 72 690 100 30 re W Q " + _at(300, 700, b"Shown")

        assert _words(content) == ["Shown"]

    def test_the_operator_twice_on_one_path_is_one_clip(self):
        content = b"q 72 690 100 30 re W W* W n " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]

    def test_each_operator_that_ends_a_path_sets_the_clip(self):
        for end in (b"n", b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
            content = b"q 1 g 1 G 72 690 100 30 re W " + end + b" 0 g " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

            assert _words(content) == ["Shown"], end

    def test_a_path_ended_without_the_operator_is_no_clip(self):
        content = b"q 72 690 100 30 re W n 290 500 10 10 re n " + _at(80, 700, b"Shown") + _at(292, 502, b"Hidden") + b"Q"

        assert _words(content) == ["Shown"]


class TestClipOnAPageThatIsTurnedOrMoved:
    def test_on_a_page_whose_box_does_not_start_at_zero(self):
        content = b"q 172 790 100 30 re W n " + _at(180, 800, b"Shown") + _at(400, 800, b"Hidden") + b"Q"

        assert _words(content, page=b"/MediaBox [100 100 695 942]") == ["Shown"]

    def test_on_a_page_turned_a_quarter(self):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q " + _at(80, 500, b"Body")

        text = " ".join(_lines(content, page=b"/MediaBox [0 0 595 842] /Rotate 90"))

        assert "Shown" in text and "Body" in text and "Hidden" not in text

    def test_on_a_page_turned_upside_down(self):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"Shown") + _at(300, 700, b"Hidden") + b"Q"

        text = " ".join(_lines(content, page=b"/MediaBox [0 0 595 842] /Rotate 180"))

        assert "Shown" in text and "Hidden" not in text


class TestClipAndFormXObjects:
    def test_a_form_is_clipped_to_its_box(self):
        form = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 300 0 Td (Hidden) Tj ET")

        assert _words(b"q 1 0 0 1 72 690 cm /X Do Q", forms={b"X": form}) == ["Inside"]

    def test_the_box_of_a_form_is_measured_through_its_matrix(self):
        form = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 120 0 Td (Further) Tj ET", extra=b"/Matrix [1 0 0 1 72 690]")
        wide = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 120 0 Td (Further) Tj ET", box=b"[0 0 300 30]", extra=b"/Matrix [1 0 0 1 72 690]")

        assert _words(b"/X Do", forms={b"X": form}) == ["Inside"]
        assert _words(b"/X Do", forms={b"X": wide}) == ["Inside", "Further"]

    def test_the_clip_of_the_page_holds_inside_a_form(self):
        form = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 150 0 Td (Hidden) Tj ET", box=b"[0 0 400 30]")

        assert _words(b"q 72 690 100 30 re W n 1 0 0 1 72 690 cm /X Do Q", forms={b"X": form}) == ["Inside"]

    def test_a_clip_set_inside_a_form_ends_with_the_form(self):
        form = _form(b"0 0 50 30 re W n BT /F1 12 Tf 0 10 Td (Inside) Tj ET", box=b"[0 0 400 30]")

        assert _words(b"q 1 0 0 1 72 690 cm /X Do Q " + _at(300, 700, b"After"), forms={b"X": form}) == ["Inside", "After"]

    def test_a_form_inside_a_form_is_clipped_to_both(self):
        inner = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 150 0 Td (Hidden) Tj ET", box=b"[0 0 400 30]")
        outer = _stream(b"/Y Do", b"/Type /XObject /Subtype /Form /BBox [0 0 100 30] /Resources << /XObject << /Y 7 0 R >> >>")

        assert _words(b"q 1 0 0 1 72 690 cm /X Do Q " + _at(72, 600, b"Body"), forms={b"X": outer, b"Y": inner}) == ["Inside", "Body"]

    def test_a_form_whose_box_cannot_be_measured_clips_nothing(self):
        form = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 300 0 Td (Further) Tj ET", box=b"[0 0 (wide) 30]")

        assert "Inside" in " ".join(_lines(b"q 1 0 0 1 72 690 cm /X Do Q", forms={b"X": form}))


class TestClipsThatAreNotFollowed:
    def test_text_drawn_as_a_clip_does_not_hide_what_comes_after(self):
        """Known limitation: the clip text itself adds (render modes 4 to 7) is not followed."""
        content = b"q " + _at(80, 700, b"Mask", b"7 Tr") + _at(300, 600, b"Further", b"0 Tr") + b"Q"

        assert "Further" in " ".join(_lines(content))
