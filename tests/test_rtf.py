"""RTF → Markdown (`extractors/rtf.py`). Decisions are in docs/decisions/rtf.md.

Every document is RTF source written in the test. Korean text is written the way Korean writers store it: CP949 bytes as
`\\'hh` escapes (`_cp949`) or `\\uN` escapes with a fallback character (`_unicode`).
"""

from io import BytesIO
import time

from PIL import Image
import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, convert
from opendockit.extractors import rtf as rtf_module
from opendockit.extractors.rtf import extract_rtf


def _rtf(body: str, *, head: str = r"\ansi\ansicpg1252\deff0") -> bytes:
    return (r"{\rtf1" + head + "\n" + body + "}").encode("ascii")


def _cp949(text: str) -> str:
    """The text as `\\'hh` escapes of its CP949 bytes."""
    return "".join(f"\\'{byte:02x}" for byte in text.encode("cp949"))


def _unicode(text: str, fallback: str = "?") -> str:
    """The text as signed 16-bit `\\uN` escapes (surrogate pairs for characters outside the BMP), each with a fallback character."""
    units = text.encode("utf-16-le")
    values = (int.from_bytes(units[index:index + 2], "little", signed=True) for index in range(0, len(units), 2))
    return "".join(f"\\u{value}{fallback}" for value in values)


def _text(body: str, options: ConvertOptions | None = None, **kwargs: str) -> str:
    return convert(_rtf(body, **kwargs), "a.rtf", options).text


def _png(side: int = 80, color: tuple[int, int, int] = (200, 30, 30)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (side, side), color).save(buffer, "PNG")
    return buffer.getvalue()


def _row(cells: list[str], edges: list[int] | None = None, flags: dict[int, str] | None = None) -> str:
    """One table row: the definition (`\\trowd`, per cell its merge flags and `\\cellx` edge), then the cells."""
    edges = edges if edges is not None else [1000 * (index + 1) for index in range(len(cells))]
    definition = "".join(f"{(flags or {}).get(index, '')}\\cellx{edge}" for index, edge in enumerate(edges))
    return "\\trowd" + definition + "".join(f"\\pard\\intbl {cell}\\cell" for cell in cells) + "\\row\n"


_STYLES = (r"{\stylesheet{\s0 Normal;}{\s1\sbasedon0 heading 1;}{\s2\sbasedon0 Heading 2;}{\s3 " + _cp949("제목") + " 3;}"
           r"{\s4\sbasedon1 Chapter;}{\s5 Title;}{\*\cs10 Default Paragraph Font;}{\s6\outlinelevel3 Custom;}}")
_LISTS = (r"{\*\listtable"
          r"{\list\listtemplateid1{\listlevel\levelnfc23{\leveltext\'01\u-3913 ?;}}{\listlevel\levelnfc23{\leveltext\'01o;}}\listid11}"
          r"{\list\listtemplateid2{\listlevel\levelnfc0\levelstartat1{\leveltext\'02\'00.;}}"
          r"{\listlevel\levelnfc4\levelstartat1{\leveltext\'02\'01.;}}\listid22}"
          r"{\list{\listlevel\levelnfc255{\leveltext\'00;}}\listid33}}"
          r"{\*\listoverridetable{\listoverride\listid11\listoverridecount0\ls1}{\listoverride\listid22\listoverridecount0\ls2}"
          r"{\listoverride\listid33\listoverridecount0\ls3}}")


class TestParagraphsAndHeadings:
    def test_paragraphs_end_at_par_and_the_last_one_needs_none(self):
        """Two `\\par` paragraphs and trailing text without `\\par` — the last paragraph of a document has no terminator."""
        assert _text(r"First\par Second\par Third") == "First\n\nSecond\n\nThird"

    def test_line_breaks_in_the_source_are_not_text(self):
        """CR/LF in RTF source only wrap the file; a space before the break is text, the break is not."""
        assert _text("one \r\ntwo\r\nthree\\par") == "one twothree"

    def test_headings_come_from_style_names_in_english_and_korean(self):
        """Styles named 'heading 1' (Word), 'Heading 2' (LibreOffice), '제목 3' (Korean Word, CP949 escapes) and 'Title'; a style
        based on a heading style inherits its level; a style with its own outline level uses it."""
        body = (_STYLES + r"\pard\s5 Report\par\pard\s1 One\par\pard\s2 Two\par\pard\s3 " + _cp949("세 번째") + r"\par"
                r"\pard\s4 Derived\par\pard\s6 Outline\par\pard\s0 Body\par")
        assert _text(body, head=r"\ansi\ansicpg949") == "# Report\n\n# One\n\n## Two\n\n### 세 번째\n\n# Derived\n\n#### Outline\n\nBody"

    def test_outline_level_on_the_paragraph_makes_a_heading(self):
        """`\\outlinelevel0` is level 1; `\\outlinelevel9` means body text even on a heading style."""
        body = _STYLES + r"\pard\outlinelevel0 Top\par\pard\outlinelevel2 Third\par\pard\s1\outlinelevel9 Body\par"
        assert _text(body) == "# Top\n\n### Third\n\nBody"

    def test_paragraph_properties_last_until_pard(self):
        """A second paragraph without `\\pard` keeps the style of the first; `\\pard` drops it."""
        assert _text(_STYLES + r"\pard\s1 A\par B\par\pard C\par") == "# A\n\n# B\n\nC"

    def test_par_inside_a_group_uses_the_properties_set_outside_it(self):
        """Word closes the run group after `\\par` (`\\pard\\s1 {text\\par}`) and pandoc wraps the whole paragraph in a group
        (`{\\pard\\s2 text\\par}`): paragraph properties are not scoped to groups."""
        assert _text(_STYLES + r"\pard\s1 {\f0 One\par}{\pard\s2 Two\par}{\f0 Still two\par}") == "# One\n\n## Two\n\n## Still two"

    def test_long_paragraph_with_a_heading_style_is_body_text(self):
        """Same rule as DOCX/HWP: more than 120 characters is not a heading."""
        long = "word " * 40
        assert _text(_STYLES + rf"\pard\s1 {long}\par") == long.strip()

    def test_heading_text_has_no_emphasis_and_joins_its_lines(self):
        """Heading styles are bold in every writer; `**` inside `#` is noise. A manual line break in a title is a space."""
        assert _text(_STYLES + r"\pard\s1\b Annual\line Report\par") == "# Annual Report"

    def test_title_and_author_become_metadata_and_other_properties_do_not(self):
        """`\\info` holds title, author and others (subject, operator, comment); none of it is body text."""
        body = (r"{\info{\title " + _cp949("분기 보고서") + r"}{\author Kim}{\subject Hidden}{\operator Lee}{\*\company ACME}}Body\par")
        result = convert(_rtf(body, head=r"\ansi\ansicpg949"), "a.rtf")
        assert result.metadata == {"title": "분기 보고서", "author": "Kim"}
        assert result.text == "Body"


class TestCharacters:
    def test_cp949_double_byte_escapes_become_hangul(self):
        """`\\ansicpg949`: each Hangul syllable is two `\\'hh` escapes. Decoded one byte at a time they were '°¡' style noise."""
        assert _text(_cp949("한글 문서") + r"\par", head=r"\ansi\ansicpg949") == "한글 문서"

    def test_font_charset_selects_the_code_page_of_a_run(self):
        """A Western document (`\\ansicpg1252`) with a Korean font (`\\fcharset129`), a Japanese one (`\\fcharset128`), a Cyrillic
        one given by `\\cpg1251`: each run is decoded with the code page of its font."""
        fonts = r"{\fonttbl{\f0\fcharset0 Arial;}{\f1\fcharset129 Batang{\*\falt \'b9\'d9\'c5\'c1};}{\f2\fcharset128 Mincho;}{\f3\cpg1251 Cyr;}}"
        japanese = "".join(f"\\'{byte:02x}" for byte in "日本".encode("cp932"))
        body = fonts + r"\f0 caf\'e9 {\f1 " + _cp949("가나") + r"} {\f2 " + japanese + r"} {\f3 \'cf\'f0}\par"
        assert _text(body) == "café 가나 日本 Пр"

    def test_trail_byte_written_as_a_plain_letter_completes_the_character(self):
        """Some writers escape only the lead byte: '똠' is CP949 8C 63, stored as `\\'8cc`."""
        assert "똠".encode("cp949") == b"\x8cc"
        assert _text(r"\'8cc\par", head=r"\ansi\ansicpg949") == "똠"

    def test_ansi_charset_font_in_a_korean_document_reads_hangul(self):
        """Every font declared `\\fcharset0` in a `\\ansicpg949` document: Hangul bytes are still CP949. Bytes that are not valid
        CP949 ('é' before a space) are Western text."""
        fonts = r"{\fonttbl{\f0\fcharset0 Gulim;}}"
        assert _text(fonts + r"\f0 " + _cp949("안녕") + r"\par caf\'e9 au lait\par", head=r"\ansi\ansicpg949") == "안녕\n\ncafé au lait"

    def test_fallback_counted_in_characters_drops_the_whole_double_byte_character(self):
        """`\\uc1` followed by a two-byte CP949 fallback (writers that count `\\ucN` in characters): the byte that completes the
        dropped lead byte is dropped too — it came out as '가�'. A fallback counted in bytes (`\\uc2`), a one-byte fallback
        followed by real CP949 text, and a CP932 half-width katakana (one byte) are read as before."""
        head = r"\ansi\ansicpg949"
        assert _text(r"\uc1\u44032\'b0\'a1 end\par", head=head) == "가 end"
        assert _text(r"\uc2\u44032\'b0\'a1 end\par", head=head) == "가 end"
        assert _text(r"\uc1\u44032?" + _cp949("가") + r"\par", head=head) == "가가"
        assert _text(r"\uc1\u44032\'b0 x\par", head=head) == "가 x"
        assert _text(r"\uc1\u65393\'b1\'82\'a0\par", head=r"\ansi\ansicpg932") == "ｱあ"

    def test_fallback_written_as_raw_8bit_text_is_dropped_whole(self):
        """The same fallback as plain 8-bit text instead of `\\'hh` (the second byte in the same run of text, or in the next
        token), and a three-byte UTF-8 fallback after `\\uc1`: the whole character is dropped (it left '가�')."""
        def text(body: bytes, page: int = 949) -> str:
            """`BS` stands for a backslash."""
            return convert((b"{BSrtf1BSansiBSansicpg%d " % page + body + b"BSpar}").replace(b"BS", b"\\"), "a.rtf").text
        ga = "가".encode("cp949")
        assert text(b"BSuc1BSu44032" + ga + b" end") == "가 end"
        assert text(b"BSuc1BSu44032" + ga[:1] + b"BS'a1 end") == "가 end"
        assert text(b"BSuc2BSu44032" + ga + ga + b" end") == "가가 end"
        assert text(b"BSuc1BSu44032BS'eaBS'b0BS'80 x", page=65001) == "가 x"
        assert text(b"BSuc1BSu44032" + "가".encode() + b" x", page=65001) == "가 x"

    def test_code_page_that_is_not_ascii_compatible_is_ignored(self):
        """`\\ansicpg875` (EBCDIC Greek) turned a whole run with one 8-bit byte into noise ('hello' → 'ΪΧ%%?'), and a font
        with `\\cpg500` (EBCDIC) did the same. RTF is ASCII text, so such a code page is ignored and the document's is used.
        A real code page that only draws a symbol differently (CP864, Arabic: '%' is '٪') is still used."""
        assert _text(r"hello \'e9\par", head=r"\ansi\ansicpg875") == "hello é"
        assert _text(r"{\fonttbl{\f1\cpg500 F;}}\f1 caf\'e9\par") == "café"
        assert _text(r"{\fonttbl{\f1\cpg1026 F;}}\f1 caf\'e9\par") == "café"
        assert _text(r"50% \'e9\par", head=r"\ansi\ansicpg864") == "50٪ ﻯ"

    def test_unicode_escapes_cover_negative_values_and_surrogate_pairs(self):
        """'한' (U+D55C) is `\\u-10916`, '😀' is the surrogate pair `\\u-10179\\u-8704`; each escape is followed by one fallback
        character that is dropped. A lone surrogate half is dropped (it cannot be encoded)."""
        assert _unicode("한") == "\\u-10916?"
        assert _text(_unicode("한글 😀") + r" end \u-10179? x\par") == "한글 😀 end x"

    def test_uc_sets_how_many_fallback_characters_are_dropped(self):
        """`\\uc2` with a two-byte CP949 fallback (what Korean Word writes), `\\uc0` with none, and `\\uc` scoped to its group."""
        body = r"{\uc2\u-21504" + _cp949("가") + r"A}{\uc0\u44033 B}\u44034?C\par"
        assert _text(body, head=r"\ansi\ansicpg949") == "가A각B갂C"

    def test_fallback_skipping_stops_at_a_control_word_or_brace(self):
        """`\\uc1` followed directly by another escape or the end of the group: nothing after it is eaten."""
        assert _text(r"{\u44032}AB\u44033\tab CD\par") == "가AB각 CD"

    def test_special_characters(self):
        body = (r"a\tab b\line c\emdash d\endash e \lquote f\rquote  \ldblquote g\rdblquote  \bullet  h\~i\_j\-k \{x\} \\ \par")
        assert _text(body) == "a b\nc—d–e ‘f’ “g” • h i-jk {x} \\"

    def test_hidden_and_deleted_text_is_left_out(self):
        """`\\v` hidden text and `\\deleted` tracked deletions; `\\v0` shows text again."""
        assert _text(r"Keep {\v secret} {\deleted old}new \v gone\v0 back\par") == "Keep new back"


class TestSkippedDestinations:
    def test_tables_of_fonts_colors_and_ignorable_groups_are_not_text(self):
        """Font names, color values, `\\*` destinations (generator, bookmarks, theme data with nested groups) and index entries."""
        body = (r"{\fonttbl{\f0 Times New Roman;}}{\colortbl;\red0\green0\blue0;}{\*\generator Word 16;}"
                r"{\*\themedata 504b0304{\*\nested deep {deeper}}}A{\*\bkmkstart mark}B{\xe index entry}C{\*\unknowndest {x} y}\par")
        assert _text(body) == "ABC"

    def test_unknown_control_words_do_not_hide_their_text(self):
        """A control word nobody knows, without `\\*`, is formatting: the text of its group stays."""
        assert _text(r"{\brandnew7 kept} {\brandnew kept too}\par") == "kept kept too"

    def test_star_in_the_middle_of_a_group_does_not_hide_its_text(self):
        """StarWriter wrote character styles as `{\\i0\\b0\\*\\cs7\\cf0 text}`: the `\\*` is not the first thing in the group, so the
        group is not an ignorable destination and its text is body text (the whole document came out empty). At the start of a
        group, also after a line break, `\\*` still hides an unknown destination."""
        assert _text(r"{\loch\f2\i0\b0\*\cs7\cf0 The quick brown fox}\par") == "The quick brown fox"
        assert _text("A{\r\n\\*\\unknowndest hidden}B\\par") == "AB"
        # … and in the middle of a group in front of a word that is not a style reference, as before.
        assert _text(r"a{\lang1033\*\xmlopen\xmlns1{\factoidname place}}b\par") == "ab"

    def test_headers_and_footers_follow_the_option(self):
        """A header, a first-page header with the same text, and a footer. Off by default; when on, each text once, header before
        the body and footer after it."""
        body = r"{\header \pard Company\par}{\headerf \pard Company\par}{\footer \pard Page {\field{\*\fldinst PAGE}{\fldrslt 1}}\par}Body\par"
        assert _text(body) == "Body"
        assert _text(body, ConvertOptions(include_headers_footers=True)) == "Company\n\nBody\n\nPage 1"

    def test_footnotes_become_references_with_definitions_at_the_end(self):
        """Two footnotes, the second with two paragraphs and Korean text; the `\\chftn` marks are not text. Paragraph properties of
        the body survive the footnote (it has its own flow)."""
        body = (_STYLES + r"\pard\s1 Title{\super\chftn}{\footnote \pard\plain {\super\chftn} First note.}\par"
                r"\pard Text{\footnote \pard " + _cp949("둘째 주석") + r"\par more}.\par")
        text = _text(body, head=r"\ansi\ansicpg949")
        assert text == "# Title[^1]\n\nText[^2].\n\n[^1]: First note.\n[^2]: 둘째 주석 more"
        assert _text(body, ConvertOptions(include_footnotes=False), head=r"\ansi\ansicpg949") == "# Title\n\nText."

    def test_empty_footnote_leaves_no_reference(self):
        assert _text(r"Text{\footnote \pard {\super\chftn}}.\par") == "Text."

    def test_hyperlink_field_becomes_a_link_around_its_result(self):
        """`HYPERLINK "url"` with the instruction split over nested groups (as Word writes it) and a bold result. Other fields keep
        their result; a bookmark link and a file link keep only the text."""
        body = (r'See {\field{\*\fldinst{ HYPERLINK "https://example.com/a b(1)" }{\*\datafield 00d0c9ea}}{\fldrslt{\ul\b the site}}} and '
                r'{\field{\*\fldinst HYPERLINK \\l "chapter2"}{\fldrslt chapter 2}}, {\field{\*\fldinst HYPERLINK "file:///c:/x.doc"}{\fldrslt x}}, '
                r"{\field{\*\fldinst DATE}{\fldrslt 2026-01-14}}\par")
        assert _text(body) == "See [**the site**](https://example.com/a%20b%281%29) and chapter 2, x, 2026-01-14"
        assert _text(body, ConvertOptions(keep_links=False)) == "See **the site** and chapter 2, x, 2026-01-14"

    def test_mailto_link_and_whitespace_stay_outside_the_brackets(self):
        body = r'{\field{\*\fldinst HYPERLINK "mailto:a@example.com"}{\fldrslt  mail us }}now\par'
        assert _text(body) == "[mail us](mailto:a@example.com) now"

    def test_shape_text_is_read_once_and_its_properties_are_not(self):
        """A text box: `\\shpinst` holds property names/values and `\\shptxt`; `\\shprslt` is the copy for old readers."""
        body = (r"Before {\shp{\*\shpinst\shpleft0{\sp{\sn shapeType}{\sv 202}}{\shptxt \pard " + _cp949("글상자") + r"\par}}"
                r"{\shprslt{\*\do\dobxcolumn{\dptxbxtext\pard " + _cp949("글상자") + r"\par}}}} after\par")
        assert _text(body, head=r"\ansi\ansicpg949") == "글상자\n\nBefore after"


class TestEmphasis:
    def test_bold_and_italic_runs_are_marked_with_whitespace_outside(self):
        """`\\b` … `\\b0`, a group-scoped `\\i`, and both together; the space inside the bold run goes outside the markers."""
        body = r"plain \b bold \b0 next {\i italic} {\b\i both}\par"
        assert _text(body) == "plain **bold** next *italic* ***both***"

    def test_adjacent_runs_with_the_same_formatting_are_merged(self):
        """One bold phrase stored as three runs (as writers split text at spelling/language boundaries)."""
        assert _text(r"{\b one}{\b  two}{\b\f0  three} four\par") == "**one two three** four"

    def test_plain_resets_emphasis_and_whitespace_only_runs_get_no_markers(self):
        assert _text(r"\b a\plain  b{\b  }c\par") == "**a** b c"

    def test_emphasis_can_be_switched_off(self):
        assert _text(r"{\b bold} {\i italic}\par", ConvertOptions(keep_emphasis=False)) == "bold italic"

    def test_bold_korean_run(self):
        body = r"{\b " + _cp949("중요") + r"}: " + _cp949("내용") + r"\par"
        assert _text(body, head=r"\ansi\ansicpg949") == "**중요**: 내용"

    def test_paragraph_list_item_and_cell_that_are_bold_throughout_are_not_marked(self):
        """A paragraph, a list item and the header cells of a table, each bold (or italic) from the first to the last word: that
        is how the block is styled, not a stress inside it. DOCX writes the same content without marks (inline-formatting.md)."""
        assert _text(r"{\b This whole paragraph is bold.}\par {\i Whole italic line.}\par") == "This whole paragraph is bold.\n\nWhole italic line."
        assert _text(r"\ls1 {\b Whole}{\b  item}\par \ls1 Item with {\b a bold part}\par") == "- Whole item\n- Item with **a bold part**"
        table = _row([r"{\b Header A}", r"{\b Header B}"]) + _row(["plain", r"{\b " + _cp949("합계") + "}"])
        assert _text(table, head=r"\ansi\ansicpg949") == "| Header A | Header B |\n| --- | --- |\n| plain | 합계 |"

    def test_bold_on_part_of_a_paragraph_or_cell_is_marked(self):
        body = r"Some {\b partial bold} and {\i italic words} here.\par" + _row(["label", r"some {\b bold} text"])
        assert _text(body) == "Some **partial bold** and *italic words* here.\n\n| label | some **bold** text |\n| --- | --- |"

    def test_korean_word_before_its_particle_is_marked(self):
        """In Korean the particle is written onto the stressed word without a space: the span starts a word and stops before its
        ending, which is kept."""
        body = _cp949("분기 ") + r"{\b " + _cp949("매출") + "}" + _cp949("은 증가했다.") + r"\par"
        assert _text(body, head=r"\ansi\ansicpg949") == "분기 **매출**은 증가했다."

    def test_emphasis_inside_a_word_is_not_marked(self):
        """A digit in another weight and a decorated initial are styling of part of a word, not emphasis."""
        assert _text(r"1{\b 2},345 and {\b S}trength\par") == "12,345 and Strength"

    def test_line_break_closes_and_reopens_the_span_and_a_typed_bullet_stays_in_front(self):
        assert _text(r"a {\b b\line c} d\par") == "a **b**\n**c** d"
        assert _text(r"\pard\li360 {\b \bullet \tab bold} rest\par \pard\li360 {\b \endash \tab part} rest\par") == "- **bold** rest\n- **part** rest"

    def test_footnote_text_follows_the_same_rules(self):
        body = r"Text{\footnote \pard {\b whole bold note}} and{\footnote \pard see {\b this} note}.\par"
        assert _text(body) == "Text[^1] and[^2].\n\n[^1]: whole bold note\n[^2]: see **this** note"

    def test_emphasis_is_marked_line_by_line_inside_a_run(self):
        """One italic group holding a paragraph with a blank line in it (`\\line\\line`, as mail bodies converted from HTML have),
        between a plain tab before and after. The whole text is italic, so nothing is marked — the blank line inside the run
        must not turn it into a span over two blocks, which left the first lines marked and the last one not. When only the
        second line is italic, that line is marked."""
        assert _text(r"\tab {\i First line. \line\line Second line.}\tab\par") == "First line.\nSecond line."
        assert _text(r"First line. \line {\i Second\line third} line.\par") == "First line.\n*Second*\n*third* line."


def _link(address: str, text: str) -> str:
    return r'{\field{\*\fldinst HYPERLINK "' + address + r'"}{\fldrslt ' + text + "}}"


class TestLinks:
    """The link rules are the shared ones (`_inline.link_target`, `resolve_inline`; inline-formatting.md); these pin that the RTF
    reader goes through them."""

    def test_link_whose_text_is_its_address_stays_the_bare_address(self):
        body = "Visit " + _link("https://example.com/", "https://example.com") + " or " + _link("mailto:team@example.com", "team@example.com") + r"\par"
        assert _text(body) == "Visit https://example.com or team@example.com"

    def test_only_web_and_mail_addresses_become_links(self):
        body = " ".join((_link("javascript:alert(1)", "js"), _link("ftp://a.example/f", "ftp"), _link("/relative", "rel"),
                         _link("HTTPS://A.example/", "upper"))) + r"\par"
        assert _text(body) == "js ftp rel [upper](HTTPS://A.example/)"

    def test_address_and_text_cannot_close_the_link_or_split_a_table_row(self):
        """An address with `)`, a space and `|`, and link text that tries to end the link and start another one."""
        body = "x " + _link("https://a.example/x) [y](https://evil.example/|z", "here](https://evil.example) [x") + r"\par"
        assert _text(body) == r"x [here\](https://evil.example) \[x](https://a.example/x%29%20[y]%28https://evil.example/%7Cz)"

    def test_bookmark_is_appended_as_the_fragment(self):
        body = r'{\field{\*\fldinst HYPERLINK "https://example.com/doc" \\l "part2"}{\fldrslt part two}} end\par'
        assert _text(body) == "[part two](https://example.com/doc#part2) end"

    def test_link_in_a_heading_is_kept_and_emphasis_stops_at_the_edge_of_a_link(self):
        assert _text(_STYLES + r"\pard\s1 {\b Annual} " + _link("https://example.com/r", "report") + r"\par") == "# Annual [report](https://example.com/r)"
        body = r"{\b before} " + _link("https://example.com/", r"{\b link}") + r" after\par"
        assert _text(body) == "**before** [**link**](https://example.com/) after"

    def test_link_around_a_picture_keeps_the_picture_and_writes_no_link(self):
        body = "see " + _link("https://example.com/", r"{\pict\pngblip " + _png().hex() + "}") + r" end\par"
        result = convert(_rtf(body), "a.rtf")
        assert "](" not in result.text and "example.com" not in result.text
        assert len(result.images) == 1

    def test_control_characters_in_the_text_cannot_forge_a_link(self):
        """The shared resolver's temporary marks are control characters (U+000E–U+0014), which RTF text can hold (`\\'12`,
        `\\u19`). The reader hands over mark objects between the pieces instead, so such characters are only text (and are
        removed by cleaning). A real bold run in the same paragraph makes sure the resolver runs."""
        body = "a \\'12click\\'13https://evil.example\\'14 b \\'0efake\\'0f c \\u18 ?d\\u19 ?https://evil.example\\u20 ? {\\b real} e\\par"
        text = _text(body)
        assert "](" not in text and "**fake**" not in text
        assert text == "a clickhttps://evil.example b fake c dhttps://evil.example **real** e"


class TestLists:
    def test_bullet_and_numbered_lists_from_the_list_table(self):
        """`\\ls1` is a bullet list (number format 23), `\\ls2` a numbered one with a second level. The writer's own `\\listtext`
        copy of the number (with its own `\\pard`) is skipped; numbers are counted, sub-levels restart."""
        def item(ls: int, level: int, text: str) -> str:
            return rf"\pard{{\listtext\pard\plain 9.\tab}}\ilvl{level}\ls{ls} {text}\par" + "\n"

        body = (_LISTS + item(1, 0, "apple") + item(1, 1, "red") + item(1, 0, "pear") + r"\pard Between\par"
                + item(2, 0, "first") + item(2, 1, "sub a") + item(2, 1, "sub b") + item(2, 0, "second") + item(2, 1, "sub c"))
        assert _text(body) == ("- apple\n  - red\n- pear\n\nBetween\n\n"
                               "1. first\n  1. sub a\n  2. sub b\n2. second\n  1. sub c")

    def test_list_without_a_definition_is_a_bullet_list(self):
        """`\\ls7` with no list table entry: the numbering type cannot be told."""
        assert _text(r"\pard\ls7 one\par\pard\ls7\ilvl1 two\par") == "- one\n  - two"

    def test_list_level_without_a_number_is_a_plain_paragraph(self):
        """Number format 255 ('none') is how writers attach outline lists to ordinary paragraphs."""
        assert _text(_LISTS + r"\pard\ls3 plain\par") == "plain"

    def test_older_pn_numbering(self):
        """Word 6 `\\pn` paragraphs: bullets (`\\pnlvlblt`), then decimal numbers (`\\pnlvlbody\\pndec`); `\\pntext` is the writer's
        copy of the mark. Numbering starts over after a paragraph outside the list."""
        def bullet(text: str) -> str:
            return r"\pard{\pntext\'b7\tab}{\*\pn\pnlvlblt\pnf1{\pntxtb\'b7}}" + text + r"\par"

        def number(text: str) -> str:
            return r"\pard{\pntext 9.\tab}{\*\pn\pnlvlbody\pndec\pnstart1{\pntxta .}}" + text + r"\par"

        body = bullet("a") + bullet("b") + number("one") + number("two") + r"\pard Stop\par" + number("again")
        assert _text(body) == "- a\n- b\n1. one\n2. two\n\nStop\n\n1. again"

    def test_list_bullet_2_style_is_one_level_down(self):
        """Word's 'List Bullet 2' style: `\\ilvl0` of its own list, the level is the digit in the style name (as in DOCX). The
        kind is still read from level 0 of the list."""
        styles = r"{\stylesheet{\s0 Normal;}{\s7 List Bullet;}{\s8 List Bullet 2;}}"
        body = styles + _LISTS + r"\pard\s7\ls1 top\par\pard\s8\ls1\ilvl0 below\par\pard\s7\ls1 top again\par"
        assert _text(body) == "- top\n  - below\n- top again"

    def test_typed_bullet_followed_by_a_tab_is_a_list_item(self):
        """pandoc's RTF: `\\bullet \\tab text` with a hanging indent per level (`\\li360`, `\\li720`) and no list table. A dash
        in running text (no tab) is not an item; a paragraph in between starts the levels over."""
        def item(indent: int, mark: str, text: str) -> str:
            return rf"{{\pard \li{indent} \fi-360 {mark} \tx360\tab {text}\par}}" + "\n"

        body = (item(360, r"\bullet", _unicode("하나")) + item(720, r"\endash", "sub a") + item(720, r"\endash", "sub b")
                + item(360, r"\bullet", "two") + r"{\pard \endash  not an item\par}" + item(720, r"\bullet", "fresh"))
        assert _text(body) == "- 하나\n  - sub a\n  - sub b\n- two\n\n– not an item\n\n- fresh"

    def test_korean_list_items(self):
        body = _LISTS + "".join(rf"\pard\ls2 {_cp949(text)}\par" for text in ("매출 집계", "환율 적용"))
        assert _text(body, head=r"\ansi\ansicpg949") == "1. 매출 집계\n2. 환율 적용"


class TestTables:
    def test_simple_table(self):
        body = r"Intro\par" + _row(["Name", "Qty"]) + _row([_cp949("사과"), "3"]) + r"\pard After\par"
        assert _text(body, head=r"\ansi\ansicpg949") == "Intro\n\n| Name | Qty |\n| --- | --- |\n| 사과 | 3 |\n\nAfter"

    def test_row_definition_after_the_cells_is_used(self):
        """Word repeats (or only writes) the definition between the last `\\cell` and `\\row`."""
        row = r"\pard\intbl a\cell b\cell\trowd\cellx1000\cellx2000\row"
        assert _text(row + row) == "| a | b |\n| --- | --- |\n| a | b |"

    def test_vertically_merged_cells_repeat_their_value_on_every_row(self):
        """`\\clvmgf` starts a vertical merge, `\\clvmrg` cells below continue it (written empty) — the 'Domestic' shape of reports."""
        body = (_row(["Category", "Region"]) + _row(["Domestic", "North"], flags={0: r"\clvmgf"})
                + _row(["", "South"], flags={0: r"\clvmrg"}) + _row(["Overseas", "West"]))
        assert _text(body) == ("| Category | Region |\n| --- | --- |\n| Domestic | North |\n| Domestic | South |\n| Overseas | West |")

    def test_horizontally_merged_cells_become_one_spanning_cell(self):
        """`\\clmgf` + `\\clmrg`: the continuation cell is written but empty. A horizontal span is drawn as an HTML table."""
        body = _row(["Total", "", "9"], flags={0: r"\clmgf", 1: r"\clmrg"}) + _row(["a", "b", "c"])
        text = _text(body)
        assert '<td colspan="2">Total</td>' in text or '<th colspan="2">Total</th>' in text
        assert "<td>a</td><td>b</td><td>c</td>" in text

    def test_wider_cell_spans_the_columns_its_edges_cover(self):
        """A row with one cell ending at the edge of the second column of the other rows (how Word stores a column span). The edges
        differ by a few twips between rows (rounding)."""
        body = _row(["Header", "x"], edges=[2000, 3000]) + _row(["a", "b", "c"], edges=[1005, 1995, 3000])
        text = _text(body)
        assert 'colspan="2">Header<' in text
        assert "<td>a</td><td>b</td><td>c</td>" in text

    def test_merged_away_cell_with_text_keeps_its_text(self):
        """A `\\clmrg` / `\\clvmrg` cell that still has text is an ordinary cell: nothing is dropped."""
        body = _row(["a", "kept"], flags={0: r"\clmgf", 1: r"\clmrg"}) + _row(["also kept", "b"], flags={0: r"\clvmrg"})
        assert _text(body) == "| a | kept |\n| --- | --- |\n| also kept | b |"

    def test_row_without_cell_edges_is_placed_in_order(self):
        """No `\\cellx` at all (a generator that leaves them out): cells go one per column."""
        body = r"\trowd\pard\intbl a\cell b\cell\row\trowd\pard\intbl c\cell d\cell\row"
        assert _text(body) == "| a | b |\n| --- | --- |\n| c | d |"

    def test_cell_with_several_paragraphs_and_a_list(self):
        """Paragraphs inside a cell are lines of the cell; a heading style inside a cell is plain text."""
        body = _STYLES + _LISTS + r"\trowd\cellx1000\cellx2000\pard\intbl\s1 Head\par\pard\intbl\ls1 item\par\pard\intbl tail\cell\pard\intbl x\cell\row"
        assert _text(body) == "| Head<br>- item<br>tail | x |\n| --- | --- |"

    def test_nested_table_keeps_its_rows_and_columns(self):
        """A 2×1 table whose first cell holds a 1×2 table (`\\itap2`, `\\nestcell`, `\\nesttableprops` … `\\nestrow`) followed by
        text. The old-reader copy in `\\nonesttables` is skipped. The inner definition does not replace the outer one, which
        LibreOffice writes only before the cells."""
        body = (r"\trowd\cellx3000\cellx6000"
                r"\pard\intbl\itap2 in1\nestcell\pard\intbl\itap2 in2\nestcell{\*\nesttableprops\trowd\cellx1500\cellx3000\nestrow}"
                r"{\nonesttables\par}\pard\intbl outer text\cell\pard\intbl right\cell\row")
        text = _text(body)
        assert text.count("<table>") == 2
        assert "<table><tr><td>in1</td><td>in2</td></tr></table>" in text.replace("th>", "td>")
        assert text.index("in2") < text.index("outer text") < text.index("right")

    def test_table_inside_a_footnote_is_one_line(self):
        body = r"Text{\footnote \pard Source:\par" + _row(["Year", "Value"]) + _row(["2025", "10"]) + r"}\par"
        assert _text(body) == "Text[^1]\n\n[^1]: Source: Year / Value; 2025 / 10"

    def test_cells_after_the_last_row_end_are_kept(self):
        """A table cut off before `\\row` (or a writer that forgets it): the cells read are a row."""
        assert "| a | b |" in _text(_row(["h1", "h2"]) + r"\pard\intbl a\cell b\cell")


class TestPictures:
    def test_png_and_jpeg_blips_are_registered_in_place(self):
        """A PNG stored as hex wrapped over lines (with a `\\*\\blipuid` group that is not picture data) between two words. The
        placeholder sits where the picture is (the converter later sets it off as its own block)."""
        png = _png()
        hex_lines = "\n".join(png.hex()[index:index + 64] for index in range(0, len(png.hex()), 64))
        body = r"Before {\pict{\*\blipuid 0123456789abcdef}\picw80\pich80\pngblip " + "\n" + hex_lines + r"} after\par"
        extraction = extract_rtf(_rtf(body), ConvertOptions())
        assert extraction.sections[0].text == "Before ⟦IMG::1⟧ after"
        assert [image.data for image in extraction.images] == [png]
        result = convert(_rtf(body), "a.rtf")
        assert "[Image]" in result.text and [(image.width, image.height) for image in result.images] == [(80, 80)]

    def test_binary_picture_data(self):
        """`\\binN` followed by N raw bytes (braces and backslashes inside are data, not syntax)."""
        png = _png(color=(123, 92, 125))  # bytes 0x7b '{', 0x5c '\\', 0x7d '}' appear raw in the header-less check below
        data = rb"{\rtf1\ansi {\pict\pngblip\bin" + str(len(png)).encode() + b" " + png + rb"}{\pict\pngblip\bin5 {\}{\}text\par}"
        result = extract_rtf(data, ConvertOptions())
        assert [image.data for image in result.images] == [png, rb"{\}{\\"[:5]]
        assert result.sections[0].text.endswith("text")

    def test_compatibility_copy_of_a_picture_is_skipped(self):
        """Word and LibreOffice write a picture twice: `\\*\\shppict` (PNG) and `\\nonshppict` (a WMF rendering of the same)."""
        body = r"{\*\shppict{\pict\pngblip " + _png().hex() + r"}}{\nonshppict{\pict\wmetafile8 010009000003}}\par"
        result = convert(_rtf(body), "a.rtf")
        assert len(result.images) == 1 and result.text == "[Image]"

    def test_metafile_is_registered_and_reported_by_the_converter(self):
        """A lone WMF picture cannot be decoded; its position is kept and the converter's own warning says so."""
        result = convert(_rtf(r"A {\pict\wmetafile8\picw100\pich100 0100090000031234} B\par"), "a.rtf")
        assert "[Image]" in result.text
        assert any("unreadable format" in warning for warning in result.warnings)

    def test_pictures_are_left_out_when_extraction_is_off(self):
        result = convert(_rtf(r"A {\pict\pngblip " + _png().hex() + r"} B\par"), "a.rtf", ConvertOptions(extract_images=False))
        assert result.text == "A B" and result.images == []

    def test_hex_data_with_stray_characters_or_an_odd_digit_is_still_read(self):
        extraction = extract_rtf(_rtf(r"{\pict\pngblip 0a0b zz 0c0}"), ConvertOptions())
        assert extraction.images[0].data == b"\x0a\x0b\x0c"

    def test_pictures_past_the_limit_are_left_out_with_a_warning(self, monkeypatch):
        monkeypatch.setattr(rtf_module, "_MAX_PICTURES", 3)
        extraction = extract_rtf(_rtf(r"{\pict\pngblip 00}" * 5 + r"text\par"), ConvertOptions())
        assert len(extraction.images) == 3
        assert any("pictures" in warning for warning in extraction.warnings)


class TestLimits:
    def test_reading_stops_once_the_character_limit_is_exceeded(self):
        """10,000 paragraphs of 100 characters with a 2,000-character limit: reading stops near the limit, not at the end."""
        body = "".join(rf"{'x' * 100} {index}\par" + "\n" for index in range(10_000))
        extraction = extract_rtf(_rtf(body), ConvertOptions(max_chars=2_000))
        assert extraction.exceeded
        assert 2_000 < extraction.chars < 2_300
        result = convert(_rtf(body), "a.rtf", ConvertOptions(max_chars=2_000))
        assert result.exceeded and "x 9999" not in result.text
        assert not any("ends before" in warning for warning in result.warnings)

    def test_a_large_table_stops_while_its_cells_are_read(self):
        """One table of 20,000 rows: the limit is noticed cell by cell, not after the whole table is built."""
        body = "".join(_row([f"{'가' * 0}cell {index}", "y" * 50]) for index in range(20_000))
        extraction = extract_rtf(_rtf(body), ConvertOptions(max_chars=1_000))
        assert extraction.exceeded
        assert extraction.sections[0].text.count("\n") < 100


    def test_text_shaped_like_a_nested_table_placeholder_is_not_counted_against_the_limit(self):
        """A cell that holds a nested table and, as text, 80 digits between the private-use characters a nested-table
        placeholder is made of. The table drops that text when it is drawn, so it must not be counted while the cells are
        read: counted, the reading stopped at the first cell and the little that was written was under the limit — a
        document over the limit came back as five characters without `exceeded`."""
        forged = _unicode("\ue020" + "1234567890" * 8 + "\ue021")
        body = (r"\trowd\cellx1000\cellx2000 inner\nestcell \itap1 " + forged + r"\cell second\cell\row"
                r"\trowd\cellx1000\cellx2000\pard\intbl third\cell fourth\cell\row\pard after\par")
        whole = convert(_rtf(body), "a.rtf", ConvertOptions(max_chars=None))
        assert "fourth" in whole.text and whole.text.endswith("after") and len(whole.text) > 60
        assert convert(_rtf(body), "a.rtf", ConvertOptions(max_chars=60)).exceeded
        same = convert(_rtf(body), "a.rtf", ConvertOptions(max_chars=len(whole.text)))
        assert same.text == whole.text and not same.exceeded


class TestHostileInput:
    def test_data_that_is_not_rtf_is_a_corrupted_document(self):
        for data in (b"", b"hello", b"{\\rt", b"x" * 300 + b"{\\rtf1 late}"):
            with pytest.raises(CorruptedDocumentError, match="RTF document structure"):
                extract_rtf(data, ConvertOptions())

    def test_header_after_a_byte_order_mark_is_read(self):
        assert extract_rtf(b"\xef\xbb\xbf\r\n{\\rtf1 ok}", ConvertOptions()).sections[0].text == "ok"

    def test_truncated_document_keeps_what_was_read_with_a_warning(self):
        """Cut inside a group, inside a control word, after a lone backslash, inside a footnote and inside a table."""
        whole = r"{\rtf1\ansi First\par {\b Second\par}{\footnote note}Third\par" + _row(["a", "b"]) + r"\pard Last\par}"
        for cut in ("{\\b Sec", "Third\\pa", "Third\\", "{\\footnote no", "\\intbl b\\cell"):
            data = whole[:whole.index(cut) + len(cut)].encode()
            result = convert(data, "a.rtf")
            assert result.text.startswith("First"), cut
            assert any("ends before it is complete" in warning for warning in result.warnings), cut
        assert convert(whole.encode(), "a.rtf").warnings == []

    def test_text_after_the_document_group_is_ignored(self):
        """Extra closing braces and trailing bytes after the document closes (mail gateways append them)."""
        result = convert(rb"{\rtf1 body\par}}} trailing \par{junk}" + b"\x00" * 10, "a.rtf")
        assert result.text == "body" and result.warnings == []

    def test_long_field_instruction_with_many_results_is_cheap(self, monkeypatch: pytest.MonkeyPatch):
        """A field whose instruction is one piece of 200,000 characters, followed by 500 `\\fldrslt` words. The instruction used to
        be kept whole (the cap was checked before the piece was added) and its address worked out again at every result: 3 MB
        and 1,000 results took 11 seconds. It is cut at the cap and resolved once."""
        seen: list[int] = []
        resolve = rtf_module.field_link
        monkeypatch.setattr(rtf_module, "field_link", lambda text: (seen.append(len(text)), resolve(text))[1])
        body = r'{\field{\*\fldinst HYPERLINK "https://example.com/' + "a" * 200_000 + r'"}' + r"\fldrslt" * 500 + r" site}\par"
        assert "site" in _text(body)
        assert seen == [rtf_module._MAX_INSTRUCTION_CHARS]

    def test_pieces_with_the_same_formatting_are_one_run(self):
        """200,000 control symbols (`\\_`, a hyphen each) in bold between two plain words: every symbol used to be a run of its
        own (10 million of them: 22 seconds and 4.6 GB). Neighbours with the same formatting are collected as one run."""
        flows: list = []
        text = _text(r"x {\b " + r"\_" * 200_000 + r"} y\par")
        assert text == "x **" + "-" * 200_000 + "** y"
        reader = rtf_module._Reader(_rtf(r"a{\b b\_c}{\b d}e"), 0, ConvertOptions(), rtf_module.Extraction())
        reader.read()
        flows.append(reader._flow)
        flows[0]._settle()
        assert [run[0] for run in flows[0].runs] == ["a", "b-cd", "e"]

    def test_cells_past_the_cell_limit_of_a_table_are_left_out_with_a_warning(self, monkeypatch: pytest.MonkeyPatch):
        """`\\cell` is five bytes and a cell of the grid costs hundreds: 4,000 rows of 1,000 empty cells took 13 seconds and
        3.4 GB, and so did the same cells with no `\\row` at all. A table keeps `_MAX_TABLE_CELLS` cells; the cells after
        that are left out, whether rows end or not, and one warning says so. The text after the table is read. Inside the
        limit a row keeps every cell, however many."""
        monkeypatch.setattr(rtf_module, "_MAX_TABLE_CELLS", 45)
        warning = "A table has more than 500,000 cells; the cells after that were left out."
        rows = r"\trowd\cellx100" + (r"\pard\intbl a" + r"\cell" * 20 + r"\row" + "\n") * 12
        for body in (rows + r"\pard\intbl tail\cell\pard after\par", rows + r"\row\row\pard after\par",
                     r"\trowd\cellx100\pard\intbl a" + r"\cell" * 300 + r"\pard after\par"):
            result = convert(_rtf(body), "a.rtf")
            assert result.text.endswith("after") and "tail" not in result.text
            assert result.text.count("a\n") in (1, 3)   # the rows that fit: 20 + 20 + 5 cells, or the one unfinished row
            assert result.warnings == [warning]
        monkeypatch.undo()
        wide = r"\trowd\cellx100\pard\intbl " + "".join(f"c{index}\\cell " for index in range(5000)) + r"\row"
        result = convert(_rtf(wide), "a.rtf")
        assert "c4999" in result.text and result.warnings == []

    def test_paragraph_of_endless_runs_keeps_its_text_in_a_bounded_number_of_runs(self, monkeypatch: pytest.MonkeyPatch):
        """One paragraph of runs that cannot be merged (`{\\b a}b` again and again; 2.8 million of them took 16 seconds and
        2.3 GB). A paragraph has at most `_MAX_PARAGRAPH_RUNS` runs; what follows is read as one plain run, so every character
        is kept and only the marks stop. The next paragraph starts over."""
        monkeypatch.setattr(rtf_module, "_MAX_PARAGRAPH_RUNS", 6)
        text = _text(r"{\b bold} plain " * 10 + r'{\field{\*\fldinst HYPERLINK "https://example.com/"}{\fldrslt link}}\par {\b next} one\par')
        first, second = text.split("\n\n")
        assert first.startswith("**bold** plain **bold** plain") and first.count("**bold**") == 4
        assert first.endswith("bold plain bold plain link")
        assert first.replace("*", "") == "bold plain " * 10 + "link"   # every character, no link around the tail
        assert second == "**next** one"

    def test_groups_nested_past_the_limit_are_skipped_with_a_warning(self):
        """100,000 nested groups: the text above the limit is kept, the text below is not read, nothing recurses."""
        data = rb"{\rtf1 top " + b"{" * 100_000 + b"deep" + b"}" * 100_000 + rb" after\par}"
        result = convert(data, "a.rtf")
        assert result.text == "top after"
        assert any("nested deeper" in warning for warning in result.warnings)

    def test_binary_length_past_the_end_of_the_file_is_cut_to_the_file(self):
        """`\\bin4000000000` in a 40-byte file, inside a picture and outside one."""
        for body in (rb"A\par{\pict\pngblip\bin4000000000 abc}", rb"A\par\bin4000000000 abc}"):
            result = convert(rb"{\rtf1 " + body, "a.rtf")
            assert result.text.startswith("A")
            assert any("ends before it is complete" in warning for warning in result.warnings)

    def test_absurd_uc_value_does_not_swallow_the_text(self):
        """`\\uc1000000` (and a negative one) before an escape: the value is out of range, so one fallback character is dropped."""
        assert _text(r"\uc1000000\u44032?" + "x" * 50 + r"\par\uc-5\u44033?y\par") == "가" + "x" * 50 + "\n\n각y"

    def test_number_with_thousands_of_digits_is_not_parsed_as_one_number(self):
        """`\\fs` followed by 5,000 digits (more than `int()` accepts), also as a `\\u` value and a `\\bin` length."""
        digits = "9" * 5000
        result = convert(_rtf(rf"a\fs{digits} b\u{digits} c\par"), "a.rtf")
        assert result.text.startswith("a") and "c" in result.text

    def test_wrong_encoding_does_not_fail(self):
        """Bytes that are invalid in the declared code page, an unknown code page number and an unknown charset."""
        fonts = r"{\fonttbl{\f0\fcharset99 X;}{\f1\cpg31337 Y;}}"
        result = convert(_rtf(fonts + r"\f0 a\'ff\'ffb {\f1 c\'81d}\par", head=r"\ansi\ansicpg99999"), "a.rtf")
        assert result.text.startswith("a") and result.text.endswith("d")
        assert convert(_rtf(r"ok \'b0\par", head=r"\ansi\ansicpg949"), "a.rtf").text.startswith("ok")

    def test_huge_span_and_cell_counts_in_a_row_definition_are_bounded(self):
        """A row definition with 100,000 `\\cellx` (all merge-continuation cells) for a two-cell row, and edges far apart."""
        definition = r"\trowd" + r"\clmrg\clvmrg\cellx5" * 100_000
        body = definition + r"\pard\intbl a\cell b\cell\row" + _row(["c", "d"], edges=[1, 2_000_000_000])
        assert "a" in _text(body) and "d" in _text(body)

    def test_absurd_table_nesting_depth_is_bounded(self):
        """`\\itap1000000` on a nested cell: the depth is capped; the text is kept."""
        text = _text(r"\trowd\cellx1000\pard\intbl\itap1000000 deep\nestcell{\*\nesttableprops\trowd\cellx500\nestrow}\pard\intbl out\cell\row")
        assert "deep" in text and "out" in text

    def test_destinations_reopened_inside_themselves_do_not_break_the_state(self):
        """A picture inside a picture, a footnote inside a footnote, a field without a result, a style sheet inside the body text."""
        body = (r"A{\pict\pngblip 00{\pict\pngblip 11}}B{\footnote one{\footnote two}}C{\field{\*\fldinst HYPERLINK}}D"
                r"{\fldrslt E}{\stylesheet{\s1 heading 1;}{{\s2 x;}}}F\par")
        assert _text(body, ConvertOptions(extract_images=False)) == "AB[^1]CDEF\n\n[^1]: one"

    @pytest.mark.timing
    def test_time_is_linear_in_the_size_of_the_file(self):
        """Shapes of 2 MB input that are slow for a reader that re-scans, concatenates or copies: unclosed groups, control words
        only, escapes only, hex blobs in a skipped destination and a picture, a 4,096-cell row definition reused by 100,000
        one-cell rows, and 300,000 headers with different texts."""
        size = 2_000_000
        cases = [
            rb"{\rtf1 " + b"{" * size,
            rb"{\rtf1 " + rb"\b\i0 " * (size // 6) + b"}",
            rb"{\rtf1\ansicpg949 " + rb"\'b0\'a1" * (size // 8) + b"}",
            rb"{\rtf1 {\*\themedata " + b"0a" * (size // 2) + rb"}{\pict\pngblip " + b"0a\n" * (size // 3) + rb"}text\par}",
            rb"{\rtf1 \trowd" + rb"\cellx1" * 5000 + rb"\pard\intbl a\cell\row " * (size // 20) + b"}",
            rb"{\rtf1 " + b"".join(rb"{\header h%d}" % index for index in range(300_000)) + b"}",
        ]
        for data in cases:
            started = time.perf_counter()
            convert(data, "a.rtf", ConvertOptions(max_chars=None, include_headers_footers=True))
            assert time.perf_counter() - started < 10
