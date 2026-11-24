"""A `#` or a `>` typed at the start of a line of a DOCX paragraph is text (`markdown.escape_block_marks`), as in a PDF, a
Hangul document and a slide.

Of 300 Word documents of an open repository of research, 5 hold such a line: the comment of a command or of code
(`# Point to your OLS config`, `# Load the dataset`), which came out as a heading, "more than" (`> 21 days`), which came
out as a quote, and "number of" (`# Peaks Selected`).
"""

from io import BytesIO

from docx import Document

from opendockit import ConvertOptions, convert


def _document(build) -> bytes:
    document = Document()
    build(document)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


def _paragraphs(*texts: str) -> bytes:
    return _document(lambda document: [document.add_paragraph(text) for text in texts])


def _text(data: bytes, **options) -> str:
    return convert(data, "a.docx", ConvertOptions(**options) if options else None).text


class TestTypedMarksInTheBody:
    def test_a_mark_at_the_start_of_a_paragraph_is_text(self):
        text = _text(_paragraphs("본문", "# Point to your OLS config", "> 21 days", ">> twice", "## two"))

        assert text == "본문\n\n\\# Point to your OLS config\n\n\\> 21 days\n\n\\>> twice\n\n\\## two"

    def test_what_is_no_mark_stays_as_typed(self):
        text = _text(_paragraphs("#hashtag", "####### seven", "a # b > c", "C#", "$x$", "<tag>"))

        assert text == "#hashtag\n\n####### seven\n\na # b > c\n\nC#\n\n$x$\n\n<tag>"

    def test_a_typed_list_stays_a_list(self):
        text = _text(_paragraphs("본문", "- 첫째", "1. 하나"))

        assert "\\" not in text
        assert "- 첫째" in text and "1. 하나" in text

    def test_a_mark_after_a_line_break_inside_a_paragraph_is_text(self):
        def build(document):
            run = document.add_paragraph().add_run("명령")
            run.add_break()
            run.add_text("# 주석")
            run.add_break()
            run.add_text("> 결과")

        assert _text(_document(build)) == "명령\n\\# 주석\n\\> 결과"

    def test_a_mark_at_the_start_of_a_list_item_is_text(self):
        """After the `- ` of an item a `#` is a heading and a `>` a quote all the same."""
        def build(document):
            document.add_paragraph("> 21 days", style="List Bullet")
            document.add_paragraph("# Peaks selected", style="List Bullet")

        assert _text(_document(build)) == "- \\> 21 days\n- \\# Peaks selected"

    def test_the_text_of_a_heading_is_not_escaped(self):
        """After the mark of the heading the reader writes, a `#` starts no line."""
        assert _text(_document(lambda document: document.add_heading("# of samples", level=1))) == "# # of samples"

    def test_a_mark_in_a_cell_of_a_table_is_as_typed(self):
        """A row of a table is one line that starts with `|`: nothing in a cell starts a line."""
        def build(document):
            table = document.add_table(rows=2, cols=2)
            for cell, text in zip(table._cells, ("# 건수", "> 50", "3", "예"), strict=True):
                cell.text = text

        assert _text(_document(build)) == "| # 건수 | > 50 |\n| --- | --- |\n| 3 | 예 |"

    def test_a_mark_in_bold_stands_behind_the_marks_of_bold(self):
        def build(document):
            paragraph = document.add_paragraph()
            paragraph.add_run("> 750 mln").bold = True
            paragraph.add_run(" in total")

        data = _document(build)

        assert _text(data, keep_emphasis=True) == "**> 750 mln** in total"
        assert _text(data, keep_emphasis=False) == "\\> 750 mln in total"

    def test_a_paragraph_bold_as_a_whole_is_escaped(self):
        """Bold over a whole paragraph is how it is styled and is left unmarked, so the mark starts the line."""
        data = _document(lambda document: setattr(document.add_paragraph().add_run("# all bold"), "bold", True))

        assert _text(data, keep_emphasis=True) == "\\# all bold"

    def test_a_mark_in_a_header_is_text_too(self):
        def build(document):
            document.sections[0].header.paragraphs[0].text = "# of pages"
            document.add_paragraph("본문")

        assert _text(_document(build), include_headers_footers=True) == "\\# of pages\n\n본문"
