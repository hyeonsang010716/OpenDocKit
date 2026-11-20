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
"""

from collections.abc import Callable
from importlib import import_module
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
