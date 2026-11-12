"""PDF form (AcroForm) values: groups of checkboxes sharing one name, radios whose values are given by `/Opt`."""


from opendockit import convert
from tests.test_pdf import _raw_pdf


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

    def test_hidden_annotation_is_not_read(self):
        text = convert(_free_text_pdf([_free_text(b"125 699 325 715", b"Secret", flags=2)]), "a.pdf").sections[0].text

        assert "Secret" not in text
