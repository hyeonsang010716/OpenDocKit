#!/bin/sh
# Install the five tools, each the way its documentation recommends for "all formats", into separate virtual
# environments under $BENCH_VENVS (default /venvs). Run as root on Debian/Ubuntu. Versions are pinned.
set -e
VENVS="${BENCH_VENVS:-/venvs}"

# System packages that unstructured and kreuzberg document as requirements for full format support:
# file-type detection, PDF rendering, OCR (English and Korean), legacy Office conversion, pandoc-based formats.
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    libmagic1 poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-kor \
    libreoffice-writer libreoffice-impress libreoffice-calc pandoc \
    libgl1 libglib2.0-0 git curl ca-certificates

command -v uv >/dev/null 2>&1 || pip install -q --no-cache-dir uv || pip install -q --no-cache-dir --break-system-packages uv
export UV_LINK_MODE=copy

install() {  # name requirement
    uv venv -q --python 3.12 "$VENVS/$1"
    uv pip install -q --python "$VENVS/$1" "$2"
}
install opendockit   "opendockit[heic]==0.1.0"
install markitdown   "markitdown[all]==0.1.9"
install kreuzberg    "kreuzberg[all]==4.10.4"
install unstructured "unstructured[all-docs]==0.27.37"
install docling      "docling==2.150.0"
# Docling keeps some document formats behind extras of its core package; add the ones for office documents and mail.
uv pip install -q --python "$VENVS/docling" "docling-slim[format-opendocument,format-office,format-email]==2.150.0"

# Docling and unstructured download models on first use; fetch them now so no timing includes a download.
"$VENVS/docling/bin/docling-tools" models download >/dev/null 2>&1 || true
"$VENVS/unstructured/bin/python" -c "import nltk; [nltk.download(p, quiet=True) for p in ('punkt_tab', 'averaged_perceptron_tagger_eng')]" || true
du -sh "$VENVS"/* || true
