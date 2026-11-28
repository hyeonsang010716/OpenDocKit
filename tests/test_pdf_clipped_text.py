"""Text drawn outside the clipping path in force is not on the page, and is not read."""

from io import BytesIO
from types import SimpleNamespace

from pdfminer import pdffont
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

        def font(descent: float = -0.2, bbox: object = (0, -200, 1000, 900), **more: object) -> object:
            return SimpleNamespace(get_descent=lambda: descent, bbox=bbox, hscale=0.001, vscale=0.001, is_vertical=lambda: False, **more)

        def outside(font: object, x: float, y: float, width: float = 6.0, size: float = 10.0, rise: float = 0.0,
                    adrift: tuple[float, float] | None = (0.0, 0.0)) -> bool:
            low = y + font.get_descent() * size   # the box as it would stand without the rise: the reach must add it
            char = SimpleNamespace(graphicstate=state, x0=x, x1=x + width, y0=low, y1=low + size, adv=width, matrix=(1, 0, 0, 1, x, y))
            device = SimpleNamespace(**{_pdf_pdfminer._ADRIFT: adrift})
            return _pdf_pdfminer._outside_its_clip(char, device, font, size, 1.0, rise)

        plain = font()
        assert not outside(plain, 80.0, 700.0) and outside(plain, 300.0, 700.0) and outside(plain, 80.0, 600.0)
        # across: the ink of a glyph may be wider than its advance, as wide as the box of the font (10pt at this size)
        assert not outside(plain, 171.0, 700.0) and outside(plain, 173.0, 700.0)
        assert not outside(plain, 64.0, 700.0) and outside(plain, 61.0, 700.0)
        assert outside(font(bbox=(-300, -200, 1000, 900)), 176.0, 700.0) and not outside(font(bbox=(-300, -200, 1000, 900)), 174.0, 700.0)
        # upwards: the box is under the clip, the glyph of a deep font is not
        deep = font(-3.0, (0, -3000, 1000, 950))
        assert outside(plain, 80.0, 725.0) and not outside(deep, 80.0, 725.0) and outside(deep, 80.0, 760.0)
        assert not outside(plain, 80.0, 660.0, rise=22.0) and outside(plain, 80.0, 660.0, rise=-22.0)
        assert outside(plain, 80.0, 660.0)
        # pdfminer may have placed the string short: it is judged as far on as it may stand
        assert outside(plain, 20.0, 700.0) and not outside(plain, 20.0, 700.0, adrift=(0.0, 65.0))
        assert outside(plain, 20.0, 700.0, adrift=(-65.0, 0.0)) and outside(plain, 20.0, 700.0, adrift=(0.0, 30.0))
        assert not outside(plain, 300.0, 700.0, adrift=(-150.0, 0.0)) and not outside(plain, 300.0, 700.0, adrift=None)
        # a character that cannot be judged is kept
        assert not outside(plain, 300.0, 700.0, width=0.0)                                   # a font whose widths are not known
        assert not outside(font(bbox=None), 300.0, 700.0) and not outside(font(bbox=(0, 0, 0, 0)), 300.0, 700.0)
        assert not outside(font(bbox=(0, 0, 1, 1)), 300.0, 700.0) and not outside(font(bbox=(0, -200, 400, 900)), 300.0, 700.0)
        type3 = object.__new__(pdffont.PDFType3Font)
        type3.__dict__.update(bbox=(0, -200, 1000, 900), hscale=0.001, vscale=0.001, descent=-200, vertical=False)
        assert not outside(type3, 300.0, 700.0)
        assert not outside(font(bbox=(0, 900, 1000, -200)), 300.0, 700.0) and not outside(font(bbox=("a", 0, 1, 1)), 300.0, 700.0)
        assert not outside(font(bbox=(0, -200, float("inf"), 900)), 300.0, 700.0)
        assert not outside(SimpleNamespace(get_descent=lambda: -0.2, bbox=(0, -200, 1000, 900), hscale=0.001, vscale=0.001,
                                           is_vertical=lambda: True), 300.0, 700.0)
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
        huge = b"1" + b"0" * 400
        content = b"q " + huge + b" 0 0 1 0 0 cm 0 0 10 10 re W n " + _at(80, 700, b"Shown") + b"Q"

        assert _pdf_pdfminer._box_of([(0.0, 0.0), (1.0, 1.0)], (float("inf"), 0, 0, 1, 0, 0)) is None
        assert _pdf_pdfminer._box_of([(1.0, 1.0)], (float("nan"), 0, 0, 1, 0, 0)) is None
        assert "Hidden" not in convert(_page(content), "a.pdf").text   # and nothing raises

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


class TestTextThatCannotBeJudgedIsKept:
    """What pdfminer says of a character is not always where a viewer draws it. Where it cannot be trusted the
    character stays, as before the clip was followed."""

    def test_a_string_in_a_font_without_widths_is_kept_whole(self):
        """pdfminer draws every glyph of such a font on the origin of its string: a line that starts outside its clip
        and ends inside went whole, the shown end too."""
        font = b"<< /Type /Font /Subtype /TrueType /BaseFont /Foo >>"
        content = b"q 172 690 300 30 re W n " + _at(72, 700, b"The start is hidden but the end is Shown") + b"Q"

        assert "Shown" in convert(_page(content, font=font), "a.pdf").text

    def test_a_glyph_of_a_type3_font_is_kept(self):
        """Its glyphs are drawings in a space of their own, and its /FontBBox may be left all zeros."""
        glyph = b"1000 0 0 0 750 750 d1 0 0 750 750 re f"
        font = (b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 0 0] /FontMatrix [0.012 0 0 0.012 0 0] /CharProcs << /a 7 0 R >> "
                b"/Encoding << /Type /Encoding /Differences [97 /a] >> /FirstChar 97 /LastChar 97 /Widths [1000] >>")
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
                         4: _stream(b"q 72 704 400 100 re W n BT /F2 1 Tf 1 0 0 1 72 700 Tm (aaa) Tj ET Q " + _at(72, 600, b"Body")),
                         5: _FONT, 6: font, 7: _stream(glyph)})
        seen = _characters(data)

        assert seen.count("a") == 3

    def test_a_font_whose_box_says_nothing_is_not_judged(self):
        font = (b"<< /Type /Font /Subtype /TrueType /BaseFont /Foo /FirstChar 32 /LastChar 122 /Widths [%s] "
                b"/FontDescriptor << /Type /FontDescriptor /FontName /Foo /Flags 32 /FontBBox [0 0 0 0] /Ascent 0 /Descent -900 "
                b"/CapHeight 0 /ItalicAngle 0 /StemV 80 >> >>") % b" ".join([b"500"] * 91)
        content = b"q 72 704 400 100 re W n " + _at(72, 700, b"Last line of a notice") + b"Q"

        assert _words(content, font=font) == "Last line of a notice".split()

    def test_a_glyph_wider_than_its_advance_cut_by_the_edge_is_kept(self):
        """/Widths may say less than the glyph is wide: the letter the edge of the clip runs through stays."""
        font = (b"<< /Type /Font /Subtype /TrueType /BaseFont /Foo /FirstChar 32 /LastChar 122 /Widths [%s] "
                b"/FontDescriptor << /Type /FontDescriptor /FontName /Foo /Flags 32 /FontBBox [0 -200 1000 900] /Ascent 900 "
                b"/Descent -200 /CapHeight 700 /ItalicAngle 0 /StemV 80 >> >>") % b" ".join([b"10"] * 91)
        content = b"q 84 690 300 30 re W n BT /F1 12 Tf 1 0 0 1 80 700 Tm [(S) -650 (h) -650 (o) -650 (w) -650 (n)] TJ ET Q"

        assert "".join(_words(content, font=font)) == "Shown"

    def test_a_string_after_others_set_with_character_spacing_is_judged_where_it_may_stand(self, monkeypatch):
        """pdfminer leaves out the spacing after the last glyph of a string: the next string of the text object stands
        one spacing short of where it is drawn (65pt on a real equation), outside a clip it is drawn inside."""
        content = (b"q 100 690 100 30 re W n BT /F1 12 Tf 80 Tc 1 0 0 1 20 700 Tm (a) Tj (b) Tj ET "
                   b"BT /F1 12 Tf 0 Tc 1 0 0 1 20 650 Tm (c) Tj (d) Tj ET Q")

        assert _drawn(monkeypatch, content) == "b"

    def test_the_spacing_that_counts_is_the_one_each_string_before_was_set_with(self, monkeypatch):
        """A run set wide, then a word set as usual: the word stands where the run before it ends."""
        def drawn(text: bytes) -> str:
            return _drawn(monkeypatch, b"q 100 690 100 30 re W n BT /F1 12 Tf 1 0 0 1 20 700 Tm " + text + b" ET Q")

        assert drawn(b"80 Tc (a) Tj 0 Tc (b) Tj") == "b"                       # 20 + 6.7 + 80
        assert drawn(b"40 Tc (a) Tj (a) Tj 0 Tc (b) Tj") == "b"               # 20 + 2 x (6.7 + 40)
        assert drawn(b"40 Tc 50 Tz (a) Tj (a) Tj 100 Tz 0 Tc (b) Tj") == ""   # 20 + 2 x (3.3 + 20): short of the clip
        assert drawn(b"-80 Tc (a) Tj 0 Tc (b) Tj") == ""                      # drawn further left still
        assert drawn(b"80 Tc (a) ' 0 Tc (b) Tj") == "b"                       # every operator that shows text

    def test_word_spacing_in_a_font_of_several_bytes_is_not_judged(self):
        """pdfminer gives such a font no word spacing; a viewer adds it after the one-byte code 32."""
        cid = (b"<< /Type /Font /Subtype /Type0 /BaseFont /Malgun /Encoding /KSCms-UHC-H /DescendantFonts [<< /Type /Font "
               b"/Subtype /CIDFontType2 /BaseFont /Malgun /CIDSystemInfo << /Registry (Adobe) /Ordering (Korea1) /Supplement 2 >> "
               b"/DW 1000 /FontDescriptor << /Type /FontDescriptor /FontName /Malgun /Flags 4 /FontBBox [0 -200 1000 900] "
               b"/Ascent 900 /Descent -200 /CapHeight 700 /ItalicAngle 0 /StemV 80 >> >>] >>")
        with_spacing = b"q 72 690 200 30 re W n BT /F1 12 Tf -6 Tw 1 0 0 1 72 700 Tm (AA AA AA AA AA AA END) Tj ET Q"
        without = with_spacing.replace(b"-6 Tw", b"0 Tw")

        assert "END" in convert(_page(with_spacing, font=cid), "a.pdf").text
        assert "END" not in convert(_page(without, font=cid), "a.pdf").text


class TestMarksWithoutAdvance:
    """A glyph with no advance of its own (a Thai vowel or tone mark) stands on the glyph drawn before it and goes
    with it: the hidden half of a real slide deck came out as its marks alone."""

    _MARKED = (b"<< /Type /Font /Subtype /TrueType /BaseFont /Marked /FirstChar 32 /LastChar 122 /Widths [%s] "
               b"/FontDescriptor << /Type /FontDescriptor /FontName /Marked /Flags 32 /FontBBox [0 -200 1000 900] /Ascent 900 "
               b"/Descent -200 /CapHeight 700 /ItalicAngle 0 /StemV 80 >> >>") % b" ".join(
                   b"0" if code == ord("x") else b"500" for code in range(32, 123))

    def _drawn(self, monkeypatch: pytest.MonkeyPatch, content: bytes, font: bytes | None = None) -> str:
        seen: list[str] = []
        read_page = pdf_module._read_page

        def noting(page: object, *args: object) -> object:
            seen.append("".join(char["text"] for char in page.chars))
            return read_page(page, *args)

        monkeypatch.setattr(pdf_module, "_read_page", noting)
        data = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            4: _stream(content), 5: self._MARKED, 6: font or _FONT})
        convert(data, "a.pdf")
        return seen[0]

    def test_the_marks_of_hidden_letters_go_with_them(self, monkeypatch):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"axbx") + _at(300, 700, b"cxdx") + b"Q"

        assert self._drawn(monkeypatch, content) == "axbx"

    def test_a_mark_drawn_in_a_string_of_its_own_goes_with_the_letter_before_it(self, monkeypatch):
        content = b"q 72 690 100 30 re W n " + _at(80, 700, b"a") + _at(85, 700, b"x") + _at(300, 700, b"c") + _at(305, 700, b"x") + b"Q"

        assert self._drawn(monkeypatch, content) == "ax"

    def test_a_string_of_a_font_without_widths_does_not_go_with_a_hidden_letter_of_another_font(self, monkeypatch):
        """Every glyph of such a font has no advance: none is a mark on a letter of another font."""
        unmeasured = b"<< /Type /Font /Subtype /TrueType /BaseFont /Foo >>"
        content = (b"q 72 690 100 30 re W n " + _at(300, 700, b"c")
                   + b"BT /F2 12 Tf 1 0 0 1 300 650 Tm (kept) Tj ET Q")

        assert self._drawn(monkeypatch, content, unmeasured) == "kept"


def _characters(data: bytes) -> str:
    """Every character the reader keeps of the first page, as pdfminer names it."""
    import pdfplumber

    from opendockit import ConvertOptions  # noqa: F401 — the reader is imported, and with it its changes to pdfminer
    token = _pdf_pdfminer._INFLATE_BUDGET.set(_pdf_pdfminer._InflateBudget(1 << 30))
    try:
        with pdfplumber.open(BytesIO(data)) as pdf:
            return "".join(char["text"] for char in pdf.pages[0].chars)
    finally:
        _pdf_pdfminer._INFLATE_BUDGET.reset(token)


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


class TestClipPathMeasuredOnce:
    def test_a_path_that_comes_again_grown_is_not_measured_from_its_start(self):
        """pdfminer does not end the path at `F`: `re W F` over and over builds one path, and measuring it whole each
        time cost the square of its length (10,000 lines took over a minute)."""
        looked = []

        class Path(list):
            def __getitem__(self, key: object) -> object:
                found = super().__getitem__(key)
                looked.append(len(found) if isinstance(key, slice) else 1)
                return found

            def __iter__(self) -> object:
                raise AssertionError("the whole path was walked")

        interpreter = SimpleNamespace(curpath=Path([("m", 5.0, 5.0)]))
        for step in range(1000):
            interpreter.curpath.append(("l", float(step), -float(step)))
            assert _pdf_pdfminer._path_box(interpreter) == (0.0, -float(step), max(5.0, float(step)), 5.0)
        assert _pdf_pdfminer._path_box(interpreter) == (0.0, -999.0, 999.0, 5.0)   # nothing added: nothing looked at

        assert sum(looked) <= 1001

    def test_another_path_is_measured_from_its_start(self):
        interpreter = SimpleNamespace(curpath=[("m", 0.0, 0.0), ("l", 10.0, 10.0)])
        assert _pdf_pdfminer._path_box(interpreter) == (0.0, 0.0, 10.0, 10.0)

        interpreter.curpath = [("m", 50.0, 50.0), ("c", 60.0, 70.0, 80.0, 90.0, 55.0, 65.0), ("h",)]

        assert _pdf_pdfminer._path_box(interpreter) == (50.0, 50.0, 80.0, 90.0)

    def test_a_path_without_a_point_has_no_box(self):
        assert _pdf_pdfminer._path_box(SimpleNamespace(curpath=[])) is None
        assert _pdf_pdfminer._path_box(SimpleNamespace(curpath=[("h",)])) is None

    def test_under_a_matrix_that_turns_the_clip_is_the_box_of_all_four_corners(self):
        turned = (0.7071, 0.7071, -0.7071, 0.7071, 0.0, 0.0)   # an eighth of a turn
        interpreter = SimpleNamespace(curpath=[("m", 0.0, 0.0), ("l", 100.0, 0.0), ("l", 100.0, 20.0), ("l", 0.0, 20.0), ("h",)],
                                      ctm=turned, graphicstate=SimpleNamespace())

        _pdf_pdfminer._clip_to_path(interpreter)

        assert [round(value, 1) for value in getattr(interpreter.graphicstate, _pdf_pdfminer._CLIP)] == [-14.1, 0.0, 70.7, 84.9]

    def test_the_clip_of_a_path_that_grew_is_the_box_of_all_of_it(self):
        """After `F` the path stays as it was for pdfminer, so the next clip is measured with it: a box larger than the
        clip (the first word is kept though a viewer hides it), never smaller."""
        content = (b"q 1 g 60 690 400 30 re W F 290 690 100 30 re W F 0 g "
                   + _at(80, 700, b"Kept") + _at(300, 700, b"Shown") + _at(500, 700, b"Hidden") + b"Q")

        assert _words(content) == ["Kept", "Shown"]


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

    def test_a_page_whose_text_is_hidden_inside_a_form_is_not_handed_to_the_other_reader(self):
        """The dropped characters are counted on the page, from inside a form too."""
        form = _form(b"BT /F1 12 Tf 300 10 Td (Hidden words in a form of the page) Tj ET")

        result = convert(_page(b"q 1 0 0 1 72 690 cm /X Do Q", forms={b"X": form}), "a.pdf")

        assert "Hidden" not in result.text
        assert not any("simple extraction" in warning for warning in result.warnings)

    def test_a_form_whose_box_cannot_be_measured_clips_nothing(self):
        form = _form(b"BT /F1 12 Tf 0 10 Td (Inside) Tj 300 0 Td (Further) Tj ET", box=b"[0 0 (wide) 30]")

        assert "Inside" in " ".join(_lines(b"q 1 0 0 1 72 690 cm /X Do Q", forms={b"X": form}))


class TestClipsThatAreNotFollowed:
    def test_text_drawn_as_a_clip_does_not_hide_what_comes_after(self):
        """Known limitation: the clip text itself adds (render modes 4 to 7) is not followed."""
        content = b"q " + _at(80, 700, b"Mask", b"7 Tr") + _at(300, 600, b"Further", b"0 Tr") + b"Q"

        assert "Further" in " ".join(_lines(content))
