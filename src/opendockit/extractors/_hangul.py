"""Paragraph rules shared by HWP and HWPX, the binary and the XML file format of Hangul.

The two extractors read different containers and arrive at the same things for a paragraph: a head (number or bullet), a
style name, a left margin, and text pieces with tables and text boxes between them. What is done with those is decided
here once, so the two formats cannot drift apart.
"""

from collections import Counter
from collections.abc import Callable
import re

from opendockit.extractors._inline import (
    LINK_OPEN,
    emphasis_marks,
    link_close,
    link_target,
    merge_emphasis_marks,
    plain_inline,
    resolve_inline,
    resolve_scripts,
    resolve_strikes,
)
from opendockit.extractors._lists import _OBJECT_PLACEHOLDERS, BulletNesting, is_placeholder_only, paragraph_head


# Hangul's "개요 1" to "개요 7" (Outline 1-7) paragraph styles are the chapter/section structure of the document. They become Markdown headings.
OUTLINE_STYLE = re.compile(r"^(?:개요|Outline)\s*([1-9])$")
# A paragraph longer than this stays body text, not a heading, even with an outline style (some documents write body text in outline styles).
HEADING_MAX_CHARS = 120
# Table cell marks in the preview text (HWP `PrvText`, HWPX `Preview/PrvText.txt`). A preview with nothing but these has no text.
PREVIEW_CELL_MARKS = str.maketrans("", "", "<>")
_WORD_CHARACTER = re.compile(r"[^\W_]")


def preview_has_words(preview: str) -> bool:
    """Whether the preview text holds a letter or digit outside its cell marks. A preview of nothing but marks, punctuation and
    blanks is a document without text: empty tables with a `.` in a text box gave `<.<><><>` as the body (Pinned by:
    test_hwp.py::TestPreviewFallback)."""
    return bool(_WORD_CHARACTER.search(preview.translate(PREVIEW_CELL_MARKS)))

# The temporary link marks of `_inline` (U+000E–U+0014). Document text never holds them in a paragraph that has a link: the text
# of a control (ruby text, composed characters) is read from a binary HWP as it is, and could otherwise forge a link.
_WITHOUT_INLINE_MARKS = str.maketrans("", "", "".join(map(chr, range(0x0E, 0x15))))
# The address of a hyperlink field command: the part before the first `;` that is not escaped (`http\://a.example;1;0;0;`).
_COMMAND_ADDRESS = re.compile(r"(?:[^\\;]|\\.)*", re.DOTALL)
_COMMAND_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


# ---- Letter-spaced labels ----------------------------------------------------------------------------------------

# A line this long or shorter can be a letter-spaced label (`unspaced_label`)
_SPACED_LABEL_MAX_CHARS = 30
# The number of syllables of a letter-spaced label (`담   당   자`): a longer row of one-syllable words is no label, and nor is
# a pair — two one-syllable words spaced wide are as often choices (`남    여`, `갑   을`, `유   무`) as a label (`성   명`)
_SPACED_LABEL_SYLLABLES = range(3, 7)
# One Hangul syllable, and the units of a blank date or time in a form (`년   월   일`, `시   분`), which are spaced for writing in,
# not for looks
_SYLLABLE = re.compile(r"[가-힣]")
_DATE_UNITS = frozenset("년월일시분초")
# One-syllable words of a sentence that are no syllable of a label: the conjunctions `및`, `또` (`수  및  양`)
_JOINING_WORDS = frozenset(("및", "또"))
# The syllables of the choices and scales of a form (`상   중   하`, `대   중   소`, `갑   을   병`, `월   화   수   목   금`,
# `유   무`): a row whose syllables all come from one of these groups — weekdays, the parties of a contract, a scale — is choices
# to tick, not a label, and joined they could not be told apart again; `수정일`, `예정일` mix the groups and are labels
_CHOICE_GROUPS = (frozenset("월화수목금토일"), frozenset("갑을병정무"), frozenset("상중하대소유무남여찬반"))
# A case particle that ends a phrase, never a label (`이   름   을`, `주   소   는`); not 과, 로, 의, which end labels (`결과`,
# `경로`, `협의`)
_LAST_PARTICLES = frozenset("을를은는")
# A list mark in front of a label: a bullet, `1.`, `가.`, `(1)`, `①`
_LABEL_LEAD = re.compile(r"(?:[-•·○●◦▪□■◇◆※*]|\(?\d{1,3}[.)]|\(?[가-힣][.)]|[①-⑳]) +")
# The gaps between the words of a label
_LABEL_GAP = re.compile(r" +")
# Whitespace other than a plain space (a tab, an ideographic space): it separates a label from its value (`성   명<tab>홍길동`),
# so each part between is looked at on its own and the whitespace is kept
_LABEL_SEPARATOR = re.compile(r"([^\S ]+)")


def unspaced_label(line: str) -> str:
    """A letter-spaced label with the spaces between its syllables taken out: `담   당   자` → `담당자`, `성   명` → `성명`; a list
    mark in front stays with one space (`□ 추   진   배   경` → `□ 추진배경`). It is applied to the written line, after links and
    emphasis: a cell that is bold throughout is a label like any other.

    Korean forms space the syllables of a label to fill the width of its cell. Read as written, the label is a row of one-syllable
    words that a search for `담당자` or `성명` does not find, and an LLM reading the form sees broken words. A line is a spaced
    label only when it looks like nothing else: two to six words, each one Hangul syllable, with gaps of the same two or more
    spaces between them. One space between one-syllable words is ordinary Korean as often (`수 및 양`, `할 수 있는 일`, the
    choices `남 여`, `상 중 하`, a label and its value `성 명 홍 길 동`), and joining it cannot be undone, so `기 관 명` stays as
    written. A blank date or time (`년   월   일`, `시   분`) is not a label either. A tab or an ideographic space separates a
    label from its value (`성   명<tab>홍 길 동` → `성명<tab>홍 길 동`): only plain spaces are taken out, each part between other
    whitespace on its own (Pinned by: test_hwp_spaced_labels.py).
    """
    if len(line.strip()) > _SPACED_LABEL_MAX_CHARS or "  " not in line:
        return line
    parts = _LABEL_SEPARATOR.split(line)
    if len(parts) > 1:
        return "".join(part if index % 2 else _unspaced_part(part) for index, part in enumerate(parts))
    return _unspaced_part(line)


def _unspaced_part(line: str) -> str:
    """`unspaced_label` for text whose only whitespace is plain spaces."""
    stripped = line.strip(" ")
    lead = _LABEL_LEAD.match(stripped)
    head, rest = (stripped[:lead.end()].rstrip(" ") + " ", stripped[lead.end():]) if lead else ("", stripped)
    words = rest.split(" ")
    words = [word for word in words if word]
    gaps = set(_LABEL_GAP.findall(rest))
    if len(words) not in _SPACED_LABEL_SYLLABLES or len(gaps) != 1 or len(next(iter(gaps))) < 2:
        return line
    if (not all(_SYLLABLE.fullmatch(word) for word in words) or all(word in _DATE_UNITS for word in words)
            or any(word in _JOINING_WORDS for word in words) or any(all(word in group for word in words) for group in _CHOICE_GROUPS)
            or words[-1] in _LAST_PARTICLES):
        return line
    return line[:len(line) - len(line.lstrip(" "))] + head + "".join(words) + line[len(line.rstrip(" ")):]


class _LinkMark(str):
    """A link or emphasis mark in the text pieces of a paragraph, told apart from the text of the document
    (`ParagraphPieces.flush`)."""

    __slots__ = ()


def emphasis_pieces(text: str, bold: bool, italic: bool) -> list[str]:
    """The text pieces of a bold and/or italic run: the text between its marks. Whitespace-only text is left unmarked, as by
    `_inline.mark_emphasis`. The marks are pieces of their own, so the text of the document cannot forge one
    (Pinned by: test_hwp_emphasis.py)."""
    if not (bold or italic) or not text.strip():
        return [text]
    opening, closing = emphasis_marks(bold, italic)
    return [_LinkMark(opening), text, _LinkMark(closing)]


def link_start() -> str:
    """The piece that starts a hyperlink in a paragraph (`ParagraphPieces`); `link_end` ends it."""
    return _LinkMark(LINK_OPEN)


def link_end(url: str) -> str:
    """The piece that ends a hyperlink started with `link_start`. `url` must come from `command_link`."""
    return _LinkMark(link_close(url))


# The guide of a click-here field (누름틀) in its command: `Clickhere:set:44:Direction:wstring:2:날짜 HelpState:…`, the length
# of the text in characters before it.
_CLICK_HERE_GUIDE = re.compile(r"Direction:wstring:(\d{1,5}):")


def click_here_guide(command: str) -> str | None:
    """The guide text of a click-here field from its command (HWP the `%clk` field control, HWPX the `Command` parameter of
    `hp:fieldBegin type="CLICK_HERE"`), or None when there is none."""
    found = _CLICK_HERE_GUIDE.search(command)
    if found is None:
        return None
    guide = command[found.end():found.end() + int(found.group(1))]
    return guide.strip() or None


def drop_unfilled_guide(pieces: list[str], start: int, guide: str | None) -> None:
    """Remove the pieces from `start` on when they are nothing but the guide text of a click-here field that was not filled in.

    Hangul shows the guide of an empty field (`이곳을 마우스로 누르고 내용을 입력하세요`, or a word such as `날짜`) in its place
    on screen and does not print it, and it was read as the content of the form (Pinned by:
    test_hwp.py::TestClickHereFields). What a person typed into the field stays, as does a field that ends in another paragraph.
    """
    if guide is None or not 0 <= start <= len(pieces):
        return
    if "".join(piece for piece in pieces[start:] if type(piece) is not _LinkMark).strip() == guide:
        del pieces[start:]


def has_text(pieces: list[str]) -> bool:
    """Whether the text pieces hold anything but whitespace and link marks."""
    return any(type(piece) is not _LinkMark and piece.strip() for piece in pieces)


def command_link(command: str) -> str | None:
    """The vetted address (`_inline.link_target`: http, https and mailto only) of a Hangul hyperlink field, from its command —
    HWP the command string of the `%hlk` field control, HWPX the `Command` parameter of `hp:fieldBegin type="HYPERLINK"`. The
    command is `address;…` with `:` and `;` of the address escaped by a backslash (`http\\://a.example/;1;0;0;`). A link to a
    bookmark, a file or another document gives None, and its text stays plain."""
    return link_target(_COMMAND_ESCAPE.sub(r"\1", _COMMAND_ADDRESS.match(command).group()))


class _SizeCandidate(str):
    """A top-level paragraph that may be a heading by its character size (`SizeHeadings`). Its value is the paragraph as body text;
    `heading` is the same paragraph written as a heading (the number without a list bullet), `size` its character size. (A `str`
    subclass cannot have `__slots__` that hold values.)"""

    heading: str
    size: int


# Characters not counted for the body size (`SizeHeadings.count`): controls and whitespace
_UNCOUNTED = dict.fromkeys([*range(32), *(code for code in range(32, 0x3001) if chr(code).isspace())])
# The most characters the largest size of a document's top level may hold to be taken for its titles when it is also the size
# most top-level characters have (`SizeHeadings.resolve`)
_TITLE_ONLY_CHARACTERS = 40
# The most paragraphs that size may have for them to be titles (a form's title, two forms' titles)
_LONE_TITLES = 2
# Ratio of a paragraph's character size to the body size → heading level (`SizeHeadings`)
_SIZE_HEADING_LEVELS = ((1.5, 1), (1.3, 2), (1.15, 3))
# A paragraph longer than this is not taken for a heading by its size (a sentence written large, a cover-page blurb)
_SIZE_HEADING_MAX_CHARS = 80
# A word in any script: a paragraph without one (a date `2025.12.12`, a blank form date `202 . .`, a page number) is no heading.
_LETTER = re.compile(r"[^\W\d_]")
_EQUATION = re.compile(r"\$[^$\n]*\$")
# A sentence: the Korean sentence endings `…다` (`…합니다`, `…한다`) and `…세요`, or a closing full stop, question or exclamation
# mark. Not `…요` alone: 개요, 필요 and 주요 end many titles ('1. 사업 개요').
_SENTENCE_END = re.compile(r"(?:다|세요)[.!?]?\Z|(?<!\d)[.!?]\Z")
# The sentence of a numbered clause (`1. 갑은 을에게 … 지급한다.`, `가. 다만, … 아니하다`, `… 따른다`, `… 본다`): an outline head
# on such a paragraph makes no heading; an outline head is the author's title otherwise, whatever it says (`1. 캐나다`,
# `Ⅰ. 총칙(개정 2024. 1. 1.)`, `2024년 12월`, `왜 바다인가?`), so only this guard of the size headings applies to it
# (`_outline_title`; Pinned by: test_hwp_size_headings.py::TestOutlineHeads::test_titles_with_outline_heads_stay_headings,
# test_numbered_clause_sentences_stay_numbered_paragraphs). A Korean sentence ends in `다` after a syllable closed by ㄴ
# (`…한다`, `…따른다`, `…본다`, `…진다`, the ㄴ다 of the plain style), after one of the stems and endings below (`…하다`,
# `…같다`, `…있다`, `…이다`, `…니다`), after one closed by ㅆ (`…했다`, `…됐다`), or in a word and a full stop; `캐나다`, `바다`,
# `플로리다` end in `다` after an open syllable and are names, and a ㄴ다 or a full stop counts only in three words or more
# (`우간다`, `Appendix A.`). A question mark ends a title as often as a sentence (FAQ headings), so it is no clause end; a colon,
# semicolon or dash after the end is looked past (`…포함한다:`).
_CLAUSE_STEMS = frozenset("하같있없이니았었였겠않")
# The final consonants (종성 index) of a syllable before `다` that end a sentence: ㄴ (`…한다`, `…따른다`) and ㅆ (`…했다`,
# `…됐다`, `…지났다`, the contracted past)
_FINAL_N, _FINAL_SS = 4, 20
_CLAUSE_CLOSING = "\"'”’):;—–"
# The addressee line of a Korean form or letter (`…사업단장 귀하`, `… 귀중`), printed large at the bottom: not a heading.
_ADDRESSEE = re.compile(r"(?:귀하|귀중)\Z")
# What follows the last tab of a table-of-contents line: a page label (`7`, `3-12`, `ii`, `n+1`, `2 3`)
_PAGE_LABEL = re.compile(r"[\divxlcIVXLCn+\-–—~., ]{1,12}")
# Closing quotes and brackets after the end of a sentence (`“오늘은 좋은 하루였다.”`, `…한다)`)
_CLOSING = "\"'”’)）"
# A Korean polite sentence ending `…요` (`함께 해요`, `좋아요`, `그렇죠`); not the `요` of 개요, 필요, 주요 or 중요
_POLITE_END = re.compile(r"(?:[어아여해에예네군데게까나래봐와워줘져려]요|죠)\Z")
# A Korean date or time, filled in or blank (`2024년 12월 31일`, `2024년    월    일`, `3월 5일(화)`, `14시 30분`)
_WEEKDAY = re.compile(r"\(?[월화수목금토일](?:요일)?\)")
_KOREAN_DATE = re.compile(r"[\d\s.,:~\-()]*(?:[년월일시분초][\d\s.,:~\-()]*){2,}")
# The signature or stamp line of a form (`신청인 : (서명 또는 인)`, `대표이사 홍 길 동 (인)`)
_SIGNATURE = re.compile(r"\(\s*(?:서명|인|날인|직인)(?:\s*(?:또는|및|/)\s*(?:서명|인|날인|직인))?\s*\)\Z")
# A line that starts with a typed bullet or note mark (`ㅇ 예산 확보 필요`, `※ 문의`, `- 세부 내용`, `▶ 일정`): an item of a
# 개조식 list, not a title. Not `□`/`■`: the section mark of 개조식 reports, where a `□ 추진 배경` set large is the heading
_BULLET_LEAD = re.compile(r"(?:[ㅇo](?=\s)|[-–·•※○●◦◎▶▷►▸◆◇◈▪▫*→⇒➢✓√❍])")
# The caption of a figure or table (`<그림 1> 추진 체계도`, `[표 2] 예산 현황`, `그림 3. 조직도`, `Table 1 Budget`): written
# large under its picture it is no heading of the text around it, and as one it split the section it belongs to
# (Pinned by: test_hwp_size_headings.py::TestSizeHeadings::test_dates_signatures_quotes_and_contacts_are_no_headings)
_CAPTION_LEAD = re.compile(r"[<〈《\[［(（【〔]?\s*(?:(?:그림|표|사진|도표|도식|차트)\s*[-–]?\s*(?:\d|[Ⅰ-ↈ])"
                           r"|(?i:figure|fig\.?|table)\s*[-–]?\s*(?:\d|[Ⅰ-ↈ]|[IVX]+(?![A-Za-z-])))")
# A web address, e-mail address or phone number (`www.example.go.kr`, `02-123-4567`): contact lines, not titles
_CONTACT = re.compile(r"(?i:https?://|www\.)|[\w.+-]+@[\w-]+\.\w|(?<![\d-])(?:0\d{1,2}-\d{3,4}|1[5-9]\d{2})-\d{4}(?![\d-])"
                      r"|(?<![\w.-])[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.(?:kr|com|net|org)\b")


def _outline_title(plain: str) -> bool:
    """Whether a paragraph with an outline head is the title it numbers, not the sentence of a numbered clause."""
    text = plain.strip().rstrip(_CLAUSE_CLOSING).rstrip()
    if not text:
        return False
    if text.endswith("다.") and len(text) > 2 and "가" <= text[-3] <= "힣":
        return False   # a Korean sentence with its full stop, of any length (`제3조를 준용한다.`)
    if (text[-1] == "." and len(text) > 1 and not text[-2].isdigit() and not text[-2].isspace() and text[-2] not in ".…"
            and len(text.split()) >= 3):
        return False   # a sentence and its full stop (not a number's or an ellipsis's, not `Appendix A.`)
    text = text.rstrip(".!").rstrip()
    if len(text) < 2 or text[-1] != "다" or not "가" <= text[-2] <= "힣":
        return True
    stem, final = text[-2], (ord(text[-2]) - 0xAC00) % 28
    if stem in _CLAUSE_STEMS or final == _FINAL_SS:
        return False
    # The ㄴ다 of the plain style is a clause end only in a sentence of three words or more: a name ends in it as often
    # (`우간다`, `르완다`, `동아프리카 우간다`, `미래가 온다`)
    return not (final == _FINAL_N and len(text.split()) >= 3)


def _size_heading_text(plain: str) -> bool:
    """Whether a paragraph can be a heading by its size, from its text: short, one line, with a word, not a sentence (also inside
    closing quotes or brackets, or in the polite `…요` form), not a line in quotes, not a date, not the addressee, signature or
    stamp line of a form, not an item of a typed bullet list or a note (`※`), not a figure or table caption, not a contact
    line (web or e-mail address, phone number), and not a table-of-contents line (text, tab, page label) (Pinned by:
    test_hwp_size_headings.py::TestSizeHeadings::test_lines_that_only_look_large_are_no_headings,
    test_dates_signatures_quotes_and_contacts_are_no_headings).

    An outline head (개요 번호) is held to `_outline_title` alone (`decorated_text`)."""
    text = plain.strip()
    if not text or len(text) > _SIZE_HEADING_MAX_CHARS or "\n" in text:
        return False
    unquoted = text.rstrip(_CLOSING).rstrip()
    if not _LETTER.search(_EQUATION.sub("", text)) or _SENTENCE_END.search(unquoted) or _POLITE_END.search(unquoted):
        return False
    if (_ADDRESSEE.search(text) or _SIGNATURE.search(text) or _BULLET_LEAD.match(text) or _CAPTION_LEAD.match(text)
            or _CONTACT.search(text)):
        return False
    if text[0] in "\"“'‘" and text[-1] in "\"”'’":
        return False
    if _KOREAN_DATE.fullmatch(_WEEKDAY.sub("", text)):
        return False
    return "\t" not in text or not _PAGE_LABEL.fullmatch(text.rsplit("\t", 1)[1].strip())


# A header that is only a page number (`header_by_default`, `_is_page_number`): beside digits and the dashes and brackets around
# them, only page words (`Page`, `p.`, `쪽`, `면`, `페이지`, and the `of`, `제`, `총`, `중` written with them: `1 of 9`,
# `제 3 쪽`, `총 9쪽 중 3쪽`; the number of a page number field is not written, so `Page` or `of` alone is left) and Roman
# numerals in lower case, or in upper case between dashes or brackets (`- iv -`, `ii`, `- IV -`). A word that only spells a
# Roman numeral in upper case (`MIX`, `CV`, `I`) or a lone `No` is a header's word (Pinned by:
# test_hwp_headers.py::TestHeadersByDefault::test_page_number_with_a_page_word_is_left_out,
# test_header_of_words_that_look_like_page_words_is_written)
_PAGE_TOKEN = re.compile(r"[^\s\d\-–—/|.,:()\[\]<>~]+")
_PAGE_WORDS = frozenset(("page", "pages", "pg", "p", "pp", "of", "쪽", "면", "페이지"))
_PAGE_FILLERS = frozenset(("제", "총", "중"))
# The Roman numerals of front-matter pages, 1 to 39 (`i` … `xxxix`): not the l, c, d, m of `cm`, `mm`, `xl`, `mix`, `(c)`
_ROMAN_NUMERAL = re.compile(r"x{0,3}(?:ix|iv|v?i{0,3})")


def _is_page_number(words: str) -> bool:
    """Whether a header's words are a page number only (`_PAGE_TOKEN`)."""
    found = False
    for token in _PAGE_TOKEN.finditer(words):
        word = token.group()
        if word.lower() in _PAGE_WORDS:
            found = True
        elif word in _PAGE_FILLERS:
            continue
        elif (_ROMAN_NUMERAL.fullmatch(word.lower()) and not _touches_a_digit(words, token.start(), token.end())
              and (word.islower() or _between_page_marks(words, token.start(), token.end()))):
            found = True
        else:
            return False
    return found


def _between_page_marks(words: str, start: int, end: int) -> bool:
    """Whether `words[start:end]` stands between a dash or an opening bracket and a dash or a closing bracket (`- IV -`). The
    neighbours are found by index, without copying the text around: one header of `(I) ` repeated took quadratic time."""
    before, after = start - 1, end
    while before >= 0 and words[before].isspace():
        before -= 1
    while after < len(words) and words[after].isspace():
        after += 1
    return before >= 0 and words[before] in "-–—([<" and after < len(words) and words[after] in "-–—)]>"


def _touches_a_digit(words: str, start: int, end: int) -> bool:
    """Whether `words[start:end]` is written against a digit (`v1.0`): part of a version or a code, not a Roman numeral."""
    return (start > 0 and words[start - 1].isdigit()) or (end < len(words) and words[end].isdigit())


def header_by_default(text: str) -> str:
    """The text of a Hangul header written when `include_headers_footers` is off: its words without pictures and other objects
    (a logo in the header was never written by default and would cost a description each time), or '' when it holds no word.

    Hangul forms and reports put the form number and the title of the document in the header (`[별지 제3호 서식] 표준 현장실습학기제
    평가표 및 출석부`, `시험합의서` and its document number): in 9 of 34 real HWP / HWPX files that text was in no other place, and
    leaving headers out dropped it without a warning. A header with no word (`- 1 -`, a page number field) is still left out, and
    so is a page number with a page word or in Roman numerals (`Page 1`, `- 3 쪽 -`, `- iv -`, `p. 2 of 9`; the number of a page
    number field is not written, so `Page` alone is left), and footers. Each distinct header is written once, before the body, as
    with the option on (Pinned by: test_hwp_headers.py).

    The pictures, notes, comments and OLE objects of a header are taken back by the extractor when the header is not written, or
    only its words are."""
    words = "\n".join(" ".join(line.split()) for line in _OBJECT_PLACEHOLDERS.sub(" ", text).split("\n")).strip()
    return words if _LETTER.search(words) and not _is_page_number(words) else ""


class SizeHeadings:
    """Headings told by character size, for Hangul documents that set their titles by size alone.

    Most Hangul documents write every paragraph in the 바탕글 (Normal) style and make a title by setting a larger (usually bold)
    character size on it, not by the 개요 (outline) styles or paragraph heads. In 26 real HWP files and 8 HWPX files, none had an
    outline style or head on a title, so no heading came out at all. A top-level paragraph whose smallest character size is at least
    1.15 / 1.3 / 1.5 times the body size becomes a level 3 / 2 / 1 heading; the body size is the size most text characters of
    the top-level body paragraphs have (counted while they are read, `count`). The paragraph must also look like a title
    (`_size_heading_text`): the same files otherwise gave a heading for a cover-page date, every table-of-contents line, the
    addressee line of a form and a sentence written large. A document with any outline heading (style or paragraph head) keeps
    those only: there the author marked the structure.

    The paragraphs are rendered as they are read, before the body size is known, so a candidate is kept as a `_SizeCandidate` in the
    blocks and settled by `resolve` at the end (Pinned by: test_hwp_size_headings.py).
    """

    def __init__(self) -> None:
        self._sizes: dict[int, int] = {}   # character size → number of characters written in it, top-level body paragraphs
        self._all_sizes: dict[int, int] = {}   # the same over every paragraph but notes (cells, text boxes, headers)
        self.outlined = False              # whether the document has an outline heading

    def count(self, size: int, text: str, *, body: bool = True) -> None:
        """Count the characters of `text`, written in `size`, of a top-level body paragraph (the paragraphs a candidate comes
        from). Only text characters count (`_UNCOUNTED`: not whitespace, not controls), the same in HWP and HWPX: the text of
        tables and notes, written smaller, pulled the body size down and made headings of body lines, and HWP counted the 8 WCHARs
        of each control and the paragraph end, so the same document had other headings as HWP than as HWPX (Pinned by:
        test_hwp_size_headings.py::TestSizeHeadings::test_body_size_is_counted_on_body_paragraphs_only,
        test_hwp_and_hwpx_count_the_same_characters)."""
        characters = len(text.translate(_UNCOUNTED)) if size > 0 else 0
        if characters:
            self._all_sizes[size] = self._all_sizes.get(size, 0) + characters
            if body:
                self._sizes[size] = self._sizes.get(size, 0) + characters

    def body_size(self, candidates: Counter[int] | None = None) -> int:
        """The size most top-level text characters have, the smaller one on a tie. A form or a memo has little top-level
        text but its title, whose size was then the body size and never larger than it: when the largest top-level size is
        also the most frequent one and holds few characters (`_TITLE_ONLY_CHARACTERS`), it is taken for the titles, and the
        body size is the most frequent of the smaller top-level sizes (`수신 각 부서` beside `회의 결과 보고`, `(앞쪽)` beside a
        form title), or, with none, that of every paragraph but notes (the cells of a form) (Pinned by:
        test_hwp_size_headings.py::TestSizeHeadings::test_form_whose_body_is_in_tables_keeps_its_title,
        test_short_body_lines_over_a_table_stay_body_text, test_title_beside_a_shorter_line_keeps_its_heading)."""
        body = _most_frequent(self._sizes)
        # ... and only for a title or two: lines of a memo all in one size, or a notice in large print, are its body
        # (Pinned by: test_hwp_size_headings.py::TestSizeHeadings::test_short_lines_all_in_one_size_stay_body_text)
        lone_title = candidates is None or candidates.get(body, 0) <= _LONE_TITLES
        if body and body == max(self._sizes) and self._sizes[body] <= _TITLE_ONLY_CHARACTERS and lone_title:
            smaller = {size: count for size, count in self._sizes.items() if size < body}
            body = _most_frequent(smaller) if smaller else _most_frequent(self._all_sizes)
        return body

    def candidate(self, body: str, heading: str, size: int) -> str:
        candidate = _SizeCandidate(body)
        candidate.heading, candidate.size = heading, size
        return candidate

    def resolve(self, blocks: list[str]) -> list[str]:
        """The blocks with each candidate settled into a heading or left as body text (a plain `str`)."""
        if not any(type(block) is _SizeCandidate for block in blocks):
            return blocks
        titles = Counter(block.size for block in blocks if type(block) is _SizeCandidate)
        body = self.body_size(titles)
        settled = []
        for block in blocks:
            if type(block) is not _SizeCandidate:
                settled.append(block)
                continue
            level = 0 if self.outlined or body <= 0 else next(
                (level for ratio, level in _SIZE_HEADING_LEVELS if block.size >= body * ratio), 0)
            settled.append(f"{'#' * level} {block.heading}" if level else str(block))
        return settled


def _most_frequent(sizes: dict[int, int]) -> int:
    """The size most characters have, the smaller one when two are as frequent; 0 without any."""
    return max(sizes.items(), key=lambda item: (item[1], -item[0]))[0] if sizes else 0


def decorated_text(text: str, mark: str, level: int, style_name: str, margin: int, nesting: BulletNesting, *, in_cell: bool,
                   check: str = "", plain: str | None = None, outline: bool = False, size: int = 0,
                   size_headings: SizeHeadings | None = None, heading_text: Callable[[], str] | None = None,
                   top: bool = False, in_note: bool = False, no_heading: bool = False) -> str:
    """The first text piece of a paragraph with its head (`_lists.paragraph_head`) and, for an outline style or outline head, the
    heading mark.

    `mark` and `level` are the paragraph head the format read, `outline` whether that head is an outline head (개요 번호),
    `style_name` the name of the paragraph style, `margin` the left margin of the paragraph shape, `check` the check state of a
    check bullet (HWPX). `plain` is the text without link marks and addresses: whether the paragraph is a heading is decided on it,
    so a link does not change that (as in DOCX). `size` is the smallest character size of the paragraph's text when it is a
    top-level body paragraph (0 otherwise); with `size_headings` such a paragraph can become a heading by its size (`SizeHeadings`).
    `heading_text` gives the text written without bold and italic, used when the paragraph is a heading (a heading is bold by
    its style, as in DOCX).

    A paragraph with an outline head is a heading of its outline level + 1, as one in a 개요 style is: Hangul builds the outline
    (and the table of contents) from the head, and a real document put it on 바탕글 paragraphs, which came out as a numbered list
    (Pinned by: test_hwp_size_headings.py::TestOutlineHeads). The style decides the level when both are there, as before.
    An outline head makes a heading only in a top-level body paragraph (`top`); in a text box, a caption or a header it stays a
    number. No paragraph of a footnote or endnote (`in_note`) is a heading: `[^1]: # 1. 항목` is no Markdown heading and broke
    the note (Pinned by: test_hwp_size_headings.py::TestOutlineHeads::test_no_heading_in_a_note_or_text_box).
    No paragraph is a heading with `no_heading`, set for a header written without `include_headers_footers`
    (`header_by_default`): a header in a 개요 style made `# 시험합의서` the first heading of each such document (Pinned by:
    test_hwp_headers.py::TestHeadersByDefault::test_header_written_by_default_is_no_heading).
    An outline head on a paragraph that is a sentence (`_outline_title`) leaves it a numbered paragraph, and does not make the
    document one with outline headings: contracts and regulations number their clauses with outline heads (`1. 갑은 을에게 …
    지급한다.`, `가. 다만, … 아니하다.`) (Pinned by:
    test_hwp_size_headings.py::TestOutlineHeads::test_numbered_clause_sentences_stay_numbered_paragraphs).
    """
    plain = text if plain is None else plain
    match = None if in_cell or in_note or no_heading else OUTLINE_STYLE.match(style_name.strip())
    heading_level = int(match.group(1)) if match is not None else level + 1 if outline and top and _outline_title(plain) else 0
    fits = len(f"{mark} {plain}" if mark else plain) <= HEADING_MAX_CHARS and "\n" not in plain
    heading = bool(heading_level) and fits
    if heading and top and size_headings is not None:
        size_headings.outlined = True

    # The level of a bullet paragraph is set from its left margin compared with the preceding items (`BulletNesting`). Other
    # paragraphs close the more deeply indented items
    if mark == "-" and not heading:
        level = nesting.bullet(level, margin)
    else:
        nesting.other(margin)

    head = paragraph_head(mark, level, in_cell=in_cell, heading=heading) + check
    if heading:
        return f"{'#' * min(heading_level, 6)} {head}{text if heading_text is None else heading_text()}"
    if size and size_headings is not None and not in_cell and mark != "-" and _size_heading_text(plain):
        heading_head = paragraph_head(mark, level, in_cell=False, heading=True) + check
        return size_headings.candidate(head + text, heading_head + (text if heading_text is None else heading_text()), size)
    return head + text


class ParagraphPieces:
    """The blocks of one paragraph: its text pieces, and the tables and text boxes that stand between them.

    The paragraph head (number or bullet) and the outline heading mark are attached only to the first text piece of the paragraph.
    Text that continues after a table or text box is the rest of the same paragraph — it got the heading mark too and became
    '# 은 아래와 같다' ('# is as follows') (Pinned by: test_hwp_heads_and_shapes.py::TestOutlineParagraphSplitByATable).

    The extractor appends text to `buffer`, calls `flush` before it appends a table or text box to `blocks`, and takes the blocks
    from `finish`. `decorate` gives a text piece its head (`decorated_text`), from the piece and its text without link marks.

    A hyperlink is a `link_start` and a `link_end` piece around its text, written by the shared rules (`_inline.resolve_inline`);
    a link that does not end in the same piece of text (across a table, a text box or a paragraph end) stays text.
    """

    def __init__(self, decorate: Callable[[str, str, Callable[[], str]], str]) -> None:
        self.blocks: list[str] = []
        self.buffer: list[str] = []
        self._decorate = decorate
        self._headed = False
        # first piece emitted without a head, only placeholders. If the paragraph ends up with no text the number goes here
        self._bare: int | None = None

    def flush(self) -> None:
        buffer = self.buffer
        linked = any(type(piece) is _LinkMark for piece in buffer)
        text = "".join(piece if type(piece) is _LinkMark else piece.translate(_WITHOUT_INLINE_MARKS) for piece in buffer) if linked \
            else "".join(buffer)
        # Marks of neighbouring runs with the same emphasis are joined first, then strikethrough (`resolve_strikes`), scripts, and
        # links and emphasis last, as in DOCX. A heading is written without emphasis (`heading`)
        text = resolve_scripts(resolve_strikes(merge_emphasis_marks(text) if linked else text))
        plain = None
        marked = text
        if linked:
            plain = plain_inline(text).strip()
            text = resolve_inline(text)
        text = text.strip()
        buffer.clear()
        if not text:
            return
        text = self._unspaced(text)
        plain = None if plain is None else self._unspaced(plain)
        if self._headed:
            self.blocks.append(text)
        elif is_placeholder_only(text):
            self._bare = len(self.blocks) if self._bare is None else self._bare
            self.blocks.append(text)
        else:
            # The paragraph written without emphasis, for a heading: resolved only when it is one (most paragraphs are not)
            heading = (lambda: self._unspaced(resolve_inline(marked, emphasis=False).strip())) if linked else (lambda: text)
            self.blocks.append(self._decorate(text, text if plain is None else plain, heading))
            self._headed = True

    def _unspaced(self, text: str) -> str:
        """The written text with its letter-spaced labels joined (`unspaced_label`). On the written text: a cell bold throughout
        has lost its marks by now, and a line with marks or a link left in it is not a label (its words are not all syllables)."""
        if len(text) > _SPACED_LABEL_MAX_CHARS or "  " not in text:
            return text
        return "\n".join(unspaced_label(line) for line in text.split("\n"))

    def finish(self, head: Callable[[], tuple[str, int]], *, in_cell: bool) -> list[str]:
        """The blocks of the paragraph. `head` reads the paragraph head (mark, level); it is called only when no text piece took
        the head, because reading a numbered head counts the number."""
        self.flush()

        # A numbered paragraph holding only a picture also counts its number — otherwise the later numbers shift up. The heading mark and
        # bullet are not attached (they would be left alone).
        # A paragraph holding only a table or text box has no piece to attach to, so just the number goes on the line before it. Without
        # counting it, '3. 추진 일정' after '2. [table]' on the Hangul screen shifted up to '2.'
        # (Pinned by: test_hwp_object_only_numbers.py).
        if not self._headed and not any(self.blocks):
            # An empty numbered paragraph takes its number too, as Hangul draws it, and writes nothing — the rule of DOCX, ODT
            # and RTF. Not counted, the items after it came one number too low ('1. 하나', '2. 셋' where Hangul shows 3.)
            # (Pinned by: test_hwp_heads_and_shapes.py::TestEmptyNumberedParagraph).
            head()
        elif not self._headed:
            mark, level = head()
            number = paragraph_head(mark, level, in_cell=in_cell, heading=False) if mark != "-" else ""
            if self._bare is not None:
                self.blocks[self._bare] = number + self.blocks[self._bare]
            elif number:
                self.blocks.insert(0, number.rstrip())
        return self.blocks
