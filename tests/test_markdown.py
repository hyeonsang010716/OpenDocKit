"""Shared normalization and table rendering."""

import unicodedata

import pytest

from opendockit.markdown import TableGrid, clean_text, join_blocks, render_table, solid_length


class TestCleanText:
    def test_normalizes_decomposed_hangul_to_nfc(self):
        """Decomposed jamo from macOS or HWP are a different string for search and embedding even though the characters are the same."""
        decomposed = unicodedata.normalize("NFD", "한글")

        result = clean_text(decomposed)

        assert result == "한글"
        assert len(result) == 2

    def test_result_is_nfc_even_when_removed_character_separated_combining_mark(self):
        """Removing invisible characters makes their neighbours newly combinable. Normalizing first gave a result that was not NFC
        (found in a real PDF)."""
        text = "e\u200b\u0301"

        result = clean_text(text)

        assert result == "é"
        assert unicodedata.is_normalized("NFC", result)

    def test_check_box_glyphs_of_symbol_fonts_survive_without_a_font(self):
        """Where the font is unknown (PDF, titles), the Wingdings check boxes ☒ (FD) and ✗ (FB) were removed, leaving only □, so
        the chosen item read the other way round. Codes whose meaning differs by font (78 is Wingdings ☒ but Symbol ξ, 6F is □ but ο)
        are not put in the shared mapping — the extractor converts them by font."""
        text = " 참석  불참  반려  승인 "

        result = clean_text(text)

        assert result == "☒ 참석 □ 불참 ✗ 반려 ☑ 승인"

    def test_expands_latin_ligatures_but_keeps_other_compatibility_characters(self):
        """Typeset PDFs hold fi and fl as one ligature character (U+FB01 etc.), so 'ﬁrst' became a different word from 'first'
        (964 places in a real PDF). NFKC would also change ①, ㈜ and ² in Korean documents, so it is not used; only ligatures are
        expanded."""
        text = "ﬁrst ﬂip oﬀer eﬃcient baﬄe ① ㈜ m² ｱ"

        result = clean_text(text)

        assert result == "first flip offer efficient baffle ① ㈜ m² ｱ"
        assert solid_length(text) == len("".join(result.split()))

    def test_removes_supplementary_private_use_characters(self):
        """Hancom font symbols are also in the supplementary private use areas (U+F0000~). Only the BMP area was removed, leaving
        meaningless characters in the result (real HWPX: `\U000f02b1 대 설`). The placeholders of a table inside a table
        (U+E020, U+E021) are used as they are."""
        text = "\U000f02b1 대 설 \U000f0854이수화학\U000f0855 \U0010fffd끝 \U0001f600"

        result = clean_text(text)

        assert result == "대 설 이수화학 끝 \U0001f600"
        assert solid_length(text) == len("".join(result.split()))

    def test_removes_invisible_and_private_use_characters(self):
        text = "가​나﻿다라 마\x07"

        result = clean_text(text)

        assert result == "가나다라 마"

    def test_collapses_spaces_and_blank_runs(self):
        text = "  첫   줄  \r\n\r\n\r\n\r\n둘째\t\t줄"

        result = clean_text(text)

        assert result == "첫 줄\n\n둘째 줄"

    @pytest.mark.parametrize("collapse_spaces", [True, False])
    def test_page_break_and_vertical_tab_become_line_breaks(self, collapse_spaces):
        """Form feed (\\f) and vertical tab (\\v) were removed without a space like other control characters, so `합계 100\\f200 이월`
        became `합계 100200 이월` with the numbers on both sides joined. Both are text boundaries (page, line), so they are line
        breaks. Other control characters (\\x07 etc.) are removed as before."""
        text = "합계 100\x0c200 이월\x0b다음\x07 끝"

        result = clean_text(text, collapse_spaces=collapse_spaces)

        assert result == "합계 100\n200 이월\n다음 끝"

    def test_keeps_nested_list_indent_but_strips_other_leading_spaces(self):
        """Four or more leading spaces make a Markdown code block, so they are removed unless the line is a list line."""
        text = "- 항목\n  - 하위 항목\n      들여쓴 본문"

        result = clean_text(text)

        assert result == "- 항목\n  - 하위 항목\n들여쓴 본문"

    def test_keeps_code_fence_content_verbatim(self):
        text = "```\ndef f():\n    return  1\n```"

        result = clean_text(text)

        assert result == text

    def test_keeps_inner_spaces_when_collapse_disabled(self):
        text = "    code  block"

        result = clean_text(text, collapse_spaces=False)

        assert result == "    code  block"


class TestRenderTable:
    def test_renders_gfm_table_with_header_row(self):
        rows = [["이름", "값"], ["a", "1"]]

        result = render_table(rows)

        assert result == "| 이름 | 값 |\n| --- | --- |\n| a | 1 |"

    def test_escapes_pipes_and_converts_newlines_to_br(self):
        rows = [["a|b", "줄1\n줄2"], ["x", "y"]]

        result = render_table(rows)

        assert "| a\\|b | 줄1<br>줄2 |" in result

    def test_page_break_and_vertical_tab_inside_a_cell_become_br(self):
        """\\f and \\v in cell text were removed too, so the CSV cell `2\\f3` became `23`. Like a line break in a cell, they are `<br>`."""
        rows = [["a", "b"], ["1\x0c2", "3\x0b4"]]

        result = render_table(rows)

        assert result == "| a | b |\n| --- | --- |\n| 1<br>2 | 3<br>4 |"

    def test_drops_empty_rows_and_columns(self):
        rows = [["a", "", "b"], ["", "", ""], ["1", None, "2"]]

        result = render_table(rows)

        assert result == "| a | b |\n| --- | --- |\n| 1 | 2 |"

    def test_single_column_table_becomes_paragraphs(self):
        """A one-column table is a layout box used in place of a text box in Hangul and Word."""
        rows = [["안내문"], ["둘째 줄\n셋째 줄"]]

        result = render_table(rows)

        assert result == "안내문\n\n둘째 줄\n셋째 줄"

    def test_lifts_single_cell_title_row_above_table(self):
        rows = [["2026년 현황", "", ""], ["지역", "지점", "금액"], ["서울", "강남", "1"]]

        result = render_table(rows)

        assert result == "2026년 현황\n\n| 지역 | 지점 | 금액 |\n| --- | --- | --- |\n| 서울 | 강남 | 1 |"

    def test_lifts_title_row_with_a_unit_note_above_table(self):
        """When the top row was the two cells 'title + (단위: 천원)', it became the header row and the real header row moved down
        into the data."""
        rows = [["2024년 월별 매출 현황", "", "", "", "(단위: 천원)"], ["월", "국내", "해외", "합계", "전년비"],
                ["1월", "100", "50", "150", "5%"]]

        result = render_table(rows)

        assert result == ("2024년 월별 매출 현황 (단위: 천원)\n\n| 월 | 국내 | 해외 | 합계 | 전년비 |\n"
                          "| --- | --- | --- | --- | --- |\n| 1월 | 100 | 50 | 150 | 5% |")

    @pytest.mark.parametrize("rows", [
        [["이름", "단위: 원"], ["가", "1"], ["나", "2"]],
        [["구분", "", "(단위: 원)"], ["가", "1", "2"]],
        [["상반기", "", "하반기", ""], ["1월", "2월", "7월", "8월"], ["1", "2", "3", "4"]],
        [["구분", "", "단위", ""], ["가", "나", "원", "라"], ["1", "2", "3", "4"]],
    ], ids=["two-columns", "three-columns", "group-header", "unit-column-header"])
    def test_two_cell_top_row_that_is_not_a_title_stays_the_header(self, rows):
        result = render_table(rows)

        assert result.startswith("| ")

    def test_returns_empty_for_all_blank_table(self):
        assert render_table([["", None], [" ", ""]]) == ""


class TestTableGrid:
    def test_repeats_vertical_merge_and_blanks_horizontal_merge(self):
        """A vertically merged value must be repeated on every row so that, even when the table is cut by rows, it is still known
        which category a value belongs to."""
        grid = TableGrid()
        grid.place(0, 0, "제목", col_span=2)
        grid.place(1, 0, "분류", row_span=2)
        grid.place(1, 1, "가")
        grid.place(2, 1, "나")

        rows = grid.rows()

        assert rows == [["제목", ""], ["분류", "가"], ["분류", "나"]]

    def test_place_next_skips_cells_occupied_by_rowspan(self):
        grid = TableGrid()
        grid.place_next(0, "A", row_span=2)
        grid.place_next(0, "B")
        grid.place_next(1, "C")

        rows = grid.rows()

        assert rows == [["A", "B"], ["A", "C"]]

    def test_vertical_merge_repeats_text_but_not_image_placeholder(self):
        """Repeating a vertically merged cell that holds a picture on every row put the picture description in once per row,
        inflating the character count and delaying the limit check during description (which counts one placeholder). The text is
        repeated and the picture placeholder stays in the top cell only."""
        grid = TableGrid()
        grid.place(0, 0, "분류 ⟦IMG:ab:1⟧", row_span=3)
        for row in range(3):
            grid.place(row, 1, f"값 {row}")

        rows = grid.rows()

        assert [row[0] for row in rows] == ["분류 ⟦IMG:ab:1⟧", "분류", "분류"]

    def test_horizontally_filled_header_keeps_image_placeholder_only_in_first_cell(self):
        grid = TableGrid()
        grid.place(0, 0, "⟦IMG:ab:1⟧ 머리글", col_span=2, fill_horizontal=True)

        rows = grid.rows()

        assert rows == [["⟦IMG:ab:1⟧ 머리글", "머리글"]]


class TestTableGridLimits:
    """Trusting the spans and cell addresses written in the file as they are makes an input of a few KB use seconds and gigabytes
    (DOCX gridSpan 3 million, HWPX addresses 3000×3000)."""

    @pytest.mark.timing
    def test_cells_placed_in_order_on_a_long_row_take_linear_time(self):
        """Every cell that comes without an address (HTML) searched for a free place from column 0 of its row, taking the square of
        the number of cells in a row (one row of 50,000 cells: 56 s)."""
        import time

        grid = TableGrid()
        started = time.perf_counter()

        for _ in range(50_000):
            grid.place_next(0, "1")

        assert time.perf_counter() - started < 2.0
        assert grid.rows() == [["1"] * 50_000]

    def test_cells_placed_in_order_still_skip_cells_spanning_from_above_and_from_the_left(self):
        grid = TableGrid()
        grid.place_next(0, "A", row_span=2)
        grid.place_next(0, "B", col_span=2)
        grid.place_next(0, "C", row_span=2)
        grid.place_next(1, "D")
        grid.place(1, 3, "E")
        grid.place_next(1, "F")
        grid.place_next(1, "G")

        rows = grid.rows()

        assert rows == [["A", "B", "", "C", ""], ["A", "D", "F", "C", "G"]]

    def test_far_cell_address_does_not_inflate_the_grid(self):
        grid = TableGrid()
        grid.place(0, 0, "a")
        grid.place(1_000_000, 1_000_000, "b")

        rows = grid.rows()

        assert rows == [["a", ""], ["", "b"]]

    def test_huge_span_is_clamped(self):
        grid = TableGrid()
        grid.place(0, 0, "x", row_span=1_000_000, col_span=1_000_000)

        rows = grid.rows()

        assert rows[0][0] == "x"
        assert len(rows) * len(rows[0]) <= 10_000

    def test_span_budget_per_table_falls_back_to_single_cells(self):
        grid = TableGrid()
        for index in range(500):
            grid.place_next(0, "x", row_span=100, col_span=100)

        rows = grid.rows()

        assert sum(len(row) for row in rows) <= 1_000_000
        assert sum(cell == "x" for cell in rows[0]) == 500

    def test_row_span_past_the_last_row_does_not_add_rows(self):
        """rowSpan=5000 in a 2-row table made 4,999 fake rows repeating the value on each row. It stops at the last row where a
        cell was placed."""
        grid = TableGrid()
        grid.place(0, 0, "구분")
        grid.place(0, 1, "값")
        grid.place(1, 0, "매출", row_span=5000)
        grid.place(1, 1, "10")

        result = grid.render()

        assert result == "| 구분 | 값 |\n| --- | --- |\n| 매출 | 10 |"

    def test_column_span_past_the_last_column_does_not_cut_rows(self):
        """gridSpan=10000 on the top cell stretched the table to 10,000 columns, which hit the grid cell limit, and only 499 of
        1,000 rows were left (no warning)."""
        grid = TableGrid()
        grid.place(0, 0, "제목", col_span=10_000)
        for row in range(1, 1001):
            grid.place(row, 0, f"행{row}")
            grid.place(row, 1, str(row))

        rows = grid.rows()

        assert len(rows) == 1001
        assert {len(row) for row in rows} == {2}
        assert grid.omitted_rows() == 0

    def test_covered_cell_reported_by_the_extractor_keeps_its_row(self):
        """A table whose last row is entirely covered by the vertical merges of the cells above (DOCX vMerge, PPTX vMerge cells)
        tells the grid that the row is inside the table (`reach`)."""
        grid = TableGrid()
        grid.place(0, 0, "가", row_span=2)
        grid.place(0, 1, "나", row_span=2)
        grid.reach(1, 1)

        rows = grid.rows()

        assert rows == [["가", "나"], ["가", "나"]]

    def test_horizontal_fill_counts_as_part_of_the_table(self):
        """Header merging (`fill_horizontal`) fills horizontally merged cells with the value, so the spanned columns are inside
        the table too."""
        grid = TableGrid()
        grid.place(0, 0, "기간", col_span=3, fill_horizontal=True)

        rows = grid.rows()

        assert rows == [["기간", "기간", "기간"]]

    def test_rows_left_out_by_the_grid_limit_are_counted(self):
        """The number of bottom rows dropped by the grid cell limit is reported so that the extractor can warn (formerly they
        were dropped silently)."""
        grid = TableGrid()
        for index in range(3000):
            grid.place(index, index, "x")

        rows = grid.rows()

        assert len(rows) < 3000
        assert grid.omitted_rows() == 3000 - len(rows)


class TestMergedTableAsHtml:
    """A Markdown table cannot draw a horizontal merge, so the value stayed in the first cell only and the rest scattered as
    empty cells (in '| 합계 | | 4개 과제 |' there is no telling whether the empty cell is the period). Only tables left with a
    horizontally spanning cell are emitted as HTML tables (rowspan, colspan); the rest stay Markdown."""

    def test_table_with_a_horizontal_merge_keeps_its_structure(self):
        grid = TableGrid()
        for col, text in enumerate(["구분", "과제", "기간", "비고"]):
            grid.place(0, col, text)
        grid.place(1, 0, "상반기", row_span=2)
        grid.place(1, 1, "데이터 정비")
        grid.place(1, 2, "1~3월")
        grid.place(1, 3, "FAQ\n정책 문서")
        grid.place(2, 1, "모델 교체")
        grid.place(2, 2, "4~6월")
        grid.place(3, 0, "합계", col_span=3)
        grid.place(3, 3, "2개 과제")

        result = grid.render()

        assert result == (
            "<table>\n"
            "<tr><th>구분</th><th>과제</th><th>기간</th><th>비고</th></tr>\n"
            '<tr><td rowspan="2">상반기</td><td>데이터 정비</td><td>1~3월</td><td>FAQ<br>정책 문서</td></tr>\n'
            "<tr><td>모델 교체</td><td>4~6월</td><td></td></tr>\n"
            '<tr><td colspan="3">합계</td><td>2개 과제</td></tr>\n'
            "</table>"
        )

    def test_table_with_only_vertical_merges_stays_markdown(self):
        """A vertical merge loses no information as Markdown that repeats the value on every row. The output does not change."""
        grid = TableGrid()
        grid.place(0, 0, "분류")
        grid.place(0, 1, "값")
        grid.place(1, 0, "가", row_span=2)
        grid.place(1, 1, "1")
        grid.place(2, 1, "2")

        result = grid.render()

        assert result == "| 분류 | 값 |\n| --- | --- |\n| 가 | 1 |\n| 가 | 2 |"

    def test_merge_over_empty_columns_only_stays_markdown(self):
        """When a horizontal merge covers only empty columns, no spanning cell is left once those columns are removed. The
        right-margin cells of form tables are a common case."""
        grid = TableGrid()
        grid.place(0, 0, "항목")
        grid.place(0, 1, "내용", col_span=3)
        grid.place(1, 0, "가")
        grid.place(1, 1, "나", col_span=3)

        result = grid.render()

        assert result == "| 항목 | 내용 |\n| --- | --- |\n| 가 | 나 |"

    def test_title_row_spanning_the_table_stays_a_paragraph_above_a_markdown_table(self):
        """The title row at the top of a table (all columns merged) is taken out as a paragraph above the table, as before. One
        title does not turn the whole table into HTML."""
        grid = TableGrid()
        grid.place(0, 0, "2026년 추진 일정", col_span=3)
        for col, text in enumerate(["구분", "과제", "기간"]):
            grid.place(1, col, text)
            grid.place(2, col, f"값{col}")

        result = grid.render()

        assert result == "2026년 추진 일정\n\n| 구분 | 과제 | 기간 |\n| --- | --- | --- |\n| 값0 | 값1 | 값2 |"

    def test_title_row_is_a_paragraph_above_an_html_table_too(self):
        grid = TableGrid()
        grid.place(0, 0, "표 제목", col_span=3)
        for col, text in enumerate(["가", "나", "다"]):
            grid.place(1, col, text)
        grid.place(2, 0, "합계", col_span=2)
        grid.place(2, 2, "3")

        result = grid.render()

        assert result == ("표 제목\n\n<table>\n<tr><th>가</th><th>나</th><th>다</th></tr>\n"
                          '<tr><td colspan="2">합계</td><td>3</td></tr>\n</table>')

    def test_title_row_with_a_unit_note_is_a_paragraph_above_an_html_table(self):
        """In an '.xls' exported as HTML, the '조회기간 … | (단위: 원)' row became the header row (th) and the real header row
        became a data row."""
        grid = TableGrid()
        grid.place(0, 0, "거래처별 매출 현황", col_span=5)
        grid.place(1, 0, "조회기간: 2024-01-01 ~ 2024-03-31", col_span=3)
        grid.place(1, 3, "(단위: 원)", col_span=2)
        for col, text in enumerate(["거래처", "담당자", "1월", "2월", "3월"]):
            grid.place(2, col, text)
        grid.place(3, 0, "합계", col_span=2)
        for col in (2, 3, 4):
            grid.place(3, col, "1,000")

        result = grid.render()

        assert result == ("거래처별 매출 현황\n\n조회기간: 2024-01-01 ~ 2024-03-31 (단위: 원)\n\n<table>\n"
                          "<tr><th>거래처</th><th>담당자</th><th>1월</th><th>2월</th><th>3월</th></tr>\n"
                          '<tr><td colspan="2">합계</td><td>1,000</td><td>1,000</td><td>1,000</td></tr>\n</table>')

    def test_only_table_tags_in_cell_text_are_neutralized(self):
        """The result is LLM input, so cell text is kept as in the source (escaping `x<5` or `A & B` is noise). Only the table
        tags that could be confused with the table structure (`</td>`, `<tr>`, `<br>` etc.) are neutralized with `&lt;`. `|` does
        not split cells in an HTML table, so it stays."""
        grid = TableGrid()
        grid.place(0, 0, "제목", col_span=2)
        grid.place(1, 0, "</td></TR><br/><script>x<5</script>")
        grid.place(1, 1, "A & B | C")

        result = grid.render()

        assert "<td>&lt;/td>&lt;/TR>&lt;br/><script>x<5</script></td><td>A & B | C</td>" in result
        assert result.count("<tr>") == 2

    def test_image_placeholder_in_a_merged_cell_appears_once(self):
        grid = TableGrid()
        grid.place(0, 0, "그림 ⟦IMG:ab:1⟧", row_span=2)
        grid.place(0, 1, "가", col_span=2)
        grid.place(1, 1, "나")
        grid.place(1, 2, "다")

        result = grid.render()

        assert result.count("⟦IMG:ab:1⟧") == 1
        assert '<th rowspan="2">그림 ⟦IMG:ab:1⟧</th><th colspan="2">가</th>' in result

    def test_overlapping_spans_from_a_damaged_file_still_give_a_well_formed_table(self):
        """Even when the file writes merges that overlap one another, the table has a matching cell count on every row (the cell
        placed first wins)."""
        grid = TableGrid()
        grid.place(0, 0, "A", col_span=2)
        grid.place(1, 1, "B", row_span=1)
        grid.place(1, 0, "C", row_span=1, col_span=3)
        grid.place(0, 2, "D")

        result = grid.render()

        assert result == ('<table>\n<tr><th colspan="2">A</th><th>D</th></tr>\n'
                          "<tr><td>C</td><td>B</td><td></td></tr>\n</table>")

    def test_every_text_the_markdown_grid_shows_is_kept_when_spans_overlap(self):
        """With broken addresses or merges, even for a cell that lost its starting place to an earlier cell, the text with which
        it filled free places is visible in the Markdown grid. HTML puts the text at that place too (the 'B' that was missing)."""
        grid = TableGrid()
        grid.place(0, 0, "A", col_span=2)
        grid.place(0, 2, "C")
        grid.place(1, 0, "x")
        grid.place(1, 1, "y")
        grid.place(1, 2, "z")
        grid.place(0, 1, "B", row_span=3)
        grid.place(2, 0, "p")
        grid.place(2, 2, "q")

        result = grid.render()

        assert result.endswith("<tr><td>p</td><td>B</td><td>q</td></tr>\n</table>")

    def test_random_damaged_tables_keep_every_text_and_a_consistent_shape(self):
        """Even when overlapping, large and negative spans and distant addresses are placed at random like a broken file, the HTML
        table holds all the text visible in the Markdown grid and has a matching cell count on every row (in review fuzzing, text
        spilled in by a cell that had lost its starting place was covered by a later merge and dropped)."""
        import random

        rng = random.Random(20261001)
        for _ in range(1_000):
            grid = TableGrid()
            for index in range(rng.randint(1, 12)):
                text = rng.choice(["", f"t{index}", f"t{index}\nu{index}", "⟦IMG:ab:1⟧"])
                row_span, col_span = rng.choice([1, 1, 2, 3, 0, -1, 50]), rng.choice([1, 1, 2, 3, 0, -2, 40])
                if rng.random() < 0.5:
                    grid.place_next(rng.randint(0, 5), text, row_span=row_span, col_span=col_span)
                else:
                    grid.place(rng.choice([0, 1, 2, 3, 1000]), rng.choice([0, 1, 2, 3, 500]), text,
                               row_span=row_span, col_span=col_span)

            result = grid.render()
            if "<table>" not in result:
                continue
            shown = {cell for row in grid.rows() for cell in row if cell.strip()}
            assert all(text in result or text.replace("\n", "<br>") in result for text in shown), (shown, result)  # the title is a paragraph
            assert _table_widths(result) == 1, result

    def test_columns_are_recomputed_after_the_title_row_is_pulled_out(self):
        """A column that had text only in the title row is an empty column once the title is taken out. It is removed, as in
        render_table."""
        grid = TableGrid()
        grid.place(0, 3, "표 제목")
        for col, text in enumerate(["가", "나", "다"]):
            grid.place(1, col, text)
        grid.place(2, 0, "합계", col_span=2)
        grid.place(2, 2, "3")

        result = grid.render()

        assert result == ("표 제목\n\n<table>\n<tr><th>가</th><th>나</th><th>다</th></tr>\n"
                          '<tr><td colspan="2">합계</td><td>3</td></tr>\n</table>')

    @pytest.mark.timing
    def test_many_single_cell_rows_render_in_linear_time(self):
        """With a run of rows that have only one filled cell, the title-row check rescanned all the cells for every row, taking the
        square of the row count (800KB HTML: 33 s)."""
        import time

        grid = TableGrid()
        for row in range(20_000):
            grid.place(row, row % 3, f"v{row}")
        started = time.perf_counter()

        grid.render()

        assert time.perf_counter() - started < 6.0

    def test_nested_table_keeps_its_structure_inside_an_html_cell(self):
        """Flattening a table inside a table into 'cell / cell' lines lost the row and column structure. The outer table is
        emitted as HTML with the inner table as one-line HTML inside the cell (the inner table's merges are kept too). It is HTML
        even when the outer table has no horizontal merge."""
        inner = TableGrid()
        inner.place(0, 0, "담당", col_span=2)
        inner.place(1, 0, "운영팀")
        inner.place(1, 1, "3명")
        outer = TableGrid()
        outer.place(0, 0, "구분")
        outer.place(0, 1, "인력")
        outer.place(1, 0, "하반기")
        outer.place(1, 1, "인원:\n" + outer.embed(inner))

        result = outer.render()

        assert result == ("<table>\n<tr><th>구분</th><th>인력</th></tr>\n"
                          '<tr><td>하반기</td><td>인원:<br><table><tr><th colspan="2">담당</th></tr>'
                          "<tr><td>운영팀</td><td>3명</td></tr></table></td></tr>\n</table>")

    def test_table_inside_a_one_column_box_becomes_its_own_block(self):
        """A one-column table (layout box) is unwrapped into paragraphs as before, and a table inside it becomes a table of its
        own rather than sitting inside a cell."""
        inner = TableGrid()
        for col, text in enumerate(["항목", "값"]):
            inner.place(0, col, text)
            inner.place(1, col, f"{text}1")
        box = TableGrid()
        box.place(0, 0, "안내 상자")
        box.place(1, 0, "아래 표 참고\n" + box.embed(inner))

        result = box.render()

        assert result == "안내 상자\n\n아래 표 참고\n\n| 항목 | 값 |\n| --- | --- |\n| 항목1 | 값1 |"

    def test_table_tags_in_nested_cell_text_are_neutralized_but_the_nested_table_is_not(self):
        inner = TableGrid()
        inner.place(0, 0, "a</td>")
        inner.place(0, 1, "b")
        outer = TableGrid()
        outer.place(0, 0, "x</tr>")
        outer.place(0, 1, outer.embed(inner))

        result = outer.render()

        assert "<th>x&lt;/tr></th><th><table><tr><th>a&lt;/td></th><th>b</th></tr></table></th>" in result

    def test_nested_table_in_a_vertically_merged_cell_appears_once(self):
        """A vertical merge repeats the text on every row but, like a picture placeholder, an inner table placeholder is not
        repeated (the table came out once per row)."""
        inner = TableGrid()
        inner.place(0, 0, "가")
        inner.place(0, 1, "나")
        box = TableGrid()
        box.place(0, 0, "상자 " + box.embed(inner), row_span=3)
        box.place(3, 0, "끝")

        result = box.render()

        assert result.count("| 가 | 나 |") == 1

    def test_empty_nested_table_leaves_nothing(self):
        outer = TableGrid()
        outer.place(0, 0, "가")
        outer.place(0, 1, "나" + outer.embed(TableGrid()))

        result = outer.render()

        assert result == "| 가 | 나 |\n| --- | --- |"

    @pytest.mark.timing
    def test_huge_spans_render_quickly(self):
        """A table that has used up the total span cap (500,000 cells) is still drawn quickly as HTML. The bottom row fills every
        column so that a merge remains."""
        import time

        grid = TableGrid()
        for index in range(50):
            grid.place(0, index * 100, "x", row_span=100, col_span=100)
        for col in range(5_000):
            grid.place(100, col, "y")
        started = time.perf_counter()

        result = grid.render()

        assert time.perf_counter() - started < 6.0
        assert result.count('rowspan="100" colspan="100"') == 50


def test_join_blocks_keeps_list_items_on_consecutive_lines():
    blocks = ["문단", "- 하나", "- 둘", "", "다음 문단"]

    result = join_blocks(blocks)

    assert result == "문단\n\n- 하나\n- 둘\n\n다음 문단"


def _table_widths(html: str) -> int:
    """Number of distinct row widths of an HTML table (rowspan and colspan applied). -1 if cells overlap or are missing."""
    import re

    occupied: dict[int, int] = {}  # column → rows still covered from above
    widths = set()
    for row in re.findall(r"<tr>(.*?)</tr>", html):
        below = {col: left - 1 for col, left in occupied.items() if left > 1}
        used = set(occupied)
        col = 0
        for row_span, col_span in re.findall(r'<t[dh](?: rowspan="(\d+)")?(?: colspan="(\d+)")?>', row):
            while col in used:
                col += 1
            for spanned in range(col, col + int(col_span or 1)):
                if spanned in used:
                    return -1
                used.add(spanned)
                if int(row_span or 1) > 1:
                    below[spanned] = int(row_span) - 1
            col += int(col_span or 1)
        if used != set(range(len(used))):
            return -1
        widths.add(len(used))
        occupied = below
    return len(widths)


class TestUnclosedCodeFence:
    def test_unclosed_fence_line_from_a_non_markdown_source_is_escaped(self):
        """A single piece of text starting with ``` in a paragraph of DOCX etc. turned the rest of the document into a code block
        and stopped the whitespace cleanup. An unpaired fence line is neutralized as `\\```."""
        text = "```python\n# 진짜 제목\n항목    사이   공백"

        result = clean_text(text)

        assert result == "\\```python\n# 진짜 제목\n항목 사이 공백"

    def test_paired_fences_still_keep_their_content_verbatim(self):
        text = "앞 글\n```\ndef f():\n    return  1\n```\n뒤    글\n```"

        result = clean_text(text)

        assert result == "앞 글\n```\ndef f():\n    return  1\n```\n뒤 글\n\\```"

    def test_markdown_and_text_sources_are_left_alone(self):
        text = "```python\n항목    사이   공백"

        result = clean_text(text, collapse_spaces=False)

        assert result == text


class TestTwoColumnTitleRow:
    """A one-cell title row at the top of a two-column table became the header row and the real header row ('부서 | 인원') moved
    down into the data rows (XLSX merged title, CSV, HTML colspan=2). In a two-column table the title is taken out only when the
    row below looks like a header row (both cells non-numeric text) and numbers come below it."""

    def test_single_cell_title_row_of_a_two_column_table_is_lifted(self):
        rows = [["부서별 인원 현황", ""], ["부서", "인원"], ["영업", "10"], ["개발", "20"]]

        result = render_table(rows)

        assert result == "부서별 인원 현황\n\n| 부서 | 인원 |\n| --- | --- |\n| 영업 | 10 |\n| 개발 | 20 |"

    @pytest.mark.parametrize("rows", [
        [["질문", ""], ["무엇인가요", "답입니다"], ["언제인가요", "내일"]],
        [["구분", ""], ["가", "1"], ["나", "2"]],
        [["", "합계"], ["부서", "인원"], ["영업", "10"]],
        [["부서별 인원 현황", ""], ["부서", "인원"]],
    ], ids=["text-only", "value-row-below", "title-in-second-column", "no-data-below"])
    def test_two_column_table_whose_top_row_is_not_a_title_keeps_its_header(self, rows):
        result = render_table(rows)

        assert result.startswith("| ")

    def test_title_row_spanning_a_two_column_grid_is_a_paragraph(self):
        grid = TableGrid()
        grid.place(0, 0, "부서별 인원 현황", col_span=2)
        for row, (name, count) in enumerate([("부서", "인원"), ("영업", "10")], start=1):
            grid.place(row, 0, name)
            grid.place(row, 1, count)

        result = grid.render()

        assert result == "부서별 인원 현황\n\n| 부서 | 인원 |\n| --- | --- |\n| 영업 | 10 |"


class TestTitleRowOnlyFromMergedCells:
    """`merged_titles_only` (continued ruled tables of a PDF): a row fragment carried over to the next page is a top row with
    only one filled cell, so it was taken out as a table title and became a paragraph. It is a title only when the other cells are
    covered by a horizontal merge (None); with empty cells ("") it is a row of the table. The default is as before."""

    rows = [["", "", "있어야 한다.\n1-4. 사용자는", ""], ["REQ-002", "기능 2", "2-1. 사용자는", "하"],
            ["REQ-003", "기능 3", "3-1. 사용자는", "상"]]

    def test_top_row_with_one_filled_cell_among_empty_cells_stays_a_table_row(self):
        result = render_table(self.rows, merged_titles_only=True)

        assert result.startswith("|  |  | 있어야 한다.<br>1-4. 사용자는 |  |\n| --- |")

    def test_top_row_merged_across_the_table_is_still_a_title(self):
        rows = [["요구사항 목록", None, None, None], *self.rows[1:]]

        result = render_table(rows, merged_titles_only=True)

        assert result.startswith("요구사항 목록\n\n| REQ-002 |")

    def test_default_keeps_lifting_a_top_row_with_one_filled_cell(self):
        result = render_table(self.rows)

        assert result.startswith("있어야 한다.\n1-4. 사용자는\n\n| REQ-002 |")


class TestBlankLinesInsideCodeFences:
    def test_blank_lines_inside_a_fence_are_kept_in_markdown_sources(self):
        """Consecutive blank lines inside a code block of MD/TXT were reduced to one, changing the shape of the code (PEP 8's two
        lines between functions)."""
        text = "앞\n\n\n\n```python\nx = 1\n\n\ny = 2\n```\n\n\n뒤"

        result = clean_text(text, collapse_spaces=False)

        assert result == "앞\n\n```python\nx = 1\n\n\ny = 2\n```\n\n뒤"

    def test_blank_lines_inside_a_paired_fence_are_kept_in_other_sources_too(self):
        text = "앞\n```\na\n\n\nb\n```\n\n\n뒤"

        result = clean_text(text)

        assert result == "앞\n```\na\n\n\nb\n```\n\n뒤"

    def test_blank_lines_after_an_unpaired_fence_are_still_collapsed(self):
        text = "```\n가\n\n\n\n나"

        result = clean_text(text, collapse_spaces=False)

        assert result == "```\n가\n\n나"
