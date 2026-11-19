"""What every adapter hands on: the text and metadata of a conversion result, whole or one piece per section.

Kept free of any framework so that the LangChain loader, the LlamaIndex reader and the MCP server cut a document the same
way and name things the same.
"""

from collections.abc import Iterator
import logging
import re
from typing import Literal

from opendockit.converter import section_heading
from opendockit.models import ConversionResult, SectionKind


type Mode = Literal["single", "sections"]
MODES: tuple[str, ...] = ("single", "sections")
_LOG = logging.getLogger("opendockit.integrations")
# What says nothing on its own (`_says_something`): a placeholder for content that was not read — a picture with no alt text or
# description, a chart, video or OLE object the converter could not read —, the bare cell of a sheet picture (`(cell C2)`; with
# the text of its row, `(cell C2: 제품A)`, it says something), and a page marker. A sheet of pictures only gave a Document of
# `(cell C2)` and `[Image]` lines (Pinned by: test_integrations.py::TestLangChainLoader::
# test_pictures_without_a_description_give_no_document).
_NOTHING_SAID = re.compile(r"\[(?:Image|Chart|Video|OLE object)\]|\(cell [A-Z]+\d+\)|<!-- Page \d+ -->")
_BARE_PICTURE = re.compile(r"\[Image\]")


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
    # Over the character limit only the sections the cut text reaches are handed on: a slide past the cut keeps its title
    # and looks like a slide that is only a title, so a 20-slide deck cut after slide 16 gave pieces for slides 17-20
    # (Pinned by: test_integrations.py::TestLangChainLoader::test_sections_past_the_character_limit_give_no_piece).
    reached = result.sections if result.reached_sections is None else result.sections[:result.reached_sections]
    # A piece of nothing but bare picture placeholders says as little: a picture file, or a scan, with no describer gave a
    # Document of `[Image]` (Pinned by: test_integrations.py::TestLangChainLoader::
    # test_pictures_without_a_description_give_no_document). A placeholder with alt text or a description is kept.
    kept = [section for section in reached
            if _says_something(section.text) or (section.kind is SectionKind.SLIDE and section.title)]
    count = 0
    if mode == "single" or all(section.kind is SectionKind.DOCUMENT for section in result.sections):
        # A document without physical units is one piece either way.
        if kept and _says_something(result.text):
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
                     " ".join(result.warnings) or ("It holds only pictures, and they were not described."
                                                   if _BARE_PICTURE.search(result.text) else "No text was extracted."))


def _says_something(text: str) -> bool:
    """Is there text besides bare placeholders, picture cells and page markers (`_NOTHING_SAID`)?"""
    return bool(_NOTHING_SAID.sub("", text).strip())
