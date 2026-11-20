"""XML parsers of lxml, one for each thread."""

from collections.abc import Callable
import threading

from lxml import etree


def thread_parser(**options: bool) -> Callable[[], etree.XMLParser]:
    """A function that gives the calling thread its own parser with these options, made on its first call there.

    One parser kept by a module and used by threads converting at once made the parses of other threads fail: python-docx read
    a sound `settings.xml` as 'Namespace prefix w on settings is not defined', openpyxl a sound stylesheet as holding a DOCX
    element, and a valid file came out as corrupted (7 of 200 runs of eight threads converting DOCX, XLSX, PPTX, PDF, HWPX,
    HTML, EPUB and mail; none of 300 with a parser per thread) (Pinned by: test_converter.py::TestXmlParsersPerThread)."""
    local = threading.local()

    def parser() -> etree.XMLParser:
        try:
            return local.parser
        except AttributeError:
            local.parser = etree.XMLParser(**options)
            return local.parser

    return parser
