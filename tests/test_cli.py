"""Command-line tool (`opendockit`): character count, path errors, output encoding, describer cleanup. The standalone CLI takes the
OpenAI key from the `OPENAI_API_KEY` environment variable, so there is no configuration-file parsing to test."""

import asyncio
import io
import os
from pathlib import Path
import subprocess
import sys

import pytest

from opendockit.cli import main
from tests import builder, file_at_length, held_open


class TestCount:
    def test_count_prints_only_the_number_of_characters(self, tmp_path, monkeypatch, capsys):
        """One file → character count. A frontend's upload limit uses this number."""
        path = tmp_path / "memo.txt"
        path.write_text("글자 수를 센다", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--count"])

        code = main()

        assert code == 0
        assert capsys.readouterr().out.strip() == "8"

    def test_count_marks_a_document_over_the_limit(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "long.txt"
        path.write_text("가" * 500_001, encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--count"])

        main()

        assert capsys.readouterr().out.strip() == "500001 exceeded"


def test_json_output_has_no_internal_fields(tmp_path, monkeypatch, capsys):
    """`--json` printed the internal assembly fields (placeholder key, etc.) as they were."""
    import json

    path = tmp_path / "memo.txt"
    path.write_text("본문", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--json"])

    code = main()

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["text"] == "본문"
    assert not {"marker_key", "base_warnings", "options", "fallbacks", "counted", "time_limited", "reached_sections"} & payload.keys()


class TestFileErrors:
    """A path problem is reported with a one-line message and exit code 1 instead of a traceback."""

    @pytest.mark.parametrize("make", [lambda root: root / "없는 파일.pdf", lambda root: root], ids=["missing", "directory"])
    def test_unreadable_path_prints_a_message_and_exits_with_1(self, tmp_path, monkeypatch, capsys, make):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["opendockit", str(make(tmp_path))])

        code = main()

        assert code == 1
        assert capsys.readouterr().err.startswith(("File not found: ", "Could not read the file "))

    @pytest.mark.skipif(os.name != "nt", reason="the 260-character limit of a path is Windows")
    def test_a_file_at_a_long_path_is_converted(self, tmp_path, monkeypatch, capsys):
        """A full path of 260 characters or more was 'File not found' on a Windows without LongPathsEnabled (the default),
        for a file that was there."""
        path = file_at_length(tmp_path, 320, builder.docx_bytes())
        monkeypatch.setattr("sys.argv", ["opendockit", str(path)])

        code = main()

        captured = capsys.readouterr()
        assert code == 0, captured.err
        assert "본문 첫 문단입니다." in captured.out

    @pytest.mark.skipif(os.name != "nt", reason="sharing modes that refuse a reader are Windows")
    def test_a_file_another_program_holds_open_says_so(self, tmp_path, monkeypatch, capsys):
        """An editor that holds its document open without sharing it is the likeliest reason a file cannot be read on
        Windows, and the message named a folder or a permission instead."""
        path = tmp_path / "열린 문서.docx"
        path.write_bytes(builder.docx_bytes())
        monkeypatch.setattr("sys.argv", ["opendockit", str(path)])

        with held_open(path):
            code = main()

        assert code == 1
        assert capsys.readouterr().err == (
            f"Could not read the file (another program has it open; close it there and try again): {path}\n")

    @pytest.mark.skipif(os.name != "nt", reason="sharing modes that refuse a reader are Windows")
    def test_a_folder_or_a_missing_permission_is_not_said_to_be_open_elsewhere(self, tmp_path, monkeypatch, capsys):
        """The C runtime reports a folder, a missing permission and a file held open alike (EACCES), so the open is asked
        again of Windows. A folder or a denied read must keep the sentence about a folder or a permission."""
        denied = tmp_path / "읽기 금지.docx"
        denied.write_bytes(builder.docx_bytes())
        user = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
        subprocess.run(["icacls", str(denied), "/deny", f"{user}:(RD)"], check=True, capture_output=True)
        try:
            for path in (tmp_path, denied):
                monkeypatch.setattr("sys.argv", ["opendockit", str(path)])

                code = main()

                assert code == 1
                assert capsys.readouterr().err == (
                    f"Could not read the file (it may be a directory, or permission may be missing): {path}\n")
        finally:
            subprocess.run(["icacls", str(denied), "/remove:d", user], check=True, capture_output=True)

    def test_reader_library_that_cannot_be_imported_prints_a_message(self, tmp_path, monkeypatch, capsys):
        """A reader library that cannot be imported on first use ended the command in a traceback."""
        from opendockit import extractors
        from opendockit.extractors import IMPORT_FAILED_MESSAGE
        from opendockit.models import DocumentFormat

        def broken(name: str):
            raise ModuleNotFoundError("No module named 'openpyxl.cell._writer'")

        # A fresh entry for the format, as in a process that has not converted one yet, whose reader cannot be imported
        monkeypatch.setattr(extractors, "import_module", broken)
        monkeypatch.setitem(extractors.EXTRACTORS, DocumentFormat.XLSX, extractors._on_first_use("spreadsheet", "extract_xlsx"))
        (tmp_path / "a.xlsx").write_bytes(builder.xlsx_bytes())
        monkeypatch.setattr("sys.argv", ["opendockit", str(tmp_path / "a.xlsx")])

        code = main()

        assert code == 1
        assert capsys.readouterr().err.strip() == IMPORT_FAILED_MESSAGE

    def test_file_over_the_size_limit_is_rejected_without_reading_it(self, tmp_path, monkeypatch, capsys):
        """The whole file was read into memory before the size check. The size is checked first from the file metadata."""

        path = tmp_path / "huge.pdf"
        with path.open("wb") as file:
            file.truncate(60 * 1024 * 1024)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["opendockit", str(path)])
        monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail("a file over the limit was read"))

        code = main()

        assert code == 1
        assert "The file is too large" in capsys.readouterr().err


def test_describe_closes_the_describer_before_the_event_loop_ends(tmp_path, monkeypatch, capsys):
    """The CLI finishes in a single asyncio.run. The HTTP client the describer created is closed before the loop ends."""
    from tests import builder

    closed = []

    class FakeDescriber:
        def __init__(self, model_name):
            pass

        async def describe(self, image):
            return "막대 차트"

        def check(self):
            pass

        async def aclose(self):
            closed.append(asyncio.get_running_loop())

    path = tmp_path / "a.png"
    path.write_bytes(builder.png_bytes())
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--describe"])
    monkeypatch.setattr("opendockit.cli.OpenAIImageDescriber", FakeDescriber)

    code = main()

    assert code == 0
    assert capsys.readouterr().out.strip() == "> [Image] 막대 차트"
    assert len(closed) == 1


class TestDescribeSetup:
    """`--describe` without the `openai` extra or an API key printed only "could not be described" and exited with 0, because
    the library reports every failed call as a warning on that image. The CLI now builds the model first and says why."""

    def _run(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "a.png"
        path.write_bytes(builder.png_bytes())
        monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--describe"])
        code = main()
        return code, capsys.readouterr()

    def test_missing_api_key_is_reported_and_exits_with_1(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_ADMIN_KEY", raising=False)

        code, output = self._run(tmp_path, monkeypatch, capsys)

        assert code == 1
        assert output.err.startswith("Cannot use --describe: ") and "OPENAI_API_KEY" in output.err
        assert output.out == ""

    def test_missing_extra_is_reported_with_the_install_hint(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "langchain_openai", None)  # importing it now raises ImportError

        code, output = self._run(tmp_path, monkeypatch, capsys)

        assert code == 1
        assert output.err.strip() == 'Cannot use --describe: OpenAIImageDescriber needs the optional vision dependencies: ' \
                                     'pip install "opendockit[openai]"'


class TestArguments:
    def test_count_and_json_cannot_be_used_together(self, tmp_path, monkeypatch, capsys):
        """Given both, only --count was honored and --json was silently ignored."""
        path = tmp_path / "memo.txt"
        path.write_text("본문", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--count", "--json"])

        with pytest.raises(SystemExit) as exited:
            main()

        assert exited.value.code == 2
        assert "--json" in capsys.readouterr().err

    @pytest.mark.parametrize("flags", [[], ["--json"], ["--count"]], ids=["text", "json", "count"])
    def test_output_has_lf_line_ends_on_every_platform(self, tmp_path, flags):
        """Python writes a text stream with the line end of the platform: on Windows every line of a redirected or piped
        output ended in CR LF, so the same document gave other bytes than on Linux and macOS. Read here through a pipe, as a
        redirect or another program reads it: the bytes are the conversion's own text, with LF line ends, everywhere."""
        import json
        import subprocess
        import sys

        from opendockit import convert_file

        path = tmp_path / "보고서.md"
        path.write_text("# 제목\n\n첫 문단입니다.\n\n- 항목 하나\n- 항목 둘\n", encoding="utf-8")

        process = subprocess.run([sys.executable, "-m", "opendockit", path.name, *flags], cwd=tmp_path, capture_output=True,
                                 timeout=60)

        assert process.returncode == 0, process.stderr
        assert b"\r" not in process.stdout
        if not flags:
            assert process.stdout == (convert_file(path).text + "\n").encode("utf-8")
        elif flags == ["--json"]:
            assert json.loads(process.stdout)["text"] == convert_file(path).text

    def test_messages_and_help_have_lf_line_ends_on_every_platform(self, tmp_path):
        """Standard error goes the same way, so that `2>&1` does not mix two line ends; so does the help, which is several
        lines."""
        import subprocess
        import sys

        missing = subprocess.run([sys.executable, "-m", "opendockit", "없는 파일.md"], cwd=tmp_path, capture_output=True,
                                 timeout=60)
        shown = subprocess.run([sys.executable, "-m", "opendockit", "--help"], cwd=tmp_path, capture_output=True, timeout=60)

        assert missing.stderr == "File not found: 없는 파일.md\n".encode()
        assert shown.returncode == 0
        assert shown.stdout.count(b"\n") > 5
        assert b"\r" not in shown.stdout

    @pytest.mark.parametrize("flag", [None, "--json"], ids=["text", "json"])
    def test_output_is_written_as_utf8_even_when_stdout_is_not(self, tmp_path, monkeypatch, flag):
        """Under `PYTHONIOENCODING=ascii` or a Windows redirect (cp949), Korean output ended in a UnicodeEncodeError traceback."""
        path = tmp_path / "memo.txt"
        path.write_text("한글 본문 ①", encoding="utf-8")
        buffer = io.BytesIO()
        monkeypatch.setattr("sys.stdout", io.TextIOWrapper(buffer, encoding="ascii"))
        monkeypatch.setattr("sys.argv", ["opendockit", str(path), *([flag] if flag else [])])

        code = main()

        import sys

        sys.stdout.flush()
        assert code == 0
        assert "한글 본문 ①" in buffer.getvalue().decode("utf-8")

    def test_closed_pipe_ends_quietly(self, tmp_path):
        """When the reader closed first, as with `| head`, a BrokenPipeError traceback was dumped on the terminal."""
        import subprocess
        import sys

        path = tmp_path / "long.txt"
        path.write_text("가나다라마바사\n" * 100_000, encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "opendockit", str(path)], cwd=tmp_path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process.stdout.read(10)
        process.stdout.close()
        stderr = process.stderr.read().decode()
        process.wait(timeout=30)

        assert "Traceback" not in stderr and "BrokenPipeError" not in stderr


    @pytest.mark.parametrize("flags", [[], ["--json"], ["--count"]])
    def test_closed_pipe_ends_quietly_after_a_short_output(self, tmp_path, flags):
        """Output smaller than the output buffer reached a closed pipe only at exit, outside the handler, and ended with
        'Exception ignored … BrokenPipeError' and exit code 120."""
        import subprocess
        import sys

        path = tmp_path / "short.txt"
        path.write_text("짧은 문서\n", encoding="utf-8")
        process = subprocess.Popen([sys.executable, "-m", "opendockit", str(path), *flags], cwd=tmp_path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process.stdout.close()  # before the conversion, which takes longer than starting the process
        stderr = process.stderr.read().decode()
        code = process.wait(timeout=30)

        assert stderr == ""
        assert code == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="closes file descriptor 1 in the child before it starts")
    @pytest.mark.parametrize("args", [["short.txt"], ["short.txt", "--count"], ["short.txt", "--json"], ["long.txt"], ["--version"]])
    def test_closed_standard_output_ends_quietly(self, tmp_path, args):
        """With file descriptor 1 closed (`opendockit file >&-`, pythonw, a detached service) Python sets sys.stdout to None,
        and the flush after the output ended in an AttributeError traceback. Nothing can be written: exit code 1, as for a
        closed pipe."""
        import os
        import subprocess

        (tmp_path / "short.txt").write_text("짧은 문서\n", encoding="utf-8")
        (tmp_path / "long.txt").write_text("가나다라마바사\n" * 100_000, encoding="utf-8")
        process = subprocess.run([sys.executable, "-m", "opendockit", *args], cwd=tmp_path, stderr=subprocess.PIPE,
                                 preexec_fn=lambda: os.close(1), timeout=60)

        assert process.stderr.decode() == ""
        assert process.returncode == 1

    @pytest.mark.parametrize("flag", ["--version", "--help", "-h"])
    def test_version_and_help_to_a_closed_pipe_end_quietly(self, tmp_path, flag):
        """argparse's version and help actions exited before the output was flushed, so `opendockit --version | true` and
        `opendockit --help | true` ended with 'Exception ignored … BrokenPipeError' and exit code 120."""
        import subprocess

        process = subprocess.Popen([sys.executable, "-m", "opendockit", flag], cwd=tmp_path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process.stdout.close()  # before the version is printed, which takes longer than starting the process
        stderr = process.stderr.read().decode()
        code = process.wait(timeout=30)

        assert stderr == ""
        assert code == 1

    @pytest.mark.skipif(sys.platform == "win32", reason="closes file descriptor 2 in the child before it starts")
    @pytest.mark.parametrize(("args", "code"), [(["missing.txt"], 1), (["a.png", "--count"], 0), (["--bogus", "a.png"], 2), ([], 2),
                                                (["a.png", "--count", "--json"], 2)],
                             ids=["error", "warning", "unknown option", "no path", "conflicting options"])
    def test_messages_go_nowhere_when_standard_error_is_closed(self, tmp_path, args, code):
        """With file descriptor 2 closed sys.stderr is None, and `print(file=None)` writes to standard output, as argparse's
        usage errors do: 'File not found', the warnings and `usage: …` went into the output. Usage errors keep exit code 2."""
        import os
        import subprocess

        (tmp_path / "a.png").write_bytes(builder.png_bytes())
        process = subprocess.run([sys.executable, "-m", "opendockit", *args], cwd=tmp_path, stdout=subprocess.PIPE,
                                 preexec_fn=lambda: os.close(2), timeout=60)

        output = process.stdout.decode()
        assert (output == "") if code else output.strip().isdigit()
        assert process.returncode == code

    @pytest.mark.parametrize(("args", "code", "output"), [(["missing.txt"], 1, ""), (["a.png", "--count"], 0, "7"),
                                                          (["blank.pdf"], 0, ""), (["--bogus", "a.png"], 2, "")],
                             ids=["error", "warning with --count", "warning", "usage error"])
    def test_messages_to_a_closed_pipe_end_quietly(self, tmp_path, args, code, output):
        """With standard error sent to a pipe whose reader has gone (`2>&1 | true`), a message for a missing file ended in a
        BrokenPipeError traceback, and a warning or a usage error failed again at exit with 'Exception ignored' and exit code
        120. The messages go nowhere; the output and the exit code are those of the run."""
        import subprocess

        (tmp_path / "a.png").write_bytes(builder.png_bytes())
        (tmp_path / "blank.pdf").write_text("  \n \n", encoding="utf-8")   # two warnings: not a PDF, and no text
        process = subprocess.Popen([sys.executable, "-m", "opendockit", *args], cwd=tmp_path,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        process.stderr.close()  # before the first message, which takes longer than starting the process
        written = process.stdout.read().decode()
        assert process.wait(timeout=60) == code
        assert written.strip() == output

    def test_help_is_printed_as_before(self, capsys, monkeypatch):
        from opendockit import cli

        monkeypatch.setattr("sys.argv", ["opendockit", "--help"])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 0
        out = capsys.readouterr().out
        assert out.startswith("usage: opendockit [-h] [--json | --count]") and "  -h, --help  " in out

    def test_version_is_printed_without_a_file(self, capsys, monkeypatch):
        """The other two commands print their version; this one wanted a file first."""
        from opendockit import __version__, cli

        monkeypatch.setattr("sys.argv", ["opendockit", "--version"])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 0
        assert capsys.readouterr().out == f"opendockit {__version__}\n"

def test_json_output_summarizes_picture_tiles_like_picture_data(tmp_path, monkeypatch, capsys):
    """The tiles of a very tall picture (`tiles`) came out in `--json` as raw bytes (repr) and the output went over 1MB. Like `data`,
    only their size is written."""
    import json

    from tests import builder

    path = tmp_path / "tall.png"
    path.write_bytes(builder.png_bytes(700, 7000))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["opendockit", str(path), "--json"])

    code = main()

    image = json.loads(capsys.readouterr().out)["images"][0]
    assert code == 0
    assert len(image["tiles"]) >= 2
    assert all(tile.startswith("<") and tile.endswith(" bytes>") for tile in image["tiles"])
