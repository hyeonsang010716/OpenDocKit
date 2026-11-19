"""Word and PowerPoint 97-2003 (.doc, .ppt), read through an installed LibreOffice.

These binary formats are not parsed in Python. When LibreOffice is installed, it is run headless to convert the file to
DOCX / PPTX inside a private temporary directory, and the result goes through the ordinary DOCX / PPTX extractors. When
it is not installed, the file is rejected with advice. Nothing else in the library depends on LibreOffice.
The decisions are in docs/decisions/legacy-office.md.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
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
import time
import zipfile

from opendockit.exceptions import (
    ConversionError,
    CorruptedDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    UnsupportedFormatError,
)
from opendockit.extractors._ole import OleFile, OleStreams
from opendockit.extractors._package import uncompressed_size
from opendockit.extractors.docx import extract_docx
from opendockit.extractors.pptx import extract_pptx
from opendockit.models import ConvertOptions, Extraction


if os.name == "nt":
    import winreg

    from opendockit import _windows
else:
    winreg = None
    _windows = None


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
_NO_SCRATCH = ("The temporary files for the LibreOffice conversion could not be written or read. "
               "Check the free space and permissions of the temporary folder.")
_ENCRYPTED = "The document is password-protected. Please remove the password and upload it again."
_TOO_LARGE = "The document becomes too large when decompressed."
CONVERTED_WARNING = "The document was converted with LibreOffice before reading, so layout details may differ from the original."

_PROGRAM_NAMES = ("soffice", "libreoffice")
_MACOS_PROGRAM = "/Applications/LibreOffice.app/Contents/MacOS/soffice"
_LINUX_PROGRAMS = ("/usr/lib/libreoffice/program/soffice", "/usr/lib64/libreoffice/program/soffice", "/opt/libreoffice/program/soffice")
_WINDOWS_FOLDER_VARIABLES = ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432")
# Where Windows itself records the folders, read when the environment does not name them. A process cannot count on its
# environment: an MCP client starts its servers with a short list of variables, and the one of the official SDK has no
# PROGRAMFILES, so a LibreOffice installed in the default place was "not installed" for the MCP server — and only there
# (Pinned by: test_legacy.py::TestLookup::test_windows_install_is_found_without_the_program_files_variables,
# TestRealLibreOffice::test_found_with_the_environment_an_mcp_client_passes). The installer records its own `program`
# folder as the default value of `UNO\InstallPath`, which also finds an install outside Program Files.
_WINDOWS_INSTALL_KEY = r"SOFTWARE\LibreOffice\UNO\InstallPath"
_WINDOWS_FOLDERS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion"
_WINDOWS_FOLDER_VALUES = ("ProgramFilesDir", "ProgramFilesDir (x86)", "ProgramW6432Dir")

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


class ProgramNotStarted(Exception):
    """The LibreOffice program could not be started (the `OSError` of `subprocess.Popen` is its cause). Raised by the
    runner; never leaves this module."""


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
    except ProgramNotStarted as exc:
        # The program disappeared or may not be executed. The cached lookup is dropped so a repaired install is found again.
        reset_soffice_cache()
        raise UnsupportedFormatError(_NOT_STARTED) from exc.__cause__
    except OSError as exc:
        # The temporary folder could not be made, written or read (a full disk, a read-only temporary folder). Taken with the
        # start of the program, it was 'LibreOffice could not be started' and dropped the lookup of a program that was fine
        # (Pinned by: test_legacy.py::TestRunner::test_temporary_folder_that_cannot_be_written_is_not_a_program_that_cannot_be_started).
        raise ConversionError(_NO_SCRATCH) from exc
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
    installed, program_folders = _windows_registry_folders()
    candidates += [os.path.join(folder, "soffice.exe") for folder in installed]
    candidates += [os.path.join(folder, "LibreOffice", "program", "soffice.exe") for folder in program_folders]
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)


def _windows_registry_folders() -> tuple[list[str], list[str]]:
    """The `program` folders LibreOffice's installer recorded, and the Program Files folders, from the registry (Windows).

    Both views of the machine key are read, so a 32-bit LibreOffice is found from a 64-bit Python and the other way round,
    and the user's own key for an install made for one user. Empty elsewhere, and wherever the registry cannot be read.
    """
    if winreg is None:
        return [], []
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    installed = [_registry_text(hive, _WINDOWS_INSTALL_KEY, "", view)
                 for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER) for view in views]
    folders = [_registry_text(winreg.HKEY_LOCAL_MACHINE, _WINDOWS_FOLDERS_KEY, name, winreg.KEY_WOW64_64KEY)
               for name in _WINDOWS_FOLDER_VALUES]
    return list(dict.fromkeys(filter(None, installed))), list(dict.fromkeys(filter(None, folders)))


def _registry_text(hive: int, key: str, name: str, view: int) -> str | None:
    """A text value of the registry (`""` is a key's default value), with its variables expanded; None without one."""
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as opened:
            value, kind = winreg.QueryValueEx(opened, name)
    except OSError:
        return None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    return winreg.ExpandEnvironmentStrings(value) if kind == winreg.REG_EXPAND_SZ else value


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

    Raises `ConversionTimeout` when the time limit passes, `ProgramNotStarted` when the program cannot be started and `OSError`
    when the temporary directory cannot be made, written or read. Everything happens inside one temporary directory that is
    removed on every path.
    """
    with _scratch_folder() as folder:
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


@contextmanager
def _scratch_folder() -> Iterator[str]:
    """The private temporary folder of one conversion, removed afterwards on every path.

    On Windows it is removed through its long form (`\\\\?\\C:\\…`) and tried again for a moment. LibreOffice fills its
    profile with paths of 140 characters and more below the folder, which it writes through the long form itself, so under
    a temporary folder of a hundred characters (a service account, a nested `TMP`) they passed the 260-character limit of
    ordinary paths: the removal failed on every conversion and `ignore_cleanup_errors` hid it — a folder left behind per
    document (Pinned by: test_legacy.py::TestRealLibreOffice::test_no_folder_is_left_under_a_long_temporary_path).
    """
    if os.name != "nt":
        with tempfile.TemporaryDirectory(prefix="opendockit-", ignore_cleanup_errors=True) as folder:
            yield folder
        return
    folder = tempfile.mkdtemp(prefix="opendockit-")
    try:
        yield folder
    finally:
        _remove_folder(folder, _REMOVE_SECONDS)


# How long the removal of a conversion's folder is tried (Windows): LibreOffice has been ended by then (`end_job`), so this
# only covers a file a scanner opened behind it.
_REMOVE_SECONDS = 2.0
# How long the processes of a LibreOffice run may take to end once their job is ended. Ending one is asynchronous, and the
# folder cannot be removed while one still holds a file of it or has it as its current directory.
_END_SECONDS = 10.0


def _long_path(path: str) -> str:
    """The form of a Windows path that is not held to 260 characters: `\\\\?\\C:\\…`, `\\\\?\\UNC\\host\\share\\…`."""
    path = os.path.abspath(path)
    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def _remove_folder(folder: str, seconds: float) -> None:
    """Remove a folder and everything in it, through its long form, trying again for up to `seconds` (Windows)."""
    target = _long_path(folder)
    deadline = time.monotonic() + seconds
    while True:
        shutil.rmtree(target, ignore_errors=True)
        if not os.path.lexists(target) or time.monotonic() >= deadline:
            return
        time.sleep(0.05)


def _run(command: list[str], working_folder: Path) -> None:
    """Run the command to its end or to the time limit. Its exit code and output are not used: LibreOffice exits with 0 on a
    file it could not read, so the only signal is whether the output file exists."""
    # `soffice` is a launcher (a shell script on Linux and Homebrew, `soffice.exe` starting `soffice.bin` on Windows), so the
    # real program is a grandchild. On POSIX it gets its own process group, and the whole group is killed on timeout; killing
    # only the launcher left `soffice.bin` running (Pinned by: test_legacy.py::TestRunner::test_timeout_kills_the_whole_process_group).
    own_group = os.name == "posix"
    guard = _start_guard(working_folder) if own_group else _start_windows_guard(working_folder)
    # On Windows a job object ends LibreOffice in place of the process group: our death closes its handle and the kernel
    # ends the launcher and the `soffice.bin` it started with us, so a worker killed on its time limit — where no `finally`
    # of ours runs — leaves no program behind, and the guard above removes the folder
    # (Pinned by: test_legacy.py::TestRunner::test_killing_the_converting_process_leaves_no_launcher_child_and_no_folder_windows).
    job = _windows.job_for_children() if _windows is not None else None
    process = None
    try:
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                       cwd=working_folder, start_new_session=own_group, shell=False)
        except OSError as exc:
            raise ProgramNotStarted from exc
        if guard is not None and own_group:   # the Windows guard watches this process, so there is nothing to tell it
            _tell_guard(guard, process.pid)
        if job is not None:
            # Known accepted gap: the launcher is put in after it started, so a `soffice.bin` it managed to start in those
            # microseconds is outside the job. `taskkill /T` below still ends that one on the normal path.
            _windows.add_process(job, process.pid)
        process.wait(timeout=CONVERSION_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise ConversionTimeout from exc
    finally:
        # Also after a normal exit of the launcher and on any other exception (KeyboardInterrupt): nothing of ours stays behind.
        if process is not None:
            _kill(process, own_group)
        if guard is not None:
            _stop_guard(guard)
        if job is not None:
            # Waited for, not only closed: a `soffice.bin` killed on the time limit was still going down when the folder was
            # removed, so the removal failed on its files and the folder stayed
            # (Pinned by: test_legacy.py::TestRealLibreOffice::test_no_folder_is_left_after_the_time_limit).
            _windows.end_job(job, _END_SECONDS)
            _windows.close_job(job)


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

# What the Windows guard runs. It waits on a handle to the process that started it — no pipe and no polling — and removes
# the folder once that process is gone. The job object ends LibreOffice itself, so the folder is all this has to remove.
#
# The removal is tried again until it works, for `_WINDOWS_GUARD_SECONDS`. Windows refuses to remove a folder that is a
# running process's current directory, and LibreOffice is started with the folder as its own (`cwd=working_folder`): the
# kernel ends it when the job's handle closes, but it does so after the process this guard waits on has already gone, so a
# single attempt removes nothing and `ignore_errors` hides that it removed nothing
# (Pinned by: test_legacy.py::TestRunner::test_the_windows_guard_removes_a_folder_still_held_as_a_current_directory).
_WINDOWS_GUARD_SECONDS = 30.0
_WINDOWS_GUARD = (
    "import ctypes, os, shutil, sys, time\n"
    "from ctypes import wintypes\n"
    "library = ctypes.WinDLL('kernel32', use_last_error=True)\n"
    "library.OpenProcess.restype = wintypes.HANDLE\n"
    "library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]\n"
    "library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]\n"
    "handle = library.OpenProcess(0x00100000, False, int(sys.argv[2]))\n"   # SYNCHRONIZE
    "if handle:\n"
    "    library.WaitForSingleObject(handle, 0xFFFFFFFF)\n"
    # Only "no such process" (87) means the converting process is gone. Any other refusal says nothing about it, and
    # removing the folder then would take the document away from a conversion that is still running.
    "elif ctypes.get_last_error() != 87:\n"
    "    sys.exit(0)\n"
    "deadline = time.monotonic() + float(sys.argv[3])\n"
    "while True:\n"
    "    shutil.rmtree(sys.argv[1], ignore_errors=True)\n"
    "    if not os.path.exists(sys.argv[1]) or time.monotonic() >= deadline:\n"
    "        break\n"
    "    time.sleep(0.1)\n"
)


def _start_windows_guard(folder: Path) -> subprocess.Popen | None:
    """Start the process that removes the folder if this one is killed outright (Windows).

    The job object ends LibreOffice when this process dies, but nothing removed the folder holding the uploaded document —
    the user's own file, left on disk. This does what the POSIX guard does, the Windows way: a bare Python process waiting
    on a handle to this one. It is killed in the `finally` of `_run`, so on the ordinary path it never acts.

    Known accepted gap: the guard is a child of this process and so is in the same job, so a server killed outright ends it
    together with the worker and the folder stays. It covers what the POSIX guard covers — a worker ended on its time
    limit, on a cancelled request and at shutdown
    (Pinned by: test_legacy.py::TestRunner::test_killing_the_converting_process_leaves_no_launcher_child_and_no_folder_windows).
    """
    program = sys.executable or ""
    if getattr(sys, "frozen", False) or not _PYTHON_PROGRAM.match(os.path.basename(program)):
        return None
    try:
        # CREATE_NO_WINDOW: a bare `python.exe` would flash a console window on every conversion.
        # The long form of the folder: the profile below it passes 260 characters under a long temporary path.
        return subprocess.Popen([program, "-I", "-S", "-c", _WINDOWS_GUARD, _long_path(str(folder)), str(os.getpid()),
                                 str(_WINDOWS_GUARD_SECONDS)],
                                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                creationflags=subprocess.CREATE_NO_WINDOW, shell=False)
    except (OSError, ValueError, AttributeError):
        return None


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
        # Windows: `taskkill /T` ends the launcher together with the `soffice.bin` it started. CREATE_NO_WINDOW, as for the
        # guard: started from a process without a console (pythonw, a service, an app's MCP server) a console program
        # opens a window of its own.
        try:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10, check=False,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        with OleFile(BytesIO(data)) as ole:
            streams = OleStreams(ole, len(data))  # a stream is read no further than the size of the file
            for entry in ole.listdir(streams=True, storages=False):
                if "/".join(entry).lower() == wanted:
                    return streams.read("/".join(entry))[:size]
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
