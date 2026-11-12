# Output by format

This page describes what the Markdown produced by OpenDocKit looks like for each input format: how a document is split into sections (`result.sections`), and which parts of the document's structure are restored. The rules that apply to every format (format detection, Unicode normalization, how tables and merged cells are written) are at the end. What is *not* restored is listed in [limitations.md](limitations.md); how pictures are handled is in [images.md](images.md).

| Format | Section unit | Marker in `result.text` |
| --- | --- | --- |
| PDF | page | `<!-- Page N -->` |
| DOCX | document | — |
| PPTX | slide | `## Slide N: title` |
| XLSX, XLS, Excel 2003 XML, CSV | sheet | `## Sheet: name` |
| HWP, HWPX | document | — |
| HTML, MHTML, email | document | — |
| TXT, MD | document | — |
| Image files | document (multi-page TIFF: page) | `<!-- Page N -->` for multi-page TIFF |

## PDF

Section unit: page (`<!-- Page N -->`). Design notes: [decisions/pdf.md](decisions/pdf.md) and the other `decisions/pdf-*.md` files.

Text flow:

- Reading order, including two-column layouts, two-column layouts with wide line or paragraph spacing, and side-by-side text boxes.
- Paragraphs, and headings inferred from font size.
- Repeated page headers and page numbers are removed.
- Printer's marks outside the trim area (outside the CropBox) are removed, except on rotated pages.
- Diagonal watermarks, fake bold and shadow duplicates are removed.
- Word spacing is restored for text that was placed word by word without space characters.
- Page numbers in a dotted table of contents are kept; the dot leader is reduced to a single ` … `.

Tables:

- Ruled tables, including tables without left and right borders, dotted cell boundaries, vertically merged values repeated on every row, tables that fill the whole page, and text written rotated inside a cell.
- Borderless tables, including splitting two cells that touch because the gap between them is narrow.
- Striped (row-shaded) tables, including vertically merged cells and an unshaded header and first row.
- A table continued on the next page gets the header row of the previous page.
- A table inside a table is flattened into one table by repeating the outer table's label on every row.
- Two-column "item value" layouts, including values wider than half the page and values wrapped inside a cell.
- Two ruled tables with different column structures stay separate even when they touch.

Forms and symbols:

- AcroForm field values are appended at the end of the page as `name: value`; choice fields show the visible text.
- Check boxes drawn with lines become ☑ / ☐, including the rounded boxes and radio buttons of printed web and Word forms.

Pictures:

- A picture inside a ruled-table cell is placed in that cell.
- A clipped picture contains only its visible part.
- A photo grid of four or more columns is written row by row as photo → caption.

## DOCX

Section unit: the whole document. Macro-enabled documents (.docm) and templates (.dotx) are read too. Design notes: [decisions/office.md](decisions/office.md), [decisions/office-lists.md](decisions/office-lists.md), [decisions/office-charts.md](decisions/office-charts.md), [decisions/office-math.md](decisions/office-math.md).

- Heading styles.
- Bulleted and numbered lists: start number, number formats such as `제%1조`, and legal-style `1.1.` numbering.
- Tables with merged cells.
- **Charts → data tables**, including the unit given in a text box inside the chart and the unit in the value number format.
- **SmartArt → nested lists**.
- Text boxes.
- Footnotes and endnotes, numbered in order of first reference as `[^1]` and `[^endnote1]`.
- **Word equations → LaTeX**. The deleted side of tracked changes is excluded; double-struck and script letters become `\mathbb` and `\mathcal`.
- Form fields (check boxes, drop-downs).
- Ruby text.
- Word enclosed characters (EQ fields → ㉑, ☑ and so on).
- Characters in symbol fonts (Symbol, Wingdings) → Unicode.
- Imported content (`w:altChunk` holding HTML, XHTML, MHT or plain text) is read in place.
- Legacy WordArt text (body only).
- The preview picture of an OLE object; when there is none, `[OLE object]` plus a warning.

## PPTX

Section unit: slide (`## Slide N: title`). Macro-enabled presentations (.pptm), shows (.ppsx) and templates (.potx) are read too.

- Shapes in on-screen position order.
- Bullet levels.
- Tables with merged cells; lower list levels inside a cell are marked with `└`.
- **Charts → data tables**: combo and scatter charts, axis titles, date categories, text boxes inside the chart (units), and text units in the value number format (`1,200억원`).
- **SmartArt → nested lists**.
- **Equations → LaTeX**.
- Automatic numbering (`1.`, `a)`, `①`), including numbering inherited from the layout.
- Line breaks inside a text-box paragraph (Shift+Enter).
- Speaker notes; lower levels are marked with `└`.
- The preview picture of an OLE object; `[OLE object]` plus a warning when image extraction is turned off or there is no preview.
- Symbol-font characters (☒, □ and so on) → Unicode.
- Shapes hidden in the Selection Pane are not included.

## Spreadsheets: XLSX, XLS, Excel 2003 XML, CSV

Section unit: sheet (`## Sheet: name`). Design notes: [decisions/spreadsheet.md](decisions/spreadsheet.md), [decisions/spreadsheet-tables.md](decisions/spreadsheet-tables.md), [decisions/spreadsheet-cells.md](decisions/spreadsheet-cells.md), [decisions/csv.md](decisions/csv.md).

Tables:

- Several tables separated by blank rows, and side-by-side tables separated by blank columns, are written as separate tables.
- One-cell title and note lines between or after tables, and a summary cell to the right of the header row (`총원 26`), become paragraphs.
- Merged cells: a vertical merge repeats its value on every row; a horizontal merge in a data row repeats its value in every column it spans.
- Hidden sheets are excluded.
- Column limit (also for XLS), counted from the first column in use.
- Sub-items expressed by cell indent or leading spaces are marked with `└` (not for CSV).

Cell values:

- Date and number formats: a date format that does not show the day gives `2024-01` or `2024`; negative sections; the `-` of negatives shown only in red; Excel-style rounding; elapsed time `26:00`.
- A time format gives only the time, rounded to the second; a negative time is `-01:30:00`.
- Error values (`#REF!`).
- A cell value whose stored type is wrong (for example `1,5` in a numeric cell) is kept as the original text and a warning is added.
- An undefined number-format id is treated as General.
- Malformed view and print settings (margins, window layout) are skipped.
- A cell using "Place in cell" pictures gets an image placeholder in that cell; a data-type cell shows its visible text.
- Form-control check boxes and option buttons become `☒` / `☐`.

Other sheet content:

- Text of text boxes and shapes, and pictures, are placed after the table.
- Charts placed on an XLSX sheet → data tables (including the unit in a text box inside the chart).
- SmartArt on an XLSX sheet → nested lists.

CSV:

- A first line such as `sep=;` is honored.
- The `\"` quote escape of MySQL exports is understood.

## HWP and HWPX

Section unit: the whole document. Design notes: [decisions/hwp.md](decisions/hwp.md).

- "개요 N" (Outline N) styles → headings.
- Paragraph numbering and bullets; lower levels become indented lists.
- Tables with merged cells.
- Captions of tables, pictures and equations, including their automatic numbers.
- Text boxes.
- Footnotes.
- **Equations → LaTeX**.
- HWPX charts → data tables (a chart inside a table cell becomes a table inside that cell).
- The picture of an HWPX shape filled with a picture.
- TextArt (글맵시).
- Overlapped characters (enclosed characters and the like).
- HWPX form objects: check boxes ☒ / ☐ and input values.
- HWPX check bullets (`- ☑`, `- ☐`); in HWP only `- `.

## HTML

Section unit: the whole document. Design notes: [decisions/html.md](decisions/html.md).

Encoding and title:

- The declared charset is followed.
- When the body has no heading element at all, `<title>` is placed first as `# title`.

Block structure:

- Headings, lists, definition lists (`term: description`), tables and code.
- Per-level lists of Google Docs exports and `mso-list` level paragraphs of Word-saved .htm files become nested lists.
- Block quotes are marked down to 20 levels; paragraphs inside are separated by one empty `>` line.
- Strikethrough becomes `~~text~~`.

Tables:

- `rowspan` and `colspan`, and `<br>` inside a cell.
- A full-width title row above the header becomes a paragraph above the table.
- Several top rows made only of `th` are combined into a multi-line header only when they are tied together by merges (`rowspan`/`colspan`).
- Indentation of the name cell in the first column is marked with `└`.
- Leading indentation of code lines inside a cell is kept as `&nbsp;`.
- Layout tables (a screen built from cells that hold an inner table, or a heading or several paragraphs) are unfolded into blocks and the data tables inside are written separately. A table that has a `th` header row is not treated as a layout table merely because a cell contains a heading.
- A div table built with ARIA roles (`role="grid"`, `row`, `gridcell`) is read as a table.
- An item/value table that uses `th` for the name cells (`th | td | th | td`) is written as data under an empty header row.

Inline SVG:

- Labels (`<text>`, and the text inside `<foreignObject>` of Mermaid and draw.io diagrams) are kept when there are 10 or fewer. With more, they are kept only for diagrams in which at least half the labels are words; charts with many numeric labels are dropped.

Forms and check states:

- Form values (input, select, textarea; check boxes ☒ / ☐).
- Completion state of Confluence checklists and Notion to-do lists (☒ / ☐).
- Symbol-font (Wingdings, Symbol) bullets and check boxes of Word-saved .htm files and Outlook mail → ▪, ☑, □.

What is removed or kept:

- Scripts, `<noframes>`, hidden elements, and side menus (`aside`) outside the main content (`main`, `article`) are removed. Warning and note boxes (`aside`) inside the main content are kept.
- The body of a page saved with a modal open, where the application root (`#root`, `#__next`) carries `aria-hidden`, is kept.
- A lazy-loading placeholder image (`src` is a 1×1 data: GIF, the real address is in `data-src`) is treated like an external image: only its alt text is kept.
- A `data:image/svg+xml` icon is not extracted as an image; only its alt text is kept.

Tolerant parsing:

- Tables and headings with missing closing tags, text outside table cells, content after `</html>` and a second concatenated document are read the way a browser reads them.

Math:

- **MathML → LaTeX** (`$\frac{3}{4}$`). When the document carries LaTeX itself (`annotation`, `alttext`), that is used.

MHTML:

- MHTML (.mht, and single-file web pages served as .xls or .doc) is read from its first HTML part.
- When the main content appears to be in a frame (iframe) part saved alongside (saved Naver blog and cafe pages), a warning says the frame was not read.

## Email (.eml, mbox)

Section unit: the whole document. Email is read through the HTML path. Design notes: [decisions/mail.md](decisions/mail.md).

Recognition:

- MIME mail is read whatever the transfer encoding of its parts is (base64, quoted-printable, 7bit, 8bit).
- This includes Office 365 mail whose headers are tens of KB long, and Thunderbird saves that begin with an mbox `From - date` line.
- A file without a `MIME-Version` line is still mail when it has sender, recipient and MIME body headers.
- ISO-2022-JP and UTF-7 bodies are read.

Body:

- The body is read once. If there is an HTML body it is used; HTML fragments separated by attachments are all read in order.
- If there is only a plain-text body it is read as text.
- If the HTML body has no visible text (a single picture, or only hidden preview text and `&zwnj;` padding), the plain-text body is added after it.
- In `multipart/mixed`, a plain-text body and an HTML part (signature, footer) are both read in order.
- A `format=flowed` plain-text body has its soft-wrapped lines joined again.

Headers:

- Subject, From and the other headers are placed first, with RFC 2047 encoding (`=?utf-8?b?…?=`) decoded. CP949 words labelled as ISO-8859-1 are decoded to Hangul as well.

Forwarded and encrypted mail:

- A mail forwarded as an attachment (`message/rfc822`) follows the outer body under a `## Forwarded message` heading with its own headers and body (up to 20; base64-wrapped ones are unwrapped).
- Mail encrypted with S/MIME or PGP raises `EncryptedDocumentError`.

Meeting invitations:

- A meeting invitation (Outlook's `text/calendar` part) is added after the body as an `Event:` list with Title, Start, End, Repeats, Location, Organizer and Attendees.
- Lines folded in the middle of a Hangul character are unfolded at the byte level.
- Times keep the time zone as written. The end of an all-day event is its last day (omitted for a one-day event). Recurrence is written like `weekly on Tuesday, 4 times`.

Attachments:

- Attachments are not read and a warning is added. This covers parts that only carry a name, PDFs displayed inline in the body, and .ics attachments.
- S/MIME and PGP signature parts are not counted as attachments.
- For Outlook's `winmail.dat` a warning says it cannot be read.

mbox:

- An mbox of several mails is written as `## Message N` + headers + body for each mail, even when the first mail is HTML (up to 100 mails; beyond that a warning).
- `>From` lines in mbox bodies are restored to `From`.

## TXT and MD

Section unit: the whole document. Design notes: [decisions/encoding.md](decisions/encoding.md).

- Encoding detection: UTF-8, CP949, UTF-16 (including UTF-16 without a BOM) and Windows-1252.
- Whitespace of the original is preserved.
- Stray NUL characters are removed.
- Terminal control codes in TXT (color `ESC[31m`, window-title OSC, character set `ESC(B` and so on) are removed.
- In a fixed-width export that pads columns with NUL, runs of NUL become spaces.
- In CP949 text with a few broken bytes, only those characters are replaced with � and a warning is added; `metadata.encoding` gets a `(replaced)` suffix, e.g. `cp949(replaced)`.
- Images embedded in MD as data: URIs (`![](data:…)`, HTML `<img>`) are extracted as images; the base64 is not left in the text.

## Image files

Section unit: the whole document; a multi-page TIFF is split into pages (`<!-- Page N -->`).

- The vision model describes the image and transcribes its text.
- Animated GIF and AVIF: first frame only.
- A JPEG above about 179 million pixels is read reduced (1/2 to 1/8).
- A very large image that cannot be read reduced raises `DocumentTooLargeError`; if it is a picture inside a document, it is left as a placeholder with a warning.

## Common rules

### Format detection

Design notes: [decisions/detect.md](decisions/detect.md).

- The format is determined **from the content (magic bytes), not the extension**.
- "Excel download" files served as `.xls` are read as what they really are, with a warning: HTML tables, Excel 2003 XML spreadsheets, TSV/CSV (including files with a few title or query-condition lines in front), and BIFF records not wrapped in OLE (PHP style; CP949 when there is no code-page record).
- A warning is also added when a text extension (.txt, .csv, .html and so on) holds a binary document such as Excel, PDF or an image. No warning is given between text formats.
- Exception: a `.csv` or `.tsv` that starts with HTML document tags and contains a table (`<table>`, `<tr>`, `<td>` and closing tags) is read as an HTML table with a warning. A `.txt` stays text.
- JSON (including JSON Lines) whose values contain HTML strings, and files with code, configuration or log extensions (.json, .py, .js, .ts, .yaml, .log and so on), are read as text, not HTML.
- XML data that starts with an XML declaration (`<?xml …?>`) and whose top element is not an HTML element (for example the `<NewDataSet><Table>…` of .NET `DataSet.WriteXml`) is read as text unchanged, even if it has elements with HTML names such as `<Table>`.

### Input

- `convert` takes the file content (`bytes`, `bytearray`, `memoryview`). A path string, a `Path` or a file object raises `TypeError`; use `convert_file` for a path.

### Text normalization

- All text is normalized to **NFC**. This prevents decomposed Hangul (from macOS and HWP) from becoming a different string in search and embeddings.
- Latin ligatures (`ﬁ`, `ﬂ` and so on) are expanded.
- NFKC is not used, so characters such as ①, ㈜ and ² are left as they are.

### Tables

- Tables are Markdown. **A vertically merged value is repeated on every row**, so each row states by itself which category it belongs to.
- A table that has horizontally spanning cells (HWP, HWPX, DOCX, PPTX, HTML) is written as an **HTML table** (`<table>`, `rowspan`, `colspan`). A Markdown table cannot draw a horizontal merge, so the meaning of the empty cells would be lost. Because the output is LLM input, cell text is left as is; only table tags that could be confused with cell and row boundaries (`<td>`, `</tr>` and so on) are escaped with `&lt;`.
- PDF, Excel and CSV tables are always Markdown, because merge information is spread into the cells while reading. A horizontal merge in Excel is repeated in each spanned column only in data rows; a merge in a table title or header row, or one that extends over unnamed empty columns, keeps its value in the first cell only.
- A one-cell title row at the top of a table becomes a paragraph above the table. A one-column table (a layout box) is unfolded into paragraphs.

### Nested tables

- **A table inside a table** (HWP, HWPX, DOCX): the outer table is written as HTML and the inner table is placed inside the cell as a one-line HTML table.
- When the outer table is a one-column box, the box is unfolded into paragraphs and the inner table becomes an independent table.
- A table inside a footnote or endnote is flattened to one line as `cell / cell; cell / cell` (cells separated by ` / `, rows by `; `). A table in a header inside a cell is flattened to `cell / cell` lines. These are places that cannot hold a table.
