"""JPEG 2000 pictures that stop the process when pdfium draws them (`stops_the_renderer`).

pdfium decodes a JPEG 2000 picture asking the decoder for as many components as the colour space it takes for the
picture has, and stops the process — a check that ends it, not an error it returns — when that is more than the picture
holds (`CJPX_Decoder::Decode`: `CHECK_LE(component_count, image_->numcomps)`); and it reads past the end of a list when an
image mask's picture is one it takes for gray (a bounds check of libc++ ends it). A file of 10 KB ended every program that
converted it in its own process, a server with it. The rules below are those of pdfium 159 (`CPDF_DIB::LoadColorInfo`,
`CPDF_DIB::LoadJpxBitmap`, `JpxDecodeConversion::Create`, `CJPX_Decoder::StartDecode`), each one measured on a picture
built for it (Pinned by: test_pdf_jpx.py::TestTheRendererStops).
"""

from dataclasses import dataclass
from io import BytesIO
import re

from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFObjRef, PDFStream
from pdfminer.psparser import PSLiteral


def _name(text: str) -> bytes:
    """A pattern for the PDF name `/text`, any of whose characters may be written as `#hh`."""
    return b"/" + b"".join(b"(?:%s|#%s[%s%s])" % (re.escape(character.encode()), b"%X" % (ord(character) >> 4),
                                                   b"%X" % (ord(character) & 15), b"%x" % (ord(character) & 15))
                           for character in text)


_JPX_NAME = re.compile(_name("JPXDecode"))
_DEFAULT_SPACE = re.compile(_name("Default") + b"(?:" + _name("RGB")[1:] + b"|" + _name("CMYK")[1:] + b")")
_OBJECT_HEADER = re.compile(rb"(?<!\d)(\d+)\s+\d+\s+obj\b")
# How far before a JPXDecode name its object's header is looked for first, then again (a stream's dictionary is short)
_HEADER_WINDOWS = (4096, 1 << 16)

_JP2_SIGNATURE = b"\x00\x00\x00\x0cjP  \r\n\x87\n"
# What the colour box of a JP2 file names (`opj_jp2_decode`): 16 sRGB, 17 gray, 18 sYCC, 24 e-sYCC, 12 CMYK
_ENUMERATED = {16: "rgb", 17: "gray", 18: "ycc", 24: "eycc", 12: "cmyk"}
# The number of components each family of colour space has, for pdfium (`CPDF_ColorSpace::ComponentCount`)
_FAMILY_COMPONENTS = {"DeviceGray": 1, "CalGray": 1, "Separation": 1, "Indexed": 1, "DeviceRGB": 3, "CalRGB": 3, "Lab": 3,
                      "DeviceCMYK": 4}


@dataclass(frozen=True, slots=True)
class _Picture:
    """What the header of a JPEG 2000 picture says: its components, the colour space its JP2 colour box names ('' for none
    or a profile), and the columns of its palette when it has one and a component map (openjpeg then decodes as many)."""

    components: int
    space: str
    palette: int


def stops_the_renderer(data: bytes) -> bool:
    """Whether pdfium would stop the process drawing a page of this PDF: it holds a JPEG 2000 picture whose colour space
    asks for more components than the picture has, or an image mask whose picture pdfium takes for gray. Every stream
    whose last filter is JPXDecode counts, wherever it is drawn from (a page, a form, an annotation, a pattern, a mask);
    a picture written inside a page's content is not decoded by pdfium and does not count.

    A picture whose header cannot be read, whose colour space cannot be told, or that lies under another filter it cannot
    undo counts as one that stops it: pdfium then draws nothing of it anyway. A file that holds no JPXDecode name (also
    written with `#hh`) is decided by that search alone."""
    matches = list(_JPX_NAME.finditer(data))
    if not matches:
        return False
    try:
        document = PDFDocument(PDFParser(BytesIO(data)))
    except Exception:  # noqa: BLE001 - a file pdfminer cannot open: its pictures cannot be judged
        return True
    defaults = _DEFAULT_SPACE.search(data) is not None
    judged: set[int] = set()
    for match in matches:
        objid = _holder(data, match.start())
        if objid is None:
            return True
        if objid in judged:
            continue
        judged.add(objid)
        try:
            stream = document.getobj(objid)
        except Exception:  # noqa: BLE001 - an object pdfminer cannot read
            return True
        # An object that is not a stream decoded with JPXDecode is not the one the name was read in: a picture written
        # inside a page's content, which pdfium does not decode, or an older revision of an object the file has since
        # replaced, which nothing draws. Known accepted gap: a name pdfminer gives to no such object is not judged (a file
        # made to look otherwise); `IsolatedConverter` is what keeps a server from the crash of a library
        if not isinstance(stream, PDFStream) or not any(_is(name, "JPXDecode") for name, _ in _filters(stream)):
            continue
        if _stops(stream, document, defaults):
            return True
    return False


def _holder(data: bytes, position: int) -> int | None:
    """The number of the object a name at `position` is read in: the one whose header comes last before it. It is looked
    for in a short stretch before the name first: found in the whole file, the headers of a 21 MB scan took 170 ms, the
    digits of its pictures read as numbers. The last header in a stretch that ends at the name is the last before it."""
    for window in (*_HEADER_WINDOWS, position):
        last = None
        for last in _OBJECT_HEADER.finditer(data, max(position - window, 0), position):
            pass
        if last is not None:
            return int(last.group(1))
    return None


def _stops(stream: PDFStream, document: PDFDocument, defaults: bool) -> bool:
    """Whether drawing this JPEG 2000 stream stops pdfium."""
    filters = _filters(stream)
    if not _is(filters[-1][0], "JPXDecode") or any(_is(name, "JPXDecode") for name, _ in filters[:-1]):
        return True  # pdfium decodes JPEG 2000 only as the last filter; anything else is not judged
    try:
        picture = _picture(stream.get_data())
    except Exception:  # noqa: BLE001 - data that cannot be undone or decrypted
        return True
    if picture is None:
        return True
    mask = _resolve(stream.attrs.get("ImageMask"), document) is True
    space = None if mask else _resolve(stream.attrs.get("ColorSpace"), document)
    indexed = _family(space, document) == "Indexed"
    count = picture.palette if picture.palette and not indexed else picture.components
    taken = _decoded_space(picture, count)
    if space is None:
        # pdfium takes the colour space from the picture; an image mask is read the same way, and reads past the end of
        # a list when that is gray
        asked = {"gray": 1, "rgb": 3, "ycc": 3, "eycc": 3, "cmyk": 4}.get(taken, count)  # '' unknown: as many as it has
        return asked > count or (mask and taken == "gray")
    family = _family(space, document)
    if family == "Pattern":
        return False  # no space for a picture: pdfium decodes nothing
    if family == "DeviceRGB" and not defaults:
        return False  # pdfium refuses an RGB picture of fewer than three components instead of decoding it
    if family == "DeviceCMYK" and not defaults:
        # refused unless the picture's own space is CMYK or unknown; one of one or two components is gray
        return count < 4 and taken in ("", "cmyk")
    asked = _components(space, family, document)
    return asked is None or asked > count


def _decoded_space(picture: _Picture, count: int) -> str:
    """The colour space pdfium takes the decoded picture for (`CJPX_Decoder::StartDecode`): one of one or two components is
    gray, whatever its colour box says. (pdfium also takes a picture of three components whose second is subsampled for
    YCC, and turns some YCC into RGB: YCC and RGB both ask for three components and neither is CMYK, so the rules below do
    not tell them apart.)"""
    return "gray" if count <= 2 else picture.space


def _picture(code: bytes) -> _Picture | None:
    """The header of a JPEG 2000 codestream or JP2 file; None when it cannot be read as openjpeg reads it."""
    if code[:len(_JP2_SIGNATURE)] != _JP2_SIGNATURE:
        return _codestream(code, "", 0)
    space, palette, mapped, position = "", 0, False, 0
    while position + 8 <= len(code):
        length, kind = int.from_bytes(code[position:position + 4], "big"), code[position + 4:position + 8]
        head = 8
        if length == 1:
            length, head = int.from_bytes(code[position + 8:position + 16], "big"), 16
        elif length == 0:
            length = len(code) - position
        if length < head:
            return None
        body = code[position + head:position + length]
        if kind == b"jp2h":
            inner = 0
            while inner + 8 <= len(body):
                size, box = int.from_bytes(body[inner:inner + 4], "big"), body[inner + 4:inner + 8]
                if size < 8:
                    return None
                content = body[inner + 8:inner + size]
                if box == b"colr" and not space and len(content) >= 7:
                    # only the first colour box counts; one with a profile instead of a name leaves the space unknown
                    space = _ENUMERATED.get(int.from_bytes(content[3:7], "big"), "?") if content[0] == 1 else "?"
                elif box == b"pclr" and len(content) >= 3:
                    palette = content[2]
                elif box == b"cmap":
                    mapped = True
                inner += size
        elif kind == b"jp2c":
            return _codestream(body, space, palette if mapped else 0)
        position += length
    return None


def _codestream(code: bytes, space: str, palette: int) -> _Picture | None:
    """The components of a codestream from its SIZ marker, which follows its first marker."""
    if code[:4] != b"\xff\x4f\xff\x51" or len(code) < 42:
        return None
    components = int.from_bytes(code[40:42], "big")
    if not 1 <= components <= 16384 or len(code) < 42 + 3 * components:
        return None
    return _Picture(components, "" if space == "?" else space, palette)


def _filters(stream: PDFStream) -> list[tuple[object, object]]:
    try:
        return stream.get_filters() or [("", None)]
    except Exception:  # noqa: BLE001 - a filter entry that is no name
        return [("", None)]


def _is(name: object, text: str) -> bool:
    return (name.name if isinstance(name, PSLiteral) else name) == text


def _resolve(value: object, document: PDFDocument, depth: int = 0) -> object:
    while isinstance(value, PDFObjRef) and depth < 32:
        try:
            value = document.getobj(value.objid)
        except Exception:  # noqa: BLE001
            return None
        depth += 1
    return value


def _family(space: object, document: PDFDocument) -> str | None:
    """The family of a colour space: its name, or the name its array starts with; None when it is neither."""
    space = _resolve(space, document)
    if isinstance(space, list) and space:
        space = _resolve(space[0], document)
    if isinstance(space, PSLiteral):
        return str(space.name)
    return None


def _components(space: object, family: str | None, document: PDFDocument) -> int | None:
    """How many components pdfium takes the colour space to have; None when that cannot be told (a name the page's
    resources would have to give, an ICC profile of no number of components)."""
    if family in _FAMILY_COMPONENTS:
        return _FAMILY_COMPONENTS[family]
    space = _resolve(space, document)
    if not isinstance(space, list) or len(space) < 2:
        return None
    if family == "DeviceN":
        names = _resolve(space[1], document)
        return len(names) if isinstance(names, list) and names else None
    if family == "ICCBased":
        profile = _resolve(space[1], document)
        count = _resolve(profile.attrs.get("N"), document) if isinstance(profile, PDFStream) else None
        return count if isinstance(count, int) and 1 <= count <= 32 else None
    return None
