"""Make the format twins of a folder of reference Markdown, for the twins scorecard (`evals/README.md`).

    uv run python evals/make_twins.py <reference-dir> <target-dir>

Each reference `<name>.md` — the hand-checked Markdown of a benchmark document — is written by pandoc as a DOCX that says
its structure outright (heading styles, tables, lists), and that DOCX is saved by LibreOffice as ODT, RTF and PDF
(`<target-dir>/docx`, `/odt`, `/rtf`, `/pdf`). The four files hold one document whose right answer is known, so what
OpenDocKit writes for each is scored against the same reference, and a difference between two formats is a difference
between two readers. Needs `pandoc` and `soffice` on the path; nothing made here is committed.

The PDF is saved twice: as LibreOffice saves it, with the structure tree of a tagged PDF (`/pdf`), and without one
(`/pdf-untagged`), the same pages with nothing but the page to read the tables from.

A reference written in Hebrew or Arabic letters is laid out from right to left (`_direction`), its tables too
(`_tables_from_the_right`).

The Markdown goes through HTML on its way to DOCX: the references write their tables as HTML, which pandoc reads as
tables only from HTML. That HTML is kept as a twin of its own (`/html`, a whole page with a head), and pandoc writes
it as an EPUB too (`/epub`).
"""

from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unicodedata
import zipfile

from opendockit.cli import _write_output_as_utf8_lines


# folder → what LibreOffice is asked to convert to
FORMATS = {"odt": "odt", "rtf": "rtf", "pdf": "pdf",
           "pdf-untagged": 'pdf:writer_pdf_Export:{"UseTaggedPDF":{"type":"boolean","value":"false"}}'}
_BATCH = 25
_SECONDS_PER_BATCH = 600
_PANDOC_SECONDS = 60


def _run(command: list[str], seconds: int, *, stdin: bytes | None = None) -> bytes | None:
    """What the command wrote, or None when it failed or took too long."""
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        return None
    return done.stdout if done.returncode == 0 else None


def _direction(reference: Path) -> list[str]:
    """What tells pandoc to lay the document out from right to left, for a reference most of whose letters are Hebrew
    or Arabic ones: a paragraph of such a document typed into a left-to-right one is another layout (its full stop at
    the right end), and not the one the documents of those languages have."""
    kinds = [unicodedata.bidirectional(letter) for letter in reference.read_text(encoding="utf-8") if letter.isalpha()]
    leftwards = sum(kind in ("R", "AL") for kind in kinds)
    return ["--metadata", "dir=rtl"] if leftwards > len(kinds) - leftwards else []


def _tables_from_the_right(document: Path) -> None:
    """Lay the tables of a DOCX out from right to left (`w:bidiVisual`), as Word does in a document of Hebrew or
    Arabic: its first column is then the one on the right. pandoc writes the paragraphs of such a document from right
    to left and leaves its tables as they are."""
    with zipfile.ZipFile(document) as package:
        parts = {name: package.read(name) for name in package.namelist()}
    body = parts["word/document.xml"]
    if b"<w:tblPr>" not in body or b"<w:bidiVisual" in body:
        return
    parts["word/document.xml"] = body.replace(b"<w:tblPr>", b"<w:tblPr><w:bidiVisual />")
    with zipfile.ZipFile(document, "w", zipfile.ZIP_DEFLATED) as package:
        for name, data in parts.items():
            package.writestr(name, data)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: make_twins.py <reference-dir> <target-dir>", file=sys.stderr)
        return 2
    references, target = Path(sys.argv[1]), Path(sys.argv[2])
    for tool in ("soffice", "pandoc"):
        if shutil.which(tool) is None:
            print(f"{tool} is not on the path", file=sys.stderr)
            return 2
    originals = target / "docx"
    originals.mkdir(parents=True, exist_ok=True)
    for reference in sorted(references.glob("*.md")):
        out = originals / f"{reference.stem}.docx"
        if out.is_file():
            continue
        html = _run(["pandoc", "-f", "gfm", "-t", "html", str(reference)], _PANDOC_SECONDS)
        direction = _direction(reference)
        command = ["pandoc", "-f", "html", "-t", "docx", *direction, "-o", str(out)]
        if html is None or _run(command, _PANDOC_SECONDS, stdin=html) is None:
            out.unlink(missing_ok=True)
        elif direction:
            _tables_from_the_right(out)
    documents = sorted(originals.glob("*.docx"))
    print(f"{'docx':12} {len(documents):4}")
    for name, writer in (("html", ["-t", "html", "-s"]), ("epub", ["-t", "epub"])):
        folder = target / name
        folder.mkdir(exist_ok=True)
        for reference in sorted(references.glob("*.md")):
            out = folder / f"{reference.stem}.{name}"
            if out.is_file() or not (originals / f"{reference.stem}.docx").is_file():
                continue
            html = _run(["pandoc", "-f", "gfm", "-t", "html", str(reference)], _PANDOC_SECONDS)
            # the title is the name of the file, which no reference holds: a page needs one, and it is left out of the body
            command = ["pandoc", "-f", "html", *writer, "--metadata", f"pagetitle={reference.stem}", "-o", str(out)]
            if html is None or _run(command, _PANDOC_SECONDS, stdin=html) is None:
                out.unlink(missing_ok=True)
        print(f"{name:12} {len(list(folder.iterdir())):4}")

    # A profile of its own, so a LibreOffice the user has open is not the one that converts
    with tempfile.TemporaryDirectory() as profile:
        for name, convert_to in FORMATS.items():
            folder = target / name
            folder.mkdir(exist_ok=True)
            suffix = convert_to.partition(":")[0]
            missing = [path for path in documents if not (folder / f"{path.stem}.{suffix}").is_file()]
            for start in range(0, len(missing), _BATCH):
                _run(["soffice", f"-env:UserInstallation={Path(profile).as_uri()}", "--headless", "--convert-to", convert_to,
                      "--outdir", str(folder), *(str(path) for path in missing[start:start + _BATCH])], _SECONDS_PER_BATCH)
            print(f"{name:12} {len(list(folder.iterdir())):4}")
    return 0


if __name__ == "__main__":
    _write_output_as_utf8_lines()   # UTF-8 whatever the code page, as the command line (Pinned by: test_evals_output.py)
    sys.exit(main())
