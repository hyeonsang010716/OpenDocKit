"""How pytest-xdist starts its workers: the code execnet sends them reaches them whatever the code page and the path
(`send_execnet_code_as_ascii` of `tests/conftest.py`).

A worker reads that code from a pipe in the locale encoding. `PYTHONIOENCODING=cp949` makes it read in code page 949, the
default of Korean Windows, on any system, so these tests run everywhere.
"""

import sys

import pytest


execnet = pytest.importorskip("execnet")

from execnet import gateway_bootstrap, gateway_io  # noqa: E402

from tests import conftest  # noqa: E402


class _Written:
    """The pipe to a worker: what was written to it."""

    def __init__(self):
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data


def _sent(*sources: str) -> bytes:
    pipe = _Written()
    gateway_bootstrap.sendexec(pipe, *sources)
    return pipe.data


class TestXdistWorkers:
    def test_a_worker_reads_a_path_with_hangul_in_code_page_949(self, monkeypatch, tmp_path):
        """The path of a checkout under the Desktop that OneDrive keeps as `바탕 화면`: the worker ended at once, before a
        test ran, when it read it in 949."""
        folder = tmp_path / "바탕 화면"
        folder.mkdir()
        monkeypatch.setattr(gateway_bootstrap, "importdir", str(folder))   # the path the code names, put first on sys.path
        monkeypatch.setenv("PYTHONIOENCODING", "cp949")

        gateway = execnet.makegateway()
        try:
            assert gateway.remote_exec("import sys; channel.send(sys.path[0])").receive() == str(folder)
        finally:
            gateway.exit()

    def test_ascii_code_is_sent_as_before(self):
        """Under an ASCII path the bytes are those execnet writes itself: `repr` of the code, as UTF-8."""
        sources = ("import sys", "if 'C:\\\\a b\\\\.venv' not in sys.path:", "    sys.path.insert(0, 'C:\\\\a b\\\\.venv')",
                   "text = '\\t\\x00\\'\"'")

        assert _sent(*sources) == (repr("\n".join(sources)) + "\n").encode("utf-8")

    def test_code_that_is_not_ascii_is_one_ascii_line_that_evaluates_to_it(self):
        source = "sys.path.insert(0, 'C:\\\\Users\\\\바탕 화면\\\\.venv')\nprint('\u00e9\u4e2d\U0001f600')"

        line = _sent(source)

        assert line.isascii()
        assert line.count(b"\n") == 1 and line.endswith(b"\n")
        for code_page in ("cp949", "cp1252", "cp932", "utf-8"):
            assert eval(line.decode(code_page)) == source

    def test_the_suite_sends_it_so(self):
        """`tests/conftest.py` has done it before the workers of this run were started."""
        assert gateway_bootstrap.sendexec.__qualname__ == "send_execnet_code_as_ascii.<locals>.sendexec"
        assert gateway_io.popen_bootstrapline == conftest.EXECNET_WORKER_LINE

    def test_another_execnet_is_left_as_it_is(self, monkeypatch):
        own = object()
        monkeypatch.setattr(gateway_bootstrap, "sendexec", own)
        monkeypatch.setattr(gateway_io, "popen_bootstrapline", "import sys;exec(sys.stdin.readline())")

        assert conftest.send_execnet_code_as_ascii() is False
        assert gateway_bootstrap.sendexec is own

    def test_without_pytest_xdist_nothing_is_done(self, monkeypatch):
        own = object()
        monkeypatch.setattr(gateway_bootstrap, "sendexec", own)
        monkeypatch.setitem(sys.modules, "execnet", None)   # not installed

        assert conftest.send_execnet_code_as_ascii() is False
        assert gateway_bootstrap.sendexec is own

    def test_it_is_done_where_execnet_starts_its_workers_with_that_line(self, monkeypatch):
        own = object()
        monkeypatch.setattr(gateway_bootstrap, "sendexec", own)

        assert conftest.send_execnet_code_as_ascii() is True
        assert gateway_bootstrap.sendexec is not own
