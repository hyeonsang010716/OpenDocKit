"""Document format detection.

The **content (magic bytes)**, not the extension, decides. The extension of an uploaded file often lies:
the "Excel download" of Korean web systems serves an HTML table as `.xls`, and users rename files before uploading.
The extension is used only to split the text family (TXT, MD, CSV, HTML), which cannot be told apart by content.
"""

import csv
from io import BytesIO
import json
from pathlib import PurePath
import re
import struct
import zipfile

import olefile

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError, UnsupportedFormatError
from opendockit.extractors.html import is_mhtml, mail_reads_as_html, mhtml_html_part
from opendockit.extractors.text import (
    decode_text,
    is_cp949_text,
    is_encrypted_mail,
    is_utf8_text,
    mail_box,
    unicode_bom_codec,
    utf16_encoding,
)
from opendockit.image import is_image_file
from opendockit.models import DocumentFormat


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"
_HWP3_MAGIC = b"HWP Document File V3"
_PDF_MAGIC = b"%PDF-"
_RTF_MAGIC = b"{\\rtf"
# SVG image: the top element (after a DOCTYPE) is <svg>. Passed through as text, the path coordinates become the body.
_SVG_ROOT = re.compile(rb"(?:<!doctype\s+svg\b[^>]*>\s*)?<svg[\s>]", re.IGNORECASE)
# Found only in a PDF body: the cross-reference table position (startxref) or an object header (`12 0 obj`).
_PDF_BODY = re.compile(rb"startxref|\d+\s+\d+\s+obj\b")
# Top element of an Excel 2003 XML spreadsheet. Its `<Table>` matched the HTML sniff below (case-insensitive) and it was read as HTML.
_XML_SPREADSHEET_SNIFF = re.compile(rb"<(?:\w+:)?Workbook\b[^>]*urn:schemas-microsoft-com:office:spreadsheet")
_HTML_SNIFF = re.compile(rb"<!doctype\s+html|<html[\s>]|<head[\s>]|<body[\s>]|<table[\s>]|<meta\s", re.IGNORECASE)
# An "Excel download" that serves only a table fragment (starting with <tr> or <td>) is HTML too. Checked only for documents that start
# with a tag.
_HTML_MARKUP_SNIFF = re.compile(rb"<!doctype\s+html|<(?:html|head|body|table|tr|td|th)[\s>]", re.IGNORECASE)
# A document that starts with prose must also have a closing tag to be HTML. A log or note that merely writes a tag name once is text.
_HTML_CLOSING_SNIFF = re.compile(rb"</(?:html|head|body|table|tr|td|th|title|div|p)\s*>", re.IGNORECASE)
# HTML table inside a `.csv`/`.tsv` (`_is_html_table_export`): a leading HTML document tag, a table tag and its closing tag.
_HTML_EXPORT_START = re.compile(rb"<!doctype\s+html|<(?:html|table|head|body|style)[\s>]|<meta\s", re.IGNORECASE)
_TABLE_TAG = re.compile(rb"<(?:table|tr|td)[\s>]", re.IGNORECASE)
_TABLE_CLOSING_TAG = re.compile(rb"</(?:table|tr|td)\s*>", re.IGNORECASE)
# Leading whitespace, comments and XML declaration. If the first character after them is `<`, the document starts with a tag.
_LEADING_MARKUP_NOISE = re.compile(rb"(?:\s+|<!--.*?-->|<\?xml[^>]*\?>)*", re.DOTALL)
# Range searched for HTML signals. Export files that start with a long <style> or comment had no signal within 2KB.
_HTML_SNIFF_BYTES = 64 * 1024
# XML prolog (whitespace, comments, processing instructions, DOCTYPE). What follows them is the top element.
_XML_PROLOG = re.compile(rb"(?:\s+|<!--.*?-->|<\?[^>]*\?>|<!doctype[^>]*>)*", re.DOTALL | re.IGNORECASE)
# Top element name of a document that starts with an XML declaration. Unless it is an HTML element (`_HTML_ROOT_NAMES`) the document is
# XML data (`_is_xml_data`).
_XML_DECLARATION = re.compile(rb"<\?xml[\s?]")
_XML_ROOT_NAME = re.compile(rb"<([A-Za-z_][\w.:-]*)")
# HTML elements that can be at the top of XHTML or an exported HTML fragment.
_HTML_ROOT_NAMES = frozenset({
    "html", "head", "body", "title", "meta", "link", "style", "script", "base", "noscript", "table", "caption", "colgroup", "col",
    "thead", "tbody", "tfoot", "tr", "td", "th", "div", "p", "span", "font", "center", "form", "br", "hr", "h1", "h2", "h3", "h4",
    "h5", "h6", "ul", "ol", "li", "dl", "pre", "blockquote", "a", "b", "i", "u", "strong", "em", "img", "section", "article",
    "main", "header", "footer", "nav",
})
# Word 2003 XML (top element w:wordDocument), a single-file XML document saved by Word (processing instruction progid="Word.Document"), and
# flat XML ODF (fodt, fods, fodp; top element office:document). These are character-based formats, so passed through as text their XML tags
# become the body.
_WORD_XML_PROGID = re.compile(rb"<\?mso-application\s[^>]*progid\s*=\s*[\"']Word\.Document[\"']", re.IGNORECASE)
_WORD_XML_ROOT = re.compile(rb"<(?:\w+:)?wordDocument\b[^>]*schemas\.microsoft\.com/office/word/2003/wordml")
_FLAT_ODF_ROOT = re.compile(rb"<(?:\w+:)?document\b[^>]*urn:oasis:names:tc:opendocument:xmlns:office:1\.0")
# Hangul XML (HWPML, .hml). HEAD, BODY and TABLE under the top element HWPML matched the HTML sniff (case-insensitive) and it was read as HTML.
_HWPML_ROOT = re.compile(rb"<HWPML[\s>]")
# NUL bytes at or below this ratio are treated as noise mixed into UTF-8 text (binaries have far more NUL bytes).
_STRAY_NUL_RATIO = 0.001
# Control bytes that do not occur in text (anything but tab, line breaks, form feed and ESC). Executables, images and compressed data
# commonly have these along with NULs.
_BINARY_CONTROL = re.compile(rb"[\x01-\x08\x0e-\x1a\x1c-\x1f\x7f]")
# Marker starting at byte 257 of a tar header (512 bytes). The header is nothing but NUL-padded ASCII fields, so it looks the same as
# "text whose fields are padded with NULs".
_TAR_MAGIC = b"ustar"

_TEXT_EXTENSIONS = {
    ".txt": DocumentFormat.TEXT,
    ".md": DocumentFormat.MARKDOWN,
    ".markdown": DocumentFormat.MARKDOWN,
    ".csv": DocumentFormat.CSV,
    ".tsv": DocumentFormat.CSV,
    ".html": DocumentFormat.HTML,
    ".htm": DocumentFormat.HTML,
    ".xhtml": DocumentFormat.HTML,
}
# Code, data, config and log extensions that are read as text even if the content has HTML strings (`_detect_text`). Server scripts
# (.jsp, .php, .asp) are not included.
_CODE_EXTENSIONS = frozenset({
    ".json", ".jsonl", ".ndjson", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".rb", ".c",
    ".h", ".cc", ".cpp", ".hpp", ".cs", ".swift", ".scala", ".sh", ".sql", ".css", ".scss", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".log",
})
# JSON detection (`_is_json`): the largest size parsed as a whole, and the opening shape checked in larger text.
_JSON_PARSE_BYTES = 1024 * 1024
_JSON_START = re.compile(rb'\s*(?:\{\s*"|\[\s*[\[{"])')
# When the extension claims one of these formats, warn if the content-based result differs.
_EXTENSION_CLAIMS = {
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".pptx": DocumentFormat.PPTX,
    ".xlsx": DocumentFormat.XLSX,
    ".xlsm": DocumentFormat.XLSX,
    ".xls": DocumentFormat.XLS,
    ".hwp": DocumentFormat.HWP,
    ".hwpx": DocumentFormat.HWPX,
    ".odt": DocumentFormat.ODT,
    ".ods": DocumentFormat.ODS,
    ".odp": DocumentFormat.ODP,
    ".epub": DocumentFormat.EPUB,
    ".rtf": DocumentFormat.RTF,
    ".doc": DocumentFormat.DOC,
    ".ppt": DocumentFormat.PPT,
    ".jpg": DocumentFormat.IMAGE,
    ".jpeg": DocumentFormat.IMAGE,
    ".png": DocumentFormat.IMAGE,
    ".heic": DocumentFormat.IMAGE,
    ".webp": DocumentFormat.IMAGE,
    ".gif": DocumentFormat.IMAGE,
    ".bmp": DocumentFormat.IMAGE,
    ".tif": DocumentFormat.IMAGE,
    ".tiff": DocumentFormat.IMAGE,
}
# A text extension (`_TEXT_EXTENSIONS`) is considered right for any character-based format (HTML inside a .txt etc. gets no warning).
# Warn only when it holds a binary document such as Excel, PDF or an image (Pinned by: test_detect.py::TestExtensionMismatchClaims).
# Exception: warn when a `.csv`/`.tsv` is read as HTML (`_is_html_table_export`, Pinned by:
# test_core_text_and_mail.py::TestHtmlTableServedAsCsv).
_TEXT_FAMILY = {DocumentFormat.TEXT, DocumentFormat.MARKDOWN, DocumentFormat.CSV, DocumentFormat.HTML,
                DocumentFormat.XML_SPREADSHEET}


# The `mimetype` entry of an OpenDocument package says which kind it is (templates carry the same word plus `-template`).
_ODF_MIMETYPES = {b"opendocument.text": DocumentFormat.ODT, b"opendocument.spreadsheet": DocumentFormat.ODS,
                  b"opendocument.presentation": DocumentFormat.ODP}


def extension_of(filename: str | None) -> str:
    return PurePath(filename).suffix.lower() if filename else ""


def detect_format(data: bytes, filename: str | None = None) -> DocumentFormat:
    """Detect the format. Raises an exception carrying the reason if the file is unsupported or password-protected."""
    if _is_pdf(data):
        return DocumentFormat.PDF
    if data.startswith(_ZIP_MAGIC):
        return _detect_zip(data)
    if data.startswith(_OLE_MAGIC):
        return _detect_ole(data)
    if is_image_file(data):
        return DocumentFormat.IMAGE
    if _is_raw_biff(data):
        return DocumentFormat.XLS
    if data[:64].lstrip().startswith(_RTF_MAGIC):
        return DocumentFormat.RTF
    if data.startswith(_HWP3_MAGIC):
        raise UnsupportedFormatError("Hangul 3.0 or earlier (HWP 3.x) documents are not supported. "
                                     "Please save the file again as HWP or HWPX in Hancom Office.")
    return _detect_text(data, extension_of(filename))


def _is_pdf(data: bytes) -> bool:
    """A file that starts with the PDF header (`%PDF-`), or has the header within the first 1KB and also has PDF body structure.

    Some PDFs have junk bytes before the header, so not only the very beginning is checked. But if any file that merely has the header
    counted as a PDF, notes and Markdown describing the PDF format would be rejected as a "corrupted PDF"
    (Pinned by: test_detect.py::test_text_that_mentions_the_pdf_header_is_not_a_pdf).
    """
    position = data.find(_PDF_MAGIC, 0, 1024)
    return position == 0 or (position > 0 and _PDF_BODY.search(data) is not None)


def pdf_lookalike_text_format(data: bytes, filename: str | None) -> DocumentFormat | None:
    """Check whether a file detected as PDF but not readable as PDF is actually text, and if so return its text-family format.

    Only when the header is not at the very beginning (other text precedes it) and the whole file is UTF-8 text. A note that wrote
    `%PDF-` together with `startxref` and `1 0 obj` was rejected as a "corrupted PDF" (Pinned by: test_detect.py::TestTextThatLooksLikeAPdf).
    Not deciding this at detection time is deliberate: a real PDF with text in front of it (mail headers etc.) also starts with text,
    so the two cannot be told apart before trying to read it as PDF. A real PDF does not decode as UTF-8 because of its binary comment
    line and compressed streams, so a corrupted PDF is still rejected as corrupted. Known accepted gap: a CP949 note is not turned back
    into text (binary PDF fragments sometimes decode as CP949 too).
    """
    if data.startswith(_PDF_MAGIC) or b"\x00" in data or not is_utf8_text(data):
        return None
    try:
        return _detect_text(data, extension_of(filename))
    except UnsupportedFormatError:
        return None


def extension_mismatch(detected: DocumentFormat, filename: str | None) -> str | None:
    """Return the warning text if the format claimed by the extension differs from the actual format."""
    extension = extension_of(filename)
    claimed = _EXTENSION_CLAIMS.get(extension)
    if extension in _TEXT_EXTENSIONS:
        # No warning between text formats. But a `.csv`/`.tsv` read as HTML (`_is_html_table_export`) did not follow the extension, so say so.
        if detected in _TEXT_FAMILY and not (_TEXT_EXTENSIONS[extension] is DocumentFormat.CSV and detected is DocumentFormat.HTML):
            return None
    elif claimed is None or claimed == detected:
        return None
    return f"The file extension ({extension}) does not match the actual format ({detected.value}), so the file was converted as the actual format."


def _detect_zip(data: bytes) -> DocumentFormat:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            mimetype = _read_prefix(archive, "mimetype", 64) if "mimetype" in names else b""
            main_part = _office_main_part(archive, names)
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError("The archive structure of the document is corrupted.") from exc

    if b"hwp+zip" in mimetype or "Contents/header.xml" in names:
        return DocumentFormat.HWPX
    if "word/document.xml" in names:
        return DocumentFormat.DOCX
    if "ppt/presentation.xml" in names:
        return DocumentFormat.PPTX
    if "xl/workbook.xml" in names:
        return DocumentFormat.XLSX
    if main_part is not None:
        return main_part
    for marker, document_format in _ODF_MIMETYPES.items():
        if marker in mimetype:
            return document_format
    if b"epub+zip" in mimetype or "META-INF/container.xml" in names:
        return DocumentFormat.EPUB
    if b"opendocument" in mimetype:
        raise UnsupportedFormatError("This OpenDocument file type (drawing, formula, database) is not supported.")
    raise UnsupportedFormatError("This archive file format is not supported.")


def _read_prefix(archive: zipfile.ZipFile, name: str, size: int) -> bytes:
    """Decompress only the first `size` bytes of a part. `archive.read(name)[:size]` decompresses the whole part and then slices it, so a
    1MB file used 2.1GB before the zip-bomb check (`converter._guard_zip`) ran
    (Pinned by: test_detect.py::TestZipDetectionMemory)."""
    with archive.open(name) as member:
        return member.read(size)


_OFFICE_DOCUMENT = re.compile(
    rb"<(?:\w+:)?Relationship\b[^>]*?Type\s*=\s*[\"'][^\"']*/officeDocument[\"'][^>]*>")
_TARGET = re.compile(rb"\bTarget\s*=\s*[\"']/?([^\"']+)[\"']")
_OFFICE_FOLDERS = {"word": DocumentFormat.DOCX, "ppt": DocumentFormat.PPTX, "xl": DocumentFormat.XLSX}


def _office_main_part(archive: zipfile.ZipFile, names: set[str]) -> DocumentFormat | None:
    """Decide the format from the folder of the main part that officeDocument in the package relationships (`_rels/.rels`) points to.

    The main part name is not fixed (some tools save it as `word/document2.xml`). Looking only at the fixed names rejected such files as an
    "unsupported archive file" (Pinned by: test_detect.py::TestRenamedMainPart). Accepted only when the main part actually exists.
    """
    if "_rels/.rels" not in names:
        return None
    relationships = _read_prefix(archive, "_rels/.rels", 65_536)
    for relationship in _OFFICE_DOCUMENT.finditer(relationships):
        target = _TARGET.search(relationship.group(0))
        if target is None:
            continue
        path = target.group(1).decode("utf-8", errors="replace").removeprefix("./")
        folder = path.split("/", 1)[0]
        if path in names and folder == "xl" and path.lower().endswith(".bin"):
            # Excel binary workbook. Handed over as XLSX it is rejected as a "corrupted document"
            # (Pinned by: test_detect.py::test_binary_workbook_is_reported_as_unsupported_not_corrupted).
            raise UnsupportedFormatError("Excel binary workbooks (.xlsb) are not supported. Please save the file as XLSX.")
        if path in names and folder in _OFFICE_FOLDERS:
            return _OFFICE_FOLDERS[folder]
    return None


def _detect_ole(data: bytes) -> DocumentFormat:
    try:
        with olefile.OleFileIO(BytesIO(data)) as ole:
            streams = {"/".join(entry) for entry in ole.listdir(streams=True, storages=True)}
    except OSError as exc:
        raise CorruptedDocumentError("The OLE structure of the document is corrupted.") from exc

    # OLE stream names are case-insensitive (olefile, xlrd and Excel alike). An XLS saved with `WORKBOOK` was rejected as an "unsupported
    # OLE document" (Pinned by: test_detect.py::TestXlsStreamNameCase), and a password-protected docx saved with `ENCRYPTEDPACKAGE` got
    # the same rejection instead of the password notice (Pinned by: test_detect.py::TestOleStreamNameCaseForOtherDocuments). So Office
    # stream names are compared in lowercase. The reading side (`spreadsheet._xls_workbook_stream`) is case-insensitive too. For HWP the
    # extractor (`hwp.py`) looks up `BodyText` by exactly that name, so detection leaves it as is.
    if "FileHeader" in streams and ("BodyText" in streams or "ViewText" in streams):
        return DocumentFormat.HWP
    streams = {name.lower() for name in streams}
    if {"workbook", "book"} & streams:
        return DocumentFormat.XLS
    if "encryptedpackage" in streams:
        # A password-protected docx/xlsx/pptx is stored not as a ZIP but as an encrypted package inside OLE.
        raise EncryptedDocumentError("The Office document is password-protected. Please remove the password and upload it again.")
    if "worddocument" in streams:
        return DocumentFormat.DOC
    if "powerpoint document" in streams:
        return DocumentFormat.PPT
    raise UnsupportedFormatError("This OLE document format is not supported.")


# First record (BOF) of a BIFF stream not wrapped in OLE: BIFF2 0x0009, BIFF3 0x0209, BIFF4 0x0409, BIFF5/8 0x0809, and their lengths.
_BIFF_BOF_LENGTHS = {0x0009: (4,), 0x0209: (6,), 0x0409: (6,), 0x0809: (8, 16)}
# Substream kinds in BOF: workbook globals, worksheet, chart, macro sheet, workspace.
_BIFF_SUBSTREAM_KINDS = frozenset((0x0005, 0x0010, 0x0020, 0x0040, 0x0100))
_BIFF_EOF = 0x000A
_BIFF_MAX_RECORD = 8224
_BIFF_SNIFF_BYTES = 64 * 1024


def _is_raw_biff(data: bytes) -> bool:
    """Is this an Excel file that has only BIFF records and no OLE container (a PHP xlsBOF-style "Excel download")?

    Excel and xlrd read such files, but detection rejected them as an "unsupported binary file" (Pinned by:
    test_detect.py::TestRawBiffWithoutOle). Checked conservatively so that not just any binary counts as XLS: the first record must have
    a BOF kind and length and a known substream kind, and within the first 64KB the record headers (kind, length) must chain without
    running past the end of the file until they reach an EOF record (length 0). If there is no EOF within 64KB, it is enough that the
    records in that range chain without a break (large export files).
    """
    if len(data) < 8:
        return False
    record_id, length, _version, kind = struct.unpack_from("<4H", data)
    if length not in _BIFF_BOF_LENGTHS.get(record_id, ()) or kind not in _BIFF_SUBSTREAM_KINDS:
        return False

    position = 4 + length

    while position < min(len(data), _BIFF_SNIFF_BYTES):
        if position + 4 > len(data):
            return False
        record_id, length = struct.unpack_from("<HH", data, position)
        if record_id == _BIFF_EOF and length == 0:
            return True
        if length > _BIFF_MAX_RECORD or position + 4 + length > len(data):
            return False
        position += 4 + length

    return position >= _BIFF_SNIFF_BYTES


def _detect_text(data: bytes, extension: str) -> DocumentFormat:
    sample = data[:8192]
    # In UTF-16/UTF-32 (BOM or NUL byte positions) NUL bytes are part of the text. The HTML sniff below runs on text decoded with that codec.
    wide = unicode_bom_codec(sample) or utf16_encoding(data)
    if b"\x00" in sample and wide is None and not _is_text_with_nuls(sample):
        raise UnsupportedFormatError("This binary file format is not supported.")

    head = data[:_HTML_SNIFF_BYTES]
    sniff = head.decode(wide, errors="ignore").encode() if wide else head
    by_extension = _TEXT_EXTENSIONS.get(extension)
    if by_extension is DocumentFormat.CSV and _is_html_table_export(sniff):
        return DocumentFormat.HTML
    if by_extension is DocumentFormat.HTML and _is_plain_mail(data):
        return DocumentFormat.TEXT
    if by_extension is not None:
        return by_extension
    if _XML_SPREADSHEET_SNIFF.search(sniff[:8192]):
        return DocumentFormat.XML_SPREADSHEET
    body = sniff.removeprefix(b"\xef\xbb\xbf")
    _reject_other_xml_documents(body)
    if _is_xml_data(body):
        return DocumentFormat.TEXT
    if is_encrypted_mail(data):
        raise EncryptedDocumentError("The body of an encrypted mail (S/MIME, PGP) cannot be read. "
                                     "Please decrypt and save it in your mail program, then upload it again.")
    # An mbox archive (two or more mails) is read as an archive even if its first mail looks like MHTML (a single text/html part,
    # multipart/related) — the later mails were appended as raw text or lost
    # (Pinned by: test_core_mailboxes_and_charsets.py::TestMailboxThatStartsWithAnHtmlMail).
    mails, _ = mail_box(data)
    if len(mails) < 2 and is_mhtml(data):
        # MHTML (single-file web page) passed through as text, and the MIME headers and base64 became a "successful" result. The HTML
        # extractor pulls out the HTML part and reads it (Pinned by: test_detect.py::TestMhtml).
        if mhtml_html_part(data) is None:
            raise UnsupportedFormatError("MHTML (single-file web page) documents without an HTML body are not supported. "
                                         "Please save the file again as HTML or XLSX.")
        return DocumentFormat.HTML
    # A mail (.eml) with an HTML body part is read as HTML like MHTML, whatever its transfer encoding. With only plain-text parts the
    # text extractor decodes and reads it (Pinned by: test_text_html.py::TestEncodedMail,
    # test_core_mail_and_detect.py::TestMailWithoutTransferEncoding).
    # If a forwarded mail (message/rfc822) has only an HTML body, the mail is read as HTML even when the outer mail is plain text
    # (`html.mail_reads_as_html`). Any other mail is text and is not passed on to the HTML sniff below — an HTML attachment attached
    # as 8bit, or the `<html>` of a forwarded mail, made the whole raw mail be read as HTML
    # (Pinned by: test_core_text_and_mail.py::TestForwardedMail).
    # An mbox archive is HTML if any of its mails has something to read as HTML (`text.mail_box`).
    if mails:
        return DocumentFormat.HTML if any(mail_reads_as_html(mail) for mail in mails) else DocumentFormat.TEXT

    # JSON, code, config and logs that hold HTML strings as values are text. Because they even had closing tags they were read as HTML
    # and their keys, indentation and code lines were mangled
    # (Pinned by: test_core_mail_and_detect.py::TestHtmlInsideJsonAndCode). Server script names (.jsp, .php, .asp) are not included —
    # "Excel download" HTML served under those names is common.
    if extension in _CODE_EXTENSIONS or _is_json(data, body, wide):
        return DocumentFormat.TEXT

    # A document that starts with prose is not taken as HTML just because of an opening tag in the first 2KB. It must have a closing
    # tag too — a log that wrote a tag name once was read as HTML and the lines after it were mangled
    # (Pinned by: test_detect.py::test_prose_that_only_mentions_an_html_tag_is_text,
    # test_html_that_starts_with_a_little_prose_or_is_not_closed_is_still_html).
    body = body[_LEADING_MARKUP_NOISE.match(body).end():]
    starts_with_tag = body.startswith(b"<")
    if _HTML_SNIFF.search(sniff.lstrip()[:2048]) and (starts_with_tag or _HTML_CLOSING_SNIFF.search(sniff)):
        return DocumentFormat.HTML
    # A document that starts with a tag is searched more widely: an "Excel download" whose table comes after a long <style> or comment,
    # or that starts from <tr>, was read as text. A document that starts with prose is not widened — a note that writes a tag name in
    # the middle of its body would become HTML
    # (Pinned by: test_detect.py::test_detects_html_export_with_long_preamble_or_bare_rows, test_text_that_mentions_a_table_tag_late_is_still_text).
    if _SVG_ROOT.match(body):
        raise UnsupportedFormatError("SVG images are not supported. Please save the file as PNG.")
    if starts_with_tag and _HTML_MARKUP_SNIFF.search(body):
        return DocumentFormat.HTML
    if extension == ".xls" and _looks_delimited(data):
        # When an "Excel download" serves TSV/CSV as .xls it was read as text and did not become a table
        # (Pinned by: test_detect.py::TestDelimitedTextServedAsXls).
        return DocumentFormat.CSV
    return DocumentFormat.TEXT


def _is_plain_mail(data: bytes) -> bool:
    """Is this a mail with nothing to read as HTML (`text.mail_box`, MHTML excluded)? A plain-text mail named `.html`/`.htm` went to the
    HTML extractor by its extension, and its headers and body were squashed into one line
    (Pinned by: test_core_mailboxes_and_charsets.py::TestPlainMailNamedAsHtml)."""
    mails, _ = mail_box(data)
    if not mails or (len(mails) < 2 and is_mhtml(data)):
        return False
    return not any(mail_reads_as_html(mail) for mail in mails)


def _is_html_table_export(sniff: bytes) -> bool:
    """Is this an HTML table served as `.csv`/`.tsv` (an "Excel download")? Only when the very beginning, after BOM and whitespace, is
    an HTML document tag (`_HTML_EXPORT_START`) and the leading part has both a table tag (`<table>`, `<tr>`, `<td>`) and its closing tag.

    Read as CSV by its extension, the tags became cell text and `1,000` was split into two cells (Pinned by: test_core_text_and_mail.py::
    TestHtmlTableServedAsCsv). Deliberately conservative: a real CSV whose first cell starts with a tag such as `<b>` or `<table>` has
    no closing table tag or does not start with a document tag, so it stays CSV. `.txt` stays text (between text formats the extension
    is followed — the extension-warning row in docs/decisions/detect.md).
    """
    body = sniff.removeprefix(b"\xef\xbb\xbf").lstrip()
    return (_HTML_EXPORT_START.match(body) is not None and _TABLE_TAG.search(body) is not None
            and _TABLE_CLOSING_TAG.search(body) is not None)


def _is_json(data: bytes, body: bytes, wide: str | None) -> bool:
    """Is this JSON text starting with `{` or `[`: either the whole text is JSON (indented JSON) or the first line is a JSON object or
    array (JSON Lines)?

    Large text (over `_JSON_PARSE_BYTES`) is not parsed as a whole (the object tree takes several times the memory of the source); only
    the opening shape (`{"`, `[{`, `["`, `[[`) is checked.
    """
    if body.lstrip()[:1] not in (b"{", b"["):
        return False
    if len(data) > _JSON_PARSE_BYTES:
        return _JSON_START.match(body) is not None
    try:
        text = data.decode(wide) if wide else data.removeprefix(b"\xef\xbb\xbf").decode("utf-8")
    except UnicodeError:
        return False

    text = text.lstrip("\ufeff").strip()
    first_line = text.split("\n", 1)[0].strip()
    for candidate in (first_line, text) if first_line != text else (text,):
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, (dict, list)):
            return True
    return False


def _reject_other_xml_documents(head: bytes) -> None:
    """Raise `UnsupportedFormatError` for Word XML, flat XML ODF and Hangul XML (HWPML). For the same reason as RTF and SVG: they passed
    through as text and the XML tags became a "successful" result (Pinned by: test_detect.py::TestOtherXmlDocuments). A text extension
    (.txt etc.) is settled as text before this check."""
    prolog = _XML_PROLOG.match(head)
    root = head[prolog.end():]
    if _WORD_XML_PROGID.search(prolog.group()) or _WORD_XML_ROOT.match(root):
        raise UnsupportedFormatError("Word XML documents are not supported. Please save the file as DOCX.")
    if _FLAT_ODF_ROOT.match(root):
        raise UnsupportedFormatError("Flat XML ODF (fodt, fods, fodp) documents are not supported. Please save the file as ODT, ODS or ODP.")
    if _HWPML_ROOT.match(root):
        # The table was scattered cell by cell and the trailing pictures (BINDATA base64) became the body (Pinned by: test_detect.py::TestHwpml).
        raise UnsupportedFormatError("Hangul XML (HWPML, .hml) documents are not supported. Please save the file as HWPX or HWP in Hancom Office.")


def _is_xml_data(head: bytes) -> bool:
    """Is this XML data that starts with an XML declaration and whose top element is not an HTML element?

    XML that uses element names shared with HTML, like `<NewDataSet><Table>…` from .NET `DataSet.WriteXml`, matched the HTML sniff
    (case-insensitive), so cell values were glued together (`1001홍길동`) and a bs4 warning leaked out. The same XML without `<Table>`
    was text as is — this keeps it the same (Pinned by: test_core_html.py::TestXmlDataWithHtmlElementNames). XHTML (top element html)
    and an HTML table fragment with an XML declaration in front are still HTML.
    Deliberate: documents without an XML declaration are not examined — there is no basis for telling them from an HTML fragment that
    starts with an unfamiliar tag.
    """
    if not _XML_DECLARATION.match(head):
        return False
    root = _XML_ROOT_NAME.match(head, _XML_PROLOG.match(head).end())
    name = root.group(1).decode("ascii").lower() if root is not None else "html"
    return name not in _HTML_ROOT_NAMES and name != "svg"  # SVG is rejected by the check below


# Title and query-condition lines in front of delimited text downloaded as '.xls' (`_looks_delimited`): the maximum number of lines to
# skip and the maximum number of characters in one such line.
_MAX_TITLE_LINES = 5
_TITLE_LINE_CHARS = 100


def _looks_delimited(data: bytes) -> bool:
    """Do all the leading lines contain a tab, or do they all have the same number of comma- or semicolon-separated cells (2 or more)?
    If the sample was cut off, the last line is left out.

    Cells are counted with `csv.reader`. Counting delimiters also counted commas inside quotes ("1,200"), and a CSV was read as text
    (Pinned by: test_detect.py::test_comma_inside_quoted_value_does_not_hide_a_csv_served_as_xls).
    If up to `_MAX_TITLE_LINES` short lines (at most `_TITLE_LINE_CHARS` characters) such as a title or query conditions come first,
    they are left out of the check. In that case, conservatively, at least 3 lines must remain and they must have at least 3 cells
    (for tabs, at least 2 per line) — so that two-cell note lines like '참석: 홍길동, 김철수' ("Attendees: Hong Gildong, Kim Cheolsu")
    do not become CSV. The CSV extractor keeps the skipped lines as paragraphs above the table
    (Pinned by: test_detect.py::TestDelimitedTextWithTitleLinesServedAsXls).
    """
    sample = data[:8192]
    lines = [line for line in decode_text(sample)[0].splitlines() if line.strip()]
    if len(data) > len(sample):
        lines = lines[:-1]

    if len(lines) < 2:
        return False
    if _delimited_rows(lines[:20], columns=2):
        return True
    return any(len(lines) - skip >= 3 and all(len(line) <= _TITLE_LINE_CHARS for line in lines[:skip])
               and _delimited_rows(lines[skip:skip + 20], columns=3) for skip in range(1, _MAX_TITLE_LINES + 1))


def _delimited_rows(lines: list[str], *, columns: int) -> bool:
    """Does every line have at least `columns - 1` tabs, or the same number (at least `columns`) of comma- or semicolon-separated cells?"""
    if all(line.count("\t") >= columns - 1 for line in lines):
        return True
    return any(len(widths := {len(row) for row in csv.reader(lines, delimiter=delimiter)}) == 1 and min(widths) >= columns
               for delimiter in ",;")


def _is_text_with_nuls(sample: bytes) -> bool:
    """Is this text although NUL bytes are mixed in: UTF-8 with very few NULs (0.1% of the sample, at least 1), or the rest without the
    NULs decodes strictly as UTF-8 or CP949 and has not a single control byte that text never has (`_BINARY_CONTROL`)?

    Rejecting as binary on a single NUL byte lost the whole text because of one NUL that strayed into a log or note (Pinned by:
    test_detect.py::test_utf8_text_with_a_stray_nul_is_text). CP949 text was rejected for one NUL, and fixed-width exports that pad
    fields with NULs were rejected whatever the encoding (Pinned by: test_detect.py::TestTextWithNulBytes). Executables, images and
    compressed data still get rejected because other control bytes come with the NULs or strict decoding fails. tar is blocked
    separately because its header is nothing but NUL-padded ASCII fields.
    """
    if sample.count(0) <= max(1, int(len(sample) * _STRAY_NUL_RATIO)) and is_utf8_text(sample):
        return True
    if sample[257:262] == _TAR_MAGIC:
        return False
    rest = sample.replace(b"\x00", b"")
    if not rest.strip() or _BINARY_CONTROL.search(rest):
        return False
    return is_utf8_text(rest) or is_cp949_text(rest)
