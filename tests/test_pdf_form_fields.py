"""PDF form (AcroForm) values: groups of checkboxes sharing one name, radios whose values are given by `/Opt`."""


import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _korean_pdf, _raw_pdf
from tests.timing import fastest


_APPEARANCE = b"<< /Length 0 /Type /XObject /Subtype /Form /BBox [0 0 12 12] >>\nstream\n\nendstream"


def _form_pdf(fields: dict[int, bytes], widgets: dict[int, bytes]) -> bytes:
    """One-page form PDF holding `fields` (field dictionaries) and `widgets` (widget annotations) as objects numbered from 6.
    The appearance stream is object 5."""
    content = b"BT /F1 12 Tf 50 780 Td (Consent and grade) Tj ET"
    page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
            b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Annots [%s] >>"
            % b" ".join(b"%d 0 R" % number for number in widgets))
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [%s] >> >>"
                        % b" ".join(b"%d 0 R" % number for number in fields),
                     2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                     4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
                     5: _APPEARANCE, **fields, **widgets})


def _widget(parent: int, x: int, state: bytes, on: bytes) -> bytes:
    return (b"<< /Type /Annot /Subtype /Widget /Parent %d 0 R /Rect [%d 775 %d 787] /AS /%s /AP << /N << /%s 5 0 R /Off 5 0 R >> >> >>"
            % (parent, x, x + 12, state, on))


class TestGroupedFormFields:
    @pytest.mark.timing
    def test_a_field_of_very_many_buttons_takes_linear_time(self):
        """The position of a checkbox among the `/Kids` of the field it shares with the others was searched for each checkbox: a
        field of 20,000 on one page took 78 seconds in a 1.4 MB file, past any time budget."""
        def form(count: int) -> bytes:
            kids = range(7, 7 + count)
            return _form_pdf({6: b"<< /FT /Btn /T (grade) /Kids [%s] >>" % b" ".join(b"%d 0 R" % kid for kid in kids)},
                             {kid: _widget(6, 100, b"On" if kid == 7 + count // 2 else b"Off", b"On") for kid in kids})

        small, large = form(1_500), form(6_000)
        assert convert(large, "a.pdf").text.endswith("grade: On")

        assert fastest(lambda: convert(large, "a.pdf")) < 8 * fastest(lambda: convert(small, "a.pdf")) + 0.5

    @pytest.mark.timing
    def test_a_large_field_on_many_pages_takes_linear_time(self):
        """A field of many widgets whose `/Kids` every page names: found once per page, the positions of a field of 200,000
        widgets shared by 1,000 pages were looked up 1,000 times, 119 seconds; they are found once per document."""
        def form(count: int) -> bytes:
            content = b"BT /F1 12 Tf 50 780 Td (Form page) Tj ET"
            pages = range(100, 100 + count)
            objects = {1: b"<< /Type /Catalog /Pages 2 0 R /AcroForm << /Fields [6 0 R] >> >>",
                       2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % page for page in pages), count),
                       4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
                       5: _APPEARANCE,
                       6: b"<< /FT /Btn /T (agree) /Kids [7 0 R %s] >>" % b" ".join(b"8 0 R" for _ in range(100 * count)),
                       7: _widget(6, 100, b"On", b"On"), 8: _widget(6, 200, b"Off", b"On")}
            for page in pages:
                objects[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << "
                                 b"/F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Annots [7 0 R] >>")
            return _raw_pdf(objects)

        small, large = form(100), form(400)
        assert convert(large, "a.pdf").sections[-1].text.endswith("agree: On")

        assert fastest(lambda: convert(large, "a.pdf")) < 8 * fastest(lambda: convert(small, "a.pdf")) + 0.5

    def test_checkboxes_sharing_one_name_give_the_chosen_export_value(self):
        """Two checkboxes sharing one name ('동의함' agree · '동의하지 않음' disagree) came out as 'consent: ☐', 'consent: ☑',
        losing which one had been chosen."""
        data = _form_pdf({6: b"<< /FT /Btn /T (consent) /V /Disagree /Kids [7 0 R 8 0 R] >>"},
                         {7: _widget(6, 110, b"Off", b"Agree"), 8: _widget(6, 190, b"Disagree", b"Disagree")})

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("consent: Disagree")
        assert "☐" not in text and "☑" not in text

    def test_radio_whose_states_are_indexes_into_its_options_gives_the_option(self):
        """A radio with `/Opt` names its states by index (/0, /1), so it became 'grade: 1'. The index is the order of the
        widgets in `/Kids`."""
        data = _form_pdf({6: b"<< /FT /Btn /Ff 49152 /T (grade) /V /1 /Opt [(Excellent) (Good) (Poor)] /Kids [7 0 R 8 0 R 9 0 R] >>"},
                         {7: _widget(6, 100, b"Off", b"0"), 8: _widget(6, 130, b"1", b"1"), 9: _widget(6, 160, b"Off", b"2")})

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("grade: Good")

    def test_export_value_written_as_korean_bytes_in_the_state_name_is_decoded(self):
        """Some generators write the state name as EUC-KR bytes (`/#B5#BF#C0#C7`). Read as Latin characters it became 'µ¿ÀÇ'."""
        data = _form_pdf({6: b"<< /FT /Btn /T (consent) /Kids [7 0 R 8 0 R] >>"},
                         {7: _widget(6, 110, b"Off", b"Agree"), 8: _widget(6, 190, b"#B5#BF#C0#C7", b"#B5#BF#C0#C7")})

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("consent: 동의")


class TestHiddenFormFields:
    """A field whose widget is not shown on screen (Hidden or NoView in the annotation's `/F`) holds a calculated or internal
    value ('SECRET-CALC-123'); it came out at the end of the page as if it had been typed there."""

    @staticmethod
    def _text_field(name: bytes, value: bytes, y: int, flags: bytes = b"") -> bytes:
        return b"<< /Type /Annot /Subtype /Widget /FT /Tx /T (%s) /V (%s) /Rect [100 %d 250 %d] %s >>" % (
            name, value, y, y + 16, flags)

    @pytest.mark.parametrize("flags", [b"/F 2", b"/F 6", b"/F 32", b"/F 36"])  # Hidden, Hidden with Print, NoView, NoView with Print
    def test_field_whose_widget_is_not_shown_is_left_out(self, flags):
        fields = {6: self._text_field(b"name", b"Jane Doe", 700, b"/F 4"), 7: self._text_field(b"internal", b"CALC-123", 670, flags)}
        data = _form_pdf(fields, fields)  # each field is its own widget

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("name: Jane Doe")
        assert "CALC-123" not in text

    def test_flags_of_the_widget_under_a_field_decide(self):
        """`/F` belongs to the annotation, the widget in the page's `/Annots`, and is not inherited from the field above it."""
        data = _form_pdf({6: b"<< /FT /Tx /T (internal) /V (CALC-123) /Kids [8 0 R] >>",
                          7: b"<< /FT /Tx /T (shown) /V (Visible) /F 2 /Kids [9 0 R] >>"},
                         {8: b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /Rect [100 700 250 716] /F 2 >>",
                          9: b"<< /Type /Annot /Subtype /Widget /Parent 7 0 R /Rect [100 670 250 686] /F 4 >>"})

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("shown: Visible")
        assert "CALC-123" not in text


class TestChoiceFieldOptions:
    """The `/V` of a choice field (`/Ch`) is the export value, so unless it is mapped through the [export value, display text]
    pairs of `/Opt` the result was 'Region: 02'."""

    @staticmethod
    def _choice(field: bytes) -> bytes:
        widget = b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /Rect [100 770 250 788] >>"
        return _form_pdf({6: field}, {7: widget})

    def test_selected_export_value_is_shown_as_its_display_text(self):
        data = self._choice(b"<< /FT /Ch /Ff 131072 /T (region) /TU (Region) /V (02) "
                            b"/Opt [[(01) (Seoul HQ)] [(02) (Busan Branch)] [(03) (Daejeon Lab)]] /Kids [7 0 R] >>")

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("Region: Busan Branch")

    def test_several_selected_values_and_plain_options_are_kept(self):
        data = self._choice(b"<< /FT /Ch /T (lang) /V [(ko) (Other)] /Opt [[(ko) (Korean)] [(en) (English)] (Other)] /Kids [7 0 R] >>")

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("lang: Korean, Other")


def _free_text_pdf(annotations: list[bytes]) -> bytes:
    """PDF whose page draws two labels ('Name:', 'Phone:') with underlines and carries FreeText annotations (`annotations`,
    objects from 9). The appearance stream is object 5."""
    content = (b"BT /F1 14 Tf 72 740 Td (Application Form) Tj ET BT /F1 11 Tf 72 700 Td (Name:) Tj ET 120 698 m 400 698 l S "
               b"BT /F1 11 Tf 72 670 Td (Phone:) Tj ET 120 668 m 400 668 l S")
    page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
            b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> /Annots [%s] >>"
            % b" ".join(b"%d 0 R" % (9 + index) for index in range(len(annotations))))
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                     4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream", 5: _APPEARANCE,
                     **{9 + index: annotation for index, annotation in enumerate(annotations)}})


def _free_text(rect: bytes, text: bytes, flags: int = 4) -> bytes:
    return b"<< /Type /Annot /Subtype /FreeText /Rect [%s] /Contents (%s) /DA (/Helv 11 Tf 0 g) /AP << /N 5 0 R >> /F %d >>" % (
        rect, text, flags)


class TestFreeTextAnnotations:
    """Text typed with a viewer's 'add text' tool (Acrobat Fill & Sign, macOS Preview) is a FreeText annotation and is not in
    the page content, so only the 'Name:' label remained and the typed value was dropped without a warning."""

    def test_typed_text_is_appended_with_the_label_on_its_left(self):
        data = _free_text_pdf([_free_text(b"125 699 325 715", b"Hong Gildong"), _free_text(b"125 669 325 685", b"010-1234-5678")])

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("Name: Hong Gildong\nPhone: 010-1234-5678")

    def test_typed_text_without_a_label_beside_it_is_appended_as_it_is(self):
        text = convert(_free_text_pdf([_free_text(b"300 400 500 416", b"Approved")]), "a.pdf").sections[0].text

        assert text.endswith("Approved")

    @pytest.mark.parametrize("flags", [2, 32, 36])  # Hidden, NoView, NoView with Print
    def test_hidden_annotation_is_not_read(self, flags):
        text = convert(_free_text_pdf([_free_text(b"125 699 325 715", b"Secret", flags=flags)]), "a.pdf").sections[0].text

        assert "Secret" not in text


class TestFieldNames:
    """`/T` and `/TU` are not inherited. The `/TU` of a field that only groups other fields was taken for each field under it, so
    every value got the same name ('Applicant details: Kim', 'Applicant details: 010-1234')."""

    @staticmethod
    def _text_widget(parent: int, y: int, entries: bytes) -> bytes:
        return b"<< /Type /Annot /Subtype /Widget /Parent %d 0 R /FT /Tx %s /Rect [72 %d 300 %d] >>" % (parent, entries, y, y + 20)

    def test_tooltip_of_a_group_does_not_name_the_fields_under_it(self):
        data = _form_pdf({6: b"<< /T (applicant) /TU (Applicant details) /Kids [7 0 R 8 0 R] >>"},
                         {7: self._text_widget(6, 600, b"/T (name) /V (Kim)"),
                          8: self._text_widget(6, 570, b"/T (phone) /TU (Phone number) /V (010-1234)")})

        text = convert(data, "a.pdf").sections[0].text

        assert text.endswith("name: Kim\nPhone number: 010-1234")
        assert "Applicant details" not in text

    def test_widget_without_a_name_takes_the_tooltip_of_its_field(self):
        data = _form_pdf({6: b"<< /FT /Tx /T (name) /TU (Full name) /V (Kim) /Kids [7 0 R] >>"},
                         {7: b"<< /Type /Annot /Subtype /Widget /Parent 6 0 R /Rect [72 600 300 620] >>"})

        assert convert(data, "a.pdf").sections[0].text.endswith("Full name: Kim")


def _filled_form(pdf, index):
    pdf.drawString(70, 760, "Application form")
    pdf.acroForm.textfield(name="applicant", tooltip="Applicant name", value="Jane Doe", x=180, y=690, width=200, height=20,
                           fontName="Helvetica")
    pdf.acroForm.checkbox(name="agree", tooltip="Agree", checked=True, x=180, y=655, size=14)


def _past_the_time_budget(monkeypatch):
    return ConvertOptions(pdf_time_budget=0.0)


def _heavy_content(monkeypatch):
    from opendockit.extractors import pdf as pdf_module

    monkeypatch.setattr(pdf_module, "_MAX_READ_CONTENT_TOKENS", 0)
    return ConvertOptions()


def _crowded_with_characters(monkeypatch):
    from opendockit.extractors import pdf as pdf_module

    monkeypatch.setattr(pdf_module, "_MAX_PAGE_CHARS", 0)
    return ConvertOptions()


def _failed_by_pdfminer(monkeypatch):
    from pdfplumber.utils.exceptions import PdfminerException

    from opendockit.extractors import pdf as pdf_module

    def read(source, page, placement_fit):
        raise PdfminerException(ValueError("broken font stream"))

    monkeypatch.setattr(pdf_module, "_read_page_from", read)
    return ConvertOptions()


class TestFormValuesOnSimpleExtraction:
    """A page read with pdfium's simple extraction lost the values of its form fields: they are in the annotations, which only
    the path for a page pdfminer reads without characters copied over. Past the time budget, on a page with too much content
    or too many characters and on a page pdfminer failed, 'Jane Doe' was dropped without a warning."""

    @pytest.mark.parametrize("path", [_past_the_time_budget, _heavy_content, _crowded_with_characters, _failed_by_pdfminer])
    def test_values_entered_in_the_form_are_kept(self, monkeypatch, path):
        options = path(monkeypatch)

        result = convert(_korean_pdf(_filled_form, pages=2), "a.pdf", options)

        assert result.warnings, "the page was not read with simple extraction"
        for section in result.sections:
            assert section.text.endswith("Applicant name: Jane Doe\nAgree: ☑")

    def test_values_past_the_annotation_bound_are_not_read(self, monkeypatch):
        """Every annotation walked is time the time budget does not bound: 2,000 pages sharing one array of 20,000 widgets took
        127 seconds past a budget of 0. Past the bound on the annotations of such pages, a page's values are left out and the
        page is named."""
        from opendockit.extractors import pdf as pdf_module

        monkeypatch.setattr(pdf_module, "_MAX_SIMPLE_FORM_ANNOTATIONS", 3)   # the two fields of the first page fit, not the next
        result = convert(_korean_pdf(_filled_form, pages=2), "a.pdf", ConvertOptions(pdf_time_budget=0.0))

        assert result.sections[0].text.endswith("Applicant name: Jane Doe\nAgree: ☑")
        assert "Jane Doe" not in result.sections[1].text
        assert ("The form field values of 1 PDF page(s) (2) read with simple extraction were not read, as the document has "
                "too many annotations.") in result.warnings
