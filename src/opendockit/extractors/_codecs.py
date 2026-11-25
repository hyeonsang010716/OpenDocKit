"""The codecs a document may name: those Python has on every system, the same everywhere.

Windows lends Python codecs that no other system has. Two of them read with the code pages of the machine itself: `mbcs`
(also named `ansi` and `dbcs`), its ANSI code page, and `oem`, its OEM code page; Python 3.12 also gives `mbcs` for a `cpNNN`
of the machine's ANSI code page that has no codec of its own. From Python 3.14 any code page Windows can convert is a codec
by its number (`cp20866`, `cp51949`), `cp0` to `cp3` among them, the ANSI, OEM, Macintosh and thread code pages of the machine.
A document that named one was read in it on Windows only, and with `cp0` to `cp3`, `mbcs` and `oem` in whatever code page the
machine had: the same RTF with `\\ansicpg51949` was `ÇÑ±Û` on Linux, macOS and Windows under Python 3.12 and `한글` on
Windows under Python 3.14, and `\\ansicpg1` was the OEM code page of the machine. `convert` is to give the same text for
the same bytes on every system (core.md), so such a name is read as a name Python does not know: on Windows as everywhere
else (Pinned by: test_codecs.py). The readers ask `portable_codec` where they look a name up themselves, and
`lent_by_windows` where a library does (expat for an XML declaration, xlrd for the code page of a workbook).
"""

import codecs
import encodings
import re


# The codecs of the machine's own code pages (`encodings.mbcs`, `encodings.oem`), whatever name led to them.
_MACHINE_CODECS = frozenset(("mbcs", "oem"))
# The name the codec of a Windows code page has, from Python 3.14 (`encodings._win_cp_codecs`): `cp` and its number.
_CODE_PAGE_NAME = re.compile(r"cp\d+")


def portable_codec(name: str | None) -> str | None:
    """The name Python gives the codec of `name` (`codecs.lookup(name).name`), or None when Python knows no codec of that
    name or only one that Windows lends it.

    A codec is Windows' when it is `mbcs` or `oem`, or when it is named as a code page (`cp20866`) and the codecs of Python's
    own `encodings` package hold none of that name: those are the ones every system has (`cp949`, `cp1252` and `cp437` are
    among them, and so are found again by `encodings.search_function`). Asked of the codec the name leads to, not of the
    name: `ansi` and `dbcs` are other names of `mbcs`. Nothing else changes: on Linux and macOS, where Python has neither
    kind, every name gives what `codecs.lookup` gives, unless a program registers a `cpNNN` codec of its own."""
    if not name:
        return None
    try:
        codec = codecs.lookup(name).name
    except (LookupError, ValueError):  # a name with a NUL byte in it is a ValueError
        return None
    if codec in _MACHINE_CODECS:
        return None
    if _CODE_PAGE_NAME.fullmatch(codec) and encodings.search_function(codec) is None:
        return None
    return codec


def lent_by_windows(name: str | None) -> bool:
    """Whether Python finds a codec of `name` only because Windows lends it (`portable_codec` refuses what `codecs.lookup`
    finds). For a library that looks the name up itself — expat for the encoding of an XML declaration, xlrd for the code page
    of a workbook: where this is True, it is to fail as it fails on every other system, with the `LookupError` of a name
    Python does not know. A name Python does not know at all is False: the library fails on it by itself, everywhere."""
    try:
        codecs.lookup(name)
    except (LookupError, ValueError, TypeError):   # TypeError: no name at all
        return False
    return portable_codec(name) is None
