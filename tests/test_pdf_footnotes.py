"""PDF footnotes: the raised marks in the body are linked to the notes under the footnote rule (`[^n]`, `[^n]: text`) only when
the page allows a single reading, and every other page is left as it was (`_pdf_footnotes._link_footnotes`).

The pages are drawn the way Word and LibreOffice print them: body text at 11pt, a raised mark at 0.6 of the size, a short rule
(a filled rectangle 0.5pt tall, a quarter to a third of the text width) at the left margin above the notes, and the notes at 9pt
with the number raised in front."""

from io import BytesIO

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _raw_pdf


_LEFT = 72.0
_BODY_TOP = 760.0
_RULE_Y = 160.0
_LATIN = "Helvetica"

type Run = str | tuple[str, str]  # text, or ("^", text) for a raised run


def _draw_line(pdf, x: float, y: float, runs: list[Run], font: str, size: float) -> None:
    for run in runs:
        raised = isinstance(run, tuple)
        text = run[1] if raised else run
        run_size = size * 0.6 if raised else size
        pdf.setFont(font, run_size)
        pdf.drawString(x, y + (size * 0.35 if raised else 0), text)
        x += pdf.stringWidth(text, font, run_size)


def _draw_page(pdf, body: list[list[Run]], notes: list[list[Run]], *, font: str = _LATIN, body_size: float = 11.0,
               note_size: float = 9.0, rule: tuple[float, float] | None = (_LEFT, 144.0), rule_y: float = _RULE_Y,
               note_x: float = _LEFT, body_x: float = _LEFT, body_top: float = _BODY_TOP) -> None:
    """One page: `body` lines from the top, then the rule (left end, length; None for no rule) and the `notes` under it."""
    for index, runs in enumerate(body):
        _draw_line(pdf, body_x, body_top - index * body_size * 1.3, runs, font, body_size)
    if rule is not None:
        pdf.rect(rule[0], rule_y, rule[1], 0.5, stroke=0, fill=1)
    for index, runs in enumerate(notes):
        _draw_line(pdf, note_x, rule_y - 12 - index * note_size * 1.2, runs, font, note_size)


def _pdf(*pages: dict) -> bytes:
    """A PDF with one A4 page per `_draw_page` keyword set."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for page in pages:
        _draw_page(pdf, **page)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


_FILLER = "The committee reviewed the results for the period against the targets set for the year."


def _text(*pages: dict, **options) -> str:
    return convert(_pdf(*pages), "a.pdf", ConvertOptions(**options)).text


def _with_sideways_text(runs: list[Run], *, second_note: bool) -> bytes:
    """A page with note 1 and its mark ('135^1%'), `runs` drawn sideways in the right margin, and with `second_note` note 2 and
    a raised 2 in the body that could take it ('m^2')."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    body = [["The debt ratio rose to 135", ("^", "1"), "% in the first half."], [_FILLER]]
    notes = [[("^", "1"), " Source of the ratio."]]
    if second_note:
        body.append(["The site covers 2,400 m", ("^", "2"), "."])
        notes.append([("^", "2"), " Source of the rotated table."])
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    _draw_page(pdf, body, notes)
    pdf.saveState()
    pdf.translate(560, 300)
    pdf.rotate(90)
    _draw_line(pdf, 0, 0, runs, _LATIN, 9.0)
    pdf.restoreState()
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestLinked:
    def test_mark_and_note_are_linked_in_place(self):
        """The raised number is the mark of the note under the rule with the same number: `135[^1]%` and `[^1]: …` at the bottom
        of the same page. Left as `135^1%` it reads like 135 to the power of 1."""
        text = _text({"body": [[_FILLER], ["The debt ratio rose to 135", ("^", "1"), "% in the first half."], [_FILLER]],
                      "notes": [[("^", "1"), " Debt ratio is total liabilities divided by total equity."]]})

        assert "The debt ratio rose to 135[^1]% in the first half." in text
        assert text.rstrip().endswith("[^1]: Debt ratio is total liabilities divided by total equity.")
        assert "135^1%" not in text and "\n^1 " not in text

    def test_numbers_with_a_parenthesis(self):
        """Korean documents number notes '1)': the mark is '^{1)}' and the note starts with a raised '1)'."""
        text = _text({"font": _FONT, "body": [["상반기 부채비율은 135", ("^", "1)"), "%이다."], ["하반기에는 낮아질 것으로 전망된다", ("^", "2)"), "."]],
                      "notes": [[("^", "1)"), " 부채총계를 자본총계로 나눈 값이다."], [("^", "2)"), "연결 기준이다."]]})

        assert "상반기 부채비율은 135[^1]%이다." in text
        assert "하반기에는 낮아질 것으로 전망된다[^2]." in text
        assert "[^1]: 부채총계를 자본총계로 나눈 값이다." in text
        assert "[^2]: 연결 기준이다." in text

    def test_each_note_is_a_paragraph_of_its_own(self):
        """A footnote definition joined to the body line above it, or to the note before it, does not start a definition in
        Markdown. Here the notes are set at 10pt right under 11pt body text (close enough in size and spacing to be read as one
        paragraph), and the wrapped line of the first note stays with it."""
        text = _text({"body": [["Revenue grew", ("^", "1"), " and costs fell", ("^", "2"), "."], [_FILLER]],
                      "body_top": 200.0, "rule_y": 176.0, "note_size": 10.0,
                      "notes": [[("^", "1"), " Revenue is stated before the deduction of returns, rebates, volume bonuses and"], ["sales discounts."],
                                [("^", "2"), " Costs exclude depreciation."]]})

        assert "Revenue grew[^1] and costs fell[^2]." in text
        assert f"{_FILLER}\n\n[^1]: Revenue is stated before the deduction of returns, rebates, volume bonuses and\nsales discounts.\n\n[^2]: Costs exclude depreciation." in text

    def test_raised_number_that_no_note_has_stays_as_it_is(self):
        """An exponent after the last mark cannot take a note: the marks are chosen in reading order, one per note number."""
        text = _text({"body": [["Rents rose", ("^", "1"), " on sites of 2,400 m", ("^", "2"), " and more."], [_FILLER],
                               ["The output reached 10", ("^", "6"), " units."]],
                      "notes": [[("^", "1"), " Rents of the head office are excluded."]]})

        assert "Rents rose[^1] on sites of 2,400 m^2 and more." in text
        assert "The output reached 10^6 units." in text
        assert "[^1]: Rents of the head office are excluded." in text

    def test_numbering_that_goes_on_keeps_the_printed_numbers(self):
        text = _text({"body": [["First claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Source of the first claim."]]},
                     {"body": [["Second claim", ("^", "2"), "."], [_FILLER]], "notes": [[("^", "2"), " Source of the second claim."]]})

        assert "First claim[^1]." in text and "[^1]: Source of the first claim." in text
        assert "Second claim[^2]." in text and "[^2]: Source of the second claim." in text

    def test_numbering_that_starts_again_is_written_with_the_page(self):
        """Footnote labels must be unique in the document: numbering that starts again (each chapter) gets the page number."""
        text = _text({"body": [["First chapter claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Chapter one source."]]},
                     {"body": [["Second chapter claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Chapter two source."]]})

        assert "First chapter claim[^p1-1]." in text and "[^p1-1]: Chapter one source." in text
        assert "Second chapter claim[^p2-1]." in text and "[^p2-1]: Chapter two source." in text

    def test_notes_under_the_rule_of_each_column(self):
        """In two columns each column has its own rule and notes; the lines of the other column beside the notes are not notes."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        left = [["Left column claim", ("^", "1"), "."]] + [["Left column text goes on."]] * 3
        right = [["Right column text."]] * 4 + [["Right column claim", ("^", "2"), "."]]
        _draw_page(pdf, left, [[("^", "1"), " Left note."]], rule=(72.0, 60.0))
        _draw_page(pdf, right, [[("^", "2"), " Right note."]], rule=(310.0, 60.0), rule_y=_RULE_Y - 30, note_x=310.0, body_x=310.0)
        _draw_line(pdf, 310.0, _RULE_Y - 12, ["Right column line beside the left note."], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        assert "Left column claim[^1]." in text and "Right column claim[^2]." in text
        assert "[^1]: Left note." in text and "[^2]: Right note." in text
        assert "Right column line beside the left note." in text and "[^1]: Left note. Right" not in text

    def test_end_of_a_note_carried_from_the_previous_page_is_kept_in_place(self):
        """A long note goes on at the top of the next page's notes without a number: it stays as it is, and the notes after it
        are linked."""
        text = _text({"body": [["A long claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " A long note that starts here and"]]},
                     {"body": [["Another claim", ("^", "2"), "."], [_FILLER]],
                      "notes": [["goes on at the top of the next page."], [("^", "2"), " The note of the other claim."]]})

        assert "goes on at the top of the next page.\n\n[^2]: The note of the other claim." in text
        assert "Another claim[^2]." in text

    def test_wrapped_line_that_starts_with_a_decimal_stays_with_its_note(self):
        """'1.5 times …' at the start of a wrapped line is not note 1 again: a plain number starts a note only when a space,
        ')' or '. ' follows it."""
        text = _text({"body": _UNLINKED_BODY, "notes": [[("^", "1"), " Debt ratio is total liabilities divided by total equity at the end of the reporting period, about"],
                                                       ["1.5 times the ratio of last year."]]})

        assert "135[^1]%" in text
        assert "[^1]: Debt ratio is total liabilities divided by total equity at the end of the reporting period, about\n1.5 times the ratio of last year." in text

    def test_wrapped_line_that_starts_with_a_number_stays_with_its_note(self):
        """'2 percent …' at the start of a wrapped line is not note 2, even with a raised 2 in the body after the mark of note 1
        ('m^2'): only a raised number starts a note."""
        text = _text({"body": [["Rents rose", ("^", "1"), " on sites of 2,400 m", ("^", "2"), " and more."], [_FILLER]],
                      "notes": [[("^", "1"), " The figure excludes the head office and the two regional warehouses, which account for"],
                                ["2 percent of the total floor area."]]})

        assert "Rents rose[^1] on sites of 2,400 m^2 and more." in text
        assert "[^1]: The figure excludes the head office and the two regional warehouses, which account for\n2 percent of the total floor area." in text

    def test_symbol_note_before_the_numbered_notes_stays_as_text(self):
        """'∗Equal contribution' above the numbered notes of a paper is not attached to a note; the numbered notes are linked."""
        text = _text({"body": [["A claim", ("^", "1"), "."], [_FILLER]],
                      "notes": [["* Equal contribution of the authors."], [("^", "1"), " The source of the claim."]]})

        assert "A claim[^1]." in text
        assert "* Equal contribution of the authors.\n\n[^1]: The source of the claim." in text

    def test_numbering_that_jumps_ahead(self):
        """The pages between may have notes that were not found (no rule, a mark in a table): a higher number still follows."""
        text = _text({"body": [["First claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " First source."]]},
                     {"body": [["Later claim", ("^", "7"), "."], [_FILLER]], "notes": [[("^", "7"), " Later source."]]})

        assert "First claim[^1]." in text and "Later claim[^7]." in text and "[^7]: Later source." in text

    def test_sideways_text_with_digits_of_its_own_size(self):
        """A stamp in the margin ('arXiv:1406.2661v1 [stat.ML] 10 Jun 2014') has digits, none of them raised."""
        text = convert(_with_sideways_text(["arXiv:1406.2661v1 [stat.ML] 10 Jun 2014"], second_note=False), "a.pdf").text

        assert "arXiv:1406.2661v1" in text and "135[^1]%" in text and "[^1]: Source of the ratio." in text

    def test_two_marks_on_one_line(self):
        text = _text({"body": [["Sales", ("^", "1"), " and margins", ("^", "2"), " both improved."], [_FILLER]],
                      "notes": [[("^", "1"), " Net of returns."], [("^", "2"), " Gross margin."]]})

        assert "Sales[^1] and margins[^2] both improved." in text

    def test_note_and_mark_stay_in_the_section_of_their_page(self):
        result = convert(_pdf({"body": [[_FILLER]], "notes": [], "rule": None},
                              {"body": [["A claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Its source."]]}), "a.pdf")

        assert "[^" not in result.sections[0].text
        assert "A claim[^1]." in result.sections[1].text and "[^1]: Its source." in result.sections[1].text

    def test_include_footnotes_off_keeps_the_notes_of_a_pdf(self):
        """`include_footnotes` is not applied to PDF: which lines are notes is inferred, and inferred text is never removed."""
        page = {"body": [["A claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Its source."]]}

        assert _text(page, include_footnotes=False) == _text(page)


_UNLINKED_BODY = [["The debt ratio rose to 135", ("^", "1"), "% in the first half."], [_FILLER]]
_TWO_MARKS = [*_UNLINKED_BODY, ["Margins rose too", ("^", "2"), "."]]


class TestLeftAsIs:
    """Pages whose evidence does not agree keep the output they had before footnotes were linked."""

    @staticmethod
    def _assert_unlinked(text: str) -> None:
        assert "[^" not in text
        assert "The debt ratio rose to 135^1% in the first half." in text

    def test_without_a_footnote_rule(self):
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "rule": None, "notes": [[("^", "1"), " Source."]]}))

    def test_rule_across_the_whole_text_is_not_a_footnote_rule(self):
        body = [*_UNLINKED_BODY, ["A line as wide as the page so that the text spans most of it from left to right edge."]]
        self._assert_unlinked(_text({"body": body, "rule": (_LEFT, 451.0), "notes": [[("^", "1"), " Source."]]}))

    def test_short_dash_is_not_a_footnote_rule(self):
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "rule": (_LEFT, 12.0), "notes": [[("^", "1"), " Source."]]}))

    def test_notes_far_below_the_rule(self):
        """A short rule higher up the page (under a heading) with small numbered lines at the bottom is not a footnote rule."""
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "rule_y": _RULE_Y + 120, "notes": [[], [], [], [], [], [], [], [], [], [], [],
                                                                                              [("^", "1"), " Source."]]}))

    def test_note_without_a_mark_in_the_body(self):
        """The mark of note 2 is somewhere the body does not show (a table cell, which is not marked): the page is not linked
        at all — the mark found for note 1 might not be the only thing the page hides."""
        self._assert_unlinked(_text({"body": _UNLINKED_BODY,
                                     "notes": [[("^", "1"), " First source."], [("^", "2"), " Second source."]]}))

    def test_more_than_one_way_to_choose_the_marks(self):
        """'m^2' between the marks 1 and 3 could be the mark of note 2 as well as the real one."""
        body = [["The debt ratio rose to 135", ("^", "1"), "% in the first half."], ["On 40 m", ("^", "2"), " of land", ("^", "2"), "."],
                ["Third", ("^", "3"), "."]]
        text = _text({"body": body, "notes": [[("^", "1"), " One."], [("^", "2"), " Two."], [("^", "3"), " Three."]]})

        self._assert_unlinked(text)
        assert "On 40 m^2 of land^2." in text

    def test_exponent_with_a_note_number_after_the_marks(self):
        """'m^2' after the mark of note 3 could still be the mark of note 2 for all the page shows (a floated caption is read out
        of place), so the page is left unlinked."""
        text = _text({"body": [["One", ("^", "1"), ", two", ("^", "2"), ", three", ("^", "3"), "."], ["An area of 40 m", ("^", "2"), "."], [_FILLER]],
                      "notes": [[("^", "1"), " First note."], [("^", "2"), " Second note."], [("^", "3"), " Third note."]]})

        assert "[^" not in text
        assert "One^1, two^2, three^3." in text and "An area of 40 m^2." in text

    @pytest.mark.parametrize("caption_y", [700.0, 300.0])
    def test_mark_in_a_floated_caption(self, caption_y):
        """LaTeX numbers a mark in a figure caption in the order of the source and floats the figure to the top or bottom of the
        page: the real mark of note 2 is read before mark 1 or after mark 3, and 'm^2' between them must not take note 2."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        body = [[_FILLER], ["The debt ratio rose to 135", ("^", "1"), "% in the first half."], [_FILLER],
                ["The largest site covers 2,400 m", ("^", "2"), " of floor area (Figure 1)."], [_FILLER],
                ["Costs were stated before tax", ("^", "3"), " as in earlier reports."], [_FILLER]]
        top = 640.0 if caption_y > 500 else 760.0
        _draw_page(pdf, body, [[("^", "1"), " Source of the ratio."], [("^", "2"), " Measured by the surveyors."],
                               [("^", "3"), " Corporate tax only."]], body_top=top)
        _draw_line(pdf, _LEFT + 40, caption_y, ["Figure 1: Floor area of the sites in the survey", ("^", "2"), "."], _LATIN, 10.0)
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        assert "[^" not in text
        assert "2,400 m^2 of floor area" in text

    def test_mark_left_unlinked_on_the_previous_page(self):
        """LaTeX can set a note on the page after its mark: note 2 at the top of page 2 belongs to the raised 2 left on page 1,
        not to 'm^2' on page 2."""
        first = {"body": [["A claim", ("^", "1"), " and a later one", ("^", "2"), "."], [_FILLER]], "notes": [[("^", "1"), " One."]]}
        second = {"body": [["The site covers 2,400 m", ("^", "2"), "."], [_FILLER]], "notes": [[("^", "2"), " Two."]]}
        result = convert(_pdf(first, second), "a.pdf")

        assert "A claim[^1] and a later one^2." in result.sections[0].text
        assert "[^" not in result.sections[1].text and "2,400 m^2." in result.sections[1].text

    def test_mark_hidden_on_the_previous_page(self):
        """The mark left on the previous page may be one it does not show as raised ('revenue2')."""
        first = {"body": [["A claim", ("^", "1"), "."], [_FILLER], ["The last line ends with the revenue2"]], "notes": [[("^", "1"), " One."]]}
        second = {"body": [["The site covers 2,400 m", ("^", "2"), "."], [_FILLER]], "notes": [[("^", "2"), " Note of the revenue figure."]]}
        result = convert(_pdf(first, second), "a.pdf")

        assert "A claim[^1]." in result.sections[0].text
        assert "[^" not in result.sections[1].text

    def test_page_after_one_with_raised_digits_it_does_not_show(self):
        """Whether the previous page left a mark is not known when it has raised digits it does not show."""
        first = {"body": [["Total sales", ("^", "(2)"), " rose."], [_FILLER]], "notes": [], "rule": None}
        second = {"body": [["The site covers 2,400 m", ("^", "2"), "."], [_FILLER]], "notes": [[("^", "2"), " Two."]]}
        result = convert(_pdf(first, second), "a.pdf")

        assert "[^" not in result.sections[1].text

    def test_page_after_a_page_read_the_simple_way(self, monkeypatch):
        """A page read with simple extraction shows no raised marks, so whether it left one for the next page is not known."""
        from opendockit.extractors import _pdf_footnotes

        pages = []
        original = _pdf_footnotes._link_footnotes

        def first_page_simple(read):
            read[0].simple = True
            pages.extend(read)
            original(read)

        monkeypatch.setattr("opendockit.extractors.pdf._link_footnotes", first_page_simple)
        result = convert(_pdf({"body": [[_FILLER]], "notes": [], "rule": None},
                              {"body": [["A claim", ("^", "1"), "."], [_FILLER]], "notes": [[("^", "1"), " Its source."]]}), "a.pdf")

        assert len(pages) == 2
        assert "[^" not in result.sections[1].text

    def test_note_numbers_with_a_gap(self):
        body = [*_UNLINKED_BODY, ["Later", ("^", "3"), "."]]
        self._assert_unlinked(_text({"body": body, "notes": [[("^", "1"), " One."], [("^", "3"), " Three."]]}))

    def test_numbers_that_do_not_follow_the_previous_page(self):
        """Page 2 starts at note 2 after page 1 ended at note 3: one of the two is not what it seems, and page 2 is left alone."""
        first = {"body": [["A", ("^", "1"), " b", ("^", "2"), " c", ("^", "3"), "."], [_FILLER]],
                 "notes": [[("^", "1"), " One."], [("^", "2"), " Two."], [("^", "3"), " Three."]]}
        second = {"body": [["The debt ratio rose to 135", ("^", "2"), "% in the first half."], [_FILLER]], "notes": [[("^", "2"), " Again two."]]}
        result = convert(_pdf(first, second), "a.pdf")

        assert "A[^1] b[^2] c[^3]." in result.sections[0].text
        assert "[^" not in result.sections[1].text and "135^2%" in result.sections[1].text

    def test_notes_as_large_as_the_body(self):
        """A numbered list under a short rule is set in the body size."""
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "note_size": 11.0, "notes": [[("^", "1"), " First item of a list."]]}))

    def test_other_line_between_the_notes(self):
        """A line of another kind between the notes (indented under them, not a line of the notes) means what is under the rule
        is not a block of notes. A line beside the notes, right of where they end, is another column (see
        `TestLinked::test_notes_under_the_rule_of_each_column`)."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, _TWO_MARKS, [[("^", "1"), " One source of the figures in this report."], [], [("^", "2"), " Two."]])
        _draw_line(pdf, 150.0, _RULE_Y - 12 - 9 * 1.2, ["An indented line"], _LATIN, 9.0)
        pdf.showPage()
        pdf.save()

        self._assert_unlinked(convert(buffer.getvalue(), "a.pdf").text)

    def test_further_note_after_a_gap(self):
        """The block of notes under the rule ends at a gap; another note further down means the block is not all of them, and
        linking the first alone would leave half of the page's notes as they were."""
        self._assert_unlinked(_text({"body": _TWO_MARKS, "notes": [[("^", "1"), " One."], [], [], [], [("^", "2"), " Two."]]}))

    def test_border_of_a_table_is_not_a_footnote_rule(self):
        """The note of a table set right under its bottom border (a table note) is not a page footnote."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, _UNLINKED_BODY, [[("^", "1"), " Source of the table."]], rule=None)
        top, row, columns = _RULE_Y + 50, 25.0, (_LEFT, _LEFT + 60, _LEFT + 120)
        for index in range(3):
            pdf.line(_LEFT, top - index * row, columns[-1], top - index * row)
        for x in columns:
            pdf.line(x, top, x, top - 2 * row)
        for index, cells in enumerate([("Item", "Value"), ("Sales", "1,200")]):
            for x, cell in zip(columns, cells, strict=False):
                _draw_line(pdf, x + 4, top - index * row - 17, [cell], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        assert "| Sales | 1,200 |" in text
        self._assert_unlinked(text)

    @pytest.mark.parametrize("label", ["1 ", "1. ", "1) ", "1)"])
    def test_note_numbers_that_are_not_raised(self, label):
        """A plain number at the start of a line under the rule may be the wrapped line of a note ('2 percent …')."""
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "notes": [[f"{label}Source of the ratio."]]}))

    @pytest.mark.parametrize("other", [["† Work done while at another company."], [("^", "a"), " Affiliation of the author."]])
    def test_symbol_note_after_a_numbered_note(self, other):
        """After a numbered note, a symbol note or a raised letter would be read as the wrapped line of that note."""
        self._assert_unlinked(_text({"body": _UNLINKED_BODY, "notes": [[("^", "1"), " Source of the ratio."], other]}))

    @pytest.mark.parametrize("cell", [(_LATIN, "Sales", "2"), (_LATIN, "1,200", "2)"), (_LATIN, "135%", "2"), (_LATIN, "Net (adj.)", "2"),
                                      (_LATIN, '"Net"', "2"), (_LATIN, "A", "2"), (_FONT, "액", "2)")])
    def test_mark_in_a_table_cell_with_an_exponent_of_the_same_number(self, cell):
        """The mark of note 2 is in a table cell, where raised numbers are not marked ('Sales2', '1,2002)'); the body's 'm^2'
        must not take note 2. A digit clearly smaller than the other cell text leaves the page unlinked."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen import canvas

        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))  # not left to another test that ran first
        font, text, mark = cell
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, ["The site covers 2,400 m", ("^", "2"), "."]],
                   [[("^", "1"), " Source of the ratio."], [("^", "2"), " Source of the table figure."]])
        top, row, columns = 600.0, 25.0, (_LEFT, _LEFT + 80, _LEFT + 160)
        for index in range(3):
            pdf.line(_LEFT, top - index * row, columns[-1], top - index * row)
        for x in columns:
            pdf.line(x, top, x, top - 2 * row)
        _draw_line(pdf, _LEFT + 4, top - 17, ["Item"], _LATIN, 11.0)
        _draw_line(pdf, columns[1] + 4, top - 17, ["Value"], _LATIN, 11.0)
        _draw_line(pdf, _LEFT + 4, top - row - 17, [text, ("^", mark)], font, 11.0)
        _draw_line(pdf, columns[1] + 4, top - row - 17, ["300"], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf").text

        assert f"| {text}{mark} | 300 |".replace('"', "&quot;") in result or f"| {text}{mark} | 300 |" in result
        self._assert_unlinked(result)
        assert "The site covers 2,400 m^2." in result

    def test_raised_number_left_as_plain_text(self):
        """A raised '(2)' is not written as a script ('^' needs a letter or digit first): it is shown as plain text, so it could
        be the mark of note 2 while 'm^2' takes the note."""
        body = [*_UNLINKED_BODY, ["Total sales", ("^", "(2)"), " rose."], ["The site covers 2,400 m", ("^", "2"), "."]]
        text = _text({"body": body, "notes": [[("^", "1"), " One."], [("^", "2"), " Two."]]})

        self._assert_unlinked(text)
        assert "Total sales(2) rose." in text

    @pytest.mark.parametrize("mark", ["2", "2)"])
    def test_raised_mark_in_a_borderless_table(self, mark):
        """The lines of a borderless table keep their scripts ('450^2'), but a mark in a table is not linked: it could be the
        mark of note 2 while 'm^2' in the body takes the note."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, ["The site covers 2,400 m", ("^", "2"), "."]],
                   [[("^", "1"), " Source of the ratio."], [("^", "2"), " Source of the table figure."]])
        rows = [["Item", "2023", "2024"], ["Sales", "900", "1,000"], ["Costs", "400", None], ["Profit", "500", "600"]]
        for index, row in enumerate(rows):
            for column, cell in enumerate(row):
                _draw_line(pdf, _LEFT + column * 120, 500 - index * 16, ["450", ("^", mark)] if cell is None else [cell], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        assert "| Costs | 400 | 450^" in text
        self._assert_unlinked(text)

    @pytest.mark.parametrize("rise", [0.0, 0.15])
    def test_small_mark_raised_too_little(self, rise):
        """A mark set small but raised less than a superscript is plain text in the line ('5%2'); it could be the mark of note 2
        while 'm^2' after it takes the note."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, [], ["The site covers 2,400 m", ("^", "2"), " of floor area."]],
                   [[("^", "1"), " Source of the ratio."], [("^", "2"), " Source of the cost figure."]])
        y = _BODY_TOP - 2 * 11.0 * 1.3
        pdf.setFont(_LATIN, 11.0)
        pdf.drawString(_LEFT, y, "Costs rose by 5%")
        pdf.setFont(_LATIN, 6.6)
        pdf.drawString(_LEFT + pdf.stringWidth("Costs rose by 5%", _LATIN, 11.0), y + 11.0 * rise, "2")
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        self._assert_unlinked(text)
        assert "Costs rose by 5%2" in text and "The site covers 2,400 m^2 of floor area." in text

    def test_two_notes_on_one_line(self):
        """Two notes set on one line: the second would be read as part of the first, and its mark left unlinked."""
        text = _text({"body": _TWO_MARKS, "notes": [[("^", "1"), " One.      ", ("^", "2"), " Two."]]})

        self._assert_unlinked(text)

    @pytest.mark.parametrize("line", ["Internal use only - page 1 of 1", "Note: all figures are unaudited."])
    def test_line_of_its_own_right_under_the_notes(self, line):
        """A footer or a 'Note:' line right under the last note, after a line that stops short of the text's right end, is not a
        wrapped line of that note; linked, it would be part of the note."""
        text = _text({"body": _UNLINKED_BODY, "notes": [[("^", "1"), " Source of the ratio."], [line]]})

        self._assert_unlinked(text)
        assert line in text

    def test_two_character_line_with_a_small_digit(self):
        """A line of two characters ('A' and a raised '2') gives no basis for the position of the digit; it could be the mark of
        note 2."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, ["The site covers 2,400 m", ("^", "2"), "."]], [[("^", "1"), " One."], [("^", "2"), " Two."]])
        _draw_line(pdf, 400.0, 560.0, ["A", ("^", "2")], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        self._assert_unlinked(convert(buffer.getvalue(), "a.pdf").text)

    def test_short_line_of_mostly_small_digits(self):
        """A line mostly of small characters ('A' and a raised '12') gives no basis for the position either."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, ["The site covers 2,400 m", ("^", "2"), "."]], [[("^", "1"), " One."], [("^", "2"), " Two."]])
        _draw_line(pdf, 400.0, 560.0, ["A", ("^", "12")], _LATIN, 11.0)
        pdf.showPage()
        pdf.save()

        self._assert_unlinked(convert(buffer.getvalue(), "a.pdf").text)

    def test_empty_raised_group_typed_in_a_line_of_mixed_sizes_converts(self):
        """A typed '^{}' on a line with a raised character is an empty raised group to the check of unshown raised digits."""
        text = _text({"body": [["Write ^{} for an empty group, as in x", ("^", "2")], [_FILLER]], "notes": [], "rule": None})

        assert "Write ^{} for an empty group, as in x^2" in text

    def test_combined_mark(self):
        """A mark for two notes ('^{2,3}') is not a mark of one note; the exponents after it must not take notes 2 and 3."""
        body = [["First", ("^", "1"), " and second", ("^", "2,3"), "."], ["An area of 40 m", ("^", "2"), " and 10", ("^", "3"), " units."], [_FILLER]]
        text = _text({"body": body, "notes": [[("^", "1"), " One."], [("^", "2"), " Two."], [("^", "3"), " Three."]]})

        assert "[^" not in text and "second^{2,3}." in text

    def test_raised_number_at_the_start_of_a_line(self):
        """A line does not start with a mark; a raised number there is the number of a note (of a block that was not taken), not
        the mark of note 2."""
        body = [*_UNLINKED_BODY, [("^", "2"), " A line that starts raised."]]
        self._assert_unlinked(_text({"body": body, "notes": [[("^", "1"), " One."], [("^", "2"), " Two."]]}))

    def test_mark_drawn_apart_as_a_small_number(self):
        """A mark drawn apart from its word is a line of its own holding a small number."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, [*_UNLINKED_BODY, ["The site covers 2,400 m", ("^", "2"), "."]], [[("^", "1"), " One."], [("^", "2"), " Two."]])
        _draw_line(pdf, 500.0, 500.0, ["2"], _LATIN, 6.6)
        pdf.showPage()
        pdf.save()

        self._assert_unlinked(convert(buffer.getvalue(), "a.pdf").text)

    def test_small_digit_in_sideways_text(self):
        """Sideways text is read apart from the body and its scripts are not marked: a raised mark in a table set sideways could
        be the mark of note 2 while 'm^2' takes the note."""
        self._assert_unlinked(convert(_with_sideways_text(["Total of the rotated table: 1,200", ("^", "2")], second_note=True), "a.pdf").text)

    def test_mark_that_was_not_recognized_as_raised(self):
        """A mark the body shows as plain text ('claim2') could be the mark of note 2 as well as the raised 2 after it."""
        body = [*_UNLINKED_BODY, ["The second claim2 is plain here."], ["The site covers 2,400 m", ("^", "2"), "."]]
        self._assert_unlinked(_text({"body": body, "notes": [[("^", "1"), " One."], [("^", "2"), " Two."]]}))

    def test_mark_in_a_font_without_a_unicode_mapping(self):
        """The mark of note 2 drawn in a font with no Unicode mapping comes out as '(cid:21)' and is dropped from the text; 'm^2'
        must not take note 2."""
        from reportlab.pdfbase.pdfmetrics import stringWidth

        def show(font: str, size: float, x: float, y: float, text: bytes) -> bytes:
            return b"BT /%s %g Tf %g %g Td %s Tj ET\n" % (font.encode(), size, x, y, text)

        def mark_line(y: float, before: str, mark: bytes, after: str) -> bytes:
            x = _LEFT + stringWidth(before, _LATIN, 11.0)
            return (show("F1", 11.0, _LEFT, y, b"(%s)" % before.encode()) + show("F2" if mark.startswith(b"<") else "F1", 6.6, x, y + 3.85, mark)
                    + show("F1", 11.0, x + stringWidth("2", _LATIN, 6.6), y, b"(%s)" % after.encode()))

        content = (show("F1", 11.0, _LEFT, 760.0, b"(%s)" % _FILLER.encode())
                   + mark_line(745.0, "The debt ratio rose to 135", b"(1)", "% in the first half.")
                   + mark_line(730.0, "The largest site covers 2,400 m", b"(2)", " of floor area.")
                   + mark_line(715.0, "Costs were stated before tax", b"<0015>", " as in earlier reports.")
                   + show("F1", 11.0, _LEFT, 700.0, b"(%s)" % _FILLER.encode()) + b"72 160 144 0.5 re f\n"
                   + show("F1", 5.4, _LEFT, 151.15, b"(1)") + show("F1", 9.0, _LEFT + 3.0, 148.0, b"( Source of the ratio.)")
                   + show("F1", 5.4, _LEFT, 140.35, b"(2)") + show("F1", 9.0, _LEFT + 3.0, 137.2, b"( Corporate tax only.)"))
        pdf = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R /F2 6 0 R >> >> >>",
            4: b"<< /Length %d >>\nstream\n%sendstream" % (len(content), content),
            5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            6: b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+Arial /Encoding /Identity-H /DescendantFonts [7 0 R] >>",
            7: b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+Arial /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) "
               b"/Supplement 0 >> /DW 556 >>",
        })

        text = convert(pdf, "a.pdf").text

        assert "[^" not in text
        assert "2,400 m^2 of floor area." in text

    def test_caret_typed_in_the_text(self):
        """A typed '^1' (a spreadsheet formula) looks the same as a raised 1 in the line text."""
        text = _text({"body": [["The formula =A1^1 is shown."], [_FILLER]], "notes": [[("^", "1"), " Source of the formula."]]})

        assert "[^" not in text
        assert "The formula =A1^1 is shown." in text

    def test_page_of_many_short_rules(self):
        """A form of underlined blanks is not searched rule by rule (`_MAX_CANDIDATE_RULES`)."""
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        _draw_page(pdf, _UNLINKED_BODY, [[("^", "1"), " Source."]])
        for index in range(60):
            pdf.rect(300 + (index % 3) * 70, 700 - (index // 3) * 20, 60, 0.5, stroke=0, fill=1)
        pdf.showPage()
        pdf.save()

        self._assert_unlinked(convert(buffer.getvalue(), "a.pdf").text)


