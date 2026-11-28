"""Test suite. Sample documents are built in memory by `tests.builder`."""

from collections.abc import Iterator
from contextlib import contextmanager
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from opendockit.extractors._html_base import HTML5_TOKENIZER, READS_LIKE_A_BROWSER


if os.name == "nt":
    from ctypes import wintypes


# Some HTML and mail tests pin behavior that needs an HTML5 parser: libxml2 2.14 or, where lxml bundles an older one (its
# Windows wheels), html5lib. Only an install that has neither loses that content (docs/limitations.md), and there these
# tests are skipped and the extractor reports the gap with a warning.
needs_html5_tokenizer = pytest.mark.skipif(not READS_LIKE_A_BROWSER, reason="needs an HTML5 parser: libxml2 2.14 or html5lib")

# A few tests pin how long a crafted page takes, and that holds only where libxml2's own tokenizer reads it. html5lib is
# quadratic on the same pages, and the builder gate (`_html_base.builder_for`) hands them to the older parser instead, which
# is quadratic on them too — so neither builder keeps the promise where libxml2 is older than 2.14.
needs_libxml2_html5_tokenizer = pytest.mark.skipif(
    not HTML5_TOKENIZER, reason="the time this pins holds only with the HTML5 tokenizer of libxml2 2.14")


if os.name == "posix":
    def process_is_alive(pid: int) -> bool:
        """Whether the process still exists. Not for a child of this process: `waitpid` is needed to see it has ended."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:   # it exists and belongs to someone else
            return True
        return True
else:
    _STILL_ACTIVE = 259

    def process_is_alive(pid: int) -> bool:
        """The same question on Windows, asked through a process handle: `os.kill(pid, 0)` ends the process there."""
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)


def file_at_length(folder: Path, length: int, data: bytes, suffix: str = ".docx") -> Path:
    """A file holding `data` whose full path is exactly `length` characters, made under `folder` (Windows).

    Written through the long form (`\\\\?\\`), so that it can be made however long it is, under folders with Korean names, as
    a synchronised folder has them. The path returned is the ordinary one, as a user or an agent would pass it.
    """
    current = os.path.abspath(folder)
    level = "깊은 폴더 이름입니다 " + "x" * 20   # 31 characters
    while length - len(current) - 1 - len(level) - 1 >= 20:
        current = os.path.join(current, level)
    name_length = length - len(current) - 1
    stem = "보고서 " + "x" * (name_length - len("보고서 ") - len(suffix))
    path = os.path.join(current, stem + suffix)
    assert len(path) == length and len(stem) > 0
    os.makedirs("\\\\?\\" + current, exist_ok=True)
    with open("\\\\?\\" + path, "wb") as handle:
        handle.write(data)
    return Path(path)


@contextmanager
def held_open(path: Path, *, sharing_reads: bool = False) -> Iterator[None]:
    """Hold `path` open without sharing it, as an editor that locks its document does (Windows), or `sharing_reads` only,
    as one that lets others read its file and not write it. Opened through the long form, so that a path of any length can
    be held."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     wintypes.DWORD, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    generic_read, open_existing = 0x80000000, 3
    sharing = 0x00000001 if sharing_reads else 0   # FILE_SHARE_READ, or nothing at all
    handle = kernel32.CreateFileW("\\\\?\\" + os.path.abspath(path), generic_read, sharing, None, open_existing, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    """Wait for a process to end. False if it is still there after `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.05)
    return not process_is_alive(pid)


def interpreter_itself() -> tuple[str, dict[str, str] | None]:
    """The program that runs this Python, and the environment to start it with (None: this one), so that the process
    started is the interpreter and not the launcher of a virtual environment (Windows), which runs the interpreter as a
    child of its own. `__PYVENV_LAUNCHER__` tells that interpreter whose environment it runs in, as multiprocessing tells
    its workers. Elsewhere, and outside a virtual environment, it is `sys.executable` as it is."""
    if os.path.normcase(sys.executable) == os.path.normcase(getattr(sys, "_base_executable", sys.executable)):
        return sys.executable, None
    return sys._base_executable, {**os.environ, "__PYVENV_LAUNCHER__": sys.executable}


def run_without_a_console(arguments: list[str], timeout: float) -> subprocess.CompletedProcess:
    """Run this Python with `arguments` in a process that has no console at all, as one started with DETACHED_PROCESS runs
    it (Windows). Standard input and output go nowhere; the caller reads a file.

    The interpreter itself is started, not the launcher of a virtual environment (`interpreter_itself`): started detached,
    the launcher gives the interpreter it starts a console of its own, and the process asked about would have one.
    """
    executable, environment = interpreter_itself()
    return subprocess.run([executable, *arguments], env=environment, creationflags=subprocess.DETACHED_PROCESS,
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout,
                          check=False)


# Read by a script run without a console (`run_without_a_console`): whether this process has a console, and whether that
# console has a window — a console made with CREATE_NO_WINDOW has none.
CONSOLE_PROBE = """
import ctypes
from ctypes import wintypes

def console_state() -> dict:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    return {"console": kernel32.GetConsoleProcessList((wintypes.DWORD * 1)(), 1) > 0,
            "window": bool(kernel32.GetConsoleWindow())}
"""
