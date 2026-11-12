# Core decisions

Guide for agents and developers working on `src/opendockit/`: package structure, dependency direction and the decisions shared by every format. Usage, options and limits are in the [README](../../README.md), [options.md](../options.md) and [limitations.md](../limitations.md).

## Structure and dependency direction

```
src/opendockit/
├── __init__.py      # public API (convert, aconvert, IsolatedConverter, describe_images, OpenAIImageDescriber, ConvertOptions, exceptions)
├── cli.py           # command line: opendockit <file> [--json] [--describe]
├── converter.py     # size check → detect → extractor → clean_text → image filtering → render (fill placeholders, assembly, character count, limit verdict)
├── isolated.py      # isolated conversion for servers (IsolatedConverter): a new worker process per file, memory and time limits, a crash fails only that file
├── image.py         # image decoding, downscaling, PNG/JPEG re-encoding, image-file magic bytes
├── describe.py      # vision-model descriptions (LangChain ChatOpenAI). The only module that uses the network
├── detect.py        # magic-byte detection. The extension is used only to tell the text family apart (TXT, MD, CSV, HTML) (decisions in detect.md)
├── markdown.py      # clean_text · render_table · TableGrid · join_blocks — shared by every format
├── models.py
├── exceptions.py
└── extractors/      # per format `(bytes, ConvertOptions) -> Extraction`. Registered in EXTRACTORS in __init__.py
    ├── _shared.py   # Strict OOXML detection (`is_strict_package`, DOCX/PPTX), OOXML charts and SmartArt (PPTX/DOCX/HWPX), Office equations (OMML → LaTeX, DOCX/PPTX), automatic number strings, paragraph heads (`paragraph_head`), ruby text and list levels inside a cell (DOCX/HWP/HWPX), chart tables inside a cell (`embedded_table`), Hangul (HWP) bullet indent levels (`BulletNesting`, HWP/HWPX), symbol-font mapping (`symbol_font_text` — including ordinary letters in Symbol), character limit inside tables (`TableBudget`, DOCX/HWP/HWPX), DrawingML `a:br` line breaks (`drawing_paragraph_lines`, PPTX/DOCX/XLSX/charts), comment lines (`comment_line`, DOCX/PPTX/HWPX/Excel)
    ├── _equation.py # Hancom equation script → LaTeX (HWP/HWPX). The original script when it cannot be converted
    ├── docx.py
    ├── html.py
    ├── hwp.py
    ├── hwpx.py
    ├── image.py
    ├── pdf.py
    ├── pptx.py
    ├── spreadsheet.py
    └── text.py

tests/               # unit tests; sample builders are in tests/builder.py
docs/decisions/      # deliberate decisions per format (file list in "Deliberate decisions" below)
```

For use on a server see [server.md](../server.md) (conversion must go through `IsolatedConverter`).

- `extractors/*` imports only `markdown`, `models` and `exceptions`. Extractors share with each other only the decoding functions of `text` (including MIME header blocks and unpacking MIME mail) and `extractors/_shared.py` / `_equation.py`. Warning filtering is `models.quiet_warnings` (spreadsheet, image, html). Exceptions: DOCX `w:altChunk` uses `html.extract_html`, `is_mhtml` and `text.extract_text` as they are (altChunk row in [office.md](office.md)). The image extractor uses `image.split_tiff_pages` (multi-page TIFF, image section of [extractors.md](extractors.md)).
- An extractor registers images **as the original bytes** with `Extraction.add_image()` and puts the placeholder it gets back (`⟦IMG:<key>:n⟧`) at the right place in the body.
  Decoding, size filtering, de-duplication and limits happen in one place only, `converter._prepare_images`.
- `convert` does not use the network (deterministic, usable in a process pool). Vision-model calls live only in `describe.py`, and once descriptions are filled in
  `converter.render` assembles again from `Section.template`. A failed description is a warning, not an exception.
- The describer builds `langchain_openai.ChatOpenAI` itself (key from `OPENAI_API_KEY`, default model `describe.DEFAULT_MODEL`) or takes a LangChain chat model from the caller.
- Extractors produce **sections only**. NFC normalization, whitespace cleanup, section heads (`## Slide N`) and the limit verdict are done by `converter` — not in extractors.
  An extractor reports the character count with `Extraction.count()` for each unit it reads (PDF page, Excel/CSV row, PPTX slide, DOCX/HWP/HWPX top-level paragraph or table — large tables per cell through `TableBudget`)
  and stops reading when it gets the exceeded signal (True). TXT and MD are read in one go; for HTML only large documents get their front part read first.
- Tables must be built with `render_table`/`TableGrid`, so that the merged-cell rules (Markdown repeats vertically and keeps the first cell horizontally; a table that still has horizontal spans becomes HTML)
  do not differ per format. A format that has span information passes the span counts to `TableGrid` (it does not repeat values itself). Exception: Excel spreads merges over the cells and is always Markdown ([spreadsheet-tables.md](spreadsheet-tables.md)).
- User-facing text (exception messages, `warnings`) is English, fixed wording, and never contains file content or parser error text
  (the cause is chained through `__cause__`). `warnings` carry no more than the sheet name or page number needed to say where.

## Adding a new format

1. Add a value to `models.DocumentFormat` → 2. content detection in `detect.py` → 3. write `extractors/<format>.py` and register it in `EXTRACTORS` →
4. a sample builder in `tests/builder.py` and `tests/test_<format>.py` → 5. update the README table.

## Deliberate decisions (do not revert)

Only the common decisions (detection, limits, pictures, assembly, description) are here. The rest:

- [extractors.md](extractors.md) — per-format decisions (PDF, PPTX, DOCX, HWP/HWPX, spreadsheets, HTML, text, mail, images)
- DOCX / PPTX
  - [office.md](office.md) — DOCX and PPTX
  - [office-lists.md](office-lists.md) — numbering and lists
  - [office-math.md](office-math.md) — equations
  - [office-charts.md](office-charts.md) — chart value tables, axes, units
  - [office-limits.md](office-limits.md) — processing time
- PDF
  - [pdf.md](pdf.md) — PDF
  - [pdf-flow.md](pdf-flow.md) — reading order, two columns, labels beside a table, superscripts and subscripts
  - [pdf-rows.md](pdf-rows.md) — joining fragments of the same row, pulling up pushed-down lines
  - [pdf-tables.md](pdf-tables.md) — splitting table cells, header rows of continued tables, tables inside tables, checkboxes
  - [pdf-borderless.md](pdf-borderless.md) — wide-cell rows of borderless tables, the empty top-left cell, narrow gaps between cells
  - [pdf-limits.md](pdf-limits.md) — resource limits, page boxes, attachments
  - [pdf-images.md](pdf-images.md) — pictures drawn in strips, pictures inside table cells, photo grids
  - [pdf-fonts.md](pdf-fonts.md) — fonts without widths, symbol-font checkboxes, CMaps of built-in encodings, lower bound on fragment character width
  - [pdf-cells.md](pdf-cells.md) — joining lines of text in ruled-table cells, filtering highlighter marks, tables made of cell shading
- HWP / HWPX
  - [hwp.md](hwp.md)
- Excel / CSV
  - [spreadsheet.md](spreadsheet.md) — spreadsheets
  - [csv.md](csv.md) — CSV
  - [spreadsheet-tables.md](spreadsheet-tables.md) — splitting tables, side-by-side tables, summary cells, filling merges, the first column of the column limit
  - [spreadsheet-cells.md](spreadsheet-cells.md) — error cell values, pictures in cells, form controls, sheet SmartArt, default-password XLS, cell comments, the cell of a floating picture, time formats, parts the converter reads itself
- Text family
  - [encoding.md](encoding.md) — encoding detection
  - [mail.md](mail.md) — mail
  - [html.md](html.md) — HTML blocks (aside, quotes, checklists, item/value tables), exceptions for hidden elements, lazy-loading placeholder pictures, MHTML frames, inline SVG labels, `data:` URI pictures in MD
- [images.md](images.md) — images, picture descriptions (the describer), placeholders
- [detect.md](detect.md) — format detection (magic bytes, ZIP, OLE, telling the text family apart, MHTML, mail, extension warnings)

| Decision | Why | Pinned by |
| --- | --- | --- |
| Markdown tables repeat a vertically merged value on every row | Each row carries by itself which category its values belong to (no information loss) | `test_markdown.py::TestTableGrid` |
| Only a table that still has horizontally spanning cells after empty rows and columns are removed becomes an HTML table (rowspan/colspan, th in the first row, one row per line). A full-width merge of the title row at the top alone does not switch it. Cell text and the picture descriptions of that cell stay as written and only table tags are neutralized with `&lt;` (`markdown.neutralize_table_tags`) — the result is not rendered on screen but fed to an LLM, so escaping everything is noise. Applies to the formats that pass span counts to `TableGrid` (HWP, HWPX, DOCX, PPTX, HTML with a single header row) | A Markdown table cannot draw a horizontal merge, so nobody could tell what the empty cell in '\| 합계 \| \| 4개 과제 \|' (Total \| \| 4 tasks) was. A table with only vertical merges loses nothing through repetition, so it is not switched (existing output kept). Keeps a `</td>` in cell text from being confused with a cell boundary. In 295 real documents all 312 HTML tables had matching cell counts; 142 limit verdicts × 3, 0 violations | `test_markdown.py::TestMergedTableAsHtml`, `test_converter.py::TestAssembly::test_picture_in_an_html_table_cell_keeps_its_text` |
| A table inside a table (HWP, HWPX, DOCX; for HTML see [extractors.md](extractors.md)) is not flattened: it is handed to the outer `TableGrid.embed`, which leaves a placeholder (private-use characters) in the cell text. The outer table becomes HTML (a one-line HTML table inside the cell); a one-column box becomes paragraphs + a standalone table. While rendering text that goes somewhere other than cell text, such as a header inside a cell, the position is marked as one that cannot take it (None) and the table is `flatten`ed ('cell / cell'). A table inside a footnote or endnote is `_shared.note_table` ('cell / cell; cell / cell'). Copies made for merged cells drop picture and table placeholders (`merged_copy`). The caption of an inner table stays with the table wherever it is | 'cell / cell' lost the row and column structure. A placeholder that leaks into a footnote is never resolved and is erased, so the content disappears. A table in a merged cell came out once per row. The inner table's caption was dropped, so a picture in the caption cost a description and then vanished | `test_markdown.py::TestMergedTableAsHtml` (4 nested cases), `test_hwp.py::TestNestedTables` (including footnotes), `test_quality.py::test_docx_nested_table_keeps_its_rows_and_columns`, `test_hwp.py::TestTableInsideFootnote`, `test_office_symbols_fields_notes.py::TestDocxTableInsideFootnote` |
| PyMuPDF and pyhwp are not used | AGPL | — |
| clean_text removes first, then NFC | In the other order the characters on both sides of a removed one combined anew and NFC broke (real PDF) | `test_markdown.py::test_result_is_nfc_even_when_removed_character_separated_combining_mark` |
| Private-use bullets of Symbol/Wingdings (U+F0B7 etc.) are not removed but turned into Unicode symbols | The list markers of a Word PDF disappeared | `test_quality.py::test_symbol_font_bullets_become_unicode_bullets` |
| Only Latin ligatures (U+FB00–FB06) are expanded (`ﬁ`→`fi`). NFKC is not used | 'ﬁrst' in a typeset PDF became a different word from 'first' (964 places in one book PDF). NFKC would also change ①, ㈜ and ² in Korean documents | `test_markdown.py::TestCleanText::test_expands_latin_ligatures_but_keeps_other_compatibility_characters` |
| `TableGrid` limits span counts and builds the grid only from rows and columns where cells are placed. Spans are clipped at render time to the table's real extent (the last row/column where a cell was placed or that was announced with `reach`), and the number of rows dropped by the grid cell limit is reported through `omitted_rows()` so the extractor warns (`_shared.omitted_rows_warning`, HTML included). A cell that arrives without an address (`place_next`, HTML) looks for a free position starting at the column placed last in that row | Trusting span counts and cell addresses lets a few KB of input use seconds and gigabytes. It is blocked in one common place, not per format. rowSpan=5000 in a 2-row table made 4,999 fake rows, and gridSpan=10000 in the top cell left only 499 of 1,000 rows (no warning). Searching from column 0 took 56 seconds for one row of 50,000 cells | `test_markdown.py::TestTableGridLimits`, `test_office_robustness.py::TestSpansPastTheTable`, `test_hwp_robustness.py::TestSpansPastTheTable`, `test_core_html.py::TestRowsLeftOutByTheGridLimit`, `test_markdown.py::TestTableGridLimits::test_cells_placed_in_order_on_a_long_row_take_linear_time` |
| The character limit (`max_chars`) is a criterion for stopping, not for cutting. Once exceeded, the result comes back with `exceeded=True` and the remaining pages, rows and pictures are not processed | Spending page analysis, picture decoding and vision-model calls on a document that will be rejected anyway is waste (130,000-row Excel 7.6 s → 0.4 s; 40 scanned pages 40 calls → 7). The former `max_output_chars` (cutting) and `truncated` were replaced by this | `test_converter.py::test_exceeding_the_character_limit_sets_exceeded_and_cuts_the_text`, `test_pdf.py::test_stops_reading_pages_once_the_character_limit_is_exceeded`, `test_image.py::test_description_stops_once_the_character_limit_is_exceeded` |
| `exceeded` must be exactly "character count of the full conversion > limit". Mid-way verdicts use only values that are never larger than the real one: during extraction `markdown.solid_length` (excluding whitespace, placeholders and characters that will be removed; NFC), during description the body assembled with every remaining picture erased including its alt text (`describe._floor_length`; when there is clearly room, `converter.image_length_bound` skips the assembly). `render` does not inherit `result.exceeded` and decides again every time | Counting high blocks documents at or under the limit as exceeded. Counting by raw length made 4 documents with N = L come out exceeded, and judging by the body length at an intermediate point made 48 documents come out exceeded whose pictures were later erased as decorative and so shrank | `test_converter.py::TestLimitIsExact`, `test_image.py::test_placeholders_of_images_that_turn_out_decorative_do_not_count_against_the_limit`, `test_retry_does_not_count_already_described_images_twice`, `TestDecorativePictureWithAltText::test_limit_is_exact_when_a_picture_with_alt_text_gets_any_answer` |
| `char_count` includes picture descriptions and `render` counts again every time. The total of a batch (several files) is added up by the caller — this library works per single file | It is the criterion the front end uses to limit uploads | `test_image.py::test_char_count_includes_image_descriptions_once_described` |
| When the limit is exceeded, `sections[].text` is cut at the same point as `text` (`_cut_sections`; sections after the cut have empty text). The section list and `template` stay as they are | Only `text` was cut and the sections kept everything, so chunking and source attribution received content beyond the limit. Erasing the originals would shrink the character count on re-assembly and flip `exceeded` | `test_converter.py::TestAuditedAssemblyGaps::test_sections_are_cut_where_the_text_is_cut` and 2 more |
| In documents that are not Markdown (anything but TXT/MD) an unpaired last ``` line is neutralized as `\```` (`clean_text`). Paired fences stay code blocks | A ``` in a single paragraph turned every section after it into a code block and whitespace cleanup stopped | `test_markdown.py::TestUnclosedCodeFence`, `test_converter.py::TestAuditedAssemblyGaps::test_stray_code_fence_in_a_docx_does_not_swallow_the_rest` |
| A line holding a picture placeholder is a table row (folded to one line) only when it starts and ends with `\|` (including MD `\|a\|b\|`) or has the shape `<tr>…</tr>`. Known accepted gap: a paragraph of the form `\|…\|` is taken for a table row | A picture description was folded to one line and glued onto a paragraph that starts with `\|` or `<tr>`. Only lines ending in ` \|` were looked at, so a `\|a\|b\|` table broke at the picture | `test_converter.py::TestAuditedAssemblyGaps::test_paragraph_that_starts_like_a_table_row_gets_a_picture_block`, `test_core_text_and_mail.py::TestPictureInACompactMarkdownTable` |
| Superscripts and subscripts become `10^6`, `10^{-3}`, `H_2O` in one place, `_shared.format_script`. Only scripts of at most 10 characters that start with a letter, digit or sign are marked; footnote marks, symbols, long text and ordinal suffixes after a number (21st) stay as they are. Scripts containing Hangul, Han characters or kana are not marked (`1,000원`, same rule as PDF). Sources: HTML sup/sub, DOCX w:vertAlign and LibreOffice w:position + small w:sz (at most 0.8× the paragraph size), PPTX baseline, HWPX hh:supscript/subscript, HWP character-shape attribute bits 15 and 16, XLSX rich text (full load only), XLS font escapement, `<Sup>`/`<Sub>` of Excel 2003 XML (`spreadsheet._xml_rich_text`) | Glued on as plain text, 10⁶ read as 106. Marking `[1]`, `†` and `: 125` as well produces hundreds of noise marks per wiki document. The HWP script bits (taken from hwplib) were confirmed on real files against the HWPX twin of the same document (hh:supscript) | `test_text_html.py::test_html_superscript_and_subscript_are_marked`, `test_html_superscript_with_hangul_or_hanja_is_left_as_plain_text`, `test_html_ordinal_suffix_after_a_number_is_not_marked_as_superscript`, `test_office.py::TestSuperscriptAndTextBoxes` (including 2 LibreOffice position cases), `test_hwp.py::TestSuperscript`, `test_spreadsheet.py::TestSuperscriptInCells`, `test_core_spreadsheet.py::TestXmlSpreadsheetScripts` |
| Private-use characters are removed in the supplementary planes too (U+F0000–10FFFD) (`markdown._PRIVATE_USE`). Mapping Hancom font ordinals (U+F02B1–) → ① is not done for lack of evidence | Characters of Hancom-only fonts stayed as meaningless tokens | `test_markdown.py::TestCleanText::test_removes_supplementary_private_use_characters` |
| PDF superscripts and subscripts are inferred from position (`pdf._scripted_text`): characters at most 0.8× the line's ordinary character size whose baseline is off by at least 0.2 upward or 0.1 downward (relative to the character size). Only marks are inserted into pdfminer's line text; lines whose character order differs from the line text, table cells and pages extracted the simple way are left alone. Runs containing Hangul or Han characters are not marked | A PDF has no script information. Rebuilding the line text changed the order and spacing of overlapping accents. A raised unit in a designed document became '12,345^{백만원}'. Of 2,377 marked lines in 20 public papers, a sample of 45 lines had 0 misjudgments | `test_pdf.py::TestSuperscriptByPosition` |
| `convert` takes bytes, bytearray and memoryview; anything else is a `TypeError` (pointing to convert_file). A ConvertOptions in the file-name position, a file name that is not a str and an options value that is not ConvertOptions are also a `TypeError` before conversion (convert, aconvert, convert_file; `converter._check_arguments`) | Path strings and memoryviews were rejected as a 'corrupted file'. `convert(data, ConvertOptions(...))` raised TypeError only after running the whole conversion (222 s) | `test_converter.py::TestInputTypes`, `test_core_mbox_markdown_and_api.py::TestArgumentsInTheWrongPlace` |
| `ConvertOptions.__post_init__` rejects values below the limit minimums (`models._OPTION_MINIMUMS`) with a `ValueError` (English). 0 for `max_chars`, `max_images`, `min_image_side` and `pdf_time_budget` means 'allow none'; `max_pages`, `max_sheet_cols`, `max_file_bytes` and the like must be at least 1. The type is checked too (`models._checked_number`, `TypeError`): None only for `max_chars`, `max_pages`, `max_images` and `pdf_time_budget`; a whole-number float for an integer limit (`5e5`) is accepted and converted to int; bool, strings and fractions are rejected. NaN and infinity are a `ValueError` (no time limit is `pdf_time_budget=None`) | `max_chars=-5` cut the last five characters and became 'exceeded', and `max_images=-1` dropped only the last picture. `max_sheet_cols=None` rejected an Excel file as 'corrupted' and `max_chars=2.5` was a TypeError in the middle of conversion. `pdf_render_scale=nan` passed the minimum comparison and a healthy PDF became 'corrupted' | `test_converter.py::TestLimits::test_negative_or_meaningless_zero_limits_are_rejected_when_options_are_made`, `test_limit_of_the_wrong_type_is_rejected_when_options_are_made`, `test_whole_number_float_limit_becomes_an_integer`, `test_non_finite_number_is_rejected_when_options_are_made` |
| The single-cell top row of a two-column table is a table title (a paragraph above the table; `markdown._title_of`, common to `render_table` and `TableGrid` HTML) only when just the left cell is filled, the row below has non-numeric text in both cells and there is a numeric cell within the 5 rows below that. HTML, which merges multi-row headers itself, uses the same test through `markdown.table_title`. A ruled table in a PDF that continues from the previous page uses `render_table(merged_titles_only=True)` — a title only when the remaining cells are merged cells (None). Known accepted gap: the title row of a two-column table without numeric cells stays a header row | Titles were taken out only for 3 or more columns, so '부서별 인원 현황 \| ' (Headcount by department) became the header row and '부서 \| 인원' (Department \| Headcount) a data row (XLSX merged title, CSV, HTML colspan=2). A '구분 \| ' header row and two-column tables with text only stay as they are | `test_markdown.py::TestTwoColumnTitleRow`, `TestTitleRowOnlyFromMergedCells`, `test_text_html.py::TestTitleRowInsideMultiRowHeader::test_two_column_table_with_a_spanning_title_row` |
| `clean_text` does not collapse consecutive blank lines inside paired fences (```) (TXT/MD and every other format alike). After an unpaired last fence it does. Assembly of a section with pictures (`converter._render_template` → `_collapse_blank_lines`) skips the inside of fences by the same pairing | Two blank lines between functions in an MD code block became one. Assembly collapsed the blank lines of the whole section again, so the same happened only in sections with pictures | `test_markdown.py::TestBlankLinesInsideCodeFences`, `test_core_image_and_assembly.py::TestBlankLinesInCodeNextToAPicture` |
| Warning filtering inside the library uses `models.quiet_warnings` instead of `warnings.catch_warnings` (narrow category and module; inserted under a lock with a use count and only that entry removed at the end). Known accepted gap: while the block is open, warnings of the same category and module from other threads are ignored too, and overlap with a `catch_warnings` outside the library cannot be prevented (Python 3.12) | `catch_warnings` is not thread-safe, so after overlapping XLSX conversions the ignore filter for openpyxl's UserWarning stayed in the process for good. Serializing whole conversions with a lock would make concurrent conversions sequential | `test_core_spreadsheet.py::TestOpenpyxlWarnings::test_overlapping_conversions_on_threads_leave_the_filters_as_they_were` |
| On a server, convert with `IsolatedConverter`: each worker gets its own `ProcessPoolExecutor(max_workers=1, max_tasks_per_child=1)`, started with forkserver (this package preloaded; spawn when unavailable). On a timeout, `BrokenProcessPool` or a cancelled request the worker is killed directly through the private `_processes` and the pool is rebuilt. On shutdown, `shutdown(wait=True)` is awaited in the default thread pool. Memory or time overruns and a dead worker are `DocumentTooLargeError` (`TOO_COMPLEX_MESSAGE`) | Run on a server thread, a memory bomb or a crashing C extension takes the whole server down, and a thread cannot be stopped. With fork, the child can hang in a server that runs DB and asyncio threads. Without preloading a new worker takes 0.2 s, with it 5 ms, so a new worker per file gives the memory back. With several files in one pool, killing one fails the others too. `shutdown(wait=False)` emptied `_processes` while the management thread was refilling workers and that thread died with a TypeError (3 times per 15 tests). Known accepted gap: the forkserver preload list is one per process, so it overwrites another code's list | `test_isolated.py` (the memory limit only on Linux — macOS does not accept `RLIMIT_AS`, so it is skipped) |

## Commands

Run from the repository root.

```bash
uv sync                                               # install dependencies
uv run pytest                                         # unit tests
uv run pytest -n auto                                 # unit tests in parallel
uv run ruff check
uv run opendockit <file> [--json] [--describe]        # --describe calls the vision model (needs OPENAI_API_KEY, costs money)
# To check the shape of a conversion, convert a real document with the CLI above (do not commit personal or copyrighted documents)
```
