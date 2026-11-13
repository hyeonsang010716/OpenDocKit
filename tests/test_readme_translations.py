"""The translated READMEs say the same thing as README.md.

A translation goes stale silently: a new option or a new benchmark figure is added to the English page and the other two
keep the old one. Prose cannot be compared by a program, but everything that is not prose can: the code, the links, the
figures, and the shape of the page.
"""

from pathlib import Path
import re

import pytest


ROOT = Path(__file__).parent.parent
TRANSLATIONS = ("README.ko.md", "README.zh-CN.md")
pytestmark = pytest.mark.skipif(not all((ROOT / name).exists() for name in TRANSLATIONS),
                                reason="the translated READMEs are not part of the source distribution")

_FENCE = re.compile(r"```(\w*)\n(.*?)```", re.DOTALL)
_COMMENT = re.compile(r"\s+#\s.*$", re.MULTILINE)
_LINK = re.compile(r"https?://[^\s)\"<>]+")
_OTHER_README = re.compile(r"README(?:\.[\w-]+)?\.md\Z")
_NUMERAL = re.compile(r"(?<![A-Za-z0-9.])\d+(?:\.\d+)*(?![A-Za-z0-9])")


def _page(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def _code(text: str) -> list[tuple[str, str]]:
    """The fenced blocks without their comments: comments are translated, the code is not."""
    return [(language, _COMMENT.sub("", body).strip()) for language, body in _FENCE.findall(text)]


def _links(text: str) -> list[str]:
    """Every address in order, apart from the links between the READMEs themselves."""
    return [link for link in _LINK.findall(text) if not _OTHER_README.search(link)]


def _shape(text: str) -> dict[str, int]:
    lines = _FENCE.sub("", text).split("\n")
    return {"h2": sum(line.startswith("## ") for line in lines), "h3": sum(line.startswith("### ") for line in lines),
            "table rows": sum(line.startswith("|") for line in lines), "list items": sum(line.startswith("- ") for line in lines)}


def _figures(text: str) -> list[str]:
    """The numerals of the prose, sorted (code and addresses left out)."""
    return sorted(_NUMERAL.findall(_LINK.sub("", _FENCE.sub("", text))))


@pytest.mark.parametrize("name", TRANSLATIONS)
class TestTranslatedReadme:
    def test_code_blocks_are_the_same(self, name: str):
        assert _code(_page(name)) == _code(_page("README.md"))

    def test_links_are_the_same_and_in_the_same_order(self, name: str):
        assert _links(_page(name)) == _links(_page("README.md"))

    def test_page_has_the_same_shape(self, name: str):
        """As many headings of each level, table rows and list items as the English page."""
        assert _shape(_page(name)) == _shape(_page("README.md"))

    def test_figures_are_the_same(self, name: str):
        """The numbers a reader acts on — benchmark results, versions, limits. A figure changed in one page and not in the
        other is caught."""
        # "about thirty times faster" and "about a second" are words in English and in Chinese, numerals in Korean.
        spelled_out = {"README.ko.md": ["30", "1"], "README.zh-CN.md": []}[name]
        assert _figures(_page(name)) == sorted([*_figures(_page("README.md")), *spelled_out])

    def test_every_page_links_to_the_other_two_and_names_the_english_page_as_authoritative(self, name: str):
        pages = ("README.md", *TRANSLATIONS)
        for page in pages:
            others = [other for other in pages if other != page]
            assert all(f"/blob/main/{other}" in _page(page) for other in others), page
        assert "/blob/main/README.md" in _page(name).rsplit("\n---\n", 1)[-1]
