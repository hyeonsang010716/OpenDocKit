"""JPEG 2000 pictures that stop the process when pdfium draws them (`stops_the_renderer`).

pdfium decodes a JPEG 2000 picture asking the decoder for as many components as the colour space it takes for the
picture has, and stops the process — a check that ends it, not an error it returns — when that is more than the picture
holds (`CJPX_Decoder::Decode`: `CHECK_LE(component_count, image_->numcomps)`); and it reads past the end of a list when an
image mask's picture is one it takes for gray (a bounds check of libc++ ends it). A file of 10 KB ended every program that
converted it in its own process, a server with it. The rules below follow pdfium 159's code (`CPDF_DIB::LoadColorInfo`,
`CPDF_DIB::LoadJpxBitmap`, `CPDF_DocPageData::GetColorSpaceInternal`, `CPDF_ColorSpace::Load`, `GetDecoderArray`,
`JpxDecodeConversion::Create`, `CJPX_Decoder::StartDecode`), each one measured on a picture built for it and drawn in a
process of its own (Pinned by: test_pdf_jpx.py::TestTheRendererStops). They are a defence against a file that happens to
hold such a picture; a file made to stop pdfium can find other ways, which is why a server converts in
`IsolatedConverter`.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from io import BytesIO
import re

from pdfminer.pdfdocument import PDFDocument, PDFObjectNotFound
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import PDFObjRef, PDFStream
from pdfminer.psparser import PSLiteral


def _name(text: str) -> bytes:
    """A pattern for the PDF name `/text`, any of whose characters may be written as `#hh` (in either case)."""
    return b"/" + b"".join(b"(?:%s|#%s[%s%s])" % (re.escape(character.encode()), b"%X" % (ord(character) >> 4),
                                                   b"%X" % (ord(character) & 15), b"%x" % (ord(character) & 15))
                           for character in text)


_JPX_NAME = re.compile(_name("JPXDecode"))
_DEFAULT_NAME = re.compile(_name("Default") + b"(?:" + b"|".join(_name(name)[1:] for name in ("Gray", "RGB", "CMYK")) + b")")
_OBJECT_HEADER = re.compile(rb"(?<!\d)(\d+)\s+\d+\s+obj\b")
# How far before a JPXDecode name its object's header is looked for first, then again (a stream's dictionary is short);
# never further back than the name before it, whose object it then is
_HEADER_WINDOWS = (4096, 1 << 16)
# Beyond this many JPXDecode names, or objects to read, the document is not judged and counts as one that stops pdfium
_MAX_NAMES = 50_000
_MAX_OBJECTS = 200_000

_JP2_SIGNATURE = b"\x00\x00\x00\x0cjP  \r\n\x87\n"
# What the colour box of a JP2 file names (`opj_jp2_decode`): 16 sRGB, 17 gray, 18 sYCC, 24 e-sYCC, 12 CMYK
_ENUMERATED = {16: "rgb", 17: "gray", 18: "ycc", 24: "eycc", 12: "cmyk"}
# The components pdfium asks for when the picture has no colour space and it takes the one the picture names
_ASKED_BY_PICTURE = {"gray": 1, "rgb": 3, "ycc": 3, "eycc": 3, "cmyk": 4}
# pdfium's stock spaces by name (`GetStockCSForName`), the default spaces resources give for them, their components
_STOCK = {"DeviceRGB": "DeviceRGB", "RGB": "DeviceRGB", "DeviceGray": "DeviceGray", "G": "DeviceGray",
          "DeviceCMYK": "DeviceCMYK", "CMYK": "DeviceCMYK"}
_DEFAULTS = {"DeviceRGB": "DefaultRGB", "DeviceGray": "DefaultGray", "DeviceCMYK": "DefaultCMYK"}
_DEVICE_COMPONENTS = {"DeviceGray": 1, "DeviceRGB": 3, "DeviceCMYK": 4}
# The family of an array by the first four letters of its first name (`AllocateColorSpace`), with its components;
# DeviceN ('Devi', also an array that starts with DeviceRGB), ICCBased and Pattern are read apart
_FAMILIES = {"CalG": 1, "CalR": 3, "Lab": 3, "Inde": 1, "I": 1, "Sepa": 1}
# The filters pdfium undoes before the last one (`ValidateDecoderPipeline`)
_BEFORE_LAST = {"FlateDecode", "Fl", "LZWDecode", "LZW", "ASCII85Decode", "A85", "ASCIIHexDecode", "AHx", "RunLengthDecode", "RL"}


@dataclass(frozen=True, slots=True)
class _Picture:
    """What the header of a JPEG 2000 picture says: the components openjpeg decodes (a palette's columns when a
    component map comes with it) and the colour space its first JP2 colour box names ('' for none or a profile)."""

    components: int
    space: str


class _TooMuch(Exception):
    """More names or objects than are judged."""


class _Document:
    """The pdfminer document, with what judging a picture may need of all of it, read once and only when asked: the
    decoded object streams, whether a default space is named anywhere, the colour spaces of every resource dictionary."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pdf = PDFDocument(PDFParser(BytesIO(data)))
        self._packed: bytes | None = None
        self._defaults: bool | None = None
        self._named: dict[str, list[object]] | None = None

    def get(self, objid: int) -> object:
        return self.pdf.getobj(objid)

    def resolve(self, value: object) -> object:
        for _ in range(32):
            if not isinstance(value, PDFObjRef):
                return value
            try:
                value = self.get(value.objid)
            except Exception:  # noqa: BLE001 - a reference to nothing
                return None
        return None

    def objids(self) -> list[int]:
        found: set[int] = set()
        for xref in self.pdf.xrefs:
            found.update(xref.get_objids())
            if len(found) > _MAX_OBJECTS:
                raise _TooMuch
        return sorted(found)

    def packed(self) -> bytes:
        """The decoded object streams: what is written there is not in the file's bytes."""
        if self._packed is None:
            streams: set[int] = set()
            for xref in self.pdf.xrefs:
                for objid in xref.get_objids():
                    try:
                        holder = xref.get_pos(objid)[0]
                    except Exception:  # noqa: BLE001 - an entry of no position
                        continue
                    if holder is not None:
                        streams.add(holder)
            parts = []
            for objid in sorted(streams):
                stream = self.resolve(PDFObjRef(self.pdf, objid))
                if isinstance(stream, PDFStream):
                    parts.append(stream.get_data())
            self._packed = b"\n".join(parts)
        return self._packed

    def defaults(self) -> bool:
        """Whether a default space (DefaultGray, DefaultRGB, DefaultCMYK) is named in the file or its object streams."""
        if self._defaults is None:
            self._defaults = _DEFAULT_NAME.search(self.data) is not None or _DEFAULT_NAME.search(self.packed()) is not None
        return self._defaults

    def named(self, name: str) -> list[object]:
        """Every colour space a resource dictionary of the document gives under `name` (its ColorSpace entry)."""
        if self._named is None:
            self._named = {}
            for objid in self.objids():
                try:
                    found = self.get(objid)
                except Exception:  # noqa: BLE001
                    continue
                for holder in _dictionaries(found):
                    spaces = self.resolve(holder.get("ColorSpace"))
                    if isinstance(spaces, dict):
                        for key, value in spaces.items():
                            self._named.setdefault(str(key), []).append(value)
        return self._named.get(name, [])


def _dictionaries(value: object) -> list[dict]:
    """The dictionaries an object is or holds directly (a stream's own, those in an array or a dictionary)."""
    if isinstance(value, PDFStream):
        value = value.attrs
    if isinstance(value, dict):
        return [value, *(inner for inner in value.values() if isinstance(inner, dict))]
    if isinstance(value, list):
        return [inner for inner in value if isinstance(inner, dict)]
    return []


def stops_the_renderer(data: bytes) -> bool:
    """Whether pdfium would stop the process drawing a page of this PDF: it holds a JPEG 2000 picture whose colour space
    asks for more components than the picture has, or an image mask whose picture pdfium takes for gray. Every stream
    pdfium decodes with JPXDecode counts, wherever it is drawn from (a page, a form, an annotation, a pattern, a mask); a
    picture written inside a page's content is not decoded by pdfium and does not count.

    A picture whose header cannot be read, or that pdfminer would undo with other filters than pdfium, counts as one that
    stops it. A file that holds no JPXDecode name (also written with `#hh`) is decided by that search alone; one that does
    is read as little as it can be: the object each name is read in, and every object of the document only when a name is
    in no such stream (a filter written as an object of its own or in an object stream) or a colour space is looked up in
    resources."""
    names = _JPX_NAME.finditer(data)
    first = next(names, None)
    if first is None:
        return False
    try:
        document = _Document(data)
    except Exception:  # noqa: BLE001 - a file pdfminer cannot open: its pictures cannot be judged
        return True
    try:
        return _judge(document, _chain(first, names))
    except _TooMuch:
        return True


def _chain(first: re.Match[bytes], rest: Iterator[re.Match[bytes]]) -> Iterator[re.Match[bytes]]:
    yield first
    yield from rest


def _judge(document: _Document, names: Iterator[re.Match[bytes]]) -> bool:
    judged: set[int] = set()
    whole = _JPX_NAME.search(document.packed()) is not None
    previous, holder = 0, None
    for count, match in enumerate(names, start=1):
        if count > _MAX_NAMES:
            raise _TooMuch
        holder = _holder(document.data, previous, match.start()) or holder
        previous = match.start()
        if holder is None:
            return True  # a name before any object
        if holder in judged:
            continue
        judged.add(holder)
        try:
            stream = document.get(holder)
        except PDFObjectNotFound:
            continue  # an object the cross-reference table does not name: nothing draws it
        except Exception:  # noqa: BLE001 - an object pdfminer cannot read
            return True
        if not isinstance(stream, PDFStream) or not _decodes_jpx(stream, document):
            # A picture written inside a page's content, which pdfium does not decode; an older revision of an object the
            # file has since replaced, which nothing draws; a filter written as an object of its own: the whole document
            # is read for the pictures it holds
            whole = True
            continue
        if _stops(stream, document):
            return True
    if whole:
        for objid in document.objids():
            if objid in judged:
                continue
            try:
                stream = document.get(objid)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(stream, PDFStream) and _decodes_jpx(stream, document) and _stops(stream, document):
                return True
    return False


def _holder(data: bytes, start: int, position: int) -> int | None:
    """The number of the object a name at `position` is read in — the one whose header comes last before it — when that
    header lies after `start` (the name before it); None otherwise, the object then being that name's. It is looked for
    in a short stretch before the name first: searched in the whole file, the headers of a 21 MB scan took 170 ms, the
    digits of its pictures read as numbers; searched back to the start of the file for every name, a stream holding a
    name every 100 bytes took a minute for a megabyte. The last header of a stretch that ends at the name is the last
    before it."""
    for window in (*_HEADER_WINDOWS, position):
        last = None
        for last in _OBJECT_HEADER.finditer(data, max(position - window, start), position):
            pass
        if last is not None:
            return int(last.group(1))
        if position - window <= start:
            break
    return None


def _decodes_jpx(stream: PDFStream, document: _Document) -> bool:
    """Whether pdfium decodes the stream with JPXDecode: its Filter, as pdfium reads it, ends with that name."""
    filters = _filters(stream, document)
    return bool(filters) and filters[-1] == "JPXDecode"  # type: ignore[index]


def _filters(stream: PDFStream, document: _Document) -> list[str] | None:
    """The filters pdfium reads (`GetDecoderArray`): the Filter entry, a name or an array of names all but the last of
    which are filters pdfium undoes before another; None when pdfium would decode nothing."""
    value = document.resolve(stream.attrs.get("Filter"))
    if isinstance(value, PSLiteral):
        return [str(value.name)]
    if not isinstance(value, list) or not value:
        return None
    names = [document.resolve(item) for item in value]
    if not all(isinstance(item, PSLiteral) for item in names):
        return None
    filters = [str(item.name) for item in names]  # type: ignore[union-attr]
    return filters if all(name in _BEFORE_LAST for name in filters[:-1]) else None


def _stops(stream: PDFStream, document: _Document) -> bool:
    """Whether drawing this JPEG 2000 stream stops pdfium."""
    try:
        picture = _picture(_jpx_data(stream, document))
    except Exception:  # noqa: BLE001 - data that cannot be undone or decrypted
        return True
    if picture is None:
        return True
    count = picture.components
    taken = "gray" if count <= 2 else picture.space  # `CJPX_Decoder::StartDecode`
    mask = stream.attrs.get("ImageMask") is True  # an indirect ImageMask is no boolean to pdfium (`GetBooleanFor`)
    if mask or "ColorSpace" not in stream.attrs:
        # pdfium takes the colour space from the picture; an image mask is read the same way, and reads past the end
        # of a list when that is gray
        asked = _ASKED_BY_PICTURE.get(taken, 0)  # '' unknown: as many as it has, which never stops it
        return asked > count or (mask and taken == "gray")
    for family, components in _readings(stream.attrs.get("ColorSpace"), document, resources=True, named=True):
        if family == "DeviceCMYK":
            # refused unless the picture's own space is CMYK or unknown; one of one or two components is gray
            if count < 4 and taken in ("", "cmyk"):
                return True
        elif family not in ("DeviceGray", "DeviceRGB") and components > count:
            return True  # a stock gray space asks for one; a stock RGB one is refused below three components
    return False


def _jpx_data(stream: PDFStream, document: _Document) -> bytes:
    """The bytes pdfium hands its JPEG 2000 decoder: the stream decrypted, and undone by the filters before JPXDecode.
    With JPXDecode alone they are decrypted here, whatever pdfminer takes for the stream's filters (it reads an F entry,
    a file specification, before Filter); with other filters pdfminer undoes them, under the reader's limit on inflated
    bytes, and only when it reads the same ones as pdfium."""
    filters = _filters(stream, document)
    if filters == ["JPXDecode"]:
        data = stream.get_rawdata() or b""
        return stream.decipher(stream.objid, stream.genno, data, stream.attrs) if stream.decipher else data
    undone = [str(name.name) if isinstance(name, PSLiteral) else name for name, _ in stream.get_filters()]
    if undone != filters:
        raise ValueError("pdfminer would undo other filters than pdfium")
    return stream.get_data()


def _readings(space: object, document: _Document, *, resources: bool, named: bool = False,
              depth: int = 0) -> list[tuple[str, int]]:
    """Every colour space pdfium may take `space` for, as (stock family or '', components): a stock space by its name,
    or a default space the resources give for it (one of ICC profiles asks for the device's components when the image
    names the device: `LoadColorInfo`); a space the resources give under a name; an array by its family. Nothing for
    what pdfium loads as no space: it then decodes nothing."""
    if depth > 16:
        return [("", 32)]
    space = document.resolve(space)
    if isinstance(space, PSLiteral):
        name = str(space.name)
        if name in _STOCK:
            family = _STOCK[name]
            readings = [(family, _DEVICE_COMPONENTS[family])]
            if resources and document.defaults():
                for default in document.named(_DEFAULTS[family]):
                    if named and _family(document.resolve(default), document) == "ICCB":
                        readings.append(("", _DEVICE_COMPONENTS[family]))
                    else:
                        readings += _readings(default, document, resources=False, depth=depth + 1)
            return readings
        if name == "Pattern" or not resources:
            return []
        return [reading for value in document.named(name)
                for reading in _readings(value, document, resources=False, depth=depth + 1)]
    if not isinstance(space, list) or not space:
        return []
    if len(space) == 1:
        return _readings(space[0], document, resources=resources, depth=depth + 1)
    family = _family(space, document)
    if family in _FAMILIES:
        return [("", _FAMILIES[family])]
    if family == "Devi":
        names = document.resolve(space[1])
        return [("", len(names))] if isinstance(names, list) and names else []
    if family == "ICCB":
        profile = document.resolve(space[1])
        count = document.resolve(profile.attrs.get("N")) if isinstance(profile, PDFStream) else None
        count = int(count) if isinstance(count, (int, float)) and not isinstance(count, bool) else 0
        return [("", count)] if count in (1, 3, 4) else []
    return []


def _family(space: object, document: _Document) -> str | None:
    """The first four letters of an array's first name (`AllocateColorSpace`), or the whole name when shorter."""
    if not isinstance(space, list) or not space:
        return None
    name = document.resolve(space[0])
    return str(name.name)[:4] if isinstance(name, PSLiteral) else None


def _picture(code: bytes) -> _Picture | None:
    """The header of a JPEG 2000 codestream or JP2 file; None when it cannot be read as openjpeg reads it."""
    view = memoryview(code)
    if bytes(view[:len(_JP2_SIGNATURE)]) != _JP2_SIGNATURE:
        return _codestream(view, "", 0)
    space, palette, mapped, coloured, position = "", 0, False, False, 0
    while position + 8 <= len(view):
        length, kind = int.from_bytes(view[position:position + 4], "big"), bytes(view[position + 4:position + 8])
        head = 8
        if length == 1:
            length, head = int.from_bytes(view[position + 8:position + 16], "big"), 16
        elif length == 0:
            length = len(view) - position
        if length < head:
            return None
        body = view[position + head:position + length]
        if kind == b"jp2h":
            inner = 0
            while inner + 8 <= len(body):
                size, box = int.from_bytes(body[inner:inner + 4], "big"), bytes(body[inner + 4:inner + 8])
                if size < 8:
                    return None
                content = body[inner + 8:inner + size]
                if box == b"colr" and not coloured:
                    # only the first colour box counts, whatever its length; one with a profile leaves the space unknown
                    coloured = True
                    enumerated = len(content) >= 7 and content[0] == 1
                    space = _ENUMERATED.get(int.from_bytes(content[3:7], "big"), "") if enumerated else ""
                elif box == b"pclr" and len(content) >= 3:
                    palette = content[2]
                elif box == b"cmap":
                    mapped = True
                inner += size
        elif kind == b"jp2c":
            return _codestream(body, space, palette if mapped else 0)
        position += length
    return None


def _codestream(code: memoryview, space: str, palette: int) -> _Picture | None:
    """The components of a codestream from its SIZ marker, which follows its first marker."""
    if bytes(code[:4]) != b"\xff\x4f\xff\x51" or len(code) < 42:
        return None
    components = int.from_bytes(code[40:42], "big")
    if not 1 <= components <= 16384 or len(code) < 42 + 3 * components:
        return None
    return _Picture(palette or components, space)
