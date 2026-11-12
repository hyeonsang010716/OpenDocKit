# Examples

One realistic document per format in [`samples/`](samples/), and exactly what OpenDocKit makes of each in
[`output/`](output/). Open a sample in its own application, then read the Markdown next to it.

```bash
uv run python examples/make_samples.py              # rebuild the samples (they are generated, not hand-made)
uv run python examples/run_examples.py              # convert them all, offline
uv run python examples/run_examples.py --describe   # also describe the pictures with a vision model
```

Warnings the converter raised are kept at the end of each output file in an HTML comment.

## What each sample shows

| Sample | Output | What to look at |
| --- | --- | --- |
| [`quarterly-report.docx`](samples/quarterly-report.docx) | [offline](output/quarterly-report.docx.md) · [described](output/quarterly-report.docx.described.md) | Heading levels, bullet and numbered lists, a table whose vertically merged cell is repeated on every row, a Word equation as LaTeX, and a chart picture that becomes a data table once a vision model reads it |
| [`launch-plan.pptx`](samples/launch-plan.pptx) | [offline](output/launch-plan.pptx.md) | One section per slide, bullet levels, speaker notes, a table, and a native chart turned into its data table |
| [`sales.xlsx`](samples/sales.xlsx) | [offline](output/sales.xlsx.md) | Two tables on one sheet kept apart, a merged title row lifted out of the table, currency / percent / date formats as Excel shows them, a chart, and a hidden sheet left out with a warning |
| [`service-report.pdf`](samples/service-report.pdf) | [offline](output/service-report.pdf.md) | The running header and page numbers are removed, a ruled table and a borderless table are both rebuilt, headings come from font size, and the two-column pages are read column by column |
| [`scanned-notice.pdf`](samples/scanned-notice.pdf) | [offline](output/scanned-notice.pdf.md) · [described](output/scanned-notice.pdf.described.md) | A page with no text layer: empty with a warning offline, fully transcribed by the vision model |
| [`revenue-chart.png`](samples/revenue-chart.png) | [offline](output/revenue-chart.png.md) · [described](output/revenue-chart.png.described.md) | An image file on its own: the chart's twelve values come back as a table |
| [`blue-green.html`](samples/blue-green.html) | [offline](output/blue-green.html.md) | Navigation, sidebar, script and hidden elements are dropped; a two-row header with `rowspan`/`colspan` is flattened; nested lists, a code block and MathML → LaTeX are kept |
| [`budget-review.eml`](samples/budget-review.eml) | [offline](output/budget-review.eml.md) | Mail headers, the HTML body read once (not the plain-text duplicate), a meeting invitation as an `Event:` list with its recurrence, and a warning naming the attachment that was not read |
| [`orders-export.xls`](samples/orders-export.xls) | [offline](output/orders-export.xls.md) | A "Download as Excel" file that is really an HTML table: detected by content, converted, and reported |
| [`branch-sales-cp949.csv`](samples/branch-sales-cp949.csv) | [offline](output/branch-sales-cp949.csv.md) | A Korean CSV in CP949 with title lines above the header row: the encoding is detected and the title lines stay outside the table |
| [`release-notes.md`](samples/release-notes.md) | [offline](output/release-notes.md.md) | Markdown passes through unchanged |
| [`table.hwp`](samples/table.hwp) | [offline](output/table.hwp.md) | HWP 5.x binary: a captioned table with a line break inside a cell |
| [`score-table.hwpx`](samples/score-table.hwpx) | [offline](output/score-table.hwpx.md) | HWPX: text and a table |
| [`numbered-outline.hwpx`](samples/numbered-outline.hwpx) | [offline](output/numbered-outline.hwpx.md) | HWPX: bullets, multi-level numbering (`3.1.`, `4.1.2.1.`) and Korean outline numbering (`가.`, `1)`) |

The Hangul samples are small public test files rather than realistic documents, because no Python library can write
those formats; see [`samples/THIRD_PARTY.md`](samples/THIRD_PARTY.md) for their source and license.

## Offline and described, side by side

`convert` never calls a network service. A picture stays where it was as a placeholder:

```markdown
## 2.1 Chart

The same figures as a picture. Without a vision model only its position is kept.

[Image]
```

With a describer, the same place holds what the picture says:

```markdown
> [Image] A grouped bar chart showing quarterly revenue by region in 2025.
>
> | Quarter | North | South | West |
> | --- | ---: | ---: | ---: |
> | Q1 | 120 | 80 | 60 |
> | Q2 | 135 | 85 | 72 |
> | Q3 | 150 | 95 | 70 |
> | Q4 | 170 | 110 | 91 |
```

The described outputs in this folder were produced with the default model (`gpt-5.6-terra`). A vision model's wording
varies from run to run, so regenerating them will change the prose, not the numbers.

## Your own files

```bash
uv run opendockit path/to/your.pdf > out.md      # warnings go to stderr
uv run opendockit path/to/your.pdf --json        # sections, metadata, warnings, images
```
