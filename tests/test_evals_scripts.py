"""The references of the script twins (`evals/fetch_script_texts.py`) and the direction their twins are laid out in
(`evals/make_twins.py`)."""

import zipfile

from evals.fetch_script_texts import LANGUAGES, REVISION, SOURCE, _start, reference
from evals.make_twins import _direction, _tables_from_the_right


_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
<udhr iso639-3='eng' xml:lang='en' key='eng' n='English' dir='ltr' xmlns="http://efele.net/udhr">
   <title>Universal Declaration   of Human Rights</title>
   <note><para>A note that is no part of the text.</para></note>
   <preamble>
      <title>Preamble</title>
      <para>Whereas recognition of the inherent dignity
         is the foundation of freedom,</para>
   </preamble>
   <article number="1">
      <title>Article 1</title>
      <para>All human beings are born free.</para>
   </article>
   <article number="2">
      <title>Article 2</title>
      <orderedlist>
         <listitem><para>Everyone is entitled to all the rights.</para></listitem>
         <listitem><para>No distinction shall be made.</para></listitem>
      </orderedlist>
   </article>
</udhr>"""


class TestReference:
    def test_a_text_is_its_title_its_headings_its_paragraphs_and_its_lists(self):
        assert reference(_TEXT.encode()) == (
            "# Universal Declaration of Human Rights\n\n## Preamble\n\n"
            "Whereas recognition of the inherent dignity is the foundation of freedom,\n\n"
            "## Article 1\n\nAll human beings are born free.\n\n"
            "## Article 2\n\n1. Everyone is entitled to all the rights.\n2. No distinction shall be made.\n\n"
            "| 1 | Article 1 | All human beings are |\n| --- | --- | --- |\n| 2 | Article 2 | Everyone is entitled to |\n")

    def test_a_reference_stops_after_twelve_articles(self):
        articles = "".join(f'<article number="{number}"><title>Article {number}</title><para>Text {number}.</para></article>'
                           for number in range(1, 31))
        text = f'<udhr xmlns="http://efele.net/udhr"><title>T</title>{articles}</udhr>'

        written = reference(text.encode())

        assert "## Article 12" in written
        assert "## Article 13" not in written

    def test_the_start_of_an_article_is_cut_between_two_letters(self):
        """A text without spaces is one 'word': it is cut after thirty characters, and not between a letter and the mark
        set on it."""
        assert _start("All human beings are born free and equal") == "All human beings are"
        assert _start("人" * 50) == "人" * 30
        marked = "ก" * 29 + "กิ" + "ก" * 10

        assert _start(marked) == "ก" * 29 + "กิ"

    def test_the_texts_are_read_at_one_revision(self):
        """The list is the same on every run only when the revision is: a branch name would follow the project."""
        assert len(REVISION) == 40 and all(letter in "0123456789abcdef" for letter in REVISION)
        assert SOURCE.format(code=LANGUAGES[0]).startswith(f"https://raw.githubusercontent.com/eric-muller/udhr/{REVISION}/")
        assert len(set(LANGUAGES)) == len(LANGUAGES) == 19


class TestTablesFromTheRight:
    def test_the_tables_of_a_document_are_laid_out_from_the_right(self, tmp_path):
        document = tmp_path / "a.docx"
        with zipfile.ZipFile(document, "w") as package:
            package.writestr("word/document.xml", "<w:tbl><w:tblPr><w:tblW/></w:tblPr></w:tbl><w:tbl><w:tblPr></w:tblPr></w:tbl>")
            package.writestr("word/styles.xml", "<w:styles/>")

        _tables_from_the_right(document)
        _tables_from_the_right(document)  # a second time changes nothing

        with zipfile.ZipFile(document) as package:
            assert package.read("word/document.xml").count(b"<w:tblPr><w:bidiVisual />") == 2
            assert package.read("word/styles.xml") == b"<w:styles/>"


class TestDirection:
    def test_a_text_in_hebrew_or_arabic_letters_is_laid_out_from_right_to_left(self, tmp_path):
        for name, text in (("he.md", "# שלום עולם\n\nטקסט עם Word אחת."), ("ar.md", "# مرحبا\n\nنص عربي 2026.")):
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")

            assert _direction(path) == ["--metadata", "dir=rtl"]

    def test_any_other_text_is_left_to_the_default(self, tmp_path):
        for name, text in (("en.md", "# Hello\n\nOne word of Hebrew: שלום."), ("ko.md", "# 제목\n\n본문입니다."), ("empty.md", "")):
            path = tmp_path / name
            path.write_text(text, encoding="utf-8")

            assert _direction(path) == []
