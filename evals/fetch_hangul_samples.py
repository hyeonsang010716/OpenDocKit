"""Fetch the sample documents of three public Hangul libraries, at fixed revisions, into one folder for `evals/recall.py`.

    uv run python evals/fetch_hangul_samples.py <target-dir>      # writes <target-dir>/hangul-samples

hwplib, hwpxlib and hwp2hwpx (github.com/neolord0, Apache-2.0) keep a small document for each thing a Hangul file can
hold - a table, a text box, a field, a form object, a comment, tracked changes, a master page - and the documents their
users reported as unreadable; hwp2hwpx keeps most of them twice, as HWP 5 and as HWPX. Only the sample folders are checked
out, and none of the code is run. A document is named by its place: `hwp2hwpx__table.hwp`, `hwplib__basic__표.hwp`.
"""

from pathlib import Path
import shutil
import subprocess
import sys

from opendockit.cli import _write_output_as_utf8_lines


SOURCES = (  # (repository, revision, folder of the samples)
    ("hwplib", "f59018d8e31acb285e180f00f5637ae36532df16", "sample_hwp"),
    ("hwpxlib", "f9fd2255ac0fc57414e0b657d115e7de51d31c65", "testFile"),
    ("hwp2hwpx", "62d95b980a4e3a6766877dd3fb6f7f45f4afcf3d", "test"),
)
SUFFIXES = (".hwp", ".hwpx")


def _git(folder: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(folder), *arguments], check=True)


def _checkout(target: Path, repository: str, revision: str, samples: str) -> Path:
    clone = target / repository
    if not clone.is_dir():
        clone.mkdir(parents=True)
        _git(clone, "init", "-q")
        _git(clone, "remote", "add", "origin", f"https://github.com/neolord0/{repository}.git")
        _git(clone, "sparse-checkout", "set", samples)
        _git(clone, "fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", revision)
        _git(clone, "checkout", "-q", "FETCH_HEAD")
    return clone / samples


def sample_name(repository: str, relative: Path) -> str:
    """`test/table/from.hwp` of hwp2hwpx is `hwp2hwpx__table.hwp`: the folders down to the file, `from` left out."""
    parts = [part for part in relative.with_suffix("").parts if part != "from"]
    return "__".join([repository, *parts]).replace(" ", "_") + relative.suffix.lower()


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if len(arguments) != 1:
        print(__doc__.split("\n\n")[1].strip())
        return 2
    target = Path(arguments[0])
    samples = target / "hangul-samples"
    samples.mkdir(parents=True, exist_ok=True)
    count = 0
    for repository, revision, folder in SOURCES:
        root = _checkout(target / "hangul-sources", repository, revision, folder)
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix.lower() in SUFFIXES:
                shutil.copyfile(path, samples / sample_name(repository, path.relative_to(root)))
                count += 1
    print(f"{count} documents in {samples}")
    return 0


if __name__ == "__main__":
    _write_output_as_utf8_lines()   # UTF-8 whatever the code page, as the command line (Pinned by: test_evals_output.py)
    sys.exit(main())
