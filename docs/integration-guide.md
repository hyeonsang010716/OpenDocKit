# Integration guide for upload UIs

This page is a summary for people building an upload screen on top of OpenDocKit. For each file format it collects only **what is limited, what is dropped, and what the user should be told**. How things work and the full option list are in [formats.md](formats.md), [images.md](images.md), [options.md](options.md), [server.md](server.md) and [limitations.md](limitations.md).

## 0. Common to all formats

### Limits

- File size: at most **50 MB** (`max_file_bytes`). Above it, a `document_too_large` error.
- Decompressed size: at most **500 MB** (`max_uncompressed_bytes`). Zip-bomb defense for DOCX, PPTX, XLSX, HWPX, ODT, ODS, ODP, EPUB, HWP, and the converted form of `.doc` / `.ppt`.
- Characters: at most **500,000** (`max_chars`). **Characters of image descriptions are included.**
  - When exceeded, conversion stops right there and returns `exceeded: true` (not an error).
  - `char_count` is then "the count at the moment it stopped"; the real document is longer.
  - The `exceeded` verdict is exact: it always equals "more than 500,000 characters if converted to the end".
- There is no row or page limit. The only size limit is the character count.
- Limits are **per file**. For a total over several files, the frontend (or the caller) adds up each file's `char_count`.

### What to look at in the response

- `char_count`: number of characters (the basis for upload restrictions).
- `exceeded`: whether the 500,000-character limit was exceeded.
- `warnings`: a list of **messages that can be shown to the user as is**. Examples:
  - the extension differs from the real format;
  - hidden sheets were excluded;
  - some columns were dropped because the column limit was exceeded;
  - an image could not be described;
  - pages whose text exists only as an image were not described;
  - conversion stopped because the character limit was exceeded;
  - an image file was not read because `extract_images` is off;
  - RTF and embedded Word document fragments in DOCX imported content (altChunk) could not be converted.
- `metadata`: title, author, page count, sheet count, encoding (only when present).

### Notation

- Links come out as `[text](url)` and bold / italic as `**bold**` / `*italic*` in Word, PowerPoint, HTML, mail, OpenDocument text and slides, RTF and EPUB (`keep_links`, `keep_emphasis`, both on by default). **The marks and addresses count toward `char_count`.** Only `http`, `https` and `mailto` addresses are written; other links keep their text. PDF, Hangul and spreadsheet cells do not carry them.
- Superscripts and subscripts come out as `10^6`, `10^{-3}`, `H_2O` (an ordinal after a number, `21st`, is left as is).
  - In PDF they are inferred from character size and position, so they can be wrong, and they are not marked inside table cells.
- A vertically merged table cell repeats its value on every row below. A table with horizontal merges (HWP, HWPX, DOCX, PPTX, HTML, ODT, ODP, RTF, EPUB) comes out as an HTML table (`rowspan`, `colspan`); the tags count toward the character count.

### Errors

Branch on `code`; the message can be displayed as is.

- `unsupported_format`: a format that is not supported — legacy .doc and .ppt on a server without LibreOffice (or where it could not be started), Hangul 3.x, .xlsb, SVG, Word XML, ODF single-file XML (.fodt, .fods, .fodp), other OpenDocument kinds (drawing, formula, database), Hangul XML (.hml — advise saving as HWPX), unknown binary.
- `encrypted_document`: a password-protected PDF, Office (legacy `.doc` / `.ppt` included), OpenDocument or HWP document, an encrypted mail, or an EPUB with DRM. Tell the user to remove the password and upload again.
- `document_too_large`: over 50 MB; or a file that becomes too large when decompressed; an HWP with an excessive number of internal records; a file that exceeded the server's conversion time (2 minutes) or memory limit (the "too large or too complex to convert" message); a `.doc` / `.ppt` that LibreOffice did not finish within 60 seconds.
- `corrupted_document`: an empty file, a broken file, a parse failure, an image file that cannot be opened (when a single image was uploaded), a `.doc` / `.ppt` that LibreOffice could not convert.

### Format detection

- The format is determined from the **file content**, not the extension.
- Even with a wrong extension the file is read as its real format and only a warning is added (for example `.xls` that is really HTML, `.csv` that is really Excel or PDF).
- The extension is used only to tell the text family apart (TXT, MD, CSV, TSV, HTML).

### Pictures

- Image description (vision model) takes **seconds to tens of seconds** → separate it from the upload response and show progress.
- Before description a picture counts only as `[Image]` → **`char_count` differs before and after description**.
- Pictures discarded without being sent to the vision model:
  - short side under 64 px (icons, bullets);
  - aspect ratio above 20:1 with a short side under 256 px (divider lines);
  - a picture repeated 3 or more times in one document (logos, backgrounds);
  - duplicates of an identical picture (described once; the same description is used at every place).
- A picture the model judges to be decoration (a logo and the like) is removed from the text (it stays as `[Image: alt text]` if the author supplied alt text).
  - Alt text that Office attached automatically ("…이(가) 표시된 사진 / 자동 생성된 설명", "Description automatically generated") does not count as author alt text.
- A failed description is not an error: the picture stays as `[Image]` and a warning is added. Retrying calls the model again only for the failed ones.
- When the model returns an **empty answer** (and the confirmation query is empty too), it is a failed description: `[Image]` and a warning remain (it can be retried).
- Known issue: when the model returns a **refusal** ("I can't help with that"), that sentence goes into the text as the description.
- WMF and EMF pictures (common in HWP and legacy Office) cannot be rendered; they stay as `[Image]` and a warning reports how many.
- Pictures inside documents (including scans of ID cards and signatures) are sent to OpenAI → if your service handles sensitive documents, consider a notice on the upload screen.

## 1. Excel (XLSX · XLSM · XLS · Excel 2003 XML)

OpenDocument spreadsheets (`.ods`) follow the same rules for sheets, several tables per sheet, merged cells, hidden sheets and the column limit; what differs is in [section 10](#10-opendocument--rtf--epub).

### Limits

- **No row limit** (size is limited only by the 500,000 characters).
- **At most 100 columns per sheet** (`max_sheet_cols`, `.xls` included). Columns beyond that are dropped, with a warning.

### What is read

- A table under `## Sheet: name` for each sheet. When there are several tables separated by blank rows (for example a '1. 매출' table followed by a '2. 인원' table) they are written separately; title and note lines with a single filled cell between or after tables ('※ 출처: …') become paragraphs.
- Cell values are written **as they appear on screen**:
  - percent (`108.2%`), thousands separators, decimal places, currency symbols, prefix and suffix text (`원`, `%p`);
  - scaling by thousands (`#,##0,`), digit padding (employee number `001234`);
  - formats with text between digits (phone number `010-1234-5678`, date-like `2026-03-31`);
  - dates.
- A duration format (`[h]:mm`) gives the accumulated time (`30:00`).
  - A format that shows negatives only in red (`#,##0;[Red]#,##0`) gets a `-`. Formats that show them with parentheses, △ or ▼ keep that appearance.
  - A format that shows only the weekday gives the date (`2024-03-14`).
- Merged cells: a vertical merge repeats the value on every row below; a horizontal merge in a data row is repeated in every named column it spans, and in a title or header row, or with nothing to its left, it stays in the first cell.
- Formulas: the **calculated result** stored by Excel is used.
- A text-only format (value 5 shown as `"매우 좋음"`) gives the text that was shown on screen.
- Pictures attached to a sheet and text of text boxes and shapes: placed after the table in their order on the sheet (top to bottom). Which cell they were next to is not kept.
- A chart placed on a sheet → data table. A chart sheet becomes `[Chart] title` and its data table. Charts in `.xls` are not converted (warning).
- "Excel download" files named `.xls` (really an HTML table, Excel 2003 XML, or BIFF without OLE — PHP style, Korean read as CP949) are read too.

### What is dropped or changed

- **Hidden sheets are excluded** (warning).
- A file with no stored calculated results (made by a program other than Excel) gives the formula (`=A2*B2`) instead of the value (warning).
  - A file whose calculated results are all stored as 0 ("Excel downloads" made with Apache POI or xlsxwriter) gives formulas for xlsx and empty cells for xls (warning). A file in which POI only filled values into an Excel template gives the 0 as is.
  - A formula without a value in Excel 2003 XML comes out as the formula, like `=SUM(B1:B2)` (warning).
  - A file in which only some formulas have no result (a report where a server program wrote an extra total row) gives the formula only for those cells (warning per sheet). Opening it in Excel and saving again turns them into values.
- In `.xls`, when no formula has a calculated result at all, the formulas come out as empty cells (warning; the formula text is not written).
- An xlsx saved in Strict Open XML format gives `unsupported_format` → advise "save again as a normal Excel Workbook (.xlsx)".
- Hidden rows and columns are included without distinction. Hyperlink addresses are not included (only the text visible in the cell). Cell comments are added at the end of the sheet as `> Comment(author, cell E5, on "…"): text`.
- A cell with a time format (`h:mm` and so on) gives only the time, as Excel does, rounded to the second (a fractional-second format also stops at seconds). A negative time is `-01:30:00`. With a date format, a value outside the date range (99999999), 0 or a negative value comes out as the number. In Excel 2003 XML, a numeric cell with a date format also comes out as a date (the 1904 date system is not supported).
- In `.xls`, when merged cells cover more than 500,000 cells, the merged value is kept in the first cell only (warning).
- Among formats, fractions (`# ?/?`), scientific notation and conditional formats (`[<=9999999]…`) come out as the raw value.
- A file with large cell data (sheet XML over 20 MB in total, roughly tens of thousands of rows) is read **without merged-cell information** (and without in-cell superscript/subscript marks).
- A file whose pivot cache exceeds 1 MB, a file that references the same picture a very large number of times, and a file whose merge or hyperlink ranges cover more than 500,000 cells are also read without merged-cell information.
- An abnormal file that points to the same sheet several times has that sheet read only once (warning).
- A file whose cell style information violates the specification is read keeping only number and date display formats (warning; superscript/subscript applied to a whole cell is lost).
- Only in the large files read without merged-cell information as above: **5,000 consecutive empty rows are taken as the end of the sheet** (warning). Other files are read to the end.
- Some abnormal xlsx files (broken styles produced by tools other than Excel) give `corrupted_document`.
- An xlsx missing relationship information for pictures, tables or hyperlinks is read without those items (warning). Non-integer cell positions and merge attributes in Excel 2003 XML are ignored (warning).
- An `.xls` is read even when part of its container is damaged, as long as the workbook stream is intact.
- An Excel Binary Workbook (`.xlsb`) gives `unsupported_format` → advise "save as XLSX and upload".

## 2. CSV · TSV

### What is read

- Encoding is detected automatically: UTF-8, CP949 (EUC-KR), UTF-16 (including without a BOM). CP949 with no Hangul and only `㎡`, `℃` or `※` is read too.
- The Excel idiom `="00123"` (preserving leading zeros) becomes `00123`. Other formulas stay as text.
- The delimiter is inferred automatically: comma, tab, semicolon, vertical bar (`|`). A tab, semicolon or `|` that gives a constant number of cells per line is preferred over commas inside values (`1,234`). A TSV that omits trailing empty cells is still read with tabs.
- No row limit (only the 500,000 characters), at most 100 columns.

### What is dropped or changed

- A file with broken quoting: from the broken line onward, lines are split by delimiter only (warning).
- Several tables separated by empty lines (`[거래 요약]`, `[거래 내역]` in a bank statement and the like) are written separately by the same rules as Excel.
- The delimiter is inferred from the content, not the extension (`.tsv`). If the first line is an Excel delimiter marker such as `sep=;`, it is followed and that line is dropped.
- A file with stray NUL characters is read if the rest is UTF-8 or CP949 text (the NULs are removed). If other control characters are present too, it is taken as binary and gives `unsupported_format`.

## 3. PDF

### Limits

- Structure-analysis time budget: **60 seconds** (`pdf_time_budget`).
  - What is counted is **only the time spent analyzing the characters, tables and paragraph structure of each page (pdfminer)**.
  - Picture rendering and AI image description (vision-model calls) are not included.
  - When exceeded, the remaining pages are read quickly as text only → on those pages there is **no table or heading detection, and pictures inside the document are dropped without a warning** (scanned pages are kept).
  - A single page that takes long cannot be cut off midway (the budget is checked only between pages).

### What is read

- A `<!-- Page N -->` marker for every page.
- Reading order (including two-column layouts and two-column layouts with wide line or paragraph spacing), paragraphs, headings inferred from character size.
- Ruled tables (including tables without left and right borders, two-row headers, dotted cell boundaries, merged cells, tables filling the page, text rotated 90 degrees inside a cell).
- Values of fillable forms (AcroForm): at the end of that page as `name: value` (check boxes ☑/☐, radio buttons give the chosen value). Not next to the label.
- Repeated headers, footers and page numbers are removed (a `(단위: 백만원)` unit line or note line attached to a table is kept even if identical on every page). Different odd/even running heads, as in a book, are removed too; headers that change per chapter are kept.
- Borderless tables (when 3 or more short cells line up in columns over 3 or more lines; a row whose cell text wraps onto two lines is joined inside the cell so the table is kept).
- A PDF in which every word is placed separately without spaces (exported from PowerPoint and the like) has its word spacing estimated and restored.
- A rotated page (landscape document) is read upright. Text drawn rotated 90 degrees within a page (identifiers in the margin, a table laid sideways) is read rotated and placed **at the end of that page**, line by line (table structure is not restored).
- **Scanned PDF**: a page without text is sent as a whole-page image to the vision model, which reads the text.
  - Converted without a vision model, scanned pages are empty and a warning is added → **`char_count` of a scanned PDF is close to 0 before description**.
- Pictures inside the document: only the picture region is cropped and sent to the vision model.

### What is dropped or changed

- A password-protected PDF gives `encrypted_document`.
- A table without rules and with only loose column alignment comes out as line-by-line text, not a table.
- Diagonal watermarks (large text slanted about 6 degrees or more) are removed from the body.
- Charts and shapes drawn with lines (not image files) cannot be extracted as pictures. Only text such as axis labels remains.
- A file exported from slides to PDF has scattered text boxes, so "label — value" relations are split into lines → **recommend uploading the original PPTX**.
- A scanned PDF that already contains OCR text uses that text as is (it is not read again even if the OCR quality is poor).
  - Exception: a scan whose text layer has no Hangul at all and only sparse characters (a Korean book in which only fragments of Latin letters and digits were recognized) is sent as whole-page images for the vision model to read (warning; the text-layer fragments are output too).
- When text is laid over a picture, that text can appear twice, in the body and in the picture description.
- XFA forms (Adobe LiveCycle) cannot be read and give a warning → advise "upload a file printed to PDF".
- Text outside the trim area of a print PDF (an area not visible on screen) is not read (printer's marks, proof line numbers).
- On pages without tables, if the same body line is at the very top or bottom of every page, it can be taken for a header and removed.
- Tables and pictures on a two-column page are placed between the paragraphs of the same column. A table at the top of a column can come before the neighboring column.
- A page whose structure analysis failed, or with more than 50,000 drawing elements, is read as text only (warning; no table or heading detection).
- A page with a very large number of scattered text fragments (more than 2,000 per page: scatter-plot labels, drawings and the like) is read without reading-order analysis (warning; the text is preserved).

## 4. Word (DOCX · DOCM · DOTX)

### What is read

- Heading styles → `#` headings (an automatically numbered heading comes with its number, like `# 1. 개요`).
- Bulleted and numbered lists (including start numbers, number strings such as `제1조`, and numbering inherited from a style).
- Tables (including merged cells, and rows and cells inside content controls).
- Text boxes.
- Footnotes (`[^1]`) and endnotes (`[^endnote1]`) at the end of the document.
- Word equations → LaTeX format.
- **Charts → data tables**, **SmartArt → nested lists**.
- Characters inserted with Insert Symbol (check boxes ☑, ☐; ≥, ±, °, μ and so on in the Symbol font), the check state of form fields (☒, ☐) and the item chosen in a drop-down.
- Ruby (phonetic guide) → `base(ruby)`; text inside pasted shapes.
- Pictures (body, inside tables, text boxes) and alt text.
- Tracked changes: **the final version** (deleted text, and footnote marks, pictures and text boxes in deleted ranges, are not included; a moved text box appears once).
- Strikethrough text → `~~text~~`.

### What is dropped or changed

- Legacy `.doc` is converted when the server has LibreOffice installed (the result carries a warning that the document went through LibreOffice, and takes a few seconds longer); otherwise it gives `unsupported_format` → advise "save as DOCX and upload".
- A docx saved in Strict Open XML format gives `unsupported_format` → advise "save again as a normal Word Document (.docx)".
- Headers and footers are excluded by default (usually noise).
- Hyperlinks come out as `[text](url)` (http, https and mailto only) and bold / italic as `**bold**` / `*italic*`; a paragraph or cell that is bold throughout is not marked. Hidden text is not included. Comments (review comments) are added at the end of the document as `> Comment(author, on "passage"): text`.
- A numbered paragraph with empty text (including one wrapped in an empty input field) only advances the numbering and is not output.
- Numbering that continues across chapters (`제1조`–`제4조` under `제1장`, `제2장`) is continued as set in Word.
- The prompt text of an empty input field ("Click here to enter text") is not included.
- New chart types (waterfall, treemap and so on): when the values cannot be read, the pre-rendered picture is handed over instead (subject to vision-model description).
- A chunk inserted from another document (altChunk) is read in place when it is HTML, MHT or plain text; RTF and embedded Word document fragments are left out and a warning reports their count.
- Legacy WordArt text is read in the body only (watermarks in headers are excluded).
- A file in which parts such as pictures, numbering definitions, document properties or the thumbnail are missing or broken still has its body read, and a warning reports the dropped parts. Only a file whose body part (`document.xml`) or package listing (`[Content_Types].xml`, `_rels/.rels`) is broken gives `corrupted_document`.

## 5. PowerPoint (PPTX · PPTM · PPSX · POTX)

### What is read

- `## Slide N: title` for every slide.
- On-screen position order (top to bottom, left to right), bullet levels, automatic numbering (`1.`, `a)`, `①`; automatic numbers in table cells and speaker notes are not added).
- Tables (including merged cells).
- **Charts → data tables** (values keep their digits; a combo chart gives all series, a scatter chart X·Y pairs; axis titles are one line under the title; date categories are like `2026-01-01`).
- **SmartArt → nested lists**.
- Speaker notes (included by default; can be turned off with `include_speaker_notes`).
- Pictures (including inside grouped shapes) and the preview picture of pasted Excel and Word objects (OLE). If the preview is EMF or WMF: `[Image]` plus a warning. The content inside the object is not read.
- Text of shapes that contain newer features such as equations (`mc:AlternateContent`). Equations inside text boxes (equation editor) → LaTeX format.
- Strikethrough text → `~~text~~` (body and table cells; not marked in slide titles and speaker notes).
- Slide comments (review comments) are added at the end of the slide as `> Comment(author): text`.

### What is dropped or changed

- Legacy `.ppt` is converted when the server has LibreOffice installed (with a warning that the document went through LibreOffice); otherwise it gives `unsupported_format` → advise "save as PPTX and upload".
- A pptx saved in Strict Open XML format gives `unsupported_format` → advise "save again as a normal PowerPoint Presentation (.pptx)".
- Shapes themselves (arrows, diagram shapes) are not extracted as pictures. Only the text inside shapes remains.
- Shapes hidden in the Selection Pane are not included (hidden slides are still output, with `(hidden)` after the slide title).
- Equations inside slide titles and speaker notes are not converted (only the plain text remains).
- Table cells and shapes with invalid attribute values (a shape whose position is text, ink annotations and the like) and slides without a part are skipped, with a warning that gives the slide number. A file in which other parts (layout, notes, chart, document properties) are broken still has its slides read.

## 6. Hangul (HWP · HWPX)

### What is read

- "개요 N" (Outline N) styles → `#` headings.
- Paragraph numbers and bullets (`1.`, `가.`, `-`).
- Tables (including merged cells); captions of tables, pictures and equations (including the number of "표 1", "그림 2").
- Multilevel lists inside a table cell are marked with `└` (level 2) and `└─` (level 3) before the sub-item.
- Text boxes, footnotes.
- A numbered paragraph containing only a table or text box still counts (the `2.` is placed on the line before the table so later numbers do not shift).
- Text of TextArt (글맵시); HWPX charts → data tables.
- Ruby (small text set above characters) → `base(ruby)`; the characters of overlapped characters (enclosed characters and the like).
- HWPX form objects: check boxes and radio buttons → `☒`/`☐` + label; combo and edit boxes → value.
- Hangul equations → LaTeX (`$\frac{1}{n}$`). Equations that cannot be converted, such as a long-division layout (LONGDIV), stay as their script (`[Equation: …]`).
- Pictures (body, inside tables, grouped objects).
- Strikethrough text → `~~text~~` (wavy and 3D-style strikethrough is not marked).

### What is dropped or changed

- **Hangul 3.x and earlier (old .hwp)** gives `unsupported_format` → advise "save again as HWP or HWPX in Hangul".
- An encrypted document gives `encrypted_document`.
- **A distribution (copy-protected) document** does not have its body decrypted; the **preview text (the beginning only)** saved by Hancom Office is used (warning).
- Headers and footers are excluded by default. Page numbers are not included. Comments (review comments) are added at the end of the document for HWPX only; in HWP they are not included (the body text a comment is attached to is kept).
- A document with no text at all (empty tables, only shapes) gives an empty result plus a warning that no text was extracted.
- Charts of HWP (binary) are not converted.
- OLE objects such as pasted Excel tables and charts, and videos, do not have their content read; only `[OLE object]` and `[Video]` placeholders remain (a warning reports the count).
- Known issues:
  - "Start new number" is not applied, so paragraph numbering continues.
  - In HWPX tracked changes, text deleted across runs with different character formatting can remain.

## 7. HTML

### What is read

- Headings, paragraphs, lists (nested ones too; deeper than 20 levels is folded onto one line), tables (`rowspan`, `colspan`; a multi-row header is combined into one row), code blocks.
- A layout table (a table holding tables inside its cells) has its cells unfolded into paragraphs and the inner tables written separately.
- "Excel download" HTML named `.xls` (including files that start from `<tr>` without `<table>`, and files that start with a long `<style>`).
- Pictures embedded in the document (`data:` addresses) and pictures saved inside MHTML.
- Mail files (.eml, whatever the transfer encoding of the parts): the HTML body once (if there is none, the plain-text body as text) + `Subject:`, `From:`, `To:`, `Cc:`, `Date:` lines at the top with RFC 2047 decoded. Attachments (including PDFs displayed inline in the body) are not read (a warning gives names and count).
- An mbox file of several mails: `## Message N` + headers + body for each mail (up to 100; beyond that a warning).
- The question of a collapsed FAQ or accordion (the expand button) and its answer (the hidden block right after the button).
- Strikethrough (tag or CSS) → `~~text~~`; the start number, reversed order and `a.`/`i.` styles of numbered lists.
- The character encoding declared by the document (`<meta charset>`) is followed (when declaration and content disagree, the content decides). Line breaks inside a table cell are kept as `<br>`.

### What is dropped or changed

- Scripts, styles, hidden elements (menus, modals, tooltips and so on), menus (`nav`), advertisements (`aside`) and comments are removed.
- **External image addresses are not fetched** (security). Only the alt text remains.
- Links come out as `[text](url)` for `http`, `https` and `mailto` addresses, in mail too; relative, `#fragment`, `javascript:` and other links keep only their text. Bold and italic come out as `**bold**` / `*italic*`. Input fields and select boxes give the value that was shown, check boxes and radio buttons come out as ☒ / ☐, and buttons other than expand buttons are not included.
- Tables and headings with missing closing tags, and text placed outside table cells (subtotals and the like), are read the way a browser reads them. Block-quote marks (`>`) go down to 20 levels.
- A large HTML stops as soon as its leading part alone exceeds 500,000 characters. An HTML of tens of MB that is mostly markup and stays within the limit can take tens of seconds.
- Inside a code block (`<pre>`), a line starting with ``` gets a `\` in front (so the code block is not closed midway).

## 8. Text (TXT · MD)

### What is read

- Encoding is detected automatically: UTF-8, CP949 (EUC-KR), UTF-16 (including without a BOM).
- Whitespace and indentation of the original are preserved (including Markdown code indentation).

### What is dropped or changed

- A file with stray NUL characters is read if the rest is UTF-8 or CP949 text (single NULs are removed; a run of NULs padding a column becomes spaces, keeping the columns of a fixed-width export). If other control characters are present too, it is taken as binary and gives `unsupported_format`.
- A partly broken file, such as CP949 whose last character is cut off, has only the broken place replaced with `�`, with a warning.
- English and Western European text containing curly quotes and accents is read as Windows-1252. Japanese (Shift_JIS, EUC-JP) and Chinese (GB2312) text files can be misread as CP949.
- Form feed (`\f`) and vertical tab (`\v`) characters come out as line breaks. UTF-32 with a BOM is read; UTF-32 without a BOM gives `unsupported_format`.

## 9. Image files (JPG · PNG · WEBP · GIF · BMP · TIFF · AVIF · HEIC)

> HEIC/HEIF (iPhone photos) is read only when the server installed `opendockit[heic]`. Without it, a HEIC upload fails with
> `unsupported_format`.

### What is read

- The vision model describes the image and transcribes its text.
- Small or thin images are not discarded (the user uploaded the picture on purpose).
- Sent reduced to a long side of 2048 px; photo rotation (EXIF) is corrected.
- A multi-page TIFF (fax or scan bundle) is read as one picture per page (`<!-- Page N -->`, at most 1,000 pages — a warning if it has more or reading stops at a damaged page). Animated GIF: first frame only.

### What is dropped or changed

- **Converted without a vision model, the text is only `[Image]`** → `char_count` is almost 0.
- When the vision model returns an empty answer it is a failed description, with a warning (it can be retried).
- Description costs money on every call.

## 10. OpenDocument · RTF · EPUB

### What is read

- **OpenDocument text (`.odt`)**: headings, lists, tables with merged cells, footnotes, comments, pictures, embedded charts as data tables, LibreOffice Math formulas as LaTeX, links and emphasis. Headers and footers follow `include_headers_footers`.
- **OpenDocument spreadsheet (`.ods`)**: the Excel rules of section 1 — one `## Sheet: name` per sheet, several tables on a sheet kept apart, merged cells, hidden sheets excluded with a warning, at most 100 columns counted from the first used column, text of drawn shapes and cell comments after the table. Cell text is what the cell displayed when the file was saved.
- **OpenDocument presentation (`.odp`)**: one `## Slide N: title` per slide, bullet levels, tables, charts as data tables, speaker notes (`include_speaker_notes`), hidden slides marked `(hidden)`.
- **RTF**: paragraphs, headings from the style or outline level, lists, tables with merged and nested cells, footnotes, pictures, links and emphasis, text in legacy code pages (CP949 and others).
- **EPUB**: the chapters in reading order as one document, with the pictures stored in the book; title, author and language go to `metadata`. The navigation page and the cover page are left out.

### What is dropped or changed

- Password-protected OpenDocument files and EPUBs protected by DRM give `encrypted_document`.
- OpenDocument single-file XML (`.fodt`, `.fods`, `.fodp`) and other OpenDocument kinds (drawings, formulas, databases) give `unsupported_format`.
- OpenDocument: text placed only on master slides, form controls and OLE objects other than charts and formulas are not converted. A row repeated a very large number of times is written a bounded number of times, with a warning.
- RTF: equations, comments and text in symbol fonts are not read; letter and roman list numbers are written as `1.`, `2.`. A table keeps at most 500,000 cells (warning). A file cut off in the middle is converted up to the cut (warning).
- EPUB: footnotes stay where the chapter has them; remote pictures are never fetched; a chapter over 32 MB is skipped, and pictures over 20 MB each or 64 MB per book keep only their alt text (warnings). **On Windows, EPUB shares the HTML limitation** described in [limitations.md](limitations.md#platforms).
