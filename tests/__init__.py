"""Test suite. Sample documents are built in memory by `tests.builder`."""

import ctypes
import os
from pathlib import Path
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


def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    """Wait for a process to end. False if it is still there after `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.05)
    return not process_is_alive(pid)
