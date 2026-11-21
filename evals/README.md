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

`overall` is the mean of the three for each document, averaged over the documents. Conversion uses the default
options without page markers and picture placeholders (`evals/run.py::OPTIONS`), which are OpenDocKit's own marks and
not text of the document.

## Where OpenDocKit stands (2026-11-21)

| Converter | Overall | NID | TEDS | MHS | Needs a model |
| :--- | ---: | ---: | ---: | ---: | :---: |
| OpenDataLoader hybrid | 0.907 | 0.934 | 0.928 | 0.821 | yes |
| Docling | 0.882 | 0.898 | 0.887 | 0.824 | yes |
| **OpenDocKit (baseline)** | **0.875** | **0.914** | **0.776** | **0.796** | no |
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
uv run python -m evals.run --documents $K/pdf --gold $K/hwpx --baseline evals/baselines/korean-pairs.json --gate nid,nid_s,teds,teds_s
```

Sixty press releases of Korean ministries that the policy briefing site publishes as HWPX and as PDF
(`korean_pairs.tsv` lists them; the files themselves are not kept here). The HWPX states its tables and its order of
text, so its conversion is the reference for the PDF's: nobody labels anything, and the corpus can grow by a command.
The scores say how far the two readers agree, so read them as a watch for change, not as a grade:

- **NID 0.911**: the same text in the same order. This is what caught the first form of the tables drawn with
  horizontal rules, which unfolded the top of 17 releases and lost their titles while the English benchmark stood still.
- **TEDS 0.475** is low for a reason that is not the PDF reader's alone: the box of release time and distribution at
  the top of every release is a table in the HWPX and four pieces of text without a rule in the PDF.
- **MHS 0.599 over 52 releases is not gated**: it says how far two readers agree, and a heading only one of them writes
  is not known to be wrong. The first run had 5 releases to measure it on: the HWPX reader wrote 11 headings in the 60
  releases and the PDF reader 140, because the title of a release stands in a one-column table in the HWPX, where a
  paragraph could not become a heading by its size. It can now (`docs/decisions/hwp.md`), the HWPX reader writes 58,
  and what is left to read in this number is mostly headings the PDF reader makes and the HWPX reader does not.

Compared with `main` before the headings by weight were put back, the PDF reader writes 45 more headings in these
releases and loses none: the section titles of two long releases, the labels of summary boxes, and — wrongly — a line
of place and the quoted slogans of a list of companies.

## Format twins: one document in four formats

```bash
uv run python evals/make_twins.py $C/ground-truth/markdown ../corpus/twins     # needs pandoc and LibreOffice; about 15 minutes
T=../corpus/twins
for f in docx odt rtf pdf; do
  uv run python -m evals.run --plain --documents $T/$f --gold $C/ground-truth/markdown --baseline evals/baselines/twins-$f.json
done
```

The 200 hand-checked references of the benchmark, written by pandoc as DOCX files that say their structure outright
(heading styles, tables, lists) and saved by LibreOffice as ODT, RTF and PDF. The four files hold one document whose
right answer is known, so each reader is scored against the same reference, and a difference between two formats is
a difference between two readers. `--plain` converts without emphasis and links, which the references do not write.

| Twin | Overall | NID | TEDS | MHS |
| :--- | ---: | ---: | ---: | ---: |
| DOCX | 0.992 | 0.991 | 0.967 | 0.995 |
| ODT | 0.992 | 0.991 | 0.967 | 0.997 |
| RTF | 0.992 | 0.991 | 0.967 | 0.995 |
| PDF, as LibreOffice sets it | 0.913 | 0.955 | 0.330 | 0.966 |

- **The readers of the formats that state their structure agree with the reference and with each other.** No document
  is more than 0.15 lower in one of them than in another; 39 of the 42 tables are read exactly. What is left is
  convention (a table of one column is written as paragraphs).
- **The same documents as PDF lose their tables.** pandoc's tables have no rules, and LibreOffice sets them as
  aligned text: 3 of 42 are read to 0.9 or more, 9 not at all, the rest in part. On the benchmark's own PDFs, most of
  which rule their tables, TEDS is 0.776. Tables without any rule are where the PDF reader is weakest, and these 42,
  with their exact answers, are the place to work on it.
- Headings are easy here (0.966): a heading style is set large. The benchmark's own PDFs, where a heading is often
  bold at the body size, are the harder measure of headings.

A first form of this corpus took 116 public DOCX test files and pandoc's reading of them as the reference. It measured
conventions and test fixtures more than readers — tables with one filled column, placeholder text of empty form
fields, files of thirty characters — and was dropped for the references a person had checked.

The twins depend on the versions of pandoc and LibreOffice that made them (3.9 and 26.2 for the baselines), so they
run where they were made, not in CI.

## What it does not measure yet

- XLSX, PPTX, HTML, EPUB and mail: the twins cover the text formats only.
- Korean headings against a reference a person checked: the pairs above have none.
- The level of a heading: MHS treats every level alike.
- Text that is right but should not be there (running headers, watermarks) only as far as it lowers NID.
