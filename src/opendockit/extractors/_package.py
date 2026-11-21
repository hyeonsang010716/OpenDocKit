"""ZIP packages and their XML parts, shared by the OOXML, HWPX, ODF and EPUB extractors: opening an archive within the
limits, reading a part without entities or network access, finding the main part of a damaged package, and reading an
integer attribute.
"""

from collections.abc import Callable
from io import BytesIO
import lzma
import posixpath
import re
import shutil
from urllib.parse import unquote
import zipfile
import zlib

from lxml import etree

from opendockit.extractors._xml import thread_parser


_xml_parser = thread_parser(resolve_entities=False, no_network=True)

# What reading one damaged member of a ZIP archive can raise: a bad CRC or local header (BadZipFile), a broken compressed stream
# (zlib.error, lzma.LZMAError, OSError from bz2), a stream cut short (EOFError), an encrypted member (RuntimeError), an unknown
# compression method (NotImplementedError), and odd header values (ValueError). Catch these around a member read so that one member
# skips only itself (Pinned by: test_office_robustness.py::TestDamagedZipMember).
ZIP_MEMBER_ERRORS: tuple[type[Exception], ...] = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, RuntimeError,
                                                  NotImplementedError, ValueError)
# What the libraries raise when a package does not open because of a part or relationship the repair can take out
# (`open_package`): a missing part, a part that is not XML, a relationship without its Id, Type or Target, a member that does not read
# (python-pptx's InvalidXmlError is added by the PPTX reader, `open_package(errors=...)`: imported here, it loaded python-pptx
# for every document)
_OPEN_ERRORS: tuple[type[Exception], ...] = (KeyError, etree.XMLSyntaxError, TypeError, AttributeError, *ZIP_MEMBER_ERRORS)
# Chunk size for reading a member through to check it (`_member_reads`)
_CHECK_CHUNK = 1 << 20


# Namespaces of OOXML package relationships (.rels) and content types ([Content_Types].xml), and the main-part relationship type
# (`repair_package`).
_PACKAGE_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_STRICT_OFFICE_DOCUMENT = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
_XML_CONTENT_TYPES = {"application/xml", "text/xml"}
_CORE_PROPERTIES = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"
# Content type of the document-properties part; the libraries load only such a part as their properties part (`core_properties`)
_CORE_PROPERTIES_TYPE = "application/vnd.openxmlformats-package.core-properties+xml"


def decimal_int[T](value: str | None, default: T, *, signed: bool = False) -> int | T:
    """Integer attribute value written in a file. `default` if it is not decimal digits (with `signed`, one leading '-' is accepted).

    `isdigit()` is also true for '²', so `int()` raised ValueError; and a number past 4,300 digits passes `isdecimal()` but `int()`
    raises ValueError (Python's integer string length limit), so one value got the whole document rejected — both count as odd values
    (Pinned by: test_office_robustness.py::TestHugeNumbersInOfficeDocuments, test_hwp_robustness.py::TestHugeNumbersInHangulDocuments).
    """
    if value is None:
        return default
    digits = value.removeprefix("-") if signed else value
    if not digits.isdecimal():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def uncompressed_size(data: bytes) -> int:
    """Total uncompressed size of a ZIP archive's members, from its central directory (the zip-bomb check of `converter._guard_zip`
    and of the file LibreOffice writes, `legacy._guard_converted_zip` — one function so the two checks cannot drift apart).
    Raises zipfile.BadZipFile when the archive cannot be opened."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return sum(info.file_size for info in archive.infolist())


def has_password_protected_part(data: bytes) -> bool:
    """Whether a member of the ZIP archive carries the encryption bit (bit 0 of its general purpose flags; `zip -e`, or a tool that
    encrypts the archive). zipfile refuses to read such a member without the password. False when the archive cannot be opened."""
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            return any(info.flag_bits & 0x1 for info in archive.infolist())
    except zipfile.BadZipFile:
        return False


def parse_part(data: bytes) -> etree._Element | None:
    """Read part XML without entities or network access. A broken part gives None (only that object is skipped)."""
    try:
        return etree.fromstring(data, parser=_xml_parser())
    except etree.XMLSyntaxError:
        return None


# ---- Package -----------------------------------------------------------------------------------------------------


# A universal measure (ECMA-376 ST_UniversalMeasure): a number and a unit. Points in one of each unit
_UNIVERSAL_MEASURE = re.compile(r"(-?\d{1,9}(?:\.\d{1,9})?)(mm|cm|in|pt|pc|pi)")
_POINTS_PER_UNIT = {"mm": 72 / 25.4, "cm": 72 / 2.54, "in": 72.0, "pt": 1.0, "pc": 12.0, "pi": 12.0}


def measure_int[T](value: str | None, per_point: int, default: T) -> int | T:
    """A length attribute in its own unit (`per_point` of them in a point: 20 for twips, 2 for half-points). Written as an integer
    it is that unit (`decimal_int`); written as a universal measure (`72pt`, `2.5cm`) it is converted, rounded. Word writes its
    twips that way in Strict Open XML (`w:ind w:left="36pt"`), so an indent read as a plain integer came out as none (Pinned by:
    test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::test_measures_written_with_a_unit_are_read)."""
    number = decimal_int(value, None, signed=True)
    if number is not None or value is None:
        return default if number is None else number
    match = _UNIVERSAL_MEASURE.fullmatch(value.strip())
    if match is None:
        return default
    return round(float(match.group(1)) * _POINTS_PER_UNIT[match.group(2)] * per_point)


def open_package[T](opener: Callable[[BytesIO], T], data: bytes, warnings: list[str],
                    errors: tuple[type[Exception], ...] = ()) -> T:
    """Open an OOXML package with `opener` (`Package.open` of python-docx / python-pptx). If opening fails because of one missing or
    non-XML part (KeyError, XMLSyntaxError) or one member that cannot be read out of the archive (`ZIP_MEMBER_ERRORS` — a picture
    with a broken compressed stream), open once more from a copy with that part and its relationship detached (`repair_package`)
    and leave a warning. The libraries read every part when opening, so one damaged picture rejected the whole document
    (Pinned by: test_office_robustness.py::TestDamagedZipMember).

    The libraries read every part the relationships point to when opening, so a document with a sound body was rejected outright when
    one picture or one numbering definition was missing or docProps was not XML. No copy is made for a healthy document — inspection
    and rebuilding happen only when opening fails, so performance and output are unchanged
    (Pinned by: test_office.py::TestOneBrokenPartDoesNotRejectTheDocument).

    A relationship without its Id, Type or Target fails inside the libraries as a TypeError or AttributeError (python-docx) or an
    InvalidXmlError (python-pptx), in any `.rels`, even one nothing uses. Those are repaired too: when nothing is found to repair,
    the original error stands. python-docx keeps a relationship with no Type, its `reltype` None, so the DOCX readers that pick a
    part by type read it as no type (Pinned by: test_office_robustness.py::TestIncompleteRelationship).

    `errors` are further exceptions of `opener` that mean the same (python-pptx's InvalidXmlError).
    """
    open_errors = (*_OPEN_ERRORS, *errors)
    try:
        return opener(BytesIO(data))
    except open_errors:
        try:
            repaired = repair_package(data)
        except ZIP_MEMBER_ERRORS:  # the archive itself cannot be read: the original error stands
            repaired = None
        if repaired is None:
            raise
    try:
        opened = opener(BytesIO(repaired[0]))
    except open_errors:
        # Deliberate: a copy that reads parts found in another case and still does not open is made again without them, which
        # detaches those parts as missing, with the warning, as before parts were found in another case: a part found that way
        # whose relationships still failed rejected a document that opened with that part detached (Pinned by:
        # test_office_robustness.py::TestTargetInAnotherCase::test_document_that_fails_with_the_part_found_opens_without_it).
        repaired = repair_package(data, in_another_case=False)
        if repaired is None:
            raise
        opened = opener(BytesIO(repaired[0]))
    if repaired[1] is not None:
        warnings.append(repaired[1])
    return opened


def main_part_types(data: bytes) -> tuple[bool, bool]:
    """Do the package relationships (_rels/.rels) name a main part with the Strict Open XML type (purl.oclc.org), and one with
    the Transitional type? Both False if unreadable.

    A DOCX/PPTX saved as a 'Strict Open XML document' differs in every relationship type and namespace, so python-docx / python-pptx
    cannot find the main part. It is not corruption (the 'document structure cannot be read' error): a DOCX is read from its
    Transitional copy (`transitional_package`), a PPTX is refused with the advice to save it again. The DOCX reader looks before
    opening the package, so a package that names only a Strict main part is never opened as it is; the PPTX reader only when
    opening failed. Looking reads the ZIP directory and one small part (Pinned by: test_office_revisions_and_numbering.py::
    TestStrictOpenXmlOfficeDocuments::test_strict_package_is_not_opened_before_the_copy).
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in archive.namelist() else None
    except ZIP_MEMBER_ERRORS:
        return False, False

    types = {relationship.get("Type") for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship")} \
        if root is not None else set()
    return _STRICT_OFFICE_DOCUMENT in types, _OFFICE_DOCUMENT in types


def is_strict_package(data: bytes) -> bool:
    """Is a main part named with the Strict Open XML type in the package relationships (`main_part_types`)?"""
    return main_part_types(data)[0]


# A Strict namespace or relationship type (`http://purl.oclc.org/ooxml/<vocabulary>/<name>`) and its Transitional form
# (`http://schemas.openxmlformats.org/<vocabulary>/2006/<name>`): ECMA-376 Part 1 gives the same vocabularies both names. The
# two property parts and their relationships are spelled apart (`_STRICT_RENAMED`).
_STRICT_NAME = re.compile(r"http://purl\.oclc\.org/ooxml/(wordprocessingml|drawingml|presentationml|spreadsheetml|officeDocument)/"
                          r"([A-Za-z]+)(/[A-Za-z]+)?")
_STRICT_RENAMED = {"extendedProperties": "extended-properties", "customProperties": "custom-properties"}


# What parsing an XML part with the writer below as its target raises for a part that is not well-formed (XMLSyntaxError) or
# whose content the writer cannot write again (a name in no namespace in scope: LookupError; ValueError, TypeError)
_PARSE_ERRORS: tuple[type[Exception], ...] = (etree.LxmlError, LookupError, ValueError, TypeError)


def _transitional_name(value: str) -> str:
    match = _STRICT_NAME.fullmatch(value)
    if match is None:
        return value
    vocabulary, name, tail = match.group(1), match.group(2), match.group(3) or ""
    if tail:
        tail = "/" + _STRICT_RENAMED.get(tail[1:], tail[1:])
    return "http://schemas.openxmlformats.org/" + vocabulary + "/2006/" + _STRICT_RENAMED.get(name, name) + tail


_XML_NAMESPACE = "http://www.w3.org/XML/1998/namespace"
# The prefixes in scope outside the root element (prefix → namespace; namespace → prefix of element names, of attribute names):
# only `xml`, which is never declared
class _Link:
    """A prefix in the list of those bound to one namespace (`_TransitionalWriter`): a doubly linked ring around its head, so a
    prefix is taken out and put back in constant time (put back in the reverse order they were taken out, the ring comes
    back as it was)."""

    __slots__ = ("next", "prefix", "previous")

    def __init__(self, prefix: str | None) -> None:
        self.prefix = prefix
        self.previous = self.next = self

    def append_to(self, head: "_Link") -> "_Link":
        self.previous, self.next = head.previous, head
        head.previous.next = head.previous = self
        return self

    def unlink(self) -> None:
        self.previous.next, self.next.previous = self.next, self.previous

    def relink(self) -> None:
        self.previous.next = self.next.previous = self


# What one namespace declaration changed: its prefix, its links (`_Link`), and the links of the binding of that prefix it hid
_Declared = tuple[str | None, list[_Link], list[_Link]]
# Longest namespace name a part is renamed with (`_TransitionalWriter.start`); real ones are under a hundred characters
_LONGEST_NAMESPACE = 1024
# How many times its part the renamed copy of a part may be (`_TransitionalWriter`)
_GROWTH = 4
# Characters gathered before they are encoded into the copy (`_TransitionalWriter`)
_CHARACTERS_PER_WRITE = 1 << 16
# Chunk size for feeding a member to the parser (`_transitional_part`)
_FEED_CHUNK = 1 << 16


def _escaped_text(text: str) -> str:
    # `replace` returns the string itself when there is nothing to replace, so text without markup characters is not copied
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\r", "&#13;")


def _escaped_value(value: str) -> str:
    return (_escaped_text(value).replace('"', "&quot;").replace("\t", "&#9;").replace("\n", "&#10;"))


class _TransitionalWriter:
    """Parser target that writes the XML part it is fed again, as the parser reads it, with its Strict names in their
    Transitional form: the namespace of each element and attribute name, declared under the prefix the part gave it (so a
    prefix list such as `mc:Ignorable` still names it), and, in a relationships part, the `Type` of each relationship. Text,
    the other attribute values, comments and processing instructions are written as they were read, a CDATA section as the
    same text escaped. Written in UTF-8 with an XML declaration; no tree of the part is built, and `close` gives the copy in
    chunks, written into the package one after the other and let go.
    (lxml's own incremental writer names `xml:space` under a prefix of its own and refuses a comment after the root element.)"""

    def __init__(self, relationships: bool, part_size: int) -> None:
        self._chunks: list[bytes] = []  # the copy, encoded
        # The copy may not grow past a few times the part: the parser hands a name over without the prefix the part wrote it
        # with, so a name is written under the prefix last declared for its namespace, and one prefix of 40,000 characters
        # declared after a short one made a part of 160 KB (a ZIP of 471 bytes) an 800 MB copy. Past the bound the part is
        # copied as written (escaping makes a copy at most five times a part only of `&` in CDATA sections, which Word does
        # not write; Pinned by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::
        # test_copy_grows_no_further_than_a_few_times_the_part)
        self._allowed = _GROWTH * part_size + _CHARACTERS_PER_WRITE
        self._encoded = 0  # bytes in `_chunks`
        self._relationships = relationships
        self._pieces: list[str] = ['<?xml version="1.0" encoding="UTF-8"?>\n']
        self._characters = 0  # in `_pieces`
        # One entry per open element: its qualified name, and what its declarations changed, undone at its end
        self._open: list[tuple[str, list[_Declared]]] = []
        # The prefixes in scope, found from a namespace in constant time: for each namespace, one list of the prefixes now bound
        # to it in the order they were declared (`_Link`), one for element names and one for attribute names (which take no
        # default); a prefix declared again inside for another namespace is taken out of its list and put back at the end of
        # that element, the changes undone in the reverse order they were made, so each list comes back as it was. The last
        # prefix of a list is the one written. A prefix in scope for each name always exists, as the part was read in those
        # names. Rebuilt from every binding in scope at each element that declared one, a part of 16,000 such elements under
        # 16,000 prefixes took 27 s; with a prefix of the copy's own declared instead where the one last declared was taken,
        # each element using it declared it again, and a ZIP of 600 bytes came to 1.5 GB (Pinned by:
        # test_office_revisions_and_numbering.py::TestStrictCopyInLinearTime::test_many_namespace_declarations,
        # TestStrictOpenXmlOfficeDocuments::test_names_keep_their_namespaces_when_their_prefix_is_declared_again,
        # test_copy_declares_no_prefix_of_its_own)
        self._bound: dict[str | None, list[list[_Link]]] = {}  # prefix → the links of each of its bindings, innermost last
        self._lists: dict[tuple[str, bool], _Link] = {}  # (namespace, for an attribute) → the head of its list
        self._bind("xml", _XML_NAMESPACE, [])
        self._tag_open = False  # a start tag is written up to its `>`, so that an empty element closes as `/>`

    def _write(self, piece: str) -> None:
        if self._tag_open:
            self._pieces.append(">")
            self._tag_open = False
        self._pieces.append(piece)
        self._characters += len(piece)
        if self._characters >= _CHARACTERS_PER_WRITE:
            self._flush()

    def _flush(self) -> None:
        chunk = "".join(self._pieces).encode("utf-8")
        self._pieces.clear()
        self._characters = 0
        self._encoded += len(chunk)
        if self._encoded > self._allowed:
            self._chunks.clear()
            raise ValueError("copy too large")
        self._chunks.append(chunk)

    def _bind(self, prefix: str | None, namespace: str, undo: list[_Declared]) -> None:
        bindings = self._bound.setdefault(prefix, [])
        shadowed = bindings[-1] if bindings else []
        for link in shadowed:
            link.unlink()
        links = []
        if namespace:  # not `xmlns=""`
            for attribute in ((False, True) if prefix else (False,)):
                head = self._lists.get((namespace, attribute))
                if head is None:
                    head = self._lists[(namespace, attribute)] = _Link(None)
                links.append(_Link(prefix).append_to(head))
        bindings.append(links)
        undo.append((prefix, links, shadowed))

    def _name(self, name: str, attribute: bool) -> str:
        """The name in its Transitional form, under the prefix last declared for its namespace that is still bound to it."""
        if not name.startswith("{"):
            return name
        namespace, _, local = name[1:].partition("}")
        namespace = _transitional_name(namespace) if namespace.startswith("http://purl.oclc.org/ooxml/") else namespace
        head = self._lists.get((namespace, attribute))
        if head is None or head.previous is head:
            raise LookupError(namespace)  # not in scope: the part is copied as written (`_PARSE_ERRORS`)
        prefix = head.previous.prefix
        return f"{prefix}:{local}" if prefix else local

    def start(self, tag: str, attrib: dict[str, str], nsmap: dict[str, str]) -> None:
        undo: list[_Declared] = []
        declarations = []
        for prefix, namespace in nsmap.items():
            if len(namespace) > _LONGEST_NAMESPACE:
                # The parser hands each name over with its namespace written out: a part of 30,000 elements in a namespace of
                # 1 MB, a ZIP of 1.4 KB, took 10 s (3.5 s for the parser alone), so such a part is copied as written
                # (Pinned by: test_office_revisions_and_numbering.py::TestStrictCopyInLinearTime::test_long_namespace_name)
                raise ValueError("namespace name too long")
            namespace = _transitional_name(namespace)
            self._bind(prefix, namespace, undo)
            declarations.append(f' xmlns{":" + prefix if prefix else ""}="{_escaped_value(namespace)}"')
        name = self._name(tag, False)
        # Fed to a parser target without entities, libxml2 hands each `&` of an attribute value on as `&#38;` (and a written
        # `&#38;` as `&#38;#38;`), where the tree python-docx builds has `&`: written again as given, the `&` of every link
        # (`?a=1&b=2`) and of every value holding one became `&#38;` in the copy (Pinned by:
        # test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::test_attribute_values_keep_their_ampersands)
        self._write(f"<{name}{''.join(declarations)}")
        # Each attribute written as a piece of its own, so the bound on the copy (`_allowed`) is checked within a start tag:
        # joined first, an element of 20,000 attributes under a prefix of 40,000 characters was a string of 800 MB before it
        # was checked (Pinned by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::
        # test_copy_grows_no_further_than_a_few_times_the_part)
        for attribute, value in attrib.items():
            value = value.replace("&#38;", "&")
            value = _transitional_name(value) if self._relationships and attribute == "Type" else value
            self._write(f' {self._name(attribute, True)}="{_escaped_value(value)}"')
        self._tag_open = True
        self._open.append((name, undo))

    def end(self, tag: str) -> None:
        name, undo = self._open.pop()
        if self._tag_open:
            self._pieces.append("/>")
            self._tag_open = False
        else:
            self._write(f"</{name}>")
        for prefix, links, shadowed in reversed(undo):
            for link in reversed(links):
                link.unlink()
            for link in reversed(shadowed):
                link.relink()
            self._bound[prefix].pop()

    def data(self, text: str) -> None:
        self._write(_escaped_text(text))

    def comment(self, text: str) -> None:
        self._write(f"<!--{text}-->")

    def pi(self, target: str, data: str | None = None) -> None:
        self._write(f"<?{target} {data}?>" if data else f"<?{target}?>")

    def close(self) -> list[bytes]:
        self._flush()
        chunks, self._chunks = self._chunks, []  # the parser may outlive the call: it does not hold the copy
        return chunks


def _transitional_part(archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, relationships: bool) -> list[bytes]:
    """An XML member with its Strict names in their Transitional form (`_TransitionalWriter`). Read by an XML parser with the
    settings of `parse_part` and python-docx (no entities, no network, libxml2's limits), so only names are renamed: renamed
    wherever they stood, a paragraph quoting a Strict namespace and a link to one came out as their Transitional forms (Pinned
    by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::
    test_strict_names_in_text_and_link_targets_stay_as_written). The member is fed to the parser in chunks as it is read, so
    neither it nor its decoded text is held whole, in any encoding, and it is read once, in linear time. Found by a hand-written
    scan, spaces closing a start tag or an unclosed comment took seconds per 10 KB, a part stored in UTF-16 was held as its
    decoded text (four bytes a character once one lies outside the BMP), and a declaration after `q:a="1>2"` or written
    `m&#97;in` stayed Strict, its paragraphs lost (Pinned by: test_office_revisions_and_numbering.py::
    TestStrictOpenXmlOfficeDocuments::test_declarations_are_read_by_a_parser, test_part_is_streamed_through_the_parser;
    test_office_revisions_and_numbering.py::TestStrictCopyInLinearTime).

    A member that does not parse is copied as written, so opening the copy reports it as opening a Transitional document does
    (Pinned by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::
    test_part_that_does_not_parse_is_copied_as_written). Raises `ZIP_MEMBER_ERRORS` when the member does not read."""
    parser = etree.XMLParser(target=_TransitionalWriter(relationships, info.file_size), resolve_entities=False, no_network=True)
    parsed = True
    with archive.open(info) as source:
        while chunk := source.read(_FEED_CHUNK):  # read to its end even once parsing failed, to check that it reads
            if parsed:
                try:
                    parser.feed(chunk)
                except _PARSE_ERRORS:
                    parsed = False
    if parsed:
        try:
            return parser.close()
        except _PARSE_ERRORS:
            pass
    del parser
    return [archive.read(info)]


def transitional_package(data: bytes) -> bytes:
    """Copy of a Strict Open XML package (`is_strict_package`) whose XML parts and relationships use the Transitional namespaces
    and relationship types, so python-docx opens it. Only names change; values written as measures (`72pt`, which Word writes in
    Strict where Transitional has twips) are left for the readers (`measure_int`). A member that does not read is left out, so
    that opening detaches it with its relationship as for any damaged part (`open_package`); with several members of one name
    the last one is kept, as when reading.

    Only the members a relationship reaches from the package relationships are copied (`_reached_members`), with their
    relationships and the content types: python-docx opens no other member. Each XML part is read, renamed and written one at a
    time, streamed through a parser (`_transitional_part`); any other member is checked and then streamed in chunks into a
    compressed entry. Copied whole, a member nothing points at (a 100 MB run of zeros, a ZIP under 1 MB) cost three times its
    size, which the same Transitional document never read; and a picture was held whole and stored uncompressed (Pinned by:
    test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments::test_members_no_relationship_reaches_are_not_read).
    Besides the document itself, the copy holds the archive being written, bounded by `max_uncompressed_bytes` (checked before
    extraction), and the renamed form of one XML part, at most four times that part (`_GROWTH`).

    Refused, a document Word saved as 'Strict Open XML Document' was not read at all, though only its names differ (Pinned by:
    test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments)."""
    buffer = BytesIO()
    with (zipfile.ZipFile(BytesIO(data)) as archive,
          zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as target):
        members = {info.filename: info for info in archive.infolist()}  # with duplicate names, the last one
        reached = _reached_members(archive, set(members))
        for name, info in members.items():
            if name.lower() not in reached:
                continue
            if name.lower().endswith((".xml", ".rels")):
                try:
                    chunks = _transitional_part(archive, info, relationships=_is_relationships(name))
                except ZIP_MEMBER_ERRORS:
                    continue
                with target.open(name, "w", force_zip64=True) as sink:
                    for index, chunk in enumerate(chunks):
                        chunks[index] = b""
                        sink.write(chunk)
                        del chunk
            elif _member_reads(archive, info):
                with archive.open(info) as source, target.open(name, "w", force_zip64=True) as sink:
                    shutil.copyfileobj(source, sink, _CHECK_CHUNK)
    # The bytes of the buffer itself: `getvalue` of a BytesIO nothing else reads shares its memory instead of copying it
    return buffer.getvalue()


def _reached_members(archive: zipfile.ZipFile, names: set[str]) -> set[str]:
    """Lower-case names of the members python-docx can open in a package: the content types, the package relationships, every
    internal target a relationship reaches from them (as written and percent-decoded, in any case, so `repair_package` still finds
    a target in another case or encoding) and the relationships of each. Strict and Transitional relationships parts share their
    namespace, so this is read before renaming."""
    by_case: dict[str, list[str]] = {}
    for name in names:
        by_case.setdefault(name.lower(), []).append(name)
    reached = {"[content_types].xml"}
    pending = ["_rels/.rels"]
    while pending:
        relationships = pending.pop()
        if relationships.lower() in reached:
            continue
        reached.add(relationships.lower())
        for member in by_case.get(relationships.lower(), ()):
            root = _parse_member(archive, member)
            if root is None:
                continue
            base = member[: member.rindex("_rels/")]
            for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship"):
                written = relationship.get("Target")
                if not written or relationship.get("TargetMode") == "External":
                    continue
                for target in {_resolve_target(base, written), _resolve_target(base, unquote(written))}:
                    reached.add(target.lower())
                    pending.append(_relationships_of(target))
    return reached


def related_part(owner: object, relationship_type: str) -> object:
    """The part that the first internal relationship of `relationship_type` from `owner` (a part or the package) points at, in the
    order of its relationships part. KeyError when there is none, as python-docx / python-pptx `part_related_by` raises.

    The libraries raise ValueError when two relationships share the type, and so do the properties built on that lookup
    (`main_document_part`, `numbering_part`, `styles`, `core_properties`, `has_notes_slide`, `slide_layout`, `slide_master`): a
    second numbering, styles or document-properties relationship rejected the whole document, and a second notes relationship the
    whole slide. The first one is used, as for the other parts looked up by type (`_docx_parts._raw_part_root`, footnotes,
    comments), and an external relationship is passed over (Pinned by: test_audit_office.py::TestRelationshipTypeGivenTwice)."""
    relationships = getattr(owner, "rels", None)
    if relationships is None:  # a python-pptx package keeps its relationships private
        relationships = owner._rels
    for relationship in relationships.values():
        if relationship.reltype == relationship_type and not relationship.is_external:
            return relationship.target_part
    raise KeyError(relationship_type)


def main_document_part(package: object) -> object:
    """The main part of an opened DOCX / PPTX package (the library's `main_document_part`, through `related_part`)."""
    return related_part(package, _OFFICE_DOCUMENT)


def core_properties(package: object) -> dict[str, str]:
    """Title (`title`) and author (`author`) from the package's document properties (docProps/core.xml). Only what is written is
    included.

    `core_properties` of python-docx / python-pptx builds default properties (title 'Word Document' / 'PowerPoint Presentation') when
    the part is missing, so a title the document does not have came out as metadata (Pinned by: test_office.py::
    TestOfficeLineBreaksNestedFieldsAndMetadata::test_package_without_document_properties_reports_no_library_default_title). Document
    properties are an extra, not the body — for a core.xml whose root element has the wrong namespace the library raised
    AttributeError and the whole document was rejected. If unreadable, they are left empty
    (Pinned by: test_office_robustness.py::TestDocumentPropertiesAndSlideRoots).

    A relationship to a part that is not a document-properties part is left out too, before the part is asked anything: pointing
    back at word/document.xml, python-docx asked the document part for the package's properties again, recursed without end, and
    the document was rejected as corrupted (Pinned by: test_audit_office.py::TestRelationshipPointingBackAtTheMainPart::
    test_document_properties_relationship_to_another_part_leaves_out_title_and_author).
    """
    try:
        part = related_part(package, _CORE_PROPERTIES)
    except KeyError:
        return {}
    if getattr(part, "content_type", None) != _CORE_PROPERTIES_TYPE:
        return {}
    try:
        # python-docx reads the values through the part's `core_properties`; a python-pptx part holds them itself
        properties = getattr(part, "core_properties", part)
        values = {"title": properties.title, "author": properties.author}
    except (AttributeError, KeyError, ValueError, TypeError):
        return {}
    return {name: value for name, value in values.items() if isinstance(value, str) and value}


# A part name as Office writes it: letters, then digits, a dot and a short extension (`image1.png`, `numbering.xml`). Only such
# names are written into the warning of `repair_package`.
_PART_LABEL = re.compile(r"[A-Za-z]{1,24}[0-9]{0,6}\.[A-Za-z0-9]{1,5}")


def repair_package(data: bytes, *, in_another_case: bool = True) -> tuple[bytes, str | None] | None:
    """A copy of the package with the relationships that point to missing parts or non-XML XML parts removed and those parts left
    out, plus the warning text (None when only percent-encoded targets were decoded). None if there is nothing to repair.

    Relationship targets are resolved against the folder of the part that owns the `.rels` (an empty target or a `../..` target
    outside the package is a missing part). A part with no content type also makes the library raise KeyError, so it counts as
    missing. A broken main part (officeDocument) is not repaired — that is real corruption. The copy stream-copies the parts (large
    pictures and videos are not loaded whole into memory) and recompresses them.

    Part names are case-insensitive in a package (OPC), and Word opens a picture written `word/Media/image1.png` behind the target
    `media/image1.png`; the libraries read the archive by the name as written, so the picture was detached as missing. A target
    that names no member but exactly one member when case is ignored is written back as that member's name (Pinned by:
    test_office_robustness.py::TestTargetInAnotherCase). A relationship without its Id, Type or Target is left out: nothing can
    tell what it points at. The relationships of a part found that way are carried over under the name the libraries look for, as
    the part was renamed and its `.rels` may not have been. With `in_another_case` off, targets are looked up as written only.
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        by_case: dict[str, str | None] = {}  # lower-case name → the member of that name, None when two differ only in case
        for name in names:
            by_case[name.lower()] = None if name.lower() in by_case else name
        content_type_of = _content_type_lookup(archive, names)
        main_part = _main_part(archive, names)
        if content_type_of is None or main_part is None:
            return None
        if main_part not in names and in_another_case:
            # The main part in another case (`word/Document.xml`) is the main part too, and its relationship is written back
            # as that member's name below like any other: detection accepts it, and the library raised KeyError for it
            # (Pinned by: test_office_robustness.py::TestTargetInAnotherCase::test_main_part_in_another_case).
            main_part = by_case.get(main_part.lower()) or main_part

        # Check the target of every relationship and remove relationships whose target cannot be read. Each XML part is parsed only once
        readable: dict[str, bool] = {}
        rewritten: dict[str, bytes] = {}
        dropped: set[str] = set()
        broken_relationships: set[str] = set()
        rewritten_targets = incomplete = 0
        found_in_another_case: set[str] = set()
        for rels_name in sorted(name for name in names if _is_relationships(name)):
            root = _parse_member(archive, rels_name)
            if root is None:
                # Deliberate: relationships that are not XML (or do not read out of the archive) are left out of the copy, so the
                # part that owns them opens without relationships (its pictures, links and the like are lost) instead of the whole
                # document being rejected. For the package relationships the main part is found through the content types
                # instead (Pinned by: test_office_robustness.py::TestBrokenRelationshipsPart).
                broken_relationships.add(rels_name)
                continue
            # The folder of the owning part: the text before the last `_rels/` — the first one is wrong for a folder whose name
            # ends in `_rels` (Pinned by: test_office_robustness.py::TestDamagedZipMember::
            # test_relationships_of_a_part_in_a_folder_ending_in_rels_resolve_against_that_folder)
            base = rels_name[: rels_name.rindex("_rels/")]
            changed = False
            for relationship in list(root):
                if relationship.tag != f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship":
                    continue
                if any(relationship.get(attribute) is None for attribute in ("Id", "Type", "Target")):
                    root.remove(relationship)
                    incomplete += 1
                    changed = True
                    continue
                if relationship.get("TargetMode") == "External":
                    continue
                written = relationship.get("Target", "")
                target = _resolve_target(base, written)
                # A target is a URI, so `%6C` stands for 'l' (OPC). The libraries look the target up as written, so a decoded
                # target that names a readable part is written back decoded. A part named as written wins when it exists (a
                # file name may hold a percent sign), also over a part with the decoded name
                # (Pinned by: test_office_robustness.py::TestPercentEncodedTargets).
                decoded = unquote(written)
                if (decoded != written and target not in names
                        and _is_readable(archive, names, content_type_of, readable, _resolve_target(base, decoded))):
                    relationship.set("Target", decoded)
                    rewritten_targets += 1
                    changed = True
                    continue
                if target == main_part or _is_readable(archive, names, content_type_of, readable, target):
                    continue
                member = by_case.get(target.lower()) if in_another_case and target not in names else None
                if member is not None and _is_readable(archive, names, content_type_of, readable, member):
                    relationship.set("Target", "/" + member)
                    found_in_another_case.add(member)
                    rewritten_targets += 1
                    changed = True
                    continue
                root.remove(relationship)
                dropped.add(target)
                changed = True
            if changed:
                rewritten[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if "_rels/.rels" in broken_relationships:
            rewritten["_rels/.rels"] = _package_relationships(main_part)
        dropped |= broken_relationships
        if not dropped and not rewritten_targets and not incomplete:
            return None
        # copied name → the name it is written under: the relationships of a part found in another case, in their own case
        renamed = {by_case[_relationships_of(member).lower()]: _relationships_of(member) for member in found_in_another_case
                   if _relationships_of(member) not in names and by_case.get(_relationships_of(member).lower())}
        # Their targets were checked against the folder they are in, and a relative one would point into the folder of the new
        # name, where a part beside it is not: they are written as the absolute names they were checked as. Carried over as
        # written, `itemProps1.xml` beside `customXml/item1.xml` was looked for as `CustomXml/itemProps1.xml` and the whole
        # document was rejected (Pinned by: test_office_robustness.py::TestTargetInAnotherCase::
        # test_relationships_carried_to_another_folder_case_keep_their_targets).
        for old_name in renamed:
            root = (etree.fromstring(rewritten[old_name], parser=_xml_parser()) if old_name in rewritten
                    else _parse_member(archive, old_name))
            if root is None:
                continue
            old_base = old_name[: old_name.rindex("_rels/")]
            for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship"):
                written = relationship.get("Target", "")
                if relationship.get("TargetMode") != "External" and not written.startswith("/"):
                    relationship.set("Target", "/" + _resolve_target(old_base, written))
            rewritten[old_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)

        # Broken parts are left out of the copy together with their .rels; the rest is carried over as is (with duplicate names, the
        # last one, as when reading)
        excluded = {name for name in dropped if name in names}
        excluded |= {_relationships_of(name) for name in excluded} & names
        excluded -= set(rewritten)
        members = {info.filename: info for info in archive.infolist()}
        # A member nothing points at is never opened by the libraries, but copying it would fail on it: it is left out too
        excluded |= {name for name, info in members.items() if name not in excluded and name not in rewritten
                     and not _member_reads(archive, info)}
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target_archive:
            for info in members.values():
                if info.filename in excluded:
                    continue
                if info.filename in rewritten:
                    target_archive.writestr(renamed.get(info.filename, info.filename), rewritten[info.filename])
                    continue
                entry = zipfile.ZipInfo(renamed.get(info.filename, info.filename), date_time=info.date_time)
                entry.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info) as source, target_archive.open(entry, "w") as sink:
                    shutil.copyfileobj(source, sink)

    left_out = incomplete_relationships_warning(incomplete) if incomplete else None
    if not dropped:  # only targets were decoded or relationships without a target left out: no part is missing
        return buffer.getvalue(), left_out
    # Deliberate: a part is named only when its name has the plain shape of a part name (`_PART_LABEL`); the others are only
    # counted. The names come from relationship targets, which the document's author writes: a sentence of any length put there
    # appeared word for word in the warning (Pinned by: test_audit_core.py::TestDetachedPartNamesInTheWarning).
    labels = sorted({posixpath.basename(name) or name for name in dropped})
    named = [label for label in labels if _PART_LABEL.fullmatch(label)][:4]
    shown = ", ".join(named) + (f" and {len(labels) - len(named)} more" if named and len(labels) > len(named) else "")
    detached = (f"Read after detaching {len(dropped)} missing or broken part(s){f' ({shown})' if shown else ''}. "
                "The contents of those parts (pictures, numbering, styles, etc.) may be missing.")
    return buffer.getvalue(), f"{detached} {left_out}" if left_out else detached


def members_named_as_referenced(data: bytes) -> bytes | None:
    """A copy of the package in which a member that a content-type override or a relationship target names only in another case
    is stored under the name it is referenced by, and its relationships beside it under the name that goes with it. None when no
    member is renamed.

    For readers that look parts up by the name a reference gives (openpyxl): part names are case-insensitive (OPC), but a sheet
    stored as `xl/worksheets/Sheet2.xml` behind the target `worksheets/sheet2.xml` was left out of the workbook without a word,
    and a workbook part or styles part in another case rejected the document or lost every display format
    (Pinned by: test_office_robustness.py::TestTargetInAnotherCase::test_xlsx_parts_in_another_case). A member is renamed only
    when it is the one member of that name ignoring case, every reference names it in the same case and none names it as it is,
    so a renamed member is never one a reference found before; nothing is guessed between two. The relationships of a renamed
    part are read again under its new name, as their relative targets are resolved against its folder. A percent-encoded target
    is looked up decoded as well (`named_part`).
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        by_case: dict[str, str | None] = {}  # lower-case name → the member of that name, None when two differ only in case
        for name in names:
            by_case[name.lower()] = None if name.lower() in by_case else name
        renamed: dict[str, str] = {}  # member → the name it is referenced by
        ambiguous: set[str] = set()  # members referenced in two cases, and members some reference names as they are

        def note(reference: str) -> str | None:
            """Note the member `reference` names in another case; the member's name as it will be stored, or None."""
            candidates = (reference, posixpath.normpath(unquote(reference))) if "%" in reference else (reference,)
            for candidate in candidates:
                if candidate in names:
                    ambiguous.add(candidate)
                    return None
                member = by_case.get(candidate.lower())
                if member is None:
                    continue
                if renamed.get(member, candidate) != candidate:
                    ambiguous.add(member)
                    return None
                renamed[member] = candidate
                return candidate
            return None

        types = _parse_member(archive, "[Content_Types].xml") if "[Content_Types].xml" in names else None
        if types is not None:
            for override in types.iter(f"{{{_CONTENT_TYPES}}}Override"):
                note(override.get("PartName", "").lstrip("/"))
        # relationships to read: (member, the name it is read under). A renamed part's relationships follow it
        pending = [(name, name) for name in sorted(names) if _is_relationships(name)]
        read: set[tuple[str, str]] = set()
        while pending:
            member, name = pending.pop()
            if (member, name) in read:
                continue
            read.add((member, name))
            root = _parse_member(archive, member)
            if root is None:
                continue
            base = name[: name.rindex("_rels/")]
            for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship"):
                if relationship.get("TargetMode") == "External" or not relationship.get("Target"):
                    continue
                stored = note(_resolve_target(base, relationship.get("Target", "")))
                if stored is None:
                    continue
                relationships = _relationships_of(stored)
                if relationships in names:
                    continue
                relationships_member = by_case.get(relationships.lower())
                if relationships_member is not None and note(relationships) == relationships:
                    pending.append((relationships_member, relationships))
        for member in ambiguous:
            renamed.pop(member, None)
        if not renamed:
            return None

        # Copied as in `repair_package` (with duplicate names, the last one, as when reading). A member that does not read through
        # leaves the package as it is, for the reader to judge as before
        members = {info.filename: info for info in archive.infolist()}
        buffer = BytesIO()
        try:
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
                for info in members.values():
                    entry = zipfile.ZipInfo(renamed.get(info.filename, info.filename), date_time=info.date_time)
                    entry.compress_type = zipfile.ZIP_DEFLATED
                    with archive.open(info) as source, copy.open(entry, "w") as sink:
                        shutil.copyfileobj(source, sink)
        except ZIP_MEMBER_ERRORS:
            return None
    return buffer.getvalue()


def incomplete_relationships_warning(count: int) -> str:
    """The warning for relationships left out because they have no Id, Type or Target (`repair_package`)."""
    return f"Read after leaving out {count} relationship(s) with no Id, Type or Target. Whatever they pointed at may be missing."


def _is_relationships(name: str) -> bool:
    return name.endswith(".rels") and posixpath.basename(posixpath.dirname(name)) == "_rels"


def _relationships_of(name: str) -> str:
    """Name of a part's relationships file: `word/document.xml` → `word/_rels/document.xml.rels`."""
    return posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")


def _resolve_target(base: str, target: str) -> str:
    """Relationship target (a path relative to the folder of the part that owns the .rels, or an absolute path starting with `/`)
    → name inside the package."""
    return posixpath.normpath(posixpath.join("/" + base, target)).lstrip("/")


def named_part(name: str, exists: Callable[[str], bool]) -> str:
    """The part a resolved relationship target names, for the readers that look parts up themselves (XLSX parts, HWPX chart
    relationships): `name` as written when that part exists or holds no percent sign, otherwise its decoded form when that part
    exists — the rule of `repair_package`. openpyxl and the part readers of the converter looked the target up as written, so a
    sheet behind `sh%65et1.xml` was left out without a word, and so were the comments, pictures and charts of a sheet
    (Pinned by: test_office_robustness.py::TestPercentEncodedTargets)."""
    if "%" not in name or exists(name):
        return name
    decoded = posixpath.normpath(unquote(name))
    return decoded if exists(decoded) else name


def _parse_member(archive: zipfile.ZipFile, name: str) -> etree._Element | None:
    """Root of an XML member. None when it does not parse or cannot be read out of the archive."""
    try:
        return etree.fromstring(archive.read(name), parser=_xml_parser())
    except (etree.XMLSyntaxError, *ZIP_MEMBER_ERRORS):
        return None


def _member_reads(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    """Can the member be read to its end (decompressed, CRC checked)? Streamed in chunks; nothing is kept."""
    try:
        with archive.open(info) as stream:
            while stream.read(_CHECK_CHUNK):
                pass
    except ZIP_MEMBER_ERRORS:
        return False
    return True


def _content_type_lookup(archive: zipfile.ZipFile, names: set[str]) -> Callable[[str], str | None] | None:
    """Part name → content type (an Override first, otherwise the extension's Default). None if the content types part is missing
    or broken."""
    root = _parse_member(archive, "[Content_Types].xml") if "[Content_Types].xml" in names else None
    if root is None:
        return None
    defaults = {e.get("Extension", "").lower(): e.get("ContentType", "") for e in root.iter(f"{{{_CONTENT_TYPES}}}Default")}
    overrides = {e.get("PartName", "").lstrip("/").lower(): e.get("ContentType", "")
                 for e in root.iter(f"{{{_CONTENT_TYPES}}}Override")}

    def lookup(name: str) -> str | None:
        basename = name.rsplit("/", 1)[-1]
        extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
        return overrides.get(name.lower()) or defaults.get(extension) or None

    return lookup


def _main_part(archive: zipfile.ZipFile, names: set[str]) -> str | None:
    """The officeDocument target of the package relationships (_rels/.rels). When those are present but not XML, the one part the
    content types give a main-document type (`….main+xml`). None if neither names it."""
    root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in names else None
    if root is None:
        return _main_part_by_content_type(archive, names) if "_rels/.rels" in names else None
    for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship"):
        if relationship.get("Type") == _OFFICE_DOCUMENT and relationship.get("TargetMode") != "External":
            return _resolve_target("", relationship.get("Target", ""))
    return None


def _main_part_by_content_type(archive: zipfile.ZipFile, names: set[str]) -> str | None:
    """The only part whose content-type override is a main-document type (`document.main+xml`, `presentation.main+xml`, their
    macro-enabled and template forms). None when there is not exactly one or it is not in the package: with two, which of them
    the document shows is not known (Pinned by: test_office_robustness.py::TestBrokenRelationshipsPart::
    test_two_parts_of_a_main_document_type_name_no_main_part)."""
    root = _parse_member(archive, "[Content_Types].xml") if "[Content_Types].xml" in names else None
    if root is None:
        return None
    found = {element.get("PartName", "").lstrip("/") for element in root.iter(f"{{{_CONTENT_TYPES}}}Override")
             if element.get("ContentType", "").endswith(".main+xml")}
    return next(iter(found)) if len(found) == 1 and found <= names else None


def _package_relationships(main_part: str) -> bytes:
    """Package relationships (_rels/.rels) that point only at the main part: they stand in for package relationships that are not
    XML, so document properties and thumbnails are left out."""
    root = etree.Element(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationships", nsmap={None: _PACKAGE_RELATIONSHIPS})
    etree.SubElement(root, f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship", Id="rId1", Type=_OFFICE_DOCUMENT, Target="/" + main_part)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _is_readable(archive: zipfile.ZipFile, names: set[str], content_type_of: Callable[[str], str | None],
                 readable: dict[str, bool], name: str) -> bool:
    """Does the part exist, have a content type, read out of the archive and, if it is an XML type, parse?"""
    content_type = content_type_of(name) if name in names else None
    if content_type is None:
        return False
    if name not in readable:
        if content_type.endswith("+xml") or content_type in _XML_CONTENT_TYPES:
            readable[name] = _parse_member(archive, name) is not None
        else:
            readable[name] = _member_reads(archive, archive.getinfo(name))
    return readable[name]
