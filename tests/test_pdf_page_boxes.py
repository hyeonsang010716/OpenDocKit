"""The various forms of PDF page boxes (MediaBox, CropBox)."""

import pytest

from opendockit import convert
from tests.test_pdf import _raw_pdf


_BODY = (b"BT /F1 12 Tf 50 780 Td (Line one at top of page) Tj ET\n"
         b"BT /F1 12 Tf 50 400 Td (Middle line of the page) Tj ET\n"
         b"BT /F1 12 Tf 50 60 Td (Bottom line near footer) Tj ET\n")


def _one_page(boxes: bytes) -> bytes:
    font = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: b"<< /Type /Page /Parent 2 0 R " + boxes + b" /Contents 4 0 R " + font + b" >>",
                     4: b"<< /Length %d >>\nstream\n" % len(_BODY) + _BODY + b"\nendstream"})


class TestReversedPageBoxes:
    """The specification lets a box be written with any two opposite corners. pdfplumber normalizes the page size, but
    pdfminer shifted the characters by the bottom edge as written (842), so every character was judged to be off the page
    and removed ('No text was extracted.')."""

    @pytest.mark.parametrize("boxes", [b"/MediaBox [0 842 595 0]", b"/MediaBox [595 842 0 0] /CropBox [0 842 595 0]"])
    def test_text_on_a_page_whose_box_corners_are_given_in_reverse_is_read(self, boxes):
        result = convert(_one_page(boxes), "a.pdf")

        assert result.sections[0].text == ("Line one at top of page\n\nMiddle line of the page\n\nBottom line near footer")
        assert not result.warnings
