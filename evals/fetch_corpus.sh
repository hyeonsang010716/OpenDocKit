#!/bin/sh
# Download the public PDF structure benchmark the scorecard is measured on, at a fixed revision (needs Git LFS).
#   sh evals/fetch_corpus.sh <target-dir>
# opendataloader-bench (Apache-2.0): 200 PDFs and a hand-checked reference Markdown for each. Only the documents and
# the references are checked out; none of its code is run.
set -e
target="${1:?usage: fetch_corpus.sh <target-dir>}"
mkdir -p "$target" && cd "$target"
[ -d opendataloader-bench ] && exit 0
git init -q opendataloader-bench && cd opendataloader-bench
git remote add origin https://github.com/opendataloader-project/opendataloader-bench.git
git sparse-checkout set pdfs ground-truth/markdown
git fetch -q --depth 1 --filter=blob:none origin 7af1d8f4d0c09f51ea1a5c6ba5f66e993286d109 && git checkout -q FETCH_HEAD
