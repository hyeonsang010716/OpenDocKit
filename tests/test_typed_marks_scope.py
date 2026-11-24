"""Where a `#` or a `>` typed at the start of a line is escaped, and where it is written as typed — on purpose
(`docs/decisions/extractors.md`).

It is escaped in PDF, HWP, HWPX, PPTX and DOCX (`test_pdf_headings.py::TestBodyTextMarks`, `test_hwp_typed_marks.py`,
`test_pptx_typed_marks.py`, `test_docx_typed_marks.py`): in the real documents of those formats that hold one, it is
the comment of a command, "more than" or "number of". It is not in ODT, RTF, HTML and EPUB: of the ODT and RTF files of
an open repository of research that hold one, half are a README or an article somebody typed as Markdown, whose
headings the rule would undo. Before changing a test here, count what real documents of the format hold.
"""

from html import escape

from opendockit import convert
from tests import builder
from tests.test_odf import _odt
from tests.test_rtf import _rtf


_TYPED = ("# Project overview", "## 1. File descriptions", "> 21 days")


class TestFormatsThatWriteTheMarksAsTyped:
    def test_odt(self):
        assert _odt("".join(f"<text:p>{escape(line)}</text:p>" for line in _TYPED)) == "\n\n".join(_TYPED)

    def test_rtf(self):
        assert convert(_rtf("".join(line + r"\par " for line in _TYPED)), "a.rtf").text == "\n\n".join(_TYPED)

    def test_html(self):
        page = "<html><body>" + "".join(f"<p>{escape(line)}</p>" for line in _TYPED) + "</body></html>"

        assert convert(page.encode(), "a.html").text == "\n\n".join(_TYPED)

    def test_text_and_markdown_files_are_markdown_already(self):
        assert convert("\n\n".join(_TYPED).encode(), "a.md").text == "\n\n".join(_TYPED)
        assert convert("\n\n".join(_TYPED).encode(), "a.txt").text == "\n\n".join(_TYPED)


class TestTypedTagsAreNotEscaped:
    def test_a_word_in_angle_brackets_is_written_as_typed(self):
        """The name of a key (`<F5>`, `<Tab>`) or a placeholder (`<filename>`) reads as an HTML tag to a program that draws
        the Markdown, and the word is gone from the page. The result is for a model to read, not to be drawn
        (`markdown.neutralize_table_tags`), so only the tags of a table are neutralized, in a cell."""
        body = "".join(f"<hp:p><hp:run><hp:t>{escape(line)}</hp:t></hp:run></hp:p>" for line in ("<F5> 키를 누른다", "<filename>.txt"))

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "<F5> 키를 누른다\n\n<filename>.txt"
