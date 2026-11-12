#!/bin/sh
# Download the public test documents used by the corpus and hostile-input suites, at fixed revisions.
#   sh benchmarks/fetch_corpus.sh <target-dir>
set -e
target="${1:?usage: fetch_corpus.sh <target-dir>}"
mkdir -p "$target" && cd "$target"

fetch() {  # name url revision [sparse paths...]
    name="$1"; url="$2"; revision="$3"; shift 3
    [ -d "$name" ] && return
    git init -q "$name" && cd "$name"
    git remote add origin "$url"
    if [ "$#" -gt 0 ]; then git sparse-checkout set "$@"; fi
    git fetch -q --depth 1 --filter=blob:none origin "$revision" && git checkout -q FETCH_HEAD
    cd ..
}

fetch poi        https://github.com/apache/poi.git        a4e8640da7c3bb906efc6fa11914d1b80f3cc9dc test-data/document test-data/slideshow test-data/spreadsheet
fetch pdfplumber https://github.com/jsvine/pdfplumber.git 4c64b92d5caccd71c645e98e0fabb0c4dba7ff45
fetch hwplib     https://github.com/neolord0/hwplib.git   6746c27f17ebf5277493206284aa32044a1839c4
fetch hwpxlib    https://github.com/neolord0/hwpxlib.git  f9fd2255ac0fc57414e0b657d115e7de51d31c65
