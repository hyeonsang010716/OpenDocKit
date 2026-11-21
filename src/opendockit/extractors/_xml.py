"""XML parsers of lxml, one for each thread, and a string dictionary of its own for each thread that converts."""

from collections.abc import Callable
import threading

from lxml import etree


def thread_parser(**options: bool) -> Callable[[], etree.XMLParser]:
    """A function that gives the calling thread its own parser with these options, made on its first call there.

    lxml gives a thread that has no string dictionary yet the one of the parser it first parses with, which for a parser kept
    by a module is the dictionary of the thread that used it last: threads converting at once were left sharing one, which
    libxml2 does not allow, and names in their parses came out wrong (`own_dictionary`). A parser for each thread keeps the
    parsers of this library out of that (7 of 200 runs of eight threads converting different formats failed with the one
    `_package` parser; none of 300) (Pinned by: test_converter.py::TestXmlParsersPerThread)."""
    local = threading.local()

    def parser() -> etree.XMLParser:
        try:
            return local.parser
        except AttributeError:
            local.parser = etree.XMLParser(**options)
            return local.parser

    return parser


_OWNED = threading.local()


def own_dictionary() -> None:
    """Make sure the calling thread has a string dictionary of lxml of its own: its first parse goes through a parser made for
    it. Called before each conversion (`converter._converted`).

    A thread whose first parse went through a parser a library keeps for every thread (python-pptx, python-docx, openpyxl)
    took the dictionary of the thread that used that parser last, and kept it: two threads converting at once then added names
    to one dictionary, which libxml2 does not allow. A valid DOCX or XLSX came out as corrupted, a DOCX or HWPX with other
    text, or the process stopped on a bad access (a pool whose threads first converted a PPTX: 18 of 150 runs; four of five
    runs of six threads parsing after one crashed). Known accepted gap: such a parser still hands dictionaries on, so a thread
    that parsed through one before it first converts keeps the dictionary it took, and a thread outside the library whose first
    parse goes through one can take a converting thread's (Pinned by: test_converter.py::TestXmlParsersPerThread::
    test_threads_converting_after_a_shared_parser_keep_their_own_dictionaries)."""
    if not getattr(_OWNED, "done", False):
        etree.fromstring(b"<x/>", etree.XMLParser())
        _OWNED.done = True
