"""JPEG 2000 pictures that stop the process when pdfium draws them (`_pdf_jpx.stops_the_renderer`)."""

import io
import subprocess
import sys
import zlib

import pytest

from opendockit import convert
from opendockit.extractors import _pdf_jpx
from opendockit.extractors._pdf_jpx import _OBJECT_HEADER, _holder, stops_the_renderer
from tests.timing import fastest


def _picture(mode: str, jp2: bool = False, colour: int | None = None) -> bytes:
    """A 16 x 16 JPEG 2000 picture of the Pillow mode (L 1 component, LA 2, RGB 3, RGBA and CMYK 4), as a codestream or a JP2
    file; `colour` writes another enumerated colour space into its colour box."""
    from PIL import Image

    output = io.BytesIO()
    Image.new(mode, (16, 16), (0, 0, 0, 0) if mode == "CMYK" else "white").save(output, "JPEG2000", no_jp2=not jp2)
    data = output.getvalue()
    if colour is not None:
        at = data.index(b"colr") + 4
        assert data[at] == 1  # an enumerated colour space
        data = data[:at + 3] + colour.to_bytes(4, "big") + data[at + 7:]
    return data


def _box(kind: bytes, body: bytes) -> bytes:
    return (8 + len(body)).to_bytes(4, "big") + kind + body


def _with_boxes(jp2: bytes, added: bytes = b"", colour_first: bytes = b"", second_colour: int | None = None) -> bytes:
    """A JP2 file with boxes added to its header box: `added` after the boxes Pillow wrote, `colour_first` before its
    colour box; `second_colour` rewrites the enumerated space of Pillow's own colour box when another comes first."""
    at = jp2.index(b"jp2h") - 4
    length = int.from_bytes(jp2[at:at + 4], "big")
    body = jp2[at + 8:at + length]
    if colour_first:
        colour = body.index(b"colr") - 4
        body = body[:colour] + colour_first + body[colour:]
        if second_colour is not None:
            own = body.index(b"colr", colour + len(colour_first)) + 4
            body = body[:own + 3] + second_colour.to_bytes(4, "big") + body[own + 7:]
    return jp2[:at] + _box(b"jp2h", body + added) + jp2[at + length:]


# A palette of two entries and three columns, the component map that applies it, and a colour box naming `space`
_PALETTE = _box(b"pclr", (2).to_bytes(2, "big") + bytes([3, 7, 7, 7, 0, 0, 0, 255, 255, 255]))
_MAP = _box(b"cmap", b"".join((0).to_bytes(2, "big") + bytes([1, column]) for column in range(3)))


def _colour(space: int) -> bytes:
    return _box(b"colr", bytes([1, 0, 0]) + space.to_bytes(4, "big"))


def _pdf(picture: bytes, space: str | None = None, *, mask: bool = False, flate: bool = False, in_form: bool = False,
         resources: str = "", objects: tuple[bytes, ...] = (), filter_name: str = "/JPXDecode") -> bytes:
    """A one-page PDF that draws `picture` (JPXDecode) with the colour space written as `space` (PDF text; `objects` are
    written as objects 6, 7, … for it to refer to), as an image mask, under FlateDecode, or from a form XObject."""
    numbered = [b""] * 5 + list(objects)
    data = zlib.compress(picture) if flate else picture
    filters = f"[/FlateDecode {filter_name}]" if flate else filter_name
    entries = (f"/ColorSpace {space} " if space else "") + ("/ImageMask true " if mask else "")
    numbered[4] = (f"<< /Type /XObject /Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 /Filter {filters} {entries}"
                   f"/Length {len(data)} >>\nstream\n").encode() + data + b"\nendstream"
    drawn = b"q 200 0 0 200 50 50 cm /Im0 Do Q"
    xobjects = "/Im0 5 0 R"
    if in_form:
        numbered.append(f"<< /Type /XObject /Subtype /Form /BBox [0 0 1 1] /Resources << /XObject << /Im0 5 0 R >> >> "
                        f"/Length {len(drawn)} >>\nstream\n".encode() + drawn + b"\nendstream")
        xobjects, drawn = f"/Fm0 {len(numbered)} 0 R", b"q /Fm0 Do Q"
    numbered[3] = f"<< /Length {len(drawn)} >>\nstream\n".encode() + drawn + b"\nendstream"
    numbered[2] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /XObject << {xobjects} >> {resources} >> "
                   "/Contents 4 0 R >>").encode()
    numbered[1] = b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"
    numbered[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    pdf, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(numbered, start=1):
        offsets.append(len(pdf))
        pdf += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    table = len(pdf)
    pdf += f"xref\n0 {len(numbered) + 1}\n0000000000 65535 f \n".encode() + b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    pdf += f"trailer\n<< /Size {len(numbered) + 1} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    return bytes(pdf)


def _packed_resources(picture: bytes, space: str, resources: str, objects: tuple[bytes, ...] = (), *,
                      packed_filter: bool = False) -> bytes:
    """A one-page PDF whose page resources (`resources`, PDF text) are an object of an object stream, which a
    cross-reference stream points into — or, with `packed_filter`, whose picture's Filter is that object; `objects`
    are objects 8, 9, … for them to refer to."""
    numbered: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        3: (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources "
            + (f"<< /XObject << /Im0 5 0 R >> {resources} >>".encode() if packed_filter else b"6 0 R") + b" /Contents 4 0 R >>"),
        4: _stream("", b"q 200 0 0 200 50 50 cm /Im0 Do Q"),
        5: _stream(f"/Type /XObject /Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 "
                   f"/Filter {'6 0 R' if packed_filter else '/JPXDecode'} /ColorSpace {space}", picture)}
    for number, body in enumerate(objects, start=8):
        numbered[number] = body
    packed = b"[/JPXDecode]" if packed_filter else f"<< /XObject << /Im0 5 0 R >> {resources} >>".encode()
    head = b"6 0 "
    numbered[7] = _stream(f"/Type /ObjStm /N 1 /First {len(head)} /Filter /FlateDecode", zlib.compress(head + packed))
    pdf, offsets = bytearray(b"%PDF-1.7\n"), {}
    for number in sorted(numbered):
        offsets[number] = len(pdf)
        pdf += f"{number} 0 obj\n".encode() + numbered[number] + b"\nendobj\n"
    size = max(numbered) + 2
    table = size - 1
    offsets[table] = len(pdf)
    rows = b"".join(b"\x00" + (0).to_bytes(4, "big") + b"\xff\xff" if number == 0 else
                    b"\x02" + (7).to_bytes(4, "big") + b"\x00\x00" if number == 6 else
                    b"\x01" + offsets[number].to_bytes(4, "big") + b"\x00\x00" for number in range(size))
    pdf += (f"{table} 0 obj\n<< /Type /XRef /Size {size} /W [1 4 2] /Root 1 0 R /Length {len(rows)} >>\nstream\n".encode()
            + rows + b"\nendstream\nendobj\n")
    pdf += f"startxref\n{offsets[table]}\n%%EOF\n".encode()
    return bytes(pdf)


def _book(pages: int) -> bytes:
    """A PDF of `pages` pages, each drawing a JPEG 2000 picture of its own in DeviceGray, whose resources each name the
    same DefaultGray ICC profile and a space /CS0."""
    numbered: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: _stream("/N 1", b"\x00" * 128)}
    picture, kids = _picture("L"), []
    for index in range(pages):
        page, content, image = 10 + 3 * index, 11 + 3 * index, 12 + 3 * index
        kids.append(f"{page} 0 R")
        numbered[page] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents {content} 0 R /Resources << "
                          f"/XObject << /Im0 {image} 0 R >> /ColorSpace << /DefaultGray [/ICCBased 3 0 R] /CS0 /DeviceGray >> >> >>").encode()
        numbered[content] = _stream("", b"q 200 0 0 200 50 50 cm /Im0 Do Q")
        numbered[image] = _stream("/Type /XObject /Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 /Filter /JPXDecode "
                                  "/ColorSpace /DeviceGray", picture)
    numbered[2] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {pages} >>".encode()
    pdf, offsets = bytearray(b"%PDF-1.7\n"), {}
    for number in sorted(numbered):
        offsets[number] = len(pdf)
        pdf += f"{number} 0 obj\n".encode() + numbered[number] + b"\nendobj\n"
    size = max(numbered) + 1
    table = len(pdf)
    pdf += f"xref\n0 {size}\n".encode() + b"".join(
        (f"{offsets[number]:010d} 00000 n \n" if number in offsets else "0000000000 65535 f \n").encode() for number in range(size))
    pdf += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{table}\n%%EOF\n".encode()
    return bytes(pdf)


def _stream(dictionary: str, body: bytes) -> bytes:
    return f"<< {dictionary} /Length {len(body)} >>\nstream\n".encode() + body + b"\nendstream"


# A DeviceN space of `n` colorants (object 6 is its tint transform) and an ICC profile of `n` components (object 6)
def _device_n(n: int) -> tuple[str, tuple[bytes, ...]]:
    domain = " ".join(["0 1"] * n)
    return (f"[/DeviceN [{' '.join(f'/C{index}' for index in range(n))}] /DeviceRGB 6 0 R]",
            (_stream(f"/FunctionType 4 /Domain [{domain}] /Range [0 1 0 1 0 1]", b"{ pop }"),))


def _icc(n: int) -> tuple[str, tuple[bytes, ...]]:
    return "[/ICCBased 6 0 R]", (_stream(f"/N {n}", b"\x00" * 128),)


_WHITE = "<< /WhitePoint [0.95 1 1.09] >>"

# Each case was drawn by pdfium 159 in a process of its own: True where that process was stopped
_CASES = {
    "DeviceN of 4 over 2 components": (lambda: _pdf(_picture("LA"), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 4 over 3 components": (lambda: _pdf(_picture("RGB"), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 2 over 2 components": (lambda: _pdf(_picture("LA"), _device_n(2)[0], objects=_device_n(2)[1]), False),
    "DeviceN of 4 over 4 components": (lambda: _pdf(_picture("RGBA"), _device_n(4)[0], objects=_device_n(4)[1]), False),
    "ICC of 3 over 1 component": (lambda: _pdf(_picture("L"), _icc(3)[0], objects=_icc(3)[1]), True),
    "ICC of 4 over 3 components": (lambda: _pdf(_picture("RGB"), _icc(4)[0], objects=_icc(4)[1]), True),
    "ICC of 3 over 3 components": (lambda: _pdf(_picture("RGB"), _icc(3)[0], objects=_icc(3)[1]), False),
    "Lab over 2 components": (lambda: _pdf(_picture("LA"), f"[/Lab {_WHITE}]"), True),
    "CalRGB over 1 component": (lambda: _pdf(_picture("L"), f"[/CalRGB {_WHITE}]"), True),
    "CalGray over 1 component": (lambda: _pdf(_picture("L"), f"[/CalGray {_WHITE}]"), False),
    "Indexed over 1 component": (lambda: _pdf(_picture("L"), "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"), False),
    "DeviceGray over 3 components": (lambda: _pdf(_picture("RGB"), "/DeviceGray"), False),
    # pdfium takes a picture of one or two components for gray and refuses it as RGB, or as CMYK
    "DeviceRGB over 1 component": (lambda: _pdf(_picture("L"), "/DeviceRGB"), False),
    "DeviceRGB over 2 components": (lambda: _pdf(_picture("LA"), "/DeviceRGB"), False),
    "DeviceCMYK over 2 components": (lambda: _pdf(_picture("LA"), "/DeviceCMYK"), False),
    "DeviceCMYK over 3 components": (lambda: _pdf(_picture("RGB"), "/DeviceCMYK"), True),
    "DeviceCMYK over 3 components named sRGB": (lambda: _pdf(_picture("RGB", jp2=True, colour=16), "/DeviceCMYK"), False),
    "DeviceCMYK over 4 components": (lambda: _pdf(_picture("CMYK"), "/DeviceCMYK"), False),
    "DeviceCMYK over 3 components named CMYK": (lambda: _pdf(_picture("RGB", jp2=True, colour=12), "/DeviceCMYK"), True),
    # a default space in the resources makes DeviceRGB a space of three components that is not refused
    "DeviceRGB under DefaultRGB over 2 components": (
        lambda: _pdf(_picture("LA"), "/DeviceRGB", resources="/ColorSpace << /DefaultRGB [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 3", b"\x00" * 128),)), True),
    # no colour space: pdfium takes the one the picture names
    "no space, 2 components": (lambda: _pdf(_picture("LA")), False),
    "no space, 3 components": (lambda: _pdf(_picture("RGB")), False),
    "no space, JP2 CMYK over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=12)), True),
    "no space, JP2 CMYK over 1 component": (lambda: _pdf(_picture("L", jp2=True, colour=12)), False),
    "no space, JP2 sRGB over 1 component": (lambda: _pdf(_picture("L", jp2=True, colour=16)), False),
    "no space, JP2 gray over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=17)), False),
    # an image mask is read as a picture without a space, and stops pdfium when that is gray
    "mask of 1 component": (lambda: _pdf(_picture("L"), mask=True), True),
    "mask of 2 components": (lambda: _pdf(_picture("LA"), mask=True), True),
    "mask of 2 components with a DeviceN space": (lambda: _pdf(_picture("LA"), _device_n(4)[0], mask=True, objects=_device_n(4)[1]), True),
    "mask of JP2 gray over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=17), mask=True), True),
    "mask of JP2 CMYK over 3 components": (lambda: _pdf(_picture("RGB", jp2=True, colour=12), mask=True), True),
    "mask of 3 components": (lambda: _pdf(_picture("RGB"), mask=True), False),
    "mask of 4 components": (lambda: _pdf(_picture("RGBA"), mask=True), False),
    # openjpeg applies a palette when a component map comes with it, and pdfium does not ask it to for an Indexed space;
    # of two colour boxes the first counts
    "DeviceN of 3 over a palette of 3 columns": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), _device_n(3)[0], objects=_device_n(3)[1]), False),
    "DeviceN of 4 over a palette of 3 columns": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), _device_n(4)[0], objects=_device_n(4)[1]), True),
    "DeviceN of 3 over a palette without a map": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE), _device_n(3)[0], objects=_device_n(3)[1]), True),
    "Indexed over a palette": (
        lambda: _pdf(_with_boxes(_picture("L", jp2=True), _PALETTE + _MAP), "[/Indexed /DeviceRGB 1 <000000FFFFFF>]"), False),
    "no space, sRGB box before a CMYK one, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_colour(16), second_colour=12)), False),
    "no space, CMYK box before an sRGB one, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_colour(12), second_colour=16)), True),
    "mask with a profile for colour box, 3 components": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_box(b"colr", bytes([2, 0, 0]) + b"\x00" * 128)), mask=True),
        False),
    # a default space in the resources stands for the device space it is named after (one of ICC profiles keeps the
    # device's components), wherever the resources are written
    "DeviceGray under a DefaultGray of CalRGB over 1 component": (
        lambda: _pdf(_picture("L"), "/DeviceGray", resources=f"/ColorSpace << /DefaultGray [/CalRGB {_WHITE}] >>"), True),
    "DeviceGray under a DefaultGray of DeviceCMYK over 3 components": (
        lambda: _pdf(_picture("RGB"), "/DeviceGray", resources="/ColorSpace << /DefaultGray /DeviceCMYK >>"), True),
    "DeviceGray under a DefaultGray of ICC 3 over 1 component": (
        lambda: _pdf(_picture("L"), "/DeviceGray", resources="/ColorSpace << /DefaultGray [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 3", b"\x00" * 128),)), False),
    "DeviceRGB under a DefaultRGB of DeviceN 4 over 3 components": (
        lambda: _pdf(_picture("RGB"), "/DeviceRGB", resources="/ColorSpace << /DefaultRGB [/DeviceN [/A /B /C /D] /DeviceRGB 6 0 R] >>",
                     objects=_device_n(4)[1]), True),
    "DeviceCMYK under a DefaultCMYK of ICC 4 over 3 components": (
        lambda: _pdf(_picture("RGB", jp2=True, colour=16), "/DeviceCMYK", resources="/ColorSpace << /DefaultCMYK [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 4", b"\x00" * 128),)), True),
    "DeviceRGB under a DefaultRGB in an object stream, over 2 components": (
        lambda: _packed_resources(_picture("LA"), "/DeviceRGB", "/ColorSpace << /DefaultRGB [/ICCBased 8 0 R] >>",
                                  (_stream("/N 3", b"\x00" * 128),)), True),
    "DeviceRGB, resources in an object stream without a default, over 2 components": (
        lambda: _packed_resources(_picture("LA"), "/DeviceRGB", "/ColorSpace << /CS0 /DeviceGray >>"), False),
    "DefaultRGB written with #hh": (
        lambda: _pdf(_picture("LA"), "/DeviceRGB", resources="/ColorSpace << /Default#52#47B [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 3", b"\x00" * 128),)), True),
    # a name the resources give
    "a name for a DeviceN of 4 over 2 components": (
        lambda: _pdf(_picture("LA"), "/CS0", resources="/ColorSpace << /CS0 [/DeviceN [/A /B /C /D] /DeviceRGB 6 0 R] >>",
                     objects=_device_n(4)[1]), True),
    "a name for DeviceGray over 2 components": (lambda: _pdf(_picture("LA"), "/CS0", resources="/ColorSpace << /CS0 /DeviceGray >>"), False),
    "a name the resources do not give": (lambda: _pdf(_picture("LA"), "/CS9"), False),
    # a default space is read without the resources: a name there is no space, and pdfium decodes nothing
    "a DefaultGray that is a name the resources give for a DeviceN of 4, over 2 components": (
        lambda: _pdf(_picture("LA"), "/DeviceGray", resources="/ColorSpace << /DefaultGray /CS0 /CS0 [/DeviceN [/A /B /C /D] /DeviceRGB 6 0 R] >>",
                     objects=_device_n(4)[1]), False),
    # arrays of one element are followed a long way (pdfium follows them without a limit; past 16 the guard stops)
    "a DeviceN of 4 inside 20 arrays of one element, over 2 components": (
        lambda: _pdf(_picture("LA"), "[" * 20 + _device_n(4)[0] + "]" * 20, objects=_device_n(4)[1]), True),
    # pdfium tells an array's family by the first four letters of its first name, and takes no other
    "an array of DeviceRGB with 4 names over 3 components": (
        lambda: _pdf(_picture("RGB"), "[/DeviceRGB [/A /B /C /D] /DeviceRGB 6 0 R]", objects=_device_n(4)[1]), True),
    "an array of DeviceGray with 3 names over 1 component": (
        lambda: _pdf(_picture("L"), "[/DeviceGray [/A /B /C] /DeviceRGB 6 0 R]", objects=_device_n(3)[1]), True),
    "an array of DeviceCMYK alone over 3 components": (lambda: _pdf(_picture("RGB"), "[/DeviceCMYK]"), True),
    "an array of DeviceRGB alone over 3 components": (lambda: _pdf(_picture("RGB"), "[/DeviceRGB]"), False),
    "CalCMYK over 3 components": (lambda: _pdf(_picture("RGB"), f"[/CalCMYK {_WHITE}]"), False),
    "Separation over 1 component": (lambda: _pdf(_picture("L"), "[/Separation /Spot /DeviceRGB 6 0 R]", objects=_device_n(1)[1]), False),
    "Pattern over 1 component": (lambda: _pdf(_picture("L"), "/Pattern"), False),
    "RGB, the abbreviation, over 2 components": (lambda: _pdf(_picture("LA"), "/RGB"), False),
    "ICC with N 3.0 over 1 component": (lambda: _pdf(_picture("L"), "[/ICCBased 6 0 R]", objects=(_stream("/N 3.0", b"\x00" * 128),)), True),
    "ICC with N 2 over 1 component": (lambda: _pdf(_picture("L"), "[/ICCBased 6 0 R]", objects=(_stream("/N 2", b"\x00" * 128),)), False),
    "an array of DeviceRGB alone under a DefaultRGB of ICC 1, over 2 components": (
        lambda: _pdf(_picture("LA"), "[/DeviceRGB]", resources="/ColorSpace << /DefaultRGB [/ICCBased 6 0 R] >>",
                     objects=(_stream("/N 1", b"\x00" * 128),)), False),
    "ICC without N over 1 component": (lambda: _pdf(_picture("L"), "[/ICCBased 6 0 R]", objects=(_stream("/Alternate /DeviceRGB", b"\x00" * 128),)), False),
    "an ICC stream for colour space over 1 component": (lambda: _pdf(_picture("L"), "6 0 R", objects=(_stream("/N 3", b"\x00" * 128),)), False),
    # an indirect ImageMask is no boolean to pdfium
    "an indirect ImageMask over 2 components": (
        lambda: _pdf(_picture("LA"), _device_n(2)[0], objects=(*_device_n(2)[1], b"true")).replace(
            b"/Filter /JPXDecode", b"/ImageMask 7 0 R /Filter /JPXDecode"), False),
    # the first colour box counts however short it is: a profile of no bytes leaves the space unknown, and CMYK is taken
    "DeviceCMYK over 3 components after a short profile box": (
        lambda: _pdf(_with_boxes(_picture("RGB", jp2=True), colour_first=_box(b"colr", bytes([2, 0, 0]))), "/DeviceCMYK"), True),
    # wherever the picture is drawn from, and however its filter is written
    "drawn from a form": (lambda: _pdf(_picture("LA"), _device_n(4)[0], in_form=True, objects=_device_n(4)[1]), True),
    "under FlateDecode": (lambda: _pdf(_picture("LA"), _device_n(4)[0], flate=True, objects=_device_n(4)[1]), True),
    "name written with #hh": (lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="/J#50X#44ecode", objects=_device_n(4)[1]), True),
    "name written with lowercase #hh": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="/#4aPXDecode", objects=_device_n(4)[1]), True),
    "a filter array whose element is an object of its own": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="[7 0 R]", objects=(*_device_n(4)[1], b"/JPXDecode")), True),
    "JPXDecode after a filter pdfium does not undo first": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="[/DCTDecode /JPXDecode]", objects=_device_n(4)[1]), False),
    "an array of JPXDecode alone": (lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="[/JPXDecode]", objects=_device_n(4)[1]), True),
    "JPXDecode before another filter": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="[/JPXDecode /FlateDecode]", objects=_device_n(4)[1]), False),
    "a filter written as an object of its own": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], filter_name="7 0 R", objects=(*_device_n(4)[1], b"[/JPXDecode]")), True),
    "an object header written in a string of the dictionary": (
        lambda: _pdf(_picture("LA"), _device_n(4)[0], objects=_device_n(4)[1]).replace(
            b"/Filter /JPXDecode", b"/Name (4 0 obj) /Filter /JPXDecode"), True),
    "drawn from a form, safe": (lambda: _pdf(_picture("LA"), _device_n(2)[0], in_form=True, objects=_device_n(2)[1]), False),
}


class TestTheRendererStops:
    """pdfium asks the JPEG 2000 decoder for as many components as the colour space it takes for the picture has, and a
    check ends the process when that is more than the picture holds; an image mask it takes for gray is read past the end
    of a list, which libc++ ends. The PDF.js test file `jpx_numcomps.pdf` (10 KB, six pictures, one of them a DeviceN of
    four colorants over two components) ended every program that converted it in its own process."""

    @pytest.mark.parametrize("case", list(_CASES))
    def test_each_case_is_judged_as_pdfium_draws_it(self, case):
        build, stops = _CASES[case]

        assert stops_the_renderer(build()) is stops

    def test_a_document_that_stops_the_renderer_is_converted_without_its_pictures(self):
        """Converted in a process of its own: before, that process ended with SIGTRAP (SIGILL on x86)."""
        stopping = _pdf(_picture("LA"), _device_n(4)[0], objects=_device_n(4)[1])
        script = ("import sys; from opendockit import convert; result = convert(sys.stdin.buffer.read(), 'a.pdf'); "
                  "print(len(result.images)); print(*result.warnings, sep='\\n')")

        done = subprocess.run([sys.executable, "-c", script], input=stopping, capture_output=True, timeout=120, check=False)

        assert done.returncode == 0, done.stderr[-500:]
        lines = done.stdout.decode().splitlines()
        assert lines[0] == "0"
        assert "1 picture(s) of the PDF were not extracted: it holds a JPEG 2000 picture that stops the PDF renderer when drawn." in lines

    def test_a_picture_that_does_not_stop_it_is_extracted(self):
        result = convert(_pdf(_picture("LA"), _device_n(2)[0], objects=_device_n(2)[1]), "a.pdf")

        assert len(result.images) == 1 and not any("JPEG 2000" in warning for warning in result.warnings)


class TestWhatIsNotJudged:
    def test_a_file_without_the_name_is_not_opened(self, monkeypatch):
        """The search for the name decides almost every file; pdfminer opens only one that holds it."""
        monkeypatch.setattr(_pdf_jpx, "PDFDocument", lambda *args: pytest.fail("opened"))

        assert stops_the_renderer(_pdf(_picture("LA"), "/DeviceRGB").replace(b"/JPXDecode", b"/DCTDecode")) is False

    def test_a_name_inside_a_page_content_is_no_picture_pdfium_decodes(self):
        """pdfium does not decode a JPEG 2000 picture written inline in a content stream."""
        drawn = b"BI /W 1 /H 1 /BPC 8 /F /JPXDecode /CS /DeviceRGB ID x EI"
        data = _pdf(_picture("RGB"), "/DeviceRGB").replace(b"/JPXDecode", b"/DCTDecode")
        data = data.replace(b"<< /Length 32 >>\nstream\nq 200 0 0 200 50 50 cm /Im0 Do Q",
                            f"<< /Length {len(drawn)} >>\nstream\n".encode() + drawn)

        assert b"/JPXDecode" in data and stops_the_renderer(data) is False

    def test_what_cannot_be_read_counts_as_stopping_it(self):
        """A header that is not JPEG 2000, a file pdfminer cannot open, a name before any object."""
        assert stops_the_renderer(_pdf(b"\x00" * 64, "/DeviceRGB")) is True
        assert stops_the_renderer(b"%PDF-1.7\n1 0 obj << /Filter /JPXDecode >> endobj") is True
        assert stops_the_renderer(b"/JPXDecode " + _pdf(_picture("RGB"), "/DeviceRGB")) is True

    def test_more_names_than_are_judged_count_as_stopping_it(self):
        names = b"% " + b"/JPXDecode " * (_pdf_jpx._MAX_NAMES + 1)
        data = _pdf(_picture("RGB"), "/DeviceRGB").replace(b"/Im0 Do Q", b"/Im0 Do Q " + names)

        assert stops_the_renderer(data) is True
        assert stops_the_renderer(data.replace(b"/JPXDecode ", b"/DCTDecode ", 1000)) is False  # within the limit: judged

    def test_data_that_cannot_be_undone_counts_as_stopping_it(self):
        """A picture under FlateDecode whose data does not inflate: not judged."""
        data = _pdf(_picture("RGB"), "/DeviceRGB", flate=True)
        start = data.index(b"stream\n", data.index(b"/JPXDecode")) + 7
        broken = data[:start] + b"\x00" * 8 + data[start + 8:]

        assert stops_the_renderer(broken) is True

    def test_a_document_of_more_objects_than_are_read_counts_as_stopping_it(self, monkeypatch):
        """Known accepted gap: a document whose resources must be read whole (a default space, a space named there) and
        that holds more objects than are read loses its pictures."""
        monkeypatch.setattr(_pdf_jpx, "_MAX_OBJECTS", 20)
        assert stops_the_renderer(_book(3)) is False  # 12 objects
        assert stops_the_renderer(_book(6)) is True  # 21 objects
        monkeypatch.setattr(_pdf_jpx, "_MAX_OBJECTS", 200_000)
        assert stops_the_renderer(_book(6)) is False

    def test_an_object_the_cross_reference_table_does_not_name_is_not_judged(self):
        """Nothing draws it: counted as stopping pdfium, it cost a document its pictures."""
        orphan = (b"99 0 obj\n<< /Filter /JPXDecode /ColorSpace /DeviceCMYK /Length 4 >>\nstream\nxxxx\nendstream\n"
                  b"endobj\n%%EOF")
        assert stops_the_renderer(_pdf(_picture("RGB"), "/DeviceRGB").replace(b"%%EOF", orphan)) is False

    def test_a_filter_written_only_in_an_object_stream_is_not_looked_for(self):
        """Known accepted gap: a file whose bytes name no JPXDecode is not opened, and a filter written as an object of an
        object stream is not in them. Opening and inflating the object streams of every PDF with pictures cost 2.6 s on
        one of 343 such files of the PDF.js tests; a producer writes the name in the picture's own dictionary. Once a
        file names JPXDecode anywhere, its object streams are read too (`test_each_case_is_judged_as_pdfium_draws_it`)."""
        hidden = _packed_resources(_picture("LA"), _device_n(4)[0].replace("6 0 R", "8 0 R"), "", _device_n(4)[1], packed_filter=True)

        assert b"JPXDecode" not in hidden and stops_the_renderer(hidden) is False
        named_too = hidden + b"% /JPXDecode\n"  # after the last object: no picture of its own
        assert stops_the_renderer(named_too) is True
        # a picture of its own named in the bytes, safe: the other is found in the object stream
        safe = _stream("/Subtype /Image /Width 16 /Height 16 /BitsPerComponent 8 /Filter /JPXDecode /ColorSpace /DeviceGray",
                       _picture("L"))
        beside = _packed_resources(_picture("LA"), _device_n(4)[0].replace("6 0 R", "8 0 R"), "", (*_device_n(4)[1], safe),
                                   packed_filter=True)
        assert stops_the_renderer(beside) is True

    def test_a_file_entry_beside_the_filter_is_not_read_as_filters(self):
        """pdfminer takes the stream's filters from F, a file specification here, before Filter; pdfium reads Filter."""
        data = _pdf(_picture("LA"), _device_n(2)[0], objects=_device_n(2)[1]).replace(b"/Filter /JPXDecode", b"/F (file) /Filter /JPXDecode")

        assert stops_the_renderer(data) is False


class TestTheObjectOfAName:
    def test_the_header_is_found_far_before_the_name(self):
        data = b"12 0 obj\n<< " + b" " * 10_000 + b"/Filter /JPXDecode >>"

        assert _holder(data, 0, data.index(b"/JPXDecode")) == 12
        far = b"12 0 obj\n<< " + b" " * 100_000 + b"/Filter /JPXDecode >>"
        assert _holder(far, 0, far.index(b"/JPXDecode")) == 12
        assert _holder(far, 10, far.index(b"/JPXDecode")) is None  # not after the name before it: that name's object

    def test_a_header_cut_by_the_start_of_the_stretch_is_not_read(self):
        data = b"x" * 5 + b"1234 0 obj"

        assert [match.group(1) for match in _OBJECT_HEADER.finditer(data, 7, len(data))] == []
        assert _holder(data, 0, len(data)) == 1234
        assert _holder(b"7 0 objection /JPXDecode", 0, 14) is None  # a word that starts like the keyword


class TestCost:
    @pytest.mark.timing
    def test_many_names_in_one_object_take_linear_time(self):
        """Each name's object was looked for back to the start of the file when no header lay near it: a stream holding a
        name every 100 bytes took 5 s for 250 KB and a minute for a megabyte."""
        def holding(size: int) -> bytes:
            filler = (b"/JPXDecode" + b" " * 90) * (size // 100)
            return _pdf(_picture("LA"), "/DeviceRGB").replace(b"/Im0 Do Q", b"/Im0 Do Q % " + filler)

        small, large = holding(250_000), holding(1_000_000)

        assert fastest(lambda: stops_the_renderer(large)) < 8 * fastest(lambda: stops_the_renderer(small)) + 0.05
        assert stops_the_renderer(large) is False

    @pytest.mark.timing
    def test_a_default_space_named_on_every_page_takes_linear_time(self):
        """The default spaces were read again for every picture over every resource dictionary: a scan of 3,000 pages that
        each name DefaultGray took 12 s, the square of its pages."""
        small, large = _book(250), _book(2000)  # eight times the pages: 27 times the time before, 10 times now

        assert fastest(lambda: stops_the_renderer(large)) < 16 * fastest(lambda: stops_the_renderer(small)) + 0.05
        assert stops_the_renderer(large) is False

