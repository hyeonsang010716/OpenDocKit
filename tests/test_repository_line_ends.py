"""The line ends of the repository (`.gitattributes`): what every checkout gets, whatever `core.autocrlf` says.

Git for Windows checks text files out with CR LF by default. The documents the readers are run on are data, so a checkout
gets them with the line ends they are stored with: on Windows the PDFs, mails, RTF and CSV samples were other bytes than on
Linux. The shell scripts and the Dockerfile are read by a POSIX shell or by Docker, from a Windows checkout too. The decision
is in docs/decisions/core.md.

Asked of Git (`check-attr`, `ls-files --eol`), not of the files on disk: a clone made before the attributes keeps its old
line ends until those files are checked out again, and that is no fault of the repository."""

from pathlib import Path
import subprocess

import pytest


_ROOT = Path(__file__).resolve().parent.parent
_DOCUMENTS = ("examples/samples/", "benchmarks/cases/")


def _git(*arguments: str, given: bytes = b"") -> list[str]:
    """The NUL-separated fields Git prints (`-z`). Bytes both ways: a text stream on Windows writes CR LF, which Git would
    read as part of a name."""
    done = subprocess.run(["git", *arguments, "-z"], cwd=_ROOT, input=given, capture_output=True, check=False)
    if done.returncode != 0:
        pytest.skip("not a checkout of the repository")
    return done.stdout.decode("utf-8").split("\0")[:-1]


def _attributes(paths: list[str]) -> dict[str, dict[str, str]]:
    """`text` and `eol` of each path, as Git applies them."""
    fields = _git("check-attr", "--stdin", "text", "eol", given=b"".join(path.encode("utf-8") + b"\0" for path in paths))
    found: dict[str, dict[str, str]] = {path: {} for path in paths}
    for path, name, value in zip(fields[0::3], fields[1::3], fields[2::3], strict=True):
        found[path][name] = value
    return found


def _tracked() -> list[str]:
    return _git("ls-files")


def test_documents_are_checked_out_with_the_line_ends_they_are_stored_with():
    documents = [path for path in _tracked() if path.startswith(_DOCUMENTS)]

    assert len(documents) > 40
    assert {path: value for path, value in _attributes(documents).items() if value != {"text": "auto", "eol": "lf"}} == {}


def test_shell_scripts_and_the_dockerfile_are_checked_out_with_lf():
    read_by_a_posix_shell = [path for path in _tracked() if path.endswith(".sh") or path.rsplit("/", 1)[-1] == "Dockerfile"]

    assert read_by_a_posix_shell
    assert {path: value for path, value in _attributes(read_by_a_posix_shell).items()
            if value != {"text": "set", "eol": "lf"}} == {}


def test_no_file_is_stored_with_cr_lf():
    # `i/crlf` or `i/mixed`: a text file whose stored form holds a CR LF. Binary files are `i/-text` and are not asked.
    stored = {entry.split("\t", 1)[1]: entry.split()[0] for entry in _git("ls-files", "--eol")}

    assert {path: kind for path, kind in stored.items() if kind in ("i/crlf", "i/mixed")} == {}
