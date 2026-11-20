"""Per-format extractors. An extractor implements just one thing: `(bytes, ConvertOptions) -> Extraction`.

The module of an extractor is imported the first time a document of its format is converted (`_on_first_use`), not when the
package is. Importing all of them pulled openpyxl, BeautifulSoup, pdfplumber, pypdfium2 and python-pptx into every process:
`import opendockit` took 0.2-0.3 s and 100 MB before a single page was read, against 0.03 s and 30 MB for the DOCX reader
alone, and a command-line run or a Windows worker (a full interpreter start per file) paid it for every document. A process
that wants them all up front — the forkserver of `IsolatedConverter` — imports `opendockit.extractors.all`
(Pinned by: test_converter.py::TestExtractorsImportedOnFirstUse).

Every import made on first use goes through `load`, under one lock. Threads that converted their first EPUB, XLSX or HTML at
the same moment imported BeautifulSoup or openpyxl at once, and Python's import machinery stopped one of them with a
`_DeadlockError` (those libraries import their own modules in a circle), which the extractor reported as a corrupted file
(Pinned by: test_converter.py::TestExtractorsImportedOnFirstUse::test_threads_converting_their_first_documents_at_once).

A fork waits until no import on first use is under way (`os.register_at_fork`, `_hold_imports_over_fork`). A process forked
from another thread while one was importing a reader got the lock held by a thread that does not exist in it, and its first
document of a format not yet loaded waited for ever; a child that also gave the lock back still waited for ever on Python's
own lock of the half-imported library, for the format being imported
(Pinned by: test_converter.py::TestImportsOnFirstUseAndFork).
"""

from collections.abc import Callable
from importlib import import_module
import logging  # noqa: F401 — imported before the fork hooks are registered, see `_hold_imports_over_fork`
import os
import threading
from types import ModuleType

from opendockit.models import ConvertOptions, DocumentFormat, Extraction


Extractor = Callable[[bytes, ConvertOptions], Extraction]

# Format → (module in this package, its extractor function)
EXTRACTOR_MODULES: dict[DocumentFormat, tuple[str, str]] = {
    DocumentFormat.PDF: ("pdf", "extract_pdf"),
    DocumentFormat.DOCX: ("docx", "extract_docx"),
    DocumentFormat.PPTX: ("pptx", "extract_pptx"),
    DocumentFormat.XLSX: ("spreadsheet", "extract_xlsx"),
    DocumentFormat.XLS: ("xls", "extract_xls"),
    DocumentFormat.XML_SPREADSHEET: ("xml_spreadsheet", "extract_xml_spreadsheet"),
    DocumentFormat.CSV: ("csv_text", "extract_csv"),
    DocumentFormat.HWP: ("hwp", "extract_hwp"),
    DocumentFormat.HWPX: ("hwpx", "extract_hwpx"),
    DocumentFormat.HTML: ("html", "extract_html"),
    DocumentFormat.MARKDOWN: ("text", "extract_markdown"),
    DocumentFormat.TEXT: ("text", "extract_text"),
    DocumentFormat.IMAGE: ("image", "extract_image"),
    DocumentFormat.ODT: ("odf", "extract_odt"),
    DocumentFormat.ODS: ("odf", "extract_ods"),
    DocumentFormat.ODP: ("odf", "extract_odp"),
    DocumentFormat.EPUB: ("epub", "extract_epub"),
    DocumentFormat.RTF: ("rtf", "extract_rtf"),
    DocumentFormat.DOC: ("legacy", "extract_doc"),
    DocumentFormat.PPT: ("legacy", "extract_ppt"),
}


# Held while a module is imported on first use (`load`); reentrant, as importing one may load another
_IMPORTING = threading.RLock()


def _hold_imports_over_fork() -> None:
    """Make `os.fork` take `_IMPORTING` before it forks and give it back on both sides after, where the platform forks.

    The thread that forks holds the lock across the fork, so the child starts with every import on first use either finished
    or not begun, and with the lock held by its own one thread, which gives it back. A thread that forks while it holds the
    lock itself (a library that forks while it is being imported) takes it again, as it is reentrant. Hooks run before a fork
    in the reverse order of their registration: `logging`, whose hook takes its module lock, is imported first, so that lock
    is not yet held while the fork waits for an import that logs. `subprocess` without `preexec_fn` and the start of the
    forkserver do not run these hooks; the forkserver forks its workers from its one thread, which holds the lock only while
    it preloads
    (Pinned by: test_converter.py::TestImportsOnFirstUseAndFork)."""
    if hasattr(os, "register_at_fork"):  # not on Windows, which spawns
        os.register_at_fork(before=_IMPORTING.acquire, after_in_parent=_IMPORTING.release,
                            after_in_child=_IMPORTING.release)


_hold_imports_over_fork()


def load(module: str) -> ModuleType:
    """Module `module` (its full name), imported under the lock every import on first use shares."""
    with _IMPORTING:
        return import_module(module)


def _on_first_use(module: str, name: str) -> Extractor:
    """The extractor `name` of `module`, imported when it is first called (`load`) and kept from then on."""
    found: list[Extractor] = []

    def extract(data: bytes, options: ConvertOptions) -> Extraction:
        if not found:
            found.append(getattr(load(f"{__name__}.{module}"), name))
        return found[0](data, options)

    extract.__name__ = extract.__qualname__ = name
    return extract


EXTRACTORS: dict[DocumentFormat, Extractor] = {
    document_format: _on_first_use(module, name) for document_format, (module, name) in EXTRACTOR_MODULES.items()
}
