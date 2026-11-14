"""Word and PowerPoint 97-2003 (.doc, .ppt), read through an installed LibreOffice.

These binary formats are not parsed in Python. When LibreOffice is installed, it is run headless to convert the file to
DOCX / PPTX inside a private temporary directory, and the result goes through the ordinary DOCX / PPTX extractors. When
it is not installed, the file is rejected with advice. Nothing else in the library depends on LibreOffice.
The decisions are in docs/decisions/legacy-office.md.
"""

from collections.abc import Callable
from functools import lru_cache
from io import BytesIO
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import zipfile

import olefile

from opendockit.exceptions import (
    CorruptedDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedFormatError,
)
from opendockit.extractors._package import uncompressed_size
from opendockit.extractors.docx import extract_docx
from opendockit.extractors.pptx import extract_pptx
from opendockit.models import ConvertOptions, Extraction


# Path of the LibreOffice program to use. An empty value switches the bridge off (no subprocess is ever started).
SOFFICE_ENVIRONMENT_VARIABLE = "OPENDOCKIT_SOFFICE"

# Time limit of one LibreOffice run, in seconds. A cold start with a fresh profile takes 1-5 seconds and an ordinary document
# well under a second more, so this is generous. It is kept below the default `IsolatedConverter` timeout (120 s): there the
# worker is killed outright when its own limit passes, which would leave LibreOffice running with nobody to stop it
# (Pinned by: test_legacy.py::TestRunner::test_time_limit_is_finite_and_below_the_isolated_converter_default).
CONVERSION_TIMEOUT = 60.0

_NOT_INSTALLED = ("Legacy {name} (.{extension}) documents need LibreOffice to be converted. Install LibreOffice "
                  "(https://www.libreoffice.org, or `brew install --cask libreoffice` / `apt install libreoffice`), "
                  "or save the file as {modern} and upload it again.")
_TIMED_OUT = "The document took too long to convert with LibreOffice. It is too large or too complex."
_NO_OUTPUT = "LibreOffice could not convert the document. The file may be corrupted."
_NOT_STARTED = "LibreOffice could not be started to convert the document."
_ENCRYPTED = "The document is password-protected. Please remove the password and upload it again."
_TOO_LARGE = "The document becomes too large when decompressed."
CONVERTED_WARNING = "The document was converted with LibreOffice before reading, so layout details may differ from the original."

_PROGRAM_NAMES = ("soffice", "libreoffice")
_MACOS_PROGRAM = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_LINUX_PROGRAMS = ("/usr/lib/libreoffice/program/soffice", "/usr/lib64/libreoffice/program/soffice", "/opt/libreoffice/program/soffice")
_WINDOWS_FOLDER_VARIABLES = ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")

# Neutral names inside the temporary directory. The user's file name never reaches a command line or the file system.
_INPUT_STEM = "input"
_PROFILE_FOLDER = "profile"
_OUTPUT_FOLDER = "out"

# Settings written into the private profile before LibreOffice starts: macros off at the highest level LibreOffice offers,
# links not updated, and every HTTP(S) request sent to a proxy that does not exist (the discard port of this machine). With
# the default profile a picture linked by URL was fetched while converting (a request to an address chosen by the document);
# with only the link settings LibreOffice still sent OPTIONS and HEAD requests to it
# (Pinned by: test_legacy.py::TestRealLibreOffice::test_linked_picture_is_not_fetched_from_the_network).
# See docs/decisions/legacy-office.md for what this does and does not guarantee.
_PROFILE_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema"\
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse">\
<value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" oor:op="fuse">\
<value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" oor:op="fuse">\
<value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="FirstRun" oor:op="fuse"><value>false</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetProxyType" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyName" oor:op="fuse"><value>127.0.0.1</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetHTTPSProxyPort" oor:op="fuse"><value>9</value></prop></item>
<item oor:path="/org.openoffice.Inet/Settings"><prop oor:name="ooInetNoProxy" oor:op="fuse"><value></value></prop></item>
</oor:items>
"""

# Word: the `fEncrypted` bit of the FIB — bit 8 of the 16-bit flags at offset 10 of the `WordDocument` stream ([MS-DOC] 2.5.2).
_FIB_FLAGS_OFFSET = 10
_FIB_ENCRYPTED = 0x0100
# PowerPoint: `headerToken` of the CurrentUserAtom, at offset 12 of the `Current User` stream ([MS-PPT] 2.3.2).
_CURRENT_USER_TOKEN_OFFSET = 12
_CURRENT_USER_ENCRYPTED_TOKEN = 0xF3D1C4DF


class ConversionTimeout(Exception):
    """LibreOffice did not finish within `CONVERSION_TIMEOUT`. Raised by the runner; never leaves this module."""


def extract_doc(data: bytes, options: ConvertOptions) -> Extraction:
    return _extract_through_libreoffice(data, options, name="Word", extension="doc", target="docx",
                                        output_filter="MS Word 2007 XML", encrypted=_is_encrypted_doc, extract=extract_docx)


def extract_ppt(data: bytes, options: ConvertOptions) -> Extraction:
    return _extract_through_libreoffice(data, options, name="PowerPoint", extension="ppt", target="pptx",
                                        output_filter="Impress MS PowerPoint 2007 XML", encrypted=_is_encrypted_ppt,
                                        extract=extract_pptx)


def _extract_through_libreoffice(data: bytes, options: ConvertOptions, *, name: str, extension: str, target: str, output_filter: str,
                                 encrypted: Callable[[bytes], bool],
                                 extract: Callable[[bytes, ConvertOptions], Extraction]) -> Extraction:
    # The password check comes first: it needs no LibreOffice, so the advice is the same on every machine, and LibreOffice
    # is never started on a file it could only fail on
    # (Pinned by: test_legacy.py::TestEncryption::test_password_is_reported_even_without_libreoffice).
    if encrypted(data):
        raise EncryptedDocumentError(_ENCRYPTED)

    program = find_soffice()
    if program is None:
        raise UnsupportedFormatError(_NOT_INSTALLED.format(name=name, extension=extension, modern=target.upper()))

    try:
        converted = _run_soffice(program, data, extension, target, output_filter, options.max_uncompressed_bytes)
    except ConversionTimeout as exc:
        raise DocumentTooLargeError(_TIMED_OUT) from exc
    except OSError as exc:
        # The program disappeared or may not be executed. The cached lookup is dropped so a repaired install is found again.
        reset_soffice_cache()
        raise UnsupportedFormatError(_NOT_STARTED) from exc
    if not converted:
        raise CorruptedDocumentError(_NO_OUTPUT)

    _guard_converted_zip(converted, options)
    extraction = extract(converted, options)
    extraction.warnings.append(CONVERTED_WARNING)
    return extraction


def find_soffice() -> str | None:
    """Path of the LibreOffice program, or None when there is none (or the bridge is switched off).

    `OPENDOCKIT_SOFFICE` wins when it is set: its value is the program to run, and nothing else is searched — a wrong path
    means "not available", not "fall back to whatever is installed"; an empty value switches the bridge off. The variable is
    read on every call, the search of the machine is cached (`reset_soffice_cache`).
    """
    override = os.environ.get(SOFFICE_ENVIRONMENT_VARIABLE)
    if override is not None:
        override = override.strip()
        return override if override and os.path.isfile(override) else None
    return _installed_soffice()


def reset_soffice_cache() -> None:
    """Forget where LibreOffice was found (after installing it in a running process; in tests)."""
    _installed_soffice.cache_clear()


@lru_cache(maxsize=1)
def _installed_soffice() -> str | None:
    for name in _PROGRAM_NAMES:
        found = shutil.which(name)
        if found:
            return found
    candidates = [_MACOS_PROGRAM, *_LINUX_PROGRAMS]
    for variable in _WINDOWS_FOLDER_VARIABLES:
        folder = os.environ.get(variable)
        if folder:
            candidates.append(os.path.join(folder, "LibreOffice", "program", "soffice.exe"))
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)


def _soffice_command(program: str, source: Path, profile: Path, out_dir: Path, target: str, output_filter: str) -> list[str]:
    """The argument list of one conversion. Every value is ours: fixed options, and paths inside the temporary directory."""
    return [
        program,
        f"-env:UserInstallation={profile.as_uri()}",  # private profile: no clash with other conversions or a desktop LibreOffice
        "--headless", "--invisible", "--norestore", "--nolockcheck", "--nodefault", "--nofirststartwizard", "--nologo",
        "--convert-to", f"{target}:{output_filter}",
        "--outdir", str(out_dir),
        str(source),
    ]


def _run_soffice(program: str, data: bytes, extension: str, target: str, output_filter: str, max_output_bytes: int) -> bytes | None:
    """Convert `data` with LibreOffice and return the converted file, or None when LibreOffice wrote none.

    Raises `ConversionTimeout` when the time limit passes and `OSError` when the program cannot be started. Everything happens
    inside one temporary directory that is removed on every path.
    """
    with tempfile.TemporaryDirectory(prefix="opendockit-", ignore_cleanup_errors=True) as folder:
        root = Path(folder).resolve()
        source = root / f"{_INPUT_STEM}.{extension}"
        source.write_bytes(data)
        profile = root / _PROFILE_FOLDER
        (profile / "user").mkdir(parents=True)
        (profile / "user" / "registrymodifications.xcu").write_text(_PROFILE_SETTINGS, encoding="utf-8")
        out_dir = root / _OUTPUT_FOLDER
        out_dir.mkdir()

        _run(_soffice_command(program, source, profile, out_dir, target, output_filter), root)

        output = out_dir / f"{_INPUT_STEM}.{target}"
        try:
            size = output.stat().st_size
        except OSError:
            return None
        if size == 0:
            return None
        # The result is read into memory, so its size on disk is checked first. A ZIP is never larger than its content plus
        # a small overhead, so anything over the uncompressed limit would be rejected by the ZIP check anyway.
        if size > max_output_bytes:
            raise DocumentTooLargeError(_TOO_LARGE)
        return output.read_bytes()


def _run(command: list[str], working_folder: Path) -> None:
    """Run the command to its end or to the time limit. Its exit code and output are not used: LibreOffice exits with 0 on a
    file it could not read, so the only signal is whether the output file exists."""
    # `soffice` is a launcher (a shell script on Linux and Homebrew, `soffice.exe` starting `soffice.bin` on Windows), so the
    # real program is a grandchild. On POSIX it gets its own process group, and the whole group is killed on timeout; killing
    # only the launcher left `soffice.bin` running (Pinned by: test_legacy.py::TestRunner::test_timeout_kills_the_whole_process_group).
    own_group = os.name == "posix"
    guard = _start_guard(working_folder) if own_group else None
    process = None
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   cwd=working_folder, start_new_session=own_group, shell=False)
        if guard is not None:
            _tell_guard(guard, process.pid)
        process.wait(timeout=CONVERSION_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ConversionTimeout from exc
    finally:
        # Also after a normal exit of the launcher and on any other exception (KeyboardInterrupt): nothing of ours stays behind.
        if process is not None:
            _kill(process, own_group)
        if guard is not None:
            _stop_guard(guard)


# What the guard process runs. It waits for its standard input to close, which happens when the process that started the
# conversion is gone, then kills the process group named on the first line and removes the folder given as its argument.
# "Gone" is seen two ways: the pipe closes, or the guard has a new parent. The second is needed when the converting process had
# forked meanwhile — the fork holds a copy of the pipe, and the guard used to wait for that process to end as well.
# The parent is watched only when it is the process named in the arguments: started through a wrapper that is not replaced
# by the interpreter, the guard has another parent from the start and relies on the pipe alone.
_GUARD = (
    "import os, select, shutil, signal, sys\n"
    "parent, fd, said = os.getppid(), sys.stdin.fileno(), b''\n"
    "watch = str(parent) == sys.argv[2]\n"
    "while not watch or os.getppid() == parent:\n"
    "    if select.select([fd], [], [], 1.0)[0]:\n"
    "        piece = os.read(fd, 64)\n"
    "        if not piece:\n"
    "            break\n"
    "        said += piece\n"
    "group = said.split(b'\\n')[0].strip()\n"
    "try:\n"
    "    os.killpg(int(group), signal.SIGKILL) if group else None\n"
    "except (OSError, ValueError):\n"
    "    pass\n"
    "shutil.rmtree(sys.argv[1], ignore_errors=True)\n"
)
_PYTHON_PROGRAM = re.compile(r"(?:python|pypy)[\w.-]*\Z", re.IGNORECASE)


def _start_guard(folder: Path) -> subprocess.Popen | None:
    """Start the process that cleans up if this one is killed outright (POSIX).

    `IsolatedConverter` ends a worker with SIGKILL on its time limit, when the request is cancelled and on shutdown. No
    `finally` runs then, so LibreOffice kept running without a parent and the folder with the uploaded file stayed on disk —
    for good when the document made LibreOffice hang (Pinned by:
    test_legacy.py::TestRunner::test_killing_the_converting_process_leaves_no_launcher_child_and_no_folder). The guard is a
    bare Python process in its own session holding a pipe from this process; it does nothing until the pipe closes. Without
    an interpreter to start (a frozen application, an embedded Python) there is no guard and the conversion goes on as before."""
    # Only a real interpreter is started: in a frozen application or an embedding host (uWSGI) `sys.executable` is the
    # application itself, and `-c` would start a second copy of it
    # (Pinned by: test_legacy.py::TestRunner::test_guard_is_started_only_with_a_python_interpreter).
    program = sys.executable or ""
    if getattr(sys, "frozen", False) or not _PYTHON_PROGRAM.match(os.path.basename(program)):
        return None
    try:
        return subprocess.Popen([program, "-I", "-S", "-c", _GUARD, str(folder), str(os.getpid())], stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True, shell=False)
    except OSError:
        return None


def _tell_guard(guard: subprocess.Popen, group: int) -> None:
    try:
        guard.stdin.write(f"{group}\n".encode("ascii"))
        guard.stdin.flush()
    except OSError:
        pass


def _stop_guard(guard: subprocess.Popen) -> None:
    """End the guard before its pipe closes, so that it does nothing: this process has cleaned up itself."""
    guard.kill()
    try:
        guard.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass
    try:
        guard.stdin.close()
    except OSError:
        pass


def _kill(process: subprocess.Popen, own_group: bool) -> None:
    if own_group:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    elif process.poll() is None:
        # Windows: `taskkill /T` ends the launcher together with the `soffice.bin` it started.
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _guard_converted_zip(data: bytes, options: ConvertOptions) -> None:
    """Zip-bomb defense for the converted file: the same check as `converter._guard_zip` (both use `_package.uncompressed_size`),
    which ran on DOC / PPT detection and so never saw this ZIP. Called here because extractors do not import the converter
    (dependency direction, core.md)."""
    try:
        total = uncompressed_size(data)
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError(_NO_OUTPUT) from exc
    if total > options.max_uncompressed_bytes:
        raise DocumentTooLargeError(_TOO_LARGE)


def _stream_head(data: bytes, wanted: str, size: int) -> bytes | None:
    """The first `size` bytes of the OLE stream with that name (case-insensitive, as in detection), or None without one."""
    try:
        with olefile.OleFileIO(BytesIO(data)) as ole:
            for entry in ole.listdir(streams=True, storages=False):
                if "/".join(entry).lower() == wanted:
                    with ole.openstream(entry) as stream:
                        return stream.read(size)
    except Exception:  # noqa: BLE001 - a damaged container is not "encrypted"; LibreOffice decides whether it can be read
        return None
    return None


def _is_encrypted_doc(data: bytes) -> bool:
    head = _stream_head(data, "worddocument", _FIB_FLAGS_OFFSET + 2)
    if head is None or len(head) < _FIB_FLAGS_OFFSET + 2:
        return False
    return bool(int.from_bytes(head[_FIB_FLAGS_OFFSET:_FIB_FLAGS_OFFSET + 2], "little") & _FIB_ENCRYPTED)


def _is_encrypted_ppt(data: bytes) -> bool:
    """An encrypted presentation says so in the `Current User` stream (header token 0xF3D1C4DF instead of 0xE391C05F), and
    usually replaces its summary streams with one `EncryptedSummary` stream. Either sign is enough."""
    if _stream_head(data, "encryptedsummary", 1) is not None:
        return True
    head = _stream_head(data, "current user", _CURRENT_USER_TOKEN_OFFSET + 4)
    if head is None or len(head) < _CURRENT_USER_TOKEN_OFFSET + 4:
        return False
    return int.from_bytes(head[_CURRENT_USER_TOKEN_OFFSET:_CURRENT_USER_TOKEN_OFFSET + 4], "little") == _CURRENT_USER_ENCRYPTED_TOKEN
