"""What every adapter hands on: the text and metadata of a conversion result, whole or one piece per section.

Kept free of any framework so that the LangChain loader, the LlamaIndex reader and the MCP server cut a document the same
way and name things the same.
"""

from collections.abc import Iterator
import logging
from typing import Literal

from opendockit.converter import section_heading
from opendockit.models import ConversionResult, SectionKind


type Mode = Literal["single", "sections"]
MODES: tuple[str, ...] = ("single", "sections")
_LOG = logging.getLogger("opendockit.integrations")


def check_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be 'single' or 'sections', not {mode!r}")


def pieces(result: ConversionResult, source: str | None, mode: Mode) -> Iterator[tuple[str, dict[str, str | int | bool]]]:
    """`(text, metadata)` for the whole document (`single`) or for every page, slide or sheet that has text (`sections`).

    Metadata values are plain strings, integers and booleans, because vector stores reject anything else: the warnings are
    one string with a line per warning. A piece of a slide or sheet starts with the heading the full text gives it
    (`## Sheet: Sales`), so the piece says what it is when it is read alone; a page has no heading (its number is metadata).
    (Pinned by: test_integrations.py::TestLangChainLoader::test_metadata_values_are_plain_scalars,
    test_sections_mode_gives_a_document_per_sheet_slide_and_page.)
    """
    base: dict[str, str | int | bool] = {"format": result.format.value, **result.metadata}
    if source:
        base["source"] = source
    if result.warnings:
        base["warnings"] = "\n".join(result.warnings)
    if result.exceeded:
        base["exceeded"] = True

    # What there is to hand on: sections with text, and slides that are only a title (a divider or agenda slide — its title
    # is its content). A scanned PDF has neither: read whole it is nothing but page markers, so it gives no piece in either
    # mode rather than a Document of `<!-- Page 1 -->` lines.
    kept = [section for section in result.sections
            if section.text.strip() or (section.kind is SectionKind.SLIDE and section.title)]
    count = 0
    if mode == "single" or all(section.kind is SectionKind.DOCUMENT for section in result.sections):
        # A document without physical units is one piece either way.
        if kept and result.text.strip():
            count = 1
            yield result.text, base
    else:
        for section in kept:
            heading = section_heading(section)
            metadata = {**base, "section_kind": section.kind.value, "section_index": section.index}
            if section.title:
                metadata["section_title"] = section.title
            count += 1
            yield "\n\n".join(part for part in (heading, section.text) if part), metadata
    if not count:
        # A file that gives no piece would otherwise be missing from the index without a word — a scanned PDF read page by
        # page, an empty workbook. The warnings have nowhere to travel, so they are logged
        # (Pinned by: test_integrations.py::TestLangChainLoader::test_file_without_any_text_gives_no_document_and_a_logged_warning).
        _LOG.warning("%s gave no text and was left out: %s", source or "A document",
                     " ".join(result.warnings) or "No text was extracted.")
