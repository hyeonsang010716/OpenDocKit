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

The other rows are that benchmark's published figures. Current versions of six converters, kordoc among them, are run
on the same PDFs and scored by this scorer in [`benchmarks/README.md`](../benchmarks/README.md#3-a-pdf-benchmark-we-did-not-write).
What the first runs showed:

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
The site does not keep every release: fetched again in November 2026, two of the sixty (156790053, 156790103) and one
release of the development set (156789960) were no longer served, and the fetch names each one it could not get. A
document of a baseline that is not in a run is named above the comparison, and the means compared are then those of
the documents in both, on both sides: the mean of 58 releases set against that of 60 would move with the reader
unchanged, and would hide a fall of the same size.
The scores say how far the two readers agree, so read them as a watch for change, not as a grade:

- **Words 0.978**, from 0.961: Hangul breaks a line of Korean after any syllable, and every body paragraph of the PDFs
  came out with a space inside several of its words ('산림 청과' for '산림청과') — 375 words in the 60 releases, which
  NID did not see (0.9115 with them, 0.9125 without). The HWPX of each release says where a space belongs, and against
  it the rule that puts such a word together again (`docs/decisions/pdf-flow.md`) is right 287 times and wrong 22:
  where a line breaks between two runs of different character properties, Hangul's PDF has no space to show.
- **NID 0.930**: the same text in the same order. This is what caught the first form of the tables drawn with
  horizontal rules, which unfolded the top of 17 releases and lost their titles while the English benchmark stood still.
- **TEDS 0.559**, from 0.475, by a change to the reference and not to the PDF reader. The low number was a finding
  about the HWPX reader: 126 of its 219 tables in these releases were of one row — the bar an attachment opens with,
  the time of release beside the date — and none held data. They are lines of text now, the bars headings
  (`docs/decisions/hwp.md`). What still separates the two readers is in the HWPX and not on the page: cells divided
  by borders that are not drawn (the contact table at the end of every release is six columns in the HWPX and four in
  the PDF).
  It rose again, to 0.568, when the box around a summary — a table of three rows and columns whose label sits across
  its top line — became a label and paragraphs in the reference (`docs/decisions/hwp.md`), as the PDF of five releases
  already read it. A wider form of that rule lowered TEDS on the holdout (0.580 to 0.578); sixty more releases fetched
  to look at showed which boxes the two readers disagree on, those were left out, and the holdout rose (0.581).
- **MHS 0.776 over 57 releases is not gated**: it says how far two readers agree, and a heading only one of them writes
  is not known to be wrong. The first run had 5 releases to measure it on: the HWPX reader wrote 11 headings in the 60
  releases and the PDF reader 140, because the title of a release stands in a one-column table in the HWPX, where a
  paragraph could not become a heading by its size. It can now (`docs/decisions/hwp.md`), the HWPX reader writes 58,
  and with the bars of the attachments 120. The PDF reader wrote 15 of those 52 bars as headings; it reads 55 lines as
  bars now (`docs/decisions/pdf.md`), which took this number from 0.640 to 0.750. What is left to read in it is
  headings one reader makes and the other does not. It rose to 0.776 when the HWPX reader stopped leaving out the
  titles by size of a document that has outline headings: the headline of one release, whose attachment is outlined,
  had been text in the HWPX and a heading in the PDF (four files). `mhs_s`, which compares the outlines without their
  text, fell from 0.875 to 0.862 on the same four: the reference has two more headings there than the PDF, the two
  outline heads, and the count of headings is all that number sees.

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
| Overall | 0.713 | 0.787 |
| NID | 0.918 | 0.935 |
| TEDS | 0.477 | 0.581 |
| MHS | 0.708 | 0.822 |
| Words | 0.963 | 0.978 |
| Line breaks taken out of a word, right / wrong by the HWPX | | 355 / 32 |

The gains are those of the first sixty (overall 0.666 → 0.747 there), and the rule that joins a word is right as often
(92% here, 93% there). The holdout also found what the first sixty did not have: a bar of three cells ('붙임2', a
figure, the title), which the HWPX reader wrote as a line and the PDF reader as a heading.

**Keep it a holdout.** Run it to judge a change; do not open its documents to write a rule. A rule comes from the
first sixty, or from a document of your own, and this list says whether it carries. When it has been looked at too
often to say so, find a new one (`--find 60 --from <id> --stride 3000`) and say so here.

## Korean releases to look at

```bash
uv run python evals/fetch_korean_pairs.py ../corpus/korean-dev --list korean_pairs_dev.tsv
D=../corpus/korean-dev
uv run python -m evals.run --documents $D/pdf --gold $D/hwpx --baseline evals/baselines/korean-dev.json
```

Sixty more releases (`korean_pairs_dev.tsv`: the ids right below the first sixty, in neither list above), for the day the
holdout says a rule does not carry and may not be opened to see why. This list may be opened. It was fetched when a
rule for the tables of a Hangul document (`docs/decisions/hwp.md`, the table that only frames text) raised every mean of
the first sixty and lowered TEDS on the holdout: two of these sixty fell the same way, and reading them showed which
tables the two readers disagree on — and showed that the first explanation, tried on them, was wrong. The rule was
narrowed from what they showed, and the holdout, run once more, rose. It is no gate and no holdout: a rule may be written
from it, and its baseline is there to see what a change does to it.

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
  TEDS on the twins is 0.846 (from 0.489). The same pages saved without tags (`pdf-untagged`) stay at 0.487 and
  stay the measure of what is read from the page alone: work on tables without rules is judged there. Outside the
  twins, four tax publications of the IRS (tagged by another program, 61 to 15 tagged tables each) kept every word
  they had and lost none, with their tables of contents and filing tables read as the tables they are tagged as.
- Headings are easy here (0.973 with tags, 0.969 without): a heading style is set large. The benchmark's own PDFs, where a heading is often
  bold at the body size, are the harder measure of headings.

A first form of this corpus took 116 public DOCX test files and pandoc's reading of them as the reference. It measured
conventions and test fixtures more than readers — tables with one filled column, placeholder text of empty form
fields, files of thirty characters — and was dropped for the references a person had checked.

The twins depend on the versions of pandoc and LibreOffice that made them (3.9 and 26.2 for the baselines), and on the
state of the machine they were made on. Made again on the same machine with the same versions in November 2026, two
of the 200 PDFs came out with another text in each of the two PDF folders, and the Japanese, Chinese and Korean script
twins in both: LibreOffice draws a character its font lacks in some other font it finds, and in the twins checked it
had found another one (a Korean display font where it had taken another before), with the scores of some moving by
more than 0.02. So they run where they were made, not in CI, and a copy of the twins that matches the baselines is
kept rather than made again.

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
- **The PDF reader reads the text and the headings of every script nearly as well** (NID 0.992, MHS 0.990 with tags; 0.983 and 0.987 without), from 0.791
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

## Recall: is every word of the document there?

```bash
uv run python -m evals.recall --documents $K/hwpx --baseline evals/baselines/recall-korean-pairs.json
uv run python -m evals.recall --documents $H/hwpx --baseline evals/baselines/recall-korean-holdout.json
uv run python -m evals.recall --documents ../corpus/twins/docx --baseline evals/baselines/recall-twins-docx.json
uv run python evals/fetch_hangul_samples.py ../corpus            # 183 sample documents of three Hangul libraries, about 30 MB
uv run python -m evals.recall --documents ../corpus/hangul-samples --baseline evals/baselines/recall-hangul-samples.json
uv run python -m evals.recall --documents <any folder of DOCX, PPTX, XLSX, HWPX, HWP> --show 10      # no baseline: just look
```

Every scorecard above needs a reference somebody wrote or another reader produced. This one needs none. A DOCX, a
PPTX, an XLSX and an HWPX state their text in their own XML (`w:t`, `a:t`, the strings of the cells, `hp:t`), and an
HWP 5 file in the text records of its paragraphs (`PARA_TEXT`), so the question "did the reader lose anything?" can be
put to any folder of documents (`evals/recall.py`). The source is read there on its own, flat - every text element,
every text record, whatever it hangs under - and not with the reader that is measured: a paragraph inside a control
the reader does not follow is still counted.

| Count | Meaning |
| :--- | :--- |
| `missing` | a word of the source that is nowhere in the Markdown, not even with a mark inside it (`CO<sub>2</sub>`) |
| `joined` | two neighbouring words the Markdown writes as one although something stands between them in the source |
| `private_use` | the private-use characters of the source: symbols of a font, which neither count above sees when one stands alone |

A run against a baseline ends with status 1 when a document misses or joins more than it did. `--show` groups the
losses by the chain of elements they sit in, which is how a cause is found: forty words under
`sdt/sdtContent/p/r/t` in one file are the prompts of empty content controls.

Where it stands (2026-11-22):

| Documents | Converted | Words | Missing | Joined |
| :--- | ---: | ---: | ---: | ---: |
| Korean pairs, HWPX | 59 of 60 (one is encrypted) | 35,190 | 0 | 0 |
| Korean holdout, HWPX | 60 | 41,505 | 0 | 0 |
| Format twins, DOCX | 200 | 64,290 | 0 | 0 |
| Hangul samples, HWP 5 | 91 of 93 | 40,234 | 91 | 0 |
| Hangul samples, HWPX | 90 | 32,645 | 78 | 0 |
| Apache POI `test-data/document`, DOCX | 117 of 134 | 87,793 | 231 | 0 |
| Apache POI `test-data/slideshow`, PPTX | 87 of 98 | 23,554 | 178 | 0 |
| Apache POI `test-data/spreadsheet`, XLSX | 338 of 367 | 95,584 | 56 | 0 |

The first three and the Hangul samples have a baseline here. The Apache POI files are that project's test documents, many of them broken on
purpose (the ones not converted are refused, cut at the character limit, or no ZIP package at all). Every place
they miss a word in was looked at. Seven words stand in a file that is broken on purpose (a document with three bodies,
a package holding one sheet twice, a header of a kind that does not exist); the rest are left out on purpose:

- the prompt of an empty content control (`w:showingPlcHdr`: "Click here to enter text") and the placeholders of a
  slide's footer, date and number;
- cells beyond the column limit, which the result says in a warning;
- the letters of an equation, written as LaTeX; a ligature written as its letters (`ﬁ` as `fi`); text moved away
  under change tracking.

The Hangul samples (`fetch_hangul_samples.py`) are the sample documents of hwplib, hwpxlib and hwp2hwpx at fixed
revisions: a small document for each thing a Hangul file can hold (a field, a form object, a comment, tracked changes,
a master page, a hidden comment …), most of them once as HWP 5 and once as HWPX, and the documents users of those
libraries reported, among them requests for proposals of 20,000 words. They are what the press releases are not: every
kind of control, and documents of another kind. Of the 183, one is a distribution document (its body is encrypted) and
one is cut at the character limit. Every missing word is of one of four kinds, in HWP and HWPX alike:

- the guide of a click-here field nobody filled in (84 words), a hidden comment (10) and a deletion under change
  tracking (1): left out on purpose (`docs/decisions/hwp.md`);
- the text of a master page (74 words in three samples made to show it): a gap, not a choice (`docs/limitations.md`).

What the count found when it was first run is the reason it is kept: 4 of the 120 Korean HWPX files wrote
`미국·영국·독일` as `미국영국독일`, because Hangul keeps that dot in a private-use character that was removed. No
word was missing, so `joined` is counted, and `private_use` says where a symbol that joins nothing may have gone the
same way. And the first run over the Hangul samples found a request for proposals refused as corrupted: its cover
sets the title in a one-column table between two empty cells, on which the rule for headings in such tables raised.
A baseline keeps the status of each document, so a document that was converted and is refused fails the run.

It does not see: text a reader should leave out and writes (a deletion under change tracking); order; structure;
numbers and dates of cells, which are written from a value and a format and not from a string; pictures. A PDF states
no text of its own apart from what the reader reads, so it has no recall of this kind.

## Changes: what did a rule do to the documents?

```bash
uv run python -m evals.changes --documents <folders> --save ../before.json.gz      # with the reader as it was (main)
uv run python -m evals.changes --documents <folders> --against ../before.json.gz   # with the change
```

A score needs a reference, and a mean can stand still while forty texts change. `evals/changes.py` needs neither: it
converts any folders of documents twice, with the reader before and after a change, and lists what moved — the texts that
changed and their changed blocks by kind (a table that became text, text that became a table), the first of each
document to read, the words a document held and holds nowhere now, the headings that appeared or went, the documents
refused now that were converted before (status 1), the warnings that changed, and the HWP and HWPX twins of one name that
converted alike and no longer do (the samples of hwp2hwpx are 41 such pairs). What it lists is what the description of a
change has to account for; it is the comparison this folder's rule asks for, on documents that have no reference.

To run the reader as it was, check `main` out beside the change (`git worktree add ../main main`) and put it first on the
path: `PYTHONPATH=../main/src uv run python -m evals.changes … --save …`.

**Time.** The seconds it prints are one run on a machine that does other work, and say nothing under a few percent. A
change must not make conversion slower than `main`, and that is measured where the change lives: wrap the function, collect
its time over every call a corpus makes of it, take the least of several passes, on both sides, in the order change, main,
main, change. Measured so, a question asked of every table cost 0.05 s on 20,640 tables, which a whole run of 91 s could
not show; and reading the parts of a cell in one pass over its children instead of three saved five times that.

## Parity: one document as HWP and as HWPX

```bash
uv run python evals/fetch_hangul_samples.py ../corpus            # once: the samples of hwplib, hwpxlib and hwp2hwpx
uv run python -m evals.parity --documents ../corpus/hangul-samples --baseline evals/baselines/parity-hangul-samples.json
```

HWP 5 and HWPX hold one document model, as records and as XML, and are read here by two readers that share their rules
(`_hangul.py`) and not their code. A rule that reaches one reader and not the other moves no scorecard — each output is
fine by itself — and is found only by a document that exists in both formats. `evals/parity.py` converts every
`<name>.hwp` and `<name>.hwpx` of a folder and says whether the two texts are the same. No reference is needed. The
samples of hwp2hwpx are 41 such pairs:

| pairs | alike | apart | refused |
|---|---|---|---|
| 41 | 40 | 1 | 0 |

The one apart is a combo box: the HWP file states its text (`def`), and the HWPX file states that text as an item of
its list with nothing selected, so there is no value to write. A baseline keeps the state of each pair, and a run ends
with status 1 when a pair that was alike is not any more. `--show <n>` prints the first blocks that differ.

## Damage: what does a broken document do to the reader?

```bash
uv run python -m evals.damage --documents ../corpus/hangul-samples [--rounds 6] [--keep ../failed]
```

`evals/damage.py` damages every document of a folder — the file cut and its bits flipped; a part of a ZIP package or a
stream of an HWP 5 file cut, flipped or given a very large number, inside a container that is written again so that the
damage reaches the reader of the part — and converts each copy in a worker of `IsolatedConverter`, as a server does. A
copy may be converted or refused. The run ends with status 1 when one ends its worker, takes more than 30 seconds, or
raises anything but a `ConversionError`; `--keep` writes those copies to a folder. The places damaged depend on the name
of the file alone, so every run and every system makes the same copies, and `--rounds` moves on to other places.

| run | copies | converted | refused | ended a worker, hung or raised |
|---|---|---|---|---|
| Hangul samples, one round (the `Hangul` workflow) | 8,018 | 6,579 | 1,439 | 0 |
| Hangul samples, six rounds, when the check was written | 47,193 | 38,675 | 8,518 | 0 |

No copy took longer than its document takes whole (the largest, 38 MB, two seconds). So the check found nothing when
it was written: it is there for the next change. It does not see memory on macOS, where a process cannot be given a
limit (the workflow runs on Linux, where a worker has two gigabytes), and it does not see a damaged copy that is read
wrongly but read.

The `Hangul` workflow runs the recall of the samples, their parity and their damage on pull requests that touch the
readers of HWP and HWPX or what they are built on.

## Encodings: text without a stated charset

```bash
uv run python -m evals.fetch_encoding_texts ../corpus/encodings        # once: one text in 59 languages, about 1.1 MB
uv run --python 3.12 python -m evals.encodings --texts ../corpus/encodings --baseline evals/baselines/encodings.json --show 3
```

A text file, a CSV, an HTML page or a mail part that names no charset and is not UTF-8 is read in a code page the reader
guesses (`_encoding.decode_text`). A wrong guess is silent and looks like text, and no corpus of documents shows it:
they are UTF-8 or say what they are. `evals/encodings.py` writes real text — the declaration of human rights in 59
languages, the texts of the script twins and 40 more — in the Windows, ISO 8859, DOS and Macintosh code pages each
language was written in, cut five ways (the start of a paragraph, a paragraph, four, the whole text, and one start among
rows of ASCII), as text and as HTML, and compares what the reader gives with what was encoded.

| samples | number | read right |
|---|---|---|
| of one language | 12,348 | 8,709 |
| with a name or a sign of another language behind them | 35,336 | 24,089 |

- **The second row is the one that found the defect.** A rule that looked the accented letters up in the alphabets of
  each code page read 10,191 of the first row right, and read French with `Žižek` in it as Czech (`thčse ŕ la
  conférence`), German with `5 µm` as Slovak (`5 ľm`) and English with `£5` as Polish. No text of one language shows
  that, so the starts of the first eight paragraphs are also written with each of 48 names and signs behind them.
- **A count is no gate.** That rule read more French right than it lost, and every count of French went up. The
  baseline keeps of each sample whether it was read right, and a run ends with status 1 when a sample that was read
  right is read wrong, or a code page and language of the baseline is missing from the run: 30 code pages of a
  language had a lost sample under that rule.
- **The baseline is written under Python 3.12.** The statistical part of the guess (charset-normalizer) gives some
  samples two code pages that are nearly as likely, and which of them comes first is not the same under every version
  of Python: 12 of 47,684 samples are read otherwise under 3.13 and 3.14, and a run there ends with status 1 on code
  that did not change. The card names the versions of Python and of charset-normalizer it was made with, the workflow
  runs 3.12, and a run with other versions says so before its comparison.
- **And with charset-normalizer 3.5.2, the version of the lockfile.** With 3.5.3, which a new install resolves to since
  2026-11-23, the same samples give 8,726 and 24,020: more of the first row, fewer of the second, and samples that were
  read right read wrong (Finnish and Spanish in Windows-1252, Estonian in Windows-1257). The rules of this reader come
  before the guess and do not change with it; what the library guesses for the rest does.
- **Most of what is read wrong is no defect of a rule but a text that reads two ways.** `è` is `č` in Windows-1250, a
  Czech sentence in Windows-1250 is a row of Western letters in Windows-1252, and DOS and Macintosh text is hardly
  guessed at all (`docs/decisions/encoding.md`). The numbers say where the reader stands, not what it should reach.

## What it does not measure yet

- XLSX, PPTX and mail: the twins cover the text formats only. HTML as it is found on the web, with navigation and
  layout tables around the text.
- Korean headings against a reference a person checked: the pairs above have none.
- The level of a heading: MHS treats every level alike.
- Text that is right but should not be there (running headers, watermarks) only as far as it lowers NID.
