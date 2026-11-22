# Scorecard: how good is the structure of the output?

The unit tests say that a rule still does what it was written to do. They cannot say whether the rule makes real
documents come out better, because each test document was built for its rule. This folder measures that instead: it
converts documents whose correct Markdown is known, scores the result, and compares the scores with a baseline, so a
change to a rule that guesses structure (what is a heading, what is a table, which text comes first) is judged by all
the documents and not by the one it was written for.

```bash
sh evals/fetch_corpus.sh ../corpus          # once; needs Git LFS, about 90 MB
C=../corpus/opendataloader-bench
uv run python -m evals.run --documents $C/pdfs --gold $C/ground-truth/markdown --baseline evals/baselines/pdf-structure.json
```

The run takes about 20 seconds, prints the mean of every metric beside the baseline, lists each document that moved
by more than 0.02, and ends with status 1 when a mean fell by more than 0.002. `--out <dir>` keeps the converted
Markdown to read; `--update-baseline` writes the run as the new baseline. The `Scorecard` workflow runs the same
command on pull requests that touch the PDF reader.

## Rule

A change to how structure is guessed from a PDF comes with the scorecard's comparison in its description. It is
merged when no mean fell, and its baseline is updated in the same change. A document that got worse is a reason to
look, not a veto: a rule that fixes forty documents and breaks two is an improvement, and the two go into the
description as what is left. A counter-example built by hand shows that a rule can be wrong, not how often it is.

## What is measured

The corpus is [opendataloader-bench](https://github.com/opendataloader-project/opendataloader-bench) (Apache-2.0) at
a fixed revision: 200 PDFs, mostly English — papers, reports, books, forms, slides — each with a reference Markdown
checked by hand. Only its documents and references are used; `metrics.py` computes its three metrics from their
definitions, and reproduces the per-document scores that benchmark publishes for Docling, MarkItDown and OpenDataLoader
to the last digit (checked for all 200 documents of each, November 2026), so the scores below can be set beside
the published ones.

| Metric | Question | Measured on |
| :--- | :--- | :--- |
| NID | Is the text there, in the right order? | 200 documents |
| TEDS | Are the tables the same tables, cell for cell? | the 42 documents with a table |
| MHS | Are the headings headings, with the right text under each? | the 107 documents with a heading |

`overall` is the mean of the three for each document, averaged over the documents.

A fourth number, **Words**, is OpenDocKit's own and stays out of `overall`: how far the two texts are made of the same
words, whatever their order (`2 * shared / (words of one + words of the other)`). The marks of emphasis are no part of
a word: `**장관**은` is the word `장관은`, since one reader marks bold and another does not. NID compares characters and hardly
sees a space, so a word a line break cut in two costs it one character in thousands; here it costs two wrong words and
a missing one. On the benchmark it is 0.954 for OpenDocKit, 0.937 for OpenDataLoader, 0.932 for Docling and 0.915 for
PyMuPDF4LLM, scored from the Markdown that benchmark publishes for each. Conversion uses the default
options without page markers and picture placeholders (`evals/run.py::OPTIONS`), which are OpenDocKit's own marks and
not text of the document.

## Reading a comparison

A run against a baseline prints the mean of each metric before and now, the documents one of whose scores moved by more
than 0.02, and the documents whose text changed at all:

```
documents that moved by more than 0.02: 1 (0 with a metric that fell)
  01030000000058  mhs +0.955
documents whose text changed: 3 of 200 (2 without a score that moved)
  01030000000017
  01030000000102
```

The last lines are there because a mean can stand still while the documents under it change: a baseline keeps a digest
of each converted text, and a document that came out differently is named even when no score noticed. Look at those
before believing a change touched only what it was meant to (`--out <dir>` on both sides, then `diff`). A change that
is meant to leave the output alone should say "0 of 200".

## Where OpenDocKit stands (2026-11-21)

| Converter | Overall | NID | TEDS | MHS | Needs a model |
| :--- | ---: | ---: | ---: | ---: | :---: |
| OpenDataLoader hybrid | 0.907 | 0.934 | 0.928 | 0.821 | yes |
| Docling | 0.882 | 0.898 | 0.887 | 0.824 | yes |
| **OpenDocKit (baseline)** | **0.877** | **0.914** | **0.776** | **0.805** | no |
| Marker | 0.861 | 0.890 | 0.808 | 0.796 | yes |
| OpenDataLoader | 0.831 | 0.902 | 0.489 | 0.739 | no |
| *OpenDocKit without headings by weight (`60a5de8`)* | *0.772* | *0.914* | *0.710* | *0.407* | no |
| PyMuPDF4LLM | 0.732 | 0.885 | 0.401 | 0.412 | no |
| MarkItDown | 0.589 | 0.844 | 0.273 | 0.000 | no |

The other rows are that benchmark's published figures. What the first runs showed:

- **Reading order is already among the best**, ahead of Docling.
- **Tables** are well ahead of OpenDataLoader's rules and PyMuPDF4LLM, and 0.11 behind Docling. The first run had
  0.710, with 8 of the 42 documents scoring zero because no table was written at all; tables drawn with horizontal
  rules only (`_pdf_banded_tables.py`) took three of those to 0.79–1.0. The loss is still in finding tables, not in
  reading the ones found.
- **Headings by weight are worth 0.38 of MHS.** They had been taken out in `60a5de8` because of forms, receipts and
  notices built by hand to break them; the first run of this scorecard measured the revision before and after
  (85 % against 43 % of the reference headings found; 74 % against 63 % of what was written as a heading being one),
  and they were put back. This corpus has few forms and few Korean documents, so the headings wrongly made there are
  still unmeasured — the reason a Korean corpus comes next.
- **What is left in headings is precision**: about one heading in four written is not one in the reference.

## Korean pairs: one document in two formats

```bash
uv run python evals/fetch_korean_pairs.py ../corpus/korean     # 60 press releases from korea.kr, about 60 MB
K=../corpus/korean
uv run python -m evals.run --documents $K/pdf --gold $K/hwpx --baseline evals/baselines/korean-pairs.json --gate nid,nid_s,teds,teds_s,words
```

Sixty press releases of Korean ministries that the policy briefing site publishes as HWPX and as PDF
(`korean_pairs.tsv` lists them; the files themselves are not kept here). The HWPX states its tables and its order of
text, so its conversion is the reference for the PDF's: nobody labels anything, and the corpus can grow by a command.
The scores say how far the two readers agree, so read them as a watch for change, not as a grade:

- **Words 0.977**, from 0.961: Hangul breaks a line of Korean after any syllable, and every body paragraph of the PDFs
  came out with a space inside several of its words ('산림 청과' for '산림청과') — 375 words in the 60 releases, which
  NID did not see (0.9115 with them, 0.9125 without). The HWPX of each release says where a space belongs, and against
  it the rule that puts such a word together again (`docs/decisions/pdf-flow.md`) is right 287 times and wrong 22:
  where a line breaks between two runs of different character properties, Hangul's PDF has no space to show.
- **NID 0.927**: the same text in the same order. This is what caught the first form of the tables drawn with
  horizontal rules, which unfolded the top of 17 releases and lost their titles while the English benchmark stood still.
- **TEDS 0.559**, from 0.475, by a change to the reference and not to the PDF reader. The low number was a finding
  about the HWPX reader: 126 of its 219 tables in these releases were of one row — the bar an attachment opens with,
  the time of release beside the date — and none held data. They are lines of text now, the bars headings
  (`docs/decisions/hwp.md`). What still separates the two readers is in the HWPX and not on the page: cells divided
  by borders that are not drawn (the contact table at the end of every release is six columns in the HWPX and four in
  the PDF).
- **MHS 0.759 over 57 releases is not gated**: it says how far two readers agree, and a heading only one of them writes
  is not known to be wrong. The first run had 5 releases to measure it on: the HWPX reader wrote 11 headings in the 60
  releases and the PDF reader 140, because the title of a release stands in a one-column table in the HWPX, where a
  paragraph could not become a heading by its size. It can now (`docs/decisions/hwp.md`), the HWPX reader writes 58,
  and with the bars of the attachments 120. The PDF reader wrote 15 of those 52 bars as headings; it reads 55 lines as
  bars now (`docs/decisions/pdf.md`), which took this number from 0.640 to 0.750. What is left to read in it is
  headings one reader makes and the other does not.

Compared with `main` before the headings by weight were put back, the PDF reader writes 45 more headings in these
releases and loses none: the section titles of two long releases, the labels of summary boxes, and — wrongly — a line
of place and the quoted slogans of a list of companies.

## Korean holdout: releases no rule was written on

```bash
uv run python evals/fetch_korean_pairs.py ../corpus/korean-holdout --list korean_pairs_holdout.tsv
H=../corpus/korean-holdout
uv run python -m evals.run --documents $H/pdf --gold $H/hwpx --baseline evals/baselines/korean-holdout.json --gate nid,nid_s,teds,teds_s,words
```

The sixty releases above come from two days of November 2026, and the rules for Korean documents were written looking
at them: the words put together again, the tables of one row, the bars of attachments. Sixty more
(`korean_pairs_holdout.tsv`), one every few thousand releases from September 2026 back to November 2023, say whether
those rules hold on releases nobody looked at. Scored with the readers as they were before those rules (`48dd49c`) and
as they are:

| Holdout, 60 releases | Before | Now |
| :--- | ---: | ---: |
| Overall | 0.713 | 0.781 |
| NID | 0.918 | 0.935 |
| TEDS | 0.477 | 0.580 |
| MHS | 0.708 | 0.807 |
| Words | 0.963 | 0.978 |
| Line breaks taken out of a word, right / wrong by the HWPX | | 355 / 32 |

The gains are those of the first sixty (overall 0.666 → 0.747 there), and the rule that joins a word is right as often
(92% here, 93% there). The holdout also found what the first sixty did not have: a bar of three cells ('붙임2', a
figure, the title), which the HWPX reader wrote as a line and the PDF reader as a heading.

**Keep it a holdout.** Run it to judge a change; do not open its documents to write a rule. A rule comes from the
first sixty, or from a document of your own, and this list says whether it carries. When it has been looked at too
often to say so, find a new one (`--find 60 --from <id> --stride 3000`) and say so here.

## Format twins: one document in six formats

```bash
uv run python evals/make_twins.py $C/ground-truth/markdown ../corpus/twins     # needs pandoc and LibreOffice; about 15 minutes
T=../corpus/twins
for f in docx odt rtf html epub pdf pdf-untagged; do
  uv run python -m evals.run --plain --documents $T/$f --gold $C/ground-truth/markdown --baseline evals/baselines/twins-$f.json
done
```

The 200 hand-checked references of the benchmark, written by pandoc as DOCX files that say their structure outright
(heading styles, tables, lists) and saved by LibreOffice as ODT, RTF and PDF; pandoc also writes each as an HTML page
and as an EPUB. The files hold one document whose right answer is known, so each reader is scored against the same reference, and a difference between two formats is
a difference between two readers. `--plain` converts without emphasis and links, which the references do not write.

| Twin | Overall | NID | TEDS | MHS |
| :--- | ---: | ---: | ---: | ---: |
| DOCX | 0.992 | 0.991 | 0.967 | 0.995 |
| ODT | 0.992 | 0.991 | 0.967 | 0.997 |
| RTF | 0.992 | 0.991 | 0.967 | 0.995 |
| HTML | 0.990 | 0.989 | 0.967 | 0.997 |
| EPUB | 0.992 | 0.991 | 0.967 | 0.997 |
| PDF, as LibreOffice saves it (tagged) | 0.969 | 0.978 | 0.846 | 0.973 |
| PDF, the same pages without tags | 0.931 | 0.965 | 0.487 | 0.969 |

- **The readers of the formats that state their structure agree with the reference and with each other.** No document
  is more than 0.15 lower in one of them than in another; 39 of the 42 tables are read exactly. What is left is
  convention (a table of one column is written as paragraphs).
- **HTML and EPUB read as well as DOCX** on the HTML pandoc writes, which is clean: no navigation, no scripts, no
  layout tables. It says the readers lose nothing of a plain page, not how they do on the pages of the web. Five
  references have no heading, and the title of the page — the name of the file — becomes one there, as the HTML
  reader means it to (`docs/decisions/extractors.md`).
- **The same documents as PDF lose their tables.** pandoc's tables have no rules, and LibreOffice sets them as
  aligned text with nothing between two rows but the next line. The first run read 3 of 42 to 0.9 or more and 9 not at
  all (TEDS 0.330); on the benchmark's own PDFs, most of which rule their tables, TEDS is 0.776. Tables without any
  rule are where the PDF reader is weakest, and these 42, with their exact answers, are the place to work on it.
  Reading the lines of a wrapped cell as one cell (`_pdf_gutter_tables.py`) took it to 0.489: 12 of 42 at 0.9 or
  more, 8 not read at all. What is left: tables of two columns, which no search takes without a rule or a caption (6
  of the 8), and wide tables still read in pieces.
- **A tagged PDF says where its tables are, and that is read now** (`_pdf_tagged_tables.py`, `docs/decisions/pdf-tables.md`).
  LibreOffice and Word write the structure tree of a tagged PDF by default: all 200 twins have one, and none of the 320
  PDFs of the benchmark and the Korean lists do. With the tables read from the tree where the page agrees with it,
  TEDS on the twins is 0.846 (from 0.489). The same pages saved without tags (`pdf-untagged`) keep the old number and
  stay the measure of what is read from the page alone: work on tables without rules is judged there. Outside the
  twins, four tax publications of the IRS (tagged by another program, 61 to 15 tagged tables each) kept every word
  they had and lost none, with their tables of contents and filing tables read as the tables they are tagged as.
- Headings are easy here (0.966): a heading style is set large. The benchmark's own PDFs, where a heading is often
  bold at the body size, are the harder measure of headings.

A first form of this corpus took 116 public DOCX test files and pandoc's reading of them as the reference. It measured
conventions and test fixtures more than readers — tables with one filled column, placeholder text of empty form
fields, files of thirty characters — and was dropped for the references a person had checked.

The twins depend on the versions of pandoc and LibreOffice that made them (3.9 and 26.2 for the baselines), so they
run where they were made, not in CI.

## Script twins: one text in nineteen languages

```bash
uv run python evals/fetch_script_texts.py ../corpus/scripts/gold       # 19 texts, about 180 KB
uv run python evals/make_twins.py ../corpus/scripts/gold ../corpus/scripts/twins
P=../corpus/scripts
for f in docx odt rtf html epub pdf pdf-untagged; do
  uv run python -m evals.run --plain --documents $P/twins/$f --gold $P/gold --baseline evals/baselines/scripts-$f.json
done
```

The benchmark is English and the pairs are Korean. A reader can be right on both and lose every other script, and the
PDF reader did: a letter was a Latin letter, a Hangul syllable or a Han character, so a Russian document had no
heading; a line of Arabic or Hebrew came out backwards; the vowel signs of Hindi and the tone marks of Thai were lost
or ended their line. These twins are the measure that was missing.

The text is the Universal Declaration of Human Rights — its title, its preamble and its first twelve articles, with
the headings and the numbered lists it has, and a table of its first six articles at the end — in Arabic, Hebrew, Persian, Urdu, Hindi, Bengali, Tamil, Thai, Khmer,
Burmese, Amharic, Georgian, Armenian, Russian, Greek, Japanese, Chinese, Korean and Vietnamese, from the XML of the
"UDHR in Unicode" project at a fixed revision (`fetch_script_texts.py`). `make_twins.py` writes each as a DOCX and
LibreOffice saves it in the other formats; a text in Hebrew or Arabic letters is laid out from right to left.

| Twin | Overall | NID | TEDS | MHS |
| :--- | ---: | ---: | ---: | ---: |
| DOCX, ODT, RTF, HTML, EPUB | 1.000 | 1.000 | 1.000 | 1.000 |
| PDF, as LibreOffice saves it (tagged) | 0.977 | 0.992 | 0.948 | 0.990 |
| PDF, the same pages without tags | 0.912 | 0.983 | 0.767 | 0.987 |

Without the table at the end of each text — the first six articles by number, heading and first words, which the
texts were given so that tables are scored too — the PDF reader scored 0.993 (NID 0.995, MHS 0.991), from 0.882 before
the text a PDF says its glyphs stand for was read and 0.740 before lines were read from right to left.

- **The readers of the formats that state their text read every script**, to the last character: all five score 1.0000
  on every measure. On their first run they scored 0.996 on Persian and Bengali, whose zero-width joiners were taken
  out with the characters that cannot be seen; those are part of a spelling and are kept now
  (`docs/decisions/core.md`).
- **The PDF reader reads the text and the headings of every script nearly as well** (NID 0.985, MHS 0.980), from 0.791
  and 0.690 three changes earlier: the four texts written from right to left scored 0.42 to 0.44 on NID read backwards
  and score 0.997 to 1.000 now, and Hindi went from 0.745 to 1.000. Lowest is Burmese (NID 0.938 on its paragraphs),
  and there the twin is at fault, not its reader: LibreOffice draws the vowel sign U+102C in a second font and names
  it a space in the PDF it writes, so no reader finds it (`pdftotext` loses the same letters). The twins found one
  fault of the reader on their first run: the title of the Japanese text, set in corner brackets (『世界人権宣言』), was taken
  for a quotation and was no heading (`docs/decisions/pdf.md`).
- **Tables are where the PDF reader loses most, as on the format twins.** A table of a text in Hebrew or Arabic
  letters is laid out from right to left, its first column on the right, and is read so (TEDS 0.52 read from the left;
  Arabic and Hebrew 1.00 now, Persian and Urdu 0.84). Without tags the first run found the table of eleven of the
  nineteen texts, for reasons that had nothing to do with tables and everything with scripts: the number that opens a
  row is set in a Latin font and stands at another height than the cells in the font of the script, and only Hangul
  and Han were looked at for that; a line of Japanese or Thai has no spaces, so no cell was ever seen to end where its
  text did; and a header such as '제 1 조' is words of one letter. With those put right TEDS without tags went from
  0.490 to 0.767: Hindi, Japanese and Khmer are read (1.00, 0.96, 0.97); Amharic, Bengali, Tamil and Thai are found
  with their wrapped cells still rows (0.53 to 0.60); Burmese is not found. With tags (0.948) what is left is tables
  that go on over a page: the Georgian, Persian and Urdu ones are written as a table on each page, as the reader
  writes every table (`docs/decisions/pdf-tables.md`).
- **Words is no measure for a script without spaces.** In Japanese, Chinese, Thai, Khmer and Burmese a "word" is what
  stands between two spaces, a whole line or sentence, and a line break in a PDF paragraph ends one. NID is the measure
  there.

They depend on the versions of pandoc and LibreOffice and on the fonts of the machine that made them, as the format
twins do, so they run where they were made.

## What it does not measure yet

- XLSX, PPTX and mail: the twins cover the text formats only. HTML as it is found on the web, with navigation and
  layout tables around the text.
- Korean headings against a reference a person checked: the pairs above have none.
- The level of a heading: MHS treats every level alike.
- Text that is right but should not be there (running headers, watermarks) only as far as it lowers NID.
