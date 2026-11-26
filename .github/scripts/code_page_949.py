"""Run this environment's Python in code page 949, the default of Korean Windows, whatever the code page of the machine.

    uv run python .github/scripts/code_page_949.py -m pytest -n auto -m "not timing"

Windows runs a program in the legacy code page of its system locale unless the program's manifest names another
(`activeCodePage`): 1252 on the CI runners, 949 on Korean Windows. This makes a copy of the interpreter whose manifest
names `ko-KR`, which runs in 949 whatever the system locale (measured on windows-latest, whose locale is en-US), and runs
the arguments with it. There `GetACP()` and `GetOEMCP()` are 949, the locale encoding is cp949 and a stream that is not a
console is written in cp949, as on a Korean machine with default settings.

- The copy is this environment's base interpreter (its executable and DLLs) with junctions to the base's `Lib` and
  `DLLs`, so it starts as the base does, under `-I` too. Every Python process it starts, a test's `sys.executable` or a
  spawned worker, is the copy and runs in 949 as well.
- The packages are this environment's: `PYTHONPATH` names `src` and a `sitecustomize` that puts this environment's
  site-packages on the path in place of the base's, its `.pth` files read as UTF-8.
- Nothing that sets the encoding of the streams from outside is passed on (`PYTHONIOENCODING`, `PYTHONUTF8`,
  `PYTHONLEGACYWINDOWSSTDIO`).
- `COPY_VARIABLE` names the copy in its environment. A copy is not copied again (its manifest names a code page, its
  site-packages are the base's), so the tests of this script are skipped where it is set.
- Known gap: a process started with an environment of its own, without `PYTHONPATH`, finds no package. One test does that
  (an MCP client's environment), and it needs LibreOffice, which the job does not install.

Ends with status 2 when the copy does not run in 949, so that the suite is never run in another code page as if it were
this one, and otherwise with the status of the command. The copy is removed afterwards, its junctions first and on their
own, so that nothing of the base interpreter they lead to is touched.
"""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CODE_PAGE = 949
LOCALE = "ko-KR"
# Set from outside, these would decide the encoding of the streams instead of the code page
STREAM_VARIABLES = ("PYTHONIOENCODING", "PYTHONUTF8", "PYTHONLEGACYWINDOWSSTDIO")
COPY_VARIABLE = "OPENDOCKIT_CODE_PAGE_949_COPY"
_RT_MANIFEST, _MANIFEST_ID, _LOAD_LIBRARY_AS_DATAFILE, _ENGLISH = 24, 1, 0x2, 0x0409

_SITECUSTOMIZE = '''"""This environment's packages for the copy of its interpreter, in place of the base interpreter's own."""
import os
import sys

_SITE = {site!r}
_BASE_SITE = os.path.normcase(os.path.join(sys.base_prefix, "Lib", "site-packages"))
sys.path[:] = [entry for entry in sys.path if not os.path.normcase(entry).startswith(_BASE_SITE)]
sys.path.append(_SITE)
for _name in sorted(os.listdir(_SITE)):
    if not _name.endswith(".pth"):
        continue
    with open(os.path.join(_SITE, _name), encoding="utf-8") as _pth:
        for _line in _pth:
            _line = _line.rstrip()
            if not _line or _line.startswith("#"):
                continue
            if _line.startswith(("import ", "import\\t")):
                exec(_line)
                continue
            _folder = os.path.join(_SITE, _line)
            if os.path.exists(_folder) and _folder not in sys.path:
                sys.path.append(_folder)
'''


def _kernel32() -> ctypes.WinDLL:
    library = ctypes.WinDLL("kernel32", use_last_error=True)
    library.LoadLibraryExW.restype = wintypes.HMODULE
    library.LoadLibraryExW.argtypes = [wintypes.LPCWSTR, wintypes.HANDLE, wintypes.DWORD]
    library.FindResourceW.restype = wintypes.HRSRC
    library.FindResourceW.argtypes = [wintypes.HMODULE, ctypes.c_void_p, ctypes.c_void_p]
    library.LoadResource.restype = wintypes.HGLOBAL
    library.LoadResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
    library.LockResource.restype = ctypes.c_void_p
    library.LockResource.argtypes = [wintypes.HGLOBAL]
    library.SizeofResource.restype = wintypes.DWORD
    library.SizeofResource.argtypes = [wintypes.HMODULE, wintypes.HRSRC]
    library.FreeLibrary.argtypes = [wintypes.HMODULE]
    library.BeginUpdateResourceW.restype = wintypes.HANDLE
    library.BeginUpdateResourceW.argtypes = [wintypes.LPCWSTR, wintypes.BOOL]
    library.UpdateResourceW.restype = wintypes.BOOL
    library.UpdateResourceW.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, wintypes.WORD, ctypes.c_void_p,
                                        wintypes.DWORD]
    library.EndUpdateResourceW.restype = wintypes.BOOL
    library.EndUpdateResourceW.argtypes = [wintypes.HANDLE, wintypes.BOOL]
    return library


def _manifest(kernel32: ctypes.WinDLL, executable: Path) -> str:
    module = kernel32.LoadLibraryExW(str(executable), None, _LOAD_LIBRARY_AS_DATAFILE)
    if not module:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        found = kernel32.FindResourceW(module, ctypes.c_void_p(_MANIFEST_ID), ctypes.c_void_p(_RT_MANIFEST))
        if not found:
            raise ctypes.WinError(ctypes.get_last_error())
        size = kernel32.SizeofResource(module, found)
        return ctypes.string_at(kernel32.LockResource(kernel32.LoadResource(module, found)), size).decode("utf-8")
    finally:
        kernel32.FreeLibrary(module)


def _set_manifest(kernel32: ctypes.WinDLL, executable: Path, manifest: str) -> None:
    data = manifest.encode("utf-8")
    update = kernel32.BeginUpdateResourceW(str(executable), False)
    if not update:
        raise ctypes.WinError(ctypes.get_last_error())
    buffer = ctypes.create_string_buffer(data, len(data))
    if not kernel32.UpdateResourceW(update, ctypes.c_void_p(_RT_MANIFEST), ctypes.c_void_p(_MANIFEST_ID), _ENGLISH, buffer,
                                    len(data)):
        error = ctypes.get_last_error()
        kernel32.EndUpdateResourceW(update, True)   # discard
        raise ctypes.WinError(error)
    if not kernel32.EndUpdateResourceW(update, False):
        raise ctypes.WinError(ctypes.get_last_error())


def _with_code_page(manifest: str) -> str:
    """The manifest with `activeCodePage` naming `LOCALE`, in its `windowsSettings` or in one of its own."""
    setting = f'<activeCodePage xmlns="http://schemas.microsoft.com/SMI/2019/WindowsSettings">{LOCALE}</activeCodePage>'
    if "activeCodePage" in manifest:
        raise ValueError("The interpreter's manifest names a code page already.")
    if "<windowsSettings>" in manifest:
        return manifest.replace("<windowsSettings>", "<windowsSettings>" + setting, 1)
    return manifest.replace("</assembly>", '<application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>' + setting
                            + "</windowsSettings></application></assembly>", 1)


def make_copy(folder: Path) -> tuple[Path, list[Path]]:
    """The copy of the base interpreter in `folder`, running in `CODE_PAGE`, and the junctions made for it."""
    import _winapi

    base = Path(sys._base_executable).parent
    for item in base.iterdir():
        if item.is_file() and (item.name.lower() == "python.exe" or item.suffix.lower() == ".dll"):
            shutil.copy2(item, folder / item.name)
    junctions = []
    for name in ("Lib", "DLLs"):
        if (base / name).is_dir():
            _winapi.CreateJunction(str(base / name), str(folder / name))
            junctions.append(folder / name)
    executable = folder / "python.exe"
    kernel32 = _kernel32()
    _set_manifest(kernel32, executable, _with_code_page(_manifest(kernel32, executable)))
    return executable, junctions


def environment_for(folder: Path, executable: Path) -> dict[str, str]:
    """What the copy is run with: this environment's packages, no stream encoding set from outside, and its own name."""
    site = folder / "site"
    site.mkdir()
    (site / "sitecustomize.py").write_text(_SITECUSTOMIZE.format(site=sysconfig.get_paths()["purelib"]), encoding="utf-8")
    environment = {name: value for name, value in os.environ.items() if name not in STREAM_VARIABLES}
    environment["PYTHONPATH"] = os.pathsep.join(filter(None, [str(ROOT / "src"), str(site), os.environ.get("PYTHONPATH")]))
    environment[COPY_VARIABLE] = str(executable)
    return environment


def _remove(folder: Path, junctions: list[Path]) -> None:
    for junction in junctions:
        os.rmdir(junction)   # the junction itself: what it leads to is the base interpreter's
    shutil.rmtree(folder, ignore_errors=True)


def main(arguments: list[str]) -> int:
    if os.name != "nt":
        print("Code pages are Windows': nothing to run.", file=sys.stderr)
        return 2
    if COPY_VARIABLE in os.environ:
        print("This runs in a copy made by this script already, which is not copied again.", file=sys.stderr)
        return 2
    folder = Path(tempfile.mkdtemp(prefix="code-page-949-"))
    junctions: list[Path] = []
    try:
        executable, junctions = make_copy(folder)
        environment = environment_for(folder, executable)
        probe = "import ctypes; kernel32 = ctypes.windll.kernel32; print(kernel32.GetACP(), kernel32.GetOEMCP())"
        found = subprocess.run([str(executable), "-c", probe], env=environment, capture_output=True, text=True,
                               encoding="ascii", errors="replace", check=False)
        if found.stdout.split() != [str(CODE_PAGE)] * 2:
            print(f"The copy of the interpreter does not run in code page {CODE_PAGE} (ANSI and OEM code pages: "
                  f"{found.stdout.strip() or found.stderr.strip()[-300:]}).", file=sys.stderr)
            return 2
        print(f"Python {sys.version.split()[0]} in code page {CODE_PAGE}: {executable}", flush=True)
        return subprocess.run([str(executable), *arguments], env=environment, check=False).returncode
    finally:
        _remove(folder, junctions)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
