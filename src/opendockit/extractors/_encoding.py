"""Text encoding detection and decoding, shared by TXT, CSV, HTML, mail and the format detection: BOMs, UTF-8, CP949 and
Western single-byte text, declared charsets, BOM-less UTF-16, and the warning for bytes that could not be decoded.
"""

import codecs
import re
import unicodedata

from charset_normalizer import CharsetMatch, CharsetMatches, from_bytes


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
            # Replacement decoding is reported here too — a lone surrogate behind a UTF-16 BOM became `�` without a warning
            # (Pinned by: test_audit_html_text.py::TestDamagedTextBehindAByteOrderMark)
            text = _strict(data, encoding)
            return (text, encoding) if text is not None else (data.decode(encoding, errors="replace"), f"{encoding}{_REPLACED}")

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
    best = _plausible_best(matches, sample)
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


# Text that UTF-16 without NUL bytes can plausibly hold: every character has a high byte other than 0, so no ASCII, and in
# practice it is CJK or Hangul text without spaces or line breaks (`_plausible_wide_text`). CJK punctuation, kana, Bopomofo,
# Hangul compatibility jamo; Hangul syllables; CJK compatibility ideographs; full-width forms; common punctuation and symbols.
_WIDE_TEXT_RANGES = ((0x2000, 0x27BF), (0x3000, 0x31FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFF00, 0xFFEF))
# At most this share of the characters may lie outside those ranges (one per 50).
_WIDE_TEXT_STRAYS = 50


def _plausible_best(matches: CharsetMatches, sample: bytes) -> CharsetMatch | None:
    """The best statistical guess, leaving out a UTF-16 or UTF-32 guess for a sample without NUL bytes unless it reads as
    plausible CJK or Hangul text (`_plausible_wide_text`). Text in those encodings has NUL bytes as soon as it holds an ASCII
    character; without any it can only be text such as Japanese or Chinese with no space or line break (`日本語の…` →
    `E5 65 2C 67 …`), which charset-normalizer guesses rightly. A short broken UTF-8 subject `ab=ZZé` + a cut character was read
    as UTF-16 and became `慢㵚嫃꧃` — rare ideographs and Javanese — or `扡娽썚쎩` without a warning
    (Pinned by: test_audit_web_fixes.py::TestNoWideEncodingGuessWithoutNulBytes)."""
    if b"\x00" in sample:
        return matches.best()
    return next((match for match in matches if not match.encoding.startswith(("utf_16", "utf_32"))
                 or _plausible_wide_text(sample, match.encoding)), None)


def _plausible_wide_text(sample: bytes, encoding: str) -> bool:
    """Does `sample` decode strictly as `encoding` into text that is almost all CJK, kana, Hangul, full-width forms or common
    punctuation (`_WIDE_TEXT_RANGES`, at most one character in `_WIDE_TEXT_STRAYS` outside), and not Han ideographs and Hangul
    syllables mixed? Korean text with hanja always has spaces (NUL bytes); pairs of arbitrary bytes land in both ranges
    (`ab=ZZé` + a cut character read as UTF-16-LE is `扡娽썚쎩`)."""
    text = _strict(sample[:len(sample) - len(sample) % 2], encoding)
    if not text:
        return False
    strays = sum(1 for ch in text if not any(low <= ord(ch) <= high for low, high in _WIDE_TEXT_RANGES))
    han = any(0x4E00 <= ord(ch) <= 0x9FFF or 0xF900 <= ord(ch) <= 0xFAFF for ch in text)
    hangul = any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in text)
    return strays * _WIDE_TEXT_STRAYS <= len(text) and not (han and hangul)


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
