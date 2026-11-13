"""What every adapter hands on: the text and metadata of a conversion result, whole or one piece per section.

Kept free of any framework so that the LangChain loader, the LlamaIndex reader and the MCP server cut a document the same
way and name things the same.
"""

from collections.abc import Iterator
from typing import Literal

from opendockit.converter import section_heading
from opendockit.models import ConversionResult, SectionKind


type Mode = Literal["single", "sections"]
MODES: tuple[str, ...] = ("single", "sections")


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

    # A document without physical units is one piece either way.
    whole = mode == "single" or all(section.kind is SectionKind.DOCUMENT for section in result.sections)
    if whole:
        if result.text.strip():
            yield result.text, base
        return
    for section in result.sections:
        if not section.text.strip():
            continue
        heading = section_heading(section)
        metadata = {**base, "section_kind": section.kind.value, "section_index": section.index}
        if section.title:
            metadata["section_title"] = section.title
        yield (f"{heading}\n\n{section.text}" if heading else section.text), metadata
