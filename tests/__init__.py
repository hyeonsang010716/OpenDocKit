"""Test suite. Sample documents are built in memory by `tests.builder`."""

import ctypes
import os
import time

import pytest

from opendockit.extractors.html import HTML5_TOKENIZER


if os.name == "nt":
    from ctypes import wintypes


# Some HTML and mail tests pin behavior that needs the HTML5 tokenizer of libxml2 2.14. lxml's Windows wheels still bundle
# libxml2 2.11, where the same input loses content (docs/limitations.md); the extractor reports that with a warning.
needs_html5_tokenizer = pytest.mark.skipif(not HTML5_TOKENIZER, reason="needs the HTML5 tokenizer of libxml2 2.14 or newer")


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


def wait_until_gone(pid: int, seconds: float = 5.0) -> bool:
    """Wait for a process to end. False if it is still there after `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not process_is_alive(pid):
            return True
        time.sleep(0.05)
    return not process_is_alive(pid)
