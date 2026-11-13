"""TXT/MD extraction and text encoding detection (shared with CSV and HTML). Unpacking the headers and bodies of MIME mail also
lives here (shared with HTML)."""

import base64
import binascii
import bisect
import codecs
from datetime import date, timedelta
import email
from email.errors import HeaderParseError
from email.header import Header, decode_header, make_header
from email.message import Message
import html
import itertools
import quopri
import re
import unicodedata

from charset_normalizer import CharsetMatch, CharsetMatches, from_bytes

from opendockit.exceptions import CorruptedDocumentError
from opendockit.extractors._shared import decimal_int
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


_BOMS = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    # The UTF-32 LE BOM (FF FE 00 00) starts with the UTF-16 LE BOM, so it is checked before UTF-16. BE (00 00 FE FF) starts with
    # a NUL and was rejected as binary (Pinned by: test_text_html.py::TestTolerantTextDetection::test_utf32_with_bom_is_text).
    # UTF-32 without a BOM is not checked.
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# Nearly every non-UTF-8 file uploaded in Korea is CP949 (a superset of EUC-KR).
_KOREAN_LEGACY = "cp949"
# The most common Western European single-byte encoding in old non-Korean documents (English + curly quotes, dashes, accents).
_WESTERN = "cp1252"
# The 2,350 precomposed Hangul syllables of KS X 1001. The 8,822 extended Hangul syllables CP949 added hardly ever appear in
# everyday text, and pour out when another encoding is wrongly decoded as CP949 (`Company’s` → `Company뭩`). When checking for
# garbled CP949, only these characters count as Hangul.
_EVERYDAY_HANGUL = frozenset(bytes((lead, trail)).decode("euc_kr") for lead in range(0xB0, 0xC9) for trail in range(0xA1, 0xFF))
# A non-ASCII byte not immediately followed by an ASCII letter. If there is none, every character CP949 decodes is of the form
# 'extended Hangul + a swallowed letter'.
_HIGH_BYTE_NOT_BEFORE_LETTER = re.compile(rb"[\x80-\xff](?![A-Za-z])")
# A run of three or more non-ASCII bytes. Accents and punctuation in Western European text sit one or two at a time between
# ASCII letters, while Cyrillic, Greek or Thai text and text in multi-byte encodings runs on for whole words.
_HIGH_BYTE_RUN = re.compile(rb"[\x80-\xff]{3}")
# HTML tags (left out of statistical detection) and a non-ASCII byte.
_MARKUP = re.compile(rb"<[^<>]*>")
_HIGH_BYTE = re.compile(rb"[\x80-\xff]")
# charset name declared by the document → the codec actually used. Read as the superset, as browsers do
# (EUC-KR → CP949, ISO-8859-1 → Windows-1252).
_DECLARED_CODECS = {"euc_kr": _KOREAN_LEGACY, "windows-949": _KOREAN_LEGACY, "x-windows-949": _KOREAN_LEGACY,
                    "iso8859-1": _WESTERN, "ascii": _WESTERN, "shift_jis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
                    "gb2312": "gbk"}
# Codec names that are not character encodings (escape, idna) or that depend on the runtime environment (mbcs, oem). Not used
# even when declared.
_NOT_A_CHARSET = re.compile(r"escape|idna|punycode|undefined|mbcs|oem")
# Codecs that carry other characters in 7-bit text through escape sequences (ESC, SO, SI; `+…-` for UTF-7). The bytes are pure
# ASCII, so strict UTF-8 decoding always takes them first — used ahead of UTF-8 only when declared (`decode_text`).
_SEVEN_BIT_CODECS = frozenset(("iso2022_jp", "iso2022_jp_1", "iso2022_jp_2", "iso2022_jp_2004", "iso2022_jp_3", "iso2022_jp_ext",
                               "iso2022_kr", "utf-7"))
# KS X 1001 8-byte filler composition (`_with_filler_syllables`): the filler character and the order of the initial, medial and
# final compatibility jamo.
_HANGUL_FILLER = "\u3164"
_INITIALS = "ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"
_MEDIALS = "ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"
_FINALS = "ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
_FILLER_SEQUENCE = re.compile("\u3164([\u3131-\u3163])([\u3131-\u3163])([\u3131-\u3164])")
# Tail of the encoding name when decoding inserted replacement characters (�).
_REPLACED = "(replaced)"
# Control characters common in text files (tab, line breaks, form feed, ESC). Allowed by the printable check of BOM-less UTF-16
# detection.
_TEXT_CONTROLS = "\t\n\r\f\v\x1b"


def decode_text(data: bytes, declared: str | None = None, *, markup: bool = False) -> tuple[str, str]:
    """Decode bytes to a string. Returns (string, encoding name). `declared` is the charset name the document itself states
    (HTML meta etc.).

    Order: BOM → strict UTF-8 → almost UTF-8 (only when the end is cut or very few bytes are broken; replacement) → declared
    charset, strict → BOM-less UTF-16 → strict CP949 → CP949 with replacement (only when very few bytes are broken)
    → statistical detection → UTF-8 with replacement characters. The name of an encoding decoded with replacement characters (�)
    gets `(replaced)` appended (`decoding_warning` turns it into a warning).
    Checking CP949 before statistical detection is deliberate: charset-normalizer sometimes misjudges short CP949 Hangul as
    UTF-16, Big5 etc. and emits unrelated characters in the Hangul syllable range. Strict CP949 decoding mostly fails on
    Latin-family text, so false positives are few (Shift_JIS, GB2312 and EUC-JP, which have the same byte structure, lean toward
    CP949 because Korean files come first here — Known accepted gap).

    Deliberate: if strict CP949 decoding succeeds, it is CP949 even without Hangul. When it was accepted only with Hangul, the
    symbols (㎡, ℃, ※, →) of mostly English/numeric CSVs saved by Korean Windows Excel were garbled into Johab/Big5 characters by
    statistical detection, and a CSV whose first Hangul appears after 20,000 characters got the `(replaced)` warning without any
    replacement character (Pinned by: test_text_html.py::TestLegacyKoreanWithoutHangulAtTheFront). However, if every non-ASCII
    byte sits only right before an ASCII letter (as CP949, all 'extended Hangul + a swallowed letter'), it is read as
    Windows-1252 — `Company’s` and `25°C` became `Company뭩` and `25캜` (Pinned by: TestWesternText). Known accepted gap: CP949
    text whose only non-ASCII characters are extended Hangul before a letter (`A햏B`) is read as Windows-1252.

    A declared charset is trusted only when strict decoding succeeds (UTF-8 labeled EUC-KR is caught first by strict UTF-8). Only
    Windows-1252/ISO-8859-1 declarations, which servers and editors attach by default, are checked after strict CP949 decoding —
    nearly every byte decodes strictly, so a CP949 page labeled ISO-8859-1 would be garbled as a whole
    (Pinned by: test_text_html.py::TestHtmlDeclaredCharset).

    `markup` means HTML: statistical detection runs on the text with the tags removed. The ASCII letters of the tags blur the
    language evidence, and a short Windows-1252 HTML was read as CP775 (`10€ café` → `10Ć cafķ`; the same text as .txt was read
    correctly. Pinned by: test_text_html.py::TestWesternHtmlWithoutAUsefulCharset). If non-ASCII bytes occur only inside tags,
    the whole sample is used as before.
    """
    for bom, encoding in _BOMS:
        if data.startswith(bom):
            return data.decode(encoding, errors="replace"), encoding

    # 7-bit text declared as ISO-2022-JP, ISO-2022-KR or UTF-7 was taken by strict UTF-8 decoding and became `$B2q5D…(B` with the
    # escape sequences erased (Pinned by: test_core_mailboxes_and_charsets.py::TestSevenBitEscapeCharsets). Used only when it
    # decodes strictly as declared and no control character foreign to text remains — when a raw `+` is mixed into a UTF-7
    # declaration (`a+b`), strict decoding fails and it goes on to the steps below.
    seven_bit = _seven_bit_codec(declared)
    if seven_bit is not None and data.isascii():
        text = _strict(data, seven_bit)
        if text is not None and all(_printable(ch) for ch in text if not ch.isascii()) and "\x1b" not in text:
            return text, seven_bit

    text = _strict(data, "utf-8")
    if text is not None:
        return text, "utf-8"
    # UTF-8 with its last character cut or a single byte broken is garbled as a whole if passed to the steps below (text of 45
    # bytes or less became ptcp154/cp875 in statistical detection). Only when everything else decodes as UTF-8 multi-byte
    # characters is that spot left as a replacement character (Pinned by: test_text_html.py::TestAlmostUtf8).
    if _almost_utf8(data):
        return data.decode("utf-8", errors="replace"), f"utf-8{_REPLACED}"
    codec = _declared_codec(declared)
    if codec not in (None, _WESTERN):
        text = _strict(data, codec)
        if text is not None:
            return (_with_filler_syllables(text) if codec == _KOREAN_LEGACY else text), codec

    utf16 = utf16_encoding(data)
    if utf16:
        return data[:len(data) - len(data) % 2].decode(utf16), utf16
    text = _strict(data, _KOREAN_LEGACY)
    if text is not None:
        western = None if _HIGH_BYTE_NOT_BEFORE_LETTER.search(data) else _strict(data, _WESTERN)
        return (_with_filler_syllables(text), _KOREAN_LEGACY) if western is None else (western, _WESTERN)
    if codec == _WESTERN:
        text = _strict(data, _WESTERN)
        if text is not None:
            return text, _WESTERN

    # CP949 whose strict decoding failed because the last character is cut, or because of a single user-defined-area character
    # (0xC9A1 etc.), is garbled as a whole into Japanese/Thai characters if passed to statistical detection. When very few bytes
    # are broken and most of the text is Hangul, only those spots become replacement characters
    # (Pinned by: test_text_html.py::TestDamagedLegacyKorean).
    text = data.decode(_KOREAN_LEGACY, errors=_CP949_PAIR_REPLACE)
    if _mostly_korean_with_few_replacements(text) or _korean_cut_at_the_end(data):
        return _with_filler_syllables(text), f"{_KOREAN_LEGACY}{_REPLACED}"

    # Statistical detection looks only at the first 200KB. Dropping the guess because later bytes cannot be decoded in that
    # encoding would garble even the front part with UTF-8 replacement. The guess is kept and only the undecodable bytes become
    # replacement characters.
    sample = data[:200_000]
    if markup:
        text_only = _MARKUP.sub(b" ", sample)
        sample = text_only if _HIGH_BYTE.search(text_only) else sample
    matches = from_bytes(sample)
    best = matches.best()
    if _reads_as_western(data, matches, best):
        return data.decode(_WESTERN), _WESTERN
    if best is not None and best.encoding:
        try:
            return data.decode(best.encoding), best.encoding
        except UnicodeDecodeError:
            return data.decode(best.encoding, errors="replace"), f"{best.encoding}{_REPLACED}"
        except LookupError:
            pass
    return data.decode("utf-8", errors="replace"), f"utf-8{_REPLACED}"


def _strict(data: bytes, encoding: str) -> str | None:
    """The string if it decodes strictly as `encoding`, otherwise None."""
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _declared_codec(name: str | None) -> str | None:
    """charset name declared by the document → Python codec name. None for unknown names and for the UTF family (already decided
    by BOM, strict decoding and NUL byte positions)."""
    label = (name or "").strip().lower()
    if not label or _NOT_A_CHARSET.search(label):
        return None
    try:
        codec = _DECLARED_CODECS.get(label) or codecs.lookup(label).name
    except (LookupError, ValueError):  # a name with a NUL byte in it (`utf-8\x00`) is a ValueError — the whole mail was rejected
        return None
    codec = _DECLARED_CODECS.get(codec, codec)
    return None if codec.startswith("utf") else codec


def _seven_bit_codec(name: str | None) -> str | None:
    """The codec name if the declared charset is a 7-bit escape codec (`_SEVEN_BIT_CODECS`), otherwise None."""
    try:
        codec = codecs.lookup(name.strip()).name if name else None
    except (LookupError, ValueError):
        return None
    return codec if codec in _SEVEN_BIT_CODECS else None


def _reads_as_western(data: bytes, matches: CharsetMatches, best: CharsetMatch | None) -> bool:
    """Is this text to read as Windows-1252 instead of by statistical detection: non-ASCII bytes are sparse (never three or more
    in a row), it decodes strictly as Windows-1252, and the top guess is not ahead of the Windows-1252 candidate in language
    evidence (coherence).

    For text with only a few accents or punctuation marks, charset-normalizer picks the first by name among equally scored
    candidates and read `naïve façade` as CP1257 (`naļve faēade`), `price €20` as CP437 and `25°C` as CP932. Text with clear
    language evidence (long Polish, Czech or Turkish text) follows the guess (Pinned by: test_text_html.py::TestWesternText).
    """
    if _HIGH_BYTE_RUN.search(data) or _strict(data, _WESTERN) is None:
        return False
    western = next((match for match in matches if _WESTERN in (match.encoding, *match.could_be_from_charset)), None)
    return best is None or best.coherence <= (western.coherence if western is not None else 0.0)


def _replace_double_byte(error: UnicodeDecodeError) -> tuple[str, int]:
    """Replace an undecodable CP949 double-byte character (lead 0x81~0xFE, trail 0x41 or above) with one replacement character.

    The default `replace` skips only one byte, so the trail byte paired up with the lead byte of the next character and
    unrelated characters followed.
    """
    data, start = error.object, error.start
    if 0x81 <= data[start] <= 0xFE and start + 1 < len(data) and data[start + 1] >= 0x41:
        return "\ufffd", start + 2
    return "\ufffd", start + 1


_CP949_PAIR_REPLACE = "opendockit.cp949_pair_replace"
codecs.register_error(_CP949_PAIR_REPLACE, _replace_double_byte)


def decoding_warning(encoding: str) -> str | None:
    """The warning to show the user if `decode_text` decoded with replacement characters, otherwise None."""
    if encoding == f"utf-8{_REPLACED}":
        return "The character encoding could not be determined, so some characters may be garbled."
    if encoding.endswith(_REPLACED):
        return f"The text was read as {encoding.removesuffix(_REPLACED).upper()}, but some bytes could not be decoded and were replaced with �."
    return None


def decoding_warnings(encodings: list[str]) -> list[str]:
    """The `decoding_warning` of the encodings used for several parts (mail body, forwarded mails), each warning once."""
    return list(dict.fromkeys(warning for encoding in encodings if (warning := decoding_warning(encoding))))


def _mostly_korean_with_few_replacements(text: str) -> bool:
    """Is there at most 1 replacement character per 200 characters, and is at least half of the non-ASCII characters everyday
    Hangul (KS X 1001 precomposed)?

    Latin-family text (é, ü) decoded as CP949 has many replacement characters, or mixes in hanja and symbols that are not Hangul
    and rare Hangul of the extended area, and is filtered out here.
    """
    replaced = text.count("\ufffd")
    non_ascii = sum(1 for ch in text if ord(ch) > 0x7F and ch != "\ufffd")
    hangul = sum(1 for ch in text if ch in _EVERYDAY_HANGUL)
    return replaced <= max(1, len(text) // 200) and hangul >= 10 * replaced and hangul * 2 >= non_ascii


def _korean_cut_at_the_end(data: bytes) -> bool:
    """Is this short CP949 text with only its final double-byte character cut: the last byte is the lead byte of a double-byte
    character, what precedes it decodes strictly as CP949 (the cut lead byte is in the Hangul range too), and the non-ASCII
    characters are all everyday Hangul (KS X 1001 precomposed) and number at least 2.

    The '10 Hangul per replacement character' condition of `_mostly_korean_with_few_replacements` filters out short text, so
    `결재 완료` with one byte cut off was garbled as a whole into cp874/utf-16 characters by statistical detection. Other
    encodings (Cyrillic, Greek, Western European, Japanese) are filtered out because the lead byte is outside the KS X 1001
    Hangul range or they decode to extended Hangul or hanja (Pinned by: test_text_html.py::TestShortDamagedLegacyKorean).
    """
    if len(data) < 5 or not 0xB0 <= data[-1] <= 0xC8:  # only when the cut character is Hangul too (a KS X 1001 Hangul lead byte)
        return False
    head = _strict(data[:-1], _KOREAN_LEGACY)
    if head is None:
        return False
    non_ascii = [ch for ch in head if ord(ch) > 0x7F]
    return len(non_ascii) >= 2 and all(ch in _EVERYDAY_HANGUL for ch in non_ascii)


def _almost_utf8(data: bytes) -> bool:
    """Are bytes that failed strict decoding 'almost UTF-8': only the final multi-byte character is cut and at least two
    multi-byte characters precede it, or there is at most 1 replacement character per 200 characters and at least 10 multi-byte
    characters per replacement character.

    CP949, Shift_JIS and Latin-1 text decoded as UTF-8 yields a replacement character for every character and is filtered out
    here. Text without multi-byte characters (a single CP949 hanja after ASCII) is not seen as cut UTF-8.
    """
    text = data.decode("utf-8", errors="replace")
    replaced = text.count("\ufffd")
    multibyte = sum(1 for ch in text if ord(ch) > 0x7F) - replaced
    if is_utf8_text(data):  # strict decoding failed, so only the last character is cut
        return multibyte >= 2
    return replaced <= max(1, len(text) // 200) and multibyte >= 10 * replaced


def is_utf8_text(sample: bytes) -> bool:
    """Does the head decode strictly as UTF-8. A multi-byte character cut at the end of the sample is allowed."""
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def is_cp949_text(sample: bytes) -> bool:
    """Does the head decode strictly as CP949. A double-byte character cut at the end of the sample is allowed."""
    try:
        codecs.getincrementaldecoder(_KOREAN_LEGACY)().decode(sample, final=False)
    except UnicodeDecodeError:
        return False
    return True


def unicode_bom_codec(data: bytes) -> str | None:
    """The codec name ("utf-16", "utf-32") if the data starts with a UTF-16/UTF-32 BOM, otherwise None. NUL bytes are part of the
    text in these formats, so they are left out of binary detection."""
    for bom, encoding in _BOMS:
        if encoding != "utf-8-sig" and data.startswith(bom):
            return encoding
    return None


def _with_filler_syllables(text: str) -> str:
    """Combine the 8-byte filler composition of KS X 1001 (filler U+3164 + initial, medial and final jamo, with the filler in an
    empty position) into one syllable.

    It is the standard way to write a syllable missing from the precomposed set ('똠'), but it decoded into four jamo ('ㅤㄸㅗㅁ')
    (Pinned by: test_core_th_rows_front_tables_and_linear_time.py::TestEucKrFillerSyllables). Left as is when the initial or
    medial is empty or is a jamo that cannot stand in that position.
    """
    if _HANGUL_FILLER not in text:
        return text

    def syllable(match: re.Match[str]) -> str:
        initial, medial, final = match.groups()
        if initial not in _INITIALS or medial not in _MEDIALS or (final != _HANGUL_FILLER and final not in _FINALS):
            return match.group()
        coda = 0 if final == _HANGUL_FILLER else _FINALS.index(final) + 1
        return chr(0xAC00 + (_INITIALS.index(initial) * 21 + _MEDIALS.index(medial)) * 28 + coda)

    return _FILLER_SEQUENCE.sub(syllable, text)


def utf16_encoding(data: bytes) -> str | None:
    """Return "utf-16-le"/"utf-16-be" for BOM-less UTF-16, otherwise None.

    UTF-16 text saved without a BOM by Windows tools was rejected as binary because of its NUL bytes. The byte order candidates
    are ordered by the NUL byte positions of ASCII characters (space, line break, digits), and the data counts as UTF-16 only
    when it decodes strictly and consists entirely of printable characters (common control characters such as \\f and ESC, and
    space and format characters, are allowed — `_printable`). With an odd length the last byte is dropped first. Arbitrary binary
    data mixes in unassigned and private-use characters and fails the printable check. UTF-8/CP949 text in which NUL bytes are
    rare (a note with one stray NUL byte) is not seen as UTF-16 — CP949 text paired two bytes at a time sometimes decodes to
    printable characters, and 9 of 2,000 random Hangul texts were garbled as UTF-16
    (Pinned by: test_text_html.py::TestTolerantTextDetection, TestLegacyTextWithNulBytes).
    """
    sample = data[:8192]
    nul_count = sample.count(0)
    if not nul_count or (nul_count * 10 < len(sample) and (is_utf8_text(sample) or is_cp949_text(sample.replace(b"\x00", b"")))):
        return None
    data = data[:len(data) - len(data) % 2]
    sample = data[:8192]
    little = sum(1 for index in range(1, len(sample), 2) if sample[index] == 0 and sample[index - 1] != 0)
    big = sum(1 for index in range(0, len(sample) - 1, 2) if sample[index] == 0 and sample[index + 1] != 0)
    candidates = ("utf-16-le", "utf-16-be") if little >= big else ("utf-16-be", "utf-16-le")

    for encoding in candidates:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        head = text[:20_000]
        if any(ch.isalnum() for ch in head) and all(_printable(ch) for ch in head):
            return encoding
    return None


def _printable(ch: str) -> bool:
    """Is this a character that can appear in text, for BOM-less UTF-16 detection: printable characters, common control
    characters (`_TEXT_CONTROLS`), spaces (Zs) and format characters (Cf — zero-width space, BOM, direction marks).

    `str.isprintable` regards spaces other than the plain space (NBSP, ideographic space U+3000) and format characters (U+200B)
    as unprintable, so a note or CSV with even one such character was rejected as binary
    (Pinned by: test_core_text_and_mail.py::TestUtf16WithoutBomAndSpaceCharacters). Filtering out arbitrary binary data is
    unchanged — paired two bytes at a time, it mixes in unassigned, private-use and surrogate characters and control characters.
    """
    return ch in _TEXT_CONTROLS or ch.isprintable() or unicodedata.category(ch) in ("Zs", "Cf")


# Two or more NUL bytes in a row. Where a fixed-width export padded its columns.
_NUL_PADDING = re.compile("\x00{2,}")
# Terminal control sequences. CSI (ESC [ parameters intermediates final — color `ESC[31m`, `ESC[38:2:1:2:3m`, erase line
# `ESC[2K`, cursor `ESC[?25l`), OSC such as the window title (`ESC]0;title` + BEL or ESC \; not removed when unterminated — so it
# does not swallow text across lines), DCS/PM/APC strings (ended by ESC \), character set designation (the `ESC(B` of
# `tput sgr0`), two-byte commands (reset `ESC c`, cursor save/restore `ESC 7`/`ESC 8`, keypad `ESC =`/`ESC >`,
# `ESC D`, `ESC E`, `ESC H`, `ESC M`, string terminator `ESC \`). When only CSI was removed, `(B`, `]0;title` and `c` stayed in
# the text (Pinned by: test_core_round44.py::TestTerminalEscapesOtherThanCsi).
_TERMINAL_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\n]*(?:\x07|\x1b\\)|[P^_][^\x1b\n]*\x1b\\|[()*+][0-9A-Za-z]|[78=>cDEHM\\])")
# Number of names written in the mail attachment warning (beyond it 'and N more'; the same number as the PDF attachment warning).
_ATTACHMENT_NAMES_SHOWN = 10


# The header line names of the MIME header block (`mime_header_block`) and the headers used to identify mail. MHTML detection for
# HTML (`html.is_mhtml`) looks at the same block.
# The range searched for the header block. When it was 8KB, Office 365 mail whose headers exceed 8KB with Received, ARC, DKIM and
# X-MS-Exchange lines was not identified as mail and became text, base64 body and all
# (Pinned by: test_core_mail_and_detect.py::TestMailWithALongHeaderBlock). The block is walked only once, line by line, so
# widening the range stays linear.
MIME_HEAD_BYTES = 256 * 1024
_MIME_HEADER_NAME = re.compile(rb"[!-9;-~]+:")
# The mbox separator line (`From - Thu Mar 07 10:15:00 2024`, `From kim@example.com Mon Sep  1 10:00:00 2025`). It stays at the
# very top of an .eml saved by Thunderbird etc. Only lines with a time (`10:15`) after the sender position count — text that
# starts with 'From the start …' does not.
_MBOX_FROM_LINE = re.compile(rb"From \S+ [^\r\n]*\d:\d\d")
# `From ` at the start of a line — a candidate separator line for the next mail of an mbox archive (`mail_box`).
_LINE_START_FROM = re.compile(rb"^From ", re.MULTILINE)
# The `>` mbox put before a body line starting with `From ` (mboxrd adds one more to `>From` lines too). Only one is stripped
# (`_unescaped_from_lines`).
_ESCAPED_FROM_LINE = re.compile(rb"^>(>*From )", re.MULTILINE)
# Upper limit on the number of mails read from an mbox archive (`mail_box`) and the heading text before each mail.
MAX_BOX_MAILS = 100
_MAIL_BOX_TITLE = "Message"
MIME_VERSION = re.compile(rb"^MIME-Version:", re.IGNORECASE | re.MULTILINE)
# Mail that carries a body: multipart (related is MHTML — `html.is_mhtml`) or a single plain-text part.
_MAIL_CONTENT_TYPE = re.compile(rb"^Content-Type:\s*(?:multipart/(?!related\b)[\w.+-]+|text/plain)\b", re.IGNORECASE | re.MULTILINE)
# Identifying mail without MIME-Version (`_mail_without_mime_version`): the sender, a recipient/subject/date, and a MIME body
# header (a mail Content-Type — related and HTML included — or Content-Transfer-Encoding).
_MAIL_SENDER = re.compile(rb"^From:", re.IGNORECASE | re.MULTILINE)
_MAIL_ADDRESSING = re.compile(rb"^(?:To|Cc|Subject|Date):", re.IGNORECASE | re.MULTILINE)
_MAIL_BODY_HEADER = re.compile(rb"^(?:Content-Type:\s*(?:multipart/[\w.+-]+|text/(?:plain|html))\b|Content-Transfer-Encoding:)",
                               re.IGNORECASE | re.MULTILINE)
# The top-level Content-Type of encrypted mail (`is_encrypted_mail`): S/MIME (application/pkcs7-mime, except the signature-only
# signed-data and certs-only) and PGP/MIME (multipart/encrypted).
_ENCRYPTED_CONTENT_TYPE = re.compile(
    rb"^Content-Type:\s*(?:multipart/encrypted\b|application/(?:x-)?pkcs7-mime\b(?![^\r\n]*smime-type\s*=\s*\"?(?:signed-data|certs-only)))",
    re.IGNORECASE | re.MULTILINE)
# RFC 2047 encoded words (`=?charset?b?…?=`). In a header mixed with 8-bit text only the words are decoded (`_decoded_header`).
_ENCODED_WORD = re.compile(r"(=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=)")
# The mail headers written out before the body.
_MAIL_HEADERS = ("Subject", "From", "To", "Cc", "Date")
# The sender position of a web page saved by a browser (MHTML): `<Saved by Windows Internet Explorer 8>`, `<Saved by Blink>`.
_SAVED_BY = re.compile(r"\s*<?\s*Saved by\b", re.IGNORECASE)


def mime_header_block(head: bytes) -> bytes | None:
    """The MIME header block at the very start of `head` (through the blank line that ends it), otherwise None. Each line is
    looked at only once (linear time).

    A header line is `name: value` (the name is ASCII without a colon or whitespace), a folded line starts with a space or tab,
    and the first line must be a header line. Lines end with `\\n` or `\\r\\n`, and the block ends with an empty line (a line of
    only whitespace is a folded line). None if any other line intervenes or it ends without a blank line.
    """
    end = _header_block_end(head, 0, len(head))
    return None if end is None else head[:end]


def _header_block_end(data: bytes, start: int, limit: int) -> int | None:
    """The position where the MIME header block (`mime_header_block`) at the very start of `data[start:limit]` ends (after the
    blank line), otherwise None. It looks in place without slicing a copy — an mbox archive calls this for every candidate
    separator line (`mail_box`)."""
    position = start
    while True:
        newline = data.find(b"\n", position, limit)
        if newline < 0:
            return None
        line = data[position:newline].removesuffix(b"\r")
        if not line:
            return newline + 1 if position > start else None
        folded = position > start and line[:1] in (b" ", b"\t")
        if b"\r" in line or not (folded or _MIME_HEADER_NAME.match(line)):
            return None
        position = newline + 1


def mime_start(data: bytes) -> int:
    """Where the MIME header block would start: after the BOM and leading whitespace, and, if what follows is an mbox separator
    line (`_MBOX_FROM_LINE`) with a header block from the next line on, after that line. Mail detection (`mime_mail`) and MHTML
    detection (`html.is_mhtml`) look for the block at the same position.

    The `From - Thu Mar 07 10:15:00 2024` line at the very top of an .eml saved by Thunderbird etc. is not shaped like a header
    line, so the mail became text, base64 body and all (Pinned by: test_core_mail_and_detect.py::TestMailWithAnMboxFromLine).
    It is not skipped unless a header block follows.
    """
    start = _content_start(data)
    after = _after_mbox_from_line(data, start)
    return start if after is None else after


def _content_start(data: bytes) -> int:
    """The position after the BOM and leading whitespace."""
    head = data[:MIME_HEAD_BYTES]
    start = len(codecs.BOM_UTF8) if head.startswith(codecs.BOM_UTF8) else 0
    return len(head) - len(head[start:].lstrip())


def _after_mbox_from_line(data: bytes, start: int) -> int | None:
    """If the line at `start` is an mbox separator line (`_MBOX_FROM_LINE`) and a MIME header block starts on the next line, the
    position of that next line, otherwise None."""
    newline = data.find(b"\n", start, start + MIME_HEAD_BYTES)
    if newline < 0 or not _MBOX_FROM_LINE.match(data, start, newline):
        return None
    after = newline + 1
    return after if _header_block_end(data, after, after + MIME_HEAD_BYTES) is not None else None


def mime_mail(data: bytes) -> Message | None:
    """The message if this is a MIME mail whose leading header block has MIME-Version and a mail Content-Type (a multipart other
    than multipart/related, or text/plain) (.eml, or .mht of the same structure), or a mail without MIME-Version
    (`_mail_without_mime_version`), otherwise None.

    Deliberate: it is read as mail whatever the transfer encoding (base64, quoted-printable, 7bit, 8bit). Previously only mail
    with at least one base64/quoted-printable part was unpacked and the rest was left as text, headers and all, as 'readable
    plain-text mail'; but in 7bit/8bit mail (the Thunderbird default, Gmail's ASCII mail) the MIME boundaries and part headers
    remained, RFC 2047 subjects (`=?utf-8?b?…?=`) were not decoded and the same body of multipart/alternative appeared twice
    (Pinned by: test_core_mail_and_detect.py::TestMailWithoutTransferEncoding). For transfer-encoded mail the base64 itself
    became a 'successful' result
    (Pinned by: test_text_html.py::TestEncodedMail, test_core_mail_and_detect.py::TestPlainMailWithAnEncodedAttachment).
    Mail that omits the MIME-Version line (old groupware, mail sent by scripts) is seen as mail only when the sender, a recipient
    (or subject/date) and a MIME body header are all present (`_mail_without_mime_version`). It used to be text, encoded subject
    and base64 body and all (Pinned by: test_core_mailboxes_and_charsets.py::TestMailWithoutMimeVersion). In that case MHTML
    detection (which needs MIME-Version) does not take it, so text/html and multipart/related are accepted too.
    """
    start = mime_start(data)
    headers = mime_header_block(data[start:start + MIME_HEAD_BYTES])
    if headers is None:
        return None
    if MIME_VERSION.search(headers) is not None:
        if _MAIL_CONTENT_TYPE.search(headers) is None:
            return None
    elif not _mail_without_mime_version(headers):
        return None
    return parse_mail(data[start:])


def parse_mail(data: bytes) -> Message:
    """Read MIME mail bytes into a message. If the multipart boundaries nest deeper than `_MAX_MIME_NESTING` levels it is
    rejected as a corrupted document before parsing (`_mime_nesting_exceeds`), and after reading, multiparts without parts and
    delivery failure reports are turned into text parts (`_readable_parts`).

    The standard library parser checks every open boundary on every line, so 900 levels + a body of 200,000 lines (438KB) took
    18 seconds (lines × depth; beyond 1000 levels it was 'corrupted' anyway through a recursion limit error.
    Pinned by: test_core_malformed_mail_and_html.py::TestDeeplyNestedMultipart). Even at the limit it costs lines × limit —
    32 levels + 2 million lines (4MB) take 5.8 seconds (1 level takes 1.2 seconds).
    """
    if _mime_nesting_exceeds(data, _MAX_MIME_NESTING):
        raise CorruptedDocumentError(f"Mail MIME parts nested deeper than {_MAX_MIME_NESTING} levels cannot be read. The file may be corrupted.")
    message = email.message_from_bytes(data)
    _readable_parts(message)
    return message


def _mime_nesting_exceeds(data: bytes, limit: int) -> bool:
    """Does the nesting of multipart boundaries exceed `limit` levels. It scans once over only the line-start `--boundary` lines
    and the `boundary=` of Content-Type headers (continuation lines included): a declaration is pushed as an open boundary, a
    delimiter line of an open boundary closes the boundaries inside it (as the parser does), and a closing line (`--boundary--`)
    closes everything up to that boundary. A `boundary=` written in body text is not a Content-Type line and is not counted.
    Known accepted gap: a part that ends without a closing line stays open and is counted deeper than it really is (sibling
    parts are closed by the next delimiter line)."""
    stack: list[bytes] = []
    depth: dict[bytes, int] = {}  # open boundary → its position in the stack

    for match in _MIME_NESTING_LINE.finditer(data):
        boundary = match.group("declared_quoted") or match.group("declared")
        if boundary is not None:
            if boundary not in depth:
                depth[boundary] = len(stack)
                stack.append(boundary)
                if len(stack) > limit:
                    return True
            continue
        line = match.group("delimiter").rstrip()
        closing = line.endswith(b"--") and line[:-2] in depth
        boundary = line[:-2] if closing else line
        if boundary not in depth:
            continue
        for opened in stack[depth[boundary] + (0 if closing else 1):]:
            del depth[opened]
        del stack[len(depth):]
    return False


def _readable_parts(message: Message) -> None:
    """Turn parts that have text to read but are not seen as body parts into text/plain: a multipart that was not split into
    parts because it has no boundary or the boundary never occurs in the body (its body is the text), and the
    message/delivery-status of a delivery failure report (recipient, status code and diagnostics as `name: value` lines).

    Neither is caught on the path that finds body parts (text/plain, text/html), so the body or the failure cause vanished
    without a warning and only the headers remained
    (Pinned by: test_core_malformed_mail_and_html.py::TestMultipartWithoutUsableBoundary).
    """
    for part in _mail_parts(message):
        if part.get_content_type() == "message/delivery-status" and part.is_multipart():
            fields = ["\n".join(f"{name}: {' '.join(str(value).split())}" for name, value in block.items())
                      for block in part.get_payload() if isinstance(block, Message)]
            del part["Content-Transfer-Encoding"]
            part.set_payload("\n\n".join(field for field in fields if field).encode())
            part.set_type("text/plain")
            part.set_param("charset", "utf-8")
        elif part.get_content_maintype() == "multipart" and not part.is_multipart():
            part.set_type("text/plain")


def _mail_without_mime_version(headers: bytes) -> bool:
    """Is a header block without MIME-Version a mail: when the sender (From), one of recipient/subject/date, and a MIME body
    header (a mail Content-Type or Content-Transfer-Encoding) are all present. Conservative — a note that starts with `From:`,
    `To:`, `Subject:` has no body header and stays text."""
    return all(pattern.search(headers) is not None for pattern in (_MAIL_SENDER, _MAIL_ADDRESSING, _MAIL_BODY_HEADER))


def is_encrypted_mail(data: bytes) -> bool:
    """Is the leading header block an encrypted mail (S/MIME enveloped-data, PGP/MIME multipart/encrypted). Only blocks with
    MIME-Version or a sender are considered.

    S/MIME encrypted mail was not identified as mail and was a 'successful' result of headers and base64 ciphertext, and
    PGP/MIME kept only the headers (Pinned by: test_core_mailboxes_and_charsets.py::TestEncryptedMail). Signature-only mail
    (multipart/signed, smime-type=signed-data) does not count.
    """
    start = mime_start(data)
    headers = mime_header_block(data[start:start + MIME_HEAD_BYTES])
    if headers is None or (MIME_VERSION.search(headers) is None and _MAIL_SENDER.search(headers) is None):
        return False
    return _ENCRYPTED_CONTENT_TYPE.search(headers) is not None


def mail_box(data: bytes) -> tuple[list[Message], int]:
    """(the mails to read, the number of mails contained) if this is mail (`mime_mail`), otherwise ([], 0). A file that starts
    with an mbox separator line is split into mails at every line-start separator line (one followed by a header block,
    `_after_mbox_from_line`) and the first `MAX_BOX_MAILS` are read (mbox archive).

    Of an mbox holding several mails only the first was read: with multipart mail the later mails were discarded as the epilogue
    of the first, and with plain-text mail the separator lines and headers of the later mails became body
    (Pinned by: test_core_mail_parts_and_icons.py::TestMailboxWithSeveralMails). Deliberate: an .eml that does not start with a
    separator line is not split — mbox rewrites a 'From …hh:mm' line inside a body as `>From`, so it is a separator line only in
    an archive. When the file splits into two or more, it is split even if the first mail does not pass mail detection
    (`mime_mail`) (two or more of separator line + header block make an archive): when the first mail was a single HTML part,
    multipart/related or an old mail without MIME headers, the later mails were appended as raw text or vanished (Pinned by:
    test_core_mailboxes_and_charsets.py::TestMailboxThatStartsWithAnHtmlMail, TestMailWithoutMimeVersion). An archive is read
    only after it is split — read whole as one mail, the boundaries of mails without closing boundaries pile up between the
    mails, and from the 33rd mail on the whole archive was rejected as 'nested deeper than 32 levels'
    (Pinned by: test_core_mbox_markdown_and_api.py::TestMailboxOfMailsWithoutClosingBoundaries).
    """
    first = _after_mbox_from_line(data, _content_start(data))
    bounds = [] if first is None else [(match.start(), after) for match in _LINE_START_FROM.finditer(data, first)
                                       if (after := _after_mbox_from_line(data, match.start())) is not None]
    if not bounds:
        mail = mime_mail(data if first is None else _unescaped_from_lines(data))
        return ([mail], 1) if mail is not None else ([], 0)

    starts = [first, *(after for _, after in bounds)]
    ends = [line for line, _ in bounds] + [len(data)]
    mails = [parse_mail(_unescaped_from_lines(data[start:end])) for start, end in zip(starts[:MAX_BOX_MAILS], ends, strict=False)]
    return mails, len(starts)


def _unescaped_from_lines(data: bytes) -> bytes:
    """In the mails of a file that starts with an mbox separator line (an archive, or an .eml saved by Thunderbird), strip one
    `>` that mbox put before a body line starting with `From ` (the mboxrd rule). Called after the separator lines have been
    found — a stripped line must not look like the separator line of the next mail.

    `>From here` stayed in the body as is
    (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestMailboxFromLineEscapes).
    Known accepted gap: in an old mboxo archive, which does not rewrite `>From` lines, a quoted line that was `>From` to begin
    with loses one `>` as well — the two rules cannot be told apart from the file alone, and without stripping, the common case
    (`From ` in a body) would always be wrong. An .eml without a separator line is not changed.
    """
    return _ESCAPED_FROM_LINE.sub(rb"\1", data)


def mail_box_warning(read: int, total: int) -> str | None:
    """The warning if the mails of an mbox archive were not all read because of the limit (`MAX_BOX_MAILS`)."""
    if read >= total:
        return None
    return f"The mailbox (mbox) holds {total:,} messages, but only the first {read:,} were read. Split the rest off and convert it separately."


def mail_box_title(number: int) -> str:
    """The heading text placed before the `number`-th mail of an mbox archive (`## Message N`). Text and HTML (`<h2>`) mail use
    the same text."""
    return f"{_MAIL_BOX_TITLE} {number}"


def is_sent_mail(message: Message) -> bool:
    """Is this a message with mail headers: it has recipients (To, Cc), or its sender is not a browser's saved-by mark
    (`<Saved by …>`).

    MHTML detection (`html.is_mhtml`) also takes mail with only a single text/html part and HTML mail with embedded pictures
    (multipart/related). The subject, sender and date of such mail vanished
    (Pinned by: test_core_mail_and_detect.py::TestSinglePartHtmlMail). A web page saved by IE, Chrome or Word has no recipient,
    and its sender is absent or `<Saved by …>`.
    """
    if message.get("To") or message.get("Cc"):
        return True
    sender = message.get("From")
    return bool(sender) and _SAVED_BY.match(str(sender)) is None


# The heading text placed before a forwarded mail (message/rfc822 part). Text (`extract_text`) and HTML (`html.extract_html`)
# mail use the same text.
FORWARDED_MAIL_TITLE = "Forwarded message"
# Upper limit on the number of forwarded mails read. Those beyond it are only named in a warning, like attachments (not reached
# unless a mail has been forwarded over and over).
_MAX_FORWARDED_MAILS = 20


def mail_messages(message: Message) -> tuple[list[Message], list[str]]:
    """The mail and the mails forwarded 'as attachment' inside it (message/rfc822 parts) in document order (a forward inside a
    forwarded mail comes right after that mail), and the names of the forwarded mails not read because they are beyond the limit
    (`_MAX_FORWARDED_MAILS`) (forwards inside those are not counted).

    Deliberate: forwarded mails are read too and placed after the outer body. The forwarder's own text
    ('아래 메일 확인 바랍니다', "please see the mail below") does not give an LLM the context. Previously the walk that finds
    body parts (`Message.walk`) went down into forwarded mails, and the HTML body of a forwarded mail pushed out the outer
    plain-text body (Pinned by: test_core_text_and_mail.py::TestForwardedMail).
    """
    messages: list[Message] = []
    skipped: list[str] = []
    stack = [message]

    while stack:
        current = stack.pop()
        if len(messages) > _MAX_FORWARDED_MAILS:
            skipped.append(_mail_name(current))
            continue
        messages.append(current)
        forwarded = [inner for part in _mail_parts(current) if part is not current
                     and (inner := _forwarded_message(part)) is not None]
        stack.extend(reversed(forwarded))
    return messages, skipped


def _mail_parts(message: Message) -> list[Message]:
    """The parts of a mail (itself included) in document order. It does not go down into forwarded mails (message/*) —
    `mail_messages` reads those separately."""
    parts: list[Message] = []
    stack = [message]

    while stack:
        part = stack.pop()
        parts.append(part)
        if part.is_multipart() and part.get_content_maintype() != "message":
            stack.extend(reversed([child for child in part.get_payload() if isinstance(child, Message)]))
    return parts


def _forwarded_message(part: Message) -> Message | None:
    """The mail inside if the part is message/rfc822, otherwise None. One wrapped in a transfer encoding (base64,
    quoted-printable) is decoded and parsed again.

    Exchange sometimes sends a forwarded mail wrapped in base64. The parser reads the wrapped text as a mail without headers, so
    the base64 appeared as is where the forwarded mail belongs
    (Pinned by: test_core_mailboxes_and_charsets.py::TestForwardedMailInATransferEncoding). The part is replaced with the decoded
    mail, so a later call returns the same message.
    """
    if part.get_content_type() != "message/rfc822":
        return None
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Message) else None
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if inner is None or encoding not in ("base64", "quoted-printable"):
        return inner

    # A mail wrapped in base64 is read by the parser as text without headers. With quoted-printable the headers are already
    # parsed, so the header lines are restored and decoded together with the rest.
    wrapped = inner.get_payload()
    if not isinstance(wrapped, str) or (encoding == "base64" and inner.keys()):
        return inner
    if inner.keys():
        wrapped = "".join(f"{name}: {value}\n" for name, value in inner.items()) + "\n" + wrapped
    try:
        raw = (base64.b64decode("".join(wrapped.split()) + "==", validate=False) if encoding == "base64"
               else quopri.decodestring(wrapped.encode("ascii", errors="replace")))
    except (binascii.Error, ValueError):
        return inner
    inner = parse_mail(raw)
    part.set_payload([inner])
    del part["Content-Transfer-Encoding"]
    return inner


def _is_attachment(part: Message, message: Message) -> bool:
    """Is this an attachment of `message`: its Content-Disposition is attachment, or a leaf part that is not the top one has a
    name (`name=`, `filename=`) and no Content-ID or Content-Location for the body to point at — a part with no Disposition, or
    an inline part that is not text (text/*).

    Attachments that carry only a name (old Outlook, groupware) were read as body, so a text/html attachment pushed out the
    plain-text body, with no warning either (Pinned by: test_core_text_and_mail.py::TestNamedMailPartWithoutDisposition).
    Apple Mail and iOS Mail send a PDF placed in the middle of the body as `inline; filename=…`, which was neither read nor
    warned about (Pinned by: test_core_mail_parts_and_icons.py::TestHtmlBodySplitAroundAnInlineAttachment). A picture embedded
    in the body (Content-ID) and the name of a top-level single-part mail are not attachments.
    Known accepted gap: a text part (text/*) with `inline; filename=…` is read as body (text a mail program shows unfolded in
    the body).
    """
    if part is message or part.is_multipart():
        return False
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return True
    if disposition == "inline" and part.get_content_maintype() == "text":
        return False
    return (disposition in (None, "inline") and part.get_filename() is not None and part.get("Content-ID") is None
            and part.get("Content-Location") is None)


def _mail_name(message: Message) -> str:
    subject = message.get("Subject")
    return _decoded_header(subject) if subject is not None else ""  # counted as an unnamed attachment (`_attachment_list`)


def mail_body_parts(message: Message) -> list[Message]:
    """The body parts of a mail (text/plain, text/html; attachments and forwarded mails excluded)."""
    return [part for part in _mail_parts(message) if part.get_content_type() in ("text/plain", "text/html")
            and not _is_attachment(part, message)]


# Event properties read from a meeting invitation (text/calendar) → label (`mail_calendar_events`). Written in this order.
_CALENDAR_FIELDS = {"SUMMARY": "Title", "DTSTART": "Start", "DTEND": "End", "RRULE": "Repeats", "LOCATION": "Location", "ORGANIZER": "Organizer",
                    "ATTENDEE": "Attendees"}
# Upper limits on the number of events (VEVENT) and attendees read and on the bytes of a calendar part. Anything beyond is
# dropped silently — a recurring event can have dozens of exception events.
_MAX_CALENDAR_EVENTS = 20
_MAX_CALENDAR_ATTENDEES = 30
_MAX_CALENDAR_BYTES = 1 << 20
# iCalendar date/time values (`20240307`, `20240307T150000`, `20240307T060000Z`) and escapes (`\,`, `\;`, `\n`, `\\`).
_CALENDAR_TIME = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})\d{2}(Z?))?")
_CALENDAR_ESCAPE = re.compile(r"\\(.)")
# Folded lines (line break + space or tab). Unfolded on the bytes (`mail_calendar_events`).
_CALENDAR_FOLD = re.compile(rb"\r?\n[ \t]")
# Splits a property line `name;parameters:value` at the first `:` outside quotes.
_CALENDAR_LINE = re.compile(r'((?:[^:"]|"[^"]*")*):(.*)')
_CALENDAR_PARAM_CN = re.compile(r";CN=(\"[^\"]*\"|[^;:]*)", re.IGNORECASE)
_CALENDAR_PARAM_TZID = re.compile(r";TZID=(\"[^\"]*\"|[^;:]*)", re.IGNORECASE)
# The written shape of the date value of an all-day event (the `2024-03-07` that `_calendar_value` produces).
_CALENDAR_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Recurrence rule (RRULE, `_recurrence`): the items rendered readably, frequency (interval 1 / interval N), weekdays, the ordinal
# before a weekday, the weekday value `[ordinal]weekday`.
# If there is any other item (BYSETPOS, BYMONTH, BYHOUR …) or frequency (HOURLY …), the rule is left as its original text.
_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}
_RRULE_FREQUENCIES = {"DAILY": ("daily", "days"), "WEEKLY": ("weekly", "weeks"), "MONTHLY": ("monthly", "months"),
                      "YEARLY": ("yearly", "years")}
_RRULE_WEEKDAYS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}
_RRULE_ORDINALS = {1: "the first", 2: "the second", 3: "the third", 4: "the fourth", 5: "the fifth", -1: "the last"}
_RRULE_WEEKDAY = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")


def mail_calendar_events(message: Message) -> list[tuple[str, list[tuple[str, str]]]]:
    """The events of the meeting invitations (text/calendar parts, attachments excluded) in a mail body: (block title `Event` or
    `Event (cancelled)`, [(label, value)]).

    An Outlook meeting invitation does not write the date/time and location in the text/plain and text/html bodies of the
    alternative, only in the text/calendar part. Reading only the body, the date/time and location vanished without a warning
    (Pinned by: test_core_mail_parts_and_icons.py::TestMeetingInvitation). Only the title, start, end (time zone as is),
    recurrence (`_recurrence`), location, organizer and attendees are read — the description (DESCRIPTION) is the same text as
    the body. A named .ics attachment keeps its attachment warning (`_is_attachment`).
    Silently an empty list when there is no event to read or it cannot be decoded.
    """
    events: list[tuple[str, list[tuple[str, str]]]] = []

    for part in _mail_parts(message):
        if part.get_content_type() != "text/calendar" or _is_attachment(part, message):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or len(payload) > _MAX_CALENDAR_BYTES:
            continue
        # Folding is by bytes (75 octets), so Outlook splits even between the bytes of a Hangul character. Unfolding after decoding
        # to text garbled that character into '예��'
        # (Pinned by: test_core_html_mail_hidden_and_frames.py::TestCalendarLinesFoldedInsideACharacter).
        text = decode_text(_CALENDAR_FOLD.sub(b"", payload), part.get_content_charset())[0]
        cancelled = str(part.get_param("method") or "").upper() == "CANCEL" or "\nMETHOD:CANCEL" in text.upper()
        title = "Event (cancelled)" if cancelled else "Event"
        events += [(title, fields) for fields in _calendar_events(text)][:_MAX_CALENDAR_EVENTS - len(events)]
    return events


def mail_calendar_text(message: Message) -> str:
    """The events of a meeting invitation (`mail_calendar_events`) as text to place after the body: an `Event:` heading paragraph
    + a `- label: value` list. Empty text if there is none."""
    return "\n\n".join(f"{title}:\n\n" + "\n".join(f"- {label}: {value}" for label, value in fields)
                       for title, fields in mail_calendar_events(message))


def _calendar_events(text: str) -> list[list[tuple[str, str]]]:
    """[(label, value)] for each VEVENT of iCalendar text. Unfolds folded lines (line break + space or tab) and leaves out events
    without a single value."""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events: list[list[tuple[str, str]]] = []
    current: dict[str, list[str]] | None = None

    for line in lines:
        split = _CALENDAR_LINE.match(line)  # do not split at a `:` inside quotes (`TZID="(UTC+09:00) 서울"`)
        name, rest = split.groups() if split else (line, "")
        key = name.split(";", 1)[0].strip().upper()
        if key == "BEGIN" and rest.strip().upper() == "VEVENT":
            current = {}
        elif key == "END" and rest.strip().upper() == "VEVENT" and current is not None:
            _end_of_all_day_event(current)
            fields = [(label, ", ".join(current[field])) for field, label in _CALENDAR_FIELDS.items() if current.get(field)]
            if fields:
                events.append(fields)
            current = None
        elif current is not None and key in _CALENDAR_FIELDS and rest.strip():
            values = current.setdefault(key, [])
            if len(values) < (_MAX_CALENDAR_ATTENDEES if key == "ATTENDEE" else 1):
                values.append(_calendar_value(key, name, rest.strip()))
    return events


def _end_of_all_day_event(event: dict[str, list[str]]) -> None:
    """Change the end of an all-day event (start and end both dates) to its last day. For a one-day event the end is removed.

    A DTEND that is a date value does not include that day (RFC 5545) — written as is, a one-day event on March 5 came out as
    'End: 2024-03-06' (Pinned by: test_core_mail_bodies_and_events.py::TestAllDayAndRecurringEvents). If the start is a time,
    the event does not follow the rule and is left as is.
    """
    start, end = event.get("DTSTART"), event.get("DTEND")
    if not (start and end and _CALENDAR_DATE.fullmatch(start[0]) and _CALENDAR_DATE.fullmatch(end[0])):
        return
    try:
        last = (date.fromisoformat(end[0]) - timedelta(days=1)).isoformat()
    except ValueError:
        return
    if last <= start[0]:
        del event["DTEND"]
    else:
        event["DTEND"] = [last]


def _recurrence(rule: str) -> str:
    """A recurrence rule (RRULE) as readable text: `weekly on Tuesday, 4 times`,
    `every 2 weeks on Monday, Wednesday, until 2024-06-30`, `monthly on the last Friday`.

    Recurrence rules were not read, so a recurring meeting read as a one-off
    (Pinned by: test_core_mail_bodies_and_events.py::TestAllDayAndRecurringEvents).
    Only the common items (`_RRULE_KEYS`) are rendered; if there is any other item or frequency, or a value does not fit, the
    rule is left as its original text.
    """
    parts: dict[str, str] = {}

    for item in rule.upper().split(";"):
        name, _, value = item.partition("=")
        parts[name.strip()] = value.strip()

    # Numbers are read with `decimal_int` — the `int()` ValueError on a COUNT/INTERVAL of more than 4,300 digits got the whole
    # mail rejected (Pinned by: test_core_malformed_mail_and_html.py::TestHugeNumbersInHtmlAndMail)
    frequency = _RRULE_FREQUENCIES.get(parts.get("FREQ", ""))
    interval = decimal_int(parts.get("INTERVAL", "1"), 0)
    if frequency is None or not parts.keys() <= _RRULE_KEYS or interval < 1:
        return rule

    head = frequency[0] if interval == 1 else f"every {interval} {frequency[1]}"
    days = []

    # weekdays (with an ordinal, `the second Tuesday`) and days of the month
    for day in filter(None, parts.get("BYDAY", "").split(",")):
        match = _RRULE_WEEKDAY.fullmatch(day)
        ordinal = _RRULE_ORDINALS.get(int(match.group(1))) if match and match.group(1) else ""
        if match is None or ordinal is None:
            return rule
        days.append(f"{ordinal} {_RRULE_WEEKDAYS[match.group(2)]}".strip())
    for day in filter(None, parts.get("BYMONTHDAY", "").split(",")):
        if day == "-1":
            days.append("the last day")
        elif 1 <= decimal_int(day, 0) <= 31:
            days.append(f"day {int(day)}")
        else:
            return rule

    # the end: count or date
    pieces = [" on ".join([head, ", ".join(days)]).removesuffix(" on ")]
    count, until = parts.get("COUNT"), parts.get("UNTIL")
    if count is not None:
        if decimal_int(count, None) is None:
            return rule
        pieces.append(f"{int(count)} times")
    if until is not None:
        match = _CALENDAR_TIME.fullmatch(until)
        if match is None:
            return rule
        pieces.append(f"until {match.group(1)}-{match.group(2)}-{match.group(3)}")
    return ", ".join(pieces)


def _calendar_value(key: str, params: str, value: str) -> str:
    """An event property value as readable text: a time as `2024-03-07 15:00 (time zone)`, `… UTC` or `2024-03-07`, a person as
    `name <address>`, and text with its escapes undone."""
    if key in ("DTSTART", "DTEND"):
        match = _CALENDAR_TIME.fullmatch(value)
        if match is None:
            return value
        year, month, day, hour, minute, utc = match.groups()
        when = f"{year}-{month}-{day}" + (f" {hour}:{minute}" if hour else "")
        zone = _CALENDAR_PARAM_TZID.search(params)
        if utc:
            return f"{when} UTC"
        return f"{when} ({zone.group(1).strip(chr(34))})" if zone and hour else when
    if key == "RRULE":
        return _recurrence(value)
    if key in ("ORGANIZER", "ATTENDEE"):
        address = re.sub(r"^mailto:", "", value, flags=re.IGNORECASE)
        name = _CALENDAR_PARAM_CN.search(params)
        name_text = name.group(1).strip().strip('"') if name else ""
        return f"{name_text} <{address}>" if name_text and name_text != address else address
    unescaped = _CALENDAR_ESCAPE.sub(lambda match: " " if match.group(1) in "nN" else match.group(1), value)
    return " ".join(unescaped.split())


# Upper limit on the multipart nesting descended to find the body HTML parts (`mail_html_body_parts`). Real mail has three or
# four levels.
_MAX_MIME_DEPTH = 64
# The nesting of multipart boundaries allowed before parsing (`parse_mail`). Even mail forwarded over and over has about ten
# levels.
_MAX_MIME_NESTING = 32
# The lines `_mime_nesting_exceeds` looks at: a line-start `--boundary`, and the boundary parameter of a Content-Type header (or
# of its continuation line).
_MIME_NESTING_LINE = re.compile(
    rb"^--(?P<delimiter>[^\r\n]*)"
    rb"|^(?:content-type:|[ \t])[^\r\n]*?\bboundary\s*=\s*(?:\"(?P<declared_quoted>[^\"\r\n]*)\"|(?P<declared>[^\s;\"]+))",
    re.IGNORECASE | re.MULTILINE)


def mail_html_body_parts(message: Message) -> list[Message]:
    """All the body HTML parts of a mail (attachments and forwarded mails excluded) in document order. In multipart/alternative
    only the last branch that has HTML (they are different renderings of the same body); in any other multipart (mixed, related)
    all the branches are visited in turn.

    Apple Mail sends an attachment placed in the middle of the body as `multipart/mixed > (text/html ①, attachment, text/html ②)`.
    Only the first text/html part was read, so body ② after the attachment vanished without a warning
    (Pinned by: test_core_mail_parts_and_icons.py::TestHtmlBodySplitAroundAnInlineAttachment).
    """
    return _html_body_parts(message, message, 0)


def _html_body_parts(part: Message, message: Message, depth: int) -> list[Message]:
    if depth > _MAX_MIME_DEPTH or (part is not message and part.get_content_maintype() == "message"):
        return []
    if not part.is_multipart():
        return [part] if part.get_content_type() == "text/html" and not _is_attachment(part, message) else []
    children = [child for child in part.get_payload() if isinstance(child, Message)]
    if part.get_content_subtype() == "alternative":
        return next((found for child in reversed(children) if (found := _html_body_parts(child, message, depth + 1))), [])
    return [found for child in children for found in _html_body_parts(child, message, depth + 1)]


def mail_body_sequence(message: Message) -> list[Message]:
    """The body parts of a mail (text/html, text/plain; attachments and forwarded mails excluded) in document order.
    multipart/alternative holds different renderings of the same body, so only one is chosen: the HTML parts of the last branch
    that has HTML (same as `mail_html_body_parts`), or, without HTML, the last branch that has a body. In any other multipart
    (mixed, related) all the branches are visited in turn.

    With a plain-text body + an HTML part (signature, mailing list footer) in a mixed, only the HTML was read and the plain-text
    body vanished without a warning
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestPlainAndHtmlBodiesSideBySide).
    """
    return _body_sequence(message, message, 0)


def mail_html_outside_alternative(message: Message) -> bool:
    """Is any of the body HTML parts of the mail (`mail_body_sequence`) outside multipart/alternative — then it is not another
    rendering of the plain-text body but a separate body (the HTML body or footer of a mixed) that vanishes if only the plain
    text is read (`html.mail_reads_as_html`)."""
    inside = {id(sub) for part in message.walk() if part.get_content_type() == "multipart/alternative" for sub in part.walk()}
    return any(part.get_content_type() == "text/html" and id(part) not in inside for part in mail_body_sequence(message))


def _body_sequence(part: Message, message: Message, depth: int) -> list[Message]:
    if depth > _MAX_MIME_DEPTH or (part is not message and part.get_content_maintype() == "message"):
        return []
    if not part.is_multipart():
        return [part] if part.get_content_type() in ("text/plain", "text/html") and not _is_attachment(part, message) else []
    children = [child for child in part.get_payload() if isinstance(child, Message)]
    if part.get_content_subtype() == "alternative":
        return _html_body_parts(part, message, depth) or next(
            (found for child in reversed(children) if (found := _body_sequence(child, message, depth + 1))), [])
    return [found for child in children for found in _body_sequence(child, message, depth + 1)]


def mail_head_text(message: Message) -> str:
    """The mail headers to place before the body (subject, sender, recipients, cc, date). Decodes RFC 2047 encoding
    (`=?euc-kr?b?…?=`)."""
    lines = []

    for name in _MAIL_HEADERS:
        value = message.get(name)
        if value is not None:
            lines.append(f"{name}: {' '.join(_decoded_header(value).split())}")
    return "\n".join(lines)


def _decoded_header(value: object) -> str:
    """A header value as text: raw 8-bit bytes are detected and decoded like a body (`decode_text`), and RFC 2047 words
    (`=?euc-kr?b?…?=`) are decoded.

    A header that writes UTF-8/CP949 bytes as they are, without RFC 2047 encoding (groupware, old mail programs), is wrapped by
    the parser in an `unknown-8bit` `Header`, and `str()` emits a replacement character per byte, so the subject and sender
    became `������` (Pinned by: test_core_text_and_mail.py::TestRawEightBitMailHeaders). It is turned back into the original
    bytes and decoded, and then any RFC 2047 words that may be mixed in are decoded.
    """
    if isinstance(value, Header):
        value = "".join(_header_chunk_text(chunk, charset) for chunk, charset in decode_header(value))
    text = str(value)
    if not text.isascii():
        # `decode_header` returns non-ASCII text mixed with encoded words as bytes of the `\\uc804` form. Only the words are decoded,
        # one by one, and the rest of the text is left as is. Whitespace between two adjacent words is removed (RFC 2047).
        parts = _ENCODED_WORD.split(text)
        return "".join(_decoded_words(part) if index % 2 else
                       ("" if 0 < index < len(parts) - 1 and not part.strip() else part) for index, part in enumerate(parts))
    return _decoded_words(text)


def _decoded_words(text: str) -> str:
    """Decode RFC 2047 words. The charset of a word is read with the superset codec, as for a body
    (`_header_codec` — EUC-KR → CP949).

    When Outlook's `=?ks_c_5601-1987?B?…?=` (an EUC-KR label) contains extended Hangul (됬, 똠), the EUC-KR codec cannot decode
    it and the subject stayed as encoded words
    (Pinned by: test_core_mail_parts_and_icons.py::TestKoreanEncodedWordsWithExtendedHangul). Words that still do not decode are
    decoded piece by piece with `decode_text`.
    """
    # A word whose base64 is broken (`=?utf-8?b?A?=`) raises HeaderParseError, which is not a ValueError: one subject or
    # attachment name rejected the whole mail
    # (Pinned by: test_audit_html_text.py::TestEncodedWordThatCannotBeDecoded).
    try:
        pieces = decode_header(text)
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return text
    # In a word labeled Windows-1252/ISO-8859-1 nearly every byte decodes strictly, so a CP949 subject was garbled into `[°øÁö]`.
    # It is judged by its content, like the same declaration on a body
    # (`_latin_labeled_text`, Pinned by: test_core_html_mail_hidden_and_frames.py::TestEncodedWordLabeledLatinButKorean).
    pieces = [(_latin_labeled_text(chunk, charset).encode(), "utf-8") if isinstance(chunk, bytes) and _header_codec(charset) == _WESTERN
              else (chunk, charset) for chunk, charset in pieces]
    try:
        return str(make_header([(chunk, _header_codec(charset)) for chunk, charset in pieces]))
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return "".join(_header_chunk_text(chunk, charset) for chunk, charset in pieces)


def _latin_labeled_text(chunk: bytes, charset: str | None) -> str:
    """A header piece labeled Windows-1252/ISO-8859-1: strict CP949 decoding is tried first, as for a body (`decode_text`), but
    if more than half of the non-ASCII characters of that text are not everyday Hangul (KS X 1001 precomposed), it is read as
    declared.

    In a short header, two adjacent Western European letters (ção, öß, üß) decode strictly as a single CP949 hanja, and
    `Informações` became `Informa寤es`
    (Pinned by: test_core_malformed_mail_and_html.py::TestLatinSubjectWithAccentedLetterPairs).
    """
    text, encoding = decode_text(chunk, charset)
    if encoding != _KOREAN_LEGACY:
        return text
    non_ascii = [ch for ch in text if not ch.isascii()]
    if 2 * sum(ch in _EVERYDAY_HANGUL for ch in non_ascii) >= len(non_ascii):
        return text
    return _strict(chunk, _WESTERN) or chunk.decode("latin-1")


def _header_codec(charset: str | None) -> str | None:
    """charset of a header piece → the codec to decode with. The superset, as for a declared charset (`_declared_codec`); unknown
    names and the UTF family as they are."""
    if charset in (None, "unknown-8bit"):
        return charset
    return _declared_codec(charset) or charset


def _header_chunk_text(chunk: bytes | str, charset: str | None) -> str:
    if isinstance(chunk, str):
        return chunk
    codec = _header_codec(charset)
    if codec == _WESTERN:
        return _latin_labeled_text(chunk, charset)
    if codec in (None, "unknown-8bit"):
        return decode_text(chunk, charset)[0]
    try:
        return chunk.decode(codec)
    except (LookupError, UnicodeError):
        return decode_text(chunk, charset)[0]


# Signature parts that are not counted as attachments (S/MIME and PGP detached signatures). They are not content but the
# signature value of the body (`mail_attachment_warning`).
_SIGNATURE_TYPES = frozenset({"application/pkcs7-signature", "application/x-pkcs7-signature", "application/pgp-signature"})
# The Outlook-only formatting part (winmail.dat). This converter cannot convert it separately either.
_TNEF_TYPES = frozenset({"application/ms-tnef", "application/vnd.ms-tnef"})


def mail_attachment_warning(*mails: Message) -> str | None:
    """One warning that they were not read, if the mails (an mbox archive has several) and the mails forwarded inside them
    (`mail_messages`) have attachments (`_is_attachment`) or forwarded mails left unread beyond the limit. Forwarded mails that
    were read are not counted as attachments.

    Signature parts (`_SIGNATURE_TYPES`, smime.p7s) are not counted, and Outlook's winmail.dat (`_TNEF_TYPES`) is reported with a
    sentence saying it cannot be converted separately — for both, 'convert them separately' was wrong advice
    (Pinned by: test_core_mail_parts_and_icons.py::TestSignaturesAndTnefAttachments).
    Known accepted gap: S/MIME that wraps the body inside the signature (smime.p7m) is reported as an attachment as before — the
    body is inside it. Up to `_ATTACHMENT_NAMES_SHOWN` names are written — the one-line warning of a mail with 20,000
    attachments was 229KB
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestManyMailAttachmentNames).
    """
    names, tnef = [], 0

    for mail in mails:
        messages, skipped = mail_messages(mail)
        for current in messages:
            for part in _mail_parts(current):
                if _forwarded_message(part) is not None or not _is_attachment(part, current):
                    continue
                kind = part.get_content_type()
                if kind in _TNEF_TYPES:
                    tnef += 1
                elif kind not in _SIGNATURE_TYPES:
                    names.append(_attachment_name(part))
        names += skipped

    warnings = []
    if names:
        warnings.append(f"{len(names)} mail attachment(s) ({_attachment_list(names)}) were not read. Convert attachments separately.")
    if tnef:
        warnings.append(f"{tnef} Outlook-only attachment(s) (winmail.dat) cannot be read. Attachments or formatted body text inside may be missing.")
    return " ".join(warnings) or None


def _attachment_list(names: list[str]) -> str:
    """The text inside the parentheses of the attachment warning: up to `_ATTACHMENT_NAMES_SHOWN` names are written and the rest
    become 'and N more'; unnamed attachments (empty text) are given only as a count. Unnamed attachments were repeated 10 times
    as 'unnamed, unnamed, …' (Pinned by: test_core_round44.py::TestUnnamedMailAttachments)."""
    named = [name for name in names if name]
    parts = [*named[:_ATTACHMENT_NAMES_SHOWN]]

    if len(named) > _ATTACHMENT_NAMES_SHOWN:
        parts[-1] += f" and {len(named) - _ATTACHMENT_NAMES_SHOWN} more"
    if len(named) < len(names):
        parts.append(f"{len(names) - len(named)} unnamed attachment(s)")
    return ", ".join(parts)


def _attachment_name(part: Message) -> str:
    """The attachment file name, or empty text if there is none (`_attachment_list` counts those separately). A name written as
    RFC 2047 words (`filename="=?UTF-8?B?…?="`) instead of RFC 2231, as Gmail does, is decoded too — it appeared in the warning
    as encoded words (Pinned by: test_core_mail_parts_and_icons.py::TestEncodedAttachmentName)."""
    name = part.get_filename()
    return _decoded_header(name) if name else ""


def mail_plain_body(message: Message, parts: list[Message] | None = None) -> tuple[str, list[str]] | None:
    """The plain-text body parts of a mail (only those among `parts` if given), with the transfer encoding undone, decoded with
    each part's charset and joined: (text, the encoding of each part). None if there is none."""
    return _mail_body(message, "text/plain", parts)


def _mail_body(message: Message, content_type: str, parts: list[Message] | None = None) -> tuple[str, list[str]] | None:
    texts, encodings = [], []

    for part in mail_body_parts(message) if parts is None else parts:
        if part.get_content_type() != content_type:
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload.strip():
            text, encoding = decode_text(payload, part.get_content_charset())
            if content_type == "text/plain" and str(part.get_param("format", "")).lower() == "flowed":
                text = _unflowed(text, delete_space=str(part.get_param("delsp", "")).lower() == "yes")
            texts.append(text.strip())
            encodings.append(encoding)
    return ("\n\n".join(texts), encodings) if texts else None


def _unflowed(text: str, *, delete_space: bool) -> str:
    """`format=flowed` (RFC 3676) plain text back into its original paragraphs: a line ending with a space (except the signature
    separator `-- `) is joined with the next line of the same quote depth. With `delete_space` (DelSp=yes) that space is dropped
    when joining. The space added at the start of a line (space-stuffing) is stripped, and quoted lines keep the `> ` shape.

    Paragraphs written as running text were broken at every line (the Thunderbird and Apple Mail default,
    Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestFormatFlowedPlainMail). Deliberate: lines are not joined
    where the quote depth changes (same as the specification).
    """
    lines: list[list[str]] = []  # the pieces to join, per line. Appending to one string is quadratic on a very long paragraph
    joining_depth: int | None = None  # the quote depth of the previous line, if that line continues

    for line in text.split("\n"):
        line = line.removesuffix("\r")
        depth = len(line) - len(line.lstrip(">"))
        content = line[depth:].removeprefix(" ")
        flowed = content.endswith(" ") and content != "-- "
        if flowed and delete_space:
            content = content[:-1]
        if joining_depth == depth and lines:
            lines[-1].append(content)
        else:
            lines.append([f"{'>' * depth} {content}" if depth else content])
        joining_depth = depth if flowed else None
    return "\n".join("".join(pieces) for pieces in lines)


def forwarded_mail_head(message: Message) -> str:
    """The heading line and headers to place before a forwarded mail."""
    return f"## {FORWARDED_MAIL_TITLE}\n\n{mail_head_text(message)}".strip()


def _mail_text(mail: Message) -> tuple[str, list[str]] | None:
    """For a mail read as plain text: (headers + body + forwarded mails, the encodings decoded). None if the outer body is HTML
    only (it is read as text in its original form).

    A forwarded mail gives its plain-text body or, without one, its HTML body in original form (HTML under a text extension is
    text — only mail named .txt or .md takes this path. `detect` sends .eml to the HTML extractor).
    """
    messages, _ = mail_messages(mail)
    outer = mail_plain_body(mail)
    if outer is None and mail_body_parts(mail):
        return None
    # A mail with attachments only and no body keeps just its headers. The events of a meeting invitation go after the body
    # (`mail_calendar_text`)
    texts = [mail_head_text(mail), outer[0] if outer else "", mail_calendar_text(mail)]
    encodings = outer[1] if outer else ["utf-8"]

    for forwarded in messages[1:]:
        body = mail_plain_body(forwarded) or _mail_body(forwarded, "text/html") or ("", [])
        texts.append(f"{forwarded_mail_head(forwarded)}\n\n{body[0]}\n\n{mail_calendar_text(forwarded)}".strip())
        encodings.extend(body[1])
    return "\n\n".join(text for text in texts if text), encodings


def _mail_box_text(mails: list[Message]) -> tuple[str, list[str]] | None:
    """For the mails read as plain text (`mail_box`): (text, the encodings decoded). With several mails, `## Message N` is placed
    before each one. None if any mail's outer body is HTML only (it is read as text in its original form — `_mail_text`)."""
    bodies = [_mail_text(mail) for mail in mails]
    if not bodies or any(body is None for body in bodies):
        return None
    if len(bodies) == 1:
        return bodies[0]
    text = "\n\n".join(f"## {mail_box_title(number)}\n\n{body[0]}" for number, body in enumerate(bodies, start=1))
    return text, [encoding for body in bodies for encoding in body[1]]


def extract_text(data: bytes, options: ConvertOptions) -> Extraction:
    """TXT (mail and mbox included). The control sequences of a terminal log (`ESC[31m`) are removed whole — only the ESC was
    removed (clean_text) and `[31m`, `[0m` stayed in the text
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestTerminalColorCodesInText). Deliberate: MD is left
    as the original (`extract_markdown`)."""
    extraction = _plain_text(data)
    section = extraction.sections[0]
    if "\x1b" in section.text:
        section.text = _TERMINAL_SEQUENCE.sub("", section.text)
    return extraction


def _plain_text(data: bytes) -> Extraction:
    """The reading shared by TXT and MD: body and attachment warnings for mail and mbox, otherwise the decoded text."""
    mails, total = mail_box(data)
    body = _mail_box_text(mails)
    if body is not None:
        text, encodings = body
    else:
        text, encoding = decode_text(data)
        encodings = [encoding]
    encoding = encodings[0]

    # In a fixed-width export that pads its columns with NULs, the columns run together like '홍길동1000' once the NULs are
    # removed (clean_text). A run of NULs is replaced with the same number of spaces to keep the column boundaries and alignment.
    # A single NUL is stray noise and is removed as before
    # (Pinned by: test_text_html.py::TestLegacyTextWithNulBytes::test_nul_padded_fixed_width_export_keeps_its_columns).
    text = _NUL_PADDING.sub(lambda match: " " * len(match.group()), text)

    extraction = Extraction(sections=[Section(SectionKind.DOCUMENT, 1, text)], metadata={"encoding": encoding})
    extraction.warnings.extend(decoding_warnings(encodings))
    if body is not None:
        extraction.warnings.extend(warning for warning in (mail_attachment_warning(*mails), mail_box_warning(len(mails), total))
                                   if warning)
    return extraction


# data: URI pictures embedded in Markdown, `![alt](data:image/png;base64,… "title")` (the address may be wrapped in `<…>`, and the
# title may be in double or single quotes or parentheses), and HTML pictures `<img … src="data:image/…;base64,…">` (an unquoted
# `src=data:…` included). Parameters after the MIME type (`;charset=utf-8`) are skipped. Not knowing single-quote and parenthesis
# titles, `<…>` addresses, parameters and unquoted src left the base64 in the body as is
# (Pinned by: test_core_mbox_markdown_and_api.py::TestMarkdownDataUriVariants). The base64 field takes only characters that are
# not quotes, parentheses or `>`, so it ends at the title, the closing parenthesis or the closing quote. The field greedily eats
# the trailing whitespace too — with `\s*` after a lazy field the two share the whitespace, and 20,000 spaces after an unclosed
# URI took 3.4 seconds (quadratic) (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri::
# test_unclosed_data_uri_followed_by_long_whitespace_takes_linear_time). The field of an unquoted src ends only where the base64
# characters end (so that it does not share characters with the attribute field after it). Trailing whitespace is dropped when
# decoding. The attribute fields of `<img` do not cross `<` or `>`. Balanced code fence blocks are found first, line by line, and
# skipped (`_fenced_spans`). The last two branches (`other_…`) are data: URI pictures that are not base64 (percent-encoded SVG
# etc.), which keep only the alt text as in HTML — left as the original, one picture used 140,000 characters
# (Pinned by: test_core_round45.py::TestMarkdownDataUriPicturesThatAreNotBase64). The address field takes, like CommonMark, no
# whitespace and one level of parentheses, and uses possessive quantifiers so that it does not backtrack.
# The alt text field (`_PICTURE_ALT`) stops before the next `![` — looking for `]` up to the end of the line for every `![` made
# a line of repeated `![` without `]` take quadratic time (3.3 seconds for 20KB, 2 minutes for 80KB)
# (Pinned by: test_core_round48.py::TestRepeatedPictureOpenersTakeLinearTime). Known accepted gap: when the alt text contains
# `![` (`![a ![b](data:…)`), the picture starts at the last `![` and the `![a ` before it stays as text (the picture is still
# extracted).
_PICTURE_ALT = r"(?:[^\]\n!]|!(?!\[))*"
_DATA_IMAGE = r"data:image/[\w.+-]+(?:;[\w.+-]+=[^;,\s()<>\"']*)*;base64,"
_MARKDOWN_DATA_PICTURE = re.compile(
    r"!\[(?P<alt>" + _PICTURE_ALT + r")\]\(\s*(?:<" + _DATA_IMAGE + r"(?P<angle_data>[A-Za-z0-9+/=\s]*)>\s*"
    r"|" + _DATA_IMAGE + r"(?P<data>[A-Za-z0-9+/=\s]*))"
    r"(?:\"[^\"\n]*\"\s*|'[^'\n]*'\s*|\([^()\n]*\)\s*)?\)"
    r"|<img\b(?P<before>[^<>]*?)\bsrc\s*=\s*(?:(?P<quote>[\"'])\s*" + _DATA_IMAGE + r"(?P<tag_data>[A-Za-z0-9+/=\s]*)(?P=quote)"
    r"|" + _DATA_IMAGE + r"(?P<bare_data>[A-Za-z0-9+/=]*)(?![A-Za-z0-9+/=]))(?P<after>[^<>]*)>"
    r"|!\[(?P<other_alt>" + _PICTURE_ALT + r")\]\(\s*(?:<data:image/[^<>\n]*+>|data:image/(?:[^\s()]|\([^\s()]*+\))*+)\s*"
    r"(?:\"[^\"\n]*\"\s*|'[^'\n]*'\s*|\([^()\n]*\)\s*)?\)"
    r"|<img\b(?P<other_before>[^<>]*?)\bsrc\s*=\s*(?:\"\s*data:image/[^\"]*+\"|'\s*data:image/[^']*+'|data:image/[^\s\"'<>`]*+)"
    r"(?P<other_after>[^<>]*)>",
    re.IGNORECASE)
# The line-start opening of a code fence (three or more backticks or tildes) and the closing line (fence marker + only spaces or
# tabs). The closing line also takes a trailing `\r` — because of the `\r` left on CRLF lines split at `\n`, a data: URI picture
# example inside a block was extracted as a picture (Pinned by: test_core_round48.py::TestCodeFenceWithWindowsLineBreaks).
_FENCE_OPENING = re.compile(r"`{3,}|~{3,}")
_FENCE_CLOSING = re.compile(r"(`{3,}|~{3,})[ \t]*\r?")
# The alt attribute of `<img>` (inside quotes, or an unquoted value).
# Is the picture address (the `](` after Markdown alt text, the src attribute of `<img`) an SVG data: URI.
_SVG_SOURCE = re.compile(r"(?:\]\(\s*<?|src\s*=\s*[\"']?\s*)data:image/svg", re.IGNORECASE)
_IMG_ALT = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>=`]+))""", re.IGNORECASE)


def extract_markdown(data: bytes, options: ConvertOptions) -> Extraction:
    """MD is read like TXT, and data: URI pictures embedded in the body (`![alt](data:…)`, HTML `<img src="data:…" alt="alt">`)
    are registered as pictures, like data: URIs in HTML, and replaced with placeholders.

    The base64 became body text as is, so one picture used tens of thousands of characters and there were 0 pictures
    (Pinned by: test_image.py::TestMarkdownDataUriPictures). The HTML `<img>` common in MD is the same — one picture went over
    the default limit with 520,000 characters (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri).
    When pictures are not extracted (`extract_images=False`), only the alt text is kept, as in HTML. Pictures at external
    addresses are not fetched (SSRF, same as HTML).
    Deliberate: the inside of a code fence block is example code and is left as is. So is the inside of inline code
    (`_pictures_outside_inline_code`). Known accepted gap: the data: URI of the reference form (`![alt][id]` + `[id]: data:…`)
    stays as text.
    """
    extraction = _plain_text(data)
    section = extraction.sections[0]
    if "data:image/" not in section.text:
        return extraction

    pieces = []
    position = 0
    for start, end in [*_fenced_spans(section.text), (len(section.text), len(section.text))]:
        pieces.extend(_pictures_outside_inline_code(section.text[position:start], extraction, options))
        pieces.append(section.text[start:end])
        position = end
    section.text = "".join(pieces)
    return extraction


def _pictures_outside_inline_code(text: str, extraction: Extraction, options: ConvertOptions) -> list[str]:
    """The pieces of the text outside fence blocks, with its data: URI pictures extracted as pictures. The inside of inline code
    (`` `…` ``) is example text and is left as is — when it was extracted as a picture the code span was emptied to `` ` ` `` and
    a picture description was attached (Pinned by: test_core_round44.py::TestDataUriPictureInInlineCode).

    Picture syntax is found first, and inline code is paired only from the backticks outside pictures. A picture being inside
    inline code means that code opened before it covers the start of the picture. Previously inline code was cut out first, so a
    backtick inside the alt text, the title or `<img alt>` split the picture in two and base64 stayed in the body
    (Pinned by: test_core_round45.py::TestBackticksAroundMarkdownPictures). Known accepted gap: code that opens in the alt text
    and closes outside the picture (`` `![a`](…) ``) is seen as a picture, unlike CommonMark.
    """
    pictures = list(_MARKDOWN_DATA_PICTURE.finditer(text))
    code_spans = _inline_code_spans("".join(_without_pictures(text, pictures)))
    code_starts = [start for start, _ in code_spans]

    pieces = []
    position = 0

    for match in pictures:
        before = bisect.bisect_left(code_starts, match.start()) - 1
        if before >= 0 and code_spans[before][1] > match.start():
            continue
        pieces.append(text[position:match.start()])
        pieces.append(_markdown_picture(match, extraction, options))
        position = match.end()
    pieces.append(text[position:])
    return pieces


def _without_pictures(text: str, pictures: list[re.Match]) -> list[str]:
    """The pieces with each picture syntax span masked by the same length of non-backtick characters (for pairing inline code — a
    backtick inside a picture is not code)."""
    pieces = []
    position = 0

    for match in pictures:
        pieces.append(text[position:match.start()])
        pieces.append("x" * (match.end() - match.start()))
        position = match.end()
    pieces.append(text[position:])
    return pieces


_BACKTICK_RUN = re.compile(r"`+")
# Paragraph boundary (blank line). Inline code is not paired across it. CRLF and CR line breaks are taken too — looking only at
# `\n`, in MD saved on Windows code was paired across paragraphs and a picture stayed as text
# (Pinned by: test_core_round47.py::TestInlineCodeAcrossParagraphsWithWindowsLineBreaks).
_BLANK_LINE = re.compile(r"(?:\r\n?|\n)[ \t]*(?:\r\n?|\n)")


def _inline_code_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) character positions of inline code. Paired only within a paragraph (a chunk separated by blank lines),
    like CommonMark — an unmatched backtick of an earlier paragraph paired with a backtick of a later paragraph and the picture
    between them stayed as text (Pinned by: test_core_round45.py::TestBackticksAroundMarkdownPictures::
    test_unmatched_backtick_in_an_earlier_paragraph_does_not_hide_the_picture)."""
    spans = []
    position = 0

    for blank in [*_BLANK_LINE.finditer(text), None]:
        end = blank.start() if blank else len(text)
        spans.extend((position + start, position + stop) for start, stop in _paragraph_code_spans(text[position:end]))
        position = blank.end() if blank else len(text)
    return spans


def _paragraph_code_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) of inline code within one paragraph. A run of n backticks closes at the first run of the same length
    after it, and is text if there is no closing run. The positions of runs of the same length are collected and binary-searched
    — close to linear even with many unmatched runs. Deliberate: backticks disabled by a backslash are not treated separately."""
    runs = [match.span() for match in _BACKTICK_RUN.finditer(text)]
    by_length: dict[int, list[int]] = {}

    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)

    spans = []
    index = 0

    while index < len(runs):
        start, end = runs[index]
        same = by_length[end - start]
        after = bisect.bisect_right(same, index)
        if after == len(same):
            index += 1
            continue
        closing = same[after]
        spans.append((start, runs[closing][1]))
        index = closing + 1
    return spans


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) character positions of balanced code fence blocks: from a line that starts with a fence marker (three or
    more backticks or tildes) to the end of the first line after it that is the same marker + whitespace only. If no closing
    line matches the opening marker, the marker is shortened one character at a time (four → three) and searched again, and if
    there is still none it is not a block (that line is text too).

    Previously a single regex (`^(`{3,}|~{3,})…(?:.*?\n)??(?P=mark)`) looked for a closing line up to the end of the text for
    every opening line, so 32,000 unmatched opening lines + one picture took the square of the number of lines
    (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri::
    test_many_unclosed_fence_lines_before_a_picture_take_linear_time). The positions of closing lines are collected per marker
    and binary-searched — the pair chosen is the same as with the old regex.
    """
    lines = text.split("\n")
    starts = list(itertools.accumulate((len(line) + 1 for line in lines), initial=0))
    closings: dict[tuple[str, int], list[int]] = {}  # (marker character, marker length) → closing line numbers

    for index, line in enumerate(lines):
        match = _FENCE_CLOSING.fullmatch(line)
        if match:
            closings.setdefault((line[0], len(match.group(1))), []).append(index)

    # For each marker character (backtick, tilde), the marker lengths of the closing lines (ascending). An opening line looks only
    # at lengths up to its own, so it walks no more steps than it has marker characters.
    lengths: dict[str, list[int]] = {}
    for character, length in sorted(closings, key=lambda mark: mark[1]):
        lengths.setdefault(character, []).append(length)
    spans = []
    index = 0

    while index < len(lines):
        closing = _fence_closing(lines[index], index, closings, lengths)
        if closing is None:
            index += 1
            continue
        spans.append((starts[index], starts[closing] + len(lines[closing])))
        index = closing + 1
    return spans


def _fence_closing(line: str, index: int, closings: dict[tuple[str, int], list[int]],
                   lengths: dict[str, list[int]]) -> int | None:
    """If the `index`-th line `line` is a fence opening line, the number of the line that closes its block (the longest marker is
    tried first), otherwise None."""
    opening = _FENCE_OPENING.match(line)
    if opening is None:
        return None
    run = opening.group()
    candidates = lengths.get(run[0], [])

    for position in range(bisect.bisect_right(candidates, len(run)) - 1, -1, -1):
        found = closings[run[0], candidates[position]]
        after = bisect.bisect_right(found, index)
        if after < len(found):
            return found[after]
    return None


def _markdown_picture(match: re.Match, extraction: Extraction, options: ConvertOptions) -> str:
    if match.group("other_before") is not None or match.group("other_alt") is not None:
        alt = _picture_alt(match, "other_before", "other_after", "other_alt")
        return f"[Image: {alt}]" if alt else ""
    alt = _picture_alt(match, "before", "after", "alt")
    source_start = match.end("before") if match.group("before") is not None else match.end("alt")
    if _SVG_SOURCE.match(match.string, source_start, match.end()):
        return f"[Image: {alt}]" if alt else ""  # like HTML, SVG is not extracted (it could not be drawn and only a warning was left)

    if match.group("before") is not None:
        encoded = match.group("tag_data") if match.group("quote") is not None else match.group("bare_data")
    else:
        encoded = match.group("data") if match.group("data") is not None else match.group("angle_data")
    if options.extract_images:
        try:
            picture = base64.b64decode("".join(encoded.split()), validate=False)
        except (binascii.Error, ValueError):
            picture = b""
        if picture:
            return extraction.add_image(picture, alt=alt)
    return f"[Image: {alt}]" if alt else ""


def _picture_alt(match: re.Match, before: str, after: str, alt: str) -> str:
    """The alt text of a picture (whitespace collapsed to one space): the alt attribute for HTML `<img>`, the text of `![…]` for
    Markdown."""
    if match.group(before) is None:
        return " ".join(match.group(alt).split())
    alt_match = _IMG_ALT.search(f"{match.group(before)} {match.group(after)}")
    text = html.unescape(next((value for value in alt_match.groups() if value is not None), "")) if alt_match else ""
    return " ".join(text.split())
