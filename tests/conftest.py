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


# A failed timing test is run up to twice more, a second apart, and passes if one of the runs does. Only a timing test: elsewhere a
# second chance would hide a real bug that shows up only sometimes. A stall of the shared CI runner fails one run; code that
# became slow fails all three (Pinned by: test_timing.py::TestTimingTestsAreRunAgain).
TIMING_RERUNS = 2
TIMING_RERUN_DELAY = 1.0


def pytest_collection_modifyitems(config, items):
    if not config.pluginmanager.hasplugin("rerunfailures"):  # switched off (`-p no:rerunfailures`): every run counts
        return
    for item in items:
        if item.get_closest_marker("timing") is not None:
            item.add_marker(pytest.mark.flaky(reruns=TIMING_RERUNS, reruns_delay=TIMING_RERUN_DELAY))
