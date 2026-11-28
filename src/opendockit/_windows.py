"""Worker limits on Windows, through a job object.

POSIX gets two things from the standard library that Windows does not have: `resource.setrlimit` for a memory limit, and a
pid that can be watched so a worker leaves when its server is gone. Here they come from two mechanisms:

- **The memory limit** is a job object's `ProcessMemoryLimit`. A worker that commits more than the limit gets a
  `MemoryError` — the same shape of failure as `RLIMIT_AS` on Linux. The server creates the job and each worker joins it by
  name. The name carries a random part so that two converters, or another program, never land in one job. It is not a
  secret: named objects can be listed by any process of the session. What protects the job is its security, the default
  one of this user, which only this user's processes and the system may open — and those could end the workers anyway.
- **Leaving with the server** has two mechanisms, and each was measured to end an idle worker at once when the server was
  killed outright. The job carries `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, so the kernel ends its processes as soon as the
  last handle to the job is closed — which the server's death does. `wait_for_process` is the second: the worker holds a
  handle to the server and waits on it. It is kept because it needs no job at all, and a job is the one part here that an
  environment can refuse (a policy, or the limit on how deeply jobs nest).

`job_for_children` is a third use: a job that LibreOffice is put into, so that it is ended with the process that started it.
That one needs no watching — the handle closes when the process dies, whatever kills it.

The workers' job lets a process of its own leave it, when that process asks (`JOB_OBJECT_LIMIT_BREAKAWAY_OK`): the guard
that removes the folder of a LibreOffice conversion asks for it, so that a server killed outright does not end the guard
with its workers and leave the uploaded document on disk. Nothing else asks. LibreOffice does not either: its launcher
starts `soffice.bin` with no flag but DETACHED_PROCESS (`desktop/win32/source/loader.cxx`), and the job of its own it is put
into inside that one lets nothing go even to a process that asks. That matters for the moment before the launcher is put
into it, when it is in the workers' job alone.

No job function raises. Where a call fails the limit is simply not applied, as `_limit_memory` does on macOS, and the callers
do not read the answer: a worker that could not join the job runs without the memory limit that was asked for, and a
LibreOffice that could not be put in one is not ended with the process that started it. Both need the job to have been made
— `open_job` failing is reported by handing out no name at all — and then an `OpenJobObjectW` of one's own user's job to
fail, which is why neither is treated as an error worth ending a conversion for. Known accepted gap: nothing in the result
says that a limit was not applied.

Three things about files belong here as well: the long form of a path (`long_path`, `openable`), telling a file another
program holds open from one this user may not read (`held_by_another_program`), and a temporary folder only this user may
open on every Python (`private_temporary_folder`). And one about the processes this one starts: whether it has a console for
them to share (`has_console`).
"""

import ctypes
from ctypes import wintypes
import errno
from functools import lru_cache
import os
import secrets
import sys
import tempfile
import time
import uuid


# A path of this many characters or more is not opened in its ordinary form, unless the machine has LongPathsEnabled set —
# off by default on Windows 10 and 11. The limit counts the terminating NUL: measured on Windows 11, a file at a full path
# of 259 characters opens and one of 260 is "not found".
MAX_PATH = 260
# What opening a file another program holds open without sharing it fails with.
_ERROR_SHARING_VIOLATION = 32
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_SHARE_EVERYTHING = 0x00000001 | 0x00000002 | 0x00000004   # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value


def long_path(path: str) -> str:
    """The form of a path that is not held to `MAX_PATH`: `\\\\?\\C:\\…`, or `\\\\?\\UNC\\host\\share\\…` for a share.

    Made absolute first: the long form is passed to the file system as written, so a `..`, a `/` or a name relative to the
    current folder in it would no longer be resolved. A path already in the long form, or a device path (`\\\\.\\…`), is
    returned as it is.
    """
    path = os.path.abspath(path)
    if path.startswith(("\\\\?\\", "\\\\.\\")):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def openable(path: str) -> str:
    """`path` as it is, or its long form when its full path is too long to be opened as it is (`MAX_PATH`).

    Only such a path changes: every other one is passed on exactly as it was given, so what an error message names is
    still what the caller wrote.
    """
    if path.startswith("\\\\?\\"):
        return path
    full = os.path.abspath(path)
    return long_path(full) if len(full) >= MAX_PATH else path


def held_by_another_program(path: str, *, for_writing: bool = False) -> bool:
    """Whether the file at `path` cannot be opened — for reading, or `for_writing` — because another program holds it open
    without sharing that.

    Asked of the file system again, after an open failed: Python opens a file through the C runtime, which reports a
    sharing violation as EACCES, the same as a missing permission, and keeps no Windows error to tell them apart. The
    second open shares everything and changes nothing — an existing file only, never created or cut — so it is in nobody's
    way; a folder or a missing permission fails with another error, and a file that opens now is not held. Writing is
    asked apart because an editor commonly lets others read its file and not write it. Nothing raises: an answer that
    cannot be had is False.
    """
    access = _GENERIC_WRITE if for_writing else _GENERIC_READ
    try:
        library = _kernel32()
        handle = library.CreateFileW(openable(path), access, _SHARE_EVERYTHING, None, _OPEN_EXISTING, 0, None)
        if handle in (None, _INVALID_HANDLE):
            return ctypes.get_last_error() == _ERROR_SHARING_VIOLATION
        library.CloseHandle(handle)
        return False
    except (OSError, AttributeError, ValueError):
        return False


# What `os.mkdir(path, 0o700)` gives a folder on Windows from Python 3.12.4 on (CVE-2024-4030), as CPython's `os_mkdir_impl`
# writes it in 3.12 and 3.14: a protected DACL, so nothing is inherited from the folder above, that lets SYSTEM, the
# administrators and the folder's owner in, and that everything made inside inherits. Before 3.12.4 the mode was ignored on
# Windows and the folder took the permissions of the one above. Measured on Windows 11 under a folder that lets Everyone
# read: 3.12.0 to 3.12.3 gave the folder `(A;OICIID;FR;;;WD)`, Everyone reading it; 3.12.4, 3.12.13, 3.13.14 and 3.14.6
# gave exactly this (Pinned by: test_legacy.py::TestScratchFolderAccess).
_PRIVATE_FOLDER = "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"
_MKDIR_IS_PRIVATE = sys.version_info >= (3, 12, 4)
_SDDL_REVISION_1 = 1
_NAME_CHARACTERS = "abcdefghijklmnopqrstuvwxyz0123456789_"   # the characters and the length of `tempfile`'s names
_NAME_LENGTH = 8


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", wintypes.BOOL)]


@lru_cache(maxsize=1)
def _advapi32() -> ctypes.WinDLL:
    library = ctypes.WinDLL("advapi32", use_last_error=True)
    library.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    library.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.ULONG)]
    return library


def private_temporary_folder(prefix: str) -> str:
    """`tempfile.mkdtemp(prefix=prefix)`, with the folder open to this user only on every Python.

    From Python 3.12.4 on it is that call. Before, `mkdtemp` made a folder with the permissions of the temporary folder above
    it, which a server that points TEMP at a shared folder lets others read — and the folder of a LibreOffice conversion holds
    the uploaded document. There the folder is made as 3.12.4 makes it (`_PRIVATE_FOLDER`): given its permissions as it is
    created, so there is no moment at which it has others.
    """
    if _MKDIR_IS_PRIVATE:
        return tempfile.mkdtemp(prefix=prefix)
    return _private_mkdtemp(prefix)


def _private_mkdtemp(prefix: str) -> str:
    """`tempfile.mkdtemp(prefix=prefix)` as 3.12 writes it, each folder made with `_PRIVATE_FOLDER`: the same names, the same
    folder above, the same answer to a name that is taken, the same errors and the same audit events.

    Where the calls cannot be had at all (no advapi32 to bind), it is `mkdtemp` as before.
    """
    parent = tempfile.gettempdir()
    try:
        security, library = _advapi32(), _kernel32()
    except (OSError, AttributeError):
        return tempfile.mkdtemp(prefix=prefix)
    descriptor = ctypes.c_void_p()
    if not security.ConvertStringSecurityDescriptorToSecurityDescriptorW(_PRIVATE_FOLDER, _SDDL_REVISION_1,
                                                                         ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor.value, False)
        for _ in range(tempfile.TMP_MAX):
            folder = os.path.join(parent, prefix + _candidate_name())
            sys.audit("tempfile.mkdtemp", folder)
            sys.audit("os.mkdir", folder, 0o700, -1)
            if library.CreateDirectoryW(folder, ctypes.byref(attributes)):
                return os.path.abspath(folder)
            code = ctypes.get_last_error()
            # The error `os.mkdir` raises, of the class the code maps to, and `mkdtemp`'s answer to that class: a name
            # that is taken is passed over, and so is a PermissionError, which a name taken on Windows can raise.
            error = OSError(None, ctypes.FormatError(code), folder, code)
            if isinstance(error, FileExistsError):
                continue
            if isinstance(error, PermissionError) and os.path.isdir(parent) and os.access(parent, os.W_OK):
                continue
            raise error
        raise FileExistsError(errno.EEXIST, "No usable temporary directory name found")
    finally:
        library.LocalFree(descriptor)


def _candidate_name() -> str:
    return "".join(secrets.choice(_NAME_CHARACTERS) for _ in range(_NAME_LENGTH))


_JobObjectBasicAccountingInformation = 1
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
# The only right `AssignProcessToJobObject` needs. Asking for the full access mask would take `JOB_OBJECT_TERMINATE` with
# it, which nothing here uses. It is least privilege for our own workers, not a barrier: the job's default security is what
# decides who may open it, and with what (see the module docstring); the random name only keeps jobs apart.
_JOB_OBJECT_ASSIGN_PROCESS = 0x0001
_ERROR_INVALID_PARAMETER = 87   # what OpenProcess reports for a pid that does not exist
_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in
                ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                 "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.POINTER(ctypes.c_ulong)), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


@lru_cache(maxsize=1)
def _kernel32() -> ctypes.WinDLL:
    """The entry points used here, with their argument types declared.

    Without `argtypes` the pseudo-handle of `GetCurrentProcess` (-1) is passed as a Python int too large for a C pointer and
    the call fails with an OverflowError instead of doing anything.
    """
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.CreateJobObjectW.restype = wintypes.HANDLE
    library.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    library.OpenJobObjectW.restype = wintypes.HANDLE
    library.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    library.SetInformationJobObject.restype = wintypes.BOOL
    library.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    library.AssignProcessToJobObject.restype = wintypes.BOOL
    library.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    library.GetCurrentProcess.restype = wintypes.HANDLE
    library.GetCurrentProcess.argtypes = []
    library.OpenProcess.restype = wintypes.HANDLE
    library.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    library.WaitForSingleObject.restype = wintypes.DWORD
    library.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    library.CloseHandle.argtypes = [wintypes.HANDLE]
    library.CreateFileW.restype = wintypes.HANDLE
    library.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                    wintypes.DWORD, wintypes.HANDLE]
    library.TerminateJobObject.restype = wintypes.BOOL
    library.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    library.QueryInformationJobObject.restype = wintypes.BOOL
    library.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                                  ctypes.POINTER(wintypes.DWORD)]
    library.GetConsoleProcessList.restype = wintypes.DWORD
    library.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
    library.CreateDirectoryW.restype = wintypes.BOOL
    library.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
    library.LocalFree.restype = ctypes.c_void_p
    library.LocalFree.argtypes = [ctypes.c_void_p]
    return library


def has_console() -> bool:
    """Whether this process has a console, with a window or without one.

    A process that has none — started detached, a service, a program with windows of its own such as `pythonw` — gives
    every console program it starts a console of its own, and that console opens a window on the desktop. A process that
    has one passes it on, and its children open nothing. Nothing raises: an answer that cannot be had is True, which leaves
    the processes this one starts as they were.
    """
    try:
        return _kernel32().GetConsoleProcessList((wintypes.DWORD * 1)(), 1) != 0   # 0: the call failed, there is no console
    except (OSError, AttributeError, ValueError):
        return True


def new_job_name() -> str:
    """A name no other process can guess, for one converter's job."""
    return f"opendockit-workers-{uuid.uuid4().hex}"


def open_job(name: str | None, memory_limit: int | None, *, lets_go: bool = False) -> int | None:
    """The job, or None when job objects cannot be used. A name makes it one other processes can join by name.

    The handle is the job's life: `close_job` or the death of this process ends every worker in it.

    `lets_go`: a process in the job may start one out of it, when it asks with CREATE_BREAKAWAY_FROM_JOB. Nothing is let go
    without asking — the processes a worker starts are in its job as before. A job that cannot be made so is made without
    it: the limits come first.
    """
    try:
        library = _kernel32()
        job = library.CreateJobObjectW(None, name)
        if not job:
            return None
        limits = _ExtendedLimits()
        flags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            limits.ProcessMemoryLimit = memory_limit
            flags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        for extra in ((_JOB_OBJECT_LIMIT_BREAKAWAY_OK, 0) if lets_go else (0,)):
            limits.BasicLimitInformation.LimitFlags = flags | extra
            if library.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(limits),
                                               ctypes.sizeof(limits)):
                return job
        library.CloseHandle(job)
        return None
    except (OSError, AttributeError, ValueError):  # no kernel32, or a policy that refuses job objects
        return None


def join_job(name: str) -> bool:
    """Runs in a worker. Put this process into the job and let go of the handle.

    The handle is closed right away: a handle held by a worker keeps the job alive past the server that made it.
    """
    try:
        library = _kernel32()
        job = library.OpenJobObjectW(_JOB_OBJECT_ASSIGN_PROCESS, False, name)
        if not job:
            return False
        try:
            return bool(library.AssignProcessToJobObject(job, library.GetCurrentProcess()))
        finally:
            library.CloseHandle(job)
    except (OSError, AttributeError, ValueError):
        return False


def wait_for_process(pid: int) -> bool:
    """Block until the process is gone. True once it has ended, False when that cannot be established.

    The handle is taken once and held for the whole wait, so the question is never asked about a pid that now belongs to
    somebody else — the hole the POSIX `os.kill(pid, 0)` watch has to live with. Nothing is polled: the kernel wakes the
    caller when the process ends.

    A handle that cannot be opened is only read as "gone" when the system says there is no such process. Anything else —
    an access check that fails because the server runs at another integrity level, under a restricted token, or behind a
    process-protection policy — says nothing about whether it is alive, and a caller that ended its worker on that would
    end every worker the moment it started, turning every document on that machine into "too large or too complex"
    (Pinned by: test_isolated.py::TestWorkerLeavesWithItsServer::test_a_server_that_cannot_be_waited_for_does_not_end_the_worker).
    """
    try:
        library = _kernel32()
        handle = library.OpenProcess(_SYNCHRONIZE, False, pid)
        if not handle:
            return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER   # the one error that means "no such process"
        try:
            library.WaitForSingleObject(handle, _INFINITE)
            return True
        finally:
            library.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False


def job_for_children() -> int | None:
    """A job for processes this one starts, ended with it. The Windows stand-in for the POSIX guard process.

    A process put into the job takes the processes it starts in with it, so a launcher and the program it really runs
    (`soffice.exe` and its `soffice.bin`) are ended together. Nothing is watching: the handle is closed when this process
    dies, whatever kills it, and the kernel does the rest. It lets no process go, even one that asks: measured on Windows 11,
    a process in it that asks for CREATE_BREAKAWAY_FROM_JOB is refused, also where the job above it lets processes go
    (Pinned by: test_legacy.py::TestRunner::test_libreoffice_cannot_leave_the_job_it_is_ended_with).
    """
    return open_job(None, None)


def add_process(job: int | None, pid: int) -> bool:
    """Put an already running process into `job`. Its own children follow it in."""
    if job is None:
        return False
    try:
        library = _kernel32()
        # PROCESS_SET_QUOTA | PROCESS_TERMINATE: what AssignProcessToJobObject asks for.
        handle = library.OpenProcess(0x0100 | 0x0001, False, pid)
        if not handle:
            return False
        try:
            return bool(library.AssignProcessToJobObject(job, handle))
        finally:
            library.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False


def end_job(job: int | None, seconds: float) -> bool:
    """End every process in `job` and wait, up to `seconds`, until none is left. True once the job is empty.

    Ending a process is asynchronous on Windows: `TerminateJobObject` returns while the processes are still being taken
    down, holding their files and their current directory. Whoever removes their folder next must wait for this, or the
    removal fails on files that are gone a moment later.
    """
    if job is None:
        return True
    try:
        library = _kernel32()
        library.TerminateJobObject(job, 1)
        accounting = _BasicAccounting()
        deadline = time.monotonic() + seconds
        while True:
            if not library.QueryInformationJobObject(job, _JobObjectBasicAccountingInformation, ctypes.byref(accounting),
                                                     ctypes.sizeof(accounting), None):
                return False
            if accounting.ActiveProcesses == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
    except (OSError, AttributeError, ValueError):
        return False


def close_job(job: int | None) -> None:
    """Runs in the server, at shutdown. Closing the last handle ends every worker still in the job."""
    if job is None:
        return
    try:
        _kernel32().CloseHandle(job)
    except (OSError, AttributeError, ValueError):
        pass
