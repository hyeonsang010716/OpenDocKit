"""`.github/scripts/code_page_949.py`, with which the `code page 949` job of CI runs the suite in the default code page of
Korean Windows: a copy of the interpreter whose manifest names `ko-KR`.

No machine this project was tested on ran in that code page (the runners have 1252), and the eval scripts stopped in it
on the first character it lacks. The copy runs in 949 whatever the system locale, measured on windows-latest (en-US).
"""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "code_page_949.py"
pytestmark = [
    pytest.mark.skipif(not SCRIPT.is_file(), reason="the CI scripts are not part of the source distribution"),
    pytest.mark.skipif(os.name != "nt", reason="code pages are Windows'"),
    # Inside the copy the job runs the suite with: a copy is not copied again (the script refuses it)
    pytest.mark.skipif("OPENDOCKIT_CODE_PAGE_949_COPY" in os.environ, reason="run by the copy this script makes"),
]

# What the copy reports, one value a line: the paths may hold any character, written in cp949 where it runs
_PROBE = ("import ctypes, locale, sys, opendockit\n"
          "kernel32 = ctypes.windll.kernel32\n"
          "print(kernel32.GetACP(), kernel32.GetOEMCP(), locale.getpreferredencoding(False), sys.stdout.encoding)\n"
          "print(sys.executable)\n"
          "print(opendockit.__file__)\n")


def _script():
    spec = importlib.util.spec_from_file_location("code_page_949", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(*arguments: str, **environment: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPT), *arguments], env={**os.environ, **environment}, capture_output=True,
                          timeout=120, check=False)


class TestCodePage949:
    def test_a_command_runs_in_code_page_949_with_the_packages_of_this_environment(self):
        """And with what a machine of default settings has: a stream encoding set from outside is left out."""
        ran = _run("-c", _PROBE, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

        assert ran.returncode == 0, ran.stderr.decode("cp949", "replace")
        header, code_pages, executable, package = ran.stdout.decode("cp949").splitlines()
        assert code_pages.split() == ["949", "949", "cp949", "cp949"]
        assert header.startswith("Python ") and header.endswith(executable)
        assert Path(package) == ROOT / "src" / "opendockit" / "__init__.py"

    def test_the_copy_is_removed_and_the_base_interpreter_is_left_as_it_was(self):
        """The copy leads to the base interpreter's `Lib` and `DLLs` through junctions: those go first and alone."""
        base = Path(sys._base_executable).parent
        before = sorted(path.name for path in (base / "Lib").iterdir())

        ran = _run("-c", "import sys; print(sys.executable)")

        copy = Path(ran.stdout.decode("cp949").splitlines()[-1]).parent
        assert ran.returncode == 0 and copy != base
        assert not copy.exists()
        assert sorted(path.name for path in (base / "Lib").iterdir()) == before
        assert (base / "Lib" / "os.py").is_file() and (base / "python.exe").is_file()

    def test_the_status_is_the_commands(self):
        assert _run("-c", "raise SystemExit(3)").returncode == 3

    def test_a_copy_that_does_not_run_in_code_page_949_runs_nothing(self, monkeypatch, capsys):
        """The suite is never run in another code page as if it were this one: a manifest that names UTF-8 runs in 65001."""
        script = _script()
        monkeypatch.setattr(script, "LOCALE", "UTF-8")

        status = script.main(["-c", "raise SystemExit(0)"])

        assert status == 2
        assert "does not run in code page 949" in capsys.readouterr().err

    def test_the_setting_is_added_to_a_manifest_with_or_without_its_settings(self):
        script = _script()
        bare = '<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0"></assembly>'
        with_settings = '<assembly><application><windowsSettings><longPathAware>true</longPathAware></windowsSettings></application></assembly>'

        assert ">ko-KR</activeCodePage></windowsSettings></application></assembly>" in script._with_code_page(bare)
        assert "<windowsSettings><activeCodePage" in script._with_code_page(with_settings)
        with pytest.raises(ValueError, match="names a code page already"):
            script._with_code_page(script._with_code_page(bare))
