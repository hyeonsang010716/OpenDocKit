"""CSV and other delimited text → one Markdown table."""

from collections.abc import Iterator
import csv
from io import StringIO
import re

from opendockit.extractors._encoding import decode_text, decoding_warning
from opendockit.extractors._sheet_layout import _collect_rows, _sheet_text
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# The delimiter hint on the first line that Excel writes and reads (`sep=;`). Honoured only when the first line is nothing but that.
_SEPARATOR_HINT = re.compile(r"sep=([^\r\n])[ \t]*(?:\r\n|\r|\n|\Z)", re.IGNORECASE)


def extract_csv(data: bytes, options: ConvertOptions) -> Extraction:
    text, encoding = decode_text(data)
    # A first line `sep=;` is Excel's delimiter hint. Use that delimiter and drop the line — it used to become the header row and
    # was not even used to decide the delimiter (Pinned by: test_spreadsheet.py::TestCsvLimitsAndHints).
    hint = _SEPARATOR_HINT.match(text)
    if hint:
        text = text[hint.end():]
    # Using only the delimiter and discarding the quote character and the skip-space-after-delimiter setting split the cell at the
    # comma inside the quotes of `홍길동, 30, "서울시, 강남구"` (Pinned by: test_spreadsheet.py::TestCsvDialect). If sniffing fails
    # the csv module defaults are used.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=hint.group(1) if hint else ",\t;|")
        style = {"delimiter": dialect.delimiter, "quotechar": dialect.quotechar or '"',
                 "skipinitialspace": dialect.skipinitialspace}
    except csv.Error:
        style = {"delimiter": hint.group(1) if hint else "\t" if sample.count("\t") > sample.count(",") else ","}
    steadier = _steadier_delimiter(sample, len(text) > len(sample)) if style["delimiter"] == "," and not hint else None
    if steadier:
        style["delimiter"], style["skipinitialspace"] = steadier
    if style.get("quotechar") == "'" and not _wraps_whole_fields(sample, "'", style["delimiter"]):
        style["quotechar"] = '"'
    if (style.get("quotechar", '"') == '"'
            and _escapes_quotes_with_backslashes(sample, str(style["delimiter"]), len(text) > len(sample))):
        text = _unescape_backslashes(text)

    extraction = Extraction(metadata={"encoding": encoding})
    warning = decoding_warning(encoding)
    if warning:
        extraction.warnings.append(warning)
    rows = _collect_rows(_csv_rows(text, style, extraction.warnings, options.max_sheet_cols), options, "CSV", extraction)
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, _sheet_text(rows)))
    return extraction


# A backslash and the one character after it. The only characters MySQL `INTO OUTFILE` (ESCAPED BY '\') escapes are the quote, the
# backslash, NULL (`\N`), NUL (`\0`), the control character names (`\n`, `\t`, `\r`, `\b`, `\Z`), the delimiter and line breaks.
_BACKSLASH_PAIR = re.compile(r"\\(.?)", re.DOTALL)
_MYSQL_ESCAPED = frozenset('"\\N0ntrbZ\r\n')
_BACKSLASH_ESCAPE = re.compile(r'\\(["\\])')


def _escapes_quotes_with_backslashes(sample: str, delimiter: str, cut: bool) -> bool:
    """Whether this is a CSV that writes a double quote inside a cell as `\\"` (the MySQL `SELECT … INTO OUTFILE` default). Only when
    every backslash in the sample stands before a character MySQL escapes, there is a place where `\\"` is followed by a character
    that is not a cell end (delimiter, line end, quote), and the sample read with those escapes resolved has the same number of
    cells on every line with double quotes wrapping whole cells (`_field_counts`; this filters out ordinary CSVs containing a path
    ending like `"c:\\"`).

    Reading with the doubled-quote rule alone broke `"27\\" 모니터"` into `27\\ 모니터"`
    (Pinned by: test_core_spreadsheet.py::TestCsvBackslashEscapes).
    Deliberate: detection is conservative — if there are other backslashes (`C:\\temp`, `\\d+`) the file is not read as escaped.
    Reading it that way would make those characters disappear. In a file detected this way only `\\"` and `\\\\` are resolved, and
    `\\N` (NULL) and the like are left as literal characters. Known accepted gap: if `\\"` occurs only after the sample (the first
    8,192 characters) it is not detected.
    """
    evidence = False

    for match in _BACKSLASH_PAIR.finditer(sample):
        escaped = match.group(1)
        if escaped not in _MYSQL_ESCAPED and escaped != delimiter:
            return False
        if escaped == '"' and sample[match.end():match.end() + 1] not in (delimiter, '"', "\r", "\n", ""):
            evidence = True
    if not evidence:
        return False

    unescaped = _unescape_backslashes(sample)
    _, steady, quoted = _field_counts(unescaped, delimiter, cut)
    return steady and quoted


def _unescape_backslashes(text: str) -> str:
    """`\\"` → `""` (the csv module's doubled quote), `\\\\` → `\\`. Other backslashes are left as is."""
    return _BACKSLASH_ESCAPE.sub(lambda match: '""' if match.group(1) == '"' else "\\", text)


def _steadier_delimiter(sample: str, cut: bool) -> tuple[str, bool] | None:
    """The delimiter to use instead of the comma (tab, semicolon, pipe) and whether to skip spaces after it. None if the comma is right.

    The Sniffer picks the comma whenever every line has the same number of commas. Tab-separated amount tables
    (`서울\t1,234\t5,678`), semicolon CSVs with a decimal comma (`2024-01-01;1,5;2,5`) and cells listing words with commas were
    therefore split in the middle of numbers and sentences. If splitting by a candidate gives the same number of cells on every
    line of the sample (2 or more), that candidate is used. For files whose lines differ in cell count, only TSVs that omit
    trailing empty cells (`_tab_on_every_comma_line`) and files with title/metadata lines in front or short lines mixed in
    (`_delimiter_of_most_lines`) are detected; the rest keep the Sniffer's decision.
    A one-line file accepts only the tab — semicolons and pipes also occur inside text, so one line is weak evidence.
    To tell apart a comma CSV whose quoted cells contain one candidate per line (`가,"확인; 완료"`), the comma is kept when the
    pattern of double quotes wrapping whole cells fits the comma but not the candidate
    (Pinned by: test_spreadsheet.py::TestCsvDialect).
    """
    _, comma_steady, comma_quoted = _field_counts(sample, ",", cut)
    for delimiter in "\t;|":
        counts, steady, quoted = _field_counts(sample, delimiter, cut)
        if steady and (len(counts) > 1 or delimiter == "\t") and (quoted or not (comma_steady and comma_quoted)):
            return delimiter, quoted and f'{delimiter} "' in sample
    if not comma_steady and _field_counts(sample, "\t", cut)[2] and _tab_on_every_comma_line(sample, cut):
        return "\t", False
    if not comma_steady:
        delimiter = _delimiter_of_most_lines(sample, cut)
        if delimiter:
            return delimiter, f'{delimiter} "' in sample
    return None


def _delimiter_of_most_lines(sample: str, cut: bool) -> str | None:
    """The delimiter to use instead of the comma (tab, semicolon, pipe) in a file whose lines differ in cell count. None if none fits.

    With title/metadata lines in front (`Umsatzanzeige;Datei erstellt am …`, `고객 목록`) or lines that omit trailing empty cells
    mixed in, no delimiter gives an even cell count, so the Sniffer's comma remained and the amount `-1.234,56` was split into
    `-1.234 | 56`. If the lines having the maximum cell count for a candidate (3 cells or more) number two or more and are the
    majority, and the comma cell count is uneven from the first such line (the header) on, that candidate is used. If the comma
    cell count is even from the header on, the `;`/`|` are lists inside values and it is a comma CSV
    (Pinned by: test_spreadsheet.py::TestCsvDelimiterOfMostLines).
    """
    commas = _line_field_counts(sample, ",", cut)
    for delimiter in "\t;|":
        counts = _line_field_counts(sample, delimiter, cut)
        width = max(counts, default=0)
        if width < 3 or counts.count(width) < 2 or counts.count(width) * 2 <= len(counts):
            continue
        if len(set(commas[counts.index(width):])) > 1:
            return delimiter
    return None


def _line_field_counts(sample: str, delimiter: str, cut: bool) -> list[int]:
    """Cell count per non-empty line when split by `delimiter` (double-quoted cells within a line are counted as in `_field_counts`)."""
    lines = [line for line in sample.splitlines() if line.strip()]
    if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return [max(_field_counts(line, delimiter, False)[0], default=1) for line in lines]


def _tab_on_every_comma_line(sample: str, cut: bool) -> bool:
    """Whether this is a TSV whose lines differ in tab count because trailing empty cells are omitted: the first line (header) has a
    tab, and every line containing a comma has a tab (2 or more lines with tabs).

    TSV exports from Excel and ERP systems omit the empty cells at the end of a line, so the tab cell count is uneven. The comma
    picked by the Sniffer then remained, cells were split at the thousands separators of amounts (`1,100,000`) and columns shifted.
    A comma CSV would instead have a header with tabs but no commas, or a line with commas but no tabs
    (Pinned by: test_spreadsheet.py::TestCsvDialect::test_tab_file_whose_rows_omit_trailing_empty_cells_is_tab_delimited,
    test_comma_file_with_some_tabs_stays_comma_delimited).
    """
    lines = [line for line in sample.splitlines() if line.strip()]
    if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return (bool(lines) and "\t" in lines[0] and sum("\t" in line for line in lines) >= 2
            and all("\t" in line for line in lines if "," in line))


def _field_counts(sample: str, delimiter: str, cut: bool) -> tuple[list[int], bool, bool]:
    """(cell count per line when split by `delimiter`, whether all counts are equal and at least 2, whether double quotes are used
    only to wrap whole cells). Empty lines are left out.

    When double quotes wrap a whole cell, the delimiters and line breaks inside count as characters. If a double quote occurs in
    the middle of a cell (it is not a quote for this delimiter), each line is counted by its number of delimiters only. `cut` means
    the sample is only the head of the file — the cut-off last line is not counted.
    """
    separator = re.escape(delimiter)
    token = re.compile(rf' *"(?:[^"]|"")*(?:" *|\Z)({separator}|\r\n|[\r\n]|\Z)|[^"{separator}\r\n]*({separator}|\r\n|[\r\n]|\Z)')
    counts: list[int] = []
    fields = position = 0
    quoted = True

    while position < len(sample):
        match = token.match(sample, position)
        if match is None:
            quoted = False
            break
        fields += 1
        position = match.end()
        end = match.group(1) if match.group(1) is not None else match.group(2)
        if end != delimiter:
            if (fields > 1 or match.group(0).strip()) and (end or not cut):
                counts.append(fields)
            fields = 0
    if quoted and fields and not cut:
        counts.append(fields + 1)  # last line ending in a delimiter: there is one more empty cell

    if not quoted:
        lines = sample.splitlines()
        if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
            lines.pop()
        counts = [line.count(delimiter) + 1 for line in lines if line.strip()]
    return counts, bool(counts) and counts[0] >= 2 and len(set(counts)) == 1, quoted


def _wraps_whole_fields(sample: str, quote: str, delimiter: str) -> bool:
    """Whether every cell containing `quote` is wrapped whole in that character (`'홍길동','1,000'`). Only the lines of the head
    sample are checked.

    The Sniffer picks the single quote as the quote character after seeing just one word such as `'목표'`. A cell starting with a
    single quote, like `'24년 상반기`, then swallowed the delimiters up to the next single quote and the cells shifted. Single
    quotes are common inside text (abbreviated years, quotations, English possessives), so they are accepted as the quote character
    only when they wrap whole cells. For double quotes the Sniffer's decision is used as is
    (Pinned by: test_spreadsheet.py::TestCsvDialect::test_apostrophe_inside_a_field_is_not_a_quote_character).
    """
    mark, separator = re.escape(quote), re.escape(delimiter)
    field = rf"(?: *{mark}(?:[^{mark}]|{mark}{mark})*{mark} *|[^{mark}{separator}]*)"
    line_pattern = re.compile(rf"{field}(?:{separator}{field})*")
    lines = sample.splitlines()
    if len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return all(line_pattern.fullmatch(line) for line in lines)


def _csv_rows(text: str, style: dict[str, object], warnings: list[str], max_cols: int) -> Iterator[tuple[str, ...]]:
    """CSV rows. If the quoting is broken and the csv module cannot read it, lines from that point on are split by the delimiter
    only. Cells are created only up to the column limit + 1 (`_within_columns`).

    A single unclosed quote swallowed everything after it into one cell, and when that cell exceeded the csv module's field size
    limit (131,072 characters) a csv.Error was raised and the whole file was rejected. The limit is a process-wide setting, so it
    is not raised (deliberate). Rows read so far are used as they are; the failed row is read again with a quote-aware split
    (`_quoted_record`), and if the quote closes only that row is taken from it and reading returns to the csv module from the next
    line. If it never closes before the end of the file, each line from that one on is split by the delimiter, preferring not to
    lose characters (Pinned by: test_office.py::TestCsvWithBrokenQuotes).
    """
    lines = StringIO(text, newline="").readlines()
    delimiter, quote = str(style["delimiter"]), str(style.get("quotechar", '"'))
    start = 0

    while True:
        # strict: the csv module otherwise ends the file inside an unclosed quote without an error, so a small file had every row
        # after the quote swallowed into one cell without a warning. Strict mode also stops at text after a closing quote
        # (`"a"b`); `_quoted_record` reads that row the way the lenient reader does
        # (Pinned by: test_audit_spreadsheet_fixes.py::TestUnclosedQuoteInASmallCsv).
        reader = csv.reader(_lines_within_columns((lines[index] for index in range(start, len(lines))), delimiter, quote,
                                                  max_cols), **style, strict=True)
        read_lines = 0
        try:
            for row in reader:
                yield tuple(map(_unwrap_text_formula, row))
                read_lines = reader.line_num
            return
        except csv.Error:
            failed = start + read_lines

        record = _quoted_record(lines, failed, style) if _plain_quoting(style) else None
        if record is None:
            warnings.append(f"The quoting in the CSV is inconsistent from line {failed + 1} on, so the rest was split by the delimiter only.")
            for line in lines[failed:]:
                yield tuple(map(_unwrap_text_formula,
                                _within_columns(line, delimiter, max_cols).rstrip("\r\n").split(delimiter)))
            return
        fields, start = record
        beyond = "…" if any(field.strip() for field in fields[max_cols:]) else ""
        yield tuple(map(_unwrap_text_formula, fields if len(fields) <= max_cols else [*fields[:max_cols], beyond]))


def _plain_quoting(style: dict[str, object]) -> bool:
    """Whether this is a dialect `_quoted_record` can read the same way as the csv module (doubled-quote rule, no escape character)."""
    return (not style.get("escapechar") and style.get("doublequote", True) is not False
            and style.get("quoting", csv.QUOTE_MINIMAL) != csv.QUOTE_NONE)


def _quoted_record(lines: list[str], first: int, style: dict[str, object]) -> tuple[list[str], int] | None:
    """Split the one row starting at line `first` with quote awareness → (cells, line number of the next row). None if the quote
    never closes before the end of the file.

    A valid row (a long body cell) on which the csv module failed because of the field size limit was split by the delimiter only,
    so the body was broken into a cell at every comma and its tail was dropped at the column limit
    (Pinned by: test_office.py::TestCsvWithBrokenQuotes::test_quoted_field_longer_than_the_csv_module_limit_is_kept_whole).
    Text between a closing quote and the next delimiter is appended to the cell, as the csv module (non-strict) does.
    """
    delimiter, quote = str(style["delimiter"]), str(style.get("quotechar", '"'))
    skip_space = bool(style.get("skipinitialspace"))
    fields: list[str] = []
    parts: list[str] = []
    index, position, at_start, quoted = first, 0, True, False
    line = lines[first]
    end = len(line.rstrip("\r\n"))

    while True:
        if at_start:
            while skip_space and line.startswith(" ", position):
                position += 1
            at_start, quoted = False, line.startswith(quote, position)
            position += quoted
        if quoted:
            close = line.find(quote, position)
            if close < 0:
                parts.append(line[position:])
                index += 1
                if index == len(lines):
                    return None
                line, position = lines[index], 0
                end = len(line.rstrip("\r\n"))
                continue
            doubled = line.startswith(quote, close + 1)  # `""` is one quote character and the cell continues
            parts.append(line[position:close + doubled])
            quoted, position = doubled, close + 1 + doubled
            continue

        cut = line.find(delimiter, position, end)
        parts.append(line[position:cut if cut >= 0 else end])
        fields.append("".join(parts))
        parts = []
        if cut < 0:
            return fields, index + 1
        position, at_start = cut + 1, True


# A formula whose whole cell returns one piece of text (`="00123"`, inner quotes are `""`). An idiom used when exporting for Excel
# to keep leading zeros and long numbers.
_TEXT_FORMULA = re.compile(r'="((?:[^"]|"")*)"')


def _unwrap_text_formula(value: str) -> str:
    """Unwrap a `="00123"` cell to `00123`. Excel shows this cell as the text 00123, but the formula characters were emitted as is.
    Other formulas (`=A1+1`, `="a"&"b"`) are not evaluated, so they are left as is
    (Pinned by: test_spreadsheet.py::TestCsvExcelTextFormula)."""
    match = _TEXT_FORMULA.fullmatch(value) if value.startswith("=") else None
    return match.group(1).replace('""', '"') if match else value


def _lines_within_columns(lines: list[str], delimiter: str, quote: str, max_cols: int) -> Iterator[str]:
    """Cut each line with `_within_columns`. Lines containing the quote character, and lines inside a quoted cell opened on an
    earlier line (between lines with an odd number of quotes), are not cut — the delimiters inside are characters."""
    quoted = False

    for line in lines:
        if quote in line:
            quoted ^= line.count(quote) % 2 == 1
            yield line
        else:
            yield line if quoted else _within_columns(line, delimiter, max_cols)


def _within_columns(line: str, delimiter: str, max_cols: int) -> str:
    """Reduce a line with more delimiters than the column limit to `max_cols` cells + one cell marking that something beyond the
    limit was not empty. What lies beyond the limit is discarded anyway.

    A single line of 2 million commas was turned into 2 million cells, and a 45MB file took 5.2 seconds and 1.55GB (same rule as
    the column limit + 1 cell of XLSX) (Pinned by: test_spreadsheet.py::TestCsvLimitsAndHints).
    """
    if line.count(delimiter) <= max_cols:
        return line
    cut = -1

    for _ in range(max_cols):
        cut = line.find(delimiter, cut + 1)

    body = line.rstrip("\r\n")
    beyond = body[cut + 1:].replace(delimiter, "").strip()
    return f"{body[:cut + 1]}{'…' if beyond else ''}{line[len(body):]}"
