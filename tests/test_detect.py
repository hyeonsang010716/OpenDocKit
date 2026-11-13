"""Content-based format detection."""

from io import BytesIO
import time

import pytest

from opendockit import (
    DocumentFormat,
    EncryptedDocumentError,
    UnsupportedFormatError,
    convert,
    detect_format,
)
from opendockit.detect import extension_mismatch
from opendockit.extractors._html_mail import is_mhtml
from tests import builder


class TestDetectFormat:
    @pytest.mark.parametrize(
        ("make", "expected"),
        [
            (builder.docx_bytes, DocumentFormat.DOCX),
            (builder.pptx_bytes, DocumentFormat.PPTX),
            (builder.xlsx_bytes, DocumentFormat.XLSX),
            (builder.hwpx_bytes, DocumentFormat.HWPX),
            (lambda: builder.pdf_bytes(pages=1), DocumentFormat.PDF),
            (lambda: builder.png_bytes(), DocumentFormat.IMAGE),
            (lambda: builder.png_bytes(image_format="JPEG"), DocumentFormat.IMAGE),
            (lambda: builder.png_bytes(image_format="WEBP"), DocumentFormat.IMAGE),
            (lambda: builder.png_bytes(image_format="BMP"), DocumentFormat.IMAGE),
        ],
    )
    def test_detects_binary_formats_by_content_regardless_of_name(self, make, expected):
        data = make()

        result = detect_format(data, "이름이_거짓말.txt")

        assert result is expected

    def test_detects_html_disguised_as_xls(self):
        """The 'Excel download' of Korean web systems serves an HTML table as .xls."""
        data = "<html><body><table><tr><td>이름</td></tr></table></body></html>".encode()

        result = detect_format(data, "목록.xls")

        assert result is DocumentFormat.HTML

    @pytest.mark.parametrize("filename", ["목록.xls", "목록.xml", None])
    def test_detects_excel_2003_xml_spreadsheet_instead_of_html(self, filename):
        """The `<Table>` of Excel 2003 XML was caught by the case-insensitive HTML detection and read as HTML, and the result was
        an empty string."""
        data = ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet">'
                "<Worksheet><Table><Row><Cell><Data>이름</Data></Cell></Row></Table></Worksheet></Workbook>").encode()

        result = detect_format(data, filename)

        assert result is DocumentFormat.XML_SPREADSHEET

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("a.md", DocumentFormat.MARKDOWN),
            ("a.csv", DocumentFormat.CSV),
            ("a.tsv", DocumentFormat.CSV),
            ("a.txt", DocumentFormat.TEXT),
            (None, DocumentFormat.TEXT),
        ],
    )
    def test_uses_extension_only_for_text_family(self, filename, expected):
        assert detect_format("가,나\n1,2".encode(), filename) is expected

    @pytest.mark.parametrize("filename", ["메모.txt", "note.md", None])
    def test_text_that_mentions_the_pdf_header_is_not_a_pdf(self, filename):
        """Anything with `%PDF-` in its first 1KB was taken for a PDF, so a memo describing the PDF format was rejected as a
        'corrupted PDF'."""
        data = "PDF 파일은 %PDF-1.7 로 시작한다.\n본문".encode()

        result = detect_format(data, filename)

        assert result is not DocumentFormat.PDF

    def test_pdf_with_leading_junk_is_still_a_pdf(self):
        """A PDF with junk bytes before its header (mail gateways, old generators) is still a PDF."""
        data = b"\r\n<!-- gateway -->\r\n" + builder.pdf_bytes(pages=1)

        result = detect_format(data, "a.pdf")

        assert result is DocumentFormat.PDF

    @pytest.mark.parametrize(
        "html",
        [
            "<style>" + ".x{color:red}\n" * 300 + "</style><table><tr><td>이름</td><td>금액</td></tr></table>",
            "<!-- 내보내기 -->\n" * 200 + "<table><tr><td>이름</td></tr></table>",
            "<tr><td>이름</td><td>금액</td></tr><tr><td>홍길동</td><td>1000</td></tr>",
        ],
        ids=["long-style", "long-comments", "rows-only"],
    )
    def test_detects_html_export_with_long_preamble_or_bare_rows(self, html):
        """HTML signals were looked for only in the first 2KB, so an 'Excel download' that starts with a long <style> or comments,
        or has only <tr>, was read as text."""
        result = detect_format(html.encode(), "목록.xls")

        assert result is DocumentFormat.HTML

    def test_text_that_mentions_a_table_tag_late_is_still_text(self):
        data = ("회의 메모\n" * 300 + "HTML 에서는 <table> 태그를 쓴다.\n").encode()

        result = detect_format(data, None)

        assert result is DocumentFormat.TEXT

    def test_utf8_text_with_a_stray_nul_is_text(self):
        data = b"hello\x00world " + "안녕하세요".encode() * 10

        result = detect_format(data, "memo.log")

        assert result is DocumentFormat.TEXT

    def test_rejects_unknown_binary(self):
        data = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00"

        with pytest.raises(UnsupportedFormatError):
            detect_format(data, "a.bin")

    def test_rejects_hwp3(self):
        with pytest.raises(UnsupportedFormatError, match="HWP 3"):
            detect_format(b"HWP Document File V3.00 \x1a\x01\x02\x03\x04\x05", "old.hwp")


def _renamed_main_part(data: bytes, folder: str, old: str, new: str) -> bytes:
    """A copy of an OOXML file with its main part renamed (`word/document.xml` → `word/document2.xml`). The package relationships,
    content types and part relationships are fixed too."""
    import zipfile

    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            if name in ("_rels/.rels", "[Content_Types].xml"):
                content = content.replace(f"{folder}/{old}".encode(), f"{folder}/{new}".encode())
            target.writestr(name.replace(f"{folder}/{old}", f"{folder}/{new}").replace(f"_rels/{old}.rels", f"_rels/{new}.rels"),
                            content)
    return buffer.getvalue()


class TestRenamedMainPart:
    """When the main part had another name such as `word/document2.xml`, the file was rejected with 'This archive file format is
    not supported.' The format is detected from the main part the package relationships point to."""

    @pytest.mark.parametrize(
        ("make", "folder", "old", "expected"),
        [
            (builder.docx_bytes, "word", "document.xml", DocumentFormat.DOCX),
            (builder.pptx_bytes, "ppt", "presentation.xml", DocumentFormat.PPTX),
            (builder.xlsx_bytes, "xl", "workbook.xml", DocumentFormat.XLSX),
        ],
    )
    def test_detects_the_main_part_from_package_relationships(self, make, folder, old, expected):
        data = _renamed_main_part(make(), folder, old, old.replace(".xml", "2.xml"))

        result = detect_format(data, "a.zip")

        assert result is expected

    def test_docx_with_renamed_main_part_is_converted(self):
        data = _renamed_main_part(builder.docx_bytes(), "word", "document.xml", "document2.xml")

        result = convert(data, "보고서.docx")

        assert "# 1장 개요" in result.text
        assert "본문 첫 문단입니다." in result.text

    def test_zip_without_office_relationships_is_still_unsupported(self):
        import zipfile

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "hello")

        with pytest.raises(UnsupportedFormatError):
            detect_format(buffer.getvalue(), "a.zip")


def test_extension_mismatch_reports_only_claimed_binary_formats():
    assert extension_mismatch(DocumentFormat.HTML, "a.xls") is not None
    assert extension_mismatch(DocumentFormat.XLS, "a.xls") is None
    assert extension_mismatch(DocumentFormat.TEXT, "a.log") is None


def test_binary_workbook_is_reported_as_unsupported_not_corrupted():
    """.xlsb has `xl/workbook.bin` as its main part, so it was detected as XLSX and then rejected as a 'corrupted document'. The
    user is told to save it again."""
    import zipfile

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("_rels/.rels", (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="xl/workbook.bin"/></Relationships>'))
        archive.writestr("xl/workbook.bin", b"\x83\x01\x00")

    with pytest.raises(UnsupportedFormatError, match="xlsb"):
        convert(buffer.getvalue(), "a.xlsb")


@pytest.mark.parametrize("data, filename", [
    (b'<?xml version="1.0"?>\n<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L5 5"/></svg>', "a.svg"),
    (b'<!DOCTYPE svg PUBLIC "-//W3C//DTD SVG 1.1//EN" "x.dtd"><svg><text>hi</text></svg>', "logo"),
])
def test_svg_is_reported_as_unsupported_instead_of_passing_as_text(data, filename):
    """SVG passed as text, so path coordinates became a 'successful' result as is."""
    with pytest.raises(UnsupportedFormatError, match="SVG"):
        convert(data, filename)


@pytest.mark.parametrize("data, filename, expected", [
    (b"{\\rtf1\\ansi\\ansicpg949 \\'c7\\'d1\\'b1\\'db\\par}", "a.rtf", "한글"),
    (b"{\\rtf1\\ansi text\\par}", "a.doc", "text"),
])
def test_rtf_is_read_as_rtf_whatever_the_extension(data, filename, expected):
    """RTF used to pass as text, so control words and Korean escapes like `\\'c7\\'d1` became a 'successful' result as they were;
    then it was rejected as unsupported. It is now read by the RTF extractor, by content — an RTF named `.doc` included."""
    result = convert(data, filename)

    assert result.format is DocumentFormat.RTF
    assert result.text == expected


def test_text_file_that_shows_svg_markup_is_still_text():
    assert detect_format("<svg> 태그 설명 메모".encode(), "a.txt") is DocumentFormat.TEXT
    assert detect_format("메모: {\\rtf1 로 시작하는 파일은 RTF 다".encode(), "a.txt") is DocumentFormat.TEXT


@pytest.mark.parametrize("filename", ["deploy.log", "data.json", None])
def test_prose_that_only_mentions_an_html_tag_is_text(filename):
    """Text other than .txt/.md was read as HTML if a tag name appeared even once in its first 2KB, and the lines after it were
    squashed into one paragraph."""
    data = "배포 로그\n10:01 <table> 렌더링 오류 발생\n10:02 재시작 완료\n10:03 정상\n".encode()

    result = convert(data, filename)

    assert result.format is DocumentFormat.TEXT
    assert result.text == "배포 로그\n10:01 <table> 렌더링 오류 발생\n10:02 재시작 완료\n10:03 정상"


@pytest.mark.parametrize(
    "html",
    [
        "조회 결과\n<table><tr><td>이름</td><td>금액</td></tr></table>",
        "Warning: date() expects parameter 2 in /www/list.php on line 3\n<html><body><p>본문</p></body></html>",
        "\ufeff\r\n\r\n<!-- jsp -->\r\n<html><body><table><tr><td>이름",
        "<table><tr><td>이름",
    ],
    ids=["title-line", "php-warning", "tag-start-unclosed", "table-unclosed"],
)
def test_html_that_starts_with_a_little_prose_or_is_not_closed_is_still_html(html):
    """A document with closing tags is HTML even if it starts with prose (HTML after a title line or a server warning), and a
    document that starts with a tag is HTML even if it is not closed."""
    result = detect_format(html.encode(), "목록.xls")

    assert result is DocumentFormat.HTML


def _mhtml(content_type: str, parts: str) -> bytes:
    return (f"MIME-Version: 1.0\r\nContent-Type: {content_type}; boundary=\"b1\"\r\n\r\n{parts}\r\n--b1--\r\n").encode()


class TestMhtml:
    HTML_PART = "--b1\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\nPHA+7ZKI7J2Y7IScPC9wPg==\r\n"

    @pytest.mark.parametrize("filename", ["결재문서.xls", "품의서.doc", "page.mht", None])
    def test_single_file_web_page_is_html(self, filename):
        """MHTML was detected as text, so the MIME headers and base64 became a 'successful' result as is."""
        data = _mhtml("multipart/related", self.HTML_PART)

        result = convert(data, filename)

        assert result.format is DocumentFormat.HTML
        assert result.text == "품의서"

    def test_single_file_web_page_without_an_html_part_is_unsupported(self):
        data = _mhtml("multipart/related", "--b1\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n\r\niVBORw0KGgo=\r\n")

        with pytest.raises(UnsupportedFormatError, match="MHTML"):
            convert(data, "a.mht")

    @pytest.mark.parametrize(
        ("data", "filename"),
        [
            (_mhtml("multipart/related", HTML_PART), "메모.txt"),
            (_mhtml("multipart/related", HTML_PART), "메모.md"),
            ("MIME-Version 머리말은 메일 형식의 판 번호다.\nContent-Type: multipart/related 는 묶음이다.\n".encode(), "note.eml"),
            ("MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n안녕하세요. 회의록입니다.\r\n".encode(), "a.eml"),
            (_mhtml("multipart/mixed", "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n메일 본문\r\n"), None),
            ("메모\nMIME-Version: 1.0\nContent-Type: multipart/related; boundary=\"b1\"\n\n--b1\nContent-Type: text/html\n\n<p>a</p>\n--b1--\n".encode(), None),
        ],
        ids=["txt", "md", "prose", "plain-mail", "mixed-mail", "not-at-the-start"],
    )
    def test_text_extensions_and_ordinary_mail_like_notes_stay_text(self, data, filename):
        result = detect_format(data, filename)

        assert result in (DocumentFormat.TEXT, DocumentFormat.MARKDOWN)

    @pytest.mark.timing
    def test_prose_with_many_indented_lines_is_decided_in_linear_time(self):
        """For text whose first line is `word:` followed only by indented lines, with no blank line within 8KB (XMP metadata, list
        memos), the header block regex backtracked over every way two quantifiers can share the whitespace of the folded lines and
        did not finish even with 12 indented lines (a text file named .pdf hung for over 180 seconds)."""
        data = b"title: hello\n" + b"        - item\n" * 2000

        started = time.perf_counter()
        result = is_mhtml(data)
        elapsed = time.perf_counter() - started

        assert result is False
        assert elapsed < 0.5
        assert detect_format(data, None) is DocumentFormat.TEXT

    def test_folded_header_lines_and_crlf_still_make_an_mhtml(self):
        """MHTML whose header lines are folded with spaces or tabs (a file saved by Word with the Content-Type boundary on the next
        line) is MHTML too."""
        data = ("MIME-Version: 1.0\r\nContent-Type: multipart/related;\r\n\tboundary=\"b1\"\r\n\r\n"
                + self.HTML_PART + "\r\n--b1--\r\n").encode()

        result = convert(data, "page.mht")

        assert result.format is DocumentFormat.HTML
        assert result.text == "품의서"

    @pytest.mark.parametrize("data", [b"MIME-Version: 1.0\nContent-Type: multipart/related\n   \nbody\n",
                                      b"  folded first\nMIME-Version: 1.0\nContent-Type: multipart/related\n\n",
                                      b"MIME-Version: 1.0\nContent-Type: multipart/related\nno colon here\n\n",
                                      b"MIME-Version: 1.0\nContent-Type: multipart/related\n"],
                             ids=["blank-line-with-spaces-is-folded", "folded-before-any-header", "line-without-colon", "no-blank-line"])
    def test_blocks_that_are_not_header_blocks_are_not_mhtml(self, data):
        """The rules of a header block: `name: value` lines from the first line on, folded lines start with a space or tab, and it
        ends at a blank line (a line break with no whitespace)."""
        result = is_mhtml(data)

        assert result is False


def _zip_with_huge_member(name: str, megabytes: int) -> bytes:
    """A ZIP whose `name` part is filled with `megabytes` MB of zero bytes (tens of KB when compressed). The other part is left
    looking like a DOCX."""
    import zipfile

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        info = zipfile.ZipInfo(name)
        info.compress_type = zipfile.ZIP_DEFLATED
        with archive.open(info, "w", force_zip64=True) as member:
            chunk = b"\0" * (1 << 20)
            for _ in range(megabytes):
                member.write(chunk)
        archive.writestr("word/document.xml", "<w:document xmlns:w='urn:w'/>")
    return buffer.getvalue()


class TestZipDetectionMemory:
    @pytest.mark.parametrize("member", ["mimetype", "_rels/.rels"])
    def test_detection_reads_only_a_prefix_of_the_parts_it_sniffs(self, member):
        """Detection decompressed mimetype and _rels/.rels whole and then sliced off the front, so a 1MB file used 2.1GB before the
        zip bomb check (`_guard_zip`). Only the front part needed for detection is decompressed."""
        import tracemalloc

        data = _zip_with_huge_member(member, 32)
        tracemalloc.start()

        result = detect_format(data, "a.docx")
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        assert result is DocumentFormat.DOCX
        assert peak < 8 * 1024 * 1024


class TestTextThatLooksLikeAPdf:
    NOTE = "# PDF 구조 메모\n\nPDF 파일은 `%PDF-1.7` 머리말로 시작하고, 파일 끝의 `startxref` 가 교차 참조표 위치를 가리킨다.\n"

    @pytest.mark.parametrize(
        ("text", "filename", "expected"),
        [
            (NOTE, "pdf-notes.md", DocumentFormat.MARKDOWN),
            ("PDF 객체는 `1 0 obj` 처럼 쓴다. 머리말은 %PDF-1.4 이다.", "n.txt", DocumentFormat.TEXT),
            ("PDF 객체는 `1 0 obj` 처럼 쓴다. 머리말은 %PDF-1.4 이다.", None, DocumentFormat.TEXT),
        ],
        ids=["md-startxref", "txt-obj", "no-name"],
    )
    def test_note_about_the_pdf_structure_is_read_as_text(self, text, filename, expected):
        """A memo that writes both the header (`%PDF-`) and body structure words (`startxref`, `1 0 obj`) was rejected as a
        'corrupted PDF'. When the header is not at the very start, the file cannot be read as a PDF and the whole file is UTF-8
        text, it is read as text."""
        result = convert(text.encode(), filename)

        assert result.format is expected
        assert "%PDF-" in result.text

    def test_pdf_with_leading_junk_is_still_converted_as_a_pdf(self):
        data = b"\r\n<!-- gateway -->\r\n" + builder.pdf_bytes(pages=1)

        result = convert(data, "a.pdf")

        assert result.format is DocumentFormat.PDF
        assert "제1장 개요" in result.text

    def test_broken_binary_pdf_with_leading_junk_is_still_corrupted(self):
        from opendockit import CorruptedDocumentError

        data = b"junk\r\n" + builder.pdf_bytes(pages=1)[:2000] + b"\xff\xfe\x00 1 0 obj"

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.pdf")


class TestOtherXmlDocuments:
    WORD_2003 = ('<?xml version="1.0"?>\n<?mso-application progid="Word.Document"?>\n'
                 '<w:wordDocument xmlns:w="http://schemas.microsoft.com/office/word/2003/wordml"><w:body><w:p><w:r>'
                 '<w:t>본문</w:t></w:r></w:p></w:body></w:wordDocument>')
    FLAT_ODF = ('<?xml version="1.0" encoding="UTF-8"?>\n<office:document xmlns:office="urn:oasis:names:tc:opendocument:'
                'xmlns:office:1.0" office:mimetype="application/vnd.oasis.opendocument.text"><office:body><office:text>'
                '<text:p>본문</text:p></office:text></office:body></office:document>')

    @pytest.mark.parametrize(
        ("xml", "filename", "message"),
        [
            (WORD_2003, "a.xml", "Word XML"),
            (WORD_2003, "a.doc", "Word XML"),
            (WORD_2003.replace('<?mso-application progid="Word.Document"?>\n', ""), None, "Word XML"),
            (FLAT_ODF, "a.fodt", "ODF"),
            (FLAT_ODF.replace("text", "spreadsheet"), "a.fods", "ODF"),
        ],
        ids=["word-xml", "word-as-doc", "word-without-pi", "fodt", "fods"],
    )
    def test_word_xml_and_flat_odf_are_reported_as_unsupported_instead_of_passing_as_text(self, xml, filename, message):
        """The same reason as RTF and SVG: being text-based formats, they passed as text and the XML tags became a 'successful'
        result."""
        with pytest.raises(UnsupportedFormatError, match=message):
            detect_format(xml.encode(), filename)

    def test_xml_named_as_text_stays_text(self):
        assert detect_format(self.WORD_2003.encode(), "a.txt") is DocumentFormat.TEXT


class TestHwpml:
    """Hangul XML (HWPML, .hml) was caught by HTML detection through its `<HEAD>` and `<TABLE>` and read as HTML. Tables scattered
    cell by cell and the BINDATA base64 at the end became body. Like Word XML and flat XML ODF, it is rejected with advice to save
    it as HWPX."""

    HWPML = ('<?xml version="1.0" encoding="UTF-8" standalone="no" ?><HWPML Version="2.8"><HEAD SecCnt="1"></HEAD><BODY>'
             '<SECTION Id="0"><P><TEXT><CHAR>보고서</CHAR></TEXT></P><P><TEXT><TABLE><ROW><CELL><PARALIST><P><TEXT>'
             '<CHAR>지역</CHAR></TEXT></P></PARALIST></CELL></ROW></TABLE></TEXT></P></SECTION></BODY><TAIL><BINDATASTORAGE>'
             '<BINDATA Encoding="Base64" Id="1">iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB</BINDATA></BINDATASTORAGE></TAIL></HWPML>')

    @pytest.mark.parametrize("filename", ["보고서.hml", "보고서.xml", None])
    def test_hwpml_is_reported_as_unsupported_regardless_of_extension(self, filename):
        with pytest.raises(UnsupportedFormatError, match="HWPX"):
            detect_format(self.HWPML.encode(), filename)

    def test_hwpml_without_xml_declaration_or_in_utf16_is_still_rejected(self):
        bare = self.HWPML.split("?>", 1)[1]

        for data in (bare.encode(), self.HWPML.encode("utf-16")):
            with pytest.raises(UnsupportedFormatError, match="HWPX"):
                detect_format(data, "a.hml")

    @pytest.mark.parametrize("filename", ["a.xml", None])
    def test_xhtml_is_still_html(self, filename):
        xhtml = ('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Strict//EN" '
                 '"http://www.w3.org/TR/xhtml1/DTD/xhtml1-strict.dtd">\n<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                 '<title>표</title></head><body><table><tr><td>지역</td><td>매출</td></tr></table></body></html>')

        assert detect_format(xhtml.encode(), filename) is DocumentFormat.HTML


class TestTextWithNulBytes:
    """CP949 text and exports that pad fields with NULs are accepted, and real binaries are still rejected."""

    def test_cp949_text_with_a_stray_nul_is_text(self):
        data = ("이름,금액\n" + "홍길동,1000\n" * 200).encode("cp949") + b"\x00"

        assert detect_format(data, "a.csv") is DocumentFormat.CSV
        assert detect_format(data, None) is DocumentFormat.TEXT

    def test_nul_padded_records_are_text(self):
        data = b"".join(name.encode("cp949").ljust(12, b"\x00") + b"1000\x00\x00\x00\x00\r\n" for name in ["홍길동", "김철수"] * 20)

        assert detect_format(data, None) is DocumentFormat.TEXT

    @pytest.mark.parametrize(
        "data",
        [
            b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 8 + b"\x02\x00\x3e\x00\x01\x00\x00\x00" + "한글".encode("cp949") * 50,
            b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00" + b"This program cannot be run in DOS mode.\r\n" * 20,
            bytes(range(256)) * 8,
            "기록".encode("cp949") + b"\x00" + b"\x01\x02\x03\x04" * 500,
        ],
        ids=["elf", "pe", "all-bytes", "cp949-with-control-bytes"],
    )
    def test_binary_with_few_nuls_is_still_rejected(self, data):
        with pytest.raises(UnsupportedFormatError, match="binary"):
            detect_format(data, "a.bin")

    def test_tar_archive_of_a_text_file_is_rejected(self):
        """A tar header is nothing but NUL-padded ASCII fields, so it has the same shape as 'text with NUL-padded fields'. It is
        an archive file, so it is not read as text."""
        import tarfile

        buffer = BytesIO()
        content = "회의록\n".encode() * 50
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            info = tarfile.TarInfo("memo.txt")
            info.size = len(content)
            archive.addfile(info, BytesIO(content))

        with pytest.raises(UnsupportedFormatError):
            detect_format(buffer.getvalue(), "memo.tar")


class TestDelimitedTextServedAsXls:
    @pytest.mark.parametrize(
        "text",
        ["이름\t금액\t비고\n홍길동\t1,000\t\n김철수\t2,500\t확인\n", "이름,금액,비고\n홍길동,1000,\n김철수,2500,확인\n"],
        ids=["tsv", "csv"],
    )
    def test_tsv_or_csv_with_xls_extension_is_read_as_csv(self, text):
        """When an 'Excel download' serves TSV or CSV as .xls, it was read as text and did not become a table."""
        result = convert(text.encode("cp949"), "목록.xls")

        assert result.format is DocumentFormat.CSV
        assert "| 홍길동 |" in result.text

    def test_prose_with_xls_extension_stays_text(self):
        result = detect_format("회의록입니다. 다음 주에 다시 모입니다.\n참석: 홍길동, 김철수\n".encode(), "a.xls")

        assert result is DocumentFormat.TEXT


class TestDelimitedTextWithTitleLinesServedAsXls:
    """When a TSV/CSV downloaded as '.xls' had a few title or query-condition lines at its start, it was read as text and did not
    become a table. Leaving out a few short leading lines (up to 5), if the remaining lines (3 or more) are a delimited table it
    is read as CSV — the CSV extractor places those lines as paragraphs above the table."""

    @pytest.mark.parametrize(
        ("text", "row"),
        [
            ("거래내역 조회\n조회기간\t2024-01-01 ~ 2024-03-31\n\n거래일자\t적요\t출금액\t입금액\t잔액\n"
             "2024-01-02\t급여\t0\t3,500,000\t5,200,000\n2024-01-03\t카드대금\t1,234,000\t0\t3,966,000\n",
             "| 2024-01-02 | 급여 | 0 | 3,500,000 | 5,200,000 |"),
            ('"거래처 원장"\n거래처,일자,적요,차변,대변,잔액\n대한물산,2024-01-02,매출,"1,100,000",0,"1,100,000"\n'
             '대한물산,2024-01-20,입금,0,"1,100,000",0\n',
             "| 대한물산 | 2024-01-02 | 매출 | 1,100,000 | 0 | 1,100,000 |"),
        ],
        ids=["tsv", "csv"],
    )
    def test_table_after_a_few_title_lines_is_read_as_csv(self, text, row):
        result = convert(text.encode("cp949"), "목록.xls")

        assert result.format is DocumentFormat.CSV
        assert row in result.text

    @pytest.mark.parametrize("text", [
        "회의록\n참석: 홍길동, 김철수\n안건: 예산, 일정\n결정: 다음 주, 재검토\n",
        "제목\n첫째 줄입니다\n둘째 줄, 쉼표 하나\n셋째 줄, 쉼표, 둘\n넷째 줄, 쉼표 하나\n",
        "메모\n" + "아주 긴 머리말 " * 20 + "\n가,나,다\n라,마,바\n사,아,자\n",
        "할 일\n장보기, 우유, 빵\n청소, 거실, 방\n",
    ])
    def test_memo_is_still_text(self, text):
        result = detect_format(text.encode(), "a.xls")

        assert result is DocumentFormat.TEXT


def test_comma_inside_quoted_value_does_not_hide_a_csv_served_as_xls():
    """Commas inside quotes ("1,200") were counted too, so the number of commas looked different from line to line and an 'Excel
    download' CSV was read as text."""
    text = '이름,금액\n홍길동,"1,200"\n김철수,"12,500"\n이영희,300\n'

    result = convert(text.encode("cp949"), "목록.xls")

    assert result.format is DocumentFormat.CSV
    assert "| 홍길동 | 1,200 |" in result.text


class TestExtensionMismatchClaims:
    """There was no warning when a text extension (.csv, .txt, .html …) actually held a binary document such as Excel or PDF, or
    when an image extension (.webp, .gif, .bmp, .tif) was another format. The case of HTML inside `.xls` was already warned about."""

    @pytest.mark.parametrize("filename", ["a.csv", "a.txt", "a.html", "a.md"])
    @pytest.mark.parametrize("detected", [DocumentFormat.XLS, DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.IMAGE])
    def test_text_extension_holding_a_binary_document_is_reported(self, filename, detected):
        assert extension_mismatch(detected, filename) is not None

    @pytest.mark.parametrize(("detected", "filename"), [
        (DocumentFormat.HTML, "a.txt"),
        (DocumentFormat.CSV, "a.txt"),
        (DocumentFormat.TEXT, "a.csv"),
        (DocumentFormat.XML_SPREADSHEET, "a.html"),
    ])
    def test_text_extension_holding_another_text_format_is_not_reported(self, detected, filename):
        assert extension_mismatch(detected, filename) is None

    @pytest.mark.parametrize("filename", ["a.webp", "a.gif", "a.bmp", "a.tif", "a.tiff"])
    def test_image_extensions_claim_an_image(self, filename):
        assert extension_mismatch(DocumentFormat.IMAGE, filename) is None
        assert extension_mismatch(DocumentFormat.PDF, filename) is not None

    def test_binary_document_named_csv_warns(self):
        result = convert(builder.pdf_bytes(1), "목록.csv")

        assert result.format is DocumentFormat.PDF
        assert result.warnings[0].startswith("The file extension (.csv)")


class TestXlsStreamNameCase:
    """OLE stream names are case-insensitive (xlrd and olefile read `WORKBOOK` and `book` too). Only `Workbook` and `Book` were
    looked for, so an XLS saved in upper case was rejected with 'This OLE document format is not supported.'"""

    @pytest.mark.parametrize("name", ["WORKBOOK", "workbook", "WorkBook", "BOOK", "book"])
    def test_workbook_stream_name_in_any_case_is_xls(self, name):
        data = builder.ole_container({name: builder.xls_bytes([("견적", [["품목", "금액"], ["볼트", "1200"]], b"")])})

        result = convert(data, "a.xls")

        assert result.format is DocumentFormat.XLS
        assert "| 볼트 | 1200 |" in result.text


class TestRawBiffWithoutOle:
    """A PHP xlsBOF-style 'Excel download' serves only BIFF records, not wrapped in OLE. The extractor can read them, but the
    detection step rejected them with 'This binary file format is not supported.'"""

    @pytest.mark.parametrize("bof", [(0x0809, 0x0000, 0x0010), (0x0809, 0x0500, 0x0010), (0x0409, 0x0000, 0x0010),
                                     (0x0209, 0x0000, 0x0010)])
    def test_raw_biff_worksheet_stream_is_xls(self, bof):
        data = builder.raw_biff_xls([["Name", "Amount"], ["Kim", 1234.5]], bof=bof)

        result = detect_format(data, "export.xls")

        assert result is DocumentFormat.XLS

    @pytest.mark.parametrize("data", [
        pytest.param(b"\x09\x08\x08\x00\x00\x00\x10\x00" + bytes(range(256)) * 4, id="bof-then-garbage"),
        pytest.param(b"\x09\x08\x08\x00\x00\x00\x99\x00\x00\x00\x00\x00" + b"\x0a\x00\x00\x00", id="unknown-substream-kind"),
        pytest.param(b"\x09\x08\xff\x00" + bytes(300), id="implausible-bof-length"),
    ])
    def test_binary_that_only_starts_like_a_bof_is_not_xls(self, data):
        """The first 4 bytes shaped like a BOF are not enough to take the file for XLS. The records must run on to the end (EOF) and
        the substream kind must be a known value."""
        with pytest.raises(UnsupportedFormatError):
            detect_format(data, "a.bin")


class TestOleStreamNameCaseForOtherDocuments:
    """Like the `WORKBOOK` of XLS, other OLE stream names are case-insensitive too. A password-protected docx saved with an
    upper-case `ENCRYPTEDPACKAGE` (the Tika test file protected_upper_case.docx) was rejected with 'This OLE document format is
    not supported.' instead of the password advice."""

    @pytest.mark.parametrize("name", ["ENCRYPTEDPACKAGE", "encryptedpackage", "EncryptedPackage"])
    def test_encrypted_package_in_any_case_asks_for_the_password_to_be_removed(self, name):
        data = builder.ole_container({name: b"\x00" * 4096, "EncryptionInfo": b"\x04\x00\x04\x00" + b"\x00" * 4092})

        with pytest.raises(EncryptedDocumentError):
            detect_format(data, "a.docx")

    @pytest.mark.parametrize("name", ["WORDDOCUMENT", "worddocument"])
    def test_old_word_stream_in_any_case_is_reported_as_old_word(self, name):
        data = builder.ole_container({name: b"\x00" * 4096})

        assert detect_format(data, "a.doc") is DocumentFormat.DOC
