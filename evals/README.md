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
| **OpenDocKit (baseline)** | **0.874** | **0.914** | **0.776** | **0.791** | no |
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

## What it does not measure yet

- Formats other than PDF. The runner takes any folder of documents and references, so a second corpus needs no code.
- Korean documents. The plan is pairs of one document in two formats — the HWPX of a press release as the reference
  for its PDF — which need no labelling by hand.
- The level of a heading: MHS treats every level alike.
- Text that is right but should not be there (running headers, watermarks) only as far as it lowers NID.
