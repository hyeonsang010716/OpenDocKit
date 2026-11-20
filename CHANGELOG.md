# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **DOCX saved as Strict Open XML is read.** It was refused with the advice to save it again; its namespaces and
  relationship types are now read as their Transitional forms, and the lengths Word writes there with a unit (`36pt`)
  as the twips and half-points they stand for, so indented lists and raised or lowered text come out as they do in an
  ordinary DOCX. A Strict PowerPoint or Excel file is still refused with that advice.
- **Windows is a supported platform.** The suite runs there for every change, and the sample outputs are identical to
  the ones Linux and macOS write.
  - `IsolatedConverter` keeps the memory limit it is given (the `ProcessMemoryLimit` of a job object, as `RLIMIT_AS` is
    on Linux); it used to be accepted, validated and then ignored, so a server ran every conversion without it. A worker
    also leaves with a server that was killed outright, as it does on POSIX.
  - **MCP server** and **Open WebUI loader**: a file is converted in a worker process there too. Both fell back to a
    thread of the server, which gave up the memory limit, the time limit and the process isolation at once.
  - **Legacy `.doc` and `.ppt`**: LibreOffice is ended with the process that started it, so a conversion whose worker is
    killed on its time limit no longer leaves it running, and a guard process removes the folder holding the uploaded
    document, as on POSIX. LibreOffice is also found where the environment does not name Program Files — an MCP
    client starts its server without PROGRAMFILES, and every `.doc` and `.ppt` sent to the MCP server failed with
    'need LibreOffice' — from the registry, which also finds an install outside Program Files. Its temporary folder
    is removed under a long temporary path and after the time limit too: both used to leave LibreOffice's profile
    behind.
  - **HTML, mail and EPUB**: malformed and multi-part pages are read whole. lxml's Windows wheels bundle a libxml2
    without the HTML5 tokenizer, so text after the end of a page, a second concatenated document, the content of
    `<textarea>`/`<xmp>` and the further parts of a mail were lost; html5lib builds the tree there instead. A page — or
    one body of a mail, whose attachments do not count — over 256 K characters, nested deeper than 1,000 elements, or
    leaving more than 16 raw-text elements open is still read by the older parser — html5lib is quadratic on those —
    and the result says so.
- **Hancom equations**: `root` without `of` is a square root (`3 root5` → `3 \sqrt{5}`; `30 root5 cos theta` gave
  `\sqrt[5]{\cos} \theta`), command words glued together are split (`costheta`), and `le`, `ge`, `nabla`, `LAND`, `LOR`,
  `SUPERSET`, `nsubset`, `BIGOPLUS` and similar words, `OVERBRACE`, `UNDERBRACE` and `BOX` are read.
- **Headings in Hangul documents that do not use the 개요 styles.** A paragraph with an outline head (개요 번호) is a
  heading of its level, whatever its style — Hangul builds the outline and the table of contents from it, and it came out
  as a numbered list; a numbered clause that is a sentence (`1. 갑은 을에게 … 지급한다.`, `가. … 따른다`) stays a
  numbered paragraph. A
  short top-level paragraph set 1.15 / 1.3 / 1.5 times larger than the size most body paragraphs are written in is a
  level 3 / 2 / 1 heading (in a form whose body is in tables, measured against the tables' text); dates,
  table-of-contents lines, the addressee, signature and stamp lines of a form, quoted lines, polite sentences, typed
  bullet items (not the `□` section mark), captions of figures and tables, contact lines and sentences written large
  stay text. In 34 real HWP / HWPX files, where every title was set by size on 바탕글, no heading came out before; now
  their chapter titles do.
- **Letter-spaced labels of Hangul forms** of three to six syllables are written without the spaces between them
  (`담   당   자` → `담당자`), so a search for the label finds it. Syllables one space apart stay as written (`수 및 양`,
  the choices `상 중 하`), and so do two syllables spaced wide (`성   명`, the choices `남    여`), the choices of a scale
  spaced wide (`상   중   하`) and blank dates (`년   월   일`).
- **Bold and italic in HWP and HWPX** under `keep_emphasis`, by the same rules as DOCX (`3. **시험기간** : …`).
- **Headers of Hangul documents that hold words** (a form's number and title) are written once before the body even
  without `include_headers_footers`, as plain paragraphs without their pictures. In 9 of 34 real files that text was
  nowhere else and was dropped without a warning. Page numbers and footers still need the option.

### Changed

- **Pictures are prepared faster.** An RGBA PNG that needs no change is passed on as it is instead of being compressed
  again (only the chunks a rewrite keeps are copied, so metadata is still stripped). A 7 MB HWP with 51 pictures
  converts in 118 ms instead of 549 ms.

### Changed

- **Faster start.** An extractor is imported the first time its format is converted, instead of all of them with the
  package. `import opendockit` takes 0.07 s and 43 MB instead of 0.23 s and 101 MB, and a fresh process converting one
  small DOCX 0.09 s instead of 0.25 s — what a command-line run and every Windows worker pay per file. The workers of
  `IsolatedConverter` still start with every extractor loaded.
- **DOCX conversion is 12-19% faster** on large documents (20,000 paragraphs, 80,000 table cells); the output is the same
  to the byte.

### Security

- **MCP server on Windows**: a path on a network share (`\\host\share\…`) is refused before anything is looked up,
  unless that share is given as a `--root` — with other roots and without any — and a device path (`\\.\…`) always.
  Looking a share up connects to its host and signs in with the user's Windows credentials, and the roots were
  checked only after the path had been looked up: an agent talked into such a path by a document it read sent the
  user's NTLM hash to the host the document named, even though the call was then refused. A share to be read is
  given as `--root \\host\share`; a mapped drive letter is an ordinary folder.
  The NT form of a path is held to the same rule: `\??\UNC\host\share\…`, which Windows sends to the kernel
  as it is written, was taken for a local path and, on Python 3.12, looked up.

### Fixed

- **DOCX lists** built in two ways ECMA-376 defines but the reader did not follow are numbered: a list whose definition
  points at a numbering style that no definition claims (it now uses the list the style names), and a paragraph style
  that only a numbering level names. Both came out as plain paragraphs.
- **DOCX**: a document whose own style is named 'Heading' (Apple Pages' first heading level) gets its headings; a legacy
  VML picture keeps the alt text written on its shape.
- **DOCX**: a table row deleted with Track Changes on is left out, as every other tracked deletion is. It came out as a
  row of the final table, so a struck-out price or clause read as if it still stood.
- Bold or italic that ends on a punctuation mark right before a letter (`**67%**으로`, `**「법」**제15조`) is no longer
  written: Markdown readers showed its asterisks as text. This affects every format that keeps emphasis.
- **Servers**: closing an `IsolatedConverter` while a file was still on its way to its worker could never return, if
  that worker died first. The queue the file travels on is no longer waited for.
- **MCP server**: the path of a call is resolved in a thread, as its content already was. A path the file system is slow
  to answer for — a disconnected network drive, a sleeping disk, a UNC name on Windows — stopped the whole server, and
  with it the agent's session, until it answered.
- **XLSX**: a sheet stored in another case than the workbook names it (`Sheet2.xml` behind `sheet2.xml`) is read; it
  was left out without a word. So is a workbook part in another case, which rejected the file, and a styles part in
  another case, which turned every date into its serial number. A sheet whose part is missing from the file is named in
  a warning instead of vanishing.
- **DOCX / PPTX / XLSX**: a main part stored in another case (`word/Document.xml`) is read; the file was rejected as an
  unsupported archive.
- **XLSX / XLS / Excel 2003 XML**: a negative number under a negative section of text alone (Yes/No, True/False,
  On/Off, `"예";"예";"아니요"`) shows that text, as Excel does, not `-Yes`.
- **HTML**: the line of a table flattened into a cell stays apart from the text beside it (`Total: in1 in2`, not
  `Total:in1 in2`). The lower levels of a list inside a table cell are marked `└ `, as in DOCX and HWP; every level came
  out as one.
- **Mail**: an invitation sent as a text/calendar part alone keeps its description (the agenda), and an event timed with
  `DURATION` gets its end. A mail without MIME-Version whose only part is an invitation, a forwarded mail or an
  attachment is read as mail, not as raw text. A mail with an empty HTML part read under a text name is read as mail.
- **Server use**: `IsolatedConverter.convert` reports running out of memory while copying the content as too large,
  as `convert` does, instead of letting a raw `MemoryError` out.
- **Open WebUI**: a UUID inside the uploaded file's own name is kept; only Open WebUI's leading file id is taken off.
- **PDF reading order**: on the last page of a two-column article whose right column holds only a few closing lines at
  its top, the rest of a ragged-right left column below them is no longer moved after the right column.
- **PDF text**: a justified line whose word gaps are 1.1 em or wider (a narrow column with long words) is read as one
  line; its words came out one by one, shuffled and scattered through the paragraph.
- **PDF reading order**: the rest of a long left column is kept before a short right column when a subheading breaks it;
  the left column from its second paragraph on was moved after the right column. The first line of a two-line paragraph
  over a table in the right column is no longer taken for a caption of a table in the left column and read ahead of it.
- **PDF forms**: the values of a filled form are kept on a page read with simple extraction (past the time budget, a
  crowded or very heavy page, a page pdfminer cannot read); they were dropped without a word. The annotations walked for
  them are bounded over the document, with a warning past it, and the widgets of a large field are found once a document
  instead of once a widget: a field of 20,000 checkboxes took 78 seconds, on any page.
- **MCP server**: one server object serves a second session after the first has ended (every SSE client and every
  in-memory client is a session of its own); the second refused every file as 'This IsolatedConverter is closed.', and
  one SSE client leaving stopped the conversions of another.
- **CLI**: a message or warning on a standard error whose reader has gone (`2>&1 | head`) ends quietly, with the exit
  code the run would have had (1 when its output was going to that pipe too), not with a BrokenPipeError traceback or
  exit code 120.
- **Pictures**: alt text that is a sentence or a longer phrase ending in a file name (`… exported from Visio as
  orgchart.png`) is kept as the author's; only what is shaped like a file name is dropped — `image1.png`, `Screenshot
  2024-01-01 at 10.00.00.png`, a title of up to four words, or up to eight with a number, that ends in one.
- **Descriptions**: no describer call is sent once the answers already in are over `max_chars`; a whole further batch of
  paid calls (one per slot) went out after the limit was known.
- **Mail**: a sent mail whose top level is `multipart/related` without an HTML part (a plain-text body, attachments, a
  forwarded mail) is read as mail, as the same mail under `multipart/mixed` is; it was rejected as a web page without
  HTML. An opaque-signed S/MIME mail whose `smime-type` is on a folded header line is no longer taken for an encrypted
  one.
- **HTML**: whether an inline style hides an element, or folds the answer of a question list, is decided by the
  declaration a browser applies — comments left out, the last one whose value it knows: `/* display:none */` and
  `display:none; display:block` hid visible text, and `display:none; display:bogus` showed hidden text.
- **XLS**: a number under a format XLSX reads as a date or time is read as one too — a time under a format with
  fractional seconds (`ss.00`, `m:ss.000`) was its serial number; superscripts and subscripts after an emoji land on the
  right characters.
- **XLSX / XLS**: an error value is shown without its number format, as Excel does (`#N/A`, was `#N/A 님` under `@" 님"`).
  An elapsed-time format keeps its backslash-escaped literals (`[mm]\m\i\n` → `90min`, was `90:00`).
- **RTF**: a paragraph whose mark was deleted with Track Changes is joined to the next one, as in DOCX; the sentence was
  cut in two and a list item took a number of its own.
- **ODT**: a picture or text box inside a sentence goes after its paragraph, as in DOCX, instead of cutting the sentence
  into two paragraphs around it; in a caption (`Figure 1: …` after its picture) the picture stays in front.
- **Plugins and the MCP package**: the Claude Code and Hermes skills and READMEs no longer say HTML, email and EPUB are
  unsupported on Windows, or that the server converts inside its own process there.

## [0.3.14] - 2026-11-19

Fixes from five audit rounds, most of them small files that cost a lot of time or memory, and content that was lost.
One sample output changed: `score-table.hwpx` no longer writes the guide text of an unfilled click-here field (`날짜`).

### Added

- **CLI**: `opendockit --version`, as the other two commands have, for bug reports.

### Changed

- **CLI**: output to a closed pipe or a closed standard output, `--help` and `--version` included, ends quietly with
  exit code 1; a short output to a closed pipe ended with `Exception ignored … BrokenPipeError` and exit code 120. With
  standard error closed, messages are no longer written into the output.

### Fixed

- **PDF shaded tables**: in a striped table, the cells beside a cell merged over two rows with its text centred on them
  no longer run into one cell (`South Lee`).
- **PDF forms**: a field is named by its own `/TU` or `/T`. The tooltip of a field grouping others named every field
  under it, so their values could not be told apart.
- **HTML**: an element with the role `main` or `article` is the main content, as `<main>` is: the footnotes, topics
  and sidebars of Sphinx and Read the Docs pages (asides since docutils 0.18) are no longer removed as side menus. A
  plain `<button>` question whose folded answer is kept is kept too, instead of leaving the answer without it.
- **Mail**: a sent mail with `multipart/related` at the top (Outlook and Apple Mail, with pictures in the body) is read
  as mail, so its meeting invitation, forwarded mails and the plain body of a picture-only HTML are no longer lost.
- **DOCX / PPTX**: a cell marked as continuing a merge that has no cell to continue (a `w:vMerge` in the first row,
  a PPTX `vMerge`/`hMerge` cell no cell spans) keeps its text.
- **XLSX**: a cell without an `r` address that holds a malformed value, and a comment with an empty `ref`, no longer
  reject the whole workbook. A value under a malformed format index is checked like any other (`1E400` was `inf`).
  Weekday-only formats (`aaa`) show the day, as `dddd` and XLS do, not the date and time.
- **PDF text**: a word in fake bold on part of a line (drawn twice 0.3pt apart, common in PDFs exported from Hangul) is
  read once; it came out again as a paragraph of its own, or split its line in two. Lifting a line pushed down in a
  single column no longer compares every pair of lines on the page: a 480-line page took 7 seconds, outside
  `pdf_time_budget` (an 11 KB file took 22 seconds); it takes 0.2.
- **PDF tables**: a borderless table that pdfminer merged differently from row to row is one table again, not three,
  with every cell in its column (one part had a column too few and its values stood one column to the left). The next
  page of such a table gets the header of its top part, not a data row of its lower part, which came out twice.
- **Character limit**: the quote marks of a quoted line holding only a picture that is erased (too small, a repeated
  logo) no longer count, so a document within `max_chars` is no longer reported `exceeded` and its pictures are decoded.
- **Pictures**: alt text Office or Hangul wrote itself (`그림 3`) is recognised in decomposed (NFD) Hangul too. A picture
  inside an HTML `<pre>` no longer adds a blank line to the code block.
- **Markdown**: the data: URI pictures inside a code block are left alone in every code block cleaning sees — a fence
  indented in a list item, or closed by a longer fence — and inline code over two lines of a CRLF file is code.
- **RTF**: a file behind a UTF-8 byte order mark (PowerShell 5, .NET) is read as RTF, not as its source text. A
  Shift_JIS, GBK or Big5 character whose second byte is written as `\\`, `\{` or `\}` is one character, not `�\`.
- **DOCX / PPTX**: a styles or document-properties relationship pointing back at the body, and a slide layout, master
  or notes relationship pointing at another kind of part, no longer reject the document or drop the slide. A merge
  continuation under the second column of a wider cell repeats its text instead of coming out empty.
- **XLSX / XLS / Excel 2003 XML**: a workbook with very many small merges (490,000: 20 seconds, 558 MB) is streamed. A
  What-If data table read as formulas is `=TABLE(…)`, not an object address that changed on every run. Zero padding
  is kept with a thousands separator (`00,000` → `01,234`). `[hhh]:mm` no longer loses a day for serial 60. Serial 0
  under a date or weekday format is `0` everywhere, and Excel 2003 XML reads weekday-only formats as dates.
- **HTML**: every formula of a saved Wikipedia article is kept, once, without its `{\displaystyle …}` wrapper. A `$`
  or brace in MathML (also in ODF) is escaped. Selects left open no longer take quadratic time (8,000: 20 seconds).
- **Mail**: a forwarded mail of type `message/global` is read. A base64 body cut off at one letter past a whole group
  of four is decoded up to the cut, not shown as base64. A `cid:` address in upper case or percent-encoded finds its
  picture.
- **EPUB**: only one branch of an `<epub:switch>` is read, not the formula and its text fallback.
- **HTML**: what follows a table that sits inside a table left open (a missing `</table>`) stays after it, as in a
  browser; the headings and paragraphs after the second table came out ahead of both. The words of a MathML `<mtext>`
  are `\text{…}` with their spaces (also in ODF): `1 if x>0` read `1ifx>0`.
- **Mail**: a body picture sent as `application/octet-stream` with a Content-ID is extracted; it was neither extracted
  nor reported.
- **PDF tables**: on a two-column page, a column of a borderless table lying in the gap between the page columns is
  written once; it was read into a table on each side.
- **PDF text**: a paragraph drawn through a Form XObject beside a paragraph on the page goes after that paragraph, as
  when both are drawn on the page; it was wedged in after its first line.
- **PPTX**: a numbered paragraph whose start number differs from the run's starts over at it (three items, then two set
  to start at 7, read `4. 5.`).
- **XLS**: a text box with an emoji ends where its text ends; its formatting was read as text after it (`on time(@`).
- **XLSX / XLS / Excel 2003 XML**: a stored `-0` shows as `0.00` or `$0.00` under its format, not `-0.00`. A text cell shows the
  literals of its format's text section (`@" 님"` → `홍길동 님`).
- **Limits**: `max_uncompressed_bytes` of `sys.maxsize` or more no longer rejects every compressed HWP and ODT as
  corrupted, nor drops the compressed pictures of an XLS.
- **Open WebUI loader**: an app from `create_app` mounted in another Starlette or FastAPI app converts in worker
  processes too; Starlette runs no lifespan of a mounted app, and every file was converted in the server process
  without the 2-minute and 2 GB limits.
- **PDF text**: small text with tight character spacing or condensed width keeps both letters of `ll` and its spaces;
  the second letter was taken for a fake-bold copy (`Al staffwil folow`). A line rebuilt around a word in fake bold keeps
  the spaces the PDF draws (`Theplanisimportant`). A drop cap drawn after its lines (as LibreOffice writes it) joins its
  word (`O` / `nce upon a time`), and one word in synthetic italic no longer keeps a letter-spaced line spaced out.
- **PDF tables**: a borderless cell joined from the pieces of one line (cells centred over a wrapped cell, as Hangul
  exports them) is no longer repeated as a paragraph under the table. The header is carried to the next page when that
  page's first row has an empty cell. An open-sided table whose last row merges across every column rule keeps that
  row, and a table shaded with one rectangle for the header row and one for the first column is no longer read as two
  cells.
- **Tables (HWP, HWPX and every format)**: a few cells far apart (one spanning 10,000 rows, another 1,000 columns) no
  longer build a dense grid of empty cells: a 1.9 KB HWPX took 6.5 seconds per table. A table is drawn from the rows and
  columns that hold text, and the empty cells of one conversion share one budget.
- **CSV**: a run of empty lines is skipped in one step and the lines are no longer all held in memory: a header and then
  50 MB of line breaks took 32 seconds and 4 GB.
- **XLSX / XLS / Excel 2003 XML**: `#.##` shows 0.5 as `.5` and `#,###.00` as `.50`, as Excel does. A bare `%` stays where
  the format puts it (`0.0 %` → `12.5 %`, `%0` → `%50`), and each one multiplies by 100 (`0%%` → `1250%%`). A huge negative under `h:mm`, and a serial past 9999-12-31
  under a date format with a quoted `[h]`, no longer reject the workbook.
- **DOCX**: a footnote with a custom mark (Insert Footnote > Custom mark) no longer writes the mark twice
  (`Text[^1]*`). First-page and even-page headers that Word does not show (the option unticked, the part kept) are left
  out. Lower-level numbers in full-width digits (`１.`) get their bullet; they ran into the item above.
- **PPTX**: unwrapping `mc:AlternateContent` takes linear time (40,000 wrapped shapes: 19.6 seconds), and a wrapped text
  box on a notes page is part of the notes.
- **RTF / ODT**: an empty numbered paragraph takes its number, as in Word, LibreOffice and DOCX (`1.` then `3.` after an
  empty item). An RTF list override starts at the number it sets (`\listoverridestartat`).
- **HTML**: a link or emphasis around the nested list of a list item is no longer written across the list. Every
  caption of a table is kept. Many `<xmp>` or `<textarea>` elements no longer take quadratic time (400,000: 10.8
  seconds).
- **Mail**: the HTML of a mail is searched for visible text only when a plain-text branch waits on it; a 37 MB HTML mail
  was parsed whole, 9.9 seconds and 1 GB.
- **EPUB**: many `<epub:switch>` elements no longer take quadratic time.
- **Pictures**: a 1-bit or palette picture (a fax TIFF, a bilevel scan, a GIF) is blended when it is shrunk; Pillow
  shrank it by nearest neighbour, and 2 px rules and thin strokes vanished. Scanned pages that are copies of one
  picture are each counted in the warning (100 such pages were '10 page(s)').
- **Markdown files**: a data: URI picture whose alt text holds brackets (`![Figure [1]](…)`) or a line break, or an
  `<img>` whose quoted alt holds `>`, is extracted; its base64 stayed in the text.
- **MCP server**: a long document without pages, slides or sheets is no longer sent to `document_outline` and
  `sections`, which gave the same first characters again; the note says to raise `max_chars`, and the outline no longer
  says it is returned whole.
- **LlamaIndex reader**: `warnings` and `exceeded` stay out of the text LlamaIndex embeds and sends to the LLM with every
  node, and `source` out of the embedded text; a file with many warnings was refused by `SentenceSplitter`.
- **DOCX**: an emoji or symbol Word 2016 writes as `w16se:symEx` is read; it vanished. A start override of one list
  (Set numbering value) no longer becomes the start of that level in every list sharing its definition. Two
  paragraphs joined with Track Changes on are one paragraph, not a sentence cut in two. A zero-padded custom number
  format (`001, 002, 003, ...`) keeps its width. A paragraph of invisible characters is empty, in PPTX too.
- **DOCX / PPTX packages**: a part named in another case than the relationship that points at it is read, as Office
  does; a DOCX lost it to the repair and a PPTX lost a picture or a slide. One relationship without its Id, Type or
  Target no longer rejects the whole document, and one without a Type to a styles, numbering or other known part
  takes that part's type.
- **HWP / HWPX**: the one-character controls of a paragraph are decoded with the text around them, without the
  control characters they held; the guide text of an unfilled click-here field is left out; an empty numbered
  paragraph takes its number; notes count toward `max_chars` while the body is read, and uncompressed streams toward
  `max_uncompressed_bytes`. Shape positions past the end of the text are dropped before the shapes are ordered, an atop inside an
  atop keeps its braces, and HWPX members are looked up in constant time.
- **RTF**: the older `\pn` numbering starts at its `\pnstart`; a list in a header or footer no longer takes the body's
  first number; the number of a numbered heading is counted; the ANSI font of the macOS text system reads as CP1252;
  the Unicode line separator is a line break.
- **ODP**: the paragraphs of a text box are numbered as Impress draws them: lists that follow each other count on
  (`1. a) b) 2. 3.`, not `1. a) b) 1. 2.`), also across a plain paragraph, and a list of another number format or
  start restarts.
- **XLSX / XML Spreadsheet**: a negative section that differs from the positive one only by a space keeps its minus;
  an escaped quote before a quoted literal is a literal in date checks; the cell comments of an Excel 2003 XML
  workbook come after the sheet.
- **HTML**: closed dialogs and popovers, the permalink marks of documentation headings and the fallback text of
  `<video>` / `<audio>` are left out; `white-space: pre-wrap` keeps its line breaks; a line-numbered code block is the
  code alone; the TeX of a MathJax script is the formula; `<q>` gets its quote marks; a table drawn inside a list item
  keeps its lines; the buttons of a heading are unwrapped in linear time, and a list item with very many code blocks
  is split in linear time (40,000 `<pre>` blocks: 21 seconds).
- **MathML**: a script base that is itself a script or a group is braced (`{x^2}^3`), also in ODF, and the pairs of
  an `<mmultiscripts>` are written without double scripts (`R_{i}^{j}{}_{k}`, `{}_{6}^{14}C`).
- **Mail**: the parts of a forwarded mail are read like those of the outer mail; delivery and read reports, and the
  returned headers of a bounce, are read; named inline text files (a patch, a CSV) are listed in the attachment warning;
  a named meeting invitation beside the text is kept, and written once when it is sent in two parts; a header with unclosed encoded words is decoded in linear time,
  and a picture shared by several references is decoded once.
- **Outlook `.msg`**: rejected with a message that says how to save the mail instead of as an unknown format.
- **EPUB**: a member whose name is in another Unicode normalization than its reference is found.
- **PDF**: a footer of a label and `Page 3 of 4` side by side, and roman page numbers (`i`, `ii`, `iii`) that count
  with the pages, are removed from every page. Prose in three or more columns that pdfminer reads column by column is
  prose, not tables, and a data row with a wrapped cell is no longer joined into the header.
- **Detection**: a DOCTYPE with an internal subset is one DOCTYPE. A note that lists a whole PDF, named as text, is
  read as that text instead of the PDF it lists.
- **Markdown**: the Unicode line and paragraph separators are line breaks outside code blocks (kept in code and data
  files such as JSON); title rows of a long table are lifted in linear time.
- **Pictures**: a Lab picture is prepared as opaque, and the split pages of a TIFF count toward
  `max_uncompressed_bytes`.
- **MCP server**: an answer lists at most 20 warnings, and the outline lists pages, slides or sheets as far as the
  server's `--max-chars` and counts the rest; with `--root`, a folder that may be passed through but not listed no
  longer refuses the files under it.
- **LangChain / LlamaIndex / Open WebUI**: a file of pictures that were not described gives no Document of `[Image]`
  placeholders, as a scanned PDF gives none of page markers; nor does a sheet of pictures with their bare cells, or a
  bare `[Chart]`, `[Video]` or `[OLE object]`.
- **PDF limits**: a Form XObject that draws itself more than once no longer makes pdfium expand it (an 800-byte file
  grew past 10 GB); the page is left out with the drawing-elements warning. A content stream listed many times in
  `/Contents` is charged to `max_uncompressed_bytes` every time, a page whose content would take minutes to interpret
  (a form of a million `q Q` drawn 16 times: 88 seconds) and a page of very many characters (800,000: 25 seconds and
  2 GB) are read with pdfium's plain text extraction, with the same warning. A page is opened in pdfium at the number
  pdfium gives it, so an odd page tree (a missing or non-page entry in `/Kids`) cannot hand it a page that was
  refused. The data of an inline image is read in linear time (5 MB: 8.5 seconds). A page that adds more than 150,000
  pieces to paths (a detailed map) is read with pdfium too: pdfminer kept about 1.4 KB a segment (600,000: 942 MB).
- **PDF tables**: a table shaded with grey set as DeviceGray (`0.85 g`) is read as shaded, as one in RGB is; each row
  came out as a table of its own.
- **Mail**: a Content-Type with many parameters no longer takes quadratic time (an 8 KB mail: 20 seconds), nor does a
  header of many encoded words on one line (1 MB: 13 seconds). An attachment name written in raw 8-bit bytes or in an
  RFC 2231 Korean charset (`ks_c_5601-1987''…`) is read in the warning, not as `���`, and such a name on a part without
  a usable boundary or on a report part no longer rejects the whole mail; nor, on Python 3.12 and 3.13, does a name the
  standard library cannot read (`filename*=` beside `filename*1=`).
- **Character limit (spreadsheets and CSV)**: a sheet is drawn only up to `max_chars`, so a merged cell repeated down
  20,000 rows (108 KB: 31 seconds and 3.5 GB), a CSV of short rows under a wide header (1 MB: 19 seconds and 1.4 GB) and
  sparse rows no longer build the whole table first; a cell holding only `>` counts.
- **XLSX / XLS / Excel 2003 XML**: a number format of unclosed brackets no longer costs milliseconds per cell (10,000
  cells: 93 seconds); one long shared formula is moved to a cell only when its row is read (8,000 cells: up to 74
  seconds); the column-limit warning is looked up once per sheet.
- **HWP / XLS / DOC**: an OLE stream is read no further than the file, so a chain that loops with a huge declared size
  no longer builds gigabytes (a 26 KB HWP: 5.4 GB); a compressed HWP stream counts its stored bytes too, a picture
  stream named by many items is read once, and a file of very many streams opens in linear time (16,000 sections:
  27 seconds).
- **HWP / HWPX**: control characters are kept out of an equation script (`\x05a+b\x06` read as struck text), and
  long `atop` chains with limits or `over` no longer take quadratic time (20 equations: 9 seconds).
- **RTF**: shapes nested hundreds deep inside a table cell no longer redraw the cell at every level (35 seconds for
  946 KB).
- **HTML**: sub- and superscripts, formulas and form values inside a ruby annotation are read (`水(H_2O)`). Rows that
  open inside an element left in an open row (a `<form>` between rows) are read; they vanished with no warning. A
  `<q>` inside a superscript, subscript or ruby gets its quote marks too.

## [0.3.13] - 2026-11-17

Titles for the two MCP tools, and the Claude Code plugin. The sample outputs in `examples/` are unchanged.

### Added

- **Claude Code plugin** (`claude-plugin/`, listed by `.claude-plugin/marketplace.json`): the MCP server and a skill
  for reading documents the Read tool cannot open, installed with
  `/plugin install opendockit --marketplace hyeonsang010716/OpenDocKit`. It starts the release the Hermes plugin pins.
  It is not part of the Python package.

### Changed

- **MCP server**: both tools have a title ("Convert document to Markdown", "Outline a document"), on the tool and in
  its annotations, which clients show where tools are listed.

## [0.3.12] - 2026-11-17

The Open WebUI document loader, and PDF footnotes under a rule typed as a line of dashes. The sample outputs in
`examples/` are unchanged.

### Added

- **Open WebUI document loader** (`opendockit-open-webui`, extra `open-webui`): a small HTTP server for Open WebUI's
  External content extraction engine. Open WebUI sends it every uploaded file and indexes one document per page, slide
  or sheet; a PDF page carries `page`, so a citation names and opens the right page. Open WebUI reads no HWP or HWPX by
  itself. Each file is converted offline in a worker process with memory and time limits; the server needs an API key
  of at least 16 characters, listens on `127.0.0.1` by default, refuses files past its queue at once, refuses rather
  than cuts a document past its limits (50 MB, 10,000,000 characters; both can be set), and answers every failure with
  a fixed sentence.

### Changed

- **PDF footnotes**: a line of em dashes above the notes (`——————`) is a footnote rule like a drawn one. The slip
  opinions of the US Supreme Court type their rule this way, so none of their notes was linked; in four opinions 345
  marks are now `[^n]` with their notes. Every other condition for linking a page is the same.

### Fixed

- **DOCX**: the text cut at `max_chars` is again the front of the full conversion for tables that hold imported content
  (`w:altChunk`) or SmartArt. Since 0.3.8 they were left out of a cell once the text before them passed the limit, and
  a left-out part that held a table, or one inside a table nested in the cell, made the table Markdown instead of HTML;
  inside a table they now stop only at the ceiling 100,000 characters past the limit, as in nested tables. Since 0.3.10
  the table of an imported part was cut at the document's limit, which emptied the cell's nested table the same way.

## [0.3.11] - 2026-11-16

The Dify plugin, and text without decoding pictures. The sample outputs in `examples/` are unchanged.

### Added

- **`decode_images=False`**: text without decoding pictures. No picture is decoded or returned, and each one keeps the
  placeholder a conversion without description gives it, so the alt text an author gave a picture stays in the text
  (`[Image: Quarterly revenue chart]`), which `extract_images=False` drops. PDF pictures and pages and image files
  are read as with `extract_images=False`.
- **Dify plugin** (`dify-plugin/`): one tool, Convert to Markdown, that turns a file uploaded to a Dify Workflow or
  Chatflow into its Markdown, one document per page, slide or sheet, and the warnings. Dify reads no HWP or HWPX by
  itself. Each file is converted in a worker process with OpenDocKit's memory and time limits (in the plugin process,
  without them, where the runtime cannot run worker processes at all). No picture is decoded, and each keeps its alt
  text (`decode_images=False`). It is not part of the Python package.

## [0.3.10] - 2026-11-16

A seventh review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **Tables (DOCX, PPTX, HWP, HWPX, ODT, RTF, HTML)**: the copies of a vertically merged value on the rows it covers are
  no longer drawn past the character limit. The cell text was counted once, so a 38 KB DOCX with one 100,000-character
  cell merged over 5,000 rows was drawn to 500 million characters (65 seconds, 8.7 GB) before the limit saw it (now
  0.3 seconds). The text cut at the limit and `exceeded` are the same as before. Tables in footnotes and headers stop
  the same way.
- **Tables**: the cells that spans may fill are bounded for the whole conversion (1,000,000), not only per table: a
  5.6 KB HWPX of fifty empty tables with 100×100 spans took 13 seconds and wrote nothing (now 0.6 seconds).
- **Pictures**: the prepared (re-encoded) pictures of one document take at most `max_uncompressed_bytes` together. A
  4.6 KB PNG of 1-bit noise re-encodes to a 3 MB JPEG, so a 376 KB DOCX held 920 MB of pictures, and an
  `IsolatedConverter` passed them on to the server process (2.3 GB). Pictures past the bound keep their placeholder,
  with a warning; scanned pages are prepared first.
- **DOCX**: only levels 0 to 8 of a number definition are kept, as Word uses (a 312 KB file with 100,000 levels and 5,000
  lists took 44 seconds; now 0.4 seconds, same output).
- **PPTX**: the placeholders of a layout are looked up once per layout instead of once per slide (400 slides on a layout
  of 4,000 placeholders: 49 seconds → 0.5 seconds).
- **Charts**: a value table stops at 1,000,000 empty cells, with a warning. Series that each use their own point
  indexes made a table of series × points cells, nearly all empty (a 186 KB PPTX: 20 seconds and 1.5 GB).

### Fixed

- **PDF**: a table or picture right under a short label whose descent touches it, or under a centred title, stays where
  it is on a one-column page instead of moving to the end of the page or before the list above it. A picture `/Width`
  that is not a number, a CropBox outside the MediaBox (with `extract_images`), and a borderless-table header with two
  pieces in one column no longer reject the whole document as corrupted.
- **Mail**: a mail whose only part is a meeting invitation (`text/calendar`) or an attachment (a scanner's
  `application/pdf`) is read as mail (headers, the event, the attachment warning) instead of raw headers and base64. A
  `format=flowed` plain part next to an HTML part has its lines joined, as without the HTML part.
- **Text, HTML, CSV**: UTF-16 without a BOM of Cyrillic, Arabic, Devanagari or Thai text is read as UTF-16; it came out
  as ASCII garbage (`@825B, :0: 45;0?`), or as an unsupported binary file when it was one paragraph.
- **HWP / HWPX**: with `include_headers_footers`, page footers come after the body, as in DOCX, ODT and RTF (they came
  before it, right after the headers).
- **XLSX / XLS / Excel 2003 XML**: square brackets inside quoted or escaped literals of a number format stay literals:
  3 under `"Lv.["0"]"` is `Lv.[3]` (was `Lv.`), 12 under `#,##0" [kg]"` is `12 [kg]` (was `12`), and a quoted `" [>="`
  or `"[h]"` no longer makes the format conditional or a duration. A date-only ISO cell (`t="d"`) follows its display
  format (`Jan-24` under `mmm-yy`, was `2024-01-05`).

## [0.3.9] - 2026-11-16

A sixth review of the whole source. The sample outputs in `examples/` are unchanged.

### Security

- **XLSX**: a styles part with more than 131,072 entries or 1,000,000 elements is replaced before loading by one that
  keeps only the display formats of the first 65,536 cell formats, with a warning (a styles part written in UTF-16 is
  counted too). A 283 KB file with one cell and 1,000,000 cell formats took 7.8 seconds and 2.3 GB (now 0.7 seconds and
  350 MB); 1,000,000 differential formats took 21 seconds.
- **PDF**: a line reaching out of the cells of a ruled table no longer scans every character of the page, and the cell
  text of a ruled table is read row by row from the characters at its height, with the same output (a 600-row table
  with a mark beside each row: 54 seconds → 4 seconds). The reading-order step that sets lines beside two columns below
  them carries what lies above instead of rescanning it, and the one that reads alternating sides in turn looks at no
  more than 1,000,000 lines per page (20,000 alternating lines: 21 seconds → 4.4 seconds). Label-value pairs beside
  another block are checked against the rows around them only (8,000 rows: 12 seconds → 0.05 seconds).

### Fixed

- **PDF**: a picture drawn at a position that is not a finite number (a scale of 10^200, written out in digits, twice)
  no longer rejects the whole document as corrupted; neither does a page that `/Count` promises but pdfium cannot open
  when pdfminer cannot read the document (64 such pages in a row end it). A vertical merge is repeated only into the
  rows it covers, so a table whose columns merge staggered pairs of rows no longer copies one merge's text into
  another's row. An open-sided table whose merged cells break every inner rule is read once instead of twice. Two rows
  of values between two rules are no longer joined into one row as a two-line header.
- **HTML**: cells that lxml leaves outside a row are read, as a browser reads them: cells wrapped by a `<form>`, `<div>`
  or `<label>` inside a row, and cells written straight under the table or after a missing `<tr>` (they used to vanish,
  often leaving "No text was extracted"). A struck italic link followed by more struck text no longer gives `~~~~`. A
  heading holding only a picture is the picture, not an empty `#`. A `data-src` written as `DATA:` no longer turns the
  real picture into a lazy-load placeholder.
- **Mail**: a decoded word next to plain text gets no space added, so `"=?euc-kr?B?…?=" <a@b>` is `"홍길동" <a@b>`
  instead of `" 홍길동 " <a@b>`, and an attachment named `=?utf-8?b?…?=.xlsx` keeps its name whole. The space of a
  folded header is kept.
- **Pictures in quotes**: a picture in a quoted table row stays in its cell and a picture in a quoted paragraph stays in
  the quote (a reply quoting a table with a photo used to split the table in two and set the photo outside the quote).
- **DOCX / RTF**: a heading paragraph holding only a picture is the picture, not an empty `#` (or `# 1.`).
- **Markdown**: a `data:` picture written in capitals (`DATA:IMAGE/PNG`) is extracted instead of leaving its base64 in
  the text.
- **XLSX / XLS / Excel 2003 XML**: number formats round the value to 15 significant digits first, as Excel does, so
  `0.7*1.5` under `0.0` is `1.1` (was `1.0`). An XLS zero duration under `[h]:mm` is `0:00` instead of `0`, and an
  XLSX cell under `[hhh]:mm` keeps its days (`36:00`, was `12:00:00`).

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

[Unreleased]: https://github.com/hyeonsang010716/OpenDocKit/compare/v0.3.14...HEAD
[0.3.14]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.14
[0.3.13]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.13
[0.3.12]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.12
[0.3.11]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.11
[0.3.10]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.10
[0.3.9]: https://github.com/hyeonsang010716/OpenDocKit/releases/tag/v0.3.9
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
