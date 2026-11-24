"""A `#` or a `>` typed at the start of a line of a slide is text (`markdown.escape_block_marks`), as in a PDF and a Hangul
document.

On a slide a `>` at the start of a line is most often "more than" (`> 750 mln`, `> 21 days`), which Markdown reads as a
quote, and a `#` the comment of a command or "number of" (`# macOS, Linux, WSL`, `# Peaks selected`), which stood as a
heading above the title of its own slide. Of 108 presentations of an open repository of research, 7 hold such a line.
"""

from io import BytesIO

from pptx import Presentation
from pptx.util import Inches

from opendockit import ConvertOptions, convert


def _deck(body: list[str] | None = None, boxes: list[list[str]] | None = None, *, title: str = "제목", notes: str = "",
          table: list[list[str]] | None = None) -> bytes:
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[1])
    slide.shapes.title.text = title
    frame = slide.placeholders[1].text_frame
    for index, text in enumerate(body or ["본문"]):
        (frame.paragraphs[0] if index == 0 else frame.add_paragraph()).text = text
    for row, lines in enumerate(boxes or []):
        box = slide.shapes.add_textbox(Inches(1), Inches(4 + row), Inches(6), Inches(1)).text_frame
        for index, text in enumerate(lines):
            (box.paragraphs[0] if index == 0 else box.add_paragraph()).text = text
    if table:
        shape = slide.shapes.add_table(len(table), len(table[0]), Inches(1), Inches(6), Inches(6), Inches(1))
        for row, cells in enumerate(table):
            for column, text in enumerate(cells):
                shape.table.cell(row, column).text = text
    if notes:
        slide.notes_slide.notes_text_frame.text = notes
    stream = BytesIO()
    presentation.save(stream)
    return stream.getvalue()


def _text(data: bytes, **options) -> str:
    return convert(data, "a.pptx", ConvertOptions(**options) if options else None).text


class TestTypedMarksOnASlide:
    def test_a_mark_at_the_start_of_a_line_of_a_text_box_is_text(self):
        text = _text(_deck(boxes=[["# macOS, Linux, WSL", "brew install uv", "> 750 mln"]]))

        assert text == "## Slide 1: 제목\n\n- 본문\n\n\\# macOS, Linux, WSL\nbrew install uv\n\\> 750 mln"

    def test_a_mark_at_the_start_of_an_item_of_the_body_is_text(self):
        """After the `- ` of an item a `#` is a heading and a `>` a quote all the same."""
        text = _text(_deck(body=["> 21 days", "# Peaks selected", ">50 participants"]))

        assert text == "## Slide 1: 제목\n\n- \\> 21 days\n- \\# Peaks selected\n- \\>50 participants"

    def test_a_mark_after_a_line_break_inside_a_paragraph_is_text(self):
        text = _text(_deck(boxes=[["명령\x0b# 주석\x0b> 결과"]]))

        assert text.endswith("명령\n\\# 주석\n\\> 결과")

    def test_what_is_no_mark_stays_as_typed(self):
        text = _text(_deck(boxes=[["#hashtag", "####### seven", "a # b > c", "C#", "- typed dash", "1. typed number"]]))

        assert "\\" not in text
        assert "#hashtag\n####### seven\na # b > c\nC#" in text

    def test_the_title_of_a_slide_is_as_typed(self):
        """It is written after the mark of the slide's heading, where a `#` starts no line."""
        assert _text(_deck(title="# of samples")) == "## Slide 1: # of samples\n\n- 본문"

    def test_a_mark_in_a_cell_of_a_table_is_as_typed(self):
        text = _text(_deck(table=[["# 건수", "> 50"], ["3", "예"]]))

        assert "| # 건수 | > 50 |" in text

    def test_a_mark_in_bold_is_text_too(self):
        """With emphasis kept, the marks of bold stand in front of it and it starts no line."""
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        frame = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(1)).text_frame
        run = frame.paragraphs[0].add_run()
        run.text, run.font.bold = "> 750 mln", True
        frame.paragraphs[0].add_run().text = " in total"
        stream = BytesIO()
        presentation.save(stream)

        assert _text(stream.getvalue(), keep_emphasis=True).endswith("**> 750 mln** in total")
        assert _text(stream.getvalue(), keep_emphasis=False).endswith("\\> 750 mln in total")


class TestALineThatStartsWithALetterIsNotCleanedToSeeWhetherItIsEmpty:
    def test_cleaning_removes_no_letter_and_no_digit(self):
        """What `_render_text_frame` relies on to keep such a line without cleaning it."""
        import sys

        from opendockit.markdown import clean_characters

        removed = [code for code in range(sys.maxunicode + 1)
                   if chr(code).isalnum() and not clean_characters(chr(code)).strip()]

        assert removed == []

    def test_a_line_of_characters_cleaning_removes_is_still_empty(self):
        text = _text(_deck(boxes=[["​­", "가​나", "​# 다"]]))

        assert text.endswith("- 본문\n\n가나\n# 다")
