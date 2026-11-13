"""Defects found by an audit of the DOCX, PPTX and ODF extractors with hand-built hostile documents: one odd structure that
rejected the whole document, values that came out wrong, and work that was repeated for every reference to the same part."""

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import _docx_with_body


_SUPERSCRIPT = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
_ENCLOSED_21 = r"<w:instrText> eq \o\ac(○,21) </w:instrText>"


def _field_char(kind: str) -> str:
    return f'<w:fldChar w:fldCharType="{kind}"/>'


def _docx(body: str, options: ConvertOptions | None = None) -> str:
    return convert(_docx_with_body(body), "sample.docx", options).text


class TestDocxFieldStartedInsideASwappedRun:
    """A complex field remembers the index of the piece where its result starts. A superscript run and ruby swap their pieces for
    one piece afterwards, so the index of a field that began inside pointed past the end (IndexError — the whole document was
    rejected as corrupted) or at another piece (the wrong 'this field has no result' verdict)."""

    def test_field_begun_in_a_superscript_run_and_ended_in_the_next_run_is_read(self):
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}<w:instrText> REF a </w:instrText>"
                     f"{_field_char('separate')}</w:r><w:r>{_field_char('end')}</w:r><w:r><w:t> tail</w:t></w:r></w:p>")

        assert "xy" in text
        assert "tail" in text

    def test_field_begun_in_a_ruby_annotation_and_ended_after_it_is_read(self):
        text = _docx(f"<w:p><w:r><w:ruby><w:rt><w:r><w:t>a</w:t><w:t>b</w:t>{_field_char('begin')}{_field_char('separate')}</w:r>"
                     f"</w:rt><w:rubyBase><w:r><w:t>c</w:t></w:r></w:rubyBase></w:ruby></w:r><w:r>{_field_char('end')}</w:r>"
                     "<w:r><w:t> tail</w:t></w:r></w:p>")

        assert text == "c(ab) tail"

    def test_result_text_inside_the_superscript_run_still_counts_as_the_result(self):
        """The field has the result 'R', so its instruction (an EQ field that would draw ㉑) is not written."""
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t>{_field_char('begin')}{_ENCLOSED_21}{_field_char('separate')}"
                     f"<w:t>R</w:t></w:r><w:r>{_field_char('end')}</w:r></w:p>")

        assert "㉑" not in text
        assert "xR" in text

    def test_text_before_the_field_in_the_same_superscript_run_is_not_its_result(self):
        """The field has no result of its own, so the EQ instruction is read (Word 'Enclose Characters')."""
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}{_ENCLOSED_21}"
                     f"{_field_char('separate')}</w:r><w:r>{_field_char('end')}</w:r></w:p>")

        assert "㉑" in text

    def test_text_left_out_of_a_field_that_never_ends_stays_after_the_superscript_run(self):
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}<w:t>h</w:t></w:r>"
                     "<w:r><w:t>z</w:t></w:r></w:p>")

        assert text.index("h") < text.index("z")
