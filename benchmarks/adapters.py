"""How each tool is called. Every adapter takes a file path and returns Markdown (or the tool's closest text output).

Each tool is used through its documented, default entry point — what a new user gets after installing it with all
optional formats enabled. No tool gets hand-tuned options, and none is given a remote model or API key. The one
exception is said where it is made (`opendockit`, for the PDF benchmark suite). kordoc, a Node library, is called the
same way from `worker_kordoc.mjs`: `parse(path)` and its `markdown`.
"""

import os
from pathlib import Path


def opendockit(path: str) -> str:
    from opendockit import convert_file

    if os.environ.get("BENCH_SUITE") == "pdfbench":
        # The PDF benchmark scores the Markdown against a reference. `<!-- Page 3 -->` and `[Image]` are marks OpenDocKit
        # adds and no text of the document, so they are switched off, as in OpenDocKit's own scorecard (evals/run.py).
        from opendockit import ConvertOptions

        return convert_file(path, ConvertOptions(page_markers=False, extract_images=False)).text
    return convert_file(path).text


def markitdown(path: str) -> str:
    from markitdown import MarkItDown

    global _markitdown
    try:
        converter = _markitdown
    except NameError:
        converter = _markitdown = MarkItDown()
    return converter.convert(path).text_content


def docling(path: str) -> str:
    from docling.document_converter import DocumentConverter

    global _docling
    try:
        converter = _docling
    except NameError:
        converter = _docling = DocumentConverter()
    return converter.convert(path).document.export_to_markdown()


def unstructured(path: str) -> str:
    from unstructured.partition.auto import partition

    elements = partition(filename=path)
    try:
        from unstructured.staging.base import elements_to_md

        return elements_to_md(elements)
    except ImportError:  # older releases: join the element texts, keeping tables as the HTML the library produced
        return "\n\n".join(getattr(element.metadata, "text_as_html", None) or element.text for element in elements)


def kreuzberg(path: str) -> str:
    import kreuzberg as library

    config = None
    try:  # ask for Markdown where the installed version lets us
        config = library.ExtractionConfig(output_format="markdown", use_cache=False)  # no cache: every run does the work
    except Exception:  # noqa: BLE001
        config = None
    result = library.extract_file_sync(path, config=config) if config is not None else library.extract_file_sync(path)
    return result.content


ADAPTERS = {"opendockit": opendockit, "markitdown": markitdown, "docling": docling, "unstructured": unstructured,
            "kreuzberg": kreuzberg}


def version(tool: str) -> str:
    from importlib.metadata import version as installed

    return installed({"opendockit": "opendockit", "markitdown": "markitdown", "docling": "docling",
                      "unstructured": "unstructured", "kreuzberg": "kreuzberg"}[tool])


if __name__ == "__main__":
    import sys

    print(ADAPTERS[sys.argv[1]](str(Path(sys.argv[2]))))
