"""Encoding detection for TXT/MD, and HTML conversion."""

import codecs
import time

import pytest

from opendockit import ConvertOptions, DocumentFormat, convert
from opendockit.extractors.text import decode_text


class TestDecodeText:
    @pytest.mark.parametrize(
        ("data", "encoding"),
        [
            ("한글 문서".encode(), "utf-8"),
            ("한글 문서".encode("utf-8-sig"), "utf-8-sig"),
            ("한글 문서".encode("utf-16"), "utf-16"),
            ("한글 문서입니다. 인코딩 판별".encode("cp949"), "cp949"),
        ],
    )
    def test_decodes_common_korean_encodings(self, data, encoding):
        text, detected = decode_text(data)

        assert "한글 문서" in text
        assert detected == encoding

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
    def test_decodes_utf16_without_bom(self, encoding):
        """UTF-16 text saved without a BOM by Windows tools was rejected as 'binary' because of its NUL bytes."""
        data = "한글 메모 2026\n둘째 줄".encode(encoding)

        result = convert(data, "memo.txt")

        assert result.format is DocumentFormat.TEXT
        assert result.text == "한글 메모 2026\n둘째 줄"
        assert result.metadata["encoding"] == encoding

    def test_short_cp949_is_not_misread_as_other_cjk_codec(self):
        """A statistical detector easily mistakes short CP949 for another East Asian codec."""
        text, _ = decode_text("가나".encode("cp949"))

        assert text == "가나"


class TestTextAndMarkdown:
    def test_markdown_keeps_code_indentation(self):
        data = "# 제목\n\n```python\ndef f():\n    return 1\n```\n".encode()

        result = convert(data, "readme.md")

        assert result.format is DocumentFormat.MARKDOWN
        assert "    return 1" in result.text

    def test_text_normalizes_newlines_and_blank_runs(self):
        result = convert("첫 줄\r\n\r\n\r\n\r\n둘째 줄  \r\n".encode(), "memo.txt")

        assert result.text == "첫 줄\n\n둘째 줄"


class TestHtml:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        html = """
        <html><head><title>공지</title><style>p{}</style><script>alert(1)</script></head>
        <body>
          <nav hidden>숨은 메뉴</nav>
          <h1>안내</h1>
          <p>첫   문단<br>둘째 줄</p>
          <div style="display:none">안 보이는 글</div>
          <ul><li>하나<ul><li>하위</li></ul></li><li>둘</li></ul>
          <ol start="3"><li>셋</li></ol>
          <table>
            <tr><th>분류</th><th>항목</th></tr>
            <tr><td rowspan="2">과일</td><td>사과</td></tr>
            <tr><td>배</td></tr>
          </table>
          <pre>code  keep</pre>
          <!-- 주석 -->
        </body></html>
        """
        return convert(html.encode(), "page.html")

    def test_removes_script_style_hidden_and_comments(self, result):
        for noise in ("alert", "p{}", "숨은 메뉴", "안 보이는 글", "주석"):
            assert noise not in result.text

    def test_renders_headings_paragraphs_and_nested_lists(self, result):
        assert "# 안내\n\n첫 문단\n둘째 줄" in result.text
        assert "- 하나\n  - 하위\n- 둘" in result.text
        assert "3. 셋" in result.text

    def test_expands_rowspan_into_grid(self, result):
        assert "| 분류 | 항목 |\n| --- | --- |\n| 과일 | 사과 |\n| 과일 | 배 |" in result.text

    def test_keeps_pre_block_and_title(self, result):
        assert "```\ncode  keep\n```" in result.text
        assert result.metadata["title"] == "공지"


def test_html_list_item_with_paragraphs_and_code_block_keeps_them_as_blocks():
    """Paragraphs and code blocks inside a list item were merged into one line. In a document with a command block per item, such
    as an installation guide, multi-line commands were joined with spaces into one line (a 6,000-character line in a real
    repository README)."""
    html = ("<ol><li><p>설정 마법사 실행</p><p>프로젝트   루트에서 실행한다:</p><pre>make setup\nmake dev</pre>"
            "<p>끝나면 확인한다.</p></li><li>다음 단계</li></ol>")

    result = convert(html.encode(), "a.html")

    assert result.text == ("1. 설정 마법사 실행\n\n프로젝트 루트에서 실행한다:\n\n```\nmake setup\nmake dev\n```\n\n"
                           "끝나면 확인한다.\n\n2. 다음 단계")


class TestDeepNesting:
    """Finding every descendant and walking up the ancestors of each one was quadratic in the depth: tables nested 20,000 deep
    (300KB) took 59 s, and lists nested 14,000 deep (112KB) were rejected with a recursion limit error after 73 s."""

    @pytest.mark.timing
    def test_deeply_nested_tables_are_read_quickly(self):
        import time

        html = "<table><tr><td>" * 20_000 + "가장 안쪽" + "</td></tr></table>" * 20_000

        started = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - started < 12
        assert "가장 안쪽" in result.text

    @pytest.mark.timing
    def test_deeply_nested_lists_are_read_quickly_and_folded(self):
        import time

        html = "<ul><li>항목" * 14_000 + "가장 안쪽" + "</li></ul>" * 14_000

        started = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - started < 5
        assert "가장 안쪽" in result.text
        assert max(len(line) - len(line.lstrip()) for line in result.text.split("\n")) <= 2 * 20


class TestDamagedLegacyKorean:
    """When strict CP949 decoding failed on a single byte, statistical detection took over and all the Hangul was garbled into
    Japanese and Thai characters, without a warning."""

    @pytest.mark.parametrize(
        "data",
        [
            ("안녕하십니까 회의록입니다" * 20).encode("cp949")[:-1],
            "안녕하세요. 회의록입니다.\n".encode("cp949") * 5 + b"\xc9\xa1" + "참석자: 홍길동".encode("cp949"),
        ],
        ids=["cut-last-character", "user-defined-character"],
    )
    def test_is_read_as_korean_with_a_warning(self, data):
        result = convert(data, "회의록.txt")

        assert "회의록입니다" in result.text
        assert result.metadata["encoding"] == "cp949(replaced)"
        assert any("were replaced with �" in warning for warning in result.warnings)

    def test_latin_text_is_not_read_as_korean(self):
        text, encoding = decode_text("Déjà vu, café crème. Ça va? Où êtes-vous?".encode("latin-1") * 20)

        assert "café" in text
        assert not encoding.startswith("cp949")

    def test_guess_from_the_head_is_kept_when_a_later_byte_is_invalid(self):
        """Statistical detection chooses from the first 200KB only. If even one later byte could not be decoded with that encoding,
        the guess was dropped for UTF-8 with replacement, garbling everything including the front."""
        data = "Привет, как дела? Это проверка.\n".encode("cp1251") * 8_000 + b"\x98" + "Конец".encode("cp1251")

        text, encoding = decode_text(data)

        assert text.startswith("Привет, как дела?")
        assert text.endswith("Конец")
        assert encoding.endswith("(replaced)")


class TestTolerantTextDetection:
    def test_utf16_without_bom_keeps_form_feed_and_escape(self):
        """UTF-16 without a BOM was rejected as binary if it had even one control character (page break \\f, ESC)."""
        data = "1쪽 내용\f2쪽 내용 \x1b[0m끝\n".encode("utf-16-le")

        result = convert(data, "memo.txt")

        assert "1쪽 내용" in result.text
        assert "2쪽 내용" in result.text

    def test_utf16_without_bom_with_odd_byte_count(self):
        data = "hello world 안녕하세요\n".encode("utf-16-le") + b"\n"

        result = convert(data, "memo.txt")

        assert result.text == "hello world 안녕하세요"

    @pytest.mark.parametrize(("bom", "encoding"), [(codecs.BOM_UTF32_BE, "utf-32-be"), (codecs.BOM_UTF32_LE, "utf-32-le")],
                             ids=["be", "le"])
    def test_utf32_with_bom_is_text(self, bom, encoding):
        """Text with a UTF-32 BE BOM (`00 00 FE FF`) was rejected as 'binary' (LE passed by chance because its BOM starts with the
        UTF-16 LE BOM). UTF-32 with a BOM is text, like UTF-16."""
        data = bom + "한글 메모 2026\n둘째 줄".encode(encoding)

        result = convert(data, "memo.txt")

        assert result.text == "한글 메모 2026\n둘째 줄"
        assert result.metadata["encoding"] == "utf-32"

    def test_utf32_with_bom_is_decided_by_content_when_the_extension_says_nothing(self):
        data = codecs.BOM_UTF32_BE + "<html><body><p>글</p></body></html>".encode("utf-32-be")

        result = convert(data, None)

        assert result.format is DocumentFormat.HTML
        assert result.text == "글"

    def test_utf32_csv_with_bom_is_a_table(self):
        data = codecs.BOM_UTF32_BE + "지역,값\n서울,1\n".encode("utf-32-be")

        result = convert(data, "a.csv")

        assert result.text == "| 지역 | 값 |\n| --- | --- |\n| 서울 | 1 |"

    def test_page_break_and_vertical_tab_in_text_and_csv_separate_their_neighbours(self):
        """`합계 100\\f200` was joined into `합계 100200`, and the CSV cell `2\\f3` into `23` (`markdown.clean_text`, `_cell_lines`)."""
        text = convert("합계 100\x0c200 이월".encode(), "a.txt")
        table = convert(b"a,b\n1,2\x0c3\n", "a.csv")

        assert text.text == "합계 100\n200 이월"
        assert table.text == "| a | b |\n| --- | --- |\n| 1 | 2<br>3 |"

    def test_utf8_text_with_a_stray_nul_is_text(self):
        """A single NUL byte made the whole UTF-8 text rejected as binary."""
        data = b"hello\x00world " + "안녕하세요. 회의록입니다.\n".encode() * 3

        result = convert(data, "memo.txt")

        assert result.format is DocumentFormat.TEXT
        assert "회의록입니다" in result.text


class TestLegacyTextWithNulBytes:
    """Only UTF-8 tolerated NUL bytes. CP949 text was rejected as binary over a single NUL, and a fixed-width export that pads its
    columns with NULs was rejected whatever its encoding."""

    @pytest.mark.parametrize("filename", ["a.csv", "a.txt", None])
    def test_cp949_text_with_a_stray_nul_is_read(self, filename):
        data = ("이름,금액\n" + "홍길동,1000\n" * 200).encode("cp949") + b"\x00"

        result = convert(data, filename)

        assert result.format is (DocumentFormat.CSV if filename == "a.csv" else DocumentFormat.TEXT)
        assert "홍길동" in result.text
        assert "\x00" not in result.text
        assert result.metadata["encoding"] == "cp949"

    def test_short_cp949_text_with_a_nul_is_not_read_as_utf16(self):
        """Taken two bytes at a time it decodes into printable characters, so it was judged to be UTF-16 without a BOM and the whole
        text was garbled."""
        data = "훔힛좼 남,100\n".encode("cp949")

        result = convert(data[:10] + b"\x00" + data[10:], "memo.txt")

        assert result.text == "훔힛좼 남,100"

    @pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
    def test_nul_padded_fixed_width_export_keeps_its_columns(self, encoding):
        """A fixed-width export that pads its columns with NULs. Just deleting the NULs runs the columns together like '홍길동1000' —
        a run of NULs is kept as the same number of spaces."""
        rows = [("이름", "금액"), ("홍길동", "1000"), ("김철수", "25000")]
        data = b"".join(name.encode(encoding).ljust(12, b"\x00") + amount.encode(encoding).ljust(8, b"\x00") + b"\r\n"
                        for name, amount in rows)

        result = convert(data, "export.txt")

        assert result.format is DocumentFormat.TEXT
        assert "홍길동" in result.text and "1000" in result.text
        assert "홍길동1000" not in result.text
        assert "\x00" not in result.text


class TestLayoutTables:
    def test_layout_table_with_a_nested_data_table_keeps_the_inner_table(self):
        """A data table inside a layout table was merged into one line in a single cell: "이름 점수 홍 90 김 80"."""
        html = ("<table><tr><td><h1>보고서</h1><p>본문</p><table><tr><th>이름</th><th>점수</th></tr>"
                "<tr><td>홍</td><td>90</td></tr><tr><td>김</td><td>80</td></tr></table></td><td>사이드</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "# 보고서\n\n본문\n\n| 이름 | 점수 |\n| --- | --- |\n| 홍 | 90 |\n| 김 | 80 |\n\n사이드"

    def test_data_table_with_rich_cells_stays_a_table(self):
        html = ("<table><tr><th>항목</th><th>설명</th></tr><tr><td>가</td><td><p>첫 문단</p><p>둘째</p></td></tr>"
                "<tr><td>나</td><td><table><tr><td>안쪽 한 줄</td></tr></table></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("| 항목 | 설명 |\n| --- | --- |\n| 가 |")
        assert "| 나 | 안쪽 한 줄 |" in result.text

    @pytest.mark.timing
    def test_deeply_nested_layout_tables_are_read_quickly(self):
        """Layout tables are unfolded into blocks only down to 20 levels; deeper ones are folded into one line (recursion limit)."""
        import time

        html = "<table><tr><td>바깥</td></tr><tr><td>" * 5_000 + "가장 안쪽" + "</td></tr></table>" * 5_000

        started = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - started < 12
        assert "가장 안쪽" in result.text

    def test_data_table_with_a_small_nested_table_in_one_cell_stays_a_table(self):
        """Unfolding the whole outer table into blocks just because one cell of an infobox (many name | value rows) holds a small
        table scatters the pairing of names and values."""
        rows = "".join(f"<tr><th>항목 {number}</th><td>값 {number}</td></tr>" for number in range(8))
        html = (f"<table>{rows}<tr><th>온도</th><td><table><tr><th>최저</th><th>최고</th></tr>"
                "<tr><td>100</td><td>390</td></tr></table></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "| 항목 7 | 값 7 |" in result.text
        assert "| 온도 | 최저 최고 100 390 |" in result.text

    @pytest.mark.parametrize("outer", [
        "<tr><td colspan=2>로고</td></tr><tr><td>메뉴 1<br>메뉴 2</td><td>{body}</td></tr><tr><td colspan=2>Copyright</td></tr>",
        "<tr><td><h3>제목</h3></td></tr><tr><td>작성일 2025-03-02</td></tr><tr><td>{body}</td></tr><tr><td>Copyright</td></tr>",
    ], ids=["menu-body-footer", "single-column"])
    def test_data_table_inside_the_body_cell_of_a_page_layout_keeps_its_rows(self, outer):
        """Groupware notices and HTML mail lay out logo / body / footer with an outer table. The body text was longer than the inner
        table, so the outer table was judged a data table and the inner table was flattened into the single line
        "구분 변경 전 변경 후 식대 10만원 15만원". When the cell holding the inner table has at least half of the outer table's text,
        the outer table is a layout table."""
        body = ("<p>" + "본 공지는 사내 복지 제도 변경에 관한 내용입니다. " * 6 + "</p><table border=1><tr><td>구분</td>"
                "<td>변경 전</td><td>변경 후</td></tr><tr><td>식대</td><td>10만원</td><td>15만원</td></tr></table><p>이상.</p>")
        html = f"<table>{outer.format(body=body)}</table>"

        result = convert(html.encode(), "a.html")

        assert "| 구분 | 변경 전 | 변경 후 |\n| --- | --- | --- |\n| 식대 | 10만원 | 15만원 |\n\n이상." in result.text
        assert result.text.endswith("Copyright")

    def test_body_cell_of_a_mail_layout_whose_table_is_longer_than_its_paragraphs_keeps_the_table(self):
        """In an HTML mail laid out as head (logo | notice number) / body / footer, when the table text in the body cell was longer
        than that cell's paragraphs (but under half of the whole), every layout condition was false and the inner table was flattened
        into the single line "구분 일정 비고 하계 휴가 …". An outer table with only one row of two or more cells has no column
        structure."""
        html = ("<table width=600><tr><td><img src='logo.png' alt=''></td><td>사내공지 2024-07호</td></tr>"
                "<tr><td colspan=2><p><b>[공지] 하계 휴가 및 사무실 이전 안내</b></p><p>임직원 여러분 안녕하십니까. 아래와 같이 안내드립니다.</p>"
                "<table border=1><tr><td>구분</td><td>일정</td><td>비고</td></tr>"
                "<tr><td>하계 휴가</td><td>7/29(월) ~ 8/2(금)</td><td>필수 근무자 제외</td></tr>"
                "<tr><td>사무실 이전</td><td>8/10(토)</td><td>판교 → 강남 (4층)</td></tr>"
                "<tr><td>비용 정산</td><td>~ 8/16(금)</td><td>1인 최대 300,000원</td></tr></table>"
                "<p>문의: 총무팀 김OO 대리 (☎ 02-123-4567)</p></td></tr>"
                "<tr><td colspan=2>본 메일은 발신 전용입니다.</td></tr></table>")

        result = convert(html.encode(), "mail.html")

        assert result.text == ("사내공지 2024-07호\n\n[공지] 하계 휴가 및 사무실 이전 안내\n\n임직원 여러분 안녕하십니까. 아래와 같이 안내드립니다.\n\n"
                               "| 구분 | 일정 | 비고 |\n| --- | --- | --- |\n| 하계 휴가 | 7/29(월) ~ 8/2(금) | 필수 근무자 제외 |\n"
                               "| 사무실 이전 | 8/10(토) | 판교 → 강남 (4층) |\n| 비용 정산 | ~ 8/16(금) | 1인 최대 300,000원 |\n\n"
                               "문의: 총무팀 김OO 대리 (☎ 02-123-4567)\n\n본 메일은 발신 전용입니다.")

    def test_form_whose_rows_pair_names_with_values_stays_a_table_around_its_body_table(self):
        """A form with several name | value rows, such as an approval request, is a data table even when its body cell has more than
        half of the text and the inner table is longer than that cell's paragraphs. Unfolding it as a layout scatters the pairing of
        '문서번호' and '재무-2024-0312'."""
        html = ("<table><tr><th colspan=2>지 출 품 의 서</th></tr><tr><th>문서번호</th><td>재무-2024-0312</td></tr>"
                "<tr><th>기안일자</th><td>2024-03-12</td></tr><tr><td colspan=2><p>1. 목적: 2분기 사무용품 일괄 구매</p>"
                "<table><tr><th>품목</th><th>수량</th><th>금액</th></tr><tr><td>A4 용지</td><td>50</td><td>1,250,000</td></tr>"
                "<tr><td>토너</td><td>10</td><td>890,000</td></tr></table></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "<tr><td>문서번호</td><td>재무-2024-0312</td></tr>" in result.text

    def test_tables_inside_the_cells_of_an_html_form_keep_their_rows_and_columns(self):
        """When the outer form is already an HTML table (horizontal spans), the approval box and item table inside its cells are kept
        as one-line HTML tables. They were flattened into the single line "품목 수량 단가 금액 A4 용지 50 …", so one could not tell
        which column the '합계' value belonged to. The same shape as `TableGrid.embed` of HWP, HWPX and DOCX."""
        html = ("<table border=1><tr><td colspan=2><b>지 출 품 의 서</b></td><td rowspan=3><table><tr><th>담당</th><th>팀장</th>"
                "</tr><tr><td>홍길동</td><td>(전결)</td></tr></table></td></tr><tr><th>문서번호</th><td>재무-2024-0312</td></tr>"
                "<tr><th>기안일자</th><td>2024-03-12</td></tr><tr><td colspan=3><p>1. 목적: 사무용품 구매</p><table border=1>"
                "<tr><th>품목</th><th>수량</th><th>금액</th></tr><tr><td>A4 용지</td><td>50</td><td>1,250,000</td></tr>"
                "<tr><td colspan=2>합계</td><td>1,250,000</td></tr></table><p>2. 지급: 법인카드</p></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == (
            "<table>\n"
            '<tr><th colspan="2">지 출 품 의 서</th><th rowspan="3"><table><tr><th>담당</th><th>팀장</th></tr>'
            "<tr><td>홍길동</td><td>(전결)</td></tr></table></th></tr>\n"
            "<tr><td>문서번호</td><td>재무-2024-0312</td></tr>\n"
            "<tr><td>기안일자</td><td>2024-03-12</td></tr>\n"
            '<tr><td colspan="3">1. 목적: 사무용품 구매<br><table><tr><th>품목</th><th>수량</th><th>금액</th></tr>'
            '<tr><td>A4 용지</td><td>50</td><td>1,250,000</td></tr><tr><td colspan="2">합계</td><td>1,250,000</td></tr>'
            "</table><br>2. 지급: 법인카드</td></tr>\n"
            "</table>")

    def test_data_table_whose_large_cell_is_mostly_a_nested_table_stays_a_table(self):
        """Unfolding as a layout a data table with a large cell holding one line of description + a table of choices (the Apache
        manual's 'parameter | default | description') scatters the pairing of parameters and defaults. When the cell's text is
        mostly the inner table, the outer table is kept."""
        choices = "".join(f"<tr><td>METHOD{number}</td><td>Send a request to the backend via HTTP/1.{number}</td></tr>"
                          for number in range(8))
        rows = "".join(f"<tr><td>hcparam{number}</td><td>{number}</td><td>Number of successful health check tests {number}</td></tr>"
                       for number in range(8))  # the inner table is under half of the text; the cell holding it is over half
        html = ("<table><tr><th>Parameter</th><th>Default</th><th>Description</th></tr><tr><td>hcmethod</td><td>None</td>"
                f"<td>No dynamic health check performed. Choices are:<table><tr><th>Method</th><th>Description</th></tr>{choices}"
                f"</table></td></tr>{rows}</table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("| Parameter | Default | Description |\n| --- | --- | --- |\n| hcmethod | None | No dynamic")
        assert "| hcparam7 | 7 | Number of successful health check tests 7 |" in result.text

    def test_name_value_table_whose_text_is_mostly_a_nested_table_keeps_both_structures(self):
        """When the '대상자' cell of an '항목 | 내용' table held a 9-row table of names, the inner table had more than half of the
        text, so the outer table was unfolded as a layout and both the pairing of '교육명' with '정보보호 교육' and the header row
        were lost. With three or more rows of two or more cells whose first cell is a short name, it stays a data table, and since
        the inner table is the main content it is not flattened into one line but kept as a table inside an HTML cell."""
        people = "".join(f"<tr><td>사람{number}</td><td>개발{number}팀</td><td>대리</td></tr>" for number in range(1, 10))
        html = ("<table><tr><th>항목</th><th>내용</th></tr><tr><td>교육명</td><td>정보보호 교육</td></tr>"
                "<tr><td>일시</td><td>2026-10-10 14:00</td></tr>"
                f"<tr><td>대상자</td><td><table><tr><th>성명</th><th>부서</th><th>직급</th></tr>{people}</table></td></tr>"
                "<tr><td>비고</td><td>필참</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("<table>\n<tr><th>항목</th><th>내용</th></tr>\n<tr><td>교육명</td><td>정보보호 교육</td></tr>")
        assert "<tr><td>대상자</td><td><table><tr><th>성명</th><th>부서</th><th>직급</th></tr>" in result.text
        assert "<tr><td>사람9</td><td>개발9팀</td><td>대리</td></tr></table></td></tr>" in result.text

    def test_data_table_with_a_small_table_in_every_row_keeps_its_header_and_rows(self):
        rows = "".join(f"<tr><td>r{number}</td><td><table><tr><td>a{number}</td><td>b{number}</td></tr>"
                       f"<tr><td>c{number}</td><td>d{number}</td></tr></table></td></tr>" for number in range(4))
        html = f"<table><tr><th>구분</th><th>값</th></tr>{rows}</table>"

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("<table>\n<tr><th>구분</th><th>값</th></tr>\n")
        assert "<tr><td>r3</td><td><table><tr><th>a3</th><th>b3</th></tr><tr><td>c3</td><td>d3</td></tr></table></td></tr>" in result.text

    def test_name_value_table_whose_nested_table_has_a_two_row_header_stays_a_layout(self):
        """A table with a multi-row header cannot be kept inside an HTML cell, so leaving the outer table a data table flattens it
        into the single line '요금 정가 할인가 100 90'."""
        html = ("<table><tr><th>a</th><th>b</th></tr><tr><td>x</td><td><table><thead><tr><th colspan=2>요금</th></tr>"
                "<tr><th>정가</th><th>할인가</th></tr></thead><tr><td>100</td><td>90</td></tr></table></td></tr>"
                "<tr><td>y</td><td>1</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "| 요금 정가 | 요금 할인가 |\n| --- | --- |\n| 100 | 90 |" in result.text

    def test_page_layout_whose_body_cell_is_a_table_beside_a_menu_stays_a_layout(self):
        """A logo | title / menu | body table / footer layout has only two rows of two or more cells, so it is not a name | value
        table. The body table is kept as a table."""
        body = ("<table border=1><tr><td>구분</td><td>변경 전</td><td>변경 후</td></tr><tr><td>식대</td><td>10만원</td>"
                "<td>15만원</td></tr><tr><td>교통비</td><td>5만원</td><td>7만원</td></tr></table>")
        html = (f"<table><tr><td>로고</td><td>사내 공지</td></tr><tr><td>메뉴</td><td>{body}</td></tr>"
                "<tr><td colspan=2>Copyright</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("로고\n\n사내 공지\n\n메뉴\n\n| 구분 | 변경 전 | 변경 후 |\n| --- | --- | --- |\n| 식대 | 10만원 | 15만원 |\n"
                               "| 교통비 | 5만원 | 7만원 |\n\nCopyright")


def test_bare_table_rows_without_a_table_element_are_rendered_as_a_table():
    """An 'Excel download' served starting from `<tr>` with no `<table>` became the single line "이름금액홍길동1000", because the
    HTML parser drops the row and cell tags."""
    html = "<tr><td>이름</td><td>금액</td></tr><tr><td>홍길동</td><td>1000</td></tr>"

    result = convert(html.encode(), "목록.xls")

    assert result.text == "| 이름 | 금액 |\n| --- | --- |\n| 홍길동 | 1000 |"


class TestListContentOutsideItems:
    """Only the direct `<li>` children of a list were read, so content placed outside an `<li>` vanished without a warning (common
    in old editors and hand-written HTML)."""

    def test_list_nested_directly_inside_a_list_is_kept_as_a_nested_list(self):
        html = "<ul><li>상위 1</li><ul><li>하위 1</li><li>하위 2</li></ul><li>상위 2</li></ul>"

        result = convert(html.encode(), "a.html")

        assert result.text == "- 상위 1\n  - 하위 1\n  - 하위 2\n- 상위 2"

    def test_text_and_wrapped_items_directly_inside_a_list_are_kept(self):
        html = "<ul>머리말 <b>글</b><li>항목</li><p>문단 글</p><div><li>감싼 항목</li></div></ul>"

        result = convert(html.encode(), "a.html")

        assert result.text == "- 머리말 글\n- 항목\n- 문단 글\n- 감싼 항목"

    def test_numbers_of_an_ordered_list_skip_content_outside_items(self):
        html = "<ol><li>첫째</li><ul><li>하위</li></ul><li>둘째</li></ol>"

        result = convert(html.encode(), "a.html")

        assert result.text == "1. 첫째\n  - 하위\n2. 둘째"


def test_html_superscript_and_subscript_are_marked():
    """Superscripts and subscripts were joined as plain text, so `10<sup>6</sup>` read as 106. Footnote marks that start with a
    bracket are left as they are."""
    html = ("<p>인구 5×10<sup>6</sup>명, 면적 605km<sup>2</sup>, CO<sub>2</sub>, 10<sup>-3</sup>, x<sub>ij</sub>, "
            "각주<sup>[1]</sup><sup>: 125</sup>, 주석<sup>†</sup></p>")

    result = convert(html.encode(), "a.html")

    assert result.text == "인구 5×10^6명, 면적 605km^2, CO_2, 10^{-3}, x_{ij}, 각주[1]: 125, 주석†"


def test_html_superscript_with_hangul_or_hanja_is_left_as_plain_text():
    """A script containing Hangul or hanja is not an exponent but a raised unit or footnote mark. They became '1,000^원' and
    '각주^{주1)}' (the same rule as PDF scripts)."""
    html = "<p>가격 1,000<sup>원</sup>, 각주<sup>주1)</sup>, 면적 10<sup>3</sup>m<sup>2</sup>, 비율<sub>年</sub></p>"

    result = convert(html.encode(), "a.html")

    assert result.text == "가격 1,000원, 각주주1), 면적 10^3m^2, 비율年"


def test_html_line_breaks_inside_a_code_block_are_kept():
    """A `<br>` inside `<pre>` was removed without leaving a character, so code lines ran together."""
    result = convert("<pre>줄1<br>줄2<br/>줄3</pre>".encode(), "a.html")

    assert result.text == "```\n줄1\n줄2\n줄3\n```"


def test_html_text_after_a_block_directly_inside_a_list_is_a_separate_line():
    """A paragraph outside the list items and the text after it were joined as '문단뒤 글'. A break is also made where a block ends."""
    result = convert("<ul><p>문단</p>뒤 글<li>항목</li></ul>".encode(), "a.html")

    assert result.text == "- 문단\n- 뒤 글\n- 항목"


class TestLargeHtmlStopsEarly:
    """HTML was parsed whole, spending 34 s and 2.5 GB on a 40MB document that would be rejected anyway for exceeding the character
    limit. A document that clearly exceeds the limit is parsed only at the front, then parsing stops."""

    @staticmethod
    def _parsed_lengths(monkeypatch):
        from opendockit.extractors import html as html_extractor

        lengths = []
        original = html_extractor.BeautifulSoup

        def recording(markup, *args, **kwargs):
            lengths.append(len(markup))
            return original(markup, *args, **kwargs)

        monkeypatch.setattr(html_extractor, "BeautifulSoup", recording)
        return lengths

    def test_document_far_over_the_limit_is_parsed_only_at_the_front(self, monkeypatch):
        lengths = self._parsed_lengths(monkeypatch)
        html = "<html><body>" + "<p>가나다라마바사아자차</p>" * 20_000 + "</body></html>"

        result = convert(html.encode(), "a.html", ConvertOptions(max_chars=1_000))

        assert result.exceeded
        assert result.text.startswith("가나다라마바사아자차")
        assert lengths == [max(lengths)] and lengths[0] < len(html) // 4

    def test_long_markup_with_little_text_is_read_to_the_end_and_not_exceeded(self, monkeypatch):
        lengths = self._parsed_lengths(monkeypatch)
        html = "<html><body><p>처음</p>" + "<!-- 채움 -->" * 20_000 + "<p>끝</p></body></html>"

        result = convert(html.encode(), "a.html", ConvertOptions(max_chars=1_000))

        assert not result.exceeded
        assert result.text == "처음\n\n끝"
        assert lengths[-1] == len(html)

    def test_markup_heavy_document_over_the_limit_is_not_parsed_whole(self, monkeypatch):
        """An 'Excel download' (47MB) with a format attribute on every cell had a front part (limit × 8 characters) within the limit,
        so the whole was parsed again, spending 22 s and 2.6 GB. The front part is grown, by its text density, to just enough to
        exceed the limit and looked at again."""
        lengths = self._parsed_lengths(monkeypatch)
        row = "<tr>" + "".join(f"<td style=\"mso-number-format:'\\@'\">값{column}</td>" for column in range(10)) + "</tr>\n"
        html = "<html><body><table>" + row * 2_000 + "</table></body></html>"

        result = convert(html.encode(), "export.xls", ConvertOptions(max_chars=1_000))

        assert result.exceeded
        assert result.text.startswith("| 값0 | 값1 |")
        assert max(lengths) < len(html) // 4

    def test_dense_table_over_the_limit_parses_little_more_than_it_needs(self, monkeypatch):
        """A text-dense 'Excel download' table exceeds the limit at about limit × 2 characters, but the front part started at
        limit × 8 characters, so four times what was needed was parsed and rendered (100,000 rows, 9MB: 7.6 s). Start small and grow
        by text density."""
        lengths = self._parsed_lengths(monkeypatch)
        rows = "".join(f"<tr><td>{i}</td><td>홍길동{i}</td><td>개발팀</td><td>{i * 1000:,}</td></tr>" for i in range(3_000))
        html = f"<html><body><table><tr><th>번호</th><th>이름</th><th>부서</th><th>금액</th></tr>{rows}</table></body></html>"

        result = convert(html.encode(), "export.html", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.text.startswith("| 번호 | 이름 | 부서 | 금액 |")
        assert sum(lengths) < 2_000 * 6

    def test_exceeded_is_the_same_as_reading_the_whole_document(self):
        row = "<tr>" + "".join(f"<td style=\"mso-number-format:'\\@'\">값{column}</td>" for column in range(10)) + "</tr>\n"
        html = ("<html><body><table>" + row * 300 + "</table></body></html>").encode()
        whole = convert(html, "a.html").char_count

        results = [convert(html, "a.html", ConvertOptions(max_chars=limit)) for limit in (whole - 1, whole)]

        assert [result.exceeded for result in results] == [True, False]


def test_html_ordinal_suffix_after_a_number_is_not_marked_as_superscript():
    """Even ordinal suffixes were marked as superscripts, so 21st became 21^{st}. An ordinal suffix after a number is correct as
    plain text."""
    html = "<p>21<sup>st</sup>, 2<sup>nd</sup>, 13<sup>TH</sup>, 3<sup>ème</sup>, x<sup>th</sup>, 2<sup>n</sup></p>"

    result = convert(html.encode(), "a.html")

    assert result.text == "21st, 2nd, 13TH, 3ème, x^{th}, 2^n"


class TestLegacyKoreanWithoutHangulAtTheFront:
    """Even when strict CP949 decoding succeeded, text with no Hangul in the first 20,000 characters was handed to statistical
    detection, so the symbols (㎡, ℃, ※, →) of mostly English-and-number CSV/TXT saved by Excel on Korean Windows were garbled
    into Johab, Big5 and CP1250 characters."""

    @pytest.mark.parametrize(
        "text",
        [
            "Model,Area,Temp,Note\nA-100,25㎡,25℃,OK\nB-200,33㎡,30℃,※ check\n",
            "Q1·Q2 Report “Final”\nRevenue: 100 → 120\n",
            "No,Item,Spec\n" + "".join(f"{number},PART-{number},10㎜×20㎜\n" for number in range(30)),
            "ID,NAME\n1,ABC\n" * 50 + "2,㈜ABC\n",
            "山田,李\n",
        ],
        ids=["units", "middle-dot-and-quotes", "many-rows", "one-symbol", "hanja-only"],
    )
    def test_symbols_without_any_hangul_are_read_as_cp949(self, text):
        decoded, encoding = decode_text(text.encode("cp949"))

        assert decoded == text
        assert encoding == "cp949"

    def test_hangul_that_first_appears_after_many_rows_needs_no_replacement_warning(self):
        """A CSV whose first Hangul appears after 3,000 rows of numbers got `cp949(replaced)` and a warning although it had no
        replacement character at all."""
        data = ("".join(f"{number},{number * 37},{number * 91}\n" for number in range(3_000)) + "합계,1,2\n").encode("cp949")

        result = convert(data, "a.csv", ConvertOptions(max_chars=None))

        assert result.text.endswith("| 합계 | 1 | 2 |")
        assert result.metadata["encoding"] == "cp949"
        assert not result.warnings

    @pytest.mark.parametrize("text", ["가나", "회의 메모", "A동 3호", "똠방각하 OK", "abc 햏햏 가 def", "값: 1㎡"])
    def test_short_korean_memo_is_still_cp949(self, text):
        """The other direction: a memo of only two or three Hangul characters, or text mixing in rare syllables (the extended area),
        must not fall through to Windows-1252."""
        decoded, encoding = decode_text(text.encode("cp949"))

        assert decoded == text
        assert encoding == "cp949"


class TestWesternText:
    """`’` (0x92) + a letter in Windows-1252 English decoded strictly as CP949 extended Hangul, so `Company’s` became `Company뭩`,
    and statistical detection mistook short text with only accents for CP1257, CP437 or CP932."""

    @pytest.mark.parametrize(
        "text",
        [
            "The Company’s obligations under this Agreement shall survive. It’s the Supplier’s duty. Don’t disclose.\n" * 5,
            "name,note\nO’Brien,customer’s order\nD’Angelo,don’t ship\n",
            "Temp 25°C, 50µm\n",
        ],
        ids=["apostrophes", "csv", "degree-and-micro"],
    )
    def test_bytes_that_only_swallow_a_following_letter_are_not_korean(self, text):
        decoded, encoding = decode_text(text.encode("cp1252"))

        assert decoded == text
        assert encoding == "cp1252"

    @pytest.mark.parametrize(
        "text",
        [
            "naïve façade déjà Señor",
            "Café résumé naïve façade – déjà vu. Señor Müller größe.\n" * 5,
            "Total – 5 items; price €20",
            "Temp 25°C ± 2°C, 50µm, ½ cup\n" * 3,
            "He said “hello” and left — it’s fine. “Quote” again.\n" * 5,
            "The Company’s duty. " * 20 + "“Quoted” text.",
            "Il fait beau aujourd’hui et il pleuvra demain. Je voudrais un café, s’il vous plaît. Où est l’arrêt ?\n" * 3,
            "A informação não está disponível. As condições são boas e as soluções também. Amanhã haverá reunião.\n" * 3,
        ],
        ids=["accents", "accents-long", "dash-and-euro", "symbols", "quotes", "apostrophes-with-one-quote", "french",
             "portuguese"],
    )
    def test_sparse_accents_and_punctuation_are_read_as_windows_1252(self, text):
        decoded, _ = decode_text(text.encode("cp1252"))

        assert decoded == text

    @pytest.mark.parametrize(
        ("text", "encoding"),
        [
            ("Привет, как дела? Это проверка.\n" * 3, "cp1251"),
            ("Zażółć gęślą jaźń. Się będzie są już można też.\n" * 5, "cp1250"),
            ("Příliš žluťoučký kůň úpěl ďábelské ódy. Dobrý den, jak se máte?\n" * 3, "cp1250"),
            ("Proszę się nie martwić, będzie dobrze. Już można też iść do domu. Dziękuję bardzo za pomoc, to było miłe. "
             "Czy możesz mi powiedzieć, gdzie jest najbliższy sklep? Wczoraj byłem w pracy, a dziś mam wolne.\n" * 3, "cp1250"),
        ],
        ids=["russian", "polish", "czech", "polish-with-sparse-accents"],
    )
    def test_other_single_byte_text_keeps_the_statistical_guess(self, text, encoding):
        """Text whose whole words are non-ASCII (Cyrillic), and text whose accents are sparse but which gives statistical detection
        clear language evidence (Polish), follow the guess."""
        decoded, _ = decode_text(text.encode(encoding))

        assert decoded == text


class TestHtmlDeclaredCharset:
    """HTML did not look at the document's charset declaration, so documents declaring `windows-1252` or `shift_jis` were garbled
    as CP949, without a warning."""

    @pytest.mark.parametrize(
        ("declaration", "text", "codec", "encoding"),
        [
            ('<meta charset="windows-1252">', "Àpropos: «Été» coûte 5 £", "cp1252", "cp1252"),
            ('<meta charset="iso-8859-1">', "Señor Müller – naïve", "cp1252", "cp1252"),
            ("<meta charset=shift_jis>", "これは日本語のテキストです。売上報告書。", "shift_jis", "cp932"),
            ('<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=gb2312">', "这是中文文本。销售报告。", "gb2312", "gbk"),
            ('<meta http-equiv="Content-Type" content="text/html; charset=windows-1251">', "Привет, мир", "cp1251", "cp1251"),
            ('<meta charset="ks_c_5601-1987">', "똠방각하 회의록", "cp949", "cp949"),
        ],
        ids=["windows-1252", "latin-1", "shift_jis", "gb2312", "windows-1251", "ks_c_5601"],
    )
    def test_declared_charset_is_used_when_it_decodes_strictly(self, declaration, text, codec, encoding):
        html = f"<html><head>{declaration}<title>T</title></head><body><p>{text}</p></body></html>"

        result = convert(html.encode(codec), "a.html")

        assert result.text == f"# T\n\n{text}"  # the <title> above a body with no heading element (TestTitleWhenTheBodyHasNoHeading)
        assert result.metadata["encoding"] == encoding
        assert not result.warnings

    def test_encoding_of_the_xml_declaration_is_used(self):
        html = '<?xml version="1.0" encoding="Shift_JIS"?><html><body><p>売上報告書</p></body></html>'

        result = convert(html.encode("shift_jis"), "a.html")

        assert result.text == "売上報告書"

    @pytest.mark.parametrize(
        ("declared", "codec", "encoding"),
        [("euc-kr", "utf-8", "utf-8"), ("iso-8859-1", "cp949", "cp949"), ("utf-8", "cp949", "cp949"), ("no-such-charset", "cp949", "cp949")],
        ids=["euc-kr-but-utf-8", "latin-1-but-cp949", "utf-8-but-cp949", "unknown-name"],
    )
    def test_declaration_that_does_not_match_the_bytes_is_not_trusted(self, declared, codec, encoding):
        """Korean pages with a false declaration (UTF-8 labelled EUC-KR, CP949 carrying the server default ISO-8859-1) are judged by
        their content."""
        html = f'<html><head><meta charset="{declared}"></head><body><p>한글 회의록입니다</p></body></html>'

        result = convert(html.encode(codec), "a.html")

        assert result.text == "한글 회의록입니다"
        assert result.metadata["encoding"] == encoding

    def test_replaced_bytes_are_reported(self):
        """Only HTML did not add the replacement-character warning (TXT and CSV do)."""
        html = "<html><body><p>" + "안녕하십니까 회의록입니다 " * 20 + "</p></body></html>"
        data = html.encode("cp949").replace("회의록".encode("cp949"), "회의록".encode("cp949") + b"\xc9\xa1", 1)

        result = convert(data, "a.html")

        assert "회의록입니다" in result.text
        assert any("were replaced with �" in warning for warning in result.warnings)


class TestHtmlInlineRendering:
    def test_line_break_inside_a_table_cell_is_kept(self):
        """After a `<br>` inside a cell was turned into a newline character, whitespace cleanup merged it into a space, giving
        `A-100 B-200`."""
        html = "<table><tr><th>모델</th><th>비고</th></tr><tr><td>A-100<br>B-200</td><td>첫 줄<br/>\n  둘째 줄</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "| 모델 | 비고 |\n| --- | --- |\n| A-100<br>B-200 | 첫 줄<br>둘째 줄 |"

    def test_heading_and_caption_use_the_same_inline_rendering_as_paragraphs(self):
        """Headings and table captions had a space inserted at every inline tag, giving `H _2 O` and `인사 발령`."""
        html = ("<h2>H<sub>2</sub>O 농도 10<sup>6</sup> 기준</h2><h3>인사<em>발령</em> <b>공지</b></h3>"
                "<table><caption>표 1. CO<sub>2</sub> <i>배출</i>량</caption><tr><th>연도</th><th>값</th></tr><tr><td>2024</td><td>1</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("## H_2O 농도 10^6 기준\n\n### 인사발령 공지\n\n표 1. CO_2 배출량\n\n"
                               "| 연도 | 값 |\n| --- | --- |\n| 2024 | 1 |")

    def test_heading_with_line_breaks_and_blocks_is_one_line(self):
        html = "<h1>첫 줄<br>둘째 줄<div>셋째</div></h1><h2><ul><li>목록</li><li>제목</li></ul></h2>"

        result = convert(html.encode(), "a.html")

        assert result.text == "# 첫 줄 둘째 줄 셋째\n\n## 목록 제목"

    def test_table_footer_written_before_the_body_goes_last(self):
        """HTML4 had `<tfoot>` written before `<tbody>`. Reading in document order put the total row before the data."""
        html = ("<table><thead><tr><th>항목</th><th>금액</th></tr></thead><tfoot><tr><td>합계</td><td>300</td></tr></tfoot>"
                "<tbody><tr><td>가</td><td>100</td></tr><tr><td>나</td><td>200</td></tr></tbody></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 항목 | 금액 |\n| --- | --- |\n| 가 | 100 |\n| 나 | 200 |\n| 합계 | 300 |"


class TestDeepGenericNesting:
    """The parser nests unclosed inline tags one level deeper on every line. Rendering used three recursion levels per DOM level,
    so a recursion limit error occurred at about 330 levels and the document was rejected as a 'corrupted document' (the `<font>`
    and `<center>` of old editors, an unclosed `<b>`)."""

    @pytest.mark.parametrize("depth", [331, 5_000])
    def test_unclosed_inline_tags_on_every_line_keep_every_line(self, depth):
        html = "".join(f"<font color=red>줄 {index}<br>" for index in range(depth))

        result = convert(html.encode(), "a.html")

        assert result.text.split("\n") == [f"줄 {index}" for index in range(depth)]

    @pytest.mark.parametrize(
        "html",
        [
            "<center>가운데 " * 5_000 + "가장 안쪽",
            "<div>" * 5_000 + "가장 안쪽" + "</div>" * 5_000,
            "<p>" + "<b>굵게 " * 5_000 + "가장 안쪽",
            "<blockquote>" * 5_000 + "가장 안쪽",
            "<del>" * 5_000 + "가장 안쪽",
            "<sup>" * 5_000 + "가장 안쪽",
        ],
        ids=["center", "div", "bold", "blockquote", "del", "sup"],
    )
    @pytest.mark.timing
    def test_deep_wrappers_are_read_quickly(self, html):
        import time

        started = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - started < 5
        assert "가장 안쪽" in result.text


class TestFormValues:
    """The values entered in a saved e-approval or web form live in tag attributes and inside form elements. input, select and
    textarea were removed whole, so the values vanished."""

    def test_filled_in_values_are_kept(self):
        html = ("<table><tr><th>성명</th><td><input type='text' value='홍길동'></td></tr>"
                "<tr><th>종류</th><td><select><option>연차</option><option selected>반차</option></select></td></tr>"
                "<tr><th>사유</th><td><textarea>개인 사정으로 인한 연차\n둘째 줄</textarea></td></tr></table>")

        result = convert(html.encode(), "a.html")

        # a name | value table whose name cells are th has no header row (test_core_html.py::TestNameValueTablesWithRowHeaders)
        assert result.text == ("| | |\n| --- | --- |\n| 성명 | 홍길동 |\n| 종류 | 반차 |\n| 사유 | 개인 사정으로 인한 연차<br>둘째 줄 |")

    def test_check_boxes_and_radio_buttons_use_the_same_marks_as_docx_form_fields(self):
        html = ("<p><input type=checkbox checked> 동의 <input type=checkbox> 거부</p>"
                "<p><input type=radio name=a> 오전<input type=radio name=a checked> 오후</p>")

        result = convert(html.encode(), "a.html")

        assert result.text == "☒ 동의 ☐ 거부\n\n☐ 오전 ☒ 오후"

    def test_select_without_a_choice_shows_the_first_option_and_multiple_shows_every_choice(self):
        html = ("<p>지역: <select><option>서울</option><option>부산</option></select></p>"
                "<p>과목: <select multiple><option selected>국어</option><option>영어</option><option selected>수학</option></select></p>"
                "<p>없음: <select multiple><option>가</option></select>끝</p>")

        result = convert(html.encode(), "a.html")

        assert result.text == "지역: 서울\n\n과목: 국어, 수학\n\n없음: 끝"

    def test_inputs_that_carry_no_content_are_dropped(self):
        html = ("<form><input type=hidden value=토큰><input type=password value=비밀><input type=submit value=전송>"
                "<input type=button value=단추><input type=reset value=초기화><input type=file value=파일>"
                "<input type=image value=그림 alt=그림단추><input type=text value='' placeholder='이름을 입력'>"
                "<input type=text placeholder=검색어><button>누르기</button><textarea placeholder=내용></textarea>"
                "<input value=기본종류><input type=NUMBER value=3></form>")

        result = convert(html.encode(), "a.html")

        assert result.text == "기본종류 3"

    def test_check_box_used_as_a_menu_toggle_is_not_a_form_value(self):
        """The check box (`role="button"`) that opens and closes a dropdown menu on every Wikipedia article came out as ☐ in the body."""
        html = "<p>본문<input type=checkbox role=button class=dropdown-checkbox> <input type=checkbox role=switch checked> 알림</p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "본문 ☒ 알림"

    def test_hidden_form_controls_are_dropped(self):
        html = "<p>보임<input type=text value=숨김 style='display:none'><input type=checkbox hidden></p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "보임"

    @pytest.mark.timing
    def test_thousands_of_form_controls_are_replaced_in_linear_time(self):
        """Replacing form elements with bs4 `replace_with` searches the parent's child list linearly for the position, which was
        quadratic in the number of siblings (16,000 controls in one form: 5.6 s). The elements are turned into text in place."""
        unit = ('<input type="text" value="값"><input type="checkbox" checked>'
                "<select><option>a</option><option selected>b</option></select><textarea>t</textarea>")
        html = f"<form>{unit * 4000}</form>"

        started = time.perf_counter()
        result = convert(html.encode(), "a.html")
        elapsed = time.perf_counter() - started

        assert result.text.count("값") == 4000 and result.text.count("☒") == 4000 and result.text.count(" b ") == 4000
        assert elapsed < 4.0


class TestHeaderRowsOfThOnlyTables:
    def test_table_made_only_of_th_cells_keeps_its_rows(self):
        """With no thead, every top row made only of th was counted as a header row, so a table whose cells are all th was merged
        into a single header row."""
        html = ("<table><tr><th>이름</th><th>홍길동</th></tr><tr><th>부서</th><th>영업</th></tr>"
                "<tr><th>직급</th><th>과장</th></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 이름 | 홍길동 |\n| --- | --- |\n| 부서 | 영업 |\n| 직급 | 과장 |"

    def test_two_header_rows_followed_by_data_rows_are_still_merged(self):
        html = ("<table><tr><th rowspan=2>구분</th><th colspan=2>요금</th></tr><tr><th>정가</th><th>할인가</th></tr>"
                "<tr><td>성인</td><td>100</td><td>80</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 구분 | 요금 정가 | 요금 할인가 |\n| --- | --- | --- |\n| 성인 | 100 | 80 |"


class TestNestedScriptsAndStrikethrough:
    def test_nested_superscript_marks_the_inner_one_first(self):
        """The text of the outer script was gathered whole, giving `e^{x2}`."""
        html = "<p>e<sup>x<sup>2</sup></sup>, a<sub>n<sub>k</sub></sub>, 10<sup>6</sup>, 21<sup>st</sup></p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "e^{x^2}, a_{n_k}, 10^6, 21st"

    def test_struck_through_text_is_marked(self):
        """Struck-through text came out as plain text, so `정가 10,000원 8,000원` read as two valid values."""
        html = ("<p>정가 <del>10,000원</del> 8,000원, <s> 품절 </s>재입고, <strike>구<b>판</b></strike>, 빈<del> </del>칸<s></s></p>"
                "<table><tr><th>품목</th><th>가격</th></tr><tr><td>가</td><td><del>100</del> 80</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("정가 ~~10,000원~~ 8,000원, ~~품절~~ 재입고, ~~구판~~, 빈 칸\n\n"
                               "| 품목 | 가격 |\n| --- | --- |\n| 가 | ~~100~~ 80 |")

    def test_strikethrough_inside_strikethrough_is_marked_once(self):
        html = "<p><del>바깥 <s>안쪽</s> 끝</del></p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "~~바깥 안쪽 끝~~"

    INNER = "<table><tr><th>대상</th><th>금액</th></tr><tr><td>청년</td><td>50</td></tr><tr><td>노인</td><td>20</td></tr></table>"

    def test_strikethrough_around_a_table_in_an_html_table_cell_closes_outside_the_inner_table(self):
        """When the outer table is an HTML table and the table inside a cell is kept as is, the closing `~~` attached to the last
        cell of the inner table ('20~~'), so the pair crossed the table boundary."""
        html = (f"<table><tr><th colspan=2>제목</th><th>비고</th></tr><tr><td>설명</td><td colspan=2><del>취소된 표 {self.INNER}</del>"
                "</td></tr><tr><td>가</td><td>b</td><td>c</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert ("<td colspan=\"2\">~~취소된 표 <table><tr><th>대상</th><th>금액</th></tr><tr><td>청년</td><td>50</td></tr>"
                "<tr><td>노인</td><td>20</td></tr></table>~~</td>") in result.text

    def test_strikethrough_around_a_table_flattened_into_a_markdown_cell_is_unchanged(self):
        html = (f"<table><tr><th>제목</th><th>비고</th></tr><tr><td>설명</td><td><del>취소된 표 {self.INNER}</del></td></tr>"
                "<tr><td>가</td><td>b</td></tr><tr><td>나</td><td>d</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "| 설명 | ~~취소된 표 대상 금액 청년 50 노인 20~~ |" in result.text

    @pytest.mark.parametrize("html", [
        "<table><tr><th>품목</th><th>가격</th></tr><tr style='text-decoration: line-through'><td>가</td><td>100</td></tr></table>",
        "<table><tr><th>품목</th><th>가격</th></tr><tr><td><s>가</s></td><td><s>100</s></td></tr></table>",
        "<del><table><tr><th>품목</th><th>가격</th></tr><tr><td><s>가</s></td><td>100</td></tr></table></del>",
    ])
    def test_strikethrough_over_several_cells_is_marked_in_each_cell(self, html):
        """When a strikethrough covered a whole row or table, the opening `~~` attached to the first cell and the closing `~~` to
        the last, crossing cell boundaries (`| ~~가 | 100~~ |`)."""
        result = convert(html.encode(), "a.html")

        assert "| ~~가~~ | ~~100~~ |" in result.text


def _mhtml(part_headers: str, body: bytes, *, preamble: str = "") -> bytes:
    head = (f"{preamble}MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=\"----=_NextPart_01\"\r\n\r\n"
            f"------=_NextPart_01\r\n{part_headers}\r\n\r\n")
    tail = ("\r\n------=_NextPart_01\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
            "Content-Location: file:///C:/image001.png\r\n\r\niVBORw0KGgo=\r\n------=_NextPart_01--\r\n")
    return head.encode() + body + tail.encode()


class TestMhtml:
    """Groupware and ERP systems serve a 'web page (single file)' (MHTML) as .xls, .doc or .mht. The MIME headers and base64 came out
    as text, and quoted-printable CP949 as `=C7=B0=C0=C7`, in a 'successful' result."""

    HTML = "<html><body><h1>품의서</h1><table><tr><th>기안자</th><td>홍길동</td></tr></table></body></html>"
    EXPECTED = "# 품의서\n\n| 기안자 | 홍길동 |\n| --- | --- |"

    def test_base64_html_part_is_converted_as_html(self):
        import base64

        body = base64.encodebytes(self.HTML.encode()).replace(b"\n", b"\r\n")
        data = _mhtml('Content-Type: text/html; charset="utf-8"\r\nContent-Transfer-Encoding: base64', body)

        result = convert(data, "결재문서.xls")

        assert result.format is DocumentFormat.HTML
        assert result.text == self.EXPECTED

    def test_quoted_printable_part_uses_the_charset_of_the_part(self):
        import quopri

        body = quopri.encodestring(self.HTML.encode("cp949"))
        data = _mhtml('Content-Type: text/html; charset="ks_c_5601-1987"\r\nContent-Transfer-Encoding: quoted-printable', body)

        result = convert(data, "품의서.doc")

        assert result.text == self.EXPECTED
        assert result.metadata["encoding"] == "cp949"

    def test_charset_of_the_part_wins_over_bytes_that_also_decode_as_korean(self):
        import quopri

        body = quopri.encodestring("<html><body><p>日本語の文書です</p></body></html>".encode("shift_jis"))
        data = _mhtml("Content-Type: text/html; charset=shift_jis\r\nContent-Transfer-Encoding: quoted-printable", body)

        result = convert(data, "a.mht")

        assert result.text == "日本語の文書です"

    def test_page_saved_by_a_browser_is_converted(self):
        """An .mhtml saved by a browser starts with `From:` and `Subject:` headers."""
        data = _mhtml("Content-Type: text/html\r\nContent-Transfer-Encoding: 8bit", self.HTML.encode(),
                      preamble="From: <Saved by Blink>\r\nSnapshot-Content-Location: https://example.com/\r\nSubject: =?utf-8?Q?a?=\r\n")

        result = convert(data, None)

        assert result.text == self.EXPECTED

    def test_picture_saved_with_the_page_is_extracted(self):
        """The picture parts saved with the page (`cid:`, Content-Location) were not extracted, so only the alt text of MHTML pictures
        remained. External addresses are not fetched."""
        import base64

        from tests import builder

        picture, chart = (base64.encodebytes(builder.png_bytes(seed=seed)).decode().replace("\n", "\r\n") for seed in (1, 2))
        html = ('<html><body><p>본문</p><img src="cid:image001@01D0"><img src="images/chart.png" alt="차트">'
                '<img src="https://example.com/b.png" alt="외부"></body></html>')
        data = (
            'MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="B"\r\n\r\n'
            "--B\r\nContent-Type: text/html; charset=\"utf-8\"\r\nContent-Location: https://example.com/page/index.html\r\n\r\n"
            f"{html}\r\n"
            f"--B\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\nContent-ID: <image001@01D0>\r\n\r\n{picture}"
            "--B\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
            f"Content-Location: https://example.com/page/images/chart.png\r\n\r\n{chart}"
            "--B--\r\n"
        ).encode()

        result = convert(data, "page.mht")

        assert len(result.images) == 2
        assert [image.alt for image in result.images] == [None, "차트"]
        assert "[Image: 외부]" in result.text


class TestCollapsedQuestionsAndAnswers:
    """Saved FAQ and accordion pages fold the answer away with an inline `display:none` and put the question in a `<button>`. Hidden
    elements and buttons were removed whole, so the questions and answers vanished. Only expander buttons and their targets are
    kept, narrowly."""

    def test_answer_folded_under_its_question_and_accordion_button_are_kept(self):
        html = ('<h1>자주 묻는 질문</h1><ul class="faq">'
                '<li><a href="#" class="q">Q. 환불은 언제 되나요?</a>'
                '<div class="a" style="display: none;">A. 영업일 기준 3일 이내 환불됩니다.</div></li></ul>'
                '<div class="accordion-item"><h2><button class="accordion-button collapsed">교환은 어떻게 하나요?</button></h2>'
                '<div class="accordion-collapse collapse"><div class="accordion-body">고객센터로 접수해 주세요.</div></div></div>')

        result = convert(html.encode(), "faq.html")

        assert result.text == ("# 자주 묻는 질문\n\n- Q. 환불은 언제 되나요?\n\nA. 영업일 기준 3일 이내 환불됩니다.\n\n"
                               "## 교환은 어떻게 하나요?\n\n고객센터로 접수해 주세요.")

    def test_button_that_expands_something_is_kept_but_other_buttons_are_dropped(self):
        html = ('<div><button aria-expanded="false" aria-controls="a1">배송은 얼마나 걸리나요?</button>'
                '<div id="a1" style="display:none">2~3일 걸립니다.</div></div>'
                "<p>본문</p><button>복사</button><button type=submit>검색</button>")

        result = convert(html.encode(), "faq.html")

        assert result.text == "배송은 얼마나 걸리나요?\n\n2~3일 걸립니다.\n\n본문"

    def test_definition_folded_under_its_term_is_kept(self):
        html = '<dl><dt>Q1</dt><dd style="display:none">답 하나</dd><dt><a href="javascript:;">Q2</a></dt><dd style="DISPLAY: none">답 둘</dd></dl>'

        result = convert(html.encode(), "faq.html")

        assert result.text == "Q1\n\n답 하나\n\nQ2\n\n답 둘"

    @pytest.mark.parametrize(
        "hidden",
        [
            '<li><a href="#">회사 소개</a><ul style="display:none"><li><a href="/a">연혁</a></li></ul></li>',
            '<li><a href="#">상품</a><div style="display:none"><a href="/a">노트북</a> <a href="/b">모니터</a></div></li>',
            '<a href="#">약관 보기</a><div class="modal" style="display:none">숨긴 모달 창의 글</div>',
            '<a href="#">도움말</a><div role="tooltip" style="display:none">숨긴 풍선 도움말</div>',
            '<a href="/page">다른 쪽으로 가는 링크</a><div style="display:none">숨긴 글</div>',
            '<p>문단</p><div style="display:none">숨긴 글</div>',
            '<a href="#">열기</a><span style="display:none">숨긴 인라인 글</span>',
            '<a href="#">열기</a><div hidden>숨긴 글</div>',
            '<a href="#">열기</a><div style="visibility:hidden">숨긴 글</div>',
            '<div style="display:none"><a href="#">열기</a><div style="display:none">숨긴 글 안의 숨긴 글</div></div>',
        ],
        ids=["dropdown-menu", "link-panel", "modal", "tooltip", "real-link", "no-opener", "inline", "hidden-attribute",
             "visibility", "inside-hidden"],
    )
    def test_other_hidden_elements_are_still_removed(self, hidden):
        html = f"<p>본문</p><ul>{hidden}</ul>" if hidden.startswith("<li>") else f"<p>본문</p>{hidden}"

        result = convert(html.encode(), "a.html")

        assert "숨긴" not in result.text
        assert "연혁" not in result.text
        assert "노트북" not in result.text


@pytest.mark.parametrize(("attribute", "value"), [("colspan", "²"), ("rowspan", "⑤"), ("colspan", "½")],
                         ids=["superscript", "circled", "fraction"])
def test_span_attribute_that_is_not_a_plain_number_does_not_reject_the_document(attribute, value):
    """For `colspan="²"`, `isdigit()` is true but `int()` fails, so the whole document was rejected as a 'corrupted document' (the
    `isdecimal()` rule of docs/decisions/extractors.md). A span count that is not a number is 1."""
    html = f'<table><tr><td {attribute}="{value}">a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>'

    result = convert(html.encode(), "a.html")

    assert result.text == "| a | b |\n| --- | --- |\n| c | d |"


def test_html_rowspan_longer_than_the_table_does_not_add_empty_rows():
    """When `rowspan=99` exceeded the number of rows left, 97 ghost rows of `| 식비 | | |` came out. It is cut at the end of the
    table, as a browser does."""
    html = ("<table><tr><th>분류</th><th>항목</th><th>금액</th></tr>"
            "<tr><td rowspan=99>식비</td><td>점심</td><td>100</td></tr><tr><td>저녁</td><td>200</td></tr></table>")

    result = convert(html.encode(), "a.html")

    assert result.text == "| 분류 | 항목 | 금액 |\n| --- | --- | --- |\n| 식비 | 점심 | 100 |\n| 식비 | 저녁 | 200 |"


class TestOrderedListNumbers:
    """`<li value>`, `<ol type>` and `<ol reversed>` were ignored, so the numbers differed from the original (3위, 2위, 1위 — 3rd,
    2nd, 1st place — became 1, 2, 3)."""

    def test_item_value_restarts_the_numbering(self):
        html = '<ol start="3"><li>셋</li><li value="10">열</li><li>열하나</li><li value="x">열둘</li></ol>'

        result = convert(html.encode(), "a.html")

        assert result.text == "3. 셋\n10. 열\n11. 열하나\n12. 열둘"

    def test_reversed_list_counts_down(self):
        html = "<ol reversed><li>3위</li><li>2위</li><li>1위</li></ol><ol reversed start=10><li>십</li><li>구</li></ol>"

        result = convert(html.encode(), "a.html")

        assert result.text == "3. 3위\n2. 2위\n1. 1위\n\n10. 십\n9. 구"

    def test_letter_and_roman_numbering_is_written_after_a_bullet(self):
        """Markdown numbered lists use digits only. Letter numbers are written after a bullet so that the indentation (nesting) is
        kept."""
        html = ('<ol type="a"><li>가</li><li>나<ol type="I" start="4"><li>안</li></ol></li></ol>'
                '<ol type="A"><li>다</li></ol><ol type="i"><li>라</li><li>마</li></ol><ol type="1"><li>바</li></ol>')

        result = convert(html.encode(), "a.html")

        assert result.text == "- a. 가\n- b. 나\n  - IV. 안\n\n- A. 다\n\n- i. 라\n- ii. 마\n\n1. 바"


def test_html_css_line_through_is_marked_like_the_strike_tags():
    html = ('<p>정가 <span style="text-decoration: line-through">10,000원</span> 8,000원, '
            '<span style="color:red; TEXT-DECORATION-LINE: underline line-through;">품절</span>, '
            '<del><span style="text-decoration:line-through">겹침</span></del>, '
            '<span style="text-decoration: underline">밑줄</span></p>')

    result = convert(html.encode(), "a.html")

    assert result.text == "정가 ~~10,000원~~ 8,000원, ~~품절~~, ~~겹침~~, 밑줄"


class TestAlmostUtf8:
    """Short UTF-8 text with its last character cut off was read by statistical detection as an unrelated encoding such as ptcp154
    and garbled as a whole."""

    @pytest.mark.parametrize(
        "data",
        [
            "회의록 요약: 다음 주 화요일".encode()[:-1],
            "회의록 요약: 다음 주 화요일".encode()[:-2],
            "안녕하세요".encode()[:-1],
            ("회의록 요약입니다. " * 30).encode() + b"\xff" + "다음 주 화요일".encode(),
        ],
        ids=["cut-1-byte", "cut-2-bytes", "very-short", "one-bad-byte-in-the-middle"],
    )
    def test_is_read_as_utf8_with_a_warning(self, data):
        result = convert(data, "a.txt")

        assert result.text.startswith(("회의록 요약", "안녕하세"))
        assert result.metadata["encoding"] == "utf-8(replaced)"
        assert result.warnings

    @pytest.mark.parametrize(
        ("text", "encoding", "expected"),
        [
            ("회의", "cp949", "cp949"),
            ("회의록 요약: 다음 주 화요일", "cp949", "cp949"),
            ("abc 西", "cp949", "cp949"),
            ("日本語の文書です。確認してください。", "shift_jis", None),
            ("naïve café", "latin-1", "cp1252"),
        ],
        ids=["two-hangul", "short-korean", "ascii-then-one-hanja", "japanese", "western"],
    )
    def test_legacy_encodings_are_not_mistaken_for_cut_utf8(self, text, encoding, expected):
        decoded, name = decode_text(text.encode(encoding))

        assert not name.startswith("utf-8")
        if expected is not None:
            assert (decoded, name) == (text, expected)


class TestMalformedNesting:
    """The parser nests unclosed tags and tag typos one level deeper each time, so input the size of a normal document took tens
    of seconds or was rejected as a 'corrupted document'."""

    @pytest.mark.timing
    def test_unclosed_nested_blockquotes_stay_small_and_fast(self):
        """Unclosed `<blockquote>`s prefixed `> ` again to all the inner text at every level, so 4,500 levels (58KB) took 48 s and
        2.9 GB. Quote marks are stacked only up to 20 levels."""
        import time

        html = "".join(f"<blockquote>인용 {index}" for index in range(4_500))

        started = time.perf_counter()
        result = convert(html.encode(), "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5
        assert "인용 0" in result.text
        assert "인용 4499" in result.text
        assert max(line.count(">") for line in result.text.split("\n")) == 20

    def test_nested_quotes_within_the_limit_keep_every_level(self):
        html = "<blockquote>바깥<blockquote>안쪽</blockquote></blockquote>"

        result = convert(html.encode(), "a.html")

        assert result.text == "> 바깥\n>\n> > 안쪽"

    def test_heading_tags_left_open_by_a_typo_become_separate_headings(self):
        """With the typo `<h3>` for `</h3>` made 400 times, headings nested 400 deep and the whole document was rejected with a
        recursion limit error. As a browser does, a heading inside a heading is moved out to after the outer heading."""
        html = "<html><body>" + "".join(f"<h3>{index}. 항목 제목<h3>\n본문 내용 {index}<br>\n" for index in range(400))

        result = convert(html.encode(), "a.html", ConvertOptions(max_chars=None))

        assert result.text.startswith("### 0. 항목 제목\n\n### 본문 내용 0\n\n### 1. 항목 제목\n\n")
        assert "### 399. 항목 제목\n\n### 본문 내용 399" in result.text

    def test_heading_inside_a_heading_follows_it(self):
        result = convert("<h2>개요<h3>세부</h3></h2><p>본문</p>".encode(), "a.html")

        assert result.text == "## 개요\n\n### 세부\n\n본문"

    @pytest.mark.timing
    def test_many_heading_tags_left_open_take_linear_time(self):
        """Each time a heading inside a heading was moved out, bs4 walked the whole remaining chain under the heading being moved, so
        156KB of repeated `<h3>제목` took 9.9 s (quadratic)."""
        import time

        def seconds(count: int) -> float:
            data = ("<html><body>" + "<h3>제목" * count + "</body></html>").encode()
            started = time.perf_counter()
            convert(data, "a.html", ConvertOptions(max_chars=None))
            return time.perf_counter() - started

        seconds(500)

        small, large = seconds(2_000), seconds(8_000)

        assert large < 8 * small + 0.5


class TestContentOutsideTableCells:
    """Text placed inside a table but outside any cell (`</tr>소계: 300<tr>`, `<table>안내</table>`), and the next table that ended
    up inside a table (outside any cell) because `</table>` was left out, vanished without a warning. As a browser does, text
    outside the cells goes before the table and a table outside the cells goes after it."""

    def test_text_between_rows_comes_before_the_table(self):
        html = "<table><tr><td>가</td><td>1</td></tr>소계: 300<tr><td>나</td><td>2</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "소계: 300\n\n| 가 | 1 |\n| --- | --- |\n| 나 | 2 |"

    def test_inline_element_between_rows_comes_before_the_table(self):
        html = "<table><tr><td>가</td><td>1</td></tr><b>소계: 300</b><tr><td>나</td><td>2</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("소계: 300\n\n| 가 | 1 |")

    def test_table_with_only_text_keeps_the_text(self):
        result = convert("<table>안내 문구</table>".encode(), "a.html")

        assert result.text == "안내 문구"

    def test_table_left_open_before_the_next_table_keeps_both(self):
        html = ("<table><tr><td>가</td><td>1</td></tr><table><tr><td>나</td><td>2</td></tr><tr><td>다</td><td>3</td></tr>"
                "</table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 가 | 1 |\n| --- | --- |\n\n| 나 | 2 |\n| --- | --- |\n| 다 | 3 |"

    def test_whitespace_and_hidden_inputs_between_rows_add_nothing(self):
        html = ("<table>\n  <input type=hidden name=id value=7>\n<tr><td>가</td><td>1</td></tr>\n&nbsp;\n"
                "<tr><td>나</td><td>2</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 가 | 1 |\n| --- | --- |\n| 나 | 2 |"

    def test_rows_wrapped_in_a_form_stay_in_the_table(self):
        html = "<table><form action=x><tr><td>이름</td><td>값</td></tr><tr><td>홍</td><td>1</td></tr></form></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "| 이름 | 값 |\n| --- | --- |\n| 홍 | 1 |"

    @pytest.mark.timing
    def test_deeply_chained_open_tables_are_read_quickly(self):
        import time

        html = "<table><tr><td>칸</td><td>값</td></tr>" * 5_000 + "가장 안쪽"

        started = time.perf_counter()
        result = convert(html.encode(), "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 12
        assert "가장 안쪽" in result.text
        assert result.text.count("| 칸 | 값 |") == 5_000


class TestShortDamagedLegacyKorean:
    """Short CP949 text with one byte cut off failed the '10 Hangul characters per replacement character' condition, fell through
    to statistical detection and was garbled as a whole into cp874/utf-16 characters, without a warning."""

    @pytest.mark.parametrize("text", ["결재 완료", "확인", "회의록 요약: 다음 주 화요일"])
    def test_short_korean_with_a_cut_last_byte_is_read_as_korean(self, text):
        data = text.encode("cp949") + b"\xb0"

        decoded, encoding = decode_text(data)

        assert decoded == f"{text}�"
        assert encoding == "cp949(replaced)"

    @pytest.mark.parametrize(
        ("text", "encoding"),
        [("Приветствую", "cp1251"), ("Ελληνικά", "cp1253"), ("Grüße aus Köln", "cp1252"), ("日本語です", "shift_jis")],
        ids=["russian", "greek", "german", "japanese"],
    )
    def test_other_encodings_with_an_odd_last_byte_are_not_read_as_korean(self, text, encoding):
        decoded, name = decode_text(text.encode(encoding) + b"\xb0")

        assert not name.startswith("cp949")


class TestCodeBlockWithFenceInside:
    """When a `<pre>` contained a ``` line (code showing a Markdown example), that line closed the code block, so the code after it
    became text outside the block, and the neutralizing of unpaired fences broke the outer closing fence."""

    @pytest.mark.parametrize("inner", ["```\nb   c", "```python\nb   c\n```", "  ```\nb   c"],
                             ids=["unpaired", "paired", "indented"])
    def test_fence_lines_inside_pre_stay_inside_the_code_block(self, inner):
        html = f"<pre>a\n{inner}</pre><p>뒤 문단</p>"

        result = convert(html.encode(), "a.html")

        lines = result.text.split("\n")
        fences = [line for line in lines if line.lstrip().startswith("```")]
        assert fences == ["```", "```"]
        assert "b   c" in lines
        assert result.text.endswith("```\n\n뒤 문단")


def _mail(*parts: tuple[str, str, str, str | None], subtype: str = "alternative", attachment: bool = False) -> bytes:
    """A MIME mail holding transfer-encoded body parts (`(subtype, text, charset, encoding)`; 7bit when the encoding is None). With
    `attachment` it is wrapped in multipart/mixed and an Excel attachment is added. The subject is encoded per RFC 2047 (the
    shape mail programs write)."""
    from email.header import Header
    from email.mime.application import MIMEApplication
    from email.mime.multipart import MIMEMultipart
    from email.mime.nonmultipart import MIMENonMultipart

    body = MIMEMultipart(subtype)
    for kind, text, charset, encoding in parts:
        part = MIMENonMultipart("text", kind, charset=charset)
        payload = text.encode(charset)
        if encoding == "base64":
            import base64

            part.set_payload(base64.encodebytes(payload).decode("ascii"))
        elif encoding == "quoted-printable":
            import quopri

            part.set_payload(quopri.encodestring(payload).decode("ascii"))
        else:
            part.set_payload(text)
        part["Content-Transfer-Encoding"] = encoding or "7bit"
        body.attach(part)
    message = body
    if attachment:
        message = MIMEMultipart("mixed")
        message.attach(body)
        sheet = MIMEApplication(b"PK\x03\x04fake", "octet-stream")
        sheet.add_header("Content-Disposition", "attachment", filename="estimate.xlsx")
        message.attach(sheet)
    message["Subject"] = Header("견적서 송부 (3월)", "euc-kr").encode()
    message["From"] = "kim@example.com"
    message["To"] = "lee@example.com"
    return message.as_bytes()


class TestEncodedMail:
    """Mail whose body parts are base64 or quoted-printable (.eml, and .mht of the same structure) was detected as plain-text mail,
    so the MIME headers and base64 came out as they were in a 'successful' result. If there is an HTML part it is read, as with
    MHTML; if there are only plain-text parts they are decoded and read as text; the subject, sender and so on are decoded and
    placed at the very front."""

    HTML = "<html><body><p>안녕하세요.</p><table><tr><th>품목</th><th>단가</th></tr><tr><td>볼트</td><td>12,500</td></tr></table></body></html>"
    PLAIN = "안녕하세요. 단가는 12,500원입니다."

    @pytest.mark.parametrize("filename", ["mail.eml", "mail.mht", None])
    def test_html_part_of_an_encoded_mail_is_read_as_html(self, filename):
        data = _mail(("plain", self.PLAIN, "euc-kr", "base64"), ("html", self.HTML, "euc-kr", "base64"), attachment=True)

        result = convert(data, filename)

        assert result.format is DocumentFormat.HTML
        assert result.text == ("Subject: 견적서 송부 (3월)\nFrom: kim@example.com\nTo: lee@example.com\n\n안녕하세요.\n\n"
                               "| 품목 | 단가 |\n| --- | --- |\n| 볼트 | 12,500 |")
        assert "1 mail attachment(s) (estimate.xlsx) were not read. Convert attachments separately." in result.warnings

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    @pytest.mark.parametrize("filename", ["mail.eml", "mail.txt"])
    def test_plain_parts_of_an_encoded_mail_are_decoded(self, encoding, filename):
        data = _mail(("plain", self.PLAIN, "utf-8", encoding), subtype="mixed")

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == f"Subject: 견적서 송부 (3월)\nFrom: kim@example.com\nTo: lee@example.com\n\n{self.PLAIN}"
        assert not result.warnings

    def test_mail_whose_parts_are_not_transfer_encoded_is_read_the_same_way(self):
        """Mail with only 7bit parts is also read with its headers decoded and without the MIME boundaries and part headers
        (previously it was text including the headers — test_core_mail_and_detect.py::TestMailWithoutTransferEncoding)."""
        data = _mail(("plain", "meeting notes", "us-ascii", None), subtype="mixed")

        result = convert(data, "mail.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: 견적서 송부 (3월)\nFrom: kim@example.com\nTo: lee@example.com\n\nmeeting notes"


class TestTitleRowInsideMultiRowHeader:
    """When the first row of the thead (or of the top th rows) is a title spanning the whole table width
    ('2024년 지점별 실적 (단위: 백만원)'), merging the multi-row header prefixed that title to every column name. The title is
    lifted out as a paragraph above the table (the same test as `markdown._title_of`), and a real parent header is still merged."""

    @pytest.mark.parametrize("head", [
        '<thead><tr><th colspan="4">2024년 지점별 실적 (단위: 백만원)</th></tr>'
        "<tr><th>지점</th><th>매출</th><th>비용</th><th>이익</th></tr></thead>",
        '<tr><th colspan="4">2024년 지점별 실적 (단위: 백만원)</th></tr><tr><th>지점</th><th>매출</th><th>비용</th><th>이익</th></tr>',
    ], ids=["thead", "th-only-rows"])
    def test_full_width_title_row_becomes_a_paragraph_above_the_table(self, head):
        html = f"<table>{head}<tr><td>서울</td><td>100</td><td>60</td><td>40</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == ("2024년 지점별 실적 (단위: 백만원)\n\n| 지점 | 매출 | 비용 | 이익 |\n| --- | --- | --- | --- |\n"
                               "| 서울 | 100 | 60 | 40 |")

    def test_title_above_a_grouped_header_is_lifted_and_the_group_is_still_merged(self):
        html = ('<table><thead><tr><th colspan="3">요금표</th></tr><tr><th rowspan="2">구분</th><th colspan="2">요금</th></tr>'
                "<tr><th>정가</th><th>할인가</th></tr></thead><tr><td>성인</td><td>100</td><td>80</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "요금표\n\n| 구분 | 요금 정가 | 요금 할인가 |\n| --- | --- | --- |\n| 성인 | 100 | 80 |"

    def test_two_column_table_with_a_spanning_title_row(self):
        html = ('<table><tr><th colspan="2">부서별 인원 현황</th></tr><tr><th>부서</th><th>인원</th></tr>'
                "<tr><td>영업</td><td>10</td></tr><tr><td>개발</td><td>20</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "부서별 인원 현황\n\n| 부서 | 인원 |\n| --- | --- |\n| 영업 | 10 |\n| 개발 | 20 |"


class TestIndentedLabelsInTables:
    """In a table that shows sub-accounts by indentation (`&nbsp;`, padding), like a financial statement, the indentation was lost
    and '매출원가' and its sub-account '재료비' read as the same level. The same mark as list levels inside a table cell
    (`└ `, `_lists.cell_level_mark`) is added."""

    def test_label_indented_with_non_breaking_spaces_is_marked(self):
        html = ("<table><tr><td>과목</td><td>금액</td></tr><tr><td>매출원가</td><td>600</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;재료비</td><td>400</td></tr>"
                "<tr><td>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;원재료</td><td>300</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("| 과목 | 금액 |\n| --- | --- |\n| 매출원가 | 600 |\n| └ 재료비 | 400 |\n"
                               "| └─ 원재료 | 300 |")

    def test_label_indented_with_padding_is_marked_relative_to_the_other_labels(self):
        html = ('<table><tr><th>과목</th><th>금액</th></tr><tr><td style="padding-left:4px">매출액</td><td>1,000</td></tr>'
                '<tr><td style="padding-left: 20px">상품매출</td><td>900</td></tr></table>')

        result = convert(html.encode(), "a.html")

        assert "| 매출액 | 1,000 |\n| └ 상품매출 | 900 |" in result.text

    def test_every_label_starting_with_the_same_spaces_is_not_marked(self):
        html = ("<table><tr><td>지점</td><td>매출</td></tr><tr><td>&nbsp;서울</td><td>100</td></tr>"
                "<tr><td>&nbsp;부산</td><td>80</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 지점 | 매출 |\n| --- | --- |\n| 서울 | 100 |\n| 부산 | 80 |"


class TestDefinitionLists:
    """The terms and definitions of a `<dl>` became separate paragraphs, blurring the pairing of '시행일' and '2024-07-01'. A short
    definition goes on one line as `term: definition`."""

    def test_short_definitions_are_joined_to_their_terms(self):
        html = "<dl><dt>시행일</dt><dd>2024-07-01</dd><dt>대상</dt><dd>전 직원</dd><dd>파견 직원</dd></dl><p>끝</p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "시행일: 2024-07-01\n\n대상: 전 직원; 파견 직원\n\n끝"

    def test_definition_with_blocks_keeps_the_term_on_its_own_line(self):
        html = "<dl><dt>절차</dt><dd><p>신청서를 낸다.</p><ul><li>온라인</li><li>방문</li></ul></dd></dl>"

        result = convert(html.encode(), "a.html")

        assert result.text == "절차\n\n신청서를 낸다.\n\n- 온라인\n- 방문"

    def test_deeply_nested_definition_lists_do_not_hit_the_recursion_limit(self):
        html = "<dl><dt>a</dt><dd>" * 3000 + "끝" + "</dd></dl>" * 3000

        result = convert(html.encode(), "a.html")

        assert result.text.endswith("끝")


class TestWesternHtmlWithoutAUsefulCharset:
    """Short Windows-1252 HTML was read as CP775 by statistical detection because of the markup characters, so `10€ café` became
    `10Ć cafķ` (a .txt of the same text is read correctly). It is judged by the text without the markup."""

    @pytest.mark.parametrize("head", ["", '<head><meta charset="euc-kr"></head>'], ids=["no-charset", "wrong-charset"])
    def test_short_cp1252_html_reads_like_the_same_text_file(self, head):
        data = f"<html>{head}<body><p>Price 10€ café — naïve</p></body></html>".encode("cp1252")

        result = convert(data, "a.html")

        assert result.text == "Price 10€ café — naïve"
