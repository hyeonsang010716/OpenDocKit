#!/bin/sh
# Install the six tools, each the way its documentation recommends for "all formats", into separate environments
# under $BENCH_VENVS (default /venvs): a virtual environment for each Python tool, a node_modules folder for kordoc.
# Run as root on Debian/Ubuntu. Versions are pinned. kordoc needs Node 20 or newer on the PATH (a GitHub runner has
# it); without it kordoc is left out and the other five run as before.
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
# OPENDOCKIT_SPEC replaces the pinned release, e.g. with a wheel built from a checkout (the workflow's "checkout" choice).
install opendockit   "${OPENDOCKIT_SPEC:-opendockit[heic]==0.3.15}"
install markitdown   "markitdown[all]==0.1.9"
install kreuzberg    "kreuzberg[all]==4.10.4"
install unstructured "unstructured[all-docs]==0.27.37"
install docling      "docling==2.151.1"
# Docling keeps some document formats behind extras of its core package; add the ones for office documents and mail.
uv pip install -q --python "$VENVS/docling" "docling-slim[format-opendocument,format-office,format-email]==2.151.1"

# Docling and unstructured download models on first use; fetch them now so no timing includes a download.
"$VENVS/docling/bin/docling-tools" models download >/dev/null 2>&1 || true
"$VENVS/unstructured/bin/python" -c "import nltk; [nltk.download(p, quiet=True) for p in ('punkt_tab', 'averaged_perceptron_tagger_eng')]" || true

# kordoc is a Node package: the library is called from a small Node worker (worker_kordoc.mjs). Its optional
# dependencies (OCR, picture formats) are installed as `npm install kordoc` installs them; it downloads no model here.
if command -v node >/dev/null 2>&1 && [ "$(node -p 'process.versions.node.split(".")[0] >= 20')" = true ]; then
    mkdir -p "$VENVS/kordoc" && (cd "$VENVS/kordoc" && npm init -y >/dev/null 2>&1 && npm install --no-audit --no-fund --loglevel=error kordoc@4.25.3)
else
    echo "Node 20+ not found: kordoc is left out"
fi

# The scorer of the PDF benchmark suite (evals/) runs in an environment of its own, so that what it needs is not counted
# with what OpenDocKit installs.
install scorer "${OPENDOCKIT_SPEC:-opendockit[heic]==0.3.15}"
uv pip install -q --python "$VENVS/scorer" "rapidfuzz>=3.9"

# What each tool takes on disk as installed here, and how many packages that is.
python3 - "$VENVS" <<'PY'
import json, subprocess, sys
from pathlib import Path
venvs = Path(sys.argv[1])
sizes = {}
for tool in ("opendockit", "markitdown", "kreuzberg", "unstructured", "docling", "kordoc"):
    folder = venvs / tool
    if not folder.is_dir():
        continue
    megabytes = int(subprocess.run(["du", "-sm", str(folder)], capture_output=True, text=True).stdout.split()[0])
    if tool == "kordoc":
        packages = sum(1 for path in (folder / "node_modules").glob("*/package.json")) + sum(1 for path in (folder / "node_modules").glob("@*/*/package.json"))
    else:
        packages = sum(1 for path in folder.glob("lib/python*/site-packages/*.dist-info"))
    sizes[tool] = {"megabytes": megabytes, "packages": packages}
(venvs / "install.json").write_text(json.dumps(sizes, indent=1))
print(sizes)
PY
