"""The output of the scripts of `evals/`: UTF-8 with LF line ends, whatever the code page or locale, as the command line
writes its own (`opendockit.cli._write_output_as_utf8_lines`)."""

import ast
import os
from pathlib import Path
import subprocess
import sys


_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS = sorted(path for path in (_ROOT / "evals").glob("*.py") if "__main__" in path.read_text(encoding="utf-8"))
# Characters the code page of Korean Windows (cp949) has no byte for, beside Hangul it has
_NAMES = ["보고서.txt", "emoji \U0001f600.txt", "Tiếng Việt ệ.txt"]


def _run(*arguments: str) -> subprocess.CompletedProcess:
    """Run a script as a redirect under cp949 would see it: standard output and error in that code page. `PYTHONIOENCODING`
    makes them so on every system."""
    environment = {**os.environ, "PYTHONIOENCODING": "cp949"}
    return subprocess.run([sys.executable, *arguments], cwd=_ROOT, env=environment, capture_output=True, timeout=300, check=False)


class TestScriptOutput:
    def test_a_report_naming_documents_outside_the_code_page_is_written(self, tmp_path):
        """A report names documents and quotes their words, and the corpora are written in nineteen scripts and more. Sent
        to a file under cp949 it stopped with UnicodeEncodeError at the first character cp949 has no byte for."""
        folder = tmp_path / "corpus"
        folder.mkdir()
        for name in _NAMES:
            (folder / name).write_text("Hello 안녕하세요\n", encoding="utf-8")
        saved = tmp_path / "before.json.gz"
        assert _run("-m", "evals.changes", "--documents", str(folder), "--save", str(saved)).returncode == 0
        for name in _NAMES:
            (folder / name).write_text("Changed 바뀜\n", encoding="utf-8")

        compared = _run("-m", "evals.changes", "--documents", str(folder), "--against", str(saved))

        assert compared.returncode == 0, compared.stderr.decode("utf-8", "replace")
        report = compared.stdout.decode("utf-8")
        assert all(f"corpus/{name}" in report for name in _NAMES)
        assert "Hello 안녕하세요" in report and b"\r" not in compared.stdout

    def test_a_script_run_by_its_path_still_starts(self):
        """Some scripts are run by their path (`python evals/fetch_hangul_samples.py` in CI), where the `evals` package is
        not on the path: what sets the output is imported from the installed package."""
        shown = _run(str(Path("evals") / "fetch_korean_pairs.py"), "--help")

        assert shown.returncode == 0, shown.stderr.decode("utf-8", "replace")
        assert shown.stdout.startswith(b"usage:")

    def test_every_script_sets_its_output_before_it_starts(self):
        """Each `if __name__ == "__main__":` calls it first. Not in `main`, which the tests call with the output captured."""
        assert len(_SCRIPTS) >= 11
        for script in _SCRIPTS:
            tree = ast.parse(script.read_text(encoding="utf-8"))
            blocks = [node for node in tree.body if isinstance(node, ast.If) and "__main__" in ast.unparse(node.test)]
            first = blocks[0].body[0] if blocks else None
            assert isinstance(first, ast.Expr) and ast.unparse(first.value) == "_write_output_as_utf8_lines()", script.name
