# Known limitations

This page lists what OpenDocKit does not restore, restores only approximately, or handles in a way that may surprise you, grouped by format. Several entries also state the exact rule that is applied, so that you can predict the output. What *is* restored is in [formats.md](formats.md); the reasons behind many of these trade-offs are in [design.md](design.md) and [decisions/](decisions/core.md).

## Platforms

- **Linux and macOS are the supported platforms.** The full test suite runs on both for every change.
- **Windows is not supported yet for HTML and email.** OpenDocKit reads malformed HTML the way a browser does, which relies on
  the HTML5 tokenizer of libxml2 2.14. lxml bundles that version on Linux and macOS, but its Windows wheels still bundle
  libxml2 2.11. With the older parser, text after the end of a page, a second concatenated document, the content of
  `<textarea>`/`<xmp>` and — most importantly — further parts of a mail (forwarded messages, calendar invitations, later
  messages of an mbox) can go missing. When that parser is in use, every HTML or mail result carries a warning saying so.
  The other formats do not depend on it. `IsolatedConverter` applies its memory limit on Linux only; elsewhere the time
  limit and process isolation remain.

## General

- **Charts and shapes drawn as vectors** (PDF charts drawn with lines rather than as images, Office shapes) cannot be extracted as pictures. Native charts of PPTX, DOCX, HWPX, XLSX sheets and chart sheets are converted to data tables (XLS charts are not converted; a chart sheet, and an otherwise empty sheet holding only a chart, give a warning). New chart types (waterfall, treemap and other chartex types) are handed over as the fallback picture stored in the file when their values cannot be read (not verified with real files). However, a PDF page with no characters at all and only curves (a design PDF whose text was converted to outlines) is sent to the vision model as a whole page, like a scanned page.
- **WMF and EMF pictures** (common in HWP and legacy Office) cannot be rendered on macOS and Linux. They stay as `[Image]`, and a warning reports how many there were.
- **Superscripts and subscripts** are written as `10^6`, `10^{-3}`, `H_2O`. An ordinal suffix after a number (21st) and a superscript containing Hangul or Hanja ('1,000원') are left as they are. In PDF they are inferred from font size and baseline position, so they can be wrong, and they are not marked inside table cells. In Excel only scripts on part of a cell's text are marked (not in large XLSX read in streaming mode).
- **Unsupported formats**: RTF, SVG, Word XML (Word 2003 XML and the like), ODF single-file XML (.fodt, .fods, .fodp) and Hangul XML (HWPML, .hml) raise `UnsupportedFormatError`. Earlier versions let them through as text or HTML. Save Hangul XML as HWPX and upload that. A UTF-8 note that merely contains `%PDF-` and PDF structure words is read as text when it cannot be read as a PDF (known accepted gap: a CP949 note is still reported as a corrupted PDF).
- **Legacy .doc and .ppt** are not supported.
- **Partly broken files** (a single slide, speaker notes, an HWP section stream, an HWPX style part): only the broken part is skipped, with a warning. In a very large table (grid over 5 million cells) the lower rows are discarded with a warning.
- **Table merges are capped.** A single cell's merge is expanded up to 10,000 cells and the merges of one table up to 500,000 cells in total; beyond that the value is kept in the first cell only. Trusting the merge counts written in a file would let a crafted file of a few KB exhaust memory. Normal documents do not reach these caps.

## PDF

### Tables

- **Borderless tables** are restored only when 3 or more short cells line up in columns over 3 or more lines.
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

- **A page with malformed page attributes**: if even one page is like this, the whole document is read by plain extraction (no table or heading detection) with a warning.
- **Right-aligned lines between left-aligned lines** (dialogue transcripts, ※ notes) are read top to bottom; but when the right-hand lines all have the same width they are taken as side-by-side text boxes and read column by column.
- **Line breaks inside a PDF paragraph are not joined with a space.** Korean PDFs often break lines at any character, so joining with a space would split words.
- **Word-spacing restoration is an estimate.** For text placed word by word, the actual gap where the font changes is estimated and a space is inserted. A thin space before a symbol (a parenthesis and the like) following Hangul is not restored (`Test(*) 진행` → `Test (*)진행`).
- **Slanted text** (about 6 degrees or more) is treated as a watermark and removed from the body. Small slanted labels inside figures are removed with it.
- **Text drawn rotated 90 degrees within a page** (identifiers in the margin of a paper, a table laid sideways on a portrait page) is read rotated and placed at the end of that page as line-by-line text. Table structure is not restored. Text written rotated inside a cell of a ruled table is read as that cell's text (except cells mixed with horizontal text).
- **Vertical writing** (pages set in WMode 1 fonts): characters are scattered one by one and the column order (right to left) is not kept.
- **Lines drawn separately per font.** A PDF that draws Hangul and digits on the same baseline as separate runs per font (Cairo output and the like) is read as one line by re-collecting the characters in x order. Characters drawn on top of each other (fake bold, overlapping text boxes) are not merged.
- **A 2-up imposition PDF that places each page as a Form XObject**: lines at the same height on the left and right pages are joined into one line.
- **A PDF exported from slides** comes out as scattered fragments of text boxes and diagrams (reading order is right, but "label — value" relations are split into lines). If the original PPTX exists, uploading it preserves the structure.
- **Removal of repeated headers** targets the margins (10% of the visible area's height) and the top and bottom two rows of the page.
  - For margin lines that differ only by a number from page to page, digits are ignored only in page-number-shaped tokens ('3', '3/10', '3쪽', '2024-01-05'), so value lines such as '소계 12건', '실지급액: 3,450,000' or '증감률 6.8%' are kept.
  - A line is removed when it repeats on at least half of all pages, or on at least 2/3 of the odd pages or of the even pages (left and right running heads of a book).
  - The title and signature block of a 4-page bundle of forms are kept. A header that changes per chapter ('“PROPERTY” 139') is kept.
  - A line in a font larger than the body ('2025년 3월 영업 실적') counts as repeated only when the digits match too. Values in a column are not removed when rows of the same shape continue at the same position in the body.
  - Lines attached to a ruled table (unit, notes) and "item value" rows are kept. But on pages without tables, the same body line at the very top or bottom of every page can be removed; turn this off with `remove_repeated_page_lines=False`.
- **Hidden text.** Text hidden from view — white text, the same color as the background, behind a picture, outside the clipping region — is included in the body if it is in the text layer. It cannot be told apart from an OCR text layer in invisible render mode, so it is not filtered.
  - Only characters outside the visible area (CropBox) and characters of size 0 are removed. A page with only such characters is an empty page (it is not revived by plain pdfium extraction).
  - Text hidden with size 0 is removed, but text with only zero width, such as horizontal scaling 0 (`0 Tz`), is not filtered out (it cannot be distinguished from a font without /Widths).
  - A value corrected by writing other text over the same place (an amount covered with a white box and rewritten) can merge both texts into one line (for example `5678` over `1234` → `12345678`).

### Forms, annotations and attachments

- **AcroForm field values** are appended at the end of the page as `name: value`, not next to their label. For a group of check boxes with the same name, and for radio buttons, only the chosen value is included (the `/Opt` item when present). When the field name is an internal name (`Text1`), the reader has to infer which label it belongs to. **XFA forms** cannot be read and give a warning.
- **Text written with a viewer's "add text" tool** (Acrobat Fill & Sign, macOS Preview; FreeText annotations) is appended at the same place as `label: value`, taking the line immediately to its left on the same row as the label.
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
- **Cell formats** are imitated only for: percent, thousands separators, scaling by thousands (`#,##0,`), digit padding (`000000`), decimals, currency, prefix and suffix text, text-only formats (`"매우 좋음"`), optional decimal places (`0.###`), and dates. Fractions (`# ?/?`), scientific notation (`E+`, `E-`) and conditional formats use the raw value. Superscripts and subscripts in Excel 2003 XML rich text are written as `m^2`, `H_2O`.
- **Times** follow the format: `h:mm` → `18:03`, `h:mm AM/PM` → `6:03 PM`, `[$-412]AM/PM h:mm` → `오후 2:30`, `h"시" mm"분"` → `14시 30분`. Seconds appear only when the format has seconds; `mm:ss` is written `00:01:44` so it is not mistaken for hours.
- **A negative value in a date format** (Excel shows `####`) is written as the raw number; a negative value in a time-only format is written as a signed time such as `-01:30:00`.

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

- **Hangul comments** (review comments) are included only for HWPX, at the end of the document, as the same `> Comment(…)` lines as Word. Comments of binary HWP are not included (the record holding the comment text has not been identified).
- **Hangul equations** are converted to LaTeX (`{1} over {n}` → `$\frac{1}{n}$`, following Hancom's published equation specification). An equation that cannot be converted, such as a long-division layout (LONGDIV), is left as its script (`[Equation: …]`).

## HTML and text

- **Adjacent inline HTML elements without whitespace** (`<span>이름</span><span>홍길동</span>`) are read joined together (they cannot be told apart from an element that only changes the font in the middle of a word).
- **Large HTML** stops as soon as its leading part alone exceeds the character limit. A large HTML that is mostly markup and stays within the limit is parsed in full and can be slow (around 30 s for 40 MB).
- **Text encoding**: when a file is not UTF-8, CP949 is tried first. Japanese (Shift_JIS, EUC-JP) and Chinese (GB2312) text files can be misread as CP949 (less so for HTML, which follows its declared charset). Short Central European text that statistics cannot identify is read as Windows-1252.
- **Text control characters**: form feed (`\f`) and vertical tab (`\v`) become line breaks (`<br>` inside a table cell). UTF-32 with a BOM (LE, BE) is read; UTF-32 without a BOM is rejected as binary.
