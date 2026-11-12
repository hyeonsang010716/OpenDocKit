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
