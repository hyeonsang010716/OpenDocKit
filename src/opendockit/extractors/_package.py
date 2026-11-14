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
import zipfile
import zlib

from lxml import etree


_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)

# What reading one damaged member of a ZIP archive can raise: a bad CRC or local header (BadZipFile), a broken compressed stream
# (zlib.error, lzma.LZMAError, OSError from bz2), a stream cut short (EOFError), an encrypted member (RuntimeError), an unknown
# compression method (NotImplementedError), and odd header values (ValueError). Catch these around a member read so that one member
# skips only itself (Pinned by: test_office_robustness.py::TestDamagedZipMember).
ZIP_MEMBER_ERRORS: tuple[type[Exception], ...] = (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, RuntimeError,
                                                  NotImplementedError, ValueError)
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


def parse_part(data: bytes) -> etree._Element | None:
    """Read part XML without entities or network access. A broken part gives None (only that object is skipped)."""
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return None


# ---- Package -----------------------------------------------------------------------------------------------------


def open_package[T](opener: Callable[[BytesIO], T], data: bytes, warnings: list[str]) -> T:
    """Open an OOXML package with `opener` (`Package.open` of python-docx / python-pptx). If opening fails because of one missing or
    non-XML part (KeyError, XMLSyntaxError) or one member that cannot be read out of the archive (`ZIP_MEMBER_ERRORS` — a picture
    with a broken compressed stream), open once more from a copy with that part and its relationship detached (`repair_package`)
    and leave a warning. The libraries read every part when opening, so one damaged picture rejected the whole document
    (Pinned by: test_office_robustness.py::TestDamagedZipMember).

    The libraries read every part the relationships point to when opening, so a document with a sound body was rejected outright when
    one picture or one numbering definition was missing or docProps was not XML. No copy is made for a healthy document — inspection
    and rebuilding happen only when opening fails, so performance and output are unchanged
    (Pinned by: test_office.py::TestOneBrokenPartDoesNotRejectTheDocument).
    """
    try:
        return opener(BytesIO(data))
    except (KeyError, etree.XMLSyntaxError, *ZIP_MEMBER_ERRORS):
        try:
            repaired = repair_package(data)
        except ZIP_MEMBER_ERRORS:  # the archive itself cannot be read: the original error stands
            repaired = None
        if repaired is None:
            raise
        warnings.append(repaired[1])
        return opener(BytesIO(repaired[0]))


def is_strict_package(data: bytes) -> bool:
    """Is the main-part relationship type in the package relationships (_rels/.rels) the Strict Open XML one (purl.oclc.org)? False
    if unreadable.

    A DOCX/PPTX saved as a 'Strict Open XML document' differs in every relationship type and namespace, so python-docx / python-pptx
    cannot find the main part. It is not corruption (the 'document structure cannot be read' error) but an unsupported format, so, to
    advise saving again, we look only when opening fails (no cost for healthy documents.
    Pinned by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments).
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in archive.namelist() else None
    except ZIP_MEMBER_ERRORS:
        return False

    relationships = root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship") if root is not None else []
    return any(relationship.get("Type") == _STRICT_OFFICE_DOCUMENT for relationship in relationships)


def core_properties(package: object) -> dict[str, str]:
    """Title (`title`) and author (`author`) from the package's document properties (docProps/core.xml). Only what is written is
    included.

    `core_properties` of python-docx / python-pptx builds default properties (title 'Word Document' / 'PowerPoint Presentation') when
    the part is missing, so a title the document does not have came out as metadata (Pinned by: test_office.py::
    TestOfficeLineBreaksNestedFieldsAndMetadata::test_package_without_document_properties_reports_no_library_default_title). Document
    properties are an extra, not the body — for a core.xml whose root element has the wrong namespace the library raised
    AttributeError and the whole document was rejected. If unreadable, they are left empty
    (Pinned by: test_office_robustness.py::TestDocumentPropertiesAndSlideRoots).
    """
    try:
        package.part_related_by(_CORE_PROPERTIES)
    except KeyError:
        return {}
    try:
        properties = package.core_properties
        values = {"title": properties.title, "author": properties.author}
    except (AttributeError, KeyError, ValueError, TypeError):
        return {}
    return {name: value for name, value in values.items() if isinstance(value, str) and value}


# A part name as Office writes it: letters, then digits, a dot and a short extension (`image1.png`, `numbering.xml`). Only such
# names are written into the warning of `repair_package`.
_PART_LABEL = re.compile(r"[A-Za-z]{1,24}[0-9]{0,6}\.[A-Za-z0-9]{1,5}")


def repair_package(data: bytes) -> tuple[bytes, str] | None:
    """A copy of the package with the relationships that point to missing parts or non-XML XML parts removed and those parts left
    out, plus the warning text. None if there is nothing to detach.

    Relationship targets are resolved against the folder of the part that owns the `.rels` (an empty target or a `../..` target
    outside the package is a missing part). A part with no content type also makes the library raise KeyError, so it counts as
    missing. A broken main part (officeDocument) is not repaired — that is real corruption. The copy stream-copies the parts (large
    pictures and videos are not loaded whole into memory) and recompresses them.
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        content_type_of = _content_type_lookup(archive, names)
        main_part = _main_part(archive, names)
        if content_type_of is None or main_part is None:
            return None

        # Check the target of every relationship and remove relationships whose target cannot be read. Each XML part is parsed only once
        readable: dict[str, bool] = {}
        rewritten: dict[str, bytes] = {}
        dropped: set[str] = set()
        for rels_name in sorted(name for name in names if _is_relationships(name)):
            root = _parse_member(archive, rels_name)
            if root is None:
                continue
            # The folder of the owning part: the text before the last `_rels/` — the first one is wrong for a folder whose name
            # ends in `_rels` (Pinned by: test_office_robustness.py::TestDamagedZipMember::
            # test_relationships_of_a_part_in_a_folder_ending_in_rels_resolve_against_that_folder)
            base = rels_name[: rels_name.rindex("_rels/")]
            removed = False
            for relationship in list(root):
                if relationship.tag != f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship" or relationship.get("TargetMode") == "External":
                    continue
                target = _resolve_target(base, relationship.get("Target", ""))
                if target == main_part or _is_readable(archive, names, content_type_of, readable, target):
                    continue
                root.remove(relationship)
                dropped.add(target)
                removed = True
            if removed:
                rewritten[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if not dropped:
            return None

        # Broken parts are left out of the copy together with their .rels; the rest is carried over as is (with duplicate names, the
        # last one, as when reading)
        excluded = {name for name in dropped if name in names}
        excluded |= {_relationships_of(name) for name in excluded} & names
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
                    target_archive.writestr(info.filename, rewritten[info.filename])
                    continue
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info) as source, target_archive.open(entry, "w") as sink:
                    shutil.copyfileobj(source, sink)

    # Deliberate: a part is named only when its name has the plain shape of a part name (`_PART_LABEL`); the others are only
    # counted. The names come from relationship targets, which the document's author writes: a sentence of any length put there
    # appeared word for word in the warning (Pinned by: test_audit_core.py::TestDetachedPartNamesInTheWarning).
    labels = sorted({posixpath.basename(name) or name for name in dropped})
    named = [label for label in labels if _PART_LABEL.fullmatch(label)][:4]
    shown = ", ".join(named) + (f" and {len(labels) - len(named)} more" if named and len(labels) > len(named) else "")
    return buffer.getvalue(), (f"Read after detaching {len(dropped)} missing or broken part(s){f' ({shown})' if shown else ''}. "
                               "The contents of those parts (pictures, numbering, styles, etc.) may be missing.")


def _is_relationships(name: str) -> bool:
    return name.endswith(".rels") and posixpath.basename(posixpath.dirname(name)) == "_rels"


def _relationships_of(name: str) -> str:
    """Name of a part's relationships file: `word/document.xml` → `word/_rels/document.xml.rels`."""
    return posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")


def _resolve_target(base: str, target: str) -> str:
    """Relationship target (a path relative to the folder of the part that owns the .rels, or an absolute path starting with `/`)
    → name inside the package."""
    return posixpath.normpath(posixpath.join("/" + base, target)).lstrip("/")


def _parse_member(archive: zipfile.ZipFile, name: str) -> etree._Element | None:
    """Root of an XML member. None when it does not parse or cannot be read out of the archive."""
    try:
        return etree.fromstring(archive.read(name), parser=_XML_PARSER)
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
    """The officeDocument target of the package relationships (_rels/.rels). None if missing or broken."""
    root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in names else None
    for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship") if root is not None else []:
        if relationship.get("Type") == _OFFICE_DOCUMENT and relationship.get("TargetMode") != "External":
            return _resolve_target("", relationship.get("Target", ""))
    return None


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
