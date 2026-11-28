# Known limitations

This page lists what OpenDocKit does not restore, restores only approximately, or handles in a way that may surprise you, grouped by format. Several entries also state the exact rule that is applied, so that you can predict the output. What *is* restored is in [formats.md](formats.md); the reasons behind many of these trade-offs are in [design.md](design.md) and [decisions/](decisions/core.md).

## Platforms

- **Linux, macOS and Windows are the supported platforms.** The full test suite runs on all three for every change.
- **Windows on Arm with an Arm64 Python, a 32-bit Python on Windows and an Intel Mac install an older cryptography.**
  pdfminer.six, which reads PDFs, needs cryptography, and its newest releases have no wheel for these: none for Windows
  on Arm since 46.0.4, none for 32-bit Windows or Intel macOS since 49.0.0. pip and uv take the newest release and build
  it from source, which needs Rust, a C toolchain and OpenSSL, so `pip install opendockit` fails on a machine without
  them. `pip install --prefer-binary opendockit` (with uv, `uv pip install --no-build-package cryptography opendockit`)
  takes the newest release with a wheel instead: 46.0.3 on Windows on Arm, 48.0.1 on the other two, without the fixes
  made since. Installed so on GitHub's runners for each, the samples convert to the same bytes as on x64, and PDFs
  encrypted with AES-128, AES-256 and RC4 are read. On Windows on Arm an x64 Python installs the newest release, as
  on x64. The MCP configurations of this project — the Claude Code and Hermes plugins, the MCP Registry entry and the
  client configurations in the READMEs, [integrations.md](integrations.md) and the launcher's README — pass
  `--no-build-package cryptography` to uvx, which takes the same release wherever the newest has a wheel. Where no
  release that pdfminer.six accepts (36.0 or later) has a wheel — Linux on s390x, riscv64 or 32-bit x86, FreeBSD — uv
  then finds nothing to install, where without the option it built the newest release from source: there, leave the two
  arguments out of the configuration. CI runs on x64 Windows and Linux and on Apple Silicon macOS, not on these. The extras `openai`, `llamaindex` and `lightrag` need
  tiktoken, which has no wheel for Windows on Arm or 32-bit Windows in any release, and `heic` needs pillow-heif, which
  has none for 32-bit Windows: there they install only where those can be built.
- **A large malformed page on Windows is still read by the older parser.** Reading malformed HTML the way a browser does
  needs an HTML5 parser. lxml bundles one (libxml2 2.14) on Linux and macOS, but its Windows wheels bundle libxml2 2.11, so
  there html5lib builds the tree instead. html5lib is written in Python and is quadratic on three shapes — the text an
  unclosed `<textarea>`/`<xmp>`/`<plaintext>` swallows, how many of them are left open, and nesting — so a page — or one body of a mail — over
  256 K characters, nested deeper than 1,000 elements, or leaving more than 16 raw-text elements open, goes to the older parser
  after all. For those pages — and only those — text after the end of the page, a
  second concatenated document, the content of raw-text elements and further parts of a mail can go missing, and the
  result carries a warning saying so. EPUB chapters, and HTML chunks imported into a DOCX, are read by the same code.
- **`IsolatedConverter` runs on all three platforms.** Its memory limit applies on Linux (`RLIMIT_AS`) and on Windows (a job
  object's `ProcessMemoryLimit`); macOS accepts neither, so there the time limit and process isolation remain. A worker
  leaves with a server that was killed outright everywhere: on POSIX by watching the server's pid, on Windows by waiting on
  a handle to it.
- **A worker costs more on Windows.** There is no fork server to preload this package into, so every file pays a full
  interpreter start and `import opendockit` — measured on Windows 11 at 0.2 s for a small text file on Python 3.12 and
  0.3 s on 3.14, against a few milliseconds elsewhere — and then the import of the reader its format needs (a DOCX
  0.1 s more). macOS and the BSDs under gevent or eventlet spawn each worker too, about 0.1 s a file
  ([server.md](server.md)). It is the price of a fresh worker per file there, and it does not apply to `convert` itself. The time
  limit counts it, so a `timeout` of a few seconds leaves that much less for the conversion.
- **Building an `IsolatedConverter` on Windows needs a `__main__` guard.** Workers are started by spawning a new interpreter
  that re-imports the program, so a converter made at module level builds converters without end. Make it inside
  `if __name__ == "__main__":`, or in a function a server calls.
- **Windows PowerShell 5.1 re-encodes what the command line prints.** The command line writes UTF-8 with LF line ends
  on every platform, and `cmd.exe` and other programs get those bytes. PowerShell 5.1 does not pass them on: a
  redirect (`opendockit report.hwp > report.md`), a pipe and a variable (`$text = opendockit report.hwp`) decode the
  output with the console's code page and write it again, with CR LF. Under a code page that is not UTF-8 — cp949,
  the Korean default — Korean text comes out garbled (`글머리` becomes `湲癒몃━`). Write the file with
  `-o report.md` instead, which the program writes itself; or run `[Console]::OutputEncoding = [Text.Encoding]::UTF8`
  in the session first, or redirect from `cmd.exe`. A pipe or a variable needs the second.

## Integrations

- **Every integration converts offline** (LangChain, LlamaIndex, LightRAG, the MCP server, Dify, Open WebUI). Pictures and scanned pages stay placeholders; to have them
  described, convert with a describer yourself ([integrations.md](integrations.md)).
- **The MCP server** speaks stdio only. Without `--root` it reads any file its user can read, and where `--root` is given,
  the folder is checked again as the file is opened on POSIX but only around the open on Windows, which narrows the moment
  a folder could be swapped without closing it. On Windows a network share is read only when it is given as a `--root`,
  with or without other roots. In `sections` mode a page, sheet or slide without text is left out, so
  section numbers can have gaps.

## General

- **Charts and shapes drawn as vectors** (PDF charts drawn with lines rather than as images, Office shapes) cannot be extracted as pictures. Native charts of PPTX, DOCX, HWP, HWPX, ODT, ODP, XLSX sheets and chart sheets are converted to data tables (XLS charts are not converted; a chart sheet, and an otherwise empty sheet holding only a chart, give a warning). New chart types (waterfall, treemap and other chartex types) are handed over as the fallback picture stored in the file when their values cannot be read (not verified with real files). However, a PDF page with no characters at all and only curves (a design PDF whose text was converted to outlines) is sent to the vision model as a whole page, like a scanned page.
- **WMF and EMF pictures** (common in HWP and legacy Office) are not rendered, on Windows either. They stay as `[Image]`, and a warning reports how many there were. Pillow draws them only on Windows, through GDI, and a picture is read only in one of the raster formats decoded in this process ([images.md](images.md#normalization)).
- **Superscripts and subscripts** are written as `10^6`, `10^{-3}`, `H_2O`. An ordinal suffix after a number (21st) and a superscript containing Hangul or Hanja ('1,000원') are left as they are. In PDF they are inferred from font size and baseline position, so they can be wrong, and they are not marked inside table cells. In Excel only scripts on part of a cell's text are marked (not in large XLSX read in streaming mode).
- **Unsupported formats**: SVG, Word XML (Word 2003 XML and the like), ODF single-file XML (.fodt, .fods, .fodp) and Hangul XML (HWPML, .hml) raise `UnsupportedFormatError`, as do Excel Binary Workbooks (.xlsb), Hangul 3.x, PowerPoint files saved as Strict Open XML (a Word document or an Excel workbook saved that way is read from a copy with the Transitional names; checked against one file Word saved and three Excel saved, while the tests build their Strict files themselves) and the other OpenDocument kinds (drawings, formulas, databases). Save Hangul XML as HWPX and upload that. Outlook messages (.msg) are not read either: save the mail in Outlook as MHT, HTML or text. A UTF-8 note that merely contains `%PDF-` and PDF structure words is read as text when it cannot be read as a PDF (known accepted gap: a CP949 note is still reported as a corrupted PDF).
- **Partly broken files** (a single slide, speaker notes, an HWP section stream or its DocInfo stream, an HWPX style part): only the broken part is skipped, with a warning. In a very large table (grid over 5 million cells) the lower rows are discarded with a warning.
- **Table merges are capped.** A single cell's merge is expanded up to 10,000 cells and the merges of one table up to 500,000 cells in total; beyond that the value is kept in the first cell only. Trusting the merge counts written in a file would let a crafted file of a few KB exhaust memory. Normal documents do not reach these caps.
- **Crafted files can still be slow.** The limits bound memory and the size of the output, and a review removed the cases found where a small file took tens of seconds, but some inputs still take time that grows faster than their size (the decision records name them under "Known accepted gap"). For untrusted uploads use `IsolatedConverter`, which ends a conversion after its time limit.

## PDF

### Tables

- **Borderless tables** are restored when 3 or more short cells line up in columns over 3 or more lines, when the table
  is set between horizontal rules (two columns and three rows are enough there), or when a tagged PDF states the table
  and the page agrees with its tags. For the first kind:
  - A row with one wide cell is attached above that table.
  - Table-like rows that do not qualify as a table, and two short rows with aligned columns (such as `적용일 | 유예기간` in an official notice), are read one line per row.
  - A row whose cell text wraps onto a second line keeps the table by joining the lines with `<br>` inside the cell (including rows whose other cells are vertically centered or bottom-aligned).
  - A row in which only one cell other than the first column is filled (a subtotal amount) stays in the table.
  - Two adjoining tables with different column counts are split where the columns stop matching.
  - A table inside one column of a two-column page is found by the same criteria.
  - Anything sparser becomes line-by-line text.
  - If every data row has a wide cell exceeding 1/4 of the page width (a long "remarks" column and the like), the rows are not turned into a table; each row becomes one line with values joined by spaces.
- **Cells of a ruled table filled only with color, without text** (Gantt-style schedules) are marked `■`. Whole-row and whole-column shading, header shading, light gray cells, and scattered highlighted cells without adjacent filled cells are not marked. A schedule bar spanning the whole period, or a light gray bar, can therefore come out as empty cells.
- **A box with only an outer border and horizontal lines, no vertical lines** (an equation cell of a form and the like) cannot be read as a table; it is read as body flow and words are broken per cell. If the original DOCX or HWP exists, that gives a better result. In a three-line table whose cells have zero top and bottom padding, a row whose values are top- or bottom-aligned can have the wrapped first-cell line split off.
- **A table row split at the page end** remains as a row of the next page's table (fragments between empty cells); rows are not merged across pages.
- **A borderless table with only its header shaded** that runs over a page: the overflowing rows come out as paragraphs outside the table and the order of cell text can change (the content remains).
- **In a borderless table**, a row whose wide first cell merged with another cell, a row in which several columns wrapped together, and a row whose baseline shifted because of a substitute font can break the table or scatter cells into line-by-line paragraphs (the content remains).
- **Tables and pictures on two-column pages** are placed between the paragraphs of the same column. A table at the top of a column can come before the neighboring column.

### Text and reading order

- **Headings** are told from the body by size (1.15 times the body size or more) and weight (a short bold paragraph at
  about the body size). A short bold label standing alone at the body size, such as a question of a form, can come out
  as a heading; a heading that differs from the body only by its font family or colour does not.
- **A page with malformed page attributes**: if even one page is like this, the whole document is read by plain extraction (no table or heading detection) with a warning.
- **Right-aligned lines between left-aligned lines** (dialogue transcripts, ※ notes) are read top to bottom; but when the right-hand lines all have the same width they are taken as side-by-side text boxes and read column by column.
- **Footnotes are linked only where the page proves it** (see [formats](formats.md#pdf)). A page with a mark in a table cell,
  a symbol mark (`*`, `†`), notes numbered with plain (not raised) numbers, notes without a footnote rule, a raised number that
  could be either a mark or an exponent, a raised or small digit shown as plain text, small digits in a table, a word with a note
  number glued to it ('conv3', 'Books2'), a line right under the notes that is not a note (a footer), two notes set on one
  line or a typed `^` keeps `135^1%` and the note as plain lines at the bottom of the page
  — an unlinked note was judged safer than a wrong one. So does a page after one that left a mark with the same number
  unlinked (its note may have moved to the next page). The end of a note carried over from the previous page stays as plain
  text above the notes of the page. `include_footnotes=False` does not remove PDF notes.
- **Line breaks inside a PDF paragraph are not joined with a space.** Korean PDFs often break lines at any character, so joining with a space would split words. In a PDF made by Hangul, where the file itself shows that a line ended inside a word, the two lines are written as one; any other PDF keeps its lines.
- **Word-spacing restoration is an estimate.** For text placed word by word, the actual gap where the font changes is estimated and a space is inserted. A thin space before a symbol (a parenthesis and the like) following Hangul is not restored (`Test(*) 진행` → `Test (*)진행`).
- **Slanted text** (about 6 degrees or more) is treated as a watermark and removed from the body. Small slanted labels inside figures are removed with it.
- **Text drawn rotated 90 degrees within a page** (identifiers in the margin of a paper, a table laid sideways on a portrait page) is read rotated and placed at the end of that page as line-by-line text. Table structure is not restored. Text written rotated inside a cell of a ruled table is read as that cell's text (except cells mixed with horizontal text).
- **Scripts whose glyphs are not their characters** (Hindi and the other scripts of India, Thai, Arabic): the text is taken
  from what the PDF says its glyphs stand for (`ActualText`), which a browser and LibreOffice write. A PDF that does not
  say it, from a program that draws a vowel sign before its consonant as a glyph without a name, loses that sign; and a
  PDF that says it wrongly is read wrongly.
- **Arabic, Hebrew, Persian and Urdu** are read in their order of reading (the page holds them backwards). Left as known
  gaps: a font that names its glyphs wrongly cannot be read by anyone — the Arabic typeface of United Nations documents
  draws a letter as a shape plus dots and names only the shape, so 'في' comes out as 'ني' (`pdftotext` and copying from a
  viewer give the same); a table is read from its right column when the page runs from right to left, so a table laid
  out from the left in such a document comes out turned round when the PDF has no tags; in a document made by Word, two numbers with a hyphen between them may come in either
  order ('2030-2015'), because Word sets them by how they were typed; and a page read by simple extraction (time budget,
  unreadable structure) keeps the order pdfium gives it.
- **Vertical writing** (pages set in WMode 1 fonts): characters are scattered one by one and the column order (right to left) is not kept.
- **Lines drawn separately per font.** A PDF that draws Hangul and digits on the same baseline as separate runs per font (Cairo output and the like) is read as one line by re-collecting the characters in x order. Characters drawn on top of each other (fake bold, overlapping text boxes) are not merged.
- **Two pages on one sheet.** A sheet of the A or B series on its side that holds two pages side by side is read page by page, the left page to its end and then the right. On a sheet of other proportions (Letter, Tabloid), or where each page is placed as a Form XObject, lines at the same height of the two pages can still be joined into one line.
- **A PDF exported from slides** comes out as scattered fragments of text boxes and diagrams (reading order is right, but "label — value" relations are split into lines). If the original PPTX exists, uploading it preserves the structure.
- **Removal of repeated headers** targets the margins (10% of the visible area's height) and the top and bottom two rows of the page.
  - A removed line that holds a word found nowhere in the text left of the document (lines, tables, text read sideways, form values) is written once, before the text of the first page that has it (after it for a line of the lower half): the title of a document that is one long list, the name of a journal, a case number. So is a line that says little but says it nowhere else ('Company confidential', the address of a website). What is written is always one stretch of the line as it stands, without the number of the page and what belongs to it; the label of a page in a language the reader has no page words for can stay at its end ('Hafenbericht Nord Seite'). A line with text on both sides of its page number is not written, nor is a title whose every word stands in the body, as part of a longer word too.
  - For margin lines that differ only by a number from page to page, digits are ignored only in page-number-shaped tokens ('3', '3/10', '3쪽', '2024-01-05'), so value lines such as '소계 12건', '실지급액: 3,450,000' or '증감률 6.8%' are kept.
  - A line is removed when it repeats on at least half of all pages, or on at least 2/3 of the odd pages or of the even pages (left and right running heads of a book).
  - The title and signature block of a 4-page bundle of forms are kept. A header that changes per chapter ('“PROPERTY” 139') is kept.
  - A line in a font larger than the body ('2025년 3월 영업 실적') counts as repeated only when the digits match too. Values in a column are not removed when rows of the same shape continue at the same position in the body.
  - Lines attached to a ruled table (unit, notes) and "item value" rows are kept. But on pages without tables, the same body line at the very top or bottom of every page can be removed; turn this off with `remove_repeated_page_lines=False`.
- **Hidden text.** Text hidden from view — white text, the same color as the background, behind a picture — is included in the body if it is in the text layer. It cannot be told apart from an OCR text layer in invisible render mode, so it is not filtered.
  - Only characters outside the visible area (CropBox), characters drawn outside the clipping path in force and characters of size 0 are removed. A page with only such characters is an empty page (it is not revived by plain pdfium extraction).
  - A character outside its clipping path is removed only when no part of it can be inside. A letter cut by the edge of the clip stays, and so can the one or two hidden letters next to it (after a cell cut off in a spreadsheet, the letter the edge runs through and its neighbour), and so does text hidden by a clip that is not a rectangle where it lies inside the box of that clip, text hidden by a clip made of text (render modes 4 to 7) or by a soft mask, and invisible text (render mode 3) wherever it is. Text is also kept where the reader cannot tell where its ink is: in a Type3 font, a font without /Widths or without a /FontBBox, vertical writing, and a font of several bytes set with word spacing. A page read with plain pdfium extraction keeps its clipped text.
  - Text hidden with size 0 is removed, but text with only zero width, such as horizontal scaling 0 (`0 Tz`), is not filtered out (it cannot be distinguished from a font without /Widths).
  - A value corrected by writing other text over the same place (an amount covered with a white box and rewritten) can merge both texts into one line (for example `5678` over `1234` → `12345678`).

### Forms, annotations and attachments

- **AcroForm field values** are appended at the end of the page as `name: value`, not next to their label. For a group of check boxes with the same name, and for radio buttons, only the chosen value is included (the `/Opt` item when present). When the field name is an internal name (`Text1`), the reader has to infer which label it belongs to. **XFA forms** cannot be read and give a warning.
- **Text written with a viewer's "add text" tool** (Acrobat Fill & Sign, macOS Preview; FreeText annotations) is appended at the same place as `label: value`, taking the line immediately to its left on the same row as the label. On a page with a very large number of such annotations the label search has a per-page work bound; past it the remaining values are written without a label.
- **Annotations** (sticky notes, shape descriptions) are not included. Only AcroForm values and free text written with "add text" (FreeText) are appended at the end of the page.
- **Attached files** (EmbeddedFiles, file-attachment annotations) are not read; a warning reports only their names and count. Convert attachments separately.

### Scans and pictures

- **A scanned PDF that already has an OCR text layer** uses that text as is. It is not read again even if the OCR quality is poor. However, when the text layer is 100 characters or fewer (a scanner-app stamp, a fax header) and an image covers the page, the page is treated as a scan and sent whole to the vision model.
- **Scan strips.** When a scanner stored a page as several strip images, the strips count as one scan if they cover the page without gaps (only the text layer if there is one; otherwise the whole page as an image). Photos with gaps between them, and photos filling the page as a grid (2×2 and so on), are each a picture.
- **Text laid over a picture**: the picture is cropped as displayed, so the overlaid text can appear twice — once in the body and once in the picture's description.

### Limits and performance

- **A PDF with many vector shapes** (design guidebooks and the like) takes pdfminer more than 2 seconds per page. Pages past the time budget (60 s by default) are read quickly as text only.
- **A single slow page cannot be interrupted.** The time budget is checked only between pages. In a server, set a separate per-job timeout ([server.md](server.md)).
- **A page with more than 50,000 drawing elements** is read as text only, without table or heading detection, with a warning (this keeps one page from using several GB of memory).
- **A page with more than 500,000 drawing elements** (including the same Form XObject drawn over and over), **or with forms nested deeper than 32 levels**, is not read (empty page) and gives a warning. Merely opening such a page uses several GB.
- **A PDF that pdfminer cannot open and is read by plain pdfium extraction** cannot have the decompression limit and the drawing-element limit applied before a page is opened (the character limit is still kept page by page).
- **Abnormally large ranges in a font's glyph-width or ToUnicode tables** (malicious files that expand to tens of GB) are partly cut, with a warning.
- **A page with one string of more than 2 MB, more than 250,000 glyphs or more than 2 million characters of text** is read as text only, like a page past the time budget. A page whose font gives one character code more than 64 characters is read by pdfminer alone, or not at all, and is then listed with the pages not read. A page of millions of glyphs in short strings is not bounded: convert files nobody vouches for in `IsolatedConverter` ([server.md](server.md)).
- **Broken content streams.** When the content stream of a page with no text or pictures at all cannot be decoded (damaged compression), a warning says the content could not be decoded. The content of that page cannot be recovered.
- pdfminer logs a WARNING such as `Could not get FontBBox…` for each piece of broken font information. This does not affect the result; if the server log is noisy, lower it with `logging.getLogger("pdfminer").setLevel(logging.ERROR)`.

## Spreadsheets (XLSX, XLS, Excel 2003 XML, CSV)

### Formulas and values

- **Excel formulas use the stored calculated value.**
  - A file made by a tool other than Excel or LibreOffice (openpyxl and the like) that has no calculated values at all gets its formulas copied as written (`=A2*B2`), with a warning.
  - When only some formulas lack a value, only those cells are written as formulas, with a warning per sheet.
  - A file from tools such as Apache POI or xlsxwriter that wrote only 0 in the result slot without calculating: XLSX gives the formula as written, XLS gives an empty cell, with a warning (large XLSX included).
  - Excel 2003 XML also writes a formula without a value as the formula text.
  - The 0 in a file where POI only filled values into an Excel template cannot be detected.
- **Cell formats** are imitated only for: percent, thousands separators, scaling by thousands (`#,##0,`), digit padding (`000000`), decimals, currency, prefix and suffix text, text-only formats (`"매우 좋음"`), the text section of a text cell (`@" 님"`), optional decimal places (`0.###`), and dates. Fractions (`# ?/?`), scientific notation (`E+`, `E-`) and conditional formats use the raw value. Superscripts and subscripts in Excel 2003 XML rich text are written as `m^2`, `H_2O`.
- **Times** follow the format: `h:mm` → `18:03`, `h:mm AM/PM` → `6:03 PM`, `[$-412]AM/PM h:mm` → `오후 2:30`, `h"시" mm"분"` → `14시 30분`. Seconds appear only when the format has seconds; `mm:ss` is written `00:01:44` so it is not mistaken for hours.
- **A negative value in a date format** (Excel shows `####`) is written as the raw number; a negative value in a time-only format is written as a signed time such as `-01:30:00`.
- **Serial 60 of the 1900 date system**, which Excel shows as the non-existent 1900-02-29, is written as `1900-02-28`, the same as serial 59.

### Sheet content

- **Hyperlink addresses** are not included. Hidden rows and columns are included without distinction. Text of text boxes and shapes, and pictures, are appended after the table in the order they are placed (for XLS, in the order written in the file). On an XLSX sheet with two or more pictures, each picture is preceded by its cell and the first text of that row (`(cell B2: 제품명)`); XLS pictures and text boxes have no cell indication.
- **Cell comments** are added at the end of the sheet as `> Comment(author, cell E5, on "first text of that row"): text` lines (replies of Excel 365 threaded comments as `> Reply(…)`).
- **Several tables on one sheet** are split only when the first row after a blank row looks like a header: text only, with a column that has numbers below it — and either preceded by a title line, or with a different column count, or identical to the previous table's header.
  - Consecutive text-only tables are merged into one table.
  - When the first row after a group-name line has text ('결시') in a numeric column, the split can happen at the wrong place.
  - Side-by-side tables separated by an empty column are split only when both sides have two or more columns, the headers are text, and the first column holds names. A side with only numbers therefore stays in one table, and a single table that has name columns on both sides with an empty spacer column is split into two (row order is unchanged).

### Large or unusual files

- **XLSX with large cell data** (sheet XML over 20 MB in total, roughly tens of thousands of rows) and **XLSX whose pivot cache exceeds 1 MB** are read in streaming mode to save memory, so there is no merged-cell information. Pictures are still extracted.
- **A large XLSX with more than 5,000 consecutive empty rows** (only when read in streaming mode) is not read past that point, with a warning.
- **XLS is read by taking only the workbook stream out of the OLE container.** A file with a partly damaged container still converts if the stream is intact (handing the whole file to xlrd never finished on a cyclic mini-stream chain).
- **An XLSX with missing relationship information** for pictures, tables or hyperlinks is read without those items, with a warning. Unreadable document properties (creation date and the like), merge ranges and defined names in XLSX are ignored without a warning (of the document properties only the title is used). Non-integer position and merge attributes in Excel 2003 XML (`ss:Index` and the like) are ignored with a warning.
- **A CSV with broken quoting** is split by delimiter only from the broken line onward, with a warning. Quoted cells after that point (containing commas or line breaks) come out split.

## Office (DOCX, PPTX)

- **Word and PowerPoint comments** (review comments) are added at the end of the document (or slide) as `> Comment(author, on "the commented passage"): text` lines; replies as `> Reply(author): text`. The resolved state and pictures inside comments are not included.
- **DOCX automatic numbering** is computed to match the specification and LibreOffice's result, not Word's display. A document with unusual numbering definitions can differ from Word.
- **DOCX tracked changes** are read as the final version. Deleted text and the original position of moved text are not included.
- **Word equations** are converted to LaTeX-style text.
- **Broken parts in DOCX and PPTX**: when parts such as pictures, numbering definitions, document properties or the thumbnail are missing or are not XML, the body is still converted and `warnings` reports the parts that were dropped. Only a document whose main part (`document.xml`, `presentation.xml`), `[Content_Types].xml` or `_rels/.rels` is broken is rejected as corrupted. PPTX skips unreadable shapes (table cells or positions with invalid attribute values, ink annotations) and slides without a part, with a warning that gives the slide number.

## HWP and HWPX

- **Hangul comments** (review comments, 메모) of HWP and HWPX are written at the end of the document, as the same `> Comment(…)` lines as Word.
- **Text of a master page** (바탕쪽), which Hangul prints on every page of a section, is not read, in HWP or in HWPX.
- **Left out on purpose**: a hidden comment (숨은 설명), the guide of a click-here field nobody filled in (누름틀: "click here and type"), the caption of a command button and the choices of a combo box (its selected value is written).
- **Hangul tracked changes**: an HWPX is read as its final version — text deleted under change tracking is left out, with the tables and pictures deleted with it, and inserted text is kept. A deleted paragraph mark does not join the two paragraphs it stood between. Binary HWP is written without telling tracked changes apart.
- **Protected HWP documents**: a password, DRM or certificate-encrypted body is not read (`EncryptedDocumentError`); a
  distribution (read-only) document gives only the preview text Hancom Office saved.
- **Hangul equations** are converted to LaTeX (`{1} over {n}` → `$\frac{1}{n}$`, following Hancom's published equation specification). An equation that cannot be converted, such as a long-division layout (LONGDIV), is left as its script (`[Equation: …]`).

## OpenDocument, RTF and EPUB

- **OpenDocument**: Flat XML files (`.fodt`, `.fods`, `.fodp`) are not supported. Text placed only on master slides,
  chapter numbers generated by outline numbering, form controls and OLE objects (other than charts and formulas) are not
  converted. A formula without usable MathML is written in LibreOffice's StarMath notation, not LaTeX. Links and emphasis
  inside spreadsheet cells are not kept. A repeated row is written at most 10,000 times, and all the repeats of a document
  add at most 5 million characters and 2 million sheet cells (200,000 cells in the tables of text documents and slides);
  beyond that it is cut, with a warning. Columns beyond 16,384 are never read. An object shown by several frames gives its values once.
  Hidden text is left out; for text hidden by a condition the state saved in the file is used (conditions are not evaluated).
- **RTF**: equations, comments and the contents of embedded OLE objects are not read (an OLE preview picture is kept).
  Text in a symbol font (Symbol, Wingdings) written as plain bytes comes out as the Latin letters of those bytes (`abc`
  where the page shows `αβχ`), without a warning; written as private-use characters, Wingdings is read and Symbol is
  dropped. Letter and roman list numbers are written as `1.`, `2.`, and the automatic number of a numbered heading is
  not reproduced. Underline and strikethrough are not kept (raised and lowered text is: `10^6`, `H_2O`). HTML
  mail bodies wrapped in RTF by Outlook yield only their plain-text rendering. A document cut off mid-file is converted
  up to the cut, with a warning. A table keeps at most 500,000 cells; the cells after that are left out with a warning.
  After 50,000 runs of formatting in one paragraph the rest of it is written without bold, italic or links. Groups
  nested deeper than 500 levels and pictures after the 10,000th are left out, each with a warning.
- **EPUB**: DRM-protected books cannot be read. Footnotes stay where the chapter has them instead of becoming `[^n]`; a reference to one is written `[n]`.
  Pictures inside an inline `<svg>` wrapper, `srcset` / `<picture>` alternatives and SVG pictures are not extracted, and
  remote pictures are never fetched. Spine pages that are SVG or plain images (some fixed-layout books and comics) are
  left out with a warning. A contents page that is an ordinary chapter is kept as a list of titles. A missing or damaged
  chapter, or one over 32 MB, is skipped with a warning; pictures over 20 MB each or 64 MB per book keep only their alt
  text; at most 5,000 chapters are read. Text inside a CDATA section of a chapter that is not well-formed XML is lost.

## Word 97-2003 (.doc) and PowerPoint 97-2003 (.ppt)

- **They need an installed LibreOffice**, and fidelity is LibreOffice's reading of the file. Content LibreOffice does
  not carry over (some equations, some table layouts) is lost before OpenDocKit sees it.
- **Not a sandbox.** Macros and linked content are switched off through LibreOffice settings in a private profile, and
  HTTP(S) is routed to a dead proxy. A service that takes legacy files from the public should run conversions in a
  container without network access and keep LibreOffice patched, or disable the bridge (`OPENDOCKIT_SOFFICE=`).
- **Automatic date fields** show the time of conversion, so that text differs from run to run.
- **Limits.** A fixed 60-second limit per file (`DocumentTooLargeError` after that); the LibreOffice process itself has
  no memory limit of its own beyond what the surrounding process or container imposes.
- **Password detection** reads the documented header fields and was not verified against files protected by Word or
  PowerPoint themselves; a protected file it misses is reported as corrupted.
- **Windows**: run and tested. A conversion whose process is killed outright leaves no LibreOffice behind (a job
  object ends it) and a guard process removes its temporary folder, as on POSIX. Known accepted gap: that guard is in the
  same job as the worker, so a server killed outright ends it too and the folder stays.

## Links and emphasis

- Relative addresses, `#fragment`, `javascript:` and other schemes stay plain text. The address of a link that wraps
  only a picture, or several blocks, is not kept. PPTX slide titles and speaker notes, DOCX comments and WordArt are read
  as plain text.
- Emphasis is read from direct run formatting and the Strong / Emphasis character styles (DOCX), run `b` / `i` (PPTX),
  `<b> <strong> <i> <em>` (HTML, mail, EPUB), the character shape (HWP, HWPX) and the text properties of ODT and RTF.
  Bold from other styles or CSS is not marked. Bold over a whole paragraph or cell,
  emphasis that starts inside a word, and a single emphasized character glued to its word are not marked.
- A span that ends in punctuation directly followed by a letter (`**…)**에`) is shown literally by strict CommonMark
  renderers. `*` and `_` typed elsewhere in the document text are not escaped.
- A `#` or a `>` typed at the start of a line is escaped (`\#`, `\>`) in the body text of PDF, HWP, HWPX, PPTX and
  DOCX. In ODT, RTF, HTML and EPUB, and in a table cell, it is written as typed, so a line that starts with `# ` reads
  as a heading: about half of the ODT and RTF files found with such a line were typed as Markdown on purpose.
- A word typed in angle brackets (`<F5>`, `<filename>`) is written as typed. A program that draws the Markdown as HTML
  takes it for a tag and does not show it; sanitize or escape before drawing a result on a page.
- HTML mail with long tracking addresses now carries those addresses, and they count against `max_chars`. Use
  `keep_links=False` for mail if that matters.

## HTML and text

- **Elements hidden only by a stylesheet rule** (`<style>.x{display:none}</style>` with `class="x"`) are kept. Hidden
  elements are recognised by the `hidden` attribute, inline `style`, `aria-hidden` and similar marks on the element itself.

- **Adjacent inline HTML elements without whitespace** (`<span>이름</span><span>홍길동</span>`) are read joined together (they cannot be told apart from an element that only changes the font in the middle of a word).
- **Large HTML** stops as soon as its leading part alone exceeds the character limit. A large HTML that is mostly markup and stays within the limit is parsed in full and can be slow (around 30 s for 40 MB).
- **Text encoding**: when a file is not UTF-8, CP949 is tried first. Japanese (Shift_JIS, EUC-JP) and Chinese (GB2312) text files can be misread as CP949 (less so for HTML, which follows its declared charset). Text in a Latin code page whose accented letters — two different ones at least — fit the alphabet of one language and read no other way is read by them; other text — one kind of accent, a few letters that fit two alphabets (`è` is `č` in Windows-1250), Czech, Hungarian, Turkish or Baltic text in a Windows code page — is left to a statistical guess and can be misread; a guess that reads it as something nobody writes (a sign inside a word, `Ket‰‰n`) is not taken, but one in the letters of another alphabet (`Kųbenhavn`) can be. Text in a code page of DOS or the Macintosh is guessed too, and in capitals it can be taken for Windows-1252.
- **Text control characters**: form feed (`\f`), vertical tab (`\v`) and next line (U+0085) become line breaks (`<br>` inside a table cell); the other C0 and C1 controls are removed. UTF-32 with a BOM (LE, BE) is read; UTF-32 without a BOM is rejected as binary.
