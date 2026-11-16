# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

A sixth review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **XLSX**: a styles part with more than 131,072 entries or 1,000,000 elements is replaced before loading by one that
  keeps only the display formats of the first 65,536 cell formats, with a warning (a styles part written in UTF-16 is
  counted too). A 283 KB file with one cell and
  1,000,000 cell formats took 7.8 seconds and 2.3 GB (now 0.7 seconds and 350 MB); 1,000,000 differential formats took
  21 seconds.
- **PDF**: a line reaching out of the cells of a ruled table no longer scans every character of the page, and the cell
  text of a ruled table is read row by row from the characters at its height, with the same output (a 600-row table
  with a mark beside each row: 54 seconds → 4 seconds). The reading-order step that sets lines beside two columns below
  them carries what lies above instead of rescanning it, and the one that reads alternating sides in turn looks at no
  more than 1,000,000 lines per page (20,000 alternating lines: 21 seconds → 4.4 seconds). Label-value pairs beside
  another block are checked against the rows around them only (8,000 rows: 12 seconds → 0.05 seconds).

### Fixed

- **PDF**: a picture drawn at a position that is not a finite number (a scale of `1e200` twice) no longer rejects the
  whole document as corrupted; neither does a page that `/Count` promises but pdfium cannot open when pdfminer cannot
  read the document. A vertical merge is repeated only into the rows it covers, so a table whose columns merge
  staggered pairs of rows no longer copies one merge's text into another's row. An open-sided table whose merged cells
  break every inner rule is read once instead of twice. Two rows of values between two rules are no longer joined into
  one row as a two-line header.
- **HTML**: cells that lxml leaves outside a row are read, as a browser reads them: cells wrapped by a `<form>`, `<div>`
  or `<label>` inside a row, and cells written straight under the table or after a missing `<tr>` (they used to vanish,
  often leaving "No text was extracted"). A struck italic link followed by more struck text no longer gives `~~~~`. A
  heading holding only a picture is the picture, not an empty `#`. A `data-src` written as `DATA:` no longer turns the
  real picture into a lazy-load placeholder.
- **Mail**: a decoded word next to plain text gets no space added, so `"=?euc-kr?B?…?=" <a@b>` is `"홍길동" <a@b>`
  instead of `" 홍길동 " <a@b>`, and an attachment named `=?utf-8?b?…?=.xlsx` keeps its name whole.
- **Pictures in quotes**: a picture in a quoted table row stays in its cell and a picture in a quoted paragraph stays in
  the quote (a reply quoting a table with a photo used to split the table in two and set the photo outside the quote).
- **DOCX / RTF**: a heading paragraph holding only a picture is the picture, not an empty `#` (or `# 1.`).
- **Markdown**: a `data:` picture written in capitals (`DATA:IMAGE/PNG`) is extracted instead of leaving its base64 in
  the text.
- **XLSX / XLS / Excel 2003 XML**: number formats round the value to 15 significant digits first, as Excel does, so
  `0.7*1.5` under `0.0` is `1.1` (was `1.0`). An XLS zero duration under `[h]:mm` is `0:00` instead of `0`, and an XLSX cell under `[hhh]:mm` keeps
  its days (`36:00`, was `12:00:00`).

## [0.3.8] - 2026-11-16

A fifth review of the whole source. The sample outputs in `examples/` are unchanged.

### Added

- **HWP / HWPX**: hyperlinks are kept as `[text](url)` under `keep_links`, as in the other document formats (http,
  https and mailto only; a link to a bookmark or a file keeps its text).
- **HTML**: ruby is written as `base(reading)` (`東京(とうきょう)`), as in DOCX, ODF and HWP, also without `<rp>`
  (rubies nested more than eight deep are plain text); content folded with `hidden="until-found"` (the standard way
  to fold FAQ answers and accordion sections) is read.

### Changed

- A DOCX, PPTX, XLSX, HWPX, OpenDocument or EPUB file protected with a ZIP password (`zip -e`) is an
  `EncryptedDocumentError` instead of a `CorruptedDocumentError`, as `docs/options.md` describes. A protected part
  that is only skipped (one slide, one picture) still leaves the document readable; a file that gives nothing at all
  because its protected parts were all of its content (a deck whose slides alone are protected) is encrypted too.

### Security

- **DOCX / PPTX**: an imported part (`w:altChunk`) or a SmartArt referenced many times stops at `max_chars` inside one
  paragraph, cell, header or slide, and inside a table nested in a cell at most 100,000 characters past it; a SmartArt
  or chartex part is read once. 200 references to one 400,000-character chunk from a 39 KB file took 6.8 to 19.5
  seconds and up to 1.9 GB (now 0.1 to 0.2 seconds).
- **DOCX / HWP / HWPX**: a list number text is split once per list and bounded at 255 characters (longer ones are the
  level's own number). A 100 KB `w:lvlText` on 300 empty numbered paragraphs took 2.3 seconds.
- **Charts**: multi-level categories join at most their innermost eight levels (16,000 levels in a 125 KB file took
  11.7 seconds and 1 GB).
- **PDF**: growing the shaded tables of a page no longer goes through every other table of the same columns for each
  one, with the same output (1,200 separate shaded tables took 4.1 seconds; now 0.7 seconds).
- **ODT / ODP**: the value table of a chart stops at `max_chars`, as in the other formats (20,000 rows under a 5,000-
  character limit counted 517,000 characters).
- **RTF**: a table inside a text box, header, footer or footnote is checked against the character limit while it is
  read (100,000 rows in a text box: 4.4 seconds → 0.02 seconds).
- **Pictures**: the tiles of a very tall picture are cut and encoded one at a time (a 3000×40000 PNG peaked at
  1,124 MB, now 710 MB), with the same bytes.

### Fixed

- **PDF**: a ruled table lying off a rotated page no longer rejects the whole document. A caption or note set tight
  against an open-sided or shaded table no longer also comes out inside its edge cell. Form fields whose widget is
  hidden (Hidden or NoView flag) are left out, as hidden FreeText annotations are. A subscript lowered far enough that
  pdfminer splits it onto a line of its own (`<sub>` in reportlab) is joined back (`H_2O` instead of `H`, `O`, `2`).
- **DOCX / PPTX**: a second relationship of the same type (numbering, styles, document properties, notes, layout)
  no longer rejects the document or skips the slide; the first one is used.
- **ODT**: footers are counted after the body, so a body table near the limit no longer stops short.
- **Text / CSV**: short Portuguese and German text in Windows-1252 (`Descrição`, `INFORMAÇÃO`, `Größe`, `gemäß`) is no
  longer read as CP949 hanja or Hangul without a warning.
- **HTML**: strikethrough across a blank line made of `<br><br>` is marked per paragraph; the suggestions of a
  `<datalist>` no longer come out glued into one word after the field.
- **All formats with inline marks**: a bold, italic, struck or script run of nothing but invisible characters (zero-width
  space, soft hyphen) no longer leaves empty marks (`****`, `~~~~`), and such a character no longer splits two runs of
  the same formatting.
- **LangChain / LlamaIndex**: in `mode="sections"`, the slides, sheets and pages past the `max_chars` cut give no piece;
  a slide past the cut used to come back as a title-only Document.

## [0.3.7] - 2026-11-15

A fourth review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **RTF**: the tables inside a table share its 500,000-cell limit, and a table holds at most 10,000 tables inside it;
  the tables after that are left out with a warning. The depth of a table is set per paragraph, so cells that opened
  and closed 15 nested tables each took 10.7 seconds and 1.84 GB for 2.5 MB, without a warning (now 1.1 seconds).
- **PPTX**: a slide table stops at `max_chars` cell by cell, as DOCX, HWP and HWPX tables do (1,000,000 rows: 20 seconds
  and 3.2 GB → 1.8 seconds). The value table of a chart in a PPTX, DOCX, XLSX or HWPX document stops once it passes the
  limit, and display formats are worked out only for the rows written (1,000,000 cached points: 9 seconds and 2.5 GB →
  2.5 seconds).
- **CSV**: a row with a quoted cell, or any row after a stray quote (`27" monitor`), is cut at the column limit like any
  other; one quoted cell in front of 20 million cells took 3.5 seconds and 900 MB (now 0.4 seconds).
- **PDF**: a page with many ruled cells holding pictures, many separate shaded tables or thousands of drawn check boxes
  beside text no longer scans the whole page once per cell, table or box, with the same output (a 3,600-cell picture
  grid spent 30 seconds reading its table, 400 shaded tables took 13 seconds, 8,000 check boxes 33 seconds). A long
  run of left-aligned lines between two table rules is no longer walked from every line to its end (12,000 lines took
  9.5 seconds); a first cell wrapped onto more than 64 lines between rules is no longer grouped from its first line. A
  page larger than the rendering pixel cap is opened once for all its pictures instead of once per picture (a page with
  3,600 pictures took 12 seconds, now 2), with the same images.

### Fixed

- **Pictures**: when the text alone is over `max_chars`, the pictures before the cut keep their placeholder
  (`[Image: alt text]` or `[Image]`) instead of vanishing with their alt text, so the MCP server shows them in long
  documents too; they are still not decoded, and dividers and icons that decoding drops are left out by the size in
  their header. A 32-bit float picture with a NaN or infinite pixel keeps its gradient
  instead of coming out black.
- **DOCX**: a header or footer reference without a relationship id no longer rejects the whole document when headers
  and footers are included; only that header is skipped.
- **XLSX**: a damaged relationship part of one sheet no longer rejects the workbook: the cells are converted and that
  sheet's comments, pictures, charts and form controls are left out with a warning.
- **Excel 2003 XML**: time-only values follow the cell's time format (`h:mm AM/PM` → `1:30 PM`, as in XLSX), and
  `<Row ss:Span>` counts the rows it stands for, so R1C1 formulas and vertical merges below it point at the right rows.
- **RTF**: a `{\upr{…}{\*\ud{…}}}` group is read from its Unicode copy instead of the ANSI copy, so a Korean title or
  paragraph in a CP1252 document no longer comes out as `??`. Any other shape of `\upr`, or one whose Unicode copy is empty, is
  read as before.
- **HTML**: strikethrough around several paragraphs, list items or a quote is marked per block (`~~a~~` / `~~b~~`)
  instead of one pair across the blank line that GFM shows as literal tildes, and two pairs never touch (`~~a~~~~b~~`),
  even where an empty or inline-drawn heading, a picture or a refused link sits between struck runs; a struck table cell
  holding a table no longer leaves a lone `~~` paragraph, and strikethrough no longer changes whether a table is drawn as
  a table or unfolded. A `rowspan` stops at the end of its `thead`, `tbody` or `tfoot`, so a body cell no longer pushes
  the cells of a total row one column right, and a `<form>` or `<font>` written between rows does not end it. An empty
  `<tr></tr>` no longer turns the first pair of a name/value table back into a header.
- **Legacy .doc / .ppt**: a temporary folder that cannot be written or read is reported as such (`conversion_failed`)
  instead of "LibreOffice could not be started", and the program lookup is kept.

## [0.3.6] - 2026-11-15

### Added

- **PDF**: footnotes are linked where the page proves them. When a page has a short footnote rule with smaller notes
  under it, raised note numbers that run on, and exactly one raised mark in the body for each note number, in the order of
  the notes, the mark becomes `[^1]` and the note `[^1]: …` at the bottom of the same page (a number used again on another
  page is written with its page, `[^p12-1]`). Every other page is left exactly as before (`135^1%`), so no note is attached
  by a guess: a page that also has `m^2` and a note 2 is not linked. On 120 documents with real footnotes (1,580 marks),
  864 marks were linked and none to the wrong note.

## [0.3.5] - 2026-11-15

A third review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **PDF**: a page whose ruling lines cross more than 8,500 times (a table of R rows × C columns has (R+1)(C+1)
  crossings) is read as if the lines were not drawn, with a warning; an empty 150 × 150 grid took 14 seconds per page
  and a 300 × 300 grid 96 seconds. Finding the cells inside merged ruled cells no longer takes quadratic time. The
  attachment name tree is read up to 10,000 nodes and 100,000 entries.
- **Spreadsheets and CSV**: splitting a sheet into tables takes linear time however many empty-row blocks it has (a 900 KB
  CSV of one-row blocks took 226 seconds). A very wide ODS row (with `max_sheet_cols` raised) converts in linear time.
- **MCP server**: a file is reached from its allowed folder one name at a time and never through a link swapped in
  after the check, so a folder replaced by a symbolic link cannot lead out of the allowed folders (Linux, macOS); only
  a regular file is read and a descriptor is closed on every refusal; `create_server` refuses a root that is not a
  folder.
- **Pictures**: a picture inside a document that runs out of memory stays a placeholder with a warning and the text is
  kept; a picture that is the document is "too large or too complex". `convert` reports running out of memory anywhere
  that way instead of as a corrupted document. Preparing a large picture needs about a third less memory.
- **DOCX**: header and footer parts that come after the text has passed `max_chars` are not rendered (40 header tables
  of 5,000 rows took 72 seconds).

### Fixed

- **Mail**: each mail of an mbox, each forwarded mail, body part and meeting invitation is rendered as a document of
  its own, so an unclosed comment, style, title, quote or hidden element in one mail no longer swallows or deforms the
  mails after it; an event with `STATUS:CANCELLED` is marked cancelled; one broken encoded word no longer leaves the
  other words of a header encoded, and a character split over two encoded words is joined.
- **Encoding**: BOM-less UTF-16 whose text is ASCII is read as UTF-16 instead of UTF-8 with NULs, and so is a part
  declared UTF-16LE/BE when its bytes bear the label out (ASCII or UTF-8 text with that label is still read as what it
  is); declared UTF-8 with a broken byte is read as UTF-8 with a warning instead of Windows-1252.
- **HTML/EPUB**: an inline SVG title no longer becomes the page or chapter heading; text shaped like the nested-list
  placeholder no longer rejects the document; lists in quotes inside list items stay in the quote, and item text
  starting with `|` or a fence stays on its bullet; a paragraph that is only a fence line no longer breaks a `<pre>`
  block next to it; `display:noneX` no longer hides; line breaks in an `href` are removed; `<object>` and `<canvas>`
  fallback content is read; CDATA in an EPUB style no longer leaks markup.
- **Text**: in TXT and Markdown files `~~~` fences are recognised and fences are paired as CommonMark pairs them; in
  every other format a line of tildes is text and is written `\~~~`; a nested list or code block inside a blockquote
  keeps its indentation; bidi isolates, C1 controls and other invisible marks are removed (U+0085 becomes a line
  break); document metadata comes out as clean one-line NFC values.
- **PDF**: a page whose MediaBox has no area is read as US Letter, like pdfium, instead of losing all of its text;
  unused codes of a font that names WinAnsiEncoding (reportlab's list bullet 0x7F) are read as `•` instead of being
  dropped with a mapping warning; words drawn right to left without space glyphs are read in place; a drop cap
  standing on a line beside it is joined to its word without splitting the paragraph; a direct file specification or
  entries that are no nodes can no longer hide another attachment; a rule or shape drawn to an infinite position no
  longer rejects the whole document.
- **PDF tables**: a ruled table whose columns are merged over different rows stays one table; a borderless table with
  one or two shaded data rows reads as if it had no shading; sparse borderless tables keep their rows with empty cells
  and label-only rows instead of scattering them column by column; a label-only row no longer leaves the merged cells
  of a borderless table unsplit.
- **DOCX/PPTX/XLSX**: a relationships part that is not XML no longer rejects the document (the part that owns it opens
  without relationships); percent-encoded relationship targets are read (pictures, slides, notes, charts, sheets,
  comments and drawings; a part named as written wins over the decoded name), and the address of an external
  relationship is not decoded into one; a list start number or chart point index of 4,300 digits no longer rejects the
  document; a field whose `separate` is in a hidden run keeps its result; a header table is no longer cut short by the
  body's characters under `max_chars` (its own characters still count); whole chart values from 10^15 on keep all
  their digits.
- **Inline formatting**: script and strikethrough marks close at a line break (`10~~^2~~` instead of `10~~^{2~~}`); a
  non-breaking hyphen or symbol stays inside its run's bold or struck span.
- **Spreadsheets**: a shared formula that cannot be moved to its cell no longer rejects the workbook; two sheets with the
  same name each keep their own comments, drawings and hidden state; `[h]:mm` truncates below the minute like `h:mm`;
  `?` digit places pad with spaces; XLS NaN and infinity cells are blank with a warning; BIFF5 chart-sheet names are read
  in the workbook's code page; English unit and as-of notes (`(Unit: USD)`, `(in thousands)`, `As of …`) in a table's top
  row are moved above the table, as the Korean ones were.
- **HWP/HWPX**: a note inside a note no longer gives both notes the inner note's number, and empty notes leave no
  `[^n]:`; numbered paragraphs in headers, footers and notes no longer move the body's numbering; text art keeps its
  caption; an `hp:switch` case without text no longer hides the fallback or registers its notes and comments twice; a
  broken style or picture record in DocInfo no longer shifts the styles and pictures after it.
- **ODF**: `text:numbered-paragraph` keeps its label, as text when the document defines no list style for it; a list
  label that is `#` or `>` stays text. **Equations**: a backslash, caret or tilde in Hancom equation text is valid
  inside `\text{}`.
- **Pictures**: Pillow warnings no longer leak (a readable picture was rejected when warnings are errors); PA and
  other alpha modes stay transparent, and the transparent colour of a PNG makes exactly its pixels transparent;
  scanned pages left out by `max_images` are named in the warnings, and PDF pages past the render budget or that could
  not be rendered are no longer said to be handed over as images.

### Changed

- `ConvertOptions` switches accept only `True` or `False`; any other value raises `TypeError` when the options are made.

## [0.3.4] - 2026-11-14

A second review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **Reading files**: `convert_file`, the CLI and the LangChain and LlamaIndex adapters read a pipe or a device (`/dev/zero`)
  only up to `max_file_bytes`; before, the size check came after reading it to the end. `IsolatedConverter` refuses an
  oversized or empty input before it is sent to a worker.
- **XLS**: merge ranges written back to front no longer cancel out the merged-cell limit (a 461 KB file took 16 seconds).
- **PDF**: a small crafted file no longer holds one page for tens of seconds through thousands of FreeText annotations, one
  long letter-spaced line, thousands of check-box shapes, faint watermark characters, shading beside a ruled table, a long
  zebra table, many highlighted lines, a borderless "table" of a thousand columns, a long paragraph or stacked text drawn
  many times. The running character limit also counts sideways text and form values, so such a document stops at the limit.
- **PDF layout analysis**: pdfminer's grouping of lines into text boxes and of text boxes into reading order is bounded per
  page (the page itself and every form drawn on it); past the bound the page is read in pdfminer's plain order with the
  existing "read without text-box reading-order analysis" warning. A 20 KB page took 62 seconds, a 68 KB page two minutes.
  A single huge character, or two characters far apart, no longer makes pdfminer fill millions of index cells.
- **XLSX**: scanning a sheet for formula results, the shared strings and the sheet parser no longer keep every row in memory
  (a 600,000-row sheet took 948 MB more), and a sheet without `<dimension>` is no longer parsed twice to measure it.
- **Mail**: mail forwarded inside forwarded mails is limited to 100 levels, counted along the structure the mail parser
  follows (part headers quoted in a body or in a text attachment are text).
- **Release**: the PyPI publish action is pinned to a commit.

### Fixed

- **PDF**: a row of a borderless table that pdfminer grouped differently came out in two tables, splitting the table in
  two, and the header of the lower of two stacked borderless tables also ended the table above as a row of its own;
  columns to the right of a nested table were shifted; FreeText labels were lost on pages whose MediaBox does not start
  at 0,0 or that are rotated, and image clips were ignored when the MediaBox starts right of 0; text at a non-finite
  position rejected the whole document or leaked off-page copies into the body; a ruled table continued on the next page
  without its header lost the text of a merged cell left open at the page end.
- **DOCX/PPTX**: one damaged picture member rejected the whole document; an empty equation wrote `$$`, which swallowed the
  following text, and `$ % # { } \` typed in an equation were read as LaTeX; a footnote reference with no note left a
  dangling `[^1]`; a struck superscript ending in a space gave `~~^2 ~~`; a comment quoted moved-away, deleted or hidden text.
- **Spreadsheets**: an integer too large for a float rejected the whole XLSX; an unclosed quote in a small CSV swallowed the
  rest of the file without a warning; a large hyperlink range forced streaming and lost merges; `1E+400` in Excel 2003 XML
  became `inf`; a literal between decimal places (`0.0\E+0`) was lost.
- **HTML**: `visibility:visible` content inside a `visibility:hidden` element, and pages hidden with
  `<body style="visibility:hidden">` until a script runs, came out empty; a page title written over several lines broke the
  heading; a `# comment` in a code block kept the title out; `~~`, `^` and `_` marks were put inside code; an empty list item
  holding a nested list lost its indentation; `rowspan="0"` spanned one row; alt text could close `[Image: …]` and form a
  link; struck elements next to each other (`<del>a</del><del>b</del>`) gave `~~a~~~~b~~`, which is not strikethrough for a
  renderer.
- **Mail**: one HTML-only mail left a whole `.txt` mailbox undecoded; an HTML attachment was read instead of the body of an
  MHTML-shaped mail; a forwarded mail that is itself a forward lost its body; meeting invitations took the reminder's title and
  attendees, and a recurrence rule ending in `;` stayed raw; short broken UTF-8 could be read as UTF-16; a multipart mail
  with an empty boundary (`boundary=""`) was rejected, as was a mail whose parts were counted deeper than they nest.
- **ODF**: hidden text (hidden spans, hidden text fields and paragraphs, hidden conditional sections) came out as body text;
  a nested list with its own style never restarted its numbering; a heading's number was glued to it (`# 1.Intro`), and a
  heading in a numbered list was not counted; `text:continue-list` now continues the list it names.
- **HWP/HWPX**: a broken DocInfo stream, or an HWPX member with an unknown compression method or the encryption flag,
  rejected the whole document instead of only that part.
- **RTF**: a code page that does not read ASCII as ASCII (EBCDIC) turned the text into noise; a `\u` fallback that a writer
  counted in characters left half a double-byte character.
- `detect_format` called on its own raises `CorruptedDocumentError` instead of library exceptions.
- `describe_images` no longer re-renders the document every few pictures when many are judged decorative near `max_chars`.

### Changed

- HWP documents protected with DRM or a public certificate raise `EncryptedDocumentError`.
- The MCP server keeps its last four results (by file content), so `document_outline` followed by `convert_document` by
  sections converts the file once.
- `detect_format` raises `TypeError` for content that is not bytes, bytearray or memoryview, and for a file name that is
  not a `str`, as `convert` does.

## [0.3.3] - 2026-11-14

A review of the whole source for defects. Ordinary documents convert as before (the sample outputs in `examples/` are
unchanged); what changed is what happens with damaged and hostile files.

### Security

- **Format detection** no longer takes time quadratic in the file size on a file with `%PDF-` after its first byte (40 KB
  took 5 seconds; every upload passes through this check).
- **PDF**: a stream compressed twice (`/Filter [/FlateDecode /FlateDecode]`) is charged against `max_uncompressed_bytes` at
  every stage. A 1.1 KB file used to inflate to 200 MB under a 10 MB limit.
- **Images**: pictures inside documents are opened with a fixed list of raster formats, so an EPS no longer reaches Pillow's
  Ghostscript plugin; a progressive JPEG over the pixel limit is refused instead of decoded (1.5 MB on disk took 946 MB).
- **XLSX**: row numbers the file skips cost nothing (a 14 KB file made ten million empty rows, 2.6 GB), and the streaming
  checks cannot be avoided by storing a sheet outside `xl/worksheets/`.
- About forty places where a small crafted file took tens of seconds (regular expressions that backtracked, work repeated
  for every reference to the same part, searches that rescanned the page) now take time proportional to the input, in PDF,
  HTML, mail, spreadsheets, DOCX, PPTX, ODF and HWPX. The decision records list each one, with what remains.

### Fixed

- **PDF**: the first line of a wrapped cell in a borderless table at the top of every page was deleted as a repeated header
  in documents of three or more pages, and the table fell apart; on two-column pages such a table could come out twice.
- **PDF**: a document was rejected when pdfium could not open one page, and a dense page was charged twice against the
  decompression limit; one unreadable annotation dropped every form value of its page.
- **HTML**: an element with `backface-visibility:hidden` was removed as hidden; an outer table took the caption of a table
  nested in a cell; a `DOCTYPE` inside the body came out as text; `data:` pictures written with capitals were skipped.
- **Mail**: a subject or attachment name that is not a decodable encoded word rejected the whole mail.
- **DOCX**: a header shared by several sections was rendered once per section (`1. Company … 4. Company`); a `gridSpan` of 0
  lost the next cell; documents without a body, with generically typed style parts or with a field begun inside a
  superscript were rejected.
- **Spreadsheets, charts, HWP, ODF**: single values no longer reject the whole document (a large negative elapsed time, a
  date past the calendar, attributes of thousands of digits, a surrogate in an HWP auto number, `ss:Index="0"`).
- **`IsolatedConverter`**: conversions under way when `aclose()` is called end with the closed error instead of "too large",
  and the pools are shut down once; the converter can be used from one `asyncio.run` after another and after a fork.
- Text behind a byte order mark that had to be decoded with replacement characters now gives the usual warning.

### Changed

- A chart or section referenced several times in one PPTX, DOCX or HWPX is written in full once; later references give the
  chart's title line.
- Number formats longer than Excel allows (255 characters for charts, 1,024 for cells) are not applied.
- The warning about detached package parts names only plain part names, at most four; attachment names in the mail warning
  are kept on one line and cut at 100 characters.
- A Hancom equation with a repeated script on one term (`x^2^3`) keeps the original script instead of LaTeX that does not
  compile.
- The release workflow runs the tests before it builds.
- The extractor modules are split by topic: no module is longer than 1,300 lines (`pdf.py` had 9,400). The public API and the
  output are unchanged. Code that imported internals must follow them: `extractors._shared` is gone, and XLS, Excel 2003 XML
  and CSV are read by `extractors.xls`, `extractors.xml_spreadsheet` and `extractors.csv_text`.

## [0.3.2] - 2026-11-13

### Added

- **`opendockit-mcp` on PyPI**: a launcher package with no code of its own, so that the MCP server starts with
  `uvx opendockit-mcp`. It installs `opendockit[mcp]` at the same version. `pip install "opendockit[mcp]"` and the
  `opendockit-mcp` command it brings work as before.
- The MCP server is listed in the [MCP Registry](https://registry.modelcontextprotocol.io) as
  `io.github.hyeonsang010716/opendockit`.

## [0.3.1] - 2026-11-13

### Added

- **Hermes Agent plugin** (`hermes-plugin/`): a portable Agent Plugins v1 package that gives Hermes the `opendockit-mcp`
  tools and a skill for reading long documents in parts. It is not part of the Python package.

### Fixed

- **MCP server**: it no longer announces prompts and resources, which it does not have. Clients that build the agent's
  tool list from the announcement (Hermes adds four tools per server) showed tools that could only return empty lists.

## [0.3.0] - 2026-11-13

### Added

- **LangChain**: `opendockit.integrations.langchain.OpenDocKitLoader` and `OpenDocKitParser`
  (`pip install "opendockit[langchain]"`).
- **LlamaIndex**: `opendockit.integrations.llama_index.OpenDocKitReader` (`pip install "opendockit[llamaindex]"`), usable
  as a `file_extractor` of `SimpleDirectoryReader`.
- **MCP server**: `opendockit-mcp` (`pip install "opendockit[mcp]"`) lets an agent read local documents as Markdown, whole
  or by page / slide / sheet, with a bounded answer and an optional `--root` restriction.
- All three give one piece per file or one per page / slide / sheet (`mode="sections"`) with the same metadata.
- Korean and Simplified Chinese READMEs.

### Fixed

- `IsolatedConverter`: an idle worker no longer outlives a server process that was killed (SIGKILL, the out-of-memory
  killer). It used to stay for ever, together with multiprocessing's fork server and resource tracker.

## [0.2.0] - 2026-11-13

### Added

- **OpenDocument** (`.odt`, `.ods`, `.odp`), read directly from the package: headings, lists, merged cells, footnotes,
  charts, formulas as LaTeX, several tables per sheet, speaker notes.
- **RTF**, with a built-in parser: legacy code pages (CP949 and others), tables with merged cells, lists, footnotes,
  pictures.
- **EPUB**: chapters in reading order, pictures stored in the book, title and author metadata.
- **Word 97-2003 (`.doc`) and PowerPoint 97-2003 (`.ppt`)** through LibreOffice when it is installed. The
  `OPENDOCKIT_SOFFICE` environment variable sets the program path; an empty value disables the bridge.
- **Links and emphasis.** `ConvertOptions.keep_links` and `keep_emphasis` (both on by default) keep hyperlinks as
  `[text](url)` and bold / italic as `**bold**` / `*italic*` in DOCX, PPTX, HTML, ODT, ODP, RTF and EPUB.
- `DocumentFormat` has the new members `odt`, `ods`, `odp`, `epub`, `rtf`, `doc` and `ppt`; code that branches on
  `result.format` will see them.
- A comparison benchmark against four other converters in `benchmarks/`.

### Changed

- **Default output changes** for documents that contain links, bold or italic: the marks and addresses are now part of
  the text and of `char_count`. Pass `keep_links=False, keep_emphasis=False` for the 0.1 output.
- RTF, ODF, EPUB, `.doc` and `.ppt` files are no longer rejected with `UnsupportedFormatError` (`.doc` / `.ppt` still are
  when LibreOffice is not installed).

### Fixed

- PPTX: a text box on the notes page that holds only a field (the slide number) is no longer written into the speaker
  notes as a `<number>` line.
- Text that has the shape of an internal placeholder is no longer taken for one. Cell text imitating the nested-table
  placeholder made a table inside a table be drawn again for every copy (a document under 1 KB could run for minutes),
  such text was also counted against `max_chars` although it is never written, which could cut a table short without
  `exceeded`; and `⟦IMG:1⟧` written in an imported DOCX chunk (`w:altChunk`) was replaced by the chunk's first picture.


## [0.1.0] - 2026-11-12

First public release.

### Added

- `convert`, `convert_file` and `aconvert`: PDF, DOCX, PPTX, XLSX, XLS, Excel 2003 XML, CSV, HWP, HWPX, HTML/MHTML,
  email (`.eml`, mbox), TXT, Markdown and image files to Markdown.
- `ConversionResult` with per-page / slide / sheet sections, metadata, warnings, extracted images and a character count.
- `ConvertOptions` with a character budget (`max_chars`) that stops conversion instead of truncating its output.
- `IsolatedConverter`: conversion in disposable worker processes with memory and time limits.
- `describe_images` and `OpenAIImageDescriber` (optional `openai` extra) to describe pictures and transcribe scanned pages;
  `OpenAIImageDescriber.check()` to fail fast on a missing extra or API key.
- `opendockit` command-line tool.
- `examples/`: sample documents for every format with their converted output.
- Optional `heic` extra for HEIC/HEIF images (kept out of the default install because its wheels bundle GPL/LGPL codecs).

[Unreleased]: https://github.com/hyeonsang010716/OpenDocKit/compare/v0.3.8...HEAD
[0.3.8]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.8
[0.3.7]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.7
[0.3.6]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.6
[0.3.5]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.5
[0.3.4]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.4
[0.3.3]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.3
[0.3.2]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.2
[0.3.1]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.1
[0.3.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.0
[0.2.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.2.0
[0.1.0]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.1.0
