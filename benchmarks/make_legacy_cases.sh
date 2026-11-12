#!/bin/sh
# Run inside the benchmark image. Writes the formats Python cannot write, by converting the documents made by
# make_cases.py with LibreOffice and pandoc, so their content is the same known content.
set -e
cd "$(dirname "$0")/cases"
convert() { soffice --headless --convert-to "$1" --outdir . "$2" >/dev/null 2>&1; }
convert doc report.docx   && mv report.doc legacy-report.doc
convert rtf report.docx
convert odt report.docx
convert ppt deck.pptx     && mv deck.ppt legacy-deck.ppt
convert odp deck.pptx
convert xls sales.xlsx    && mv sales.xls legacy-sales.xls
convert ods sales.xlsx
pandoc release-notes.md -o release-notes.epub --metadata title="Release notes 2.4"
ls -1 | wc -l
