"""Text encoding detection and decoding, shared by TXT, CSV, HTML, mail and the format detection: BOMs, UTF-8, CP949 and
Western single-byte text, declared charsets, BOM-less UTF-16, and the warning for bytes that could not be decoded.
"""

import codecs
from collections import Counter
import functools
import itertools
import re
import unicodedata

from charset_normalizer import CharsetMatch, CharsetMatches, from_bytes

from opendockit.extractors._codecs import portable_codec


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
# A run of non-ASCII bytes (`_accents_read_as_cp949`).
_HIGH_BYTES = re.compile(rb"[\x80-\xff]+")
# Punctuation and signs Western European text puts next to its words in Windows-1252 (`_accents_read_as_cp949`); everything
# else outside ASCII must be a letter.
_WESTERN_SIGNS = frozenset("‘’‚“”„–—…€«»‹›°·•")
# Everyday Hangul syllables whose two CP949 bytes are both uppercase accented letters in Windows-1252 (À–Þ without ×): `ÇÃ` is `플`
# (`_accents_read_as_cp949`).
_UPPERCASE_ACCENT_SYLLABLES = frozenset(
    syllable for syllable in _EVERYDAY_HANGUL
    if all(0xC0 <= byte <= 0xDE and byte != 0xD7 for byte in syllable.encode("euc_kr")))
# How many places of bytes outside ASCII a statistical guess is judged on (`_written_readings`).
_JUDGED_PLACES = 1000
# A run of three or more non-ASCII bytes. Accents and punctuation in Western European text sit one or two at a time between
# ASCII letters, while Cyrillic, Greek or Thai text and text in multi-byte encodings runs on for whole words.
_HIGH_BYTE_RUN = re.compile(rb"[\x80-\xff]{3}")
# The letters outside ASCII of the languages written in the Latin code pages of Windows and in ISO 8859-2, a language a row,
# lowercase (`_one_alphabet_page`). Signs a language sets inside or against its words stand with its letters: the Spanish
# `¿ ¡ º ª`.
_VIETNAMESE = "cp1258"
_ALPHABETS = (
    ("cp1252", "àâæçéèêëîïôœùûüÿ"),      # French
    ("cp1252", "äöüß"),                   # German
    ("cp1252", "áéíñóúü¿¡ºª"),            # Spanish, Galician
    ("cp1252", "àèéìíîòóùúºª"),           # Italian
    ("cp1252", "áâãàçéêíóôõúüºª"),        # Portuguese
    ("cp1252", "àçèéíïòóúü·"),            # Catalan
    ("cp1252", "áàäéèëêíïóöúü"),          # Dutch
    ("cp1252", "áéèêëîïóôöúûüý"),         # Afrikaans
    ("cp1252", "åäöé"),                   # Swedish
    ("cp1252", "æøåéèêóòôà"),             # Danish, Norwegian
    ("cp1252", "äöåšž"),                  # Finnish
    ("cp1252", "áðéíóúýþæö"),             # Icelandic
    ("cp1252", "áðíóúýæø"),               # Faroese
    ("cp1252", "áéíóúàèìòù"),             # Irish, Scottish Gaelic
    ("cp1252", "âêîôûáéïö"),              # Welsh
    ("cp1252", "çë"),                     # Albanian
    ("cp1252", "äëéöüèêîôûâ"),            # Luxembourgish
    ("cp1252", "ñùêéèüâô"),               # Breton
    ("cp1252", "äöüõšž"),                 # Estonian
    ("cp1250", "ąćęłńóśźż"),              # Polish
    ("cp1250", "áčďéěíňóřšťúůýž"),        # Czech
    ("cp1250", "áäčďéíĺľňóôŕšťúýž"),      # Slovak
    ("cp1250", "áéíóöőúüű"),              # Hungarian
    ("cp1250", "čćđšž"),                  # Croatian, Bosnian, Slovene
    ("cp1250", "ăâîşţ"),                  # Romanian
    ("iso8859_2", "ąćęłńóśźż"),
    ("iso8859_2", "áčďéěíňóřšťúůýž"),
    ("iso8859_2", "áäčďéíĺľňóôŕšťúýž"),
    ("iso8859_2", "áéíóöőúüű"),
    ("iso8859_2", "čćđšž"),
    ("iso8859_2", "ăâîşţ"),
    ("iso8859_16", "ăâîșț"),              # Romanian with the comma below, which Windows-1250 writes as a cedilla
    ("cp1257", "ąčęėįšųūž"),              # Lithuanian
    ("cp1257", "āčēģīķļņšūž"),            # Latvian
    ("cp1257", "äöüõšž"),                 # Estonian
    ("cp1254", "çğıöşüâîûİ"),             # Turkish
    ("cp1258", "àáâăèéêíóôơùúưđ"),        # Vietnamese; its tone marks are in `_VIETNAMESE_MARKS`
)
# The Western alphabets are written in ISO 8859-15 as well, which has `€ Š š Ž ž Œ œ Ÿ` at the bytes where Windows-1252 has
# `¤ ¦ ¨ ´ ¸ ¼ ½ ¾`: a text that holds one of them reads two ways, and the alphabets say nothing of it.
_ISO_8859_15_BYTES = frozenset(b"\xa4\xa6\xa8\xb4\xb8\xbc\xbd\xbe")
# The tone marks of Vietnamese in Windows-1258, bytes of their own behind the vowel they belong to, and such a mark behind
# something that is no vowel: in Italian read so, the mark of `martedì` stands on its `d`.
_VIETNAMESE_MARKS = b"\xCC\xD2\xDE\xEC\xF2"
_MARK_OFF_A_VOWEL = re.compile(rb"(?<![AEIOUYaeiouy\xC2\xC3\xCA\xD4\xD5\xDD\xE2\xE3\xEA\xF4\xF5\xFD])[\xCC\xD2\xDE\xEC\xF2]")
# Signs that stand between two letters in text: the apostrophes and the acute used for one, the dashes, the space that does not
# break, the soft hyphen and the dot of the Catalan `l·l`. They are at these bytes in every Latin code page of Windows.
_SIGNS_INSIDE_WORDS = b"\x91\x92\x96\x97\xa0\xad\xb4\xb7"
# A text of one alphabet has at least this many ASCII letters for each byte outside ASCII: Czech and Lithuanian have five,
# and a Cyrillic or Greek word read as Latin letters has none.
_ALPHABET_ASCII_LETTERS = 2
_ASCII_LETTERS = bytes(range(0x41, 0x5B)) + bytes(range(0x61, 0x7B))
_SEVEN_BITS = bytes(range(0x80))
# Where the letters of Latin-1 begin in Windows-1252 (`À`), and how many different letters of its alphabet a text shows for a
# code page other than Windows-1252 to be its reading (`_alphabet_page`).
_LATIN_1_LETTERS = 0xC0
_OTHER_ALPHABET_LETTERS = 3
_WESTERN_LETTERS = 2
# HTML tags (left out of statistical detection) and a non-ASCII byte.
_MARKUP = re.compile(rb"<[^<>]*>")
_HIGH_BYTE = re.compile(rb"[\x80-\xff]")
# charset name declared by the document → the codec actually used. Read as the superset, as browsers do
# (EUC-KR → CP949, ISO-8859-1 → Windows-1252).
_DECLARED_CODECS = {"euc_kr": _KOREAN_LEGACY, "windows-949": _KOREAN_LEGACY, "x-windows-949": _KOREAN_LEGACY,
                    "iso8859-1": _WESTERN, "ascii": _WESTERN, "shift_jis": "cp932", "x-sjis": "cp932", "windows-31j": "cp932",
                    "gb2312": "gbk"}
# Codec names that are not character encodings (escape, idna) or that depend on the runtime environment (mbcs, oem). Not used
# even when declared. The codecs only Windows has are also refused by what a name leads to (`_codecs.portable_codec`): `ansi`
# and `dbcs` are `mbcs`, and from Python 3.14 `cp20866` and `cp0` are code pages Windows converts.
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

    Order: BOM → BOM-less UTF-16 declared and borne out by the bytes, or with NULs at every other byte, or with NULs and
    control bytes of one script below U+2000 (`_high_bytes_of_one_block`) → strict UTF-8 →
    almost UTF-8 (only when the end is cut or very few bytes are broken, or UTF-8 is declared and has a complete multi-byte
    character per broken one; replacement) → declared charset, strict → BOM-less UTF-16 → strict CP949 → CP949 with
    replacement (only when very few bytes are broken) → the Latin code page whose alphabet the letters fit
    (`_alphabet_page`) → statistical detection, without the readings nobody writes (`_written_readings`) → UTF-8 with
    replacement characters. The name
    of an encoding decoded with replacement characters (�) gets `(replaced)` appended (`decoding_warning` turns it into a warning).
    Checking CP949 before statistical detection is deliberate: charset-normalizer sometimes misjudges short CP949 Hangul as
    UTF-16, Big5 etc. and emits unrelated characters in the Hangul syllable range. Strict CP949 decoding mostly fails on
    Latin-family text, so false positives are few (Shift_JIS, GB2312 and EUC-JP, which have the same byte structure, lean toward
    CP949 because Korean files come first here — Known accepted gap).

    Deliberate: if strict CP949 decoding succeeds, it is CP949 even without Hangul. When it was accepted only with Hangul, the
    symbols (㎡, ℃, ※, →) of mostly English/numeric CSVs saved by Korean Windows Excel were garbled into Johab/Big5 characters by
    statistical detection, and a CSV whose first Hangul appears after 20,000 characters got the `(replaced)` warning without any
    replacement character (Pinned by: test_text_html.py::TestLegacyKoreanWithoutHangulAtTheFront). However, if every non-ASCII
    byte sits only right before an ASCII letter (as CP949, all 'extended Hangul + a swallowed letter'), it is read as
    Windows-1252 — `Company’s` and `25°C` became `Company뭩` and `25캜` (Pinned by: TestWesternText). So is text whose CP949
    reading holds only hanja and extended Hangul while its Windows-1252 reading holds accents inside Western words
    (`_accents_read_as_cp949` — `Situação` became `Situa豫o`). Known accepted gap: CP949 text whose only non-ASCII characters are
    extended Hangul before a letter (`A햏B`), or one or two hanja against a Latin letter (`李OK`), is read as Windows-1252.

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

    # UTF-16 without a BOM whose text is all ASCII decodes strictly as UTF-8 too, with a NUL after every letter: an HTML page
    # or a mail part saved that way came out as `<\x00h\x00t\x00…` (Pinned by: test_text_html.py::TestAsciiUtf16WithoutBom).
    # Only NUL bytes that sit at one parity of nearly every other byte go to this check first (`_alternating_nuls`); UTF-8 with
    # stray NULs or NUL-padded fields keeps the order below, and only the byte order the NULs point to is tried here: broken
    # big-endian text read in the other order is `伀䯾…`. A mail part declared UTF-16LE or UTF-16BE is read so when its bytes
    # bear the label out (`_declared_utf16`).
    utf16 = _declared_utf16(data, declared) or (utf16_encoding(data, by_nul_side_only=True) if _alternating_nuls(data) else None)
    if utf16:
        return data[:len(data) - len(data) % 2].decode(utf16) + _cut_ascii_character(data, utf16), utf16

    # UTF-16 of Cyrillic, Arabic, Devanagari or Thai text can have every byte below 0x80 (`Привет` is `1F 04 40 04 …`) and
    # NULs only after its spaces and punctuation, an eighth of the bytes, so strict UTF-8 took it and cleaning dropped the NULs
    # and the 0x04 bytes: `@825B, :0: 45;0?` (Pinned by: test_text_html.py::TestSmallScriptUtf16WithoutBom). With NULs and a
    # control byte that byte-oriented text never holds, UTF-16 is tried first, and taken only when nine in ten bytes at the
    # high-byte side are NUL or the one control byte of the script (`_high_bytes_of_one_block`). ASCII or UTF-8 text with a
    # stray NUL and SOH has letters there, and keeps its strict UTF-8 reading.
    utf16 = utf16_encoding(data) if _holds_nuls_and_foreign_controls(data[:8192]) else None
    if utf16 and _high_bytes_of_one_block(data, utf16):
        return data[:len(data) - len(data) % 2].decode(utf16) + _cut_ascii_character(data, utf16), utf16

    text = _strict(data, "utf-8")
    if text is not None:
        return text, "utf-8"
    # UTF-8 with its last character cut or a single byte broken is garbled as a whole if passed to the steps below (text of 45
    # bytes or less became ptcp154/cp875 in statistical detection). Only when everything else decodes as UTF-8 multi-byte
    # characters is that spot left as a replacement character (Pinned by: test_text_html.py::TestAlmostUtf8).
    if _almost_utf8(data) or (_declares_utf8(declared) and _mostly_utf8(data)):
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
        if western is None and _accents_read_as_cp949(data, text):
            western = _strict(data, _WESTERN)
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
    # Accents that sit one or two at a time between ASCII letters: the letters name the alphabet, and with it the code page,
    # where the statistics of a short text do not (Pinned by: test_text_html.py::TestTextOfOneAlphabet)
    sparse = _HIGH_BYTE_RUN.search(data) is None
    page = _alphabet_page(data[:200_000]) if sparse else None
    if page is not None:
        text = _strict(data, page)
        if text is not None:
            return text, page
    # A reading nobody writes is no candidate, whatever its language statistics say: `Ketään` is not `Ket‰‰n`
    # (Pinned by: test_text_html.py::TestReadingsNobodyWrites)
    matches = _written_readings(from_bytes(sample), sample)
    best = _plausible_best(matches, sample)
    if sparse and _reads_as_western(data, matches, best):
        return data.decode(_WESTERN), _WESTERN
    if best is not None and best.encoding:
        try:
            return data.decode(best.encoding), best.encoding
        except UnicodeDecodeError:
            return data.decode(best.encoding, errors="replace"), f"{best.encoding}{_REPLACED}"
        except LookupError:
            pass
    return data.decode("utf-8", errors="replace"), f"utf-8{_REPLACED}"


def _accents_read_as_cp949(data: bytes, korean: str) -> bool:
    """Is text that decodes strictly as CP949 Windows-1252 accents instead? Two accented letters in a row pair up into a CP949
    hanja (`ção` → `豫o`, `Größe` → `Gr秤e`, `gemäß` → `gem癌`), and an uppercase accent before a letter into extended Hangul
    (`Äpfel` → `훡fel`), so short Portuguese and German text was garbled without a warning
    (Pinned by: test_text_html.py::TestWesternText::test_accented_letter_pairs_are_not_read_as_hanja).

    Yes only when the CP949 reading has no everyday Hangul syllable (KS X 1001) and no CP949 symbol (㎡, ℃, ※, →, `·`, `“`) —
    nothing but hanja and extended Hangul — and the Windows-1252 reading looks like Western words: every non-ASCII run is at
    most two bytes and touches an ASCII letter, and each such character is a letter or a sign of `_WESTERN_SIGNS`. Korean
    written in hanja alone (`大韓民國`, `山田,李`) runs on for whole words or stands apart from Latin letters, so it stays CP949.
    Known accepted gap: CP949 text whose only non-ASCII characters are one or two hanja written against a Latin letter (`李OK`)
    is read as Windows-1252.

    Two uppercase accents in a row (`ÇÃ`, `ÇÕ` of `INFORMAÇÃO`, `OBSERVAÇÕES`) are an everyday syllable in CP949 (`플`), so an
    everyday syllable is let through too when both its bytes are uppercase accents in Windows-1252 and it stands between two
    uppercase Latin letters, as those pairs stand inside an uppercase word (`ATEN플O`). One Korean syllable set alone between
    two capital letters (`A이B`) is not Korean writing
    (Pinned by: test_text_html.py::TestWesternText::test_uppercase_accent_pairs_are_not_read_as_hangul).
    """
    for ch in korean:
        if ch.isascii():
            continue
        code = ord(ch)
        hanja = 0x4E00 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF
        extended_hangul = 0xAC00 <= code <= 0xD7A3 and ch not in _EVERYDAY_HANGUL
        if not (hanja or extended_hangul or ch in _UPPERCASE_ACCENT_SYLLABLES):
            return False
    if _HIGH_BYTE_RUN.search(data):
        return False
    for run in _HIGH_BYTES.finditer(data):
        before, after = data[run.start() - 1:run.start()], data[run.end():run.end() + 1]
        if not (before.isalpha() or after.isalpha()):
            return False
        if run.group().decode(_KOREAN_LEGACY, errors="ignore") in _UPPERCASE_ACCENT_SYLLABLES \
                and not (before.isupper() and after.isupper()):
            return False
    western = _strict(data, _WESTERN)
    return western is not None and all(ch.isalpha() or ch in _WESTERN_SIGNS for ch in western if not ch.isascii())


# CJK text that UTF-16 without NUL bytes plausibly holds (`_reads_as_cjk`): common punctuation and symbols; CJK punctuation, kana,
# Bopomofo, Hangul compatibility jamo; ideographs; Hangul syllables; CJK compatibility ideographs; full-width forms; and, as
# surrogate pairs, emoji and the ideographs outside the Basic Multilingual Plane (rare kanji such as `𠮷`) — arbitrary bytes
# rarely pair up into one, and a text with a single such kanji failed the check as a whole.
_WIDE_TEXT_RANGES = ((0x2000, 0x27BF), (0x3000, 0x31FF), (0x4E00, 0x9FFF), (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFF00, 0xFFEF),
                     (0x1F000, 0x1FAFF), (0x20000, 0x2FA1F), (0x30000, 0x323AF))
# At most this share of the characters may lie outside those ranges, or outside the one script of the text (one per 50).
_WIDE_TEXT_STRAYS = 50
# Han ideographs mixed with Hangul syllables are Korean with hanja in a text of at least this many characters of which at least
# this many tenths are Hangul; pairs of arbitrary bytes land in both ranges in a few characters.
_WIDE_MIXED_MIN_CHARS = 8
_WIDE_MIXED_HANGUL_TENTHS = 3
# What a shorter mix or one with less Hangul may hold besides everyday ideographs and syllables (`_everyday_cjk`): CJK
# punctuation and kana, full-width forms.
_EVERYDAY_CJK_RANGES = ((0x3000, 0x30FF), (0xFF00, 0xFFEF))
# Control bytes that byte-oriented text (UTF-8, CP949, Windows-1252 …) never holds — the same set as `detect._BINARY_CONTROL`.
# In UTF-16 they are half of an ordinary character: every Khmer letter has 0x17, every Tibetan one 0x0F, `氏` is 0F 6C.
_NOT_IN_BYTE_TEXT = re.compile(rb"[\x01-\x08\x0e-\x1a\x1c-\x1f\x7f]")
# Tab, line breaks and the other control bytes that text does hold. When they are at least this many fifths of the sample
# and it reads as one script other than CJK, it is not byte-oriented text either (every Bengali letter has 0x09).
_TEXT_CONTROL_BYTES = b"\t\n\r\f\v\x1b"
# Text made of nothing but printable ASCII, tab and line breaks: ASCII whatever its label says (`_declared_utf16`).
_PLAIN_ASCII = re.compile(rb"[\t\n\r\x20-\x7e]*")
_WIDE_CONTROL_FIFTHS = 2
# The Unicode names of the characters written together in Chinese, Japanese and Korean text start with one of these words; they
# count as one script (`_one_script`).
_CJK_SCRIPT = "CJK"
_CJK_NAME_WORDS = frozenset((_CJK_SCRIPT, "HIRAGANA", "KATAKANA", "HANGUL", "BOPOMOFO", "IDEOGRAPHIC", "FULLWIDTH", "HALFWIDTH"))
_NAME_WORD_END = re.compile("[ -]")
# The script check looks at this many characters from the front, like the printable check of `utf16_encoding`.
_WIDE_SCRIPT_HEAD = 20_000
# A text of one script other than CJK is preferred to the CJK reading of the same bytes in the other byte order from this many
# letters on (`_wide_text_rank`); one or two characters read as an ideograph either way.
_WIDE_SCRIPT_MIN_LETTERS = 3


def _written_readings(matches: CharsetMatches, sample: bytes) -> CharsetMatches:
    """The statistical guesses of a text whose bytes outside ASCII stand one or two at a time, without those of a single-byte
    code page that reads it as something nobody writes (`_unwritten`): the language statistics of a short text cannot tell
    Latin code pages apart, and they ranked such readings first. In Finnish `Ketään` read as Macintosh Icelandic is `Ket‰‰n`,
    a sign inside a word; Spanish `distinción` as DOS Central European is `distinciˇn`, German `geführt` as DOS Baltic
    `gef³hrt`, Portuguese `cooperação` as ISO 8859-14 `cooperaÓÐo`. Which of them came first changed with the version of
    charset-normalizer (3.5.3 put them ahead of Windows-1252 where 3.5.2 offered no single-byte reading at all), so the
    answer depended on the version installed; with them left out it does not. When every reading is one nobody writes, the
    guesses are kept as they are. Known accepted gaps: a reading with letters of another alphabet in it (`Kųbenhavn` for
    `København`) is written as far as this can tell; and a text that already holds such shapes (`Ã¥`, the UTF-8 of `å` read
    as Windows-1252 before it was saved) can lose its own reading."""
    if b"\x00" in sample:
        return matches
    around: bytes | None = None
    kept: list[CharsetMatch] = []
    best = western = False
    for position, match in enumerate(matches):
        if best and western:
            # What comes after the first guess and the first Windows-1252 one is never looked at (`_plausible_best`,
            # `_reads_as_western`): it is kept unjudged
            kept.extend(list(matches)[position:])
            break
        if _judged(match.encoding):
            if around is None:
                # A code page that misreads a text misreads the same bytes everywhere: the first places tell
                places = itertools.islice(_HIGH_BYTES.finditer(sample), _JUDGED_PLACES)
                around = b"\n".join({sample[max(found.start() - 1, 0):found.end() + 1] for found in places})
            if _reads_unwritten(around, match.encoding):
                continue
        kept.append(match)
        best = best or not match.encoding.startswith(("utf_16", "utf_32"))
        western = western or _WESTERN in (match.encoding, *match.could_be_from_charset)
    return CharsetMatches(kept) if kept else matches


def _reads_unwritten(around: bytes, encoding: str) -> bool:
    """Does a single-byte code page read the bytes around those outside ASCII as something nobody writes?"""
    try:
        return _unwritten().search(around.decode(encoding)) is not None
    except (UnicodeDecodeError, LookupError):
        return False


@functools.cache
def _judged(encoding: str) -> bool:
    """Is this a code page of one byte a character that reads ASCII as ASCII? Another (CJK, UTF-16, EBCDIC) is not judged."""
    # Pinned by: test_text_html.py::TestReadingsNobodyWrites::test_code_pages_of_several_bytes_or_without_ascii_are_not_judged
    # (EBCDIC Hebrew leaves bytes of ASCII undefined: a strict decoding raised, and its files were refused)
    try:
        return (bytes(range(0x80)).decode(encoding, errors="replace") == bytes(range(0x80)).decode("ascii")
                and len(bytes(range(0x80, 0x100)).decode(encoding, errors="replace")) == 0x80)
    except LookupError:
        return False


@functools.cache
def _unwritten() -> re.Pattern[str]:
    """What nobody writes next to a character outside ASCII, in Latin text: a control; a character that is no Latin letter
    and no mark, or a run of them, between two Latin letters (a sign, a figure, a letter of another script) but for the
    signs words hold (`_SIGNS_INSIDE_WORDS`); a currency sign against a letter; a capital behind a small letter, or between an ASCII capital
    and a small letter; a small letter that no letter precedes before an ASCII capital; a combining mark that follows no
    letter and no mark, before a Latin letter. The same shapes as those of `_alphabet_bytes`, on characters instead of the
    bytes of one code page: any code page can be judged, and the marks of Arabic or Thai after a space are left alone."""
    characters = [chr(code) for code in range(0x80, 0x3000)]
    latin = [ch for ch in characters if ch.isalpha() and unicodedata.name(ch, "").startswith("LATIN ")]
    small = "".join(ch for ch in latin if ch.islower())
    capital = "".join(ch for ch in latin if ch.isupper())
    marks = "".join(ch for ch in characters if unicodedata.category(ch)[0] == "M")
    currency = "".join(ch for ch in characters if unicodedata.category(ch) == "Sc")
    inside = _SIGNS_INSIDE_WORDS.decode(_WESTERN) + "\u02bc\u2010\u2011"   # the letter apostrophe, the hyphens
    letter = "A-Za-z" + re.escape("".join(latin))
    small, capital, marks, currency, inside = map(re.escape, (small, capital, marks, currency, inside))
    return re.compile(
        "[\x80-\x9f]"
        f"|(?<=[{letter}])[^\x00-\x7f{letter}{marks}{inside}]+(?=[{letter}])"
        f"|(?<=[{letter}])[{currency}]|[{currency}](?=[{letter}])"
        f"|(?<=[a-z{small}])[{capital}]|(?<=[A-Z])[{capital}](?=[a-z{small}])"
        f"|(?<![{letter}])[{small}](?=[A-Z])"
        f"|(?<![^\\W\\d_])(?<![{marks}])[{marks}](?=[{letter}])")


def _plausible_best(matches: CharsetMatches, sample: bytes) -> CharsetMatch | None:
    """The best statistical guess, leaving out a UTF-16 or UTF-32 guess for a sample without NUL bytes unless it reads as
    plausible text in such an encoding (`_wide_text_rank`). Text in those encodings has NUL bytes as soon as it holds an ASCII
    character; without any it is text with no space and no line break — Japanese or Chinese (`日本語の…` → `E5 65 2C 67 …`),
    Korean, or Khmer, Tibetan and the other scripts written without spaces — which charset-normalizer guesses rightly. A short
    broken UTF-8 subject `ab=ZZé` + a cut character was read as UTF-16 and became `慢㵚嫃꧃` — rare ideographs and Javanese
    — or `扡娽썚쎩` without a warning (Pinned by: test_audit_web_fixes.py::TestNoWideEncodingGuessWithoutNulBytes).

    When the first guess that stands is such an encoding, the most plausible of them is taken (the earlier guess on a tie):
    the same bytes often read as text in both byte orders — Khmer `សួស្តី…` with its bytes swapped is `鼗봗鼗툗…`, Han and Hangul
    (Pinned by: test_audit_web_fixes.py::TestWideEncodingGuessOutsidePlainCjk)."""
    if b"\x00" in sample:
        return matches.best()
    best, best_rank = None, 0
    for match in matches:
        if not match.encoding.startswith(("utf_16", "utf_32")):
            if best is None:
                return match
            continue
        rank = _wide_text_rank(sample, match.encoding)
        if rank > best_rank:
            best, best_rank = match, rank
    return best


def _wide_text_rank(sample: bytes, encoding: str) -> int:
    """How plausible is `sample` as text in `encoding` (UTF-16 or UTF-32, no NUL bytes), 0 when it does not decode strictly or
    reads as nothing a file holds. Otherwise, from the most plausible down:

    4. text of one script other than CJK with at least `_WIDE_SCRIPT_MIN_LETTERS` letters, in bytes that cannot be
       byte-oriented text to begin with (`_one_script`, `_control_bytes`) — one byte of every pair is the same, which CJK text
       in the other byte order never is;
    3. CJK text of everyday characters only (`_reads_as_cjk`, `_everyday_cjk`), or one or two letters of another script
       (`12 90` is `递` or Ethiopic `ነ`: undecidable, the earlier guess stands);
    2. other CJK text;
    1. text in the CJK scripts that the ranges of `_reads_as_cjk` turn down, in bytes with a control byte text never holds.

    The one-script readings keep the scripts below U+2000. Judged by the CJK ranges alone, Khmer was read with its bytes
    swapped as Han and Hangul noise without a warning, and Tibetan, Ethiopic, Bengali and Greek became UTF-8 replacement
    characters (Pinned by: test_audit_web_fixes.py::TestWideEncodingGuessOutsidePlainCjk)."""
    text = _strict(sample[:len(sample) - len(sample) % 2], encoding)
    if not text:
        return 0
    binary, spacing = _control_bytes(sample)
    script, letters = _one_script(text) if binary or spacing else (None, 0)
    if script not in (None, _CJK_SCRIPT):
        return 4 if letters >= _WIDE_SCRIPT_MIN_LETTERS else 3
    if _reads_as_cjk(text):
        return 3 if _everyday_cjk(text) else 2
    return 1 if script == _CJK_SCRIPT and binary else 0


def _reads_as_cjk(text: str) -> bool:
    """Is `text` almost all CJK, kana, Hangul, full-width forms, common punctuation, emoji or ideographs outside the Basic
    Multilingual Plane (`_WIDE_TEXT_RANGES`, at most one character in `_WIDE_TEXT_STRAYS` outside)? Han ideographs mixed with Hangul
    syllables count only as Korean with hanja (at least `_WIDE_MIXED_MIN_CHARS` characters, at least three tenths Hangul) or
    as everyday characters throughout (`_everyday_cjk`; in a text shorter than that none of them with a space byte in it,
    the most common byte of byte-oriented text: `袅유法` is `88 85 C7 20 6C D5`) — pairs of arbitrary bytes land in both
    ranges (`ab=ZZé` + a cut character read as UTF-16-LE is `扡娽썚쎩`)."""
    strays = sum(1 for ch in text if not any(low <= ord(ch) <= high for low, high in _WIDE_TEXT_RANGES))
    han = any(0x4E00 <= ord(ch) <= 0x9FFF or 0xF900 <= ord(ch) <= 0xFAFF for ch in text)
    hangul = any(0xAC00 <= ord(ch) <= 0xD7A3 for ch in text)
    if han and hangul:
        # Korean with hanja (`大韓民國의首都는서울이다`): Hangul makes up a good share of a text that is not tiny
        # (Pinned by: test_audit_web_fixes.py::TestNoWideEncodingGuessWithoutNulBytes::test_cjk_text_without_ascii_stays_utf16)
        syllables = sum(1 for ch in text if 0xAC00 <= ord(ch) <= 0xD7A3)
        korean_with_hanja = len(text) >= _WIDE_MIXED_MIN_CHARS and syllables * 10 >= len(text) * _WIDE_MIXED_HANGUL_TENTHS
        space_byte = len(text) < _WIDE_MIXED_MIN_CHARS and any(ord(ch) & 0xFF == 0x20 for ch in text)
        if not korean_with_hanja and (space_byte or not _everyday_cjk(text)):
            return False
    return strays * _WIDE_TEXT_STRAYS <= len(text)


@functools.cache
def _everyday_han() -> frozenset[str]:
    """The ideographs of the national standards that everyday Chinese, Japanese and Korean text is written in: GB 2312 (6,763),
    JIS X 0208 (6,355) and KS X 1001 (4,888) — together about half of the 20,992 ideographs of the main Unicode block."""
    decoded = (bytes((lead, trail)).decode(codec, errors="ignore")
               for codec in ("gb2312", "euc_jp", "euc_kr") for lead in range(0xB0, 0xFF) for trail in range(0xA1, 0xFF))
    return frozenset(ch for ch in decoded if ch and 0x4E00 <= ord(ch) <= 0x9FFF)


def _everyday_cjk(text: str) -> bool:
    """Is every character of `text` an everyday ideograph (`_everyday_han`), an everyday Hangul syllable (`_EVERYDAY_HANGUL`),
    kana, CJK punctuation or a full-width form (`_EVERYDAY_CJK_RANGES`)?

    A Korean name in Japanese or Chinese text or a few hanja in a short Korean one (`東京から서울まで`, `부산是第二大城市`) are
    written with them. Pairs of arbitrary bytes are not: four syllables in five are outside KS X 1001, every other ideograph
    is a rare one, and a space byte makes a symbol (`鋪₆삶`)
    (Pinned by: test_audit_web_fixes.py::TestWideEncodingGuessOutsidePlainCjk::test_everyday_han_with_a_little_hangul)."""
    everyday = _everyday_han()
    return all(ch in everyday or ch in _EVERYDAY_HANGUL or any(low <= ord(ch) <= high for low, high in _EVERYDAY_CJK_RANGES)
               for ch in text)


def _control_bytes(sample: bytes) -> tuple[bool, bool]:
    """Two signs that `sample` cannot be byte-oriented text (UTF-8 or a legacy encoding, whole or broken): does it hold a
    control byte that such text never has (`_NOT_IN_BYTE_TEXT`), and are at least two fifths of it tabs, line breaks and the
    like (`_TEXT_CONTROL_BYTES`)?

    Broken UTF-8 and legacy text — what the UTF-16 guess must not swallow — is letters, digits, punctuation and bytes above
    0x7F, with a tab or a line break here and there."""
    spacing = sum(sample.count(byte) for byte in _TEXT_CONTROL_BYTES)
    return _NOT_IN_BYTE_TEXT.search(sample) is not None, spacing * 5 >= len(sample) * _WIDE_CONTROL_FIFTHS


def _one_script(text: str) -> tuple[str | None, int]:
    """(script, number of its letters) when `text` (its first `_WIDE_SCRIPT_HEAD` characters) is printable text in one script,
    otherwise (None, 0): at most one character in `_WIDE_TEXT_STRAYS` is unprintable, and at most that share of the letters,
    marks and digits is outside the script most of them belong to. The script is the first word of the Unicode character name
    (`KHMER`, `TIBETAN`, `GREEK`); the CJK scripts count as one (`_CJK_NAME_WORDS`).

    Pairs of arbitrary bytes land on unassigned and private-use code points and in another script every few characters; Khmer,
    Tibetan or Greek text stays in its own block."""
    head = text[:_WIDE_SCRIPT_HEAD]
    scripts: dict[str, int] = {}
    unprintable = 0
    for ch in head:
        if not _printable(ch):
            unprintable += 1
        elif unicodedata.category(ch)[0] in "LMN":
            word = _NAME_WORD_END.split(unicodedata.name(ch, ""), maxsplit=1)[0]
            script = _CJK_SCRIPT if word in _CJK_NAME_WORDS else word
            scripts[script] = scripts.get(script, 0) + 1
    letters = sum(scripts.values())
    if not letters or unprintable * _WIDE_TEXT_STRAYS > len(head):
        return None, 0
    script = max(scripts, key=scripts.__getitem__)
    return (script, scripts[script]) if (letters - scripts[script]) * _WIDE_TEXT_STRAYS <= letters else (None, 0)


def _strict(data: bytes, encoding: str) -> str | None:
    """The string if it decodes strictly as `encoding`, otherwise None."""
    try:
        return data.decode(encoding)
    except (UnicodeDecodeError, LookupError):
        return None


def _declared_codec(name: str | None) -> str | None:
    """charset name declared by the document → Python codec name. None for unknown names and for the UTF family (already decided
    by BOM, strict decoding and NUL byte positions), and for a codec only Windows has, which is an unknown name everywhere
    (`portable_codec`: `<meta charset="ansi">` was read in the machine's ANSI code page on Windows and `charset=cp20866` as
    KOI8-R on Windows under Python 3.14, and as a charset Python does not know elsewhere — Pinned by:
    test_codecs.py::TestDeclaredCharset)."""
    label = (name or "").strip().lower()
    if not label or _NOT_A_CHARSET.search(label):
        return None
    # A name with a NUL byte in it (`utf-8\x00`) is a ValueError of the lookup — the whole mail was rejected; `portable_codec`
    # takes it for an unknown name
    codec = _DECLARED_CODECS.get(label) or portable_codec(label)
    if codec is None:
        return None
    codec = _DECLARED_CODECS.get(codec, codec)
    return None if codec.startswith("utf") else codec


def _lookup(name: str | None) -> str | None:
    """The Python codec name of a charset name, None when it is unknown."""
    try:
        return codecs.lookup(name.strip()).name if name and name.strip() else None
    except (LookupError, ValueError):
        return None


def _declares_utf8(name: str | None) -> bool:
    return _lookup(name) == "utf-8"


def _mostly_utf8(data: bytes) -> bool:
    """Is text that declares UTF-8 and failed strict decoding still UTF-8 with a few broken bytes: it has a complete multi-byte
    character for every replacement character, and CP949 reads it with no fewer replacement characters.

    A declared UTF-8 page or mail part with one broken byte and only a few accented letters fell short of `_almost_utf8` and
    was read as Windows-1252 or Windows-1250 without a warning (`café` → `cafÃ©`). Text with no multi-byte character at all
    (Windows-1252 labelled UTF-8, as old servers send it) and CP949 Hangul labelled UTF-8, which CP949 reads cleanly, are left
    to the steps below: `기간` is `B1 E2 B0 A3`, whose last three bytes are the UTF-8 character `Ⱓ`
    (Pinned by: test_text_html.py::TestDeclaredUtf8WithABrokenByte)."""
    text = data.decode("utf-8", errors="replace")
    replaced = text.count("\ufffd")
    multibyte = sum(1 for ch in text if ord(ch) > 0x7F) - replaced
    return 1 <= multibyte and replaced <= multibyte and replaced <= data.decode(_KOREAN_LEGACY, errors=_CP949_PAIR_REPLACE).count("\ufffd")


def _declared_utf16(data: bytes, name: str | None) -> str | None:
    """"utf-16-le"/"utf-16-be" when the text declares that byte order (a mail part `charset=utf-16le`), decodes strictly into
    printable text (`_printable`) and its bytes bear the label out, otherwise None (the steps below then read it like any
    other text). The bytes bear it out in one of two ways:

    1. At least half of the characters are below U+2000, the high byte of the pair being a NUL or a control byte: ASCII
       (`xx 00`), Cyrillic (`xx 04`), Arabic (`xx 06`), Devanagari (`xx 09`), Thai (`xx 0E`). Byte-oriented text has a control
       byte here and there, not in every other position of one parity. Such text is often valid UTF-8 as well — a Russian or
       an Arabic word without a space has not even a NUL — so 'valid UTF-8 without a NUL' cannot be the test for a wrong label.
    2. Otherwise it is not valid UTF-8, and every character outside ASCII is an everyday CJK one (`_everyday_cjk`): Korean,
       Japanese or Chinese text, whose byte pairs are otherwise as ordinary as those of byte-oriented text.

    The label alone is not enough: pairs of ASCII bytes are nearly always printable ideographs, so an ASCII page with
    `<meta charset="utf-16le">` and a mail part `charset="utf-16le"` holding `Total: 1,250 USD` came out as `格浴㹬…` and
    `潔慴㩬ㄠ㈬〵唠䑓` without a warning; 82% of English snippets of 8 bytes and 34% of those of 32 bytes did. So did UTF-8 Korean,
    Windows-1252 and broken UTF-8 with that label, and UTF-16 labelled with the other byte order
    (Pinned by: test_text_html.py::TestDeclaredUtf16WithoutBom).

    Text made only of printable ASCII bytes, tab, CR and LF is ASCII whatever the label says (`_PLAIN_ASCII`). A tab or a
    line break in the high-byte position is the control byte of way 1: the mail body `Yes\n` read `教ੳ`, `5\n` read `ਵ` and a
    table of one-digit numbers between tabs read as Devanagari — 9% of the pieces of 1 to 200 characters cut from ASCII
    text and labelled UTF-16 (7,356 of 80,480), up to 32 characters long. Real UTF-16 has such bytes only when it holds no
    space, no line break and no ASCII character at all (each of them has a NUL), and every character is one like U+0928
    or U+4E2A whose two bytes are both printable or tab / CR / LF: 0.4% of the pieces cut from Korean, Japanese, Chinese,
    Russian, Arabic, Hindi, Punjabi, Thai, Greek, Hebrew and English text (1,593 of 399,712), all of them one to six letters
    of Hindi or Punjabi (`नमस्ते` is `(\t.\t8\tM\t$\tG\t`) — the Japanese and Chinese ones (one to eight characters) were read as
    UTF-8 already (Pinned by: test_text_html.py::TestDeclaredUtf16WithoutBom::test_plain_ascii_bytes_are_ascii_whatever_the_label).
    Known accepted gap: such a word — a single word of up to eight Devanagari, Gurmukhi or Malayalam letters without a space or a line break, two to
    five ideographs or kana whose bytes all are ASCII (`你好` is `` `O}Y ``; at most 94 of 19,800 pieces of 2 to 100 characters
    cut from Japanese or Chinese text, 3 from Korean) — is read as the ASCII it also is."""
    codec = {"utf-16-le": "utf-16-le", "utf-16-be": "utf-16-be"}.get(_lookup(name) or "")
    if codec is None or len(data) % 2 or _PLAIN_ASCII.fullmatch(data):
        return None
    text = _strict(data, codec)
    if not text or not all(_printable(ch) for ch in text[:20_000]):
        return None
    if _high_bytes_are_controls(data, codec):
        return codec
    return codec if _strict(data, "utf-8") is None and _everyday_cjk("".join(ch for ch in text[:20_000] if not ch.isascii())) else None


def _high_bytes_are_controls(data: bytes, codec: str) -> bool:
    """Are at least half of the bytes at the high-byte side of the head (`_high_bytes`) below 0x20 — the shape of UTF-16 text
    of characters below U+2000: ASCII (`xx 00`), Cyrillic (`xx 04`), Arabic (`xx 06`), Devanagari (`xx 09`)."""
    high_bytes = _high_bytes(data, codec)
    return sum(1 for byte in high_bytes if byte < 0x20) * 2 >= len(high_bytes)


def _high_bytes_of_one_block(data: bytes, codec: str) -> bool:
    """Are nine in ten bytes at the high-byte side of the head (`_high_bytes`) either NUL or one and the same control byte —
    the shape of UTF-16 text of one script below U+2000 and ASCII: every Cyrillic letter has 0x04, every Thai one 0x0E, every
    space, digit and comma 0x00. Arbitrary control bytes with a few NULs have several values there (`\\x00\\x02\\x04\\x02a\\x01`
    read `ȀȄšȄ`), and ASCII or UTF-8 text with a stray NUL and SOH has letters there."""
    high_bytes = _high_bytes(data, codec)
    controls = Counter(byte for byte in high_bytes if 0 < byte < 0x20)
    return (high_bytes.count(0) + max(controls.values(), default=0)) * 10 >= len(high_bytes) * 9


def _high_bytes(data: bytes, codec: str) -> bytes:
    """The bytes at the high-byte side of the head: odd positions for "utf-16-le", even ones for "utf-16-be"."""
    return data[:8192][1 if codec == "utf-16-le" else 0::2]


def _holds_nuls_and_foreign_controls(sample: bytes) -> bool:
    """Does the sample hold a NUL byte and a control byte that byte-oriented text never holds (`_NOT_IN_BYTE_TEXT`)? Then a
    strict UTF-8 or CP949 reading is no evidence against UTF-16 (`decode_text`, `utf16_encoding`)."""
    return b"\x00" in sample and _NOT_IN_BYTE_TEXT.search(sample) is not None


def _alternating_nuls(data: bytes) -> bool:
    """Does the head hold NUL bytes at nearly every other byte, nearly all at one parity — the shape of UTF-16 text that is mostly
    ASCII. A third of the bytes at least (NUL-terminated names `Kim\x00Lee\x00Han\x00` have a quarter, and read `楋m敌e慈n`),
    and nine in ten of them at the same parity (fields between two NULs, `A\x00\x00BC\x00\x00D`, have half at each)
    (Pinned by: test_text_html.py::TestAsciiUtf16WithoutBom)."""
    sample = data[:8192]
    nuls = sample.count(0)
    if not nuls or nuls * 3 < len(sample):
        return False
    odd = sample[1::2].count(0)
    return max(odd, nuls - odd) * 10 >= nuls * 9


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
    if _strict(data, _WESTERN) is None:
        return False
    western = next((match for match in matches if _WESTERN in (match.encoding, *match.could_be_from_charset)), None)
    return best is None or best.coherence <= (western.coherence if western is not None else 0.0)


def _alphabet_page(sample: bytes) -> str | None:
    """The Latin code page this text is in, when the letters outside ASCII leave no other reading: they are the alphabet of
    one language in that code page, and the text reads no other way in any code page of `_ALPHABETS`. None says nothing, and
    the statistical guess decides as it always did.

    Language statistics cannot tell these code pages apart in a short text: `martedì` in Windows-1252 was read as Windows-1258
    (the `ì` a tone mark on the `d`), `reunión` as CP775 (`reuni¾n`), Polish in ISO 8859-2 as Windows-1252 (`obecno¶æ`). The
    bytes can: `é ì à` are Italian letters in Windows-1252 and the letters of no one language in Windows-1250 (`é ě ŕ`: `ŕ` is
    Slovak, `ě` Czech), and `ą ę ł ś ż` are Polish in Windows-1250 and mostly signs in Windows-1252.

    A reading fits when every byte outside ASCII is a letter of one alphabet or a sign of that code page, and it is a text one
    would write (`_alphabet_bytes`: `r‚union` and `libertŽ` are `réunion` of DOS and `liberté` of a Macintosh; Windows-1258
    only with a tone mark, each on a vowel). Then
    - Windows-1252 is the answer when a Western alphabet fits with `_WESTERN_LETTERS` different letters of the Latin-1 block
      that are at least every other byte outside ASCII (`Š œ ž` below that block, and in Macintosh and DOS text every accent
      but a stray one, are signs; signs alone name no language), every other code page that fits reads the same text, and
      none of the bytes that are letters in ISO 8859-15 is there. When another alphabet reads it differently (`è` is `č` in
      Windows-1250), nothing is said;
    - another code page is the answer only when the text cannot be Windows-1252 at all - a byte it does not define, or a
      reading nobody would write, `wys³aæ` - and the code pages that fit read one text with `_OTHER_ALPHABET_LETTERS`
      different letters that Windows-1252 reads as something else. Text that Windows-1252 reads as letters is left alone
      even when they are the letters of no one Western language: one foreign name does that to a French text (`Žižek` +
      `thèse à` is `thčse ŕ` in Windows-1250), and `µ ¥ © «` are letters in ISO 8859-2.
    The first version let another alphabet win on its letters alone and let the statistics choose among the code pages that
    fit: Western text with `µ` or one Slavic name became Slovak or Czech, and English with `£ ©` Windows-1250
    (Pinned by: test_text_html.py::TestTextOfOneAlphabet).
    Known accepted gaps: text of a code page that is not in `_ALPHABETS` (DOS, Macintosh, ISO 8859-4) whose accents happen
    to be two Latin-1 letters of one Western alphabet - capitals mostly, `VÖDEOS … SESSIÓ` - is read as Windows-1252; and
    Central European or Baltic text with a foreign name in which every accent is a letter of one Western alphabet.
    """
    outside = sample.translate(None, _SEVEN_BITS)
    if not outside or len(sample) - len(sample.translate(None, _ASCII_LETTERS)) < _ALPHABET_ASCII_LETTERS * len(outside):
        return None
    if b"\x00" in sample:
        return None  # no text of single bytes: UTF-32 without a BOM is `L\0\0\0u\0\0\0`, its accents sparse
    high = frozenset(outside)
    rows = [row for row in _alphabet_bytes() if high & row[1].keys() and high <= row[2]]
    if not rows:
        return None  # signs alone, or letters of no alphabet
    # What stands around the bytes outside ASCII, each kind once: all that the searches for what nobody writes look at (two
    # bytes before and one behind)
    around = b"  ".join({sample[max(found.start() - 2, 0):found.end() + 1] for found in _HIGH_BYTES.finditer(sample)})
    written = {page: unwritten.search(around) is None for page, _, _, unwritten in rows}
    letters_read: dict[str, int] = {}
    for page, letters, _, _ in rows:
        if not written[page] or (page == _VIETNAMESE and high.isdisjoint(_VIETNAMESE_MARKS)):
            continue
        if page == _WESTERN:
            # Its different letters of the Latin-1 block, when they are at least every other byte outside ASCII: in
            # Macintosh or DOS text read so, the accents are signs (`Ubicaci—n`, `‡lbumes`) and one `¿` is an `À`
            latin_1 = [byte for byte in high & letters.keys() if byte >= _LATIN_1_LETTERS]
            count = len({letters[byte] for byte in latin_1})
            count = count if 2 * sum(outside.count(byte) for byte in latin_1) >= len(outside) else 0
        else:
            count = len({letters[byte] for byte in high & letters.keys() if _reads_otherwise(page, byte)})
        letters_read[page] = max(count, letters_read.get(page, 0))
    texts = {page: _strict(sample, page) for page in letters_read}
    if len(set(texts.values())) != 1 or None in texts.values():
        return None
    if _WESTERN in letters_read:
        return _WESTERN if letters_read[_WESTERN] >= _WESTERN_LETTERS and high.isdisjoint(_ISO_8859_15_BYTES) else None
    if _strict(sample, _WESTERN) is not None and _alphabet_bytes()[0][3].search(around) is None:
        return None  # it may be Windows-1252 as well
    page = max(letters_read, key=letters_read.__getitem__)
    return page if letters_read[page] >= _OTHER_ALPHABET_LETTERS else None


@functools.cache
def _reads_otherwise(page: str, byte: int) -> bool:
    """Does `page` read this byte as another character than Windows-1252 does?"""
    return bytes((byte,)).decode(page, errors="ignore") != bytes((byte,)).decode(_WESTERN, errors="ignore")


@functools.cache
def _alphabet_bytes() -> tuple[tuple[str, dict[int, str], frozenset[int], re.Pattern[bytes]], ...]:
    """Of each alphabet of `_ALPHABETS`: its code page, the small letter of each byte that is a letter of it (both cases; the
    tone marks for Vietnamese),
    those and the bytes that are a sign in that code page (no letter, no mark, no control and not undefined), and a search
    for what nobody writes: a sign between two letters, a capital behind a small letter or between a capital and a small
    one, a small accented letter before a capital that no small letter precedes (`STêLEANNA`, `êTEMS` are `STÍLEANNA`, `ÍTEMS`
    of a Macintosh) or ending a word behind two capitals (`SESSIà` is `SESSIÓ` of DOS), a Vietnamese mark off a vowel."""
    rows = []
    every = range(0x80, 0x100)
    for page, lowercase in _ALPHABETS:
        both = lowercase + "".join(letter.upper() for letter in lowercase if len(letter.upper()) == 1)
        letters = {byte: character.lower() for character in both for byte in character.encode(page) if byte >= 0x80}
        if page == _VIETNAMESE:
            letters.update((byte, chr(byte)) for byte in _VIETNAMESE_MARKS)
        characters = {byte: bytes((byte,)).decode(page, errors="ignore") for byte in every}
        kinds = {byte: unicodedata.category(character) if character else "Cn" for byte, character in characters.items()}
        signs = frozenset(byte for byte in every if kinds[byte][0] not in "LMC")
        word = b"[A-Za-z" + re.escape(bytes(byte for byte in every if kinds[byte][0] in "LM")) + b"]"
        inside = re.escape(bytes(sorted(signs - letters.keys() - frozenset(_SIGNS_INSIDE_WORDS))))
        capitals = re.escape(bytes(byte for byte in every if kinds[byte] == "Lu"))
        smalls = re.escape(bytes(byte for byte in every if kinds[byte] == "Ll" and len(characters[byte].upper()) == 1))
        unwritten = (b"(?<=" + word + b")[" + inside + b"](?=" + word + b")|(?<=[a-z" + smalls + b"])[" + capitals + b"]"
                     b"|(?<=[A-Z])[" + capitals + b"](?=[a-z])|(?<![a-z])[" + smalls + b"](?=[A-Z])"
                     b"|(?<=[A-Z][A-Z])[" + smalls + b"](?![a-z" + smalls + b"])")
        if page == _VIETNAMESE:
            unwritten += b"|" + _MARK_OFF_A_VOWEL.pattern
        rows.append((page, letters, signs | letters.keys(), re.compile(unwritten)))
    return tuple(rows)


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


def _cut_ascii_character(data: bytes, utf16: str) -> str:
    """The last character of mostly-ASCII little-endian UTF-16 (`_alternating_nuls`) cut after its first byte, when that byte
    and the character before it are ASCII; otherwise "".

    The first byte of a little-endian character is the letter itself and the missing one its NUL. Dropped with the odd
    byte, the last letter of a file cut one byte short was lost (`OK thanks` → `OK thank`), where reading the same bytes
    as UTF-8 kept it (Pinned by: test_text_html.py::TestAsciiUtf16WithoutBom::test_last_letter_cut_after_its_first_byte_is_kept)."""
    if not len(data) % 2 or utf16 != "utf-16-le" or len(data) < 3 or data[-2] != 0:
        return ""
    last = chr(data[-1])
    return last if last.isascii() and (last.isprintable() or last in _TEXT_CONTROLS) else ""


def utf16_encoding(data: bytes, *, by_nul_side_only: bool = False) -> str | None:
    """Return "utf-16-le"/"utf-16-be" for BOM-less UTF-16, otherwise None. `by_nul_side_only` tries only the byte order the NUL
    positions point to.

    UTF-16 text saved without a BOM by Windows tools was rejected as binary because of its NUL bytes. The byte order candidates
    are ordered by the NUL byte positions of ASCII characters (space, line break, digits), and the data counts as UTF-16 only
    when it decodes strictly and consists entirely of printable characters (common control characters such as \\f and ESC, and
    space and format characters, are allowed — `_printable`). With an odd length the last byte is dropped first. Arbitrary binary
    data mixes in unassigned and private-use characters and fails the printable check. UTF-8/CP949 text in which NUL bytes are
    rare (a note with one stray NUL byte) is not seen as UTF-16 — CP949 text paired two bytes at a time sometimes decodes to
    printable characters, and 9 of 2,000 random Hangul texts were garbled as UTF-16
    (Pinned by: test_text_html.py::TestTolerantTextDetection, TestLegacyTextWithNulBytes). Unless the head also holds a control
    byte such text never has (`_holds_nuls_and_foreign_controls`): then only a byte order whose high-byte side is NUL or one
    control byte (`_high_bytes_of_one_block`) is taken. Cyrillic UTF-16 has NULs only after its spaces and punctuation and is
    ASCII without them, so a paragraph of it was rejected as binary
    (Pinned by: test_detect.py::TestDetectFormat::test_cyrillic_utf16_paragraph_without_a_bom_is_text).
    """
    sample = data[:8192]
    nul_count = sample.count(0)
    rare_nuls = nul_count * 10 < len(sample) and (is_utf8_text(sample) or is_cp949_text(sample.replace(b"\x00", b"")))
    if not nul_count or (rare_nuls and not _holds_nuls_and_foreign_controls(sample)):
        return None
    data = data[:len(data) - len(data) % 2]
    sample = data[:8192]
    little = sum(1 for index in range(1, len(sample), 2) if sample[index] == 0 and sample[index - 1] != 0)
    big = sum(1 for index in range(0, len(sample) - 1, 2) if sample[index] == 0 and sample[index + 1] != 0)
    candidates = ("utf-16-le", "utf-16-be") if little >= big else ("utf-16-be", "utf-16-le")

    for encoding in candidates[:1] if by_nul_side_only else candidates:
        if rare_nuls and not _high_bytes_of_one_block(data, encoding):
            continue  # looked at in the head before the whole input is decoded
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
