"""Legacy Word (.doc) and PowerPoint (.ppt), converted through an installed LibreOffice (docs/decisions/legacy-office.md).

The unit tests replace the lookup and the runner, so they pass on a machine without LibreOffice and never start it. The
tests in `TestRealLibreOffice` start the real program; they are skipped when it is not installed and are marked `timing`
because one start takes seconds and should not compete with the parallel group.
"""

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import zipfile

import pytest

from opendockit import (
    ConversionError,
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
from tests import CONSOLE_PROBE, builder, run_without_a_console, wait_until_gone


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
            # What the Windows registry says: the `program` folders the installer recorded, and the Program Files folders.
            installed: list[str] = []
            program_folders: list[str] = []

        machine = Machine()

        def which(name):
            machine.searches += 1
            return machine.on_path.get(name)

        monkeypatch.setattr(legacy.shutil, "which", which)
        monkeypatch.setattr(legacy.os.path, "isfile", lambda path: path in machine.files)
        monkeypatch.setattr(legacy, "_windows_registry_folders", lambda: (machine.installed, machine.program_folders))
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

    def test_windows_install_is_found_without_the_program_files_variables(self, machine):
        """An MCP client starts its server with a short list of variables, and the official SDK's has no PROGRAMFILES: a
        LibreOffice installed in the default place was "not installed" for the MCP server alone. Windows records the folders
        in the registry as well, and the registry does not depend on the environment."""
        expected = os.path.join("C:\\Program Files", "LibreOffice", "program", "soffice.exe")
        machine.files = {expected}
        machine.program_folders = ["C:\\Program Files", "C:\\Program Files (x86)"]

        assert legacy.find_soffice() == expected

    def test_windows_install_outside_program_files_is_found_through_the_installer_record(self, machine):
        """The installer records its own `program` folder, so an install chosen elsewhere (another drive) is found too."""
        expected = os.path.join("D:\\Apps\\LibreOffice\\program", "soffice.exe")
        machine.files = {expected}
        machine.installed = ["D:\\Apps\\LibreOffice\\program"]
        machine.program_folders = ["C:\\Program Files"]

        assert legacy.find_soffice() == expected

    @pytest.mark.skipif(os.name != "nt", reason="the registry is Windows")
    def test_the_registry_is_read_for_the_program_files_folders(self):
        """The reader itself, on the real registry: every Windows records its Program Files folder."""
        _, folders = legacy._windows_registry_folders()

        assert folders, "no Program Files folder was read from the registry"
        assert all(os.path.isdir(folder) for folder in folders)

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
        runner.result = legacy.ProgramNotStarted()
        runner.result.__cause__ = PermissionError(13, "Permission denied: '/secret/path/soffice'")

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

    def test_stream_that_loops_on_itself_is_read_no_further_than_the_file(self, runner):
        """olefile reads a stream whole, as many sectors as its entry declares, following a chain that may point at itself: a
        5.5 KB `.doc` declaring a 2 GB `WordDocument` made 2.5 GB in the encryption check. It is read no further than the file."""
        fib = b"\xec\xa5\xc1\x00" + b"\x00" * 6 + (0x0100).to_bytes(2, "little")
        data = builder.ole_file({"WordDocument": fib.ljust(4096, b"\x00")}, loops=("WordDocument",), sizes={"WordDocument": 200_000_000})

        tracemalloc.start()
        try:
            with pytest.raises(EncryptedDocumentError):
                convert(data, "a.doc")
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()

        assert peak < 20_000_000
        assert runner.calls == []

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

    @pytest.mark.parametrize("failing", ["folder", "input", "output"])
    def test_temporary_folder_that_cannot_be_written_is_not_a_program_that_cannot_be_started(self, monkeypatch, scratch, failing):
        """A full disk or a read-only temporary folder (making the folder, writing the input, reading the output) was reported
        as 'LibreOffice could not be started' and dropped the cached lookup of a program that was fine. It is a conversion
        failure of its own, with a fixed message, and the lookup stays."""
        cleared, started = [], []
        monkeypatch.setattr(legacy, "find_soffice", lambda: _FAKE_PROGRAM)
        monkeypatch.setattr(legacy, "reset_soffice_cache", lambda: cleared.append(True))

        def run(command, working_folder):
            started.append(command)
            (Path(command[command.index("--outdir") + 1]) / "input.docx").write_bytes(b"PK")   # written, but not readable below

        def no_space(*args, **kwargs):
            raise OSError(28, "No space left on device: '/secret/tmp'")

        monkeypatch.setattr(legacy, "_run", run)
        if failing == "folder":
            monkeypatch.setattr(legacy.tempfile, "mkdtemp", no_space)
        elif failing == "input":
            monkeypatch.setattr(Path, "write_bytes", no_space)
        else:
            # A read that fails (a directory in place of the file has size 0 on Windows and reads as 'no output' there)
            monkeypatch.setattr(Path, "read_bytes", no_space)

        with pytest.raises(ConversionError) as caught:
            convert(_doc(), "a.doc")

        assert type(caught.value) is ConversionError
        assert str(caught.value) == legacy._NO_SCRATCH
        assert "secret" not in str(caught.value)
        assert isinstance(caught.value.__cause__, OSError)
        assert cleared == []
        assert len(started) == (1 if failing == "output" else 0)
        assert list(scratch.iterdir()) == []

    def test_only_a_failed_start_of_the_program_counts_as_not_started(self, monkeypatch, scratch):
        def refused(*args, **kwargs):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(legacy.subprocess, "Popen", refused)

        with pytest.raises(legacy.ProgramNotStarted) as caught:
            legacy._run_soffice(_FAKE_PROGRAM, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT)

        assert isinstance(caught.value.__cause__, PermissionError)
        assert list(scratch.iterdir()) == []

    @pytest.mark.skipif(os.name != "posix", reason="the stand-in launcher is a shell script")
    def test_launcher_output_is_discarded_and_the_written_file_is_returned(self, scratch, tmp_path):
        """A stand-in launcher that prints to both streams and writes the output file where `--outdir` says: the real
        process path (Popen, wait, clean-up) without LibreOffice."""
        launcher = _launcher(tmp_path, 'echo "noise 보고서"; echo "error text" >&2; printf converted > "$out/input.docx"')

        assert legacy._run_soffice(launcher, b"x", "doc", "docx", _WORD_FILTER, _NO_LIMIT) == b"converted"
        assert list(scratch.iterdir()) == []

    @pytest.mark.timing
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
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
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
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

    @pytest.mark.timing
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
    @pytest.mark.skipif(os.name != "nt", reason="the job object is the Windows mechanism; the stand-in launcher is a .cmd")
    def test_killing_the_converting_process_leaves_no_launcher_child_and_no_folder_windows(self, scratch, tmp_path):
        """The same promise the guard process keeps on POSIX, kept by a job object here.

        `IsolatedConverter` ends a worker outright on its time limit, on a cancelled request and on shutdown, so no `finally`
        of the worker runs and `taskkill /T` is never reached. The launcher is put into a job whose handle the converting
        process holds, so the kernel ends the launcher and its child when that process dies. The temporary folder is not
        removed — there is no guard process to do it (docs/decisions/legacy-office.md).
        """
        pid_file = tmp_path / "child.pid"
        launcher, paths = _windows_launcher(tmp_path, pid_file)
        script = ("import sys, tempfile\n"
                  "from opendockit import convert\n"
                  "from opendockit.extractors import legacy\n"
                  "tempfile.tempdir = sys.argv[1]\n"
                  "legacy.find_soffice = lambda: sys.argv[2]\n"
                  "convert(open(sys.argv[3], 'rb').read(), 'a.doc')\n")
        document = tmp_path / "a.doc"
        document.write_bytes(_doc())
        worker = subprocess.Popen([sys.executable, "-c", script, str(scratch), launcher, str(document)], env={**os.environ, **paths})
        try:
            deadline = time.monotonic() + 30
            while not (pid_file.exists() and pid_file.read_text().strip()) and time.monotonic() < deadline:
                time.sleep(0.05)
            child = int(pid_file.read_text())
        finally:
            worker.kill()
            worker.wait()

        assert wait_until_gone(child), "the launcher's child survived the death of the converting process"
        deadline = time.monotonic() + 10   # the guard removes it once it sees this process end
        while list(scratch.iterdir()) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert list(scratch.iterdir()) == [], "the folder holding the uploaded document stayed on disk"

    @pytest.mark.timing
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
    @pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to remove a folder that is a process's current directory")
    def test_the_windows_guard_removes_a_folder_still_held_as_a_current_directory(self, tmp_path):
        """Windows refuses to remove a folder that a running process has as its current directory, and LibreOffice is started
        with the working folder as its own. The job object ends it only after the converting process has gone, so the guard
        has to try again rather than once: a single attempt removes nothing, and `ignore_errors` hides that it removed
        nothing."""
        folder = tmp_path / "work"
        folder.mkdir()
        (folder / "uploaded.doc").write_bytes(_doc())
        # A process that keeps the folder as its current directory, as LibreOffice does, and outlives the guard's wait.
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], cwd=folder)
        # The guard watches whoever started it, so it is started from a process that then ends — this one never does.
        starter = ("import sys\n"
                   "from opendockit.extractors import legacy\n"
                   "legacy._start_windows_guard(sys.argv[1])\n")
        try:
            subprocess.run([sys.executable, "-c", starter, str(folder)], check=True, timeout=60)
            deadline = time.monotonic() + 20
            while folder.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert folder.exists(), "the folder cannot go while a process still has it as its current directory"
            holder.kill()   # as the job object ends LibreOffice, after the guard's wait is already over
            holder.wait()

            deadline = time.monotonic() + 20
            while folder.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            holder.kill()

        assert not folder.exists(), "the guard gave up after one attempt while the folder was still held"

    @pytest.mark.skipif(os.name != "nt", reason="the guard that removes the folder is started only on Windows here")
    def test_the_windows_guard_leaves_the_folder_when_it_cannot_watch_the_converting_process(self, tmp_path):
        """A process that cannot be opened is gone only when Windows says there is no such process. Refused for any other
        reason (another integrity level, a restricted token), the guard removed the folder at once — taking the document
        away from a conversion still running. The refusal is made certain by running the guard with an `OpenProcess` that
        fails with "access denied" (5): which real process an account may open differs between machines (an elevated CI
        runner may open more)."""
        kept = tmp_path / "kept"
        kept.mkdir()
        (kept / "uploaded.doc").write_bytes(_doc())
        removed = tmp_path / "removed"
        removed.mkdir()
        (removed / "uploaded.doc").write_bytes(_doc())
        refused = ("import ctypes, sys\n"
                   "real = ctypes.WinDLL\n"
                   "class Refusing:\n"
                   "    restype = argtypes = None\n"
                   "    def __call__(self, *arguments):\n"
                   "        ctypes.set_last_error(5)\n"
                   "        return None\n"
                   "class Kernel:\n"
                   "    def __init__(self, *arguments, **options):\n"
                   "        self.__dict__['real'] = real(*arguments, **options)\n"
                   "        self.__dict__['OpenProcess'] = Refusing()\n"
                   "    def __getattr__(self, name):\n"
                   "        return getattr(self.real, name)\n"
                   "ctypes.WinDLL = Kernel\n"
                   "sys.argv = sys.argv[1:]\n"
                   f"exec({legacy._WINDOWS_GUARD!r})\n")

        subprocess.run([sys.executable, "-I", "-S", "-c", refused, "guard", legacy._windows.long_path(str(kept)), str(os.getpid()),
                        "5"], check=True, timeout=60)
        # And a process that does not exist: that one is gone, and its folder is removed.
        subprocess.run([sys.executable, "-I", "-S", "-c", legacy._WINDOWS_GUARD, legacy._windows.long_path(str(removed)),
                        str(4_000_000_000), "5"], check=True, timeout=60)

        assert (kept / "uploaded.doc").exists(), "the guard removed the folder of a process it could not watch"
        assert not removed.exists(), "the guard kept the folder of a process that is gone"

    @pytest.mark.skipif(os.name != "nt", reason="the guard that removes the folder is started only on Windows here")
    def test_the_windows_guard_is_started_only_with_a_python_interpreter(self, monkeypatch, tmp_path):
        """As for the POSIX guard: in a frozen application or an embedding host `sys.executable` is the application, not
        Python, and running it with `-c` would start a second copy of it for every conversion."""
        monkeypatch.setattr(sys, "executable", r"C:\Program Files\MyApp\MyApp.exe")

        assert legacy._start_windows_guard(tmp_path) is None

    @pytest.mark.skipif(os.name != "nt", reason="a console window is Windows'")
    def test_a_console_launcher_opens_no_window_from_a_process_without_a_console(self, tmp_path):
        """A process without a console (a desktop program under pythonw, one started detached) gives every console program
        it starts a console of its own, with a window. With LibreOffice's folder on PATH, `soffice` is its `soffice.com`,
        a console program, and every .doc or .ppt opened a window on the user's desktop while it converted. The launcher
        is started with CREATE_NO_WINDOW: a console program gets a console without a window, and a program with windows of
        its own, as `soffice.exe` is, is not changed by it. The stand-in is a `.cmd`, a console program as `soffice.com`
        is; its paths reach it through the environment, as those of `_windows_launcher` do."""
        launcher = tmp_path / "soffice.cmd"
        launcher.write_bytes(b'@echo off\r\n"%OPENDOCKIT_TEST_PYTHON%" -I -S "%OPENDOCKIT_TEST_CHILD%" "%OPENDOCKIT_TEST_RECORD%"\r\n')
        child = tmp_path / "child.py"
        child.write_text(CONSOLE_PROBE + "import json, sys\njson.dump(console_state(), open(sys.argv[1], 'w', encoding='utf-8'))\n",
                         encoding="utf-8")
        script = tmp_path / "convert.py"
        script.write_text(CONSOLE_PROBE + (
            "import json, os, sys\n"
            "from opendockit import ConversionError, convert\n"
            "os.environ.update(OPENDOCKIT_SOFFICE=sys.argv[1], OPENDOCKIT_TEST_PYTHON=sys.executable,\n"
            "                  OPENDOCKIT_TEST_CHILD=sys.argv[2], OPENDOCKIT_TEST_RECORD=sys.argv[3])\n"
            "state = console_state()\n"
            "try:\n"
            "    convert(open(sys.argv[4], 'rb').read(), 'a.doc')\n"
            "except ConversionError:\n"
            "    pass   # the stand-in writes no document\n"
            "json.dump(state, open(sys.argv[5], 'w', encoding='utf-8'))\n"), encoding="utf-8")
        document = tmp_path / "a.doc"
        document.write_bytes(_doc())
        record, report = tmp_path / "launcher.json", tmp_path / "converting.json"

        ran = run_without_a_console([str(script), str(launcher), str(child), str(record), str(document), str(report)], timeout=120)

        assert ran.returncode == 0, "the converting script failed"
        assert json.loads(report.read_text(encoding="utf-8")) == {"console": False, "window": False}, "the converting process had a console"
        assert json.loads(record.read_text(encoding="utf-8")) == {"console": True, "window": False}

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
    @pytest.mark.no_rerun  # starts processes and checks how they end: one failure can be a real race
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


def _windows_launcher(folder: Path, pid_file: Path) -> tuple[str, dict[str, str]]:
    """A `.cmd` standing in for `soffice.exe`, which starts a long-lived child and waits for it, as the real launcher starts
    `soffice.bin`. The child writes its pid, so a test can ask whether it outlived the conversion.

    The paths it runs reach it as environment variables, returned with it for the process that starts it, and are not
    written into it. cmd.exe reads a batch file in the console's code page, so a path outside ASCII in its text names no
    file there: with the interpreter under a folder of a Korean name and code page 949, the default of Korean Windows, the
    child never started and the test failed after 30 s. A variable is expanded from the environment, which holds the
    path as it is. The file itself is ASCII, written byte for byte."""
    child = folder / "child.py"
    child.write_text("import os, sys, time\n"
                     "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
                     "time.sleep(60)\n", encoding="utf-8")
    script = folder / "soffice.cmd"
    script.write_bytes(b'@echo off\r\n"%OPENDOCKIT_TEST_PYTHON%" "%OPENDOCKIT_TEST_CHILD%" "%OPENDOCKIT_TEST_PID_FILE%"\r\n')
    return str(script), {"OPENDOCKIT_TEST_PYTHON": sys.executable, "OPENDOCKIT_TEST_CHILD": str(child),
                         "OPENDOCKIT_TEST_PID_FILE": str(pid_file)}


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
@pytest.mark.no_rerun  # starts LibreOffice, several at once: one failure can be a real collision
class TestRealLibreOffice:
    """End to end with the installed LibreOffice. The .doc / .ppt inputs are made by LibreOffice itself from the in-memory
    DOCX / PPTX of `tests/builder.py`."""

    def test_found_with_the_environment_an_mcp_client_passes(self):
        """The MCP server is started by its client with only the variables the client chooses to pass. The official SDK's
        list has no PROGRAMFILES on Windows, and there the installed LibreOffice was not found: every .doc and .ppt the MCP
        server was asked for failed with "need LibreOffice" while the same call in a terminal worked. The lookup is run in
        a process started with exactly that environment."""
        stdio = pytest.importorskip("mcp.client.stdio")
        environment = stdio.get_default_environment()
        environment.pop(legacy.SOFFICE_ENVIRONMENT_VARIABLE, None)
        lookup = "from opendockit.extractors import legacy; print(legacy.find_soffice() or '')"

        found = subprocess.run([sys.executable, "-c", lookup], env=environment, capture_output=True, text=True,
                               timeout=60, check=True).stdout.strip()

        assert found, "LibreOffice was not found with the environment an MCP client gives its server"
        assert os.path.isfile(found)

    def test_no_folder_is_left_under_a_long_temporary_path(self, real_doc, tmp_path, monkeypatch):
        """LibreOffice writes paths of 140 characters and more inside its profile, through the long path form. Under a
        temporary folder of a hundred characters they passed Windows' 260-character limit, the ordinary removal failed on
        them, and every conversion left its folder behind."""
        # 114 characters, where it was seen: short enough for the folders this package makes, too long for LibreOffice's.
        deep = tmp_path / ("d" * max(1, 114 - len(str(tmp_path)) - 1))
        deep.mkdir()
        monkeypatch.setattr(tempfile, "tempdir", str(deep))

        result = convert(real_doc, "a.doc")

        assert "본문 첫 문단입니다." in result.text
        assert len(str(deep)) >= 114
        assert list(deep.iterdir()) == [], "the conversion's folder stayed on disk"

    def test_no_folder_is_left_after_the_time_limit(self, real_doc, tmp_path, monkeypatch):
        """Ending LibreOffice on its time limit is asynchronous on Windows: `soffice.bin` was still going down, holding its
        files, when the folder was removed, so the removal failed and the folder stayed. It is cut off at several points
        of its run, since where it is when the limit passes differs from machine to machine."""
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        for limit in (0.3, 0.8, 1.5, 2.5):
            monkeypatch.setattr(legacy, "CONVERSION_TIMEOUT", limit)
            try:
                convert(real_doc, "a.doc")
            except DocumentTooLargeError:
                pass

            assert list(tmp_path.iterdir()) == [], f"a folder stayed after a time limit of {limit} s"

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

    @pytest.mark.skipif(os.name == "nt",
                        reason="LibreOffice on Windows does not fetch a linked picture even with a bare profile, so the "
                               "baseline this test needs cannot be established there")
    def test_linked_picture_is_not_fetched_from_the_network(self):
        """A .doc whose picture is a link to a URL. With LibreOffice's default settings, converting it sends requests to that
        address — an address the uploader chose, reached from inside the server's network. The private profile blocks it: no
        request of any kind arrives, and the document is still converted.

        The requests of the default settings are counted on the conversion `convert` runs (the .doc read, DOCX written), with
        a bare profile. They were counted while the .doc was made from a DOCX, until LibreOffice 26.2.7 stopped fetching the
        linked picture of a DOCX: nothing arrived there, while reading the .doc still sent OPTIONS and HEAD."""
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
            bare = '<?xml version="1.0" encoding="UTF-8"?>\n<oor:items xmlns:oor="http://openoffice.org/2001/registry"/>\n'
            hardened, legacy._PROFILE_SETTINGS = legacy._PROFILE_SETTINGS, bare
            try:
                doc = _to_legacy(_docx_with_linked_picture(url), "docx", "doc", "MS Word 97")
                assert url.encode() in doc or url.encode("utf-16-le") in doc, "the link did not survive into the .doc"
                requests.clear()  # whether making the .doc asks for the picture depends on the version: not what is measured
                # The conversion of `extract_doc`, with a bare profile: this is what the private profile has to stop
                assert legacy._run_soffice(_SOFFICE, doc, "doc", "docx", _WORD_FILTER, _NO_LIMIT)
            finally:
                legacy._PROFILE_SETTINGS = hardened
            assert any("/linked.png" in request for request in requests), (
                "LibreOffice with default settings sent no request for the linked picture of the .doc: the private profile is "
                "not shown to be what stops it")
            requests.clear()

            result = convert(doc, "a.doc")
        finally:
            server.shutdown()
            server.server_close()

        assert requests == []
        assert "차트 앞 문단입니다." in result.text
