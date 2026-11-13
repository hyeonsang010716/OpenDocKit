import pytest


@pytest.fixture(autouse=True)
def _no_old_parser_warning(monkeypatch):
    """Keep the "HTML parser older than libxml2 2.14" warning out of results during tests.

    On a platform whose lxml bundles the older parser (Windows), the extractor adds that warning to every HTML result, which
    would fail the many tests that compare `result.warnings` exactly. The tests whose content really depends on the newer
    parser are skipped there by `tests.needs_html5_tokenizer`; the warning itself is pinned by
    test_core_html.py::TestOlderHtmlParser, which switches it back on.
    """
    monkeypatch.setattr("opendockit.extractors.html.HTML5_TOKENIZER", True)


BOUNDARIES: list[str] = []
_PROBE: dict = {}


def pytest_runtest_setup(item):
    import io
    import time
    import zipfile

    from lxml import etree

    from opendockit import convert
    from tests import builder

    if not item.config.getoption("-m") == "timing":
        return
    name = item.module.__name__
    if _PROBE.get("module") == name:
        return
    _PROBE["module"] = name
    if "data" not in _PROBE:
        body = "".join(f'<hp:p><hp:run><hp:ctrl><hp:fieldBegin id="{index}" type="MEMO"><hp:subList><hp:p><hp:run>'
                       f"<hp:t>메모{index}</hp:t></hp:run></hp:p></hp:subList></hp:fieldBegin></hp:ctrl></hp:run></hp:p>"
                       for index in range(8_000))
        _PROBE["data"] = builder.hwpx_bytes(body)
        with zipfile.ZipFile(io.BytesIO(_PROBE["data"])) as archive:
            _PROBE["xml"] = max((archive.read(member) for member in archive.namelist()), key=len)

    def spent(work) -> float:
        started = time.perf_counter()
        work()
        return time.perf_counter() - started

    parse = spent(lambda: etree.fromstring(_PROBE["xml"]))
    allocate = spent(lambda: [{"number": number, "text": str(number)} for number in range(200_000)])
    first = spent(lambda: convert(_PROBE["data"], "a.hwpx"))
    second = spent(lambda: convert(_PROBE["data"], "a.hwpx"))
    BOUNDARIES.append(f"{name.removeprefix('tests.test_')}: parse {parse:.3f} alloc {allocate:.3f} convert {first:.3f} again {second:.3f}")
