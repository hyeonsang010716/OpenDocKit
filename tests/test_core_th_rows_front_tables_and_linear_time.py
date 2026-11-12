"""Several rows made only of th in HTML, the verdict on a table cut off in the front part of a large HTML, the processing time of
replacing elements with many siblings and of joining Google Docs lists, EUC-KR 8-byte filler-composed characters."""

import time

import pytest

from opendockit import ConvertOptions, convert


class TestThRowsAboveADataRow:
    """The th-only rows at the top were counted as header rows with no limit on their number, so when there was a single td row
    below, all the rows above were merged into one header line and a repeated value in the same column (김철수's 90) was erased."""

    def test_th_rows_without_spans_above_a_td_row_stay_rows(self):
        html = ("<table><tr><th>이름<th>점수<tr><th>홍길동<th>90<tr><th>김철수<th>90<tr><th>이영희<th>85"
                "<tr><td>합계<td>265</table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("| 이름 | 점수 |\n| --- | --- |\n| 홍길동 | 90 |\n| 김철수 | 90 |\n| 이영희 | 85 |\n"
                               "| 합계 | 265 |")

    def test_header_rows_tied_by_a_column_span_are_still_merged(self):
        html = ("<table><tr><th>구분<th colspan=2>요금<tr><th>대상<th>정가<th>할인가"
                "<tr><th>성인<th>100<th>80<tr><td>합계<td>100<td>80</table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("| 구분 대상 | 요금 정가 | 요금 할인가 |\n| --- | --- | --- |\n| 성인 | 100 | 80 |\n"
                               "| 합계 | 100 | 80 |")


def _nested_rows_page(rows: int, pad: int) -> bytes:
    inner = "<table>" + "".join("<tr>" + f"<td class={'p' * pad}>x" * 10 for _ in range(10)) + "</table>"
    body = "".join(f"<tr><td>가<td>{inner}" for _ in range(rows))
    return f"<html><body><table>{body}<tr><td><table><tr><td>a<tr><td>b</table><td>x</table></body></html>".encode()


class TestTableCutAtTheEndOfTheFront:
    """In the front-part verdict of a large HTML (`html._extract_front`), a table cut off at the end of the front part had its
    number of header rows and whether it is a layout table decided from the cut rows alone, which blocked a document within the
    limit as 'exceeded' with text longer than the full conversion."""

    def test_th_rows_cut_before_the_td_row_do_not_exceed_a_limit_the_whole_table_meets(self):
        rows = "".join(f"<tr><th class={'p' * 28}>항목<th class={'p' * 28}>값" for _ in range(3000))
        data = f"<html><body><table>{rows}<tr><td>합계<td>1</table></body></html>".encode()
        whole = convert(data, "a.html", ConvertOptions(max_chars=None))

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count))

        assert not result.exceeded
        assert result.text == whole.text

    @pytest.mark.parametrize("extra", [0, 588])
    def test_layout_table_cut_in_the_front_does_not_exceed_a_limit_the_whole_table_meets(self, extra):
        data = _nested_rows_page(60, 10)
        whole = convert(data, "a.html", ConvertOptions(max_chars=None))

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count + extra))

        assert not result.exceeded
        assert result.text == whole.text

    def test_large_data_table_over_the_limit_still_stops_at_the_front(self):
        rows = "".join(f"<tr><td>{index}<td>{'값' * 20}" for index in range(20_000))
        data = f"<html><body><table><tr><th>번호<th>내용{rows}</table></body></html>".encode()

        result = convert(data, "a.html", ConvertOptions(max_chars=5_000))

        assert result.exceeded
        assert result.text.startswith("| 번호 | 내용 |")


class TestManySiblingsTakeLinearTime:
    """bs4's `replace_with` looks for its own position in the parent's child list from the start, so many superscripts, pictures or
    svg under one parent took the square of their number (40,000 superscripts 27.6 s, 40,000 pictures 14.9 s). Each time a Google
    Docs list piece was joined, the items of the growing earlier list were counted again, so 40,000 pieces took 79 seconds."""

    @pytest.mark.parametrize("piece", ["1<sup>2</sup> ", "<img alt=그림 src=a.png> ", "<svg><text>이름표</text></svg> "])
    @pytest.mark.timing
    def test_many_replaced_siblings_under_one_parent(self, piece):
        data = f"<html><body><p>{piece * 40_000}</p></body></html>".encode()
        started = time.perf_counter()

        result = convert(data, "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 8.0
        assert result.char_count > 40_000

    @pytest.mark.timing
    def test_many_google_docs_list_pieces(self):
        data = ("<html><body>" + "<ol class='lst-kix_abc-1'><li>항목</li></ol>" * 40_000 + "</body></html>").encode()
        started = time.perf_counter()

        result = convert(data, "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 8.0
        assert result.text.splitlines()[-1] == "40000. 항목"


class TestEucKrFillerSyllables:
    """The 8-byte filler composition of KS X 1001 (a filler character + initial, medial and final jamo) is the way to write a
    syllable missing from the precomposed set ('똠'), but it was decoded as four jamo, so '똠방각하' became 'ㅤㄸㅗㅁ방각하'."""

    def test_eight_byte_filler_sequence_becomes_one_syllable(self):
        data = "회의록: ".encode("cp949") + bytes.fromhex("A4D4A4A8A4C7A4B1") + "방각하 참석".encode("cp949")

        result = convert(data, "a.txt")

        assert result.text == "회의록: 똠방각하 참석"

    def test_filler_sequence_without_a_final_consonant(self):
        data = "가 ".encode("cp949") + bytes.fromhex("A4D4A4A1A4C1A4D4") + " 나".encode("cp949")

        result = convert(data, "a.txt")

        assert result.text == "가 갸 나"
