"""Small pieces several formats write in one shape: the character budget of a table, a table inside a footnote
('cell / cell; cell / cell'), a review comment line, dutmal ("main(annotation)") and the placeholder of an OLE object.
"""

from collections.abc import Iterable

from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction


# ---- Tables: rows left out, and the character limit --------------------------------------------------------------

def omitted_rows_warning(count: int) -> str:
    """Warning for rows dropped by the table grid cell limit (`markdown.TableGrid.omitted_rows`), shared by DOCX, PPTX, HWP and HWPX.
    The bottom rows used to vanish without a warning
    (Pinned by: test_office_robustness.py::TestSpansPastTheTable::test_docx_rows_left_out_by_the_grid_limit_are_reported)."""
    return f"The table is too large; the bottom {count} row(s) could not be converted."


class TableBudget:
    """Watches the character limit (`max_chars`) while a top-level table is read cell by cell (shared by DOCX, HWP and HWPX). Counting
    only per top-level block, the overrun was noticed only after a table of hundreds of thousands of rows had been rendered to the end
    (a 200,000-row DOCX: 33 s, 2.3 GB. Pinned by: test_office_table_char_limit.py).

    `Extraction.count` is not called — after the table is rendered the extractor counts again per top-level block, so it would be
    counted twice. Here we only report whether 'characters already counted + cell text read from this table (`solid_length`)' exceeded
    the limit, so that cell reading stops. The text rendered from a stopped table holds all the cell text read, so the later per-block
    count also exceeds the limit (it never counts more than the real amount — Pinned by: TestLimitIsExactAroundTables).

    Only cell text that actually lands in the table is counted. A cell placed where an earlier cell or an earlier cell's span already
    sits is not counted — DOCX: `TableGrid` drops it; HWP/HWPX: it moves to the next free slot of that row
    (`TableGrid.place_or_next`), but that slot is unknown here, so this errs toward undercounting. A span is taken to cover exactly what
    the file says — `TableGrid` only ever shrinks spans, so this errs toward undercounting. Once the memory of covered slots exceeds
    `_MAX_MARKS`, or a cell with an unknown position (`forget`) arrives, the remaining cells of that table are not counted (reading
    just does not stop; the result is the same).
    Known accepted gap: a table whose bottom rows are dropped by the grid cell limit (5 million cells) may stop early by counting rows
    that will be dropped — dropped rows require filling 5 million cells with characters below the limit, which real documents do not.
    """

    _MAX_MARKS = 1_000_000

    def __init__(self, extraction: Extraction, options: ConvertOptions) -> None:
        self._extraction = extraction
        self._limit = options.max_chars
        self._counted = 0
        self._taken: set[tuple[int, int]] = set()
        self._marks = self._MAX_MARKS

    def add(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1) -> bool:
        """One cell placed at (row, column). True once the limit is exceeded — the signal not to read the remaining cells."""
        if self._limit is None:
            return False
        if self._marks >= 0 and (row, col) not in self._taken:
            row_span, col_span = max(row_span, 1), max(col_span, 1)
            self._marks -= row_span * col_span
            if self._marks >= 0:
                self._taken.update((row + dr, col + dc) for dr in range(row_span) for dc in range(col_span))
                self._counted += solid_length(text)
        return self._extraction.chars + self._counted > self._limit

    def forget(self) -> None:
        """A cell with an unknown position (`TableGrid.place_next`) arrived. Where it will sit is unknown, so the remaining cells of
        this table are not counted."""
        self._marks = -1


# ---- Dutmal ------------------------------------------------------------------------------------------------------

def format_dutmal(main: str, annotation: str = "") -> str:
    """Hangul (HWP) dutmal (a small annotation attached above or below the main text) → "main(annotation)". HWP and HWPX use the
    same shape."""
    return f"{main}({annotation})" if main and annotation else main or annotation


# ---- Tables inside footnotes -------------------------------------------------------------------------------------

# A footnote is one line (`[^1]: …`), so a table inside it joins cells with ' / ' (same as `TableGrid.flatten`) and rows with '; '.
NOTE_ROW_SEPARATOR = "; "


def note_table(rows: Iterable[Iterable[str]]) -> str:
    """Rows of a table inside a footnote → one line. Line breaks in cell text are merged into spaces; empty cells and empty rows are
    dropped. DOCX, HWP and HWPX use the same shape. Rendering a Markdown table and then merging with spaces gave
    '| 연도 | 값 | | --- |', and joining the cell paragraphs gave '구분 값 A 10'
    (Pinned by: test_office_symbols_fields_notes.py::TestDocxTableInsideFootnote, test_hwp.py::TestTableInsideFootnote)."""
    lines = (" / ".join(text for text in (" ".join(cell.split()) for cell in row) if text) for row in rows)
    return NOTE_ROW_SEPARATOR.join(line for line in lines if line)


# ---- Comments (review remarks) -----------------------------------------------------------------------------------

COMMENT_TARGET_MAX = 40  # show only this much of the body span the comment is attached to ('…' beyond it)


def comment_line(author: str, text: str, target: str = "", *, reply: bool = False, cell: str = "") -> str:
    """One comment line: `> Comment(author, on "body span"): text`; a reply is `> Reply(author): text`. DOCX, PPTX and HWPX put them
    at the end of the document/slide, Excel at the end of the sheet with `cell E5` added. Comments were dropped wholesale without a
    warning, so the LLM could not see review remarks (Pinned by: test_office_comments.py, test_hwp.py::TestMemo,
    test_spreadsheet_notes_pictures_and_times.py). Author, cell and span are left out when absent."""
    target = " ".join(target.split())
    if len(target) > COMMENT_TARGET_MAX:
        target = target[:COMMENT_TARGET_MAX] + "…"
    details = ", ".join(part for part in (" ".join(author.split()), f"cell {cell}" if cell else "",
                                          f'on "{target}"' if target else "") if part)
    label = "Reply" if reply else "Comment"
    return f"> {label}({details}): {' '.join(text.split())}" if details else f"> {label}: {' '.join(text.split())}"


# ---- OLE objects -------------------------------------------------------------------------------------------------

# Placeholder for an OLE object whose contents were not read (a pasted Excel sheet or chart). HWP/HWPX always put it; DOCX/PPTX put
# it when the preview picture could not be registered (picture extraction is off or there is no preview)
# (Pinned by: test_hwp.py::TestEmbeddedObjects, test_office_chart_units_and_ole.py::TestOfficeOleObjectPlaceholders).
OLE_PLACEHOLDER = "[OLE object]"


def unread_ole_warning(count: int) -> str:
    return f"The contents of {count} OLE object(s) (pasted tables, charts, etc.) could not be converted."
