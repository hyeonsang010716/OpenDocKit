"""The codecs a document may name (`extractors/_codecs.py`): a codec only Windows has is read as a name Python does not know.

Windows lends Python the codecs of the machine's own code pages (`mbcs`, also named `ansi` and `dbcs`, and `oem`), and from
Python 3.14 one for any code page Windows converts (`cp20866`, `cp0`). A document that named one was read in it on Windows
only. The decision is in docs/decisions/encoding.md.

`lent_codecs` registers a search function that lends such codecs the way Windows does, so these tests read a document with
them on every system, not only where Windows itself lends them. Each test compares a document that names such a codec
with the same document naming a charset no system has: the two must be read alike.
"""

import base64
import codecs
import encodings.aliases
from io import BytesIO
import re
import zipfile

import pytest

from opendockit import ConversionError, convert
from opendockit.extractors import spreadsheet
from opendockit.extractors._codecs import lent_by_windows, portable_codec
from opendockit.extractors._encoding import _declared_codec, decode_text
from tests import builder


# Names that lead to a codec only Windows has: what Windows itself lends (`ansi` and `dbcs` are `mbcs`), and the code pages
# of Python 3.14 (`cp0` to `cp3` are the ANSI, OEM, Macintosh and thread code pages of the machine; xlrd names 708 and 1047
# of a workbook `cp708` and `cp1047`).
WINDOWS_ONLY = ("ansi", "dbcs", "mbcs", "oem", "cp0", "cp1", "cp2", "cp3", "cp708", "cp1047", "cp20866", "cp51949")
UNKNOWN = "x-no-such-charset"
# KOI8-R, what each lent codec reads (`lent_codecs`): it is no codec Python has under these names, and the reading that
# shows a lent codec was used. A name Python does not know reads these bytes otherwise.
RUSSIAN = "Привет мир"
KOI8 = RUSSIAN.encode("koi8-r")


def _lent(name: str) -> codecs.CodecInfo:
    """A codec under `name` that reads KOI8-R: the shape of what Windows lends, `mbcs` or `cp20866`."""
    koi8 = codecs.lookup("koi8-r")
    return codecs.CodecInfo(koi8.encode, koi8.decode, koi8.streamreader, koi8.streamwriter, koi8.incrementalencoder,
                            koi8.incrementaldecoder, name=name)


def _lend(name: str) -> codecs.CodecInfo | None:
    if name in ("mbcs", "ansi", "dbcs"):
        return _lent("mbcs")
    if name == "oem":
        return _lent("oem")
    return _lent(name) if re.fullmatch(r"cp\d+", name) and name in WINDOWS_ONLY else None


@pytest.fixture
def lent_codecs():
    """Every name of `WINDOWS_ONLY` leads to a codec, as on Windows: Windows' own where it lends one (it is asked first), the
    one of `_lend` elsewhere. Unregistered afterwards, which also empties the lookup cache."""
    codecs.register(_lend)
    try:
        for name in WINDOWS_ONLY:
            codecs.lookup(name)   # each name is a codec now: the tests below read documents with it
        yield
    finally:
        codecs.unregister(_lend)


class TestPortableCodec:
    def test_every_codec_python_has_is_given_as_codecs_gives_it(self):
        checked = 0
        for name in sorted(set(encodings.aliases.aliases) | set(encodings.aliases.aliases.values())):
            try:
                codec = codecs.lookup(name).name
            except LookupError:
                continue   # `mbcs` and `oem` off Windows, and names Python lists for codecs it lacks here
            checked += 1
            assert portable_codec(name) == (None if codec in ("mbcs", "oem") else codec), name
        assert checked > 300

    def test_code_pages_python_has_on_every_system_are_kept(self):
        assert [portable_codec(name) for name in ("cp949", "CP949", "cp1252", "cp437", "cp65001", "windows-1251", "euc-kr")] == [
            "cp949", "cp949", "cp1252", "cp437", "utf-8", "cp1251", "euc_kr"]

    def test_codecs_only_windows_has_are_none(self, lent_codecs):
        assert [portable_codec(name) for name in WINDOWS_ONLY] == [None] * len(WINDOWS_ONLY)

    def test_codecs_windows_itself_lends_are_none(self):
        # On Windows these are Windows' own (`cp20866` from Python 3.14); elsewhere they are no codec at all
        assert [portable_codec(name) for name in WINDOWS_ONLY] == [None] * len(WINDOWS_ONLY)

    @pytest.mark.parametrize("name", [None, "", UNKNOWN, "utf-8\x00", "cp99998"])
    def test_no_codec_is_none(self, name):
        assert portable_codec(name) is None

    def test_lent_by_windows_is_a_codec_found_that_is_not_portable(self, lent_codecs):
        assert all(lent_by_windows(name) for name in WINDOWS_ONLY)
        assert not any(lent_by_windows(name) for name in (None, "", UNKNOWN, "utf-8\x00", "koi8-r", "cp949", "utf-8", "latin-1"))


def _outcome(data: bytes, filename: str) -> tuple:
    """What `convert` gives: the text and the warnings, or the kind and the sentence of the error."""
    try:
        result = convert(data, filename)
    except ConversionError as error:
        return type(error).__name__, str(error)
    return result.text, result.warnings


class TestDeclaredCharset:
    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_is_not_a_codec(self, lent_codecs, name):
        assert _declared_codec(name) is None and _declared_codec(name.upper()) is None

    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_text_is_read_as_under_an_unknown_name(self, lent_codecs, name):
        assert decode_text(KOI8, name) == decode_text(KOI8, UNKNOWN)

    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_html_is_read_as_under_an_unknown_name(self, lent_codecs, name):
        def page(charset: str) -> bytes:
            return b'<html><head><meta charset="' + charset.encode("ascii") + b'"></head><body><p>' + KOI8 + b"</p></body></html>"

        assert convert(page(name), "a.html").text == convert(page(UNKNOWN), "a.html").text

    def test_a_codec_every_system_has_is_still_used(self, lent_codecs):
        assert decode_text(KOI8, "koi8-r") == (RUSSIAN, "koi8-r")


class TestMail:
    @staticmethod
    def _mail(charset: str) -> bytes:
        name = charset.encode("ascii")
        escaped = "".join(f"%{byte:02X}" for byte in KOI8)
        return (b"Subject: =?" + name + b"?B?" + base64.b64encode(KOI8) + b"?=\r\n"
                b"From: a@b.c\r\nMIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=\"B\"\r\n\r\n"
                b"--B\r\nContent-Type: text/plain; charset=\"" + name + b"\"\r\n\r\n" + KOI8 + b"\r\n"
                b"--B\r\nContent-Type: application/octet-stream\r\nContent-Disposition: attachment; filename*=" + name + b"''"
                + escaped.encode("ascii") + b".pdf\r\nContent-Transfer-Encoding: base64\r\n\r\nAAAA\r\n--B--\r\n")

    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_body_and_subject(self, lent_codecs, name):
        assert convert(self._mail(name), "a.eml").text == convert(self._mail(UNKNOWN), "a.eml").text

    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_attachment_name(self, lent_codecs, name):
        assert convert(self._mail(name), "a.eml").warnings == convert(self._mail(UNKNOWN), "a.eml").warnings

    def test_a_codec_every_system_has_is_still_used(self, lent_codecs):
        result = convert(self._mail("koi8-r"), "a.eml")

        assert result.text.startswith(f"Subject: {RUSSIAN}") and result.text.endswith(RUSSIAN)
        assert f"({RUSSIAN}.pdf)" in result.warnings[0]


class TestRtf:
    @staticmethod
    def _rtf(page: str) -> bytes:
        return ("{\\rtf1\\ansi\\ansicpg" + page + " " + "".join(f"\\'{byte:02x}" for byte in KOI8) + "}").encode("ascii")

    @pytest.mark.parametrize("page", [name.removeprefix("cp") for name in WINDOWS_ONLY if name.startswith("cp")])
    def test_document_code_page_is_read_as_an_unknown_one(self, lent_codecs, page):
        assert convert(self._rtf(page), "a.rtf").text == convert(self._rtf("99998"), "a.rtf").text

    def test_a_code_page_every_system_has_is_still_used(self, lent_codecs):
        assert convert(self._rtf("1251"), "a.rtf").text == RUSSIAN.encode("koi8-r").decode("cp1251")


class TestSpreadsheetStyles:
    @staticmethod
    def _head(encoding: str) -> bytes:
        return b'<?xml version="1.0" encoding="' + encoding.encode("ascii") + b'"?><styleSheet/>'

    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_declared_codec_of_the_styles_part_is_one_python_does_not_know(self, lent_codecs, name):
        assert spreadsheet._styles_codec(self._head(name)) is None

    def test_a_codec_every_system_has_is_still_used(self, lent_codecs):
        assert spreadsheet._styles_codec(self._head("koi8-r")) == "koi8-r"


class TestSpreadsheetParts:
    """A sheet or a string table whose XML declaration is not UTF-8 is read by openpyxl's parser, expat, which reads an
    encoding it does not know itself through Python's codecs."""

    @staticmethod
    def _xlsx(encoding: str, part: str) -> bytes:
        with zipfile.ZipFile(BytesIO(builder.xlsx_bytes())) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        declaration = b'<?xml version="1.0" encoding="' + encoding.encode("ascii") + b'"?>'
        sheet = re.sub(rb"^<\?xml[^>]*\?>", b"", parts["xl/worksheets/sheet1.xml"]).lstrip()
        main = b"http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        if part == "sheet":
            cell = b'<c r="A1" t="inlineStr"><is><t>' + KOI8 + b"</t></is></c>"
            parts["xl/worksheets/sheet1.xml"] = declaration + re.sub(
                rb"<sheetData>.*</sheetData>|<sheetData/>", b'<sheetData><row r="1">' + cell + b"</row></sheetData>", sheet, flags=re.S)
        else:
            parts["xl/worksheets/sheet1.xml"] = re.sub(
                rb"<sheetData>.*</sheetData>|<sheetData/>", b'<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>',
                sheet, flags=re.S)
            parts["xl/sharedStrings.xml"] = (declaration + b'<sst xmlns="' + main + b'" count="1" uniqueCount="1"><si><t>' + KOI8
                                             + b"</t></si></sst>")
            parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
                b"</Types>", b'<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument'
                             b'.spreadsheetml.sharedStrings+xml"/></Types>')
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items():
                archive.writestr(name, data)
        return buffer.getvalue()

    @pytest.fixture(params=[False, True], ids=["full-load", "streamed"])
    def streamed(self, request, monkeypatch):
        """Both ways a sheet is read: loaded whole, and streamed (a merged-cell cap below zero streams every sheet)."""
        if request.param:
            monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)

    @pytest.mark.parametrize("part", ["sheet", "strings"])
    @pytest.mark.parametrize("name", WINDOWS_ONLY)
    def test_is_read_as_under_an_unknown_name(self, lent_codecs, streamed, name, part):
        assert _outcome(self._xlsx(name, part), "a.xlsx") == _outcome(self._xlsx(UNKNOWN, part), "a.xlsx")

    @pytest.mark.parametrize("part", ["sheet", "strings"])
    def test_a_codec_every_system_has_is_still_used(self, lent_codecs, streamed, part):
        assert RUSSIAN in convert(self._xlsx("koi8-r", part), "a.xlsx").text


class TestXls:
    """xlrd names the codec of a code page it has no name for `cpNNN` and stops when Python has none."""

    @staticmethod
    def _xls(page: int) -> bytes:
        return builder.raw_biff_xls([["Name", "No"], ["Total", 1]], encoding="ascii", codepage=page)

    @pytest.mark.parametrize("page", [708, 1047])
    def test_code_page_is_one_python_does_not_know(self, lent_codecs, page):
        assert _outcome(self._xls(page), "a.xls") == _outcome(self._xls(999), "a.xls")

    def test_a_code_page_every_system_has_is_still_read(self, lent_codecs):
        assert "| Total | 1 |" in convert(self._xls(1256), "a.xls").text
