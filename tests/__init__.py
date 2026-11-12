"""Test suite. Sample documents are built in memory by `tests.builder`."""

import pytest

from opendockit.extractors.html import HTML5_TOKENIZER


# Some HTML and mail tests pin behavior that needs the HTML5 tokenizer of libxml2 2.14. lxml's Windows wheels still bundle
# libxml2 2.11, where the same input loses content (docs/limitations.md); the extractor reports that with a warning.
needs_html5_tokenizer = pytest.mark.skipif(not HTML5_TOKENIZER, reason="needs the HTML5 tokenizer of libxml2 2.14 or newer")
