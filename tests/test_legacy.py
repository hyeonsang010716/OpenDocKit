"""Legacy Word (.doc) and PowerPoint (.ppt), converted through an installed LibreOffice (docs/decisions/legacy-office.md).

The unit tests replace the lookup and the runner, so they pass on a machine without LibreOffice and never start it. The
tests in `TestRealLibreOffice` start the real program; they are skipped when it is not installed and are marked `timing`
because one start takes seconds and should not compete with the parallel group.
"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

import pytest

from opendockit import (
    ConvertOptions,
    CorruptedDocumentError,
    DocumentFormat,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedFormatError,
    convert,
)
from opendockit.extractors import legacy
from opendockit.isolated import _DEFAULT_TIMEOUT
from tests import builder


_FAKE_PROGRAM = "/nonexistent/soffice"
_WORD_FILTER = "MS Word 2007 XML"
_NO_LIMIT = 10**9


def _doc(flags: int = 0, *, stream: str = "WordDocument", size: int = 4096) -> bytes:
    """An OLE container that detection takes for a Word document: a `WordDocument` stream that starts with a FIB whose 16-bit
    flags (offset 10) are `flags`. Nothing else of the document exists — the unit tests never read further."""
    fib = b"\xec\xa5\xc1\x00" + b"\x00" * 6 + flags.to_bytes(2, "little")
    return builder.ole_container({stream: fib[:size].ljust(size, b"\x00")})


def _ppt(token: int = 0xE391C05F, *, more: dict[str, bytes] | None = None, current_user: bool = True) -> bytes:
    """An OLE container that detection takes for a presentation: a `PowerPoint Document` stream and a `Current User` stream
    whose CurrentUserAtom (8-byte record header, 4-byte size, then the header token) carries `token`."""
    streams = {"PowerPoint Document": b"\x00" * 4096}
    if current_user:
        streams["Current User"] = b"\x00\x00\xf6\x0f\x20\x00\x00\x00" + (0x14).to_bytes(4, "little") + token.to_bytes(4, "little") + b"\x00" * 16
    return builder.ole_container(streams | (more or {}))


def _zip_of(size: int) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", b"\x00" * size)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _fresh_lookup(monkeypatch):
    """Every test starts without a cached lookup and without the override of the developer's shell."""
    monkeypatch.delenv(legacy.SOFFICE_ENVIRONMENT_VARIABLE, raising=False)
    legacy.reset_soffice_cache()
    yield
    legacy.reset_soffice_cache()


@pytest.fixture
def runner(monkeypatch):
    """LibreOffice "installed", with the runner replaced: `runner.result` is returned (or raised, when it is an exception)
    and the calls are recorded. No process is started."""

    class Runner:
        result: object = None

        def __init__(self) -> None:
            self.calls: list[tuple] = []

        def __call__(self, program, data, extension, target, output_filter, max_output_bytes):
            self.calls.append((program, data, extension, target, output_filter, max_output_bytes))
            if isinstance(self.result, BaseException):
                raise self.result
            return self.result

    fake = Runner()
    monkeypatch.setattr(legacy, "find_soffice", lambda: _FAKE_PROGRAM)
    monkeypatch.setattr(legacy, "_run_soffice", fake)
    return fake


class TestWithoutLibreOffice:
    """Without LibreOffice the formats stay unsupported, but the message says what to do about it."""

    @pytest.fixture(autouse=True)
    def _not_installed(self, monkeypatch):
        monkeypatch.setattr(legacy, "find_soffice", lambda: None)
        monkeypatch.setattr(legacy, "_run_soffice", lambda *args: pytest.fail("LibreOffice must not be started"))

    def test_word_document_is_rejected_with_install_or_resave_advice(self):
        with pytest.raises(UnsupportedFormatError) as caught:
            convert(_doc(), "분기 보고서.doc")

        message = str(caught.value)
        assert "Legacy Word (.doc)" in message
        assert "LibreOffice" in message and "apt install libreoffice" in message
        assert "save the file as DOCX" in message
        assert "분기" not in message

    def test_presentation_is_rejected_with_install_or_resave_advice(self):
        with pytest.raises(UnsupportedFormatError) as caught:
            convert(_ppt(), "deck.ppt")

        message = str(caught.value)
        assert "Legacy PowerPoint (.ppt)" in message
        assert "LibreOffice" in message
        assert "save the file as PPTX" in message


class TestLookup:
    """Where the program is looked for: the override first, then PATH, then the standard install locations."""

    @pytest.fixture
    def machine(self, monkeypatch):
        """A machine described by two sets: the names found on PATH and the files that exist."""

        class Machine:
            on_path: dict[str, str] = {}
            files: set[str] = set()
            searches = 0

        machine = Machine()

        def which(name):
            machine.searches += 1
            return machine.on_path.get(name)

        monkeypatch.setattr(legacy.shutil, "which", which)
        monkeypatch.setattr(legacy.os.path, "isfile", lambda path: path in machine.files)
        for variable in legacy._WINDOWS_FOLDER_VARIABLES:
            monkeypatch.delenv(variable, raising=False)
        return machine

    def test_program_on_path_is_found_under_either_name(self, machine):
        machine.on_path = {"libreoffice": "/usr/bin/libreoffice"}
        assert legacy.find_soffice() == "/usr/bin/libreoffice"

        legacy.reset_soffice_cache()
        machine.on_path = {"soffice": "/opt/homebrew/bin/soffice", "libreoffice": "/usr/bin/libreoffice"}
        assert legacy.find_soffice() == "/opt/homebrew/bin/soffice"

    def test_standard_macos_application_is_found_without_path(self, machine):
        machine.files = {"/Applications/LibreOffice.app/Contents/MacOS/soffice"}

        assert legacy.find_soffice() == "/Applications/LibreOffice.app/Contents/MacOS/soffice"

    def test_standard_windows_folder_is_found_without_path(self, machine, monkeypatch):
        monkeypatch.setenv("PROGRAMFILES", "C:\\Program Files")
        expected = os.path.join("C:\\Program Files", "LibreOffice", "program", "soffice.exe")
        machine.files = {expected}

        assert legacy.find_soffice() == expected

    def test_nothing_installed_is_none(self, machine):
        assert legacy.find_soffice() is None

    def test_search_is_cached_until_reset(self, machine):
        """The search walks PATH and probes several folders; one conversion per upload must not repeat it. A reset is the
        way to notice a LibreOffice installed while the process runs."""
        assert legacy.find_soffice() is None
        searches = machine.searches
        machine.on_path = {"soffice": "/usr/bin/soffice"}

        assert legacy.find_soffice() is None
        assert machine.searches == searches

        legacy.reset_soffice_cache()
        assert legacy.find_soffice() == "/usr/bin/soffice"

    def test_environment_override_is_used_instead_of_the_search(self, machine, monkeypatch):
        machine.on_path = {"soffice": "/usr/bin/soffice"}
        machine.files = {"/custom/lo/soffice"}
        monkeypatch.setenv(legacy.SOFFICE_ENVIRONMENT_VARIABLE, "/custom/lo/soffice")

        assert legacy.find_soffice() == "/custom/lo/soffice"
        assert machine.searches == 0

    def test_empty_override_switches_the_bridge_off(self, machine, monkeypatch):
        """An operator who does not want a subprocess sets the variable to nothing; an installed LibreOffice is then ignored."""
        machine.on_path = {"soffice": "/usr/bin/soffice"}
        monkeypatch.setenv(legacy.SOFFICE_ENVIRONMENT_VARIABLE, "")

        assert legacy.find_soffice() is None
        with pytest.raises(UnsupportedFormatError, match="need LibreOffice"):
            convert(_doc(), "a.doc")

    def test_override_that_is_not_a_file_does_not_fall_back_to_the_search(self, machine, monkeypatch):
        """A mistyped override must not silently run some other program found on PATH."""
        machine.on_path = {"soffice": "/usr/bin/soffice"}
        monkeypatch.setenv(legacy.SOFFICE_ENVIRONMENT_VARIABLE, "/custom/missing/soffice")

        assert legacy.find_soffice() is None

    def test_override_is_read_on_every_call(self, machine, monkeypatch):
        machine.on_path = {"soffice": "/usr/bin/soffice"}
        assert legacy.find_soffice() == "/usr/bin/soffice"

        monkeypatch.setenv(legacy.SOFFICE_ENVIRONMENT_VARIABLE, "")
        assert legacy.find_soffice() is None


class TestConversion:
    """What happens around the LibreOffice run, with the run itself replaced."""

    def test_converted_docx_is_read_by_the_docx_extractor_and_reported_as_doc(self, runner):
        runner.result = builder.docx_bytes()
        data = _doc()

        result = convert(data, "보고서.doc")

        assert result.format is DocumentFormat.DOC
        assert "# 1장 개요" in result.text
        assert "| 과일 | 배 | 20 |" in result.text
        assert result.metadata["title"] == "분기 보고서"
        assert result.warnings == [legacy.CONVERTED_WARNING]
        assert runner.calls == [(_FAKE_PROGRAM, data, "doc", "docx", _WORD_FILTER, ConvertOptions().max_uncompressed_bytes)]

    def test_converted_pptx_is_read_by_the_pptx_extractor_and_reported_as_ppt(self, runner):
        runner.result = builder.pptx_bytes()
        data = _ppt()

        result = convert(data, "계획.ppt")

        assert result.format is DocumentFormat.PPT
        assert [section.title for section in result.sections] == ["사업 계획", "핵심 목표", "실적"]
        assert "> Speaker notes: 여기서 수치를 강조" in result.text
        assert result.warnings == [legacy.CONVERTED_WARNING]
        assert runner.calls[0][2:5] == ("ppt", "pptx", "Impress MS PowerPoint 2007 XML")

    def test_options_reach_the_inner_extractor(self, runner):
        """The caller's options apply to the converted file: speaker notes can be left out of a .ppt like of a .pptx."""
        runner.result = builder.pptx_bytes()

        result = convert(_ppt(), "a.ppt", ConvertOptions(include_speaker_notes=False))

        assert "Speaker notes" not in result.text

    def test_character_limit_stops_the_converted_document(self, runner):
        runner.result = builder.docx_bytes()

        result = convert(_doc(), "a.doc", ConvertOptions(max_chars=10))

        assert result.exceeded

    def test_no_output_is_a_corrupted_document(self, runner):
        """LibreOffice exits normally and writes nothing for a file it cannot read (also for a password it cannot ask for)."""
        runner.result = None

        with pytest.raises(CorruptedDocumentError, match="LibreOffice could not convert"):
            convert(_doc(), "a.doc")

    def test_timeout_is_a_too_large_document(self, runner):
        runner.result = legacy.ConversionTimeout()

        with pytest.raises(DocumentTooLargeError, match="took too long"):
            convert(_ppt(), "a.ppt")

    def test_program_that_cannot_be_started_is_unsupported_without_the_system_error_text(self, runner):
        runner.result = PermissionError(13, "Permission denied: '/secret/path/soffice'")

        with pytest.raises(UnsupportedFormatError) as caught:
            convert(_doc(), "a.doc")

        assert str(caught.value) == "LibreOffice could not be started to convert the document."
        assert isinstance(caught.value.__cause__, PermissionError)

    def test_output_that_is_not_a_zip_is_a_corrupted_document(self, runner):
        runner.result = b"not a zip archive"

        with pytest.raises(CorruptedDocumentError, match="LibreOffice could not convert"):
            convert(_doc(), "a.doc")

    def test_converted_file_that_expands_past_the_limit_is_rejected_before_extraction(self, runner, monkeypatch):
        """The converter's ZIP check runs on the detected format (DOC), so the DOCX that comes back has not been through it.
        A small .doc that LibreOffice turns into a ZIP of 2 MB of content must not reach python-docx when the limit is 1 MB."""
        runner.result = _zip_of(2 * 1024 * 1024)
        monkeypatch.setattr(legacy, "extract_docx", lambda *args: pytest.fail("extracted a document over the limit"))

        with pytest.raises(DocumentTooLargeError, match="too large when decompressed"):
            convert(_doc(), "a.doc", ConvertOptions(max_uncompressed_bytes=1024 * 1024))

        assert runner.calls[0][5] == 1024 * 1024


class TestEncryption:
    """Password-protected files are recognized from the container, before (and without) LibreOffice."""

    def test_word_encryption_flag_is_reported_as_encrypted(self, runner):
        """fEncrypted is bit 8 of the FIB flags. LibreOffice is not started on a file it could only fail on."""
        with pytest.raises(EncryptedDocumentError, match="password-protected"):
            convert(_doc(0x0100), "a.doc")

        assert runner.calls == []

    def test_word_obfuscation_flag_alone_is_encrypted_only_with_the_encryption_bit(self, runner):
        """Every other bit of the flags set (table stream choice, obfuscation, far-east…) is not encryption: fObfuscated only
        qualifies fEncrypted. Such a file goes on to LibreOffice."""
        runner.result = builder.docx_bytes()

        result = convert(_doc(0xFEFF), "a.doc")

        assert len(runner.calls) == 1
        assert result.warnings == [legacy.CONVERTED_WARNING]

    def test_word_stream_name_is_matched_in_any_case(self, runner):
        with pytest.raises(EncryptedDocumentError):
            convert(_doc(0x0100, stream="WORDDOCUMENT"), "a.doc")

    def test_word_stream_too_short_for_the_flags_goes_on_to_libreoffice(self, runner):
        """A 4-byte stream has no flags to read; that is a damaged file, not an encrypted one."""
        runner.result = None

        with pytest.raises(CorruptedDocumentError):
            convert(_doc(0x0100, size=4), "a.doc")

        assert len(runner.calls) == 1

    def test_presentation_with_the_encrypted_header_token_is_reported_as_encrypted(self, runner):
        with pytest.raises(EncryptedDocumentError, match="password-protected"):
            convert(_ppt(0xF3D1C4DF), "a.ppt")

        assert runner.calls == []

    def test_presentation_with_an_encrypted_summary_stream_is_reported_as_encrypted(self, runner):
        with pytest.raises(EncryptedDocumentError):
            convert(_ppt(more={"EncryptedSummary": b"\x00" * 64}), "a.ppt")

    def test_presentation_with_the_ordinary_token_or_no_current_user_stream_is_not_encrypted(self, runner):
        runner.result = builder.pptx_bytes()

        assert convert(_ppt(), "a.ppt").format is DocumentFormat.PPT
        assert convert(_ppt(current_user=False), "a.ppt").format is DocumentFormat.PPT
        assert len(runner.calls) == 2

    def test_password_is_reported_even_without_libreoffice(self, monkeypatch):
        """The same upload gets the same answer on every machine: 'remove the password', not 'install LibreOffice'."""
        monkeypatch.setattr(legacy, "find_soffice", lambda: None)

        with pytest.raises(EncryptedDocumentError):
            convert(_doc(0x0100), "a.doc")
        with pytest.raises(EncryptedDocumentError):
            convert(_ppt(0xF3D1C4DF), "a.ppt")


class TestRunner:
    """How LibreOffice is started: the command, the private folder, the time limit, the clean-up."""

    @pytest.fixture
    def scratch(self, monkeypatch, tmp_path):
        """Temporary directories are created under this folder, so a test can see whether any was left behind."""
        folder = tmp_path / "scratch"
        folder.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(folder))
        return folder

    @pytest.fixture
    def process(self, monkeypatch):
        """The step that starts the process is replaced; `process.seen` records what it was given and what was on disk."""

        class Process:
            output: bytes | None = b"converted"
            error: BaseException | None = None

            def __init__(self) -> None:
                self.seen: list[dict] = []

            def __call__(self, command, working_folder):
                source = Path(command[-1])
                out_dir = Path(command[command.index("--outdir") + 1])
                profile = next(argument for argument in command if argument.startswith("-env:UserInstallation="))
                settings = Path(working_folder) / "profile" / "user" / "registrymodifications.xcu"
                self.seen.append({"command": command, "folder": Path(working_folder), "source": source, "input": source.read_bytes(),
                                  "profile": profile, "settings": settings.read_text(encoding="utf-8")})
                if self.error:
                    raise self.error
                if self.output is not None:
                    (out_dir / "input.docx").write_bytes(self.output)

        fake = Process()
        monkeypatch.setattr(legacy, "_run", fake)
        return fake

    def test_command_is_an_argument_list_with_fixed_options_and_private_paths(self, process, scratch):
        """Nothing in the command comes from the document or its name: the program, fixed options, and paths inside the
        temporary directory. The input has a neutral name and the profile is inside the same directory."""
        result = legacy._run_soffice(_FAKE_PROGRAM, b"document bytes", "doc", "docx", _WORD_FILTER, _NO_LIMIT)

        seen = process.seen[0]
        command, folder = seen["command"], seen["folder"]
        assert result == b"converted"
        assert isinstance(command, list) and all(isinstance(argument, str) for argument in command)
        assert command[0] == _FAKE_PROGRAM
        for option in ("--headless", "--norestore", "--nolockcheck"):
            assert option in command
        assert command[command.index("--convert-to") + 1] == "docx:MS Word 2007 XML"
        assert folder.parent == scratch.resolve()
        assert seen["source"] == folder / "input.doc"
        assert seen["input"] == b"document bytes"
        assert Path(command[command.index("--outdir") + 1]).parent == folder
        assert seen["profile"] == "-env:UserInstallation=" + (folder / "profile").as_uri()
        assert seen["profile"].startswith("-env:UserInstallation=file:///")

    def test_profile_disables_macros_link_updates_and_network_access(self, process, scratch):
        legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT)

        settings = process.seen[0]["settings"]
        for name, value in (("DisableMacrosExecution", "true"), ("MacroSecurityLevel", "3"), ("BlockUntrustedRefererLinks", "true"),
                            ("Link", "0"), ("ooInetProxyType", "1"), ("ooInetHTTPProxyName", "127.0.0.1"),
                            ("ooInetHTTPSProxyName", "127.0.0.1"), ("ooInetNoProxy", "")):
            assert re.search(rf'oor:name="{name}" oor:op="fuse"><value>{re.escape(value)}</value>', settings), name

    def test_each_run_has_its_own_folder_and_profile(self, process, scratch):
        """Two conversions at once (and a desktop LibreOffice of the same user) must not share a profile: the second
        instance would hand its file to the first and exit without output."""
        legacy._run_soffice(_FAKE_PROGRAM, b"one", "doc", "docx", _WORD_FILTER, _NO_LIMIT)
        legacy._run_soffice(_FAKE_PROGRAM, b"two", "doc", "docx", _WORD_FILTER, _NO_LIMIT)

        first, second = process.seen
        assert first["folder"] != second["folder"]
        assert first["profile"] != second["profile"]

    def test_temporary_folder_is_removed_after_success_no_output_timeout_and_failure(self, process, scratch):
        legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT)
        assert list(scratch.iterdir()) == []

        process.output = None
        assert legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT) is None
        assert list(scratch.iterdir()) == []

        for error in (legacy.ConversionTimeout(), FileNotFoundError(2, "No such file"), KeyboardInterrupt()):
            process.error = error
            with pytest.raises(type(error)):
                legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT)
            assert list(scratch.iterdir()) == []

    def test_empty_output_file_counts_as_no_output(self, process, scratch):
        process.output = b""

        assert legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT) is None

    def test_output_larger_than_the_limit_is_not_read_into_memory(self, process, scratch):
        process.output = b"\x00" * 2000

        with pytest.raises(DocumentTooLargeError):
            legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, 1000)
        assert list(scratch.iterdir()) == []

    def test_time_limit_is_finite_and_below_the_isolated_converter_default(self):
        """`IsolatedConverter` kills its worker outright at its own limit; LibreOffice started by that worker would then run
        on with nobody to stop it. The bridge's limit comes first, so its own clean-up normally runs."""
        assert 0 < legacy.CONVERSION_TIMEOUT < _DEFAULT_TIMEOUT

    def test_missing_program_is_reported_and_the_cached_lookup_is_dropped(self, monkeypatch, scratch):
        """A real start of a program that does not exist: the OSError becomes the fixed message, and the next call searches
        again instead of trusting the stale path."""
        cleared = []
        monkeypatch.setattr(legacy, "find_soffice", lambda: str(scratch / "missing" / "soffice"))
        monkeypatch.setattr(legacy, "reset_soffice_cache", lambda: cleared.append(True))

        with pytest.raises(UnsupportedFormatError, match="could not be started"):
            convert(_doc(), "a.doc")

        assert cleared == [True]
        assert list(scratch.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="the stand-in launcher is a shell script")
    def test_launcher_output_is_discarded_and_the_written_file_is_returned(self, scratch, tmp_path):
        """A stand-in launcher that prints to both streams and writes the output file where `--outdir` says: the real
        process path (Popen, wait, clean-up) without LibreOffice."""
        launcher = _launcher(tmp_path, 'echo "noise 보고서"; echo "error text" >&2; printf converted > "$out/input.docx"')

        assert legacy._run_soffice(launcher, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT) == b"converted"
        assert list(scratch.iterdir()) == []

    @pytest.mark.timing
    @pytest.mark.skipif(os.name != "posix", reason="process groups are the POSIX mechanism; the stand-in launcher is a shell script")
    def test_timeout_kills_the_whole_process_group(self, monkeypatch, scratch, tmp_path):
        """`soffice` is a launcher and the real program its child. The stand-in starts a long-running child and waits for it;
        when the time limit passes, both must be gone and the conversion reported as too large."""
        pid_file = tmp_path / "child.pid"
        launcher = _launcher(tmp_path, f'sleep 60 & echo $! > "{pid_file}"; wait')
        monkeypatch.setattr(legacy, "CONVERSION_TIMEOUT", 1.0)
        monkeypatch.setattr(legacy, "find_soffice", lambda: launcher)

        started = time.monotonic()
        with pytest.raises(DocumentTooLargeError, match="took too long"):
            convert(_doc(), "a.doc")

        assert time.monotonic() - started < 15
        child = int(pid_file.read_text())
        assert _wait_until_gone(child), "the launcher's child survived the timeout"
        assert list(scratch.iterdir()) == []

    @pytest.mark.timing
    @pytest.mark.skipif(os.name != "posix", reason="the guard process is the POSIX mechanism; the stand-in launcher is a shell script")
    def test_killing_the_converting_process_leaves_no_launcher_child_and_no_folder(self, scratch, tmp_path):
        """`IsolatedConverter` ends a worker with SIGKILL (time limit, cancelled request, shutdown), so no `finally` of the
        worker runs. A conversion is started in a separate Python process and that process is killed while the stand-in
        launcher's child is still running: the child must be gone and the folder holding the uploaded file removed."""
        pid_file = tmp_path / "child.pid"
        launcher = _launcher(tmp_path, f'sleep 60 & echo $! > "{pid_file}"; wait')
        script = ("import sys, tempfile\n"
                  "from opendockit import convert\n"
                  "from opendockit.extractors import legacy\n"
                  "tempfile.tempdir = sys.argv[1]\n"
                  "legacy.find_soffice = lambda: sys.argv[2]\n"
                  "convert(open(sys.argv[3], 'rb').read(), 'a.doc')\n")
        document = tmp_path / "a.doc"
        document.write_bytes(_doc())
        worker = subprocess.Popen([sys.executable, "-c", script, str(scratch), launcher, str(document)])
        try:
            deadline = time.monotonic() + 30
            while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pid_file.read_text())
            assert list(scratch.iterdir()) != []
        finally:
            worker.kill()
            worker.wait()

        assert _wait_until_gone(child), "the launcher's child survived the death of the converting process"
        deadline = time.monotonic() + 5
        while list(scratch.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list(scratch.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="the guard process is the POSIX mechanism")
    def test_guard_is_started_only_with_a_python_interpreter(self, monkeypatch, tmp_path):
        """In a frozen application or an embedding host (uWSGI) `sys.executable` is the application, not Python: running it
        with `-c` would start a second copy of the application for every conversion. The guard is left out there."""
        monkeypatch.setattr(sys, "executable", "/usr/local/bin/uwsgi")
        assert legacy._start_guard(tmp_path) is None
        monkeypatch.undo()
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert legacy._start_guard(tmp_path) is None
        monkeypatch.undo()
        monkeypatch.setattr(sys, "executable", "")
        assert legacy._start_guard(tmp_path) is None
        monkeypatch.undo()
        guard = legacy._start_guard(tmp_path)
        assert guard is not None
        legacy._stop_guard(guard)
        assert guard.poll() is not None and tmp_path.exists()   # stopped by us: it removed nothing

    @pytest.mark.timing
    @pytest.mark.skipif(os.name != "posix", reason="the guard process is the POSIX mechanism; the stand-in launcher is a shell script")
    def test_guard_does_not_wait_for_a_process_forked_during_the_conversion(self, scratch, tmp_path):
        """The converting process forks while a conversion runs (a pre-fork server, fork-start multiprocessing), so the fork
        holds a copy of the guard's pipe and the pipe does not close when the converting process is killed. The guard also
        watches for a new parent: the launcher's child and the folder are gone while the fork is still alive."""
        pid_file = tmp_path / "child.pid"
        launcher = _launcher(tmp_path, f'sleep 60 & echo $! > "{pid_file}"; wait')
        script = ("import os, sys, tempfile, threading, time\n"
                  "from opendockit import convert\n"
                  "from opendockit.extractors import legacy\n"
                  "tempfile.tempdir = sys.argv[1]\n"
                  "legacy.find_soffice = lambda: sys.argv[2]\n"
                  "data = open(sys.argv[3], 'rb').read()\n"
                  "threading.Thread(target=convert, args=(data, 'a.doc'), daemon=True).start()\n"
                  "while not os.path.exists(sys.argv[4]): time.sleep(0.05)\n"
                  "if os.fork() == 0:\n"
                  "    time.sleep(20)\n"
                  "    os._exit(0)\n"
                  "open(sys.argv[4] + '.forked', 'w').close()\n"
                  "time.sleep(60)\n")
        document = tmp_path / "a.doc"
        document.write_bytes(_doc())
        worker = subprocess.Popen([sys.executable, "-c", script, str(scratch), launcher, str(document), str(pid_file)])
        try:
            deadline = time.monotonic() + 30
            while not Path(f"{pid_file}.forked").exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pid_file.read_text())
        finally:
            worker.kill()
            worker.wait()

        assert _wait_until_gone(child), "the launcher's child outlived the converting process"
        deadline = time.monotonic() + 5
        while list(scratch.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list(scratch.iterdir()) == []


def _launcher(folder: Path, body: str) -> str:
    """A shell script standing in for `soffice`: `$out` is the value of `--outdir`."""
    script = folder / "soffice"
    script.write_text('#!/bin/sh\nout=""\nprev=""\nfor a in "$@"; do [ "$prev" = "--outdir" ] && out="$a"; prev="$a"; done\n' + body + "\n",
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def _wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        time.sleep(0.05)
    return False


# ---- the real program -------------------------------------------------------------------------------------------------------

_SOFFICE = legacy.find_soffice()
real_libreoffice = pytest.mark.skipif(_SOFFICE is None, reason="LibreOffice is not installed")


def _to_legacy(data: bytes, source: str, target: str, output_filter: str) -> bytes:
    converted = legacy._run_soffice(_SOFFICE, data, source, target, output_filter, _NO_LIMIT)
    assert converted, "LibreOffice did not write the legacy file"
    return converted


@pytest.fixture(scope="session")
def real_doc() -> bytes:
    """`builder.docx_bytes()` saved as Word 97-2003 by LibreOffice."""
    return _to_legacy(builder.docx_bytes(), "docx", "doc", "MS Word 97")


@pytest.fixture(scope="session")
def real_ppt() -> bytes:
    """`builder.pptx_bytes()` saved as PowerPoint 97-2003 by LibreOffice."""
    return _to_legacy(builder.pptx_bytes(), "pptx", "ppt", "MS PowerPoint 97")


def _docx_with_linked_picture(url: str) -> bytes:
    """A DOCX whose first picture is not embedded but linked: the relationship points at `url` (TargetMode External) and the
    blip uses `r:link`."""
    source = zipfile.ZipFile(BytesIO(builder.docx_with_images()))
    relationships = source.read("word/_rels/document.xml.rels").decode()
    relationship = re.search(r'<Relationship [^>]*Target="media/[^"]+"[^>]*/>', relationships).group(0)
    linked = re.sub(r'Target="media/[^"]+"', f'Target="{url}" TargetMode="External"', relationship)
    identifier = re.search(r'Id="([^"]+)"', relationship).group(1)
    replaced = {
        "word/_rels/document.xml.rels": relationships.replace(relationship, linked),
        "word/document.xml": source.read("word/document.xml").decode().replace(f'r:embed="{identifier}"', f'r:link="{identifier}"', 1),
    }
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            target.writestr(name, replaced[name].encode() if name in replaced else source.read(name))
    return buffer.getvalue()


@real_libreoffice
@pytest.mark.timing
class TestRealLibreOffice:
    """End to end with the installed LibreOffice. The .doc / .ppt inputs are made by LibreOffice itself from the in-memory
    DOCX / PPTX of `tests/builder.py`."""

    def test_word_document_keeps_headings_lists_table_and_korean_text(self, real_doc):
        result = convert(real_doc, "분기 보고서.doc")

        assert result.format is DocumentFormat.DOC
        assert result.warnings == [legacy.CONVERTED_WARNING]
        assert result.metadata["title"] == "분기 보고서"
        text = result.text
        assert text.startswith("# 1장 개요\n\n본문 첫 문단입니다.\n\n## 1.1 세부")
        assert "- 사과\n- 배" in text
        assert "1. 첫째\n2. 둘째" in text
        assert "| 분류 | 항목 | 값 |\n| --- | --- | --- |\n| 과일 | 사과 | 10 |\n| 과일 | 배 | 20 |" in text
        assert "끝 | 파이프" in text

    def test_presentation_keeps_slides_titles_levels_notes_and_table(self, real_ppt):
        result = convert(real_ppt, "사업 계획.ppt")

        assert result.format is DocumentFormat.PPT
        assert result.warnings == [legacy.CONVERTED_WARNING]
        assert [section.title for section in result.sections] == ["사업 계획", "핵심 목표", "실적"]
        text = result.text
        assert "## Slide 1: 사업 계획\n\n2026년 상반기" in text
        assert "- 매출 성장\n  - 신규 고객 확보" in text
        assert "> Speaker notes: 여기서 수치를 강조" in text
        assert "| 지역 | 매출 |\n| --- | --- |\n| 서울 | 100 |" in text
        assert "| 1분기 | 10.5 |" in text

    def test_container_without_a_readable_document_is_corrupted_not_empty_success(self):
        """A `WordDocument` stream of zero bytes passes detection. LibreOffice writes nothing for it, and that must not
        become an empty successful result."""
        with pytest.raises(CorruptedDocumentError, match="LibreOffice could not convert"):
            convert(_doc(), "a.doc")

    def test_conversions_at_the_same_time_do_not_collide(self, real_doc, real_ppt):
        """With a shared profile the second LibreOffice hands its file to the first one and writes nothing. Each call has its
        own profile, so four at once all succeed."""
        jobs = [(real_doc, "a.doc"), (real_ppt, "b.ppt"), (real_doc, "c.doc"), (real_ppt, "d.ppt")]

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda job: convert(*job), jobs))

        assert [result.format for result in results] == [DocumentFormat.DOC, DocumentFormat.PPT, DocumentFormat.DOC, DocumentFormat.PPT]
        assert all("과일" in result.text or "서울" in result.text for result in results)

    def test_linked_picture_is_not_fetched_from_the_network(self):
        """A .doc whose picture is a link to a URL. With LibreOffice's default settings, converting it sends requests to that
        address — an address the uploader chose, reached from inside the server's network. The private profile blocks it: no
        request of any kind arrives, and the document is still converted."""
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def _answer(self):
                requests.append(f"{self.command} {self.path}")
                body = builder.png_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_HEAD = do_OPTIONS = do_PROPFIND = _answer

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/linked.png"
            # Making the .doc is done with a bare profile (the picture is fetched here, which also proves the server answers).
            bare = '<?xml version="1.0" encoding="UTF-8"?>\n<oor:items xmlns:oor="http://openoffice.org/2001/registry"/>\n'
            hardened, legacy._PROFILE_SETTINGS = legacy._PROFILE_SETTINGS, bare
            try:
                doc = _to_legacy(_docx_with_linked_picture(url), "docx", "doc", "MS Word 97")
            finally:
                legacy._PROFILE_SETTINGS = hardened
            assert url.encode() in doc or url.encode("utf-16-le") in doc, "the link did not survive into the .doc"
            assert requests, "the test server was never reached while building the input"
            requests.clear()

            result = convert(doc, "a.doc")
        finally:
            server.shutdown()
            server.server_close()

        assert requests == []
        assert "차트 앞 문단입니다." in result.text
