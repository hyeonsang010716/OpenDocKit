"""XLS (BIFF) → one Markdown table per sheet, with the text boxes, pictures, form controls and chart sheets of its drawing
records.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
import hashlib
from io import BytesIO, StringIO
import struct
import zlib

import olefile
from openpyxl.styles.numbers import is_date_format
import xlrd

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._sheet_format import _ELAPSED_FORMAT, _format_within_length, _is_weekday_only, _shows_date, format_cell
from opendockit.extractors._sheet_layout import (
    _CHECKED,
    _MAX_MERGED_CELLS,
    _UNCHECKED,
    _Across,
    _append_sheet,
    _collect_rows,
    _comment_block,
    _level_marked,
    _repeat_merge,
    _SheetRows,
)
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction


def extract_xls(data: bytes, options: ConvertOptions) -> Extraction:
    # xlrd writes minor inconsistencies of the file ("WARNING *** OLE2 inconsistency …") to logfile (sys.stdout by default). Mixed
    # into the caller's output, it broke the CLI `--json` result. It is redirected to a sink
    # (Pinned by: test_spreadsheet.py::test_xls_reader_does_not_write_to_stdout).
    # ragged_rows: each row is built only up to its own cells. The default pads every row to the widest row (256 columns from one
    # far cell)
    # (Pinned by: test_spreadsheet.py::TestXlsResourceBounds).
    # The OLE container is not left to xlrd; olefile extracts just the workbook stream and passes it on. xlrd's OLE parsing does
    # not check for chain cycles in streams under 4KB (mini streams), so a 5KB file whose mini FAT points to itself looped forever
    # and used several GB. olefile follows a chain only as far as the stream size
    # (Pinned by: test_spreadsheet.py::TestXlsOleContainer).
    stream = _without_default_password(_xls_workbook_stream(data))
    try:
        book = _open_xls_book(stream)
    except xlrd.biffh.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise EncryptedDocumentError("This Excel document is password-protected. Remove the password and try again.") from exc
        raise CorruptedDocumentError("The XLS document structure could not be read.") from exc

    extraction = Extraction()
    drawings = _xls_drawings(stream, options)
    if drawings is None:
        extraction.warnings.append("Text box and shape text and pictures in the XLS could not be read, so only cell contents were converted.")
    for name in _xls_chart_sheets(stream, options.include_hidden_sheets):
        extraction.warnings.append(f"Chart sheet '{name}' was not read "
                                   "(XLS charts are not converted to value tables — saving as XLSX converts them).")
    unresolved, placeholder_zeros = _xls_formulas_without_results(stream)
    if unresolved:
        extraction.warnings.append(f"The results of {unresolved:,} formula(s) are not stored in the file, so those cells were read as blank "
                                   "(opening and re-saving the file in Excel converts them to values).")
    if placeholder_zeros:
        count = sum(len(cells) for cells in placeholder_zeros.values())
        extraction.warnings.append(f"The results of {count:,} formula(s) are stored only as 0 (the file was made by a program that does not "
                                   "calculate), so those cells were read as blank "
                                   "(opening and re-saving the file in Excel converts them to values).")
    holding_charts = _xls_sheets_holding_charts(stream)
    for sheet_index in range(book.nsheets):
        sheet = book.sheet_by_index(sheet_index)
        if sheet.visibility != 0 and not options.include_hidden_sheets:
            extraction.warnings.append(f"Hidden sheet '{sheet.name}' was excluded.")
            continue
        if sheet.nrows == 0 and sheet_index in holding_charts:
            extraction.warnings.append(f"Chart sheet '{sheet.name}' was not read "
                                       "(XLS charts are not converted to value tables — saving as XLSX converts them).")

        blank = placeholder_zeros.get(sheet_index, set())  # formula cells whose 0 in the result slot cannot be trusted

        offset = _xls_first_used_column(sheet)  # the column limit counts from the first used column (`_first_used_column`)
        rows = _collect_rows((_xls_row(book, sheet, row_index, blank, options.max_sheet_cols, offset)
                              for row_index in range(sheet.nrows)), options, sheet.name, extraction)
        across = _fill_xls_merges(sheet, rows, extraction.warnings, options.max_sheet_cols, offset)
        drawn = (_xls_drawing_blocks(drawings[sheet_index], sheet.name, extraction, options)
                 if drawings and sheet_index < len(drawings) and not extraction.exceeded else [])
        if not extraction.exceeded:
            notes = [(row + 1, column + 1, note.author or "", note.text or "", [])
                     for (row, column), note in sheet.cell_note_map.items()]
            row_at = _SheetRows(dict(enumerate(rows, start=1)), offset)
            drawn.append(_comment_block(notes, row_at, extraction, options))
        _append_sheet(extraction, sheet.name, rows, drawn, across)
        if extraction.exceeded:
            break

    extraction.metadata["sheet_count"] = str(len(extraction.sections))
    return extraction


# The default password Excel uses when it encrypts without asking the person opening the file for a password (workbook protection,
# read-only recommended, etc.) ([MS-OFFCRYPTO] 2.3.6.4 etc.).
_DEFAULT_XLS_PASSWORD = "VelvetSweatshop".encode("utf-16-le")
_BIFF_FILEPASS = 0x002F
# Records that are not encrypted: BOF, FILEPASS, USREXCL, FILELOCK, INTERFACEHDR, RRDINFO, RRDHEAD. BOUNDSHEET has only its first 4
# bytes (the sheet position) in plaintext ([MS-XLS] 2.2.10).
_UNENCRYPTED_RECORDS = frozenset((0x0809, 0x002F, 0x0194, 0x0195, 0x00E1, 0x0196, 0x0138))
_RC4_BLOCK_BYTES = 1024
# Record type put in place of FILEPASS after decryption. Both xlrd and this module's scanner skip it as an unknown id (the length is
# kept, so sheet positions are unchanged).
_BIFF_IGNORED = 0x0FFF


def _without_default_password(stream: bytes) -> bytes:
    """Decrypt a workbook stream RC4-encrypted with the default password (VelvetSweatshop). If it is not encrypted, or uses another
    password or method, it is left as is (xlrd rejects it as a 'password-protected document').

    Excel also encrypts files that do not ask the opener for a password, saving them with this default password. Excel and
    LibreOffice open them directly, but they were rejected as a 'password-protected Excel document'. Standard RC4 (version 1.1,
    MD5) and RC4 CryptoAPI (versions 2–4.2, SHA-1, 40/128-bit) are decrypted, and the password is confirmed with the verifier.
    Known accepted gap: XOR obfuscation (the old method) is not decrypted and is rejected
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestXlsEncryptedWithTheDefaultPassword).
    """
    records = _biff_records(stream)
    begin, filepass = next(records, None), next(records, None)
    if begin is None or filepass is None or begin[1] != _BIFF_BOF or filepass[1] != _BIFF_FILEPASS:
        return stream
    key_of = _default_password_key(filepass[2])
    if key_of is None:
        return stream

    # The keystream is determined by stream position (a new key for every 1,024-byte block). Plaintext records and record headers
    # take up positions too.
    plain = bytearray(stream)
    keystream: tuple[int, bytes] = (-1, b"")  # current block number and its keystream. Positions only move forward
    for position, record_id, body in records:
        if record_id in _UNENCRYPTED_RECORDS:
            continue
        at, end = position + 4 + (4 if record_id == _BIFF_BOUNDSHEET else 0), position + 4 + len(body)
        while at < end:
            block, offset = divmod(at, _RC4_BLOCK_BYTES)
            if keystream[0] != block:
                keystream = (block, _rc4_keystream(key_of(block), _RC4_BLOCK_BYTES))
            size = min(end - at, _RC4_BLOCK_BYTES - offset)
            mixed = int.from_bytes(plain[at:at + size], "little") ^ int.from_bytes(keystream[1][offset:offset + size], "little")
            plain[at:at + size] = mixed.to_bytes(size, "little")
            at += size
    struct.pack_into("<H", plain, filepass[0], _BIFF_IGNORED)
    return bytes(plain)


def _default_password_key(filepass: bytes) -> Callable[[int], bytes] | None:
    """A function block number → RC4 key, if FILEPASS is RC4 and the verifier of the default password matches. Otherwise None."""
    if len(filepass) < 6 or struct.unpack_from("<H", filepass)[0] != 1:
        return None  # 0 = XOR obfuscation
    major, minor = struct.unpack_from("<HH", filepass, 2)
    if major == 1 and minor == 1 and len(filepass) >= 54:
        salt, sealed, check = filepass[6:22], filepass[22:38], filepass[38:54]
        first = hashlib.md5(_DEFAULT_XLS_PASSWORD).digest()[:5]
        second = hashlib.md5((first + salt) * 16).digest()[:5]

        def key_of(block: int) -> bytes:
            return hashlib.md5(second + struct.pack("<I", block)).digest()

        digest = hashlib.md5
    elif major in (2, 3, 4) and minor == 2 and len(filepass) >= 14:
        header_size = struct.unpack_from("<I", filepass, 10)[0]
        verifier_at = 14 + header_size
        if header_size < 20 or len(filepass) < verifier_at + 60:
            return None
        key_bits = struct.unpack_from("<I", filepass, 14 + 16)[0] or 40
        salt, sealed = filepass[verifier_at + 4:verifier_at + 20], filepass[verifier_at + 20:verifier_at + 36]
        check = filepass[verifier_at + 40:verifier_at + 60]
        base = hashlib.sha1(salt + _DEFAULT_XLS_PASSWORD).digest()

        def key_of(block: int) -> bytes:
            key = hashlib.sha1(base + struct.pack("<I", block)).digest()
            return key[:5] + bytes(11) if key_bits == 40 else key[:key_bits // 8]

        digest = hashlib.sha1
    else:
        return None

    opened = _rc4_keystream(key_of(0), len(sealed) + len(check))
    verifier = bytes(a ^ b for a, b in zip(sealed + check, opened, strict=True))
    return key_of if digest(verifier[:16]).digest() == verifier[16:] else None


def _rc4_keystream(key: bytes, length: int) -> bytes:
    """`length` bytes of RC4 keystream. An empty key gives an empty stream (the caller's verifier will not match)."""
    if not key:
        return bytes(length)
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    stream = bytearray(length)
    i = j = 0
    for index in range(length):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        stream[index] = state[(state[i] + state[j]) & 0xFF]
    return bytes(stream)


def _open_xls_book(stream: bytes) -> xlrd.book.Book:
    """Open the workbook stream. Without a CODEPAGE record, 8-bit text (LABEL etc. of BIFF5 and below) is decoded as CP949.

    A BIFF 'Excel download' without OLE (PHP xlsBOF style) writes no CODEPAGE and writes text as CP949 bytes. xlrd then decoded it
    as latin-1, so '이름' came out as 'ÀÌ¸§'. If some text does not decode as CP949, the book is reopened with the xlrd default
    (latin-1) — with CP949 a failure is known only after reading all sheets, so only then are sheets read up front
    (on_demand=False). BIFF8 text is Unicode and independent of the code page. Known accepted gap: among Western European text
    without CODEPAGE, byte sequences that also decode as CP949 are read as Hangul (Korean documents take priority)
    (Pinned by: test_spreadsheet.py::TestRawBiffEncoding).
    """
    options = {"formatting_info": True, "on_demand": True, "logfile": StringIO(), "ragged_rows": True}
    if any(record_id == _BIFF_CODEPAGE for _, record_id, _ in _biff_records(stream)):
        return xlrd.open_workbook(file_contents=stream, **options)
    try:
        return xlrd.open_workbook(file_contents=stream, **{**options, "on_demand": False}, encoding_override="cp949")
    except UnicodeDecodeError:
        return xlrd.open_workbook(file_contents=stream, **options)


def _xls_first_used_column(sheet: xlrd.sheet.Sheet) -> int:
    """Index of the leftmost column holding a value in an XLS sheet (0-based, 0 if none). Same rule as `_first_used_column` for XLSX."""
    empty = (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK)
    firsts = (next((column for column, cell in enumerate(sheet.row(row)) if cell.ctype not in empty and cell.value != ""), None)
              for row in range(sheet.nrows))
    return min((column for column in firsts if column is not None), default=0)


def _xls_row(book: xlrd.book.Book, sheet: xlrd.sheet.Sheet, row_index: int, blank: set[tuple[int, int]],
             max_cols: int, offset: int = 0) -> tuple[str, ...]:
    """Cell texts of one XLS row. As for XLSX, it builds only up to the column limit, and the first value beyond the limit is placed
    at position limit+1 just so that the 'columns excluded' warning fires. `blank` is the formula cells emptied because the 0 in
    their result slot cannot be trusted; `offset` is the number of leading empty columns dropped from the row
    (`_xls_first_used_column`)."""
    rich = sheet.rich_text_runlist_map

    def text(col: int) -> str:
        if (row_index, col) in blank:
            return ""
        cell = sheet.cell(row_index, col)
        if (row_index, col) in rich and cell.ctype == xlrd.XL_CELL_TEXT:
            return _xls_rich_text(cell.value, rich[row_index, col], lambda index: _xls_escapement(book, index))
        return _xls_cell_text(cell, book)

    length = sheet.row_len(row_index)
    values = [text(col) for col in range(offset, min(length, offset + max_cols))]
    first = next((col for col, value in enumerate(values) if value), None)
    cell = sheet.cell(row_index, offset + first) if first is not None else None
    if cell is not None and cell.ctype == xlrd.XL_CELL_TEXT:
        values[first] = _level_marked(values[first], cell.value, _xls_indent(book, cell))  # indentation of the first text cell of the row
    overflow = next((value for value in map(text, range(offset + max_cols, length)) if value), None)
    return (*values, overflow) if overflow is not None else tuple(values)


def _xls_indent(book: xlrd.book.Book, cell: xlrd.sheet.Cell) -> int:
    try:
        return book.xf_list[cell.xf_index].alignment.indent_level
    except (AttributeError, IndexError, TypeError):
        return 0


def _fill_xls_merges(sheet: xlrd.sheet.Sheet, rows: list[list[str]], warnings: list[str], width: int,
                     offset: int = 0) -> _Across:
    """Repeat XLS vertical merge values and collect the cells spanned by horizontal merges (`_repeat_merge`). The same range is
    handled only once, and when the total of cells covered by merges exceeds the same limit as XLSX (`_MAX_MERGED_CELLS`) nothing
    is repeated (the value stays in the first cell only), with a warning. The same range 20,000 times was rescanned for every row
    and took 17s (Pinned by: test_spreadsheet.py::TestXlsResourceBounds)."""
    across: _Across = {}
    merges = sorted(set(sheet.merged_cells))
    if sum((bottom - top) * (right - left) for top, bottom, left, right in merges) > _MAX_MERGED_CELLS:
        warnings.append(f"Sheet '{sheet.name}': there are too many merged cells, so vertical merge values were left in the first cell only.")
        return across
    for top, bottom, left, right in merges:
        if left >= offset:  # a merge starting in a dropped leading empty column has an empty first cell, so there is no value to fill
            _repeat_merge(rows, top, bottom, left - offset, min(right - offset, width), across)
    return across


_BIFF_FORMULA = 0x0006
_BIFF_FORMULA_EMPTY_RESULT = (3, b"\xff\xff")  # first byte of the 8-byte result (kind 3 = empty text) and its last two bytes (not a number)
# Build number and year that Apache POI (HSSF) writes in the workbook BOF. Excel (0x3266, 1997, etc.) and LibreOffice (0x0DBB) write
# other values.
_BIFF_POI_BUILD = (0x10D3, 0x07CC)


def _xls_formulas_without_results(stream: bytes) -> tuple[int, dict[int | None, set[tuple[int, int]]]]:
    """(number of formula cells in a workbook where not a single result is stored, formula cells whose 0 in the result slot cannot
    be trusted: worksheet index → (row, column)). 0 and an empty dict when neither applies.

    A library (xlwt) writes 'empty text' in the result slot of every formula. xlrd returns such a cell as empty text, so totals
    cells became blank without a warning. Excel writes a formula computed to empty text (`=IF(A1="","",…)`) in exactly the same
    way, so a single cell cannot tell them apart. As with XLSX, if there is even one result the file is taken as saved by Excel.
    xlrd does not provide formula text, so the formulas cannot be carried over; there is only a warning
    (Pinned by: test_spreadsheet.py::TestXlsFormulasWithoutResults).
    Apache POI (HSSF) does not calculate and writes the number 0 in the result slot, so totals and ratios all came out as 0 (Excel
    recalculates on open). Those cells are emptied only when the workbook BOF is POI's and every formula result is the number 0 —
    the 0 in an empty form saved by Excel or LibreOffice is a computed value. Known accepted gap: a file made by opening an Excel
    template with POI and filling in values only keeps the template's BOF, so its 0s are read as is
    (Pinned by: test_spreadsheet.py::TestXlsFormulasWithPlaceholderZeroResults).
    If the scan fails it moves on without a warning — cell contents are read by xlrd.
    """
    empty = 0
    zeros: dict[int | None, set[tuple[int, int]]] = {}
    sheet_offsets: dict[int, int] = {}  # worksheet BOF position → sheet index (the same mapping as `_scan_xls_drawings`)
    written_by_poi = False
    sheet = None
    kind, marker = _BIFF_FORMULA_EMPTY_RESULT
    for position, record_id, body in _biff_records(stream):
        if record_id == _BIFF_BOF and len(body) >= 8:
            if position == 0:
                written_by_poi = struct.unpack_from("<HH", body, 4) == _BIFF_POI_BUILD
            sheet = sheet_offsets.get(position, sheet)
        elif record_id == _BIFF_BOUNDSHEET and len(body) >= 6 and body[5] == 0:
            sheet_offsets[struct.unpack_from("<I", body)[0]] = len(sheet_offsets)
        elif record_id == _BIFF_FORMULA and len(body) >= 14:
            if body[6] == kind and body[12:14] == marker:
                empty += 1
            elif body[12:14] != b"\xff\xff" and struct.unpack_from("<d", body, 6)[0] == 0 and written_by_poi:
                zeros.setdefault(sheet, set()).add(struct.unpack_from("<HH", body))
            else:
                return 0, {}
    if empty and zeros:
        return 0, {}
    return empty, zeros


# ---- XLS drawing objects (text box / shape text, pictures) --------------------------------------------------------

_BIFF_BOF, _BIFF_EOF, _BIFF_BOUNDSHEET, _BIFF_CONTINUE, _BIFF_CODEPAGE = 0x0809, 0x000A, 0x0085, 0x003C, 0x0042
_BIFF_OBJECT, _BIFF_TEXT_OBJECT, _BIFF_DRAWING, _BIFF_DRAWING_GROUP = 0x005D, 0x01B6, 0x00EC, 0x00EB
_BIFF8 = 0x0600
_OBJECT_PICTURE, _OBJECT_NOTE = 0x08, 0x19
_OBJECT_CHOICES = frozenset((0x0B, 0x0C))  # form control check boxes and option buttons
# Among the OBJ sub-records, the check box state (first 2 bytes: 0 unchecked, 1 checked, 2 mixed). Excel writes it in FtCblsData
# (0x12), LibreOffice in FtCbls (0x0A).
_OBJECT_CHECK_STATES = (0x12, 0x0A)
_ESCHER_STORE_ENTRY, _ESCHER_OPTIONS = 0xF007, 0xF00B
_ESCHER_PICTURE_INDEX = 0x0104  # shape property pib: index into the global picture store (1-based)
_ESCHER_METAFILES = frozenset((0xF01A, 0xF01B, 0xF01C))  # EMF, WMF, PICT
_ESCHER_BITMAPS = frozenset((0xF01D, 0xF01E, 0xF01F, 0xF029, 0xF02A))  # JPEG, PNG, DIB, TIFF, JPEG (CMYK)
_ESCHER_DIB = 0xF01F


def _xls_drawings(stream: bytes, options: ConvertOptions) -> list[list[str | bytes | None]] | None:
    """For each worksheet (BOUNDSHEET order = xlrd's sheet order), collect text box / shape text (str) and pictures (bytes; None for
    a picture that was not found) in the order they sit in the workbook stream. None when it cannot be parsed — the caller only
    leaves a warning and cell contents are converted as usual.

    xlrd does not hand out drawing objects (it reads the text records and discards them unless they are cell comments), so
    annotations in a text box beside a table (unit, basis) and pictures were dropped without a warning. The records of the
    workbook stream are scanned directly (Pinned by: test_spreadsheet.py::TestXlsDrawings).
    """
    try:
        return _scan_xls_drawings(stream, options.max_uncompressed_bytes)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — this content is read as an extra. Whatever broke, the whole document is not rejected
        return None


def _xls_chart_sheets(stream: bytes, include_hidden: bool) -> list[str]:
    """Names of the chart sheets (type 2) among the BOUNDSHEET records of the workbook globals section. xlrd does not count chart
    sheets as sheets, so a workbook where only chart sheets are visible gave an empty result with no reason
    (Pinned by: test_spreadsheet.py::TestChartSheets::test_xls_chart_sheet_is_reported)."""
    names = []
    for _, record_id, body in _biff_records(stream):
        if record_id == _BIFF_EOF:
            break
        if record_id != _BIFF_BOUNDSHEET or len(body) < 8 or body[5] != 2 or (body[4] & 3 and not include_hidden):
            continue
        length, wide = body[6], body[7] & 1
        names.append(body[8:8 + length * 2].decode("utf-16-le", errors="replace") if wide
                     else body[8:8 + length].decode("latin-1"))
    return names


_BIFF_CHART_SUBSTREAM = 0x0020


def _xls_sheets_holding_charts(stream: bytes) -> set[int]:
    """Indexes of the ordinary sheets holding a chart placed on the sheet (BOF type 0x20 inside the sheet substream) (xlrd's sheet
    index — only ordinary sheets are counted). LibreOffice saves a chart sheet as an ordinary sheet + a chart placed on it, so it
    was dropped as an empty sheet without a warning. `extract_xls` warns, as for a chart sheet, only for sheets without cells
    (Pinned by: test_office_round43.py::TestXlsChartSheetSavedAsAWorksheet)."""
    starts = []
    for _, record_id, body in _biff_records(stream):
        if record_id == _BIFF_EOF:
            break
        if record_id == _BIFF_BOUNDSHEET and len(body) >= 6 and body[5] == 0:
            starts.append(struct.unpack_from("<I", body)[0])

    charted = _chart_holders(stream, set(starts))
    return {index for index, start in enumerate(starts) if start in charted}


def _chart_holders(stream: bytes, starts: set[int]) -> set[int]:
    """Those of the sheet positions `starts` whose sheet holds a chart substream: the first BOF at or after the position (a
    position right at an EOF has no sheet) has a chart BOF inside it, before its own EOF.

    The stream is read once for all sheets. Walking it again from every sheet position, to the end when no EOF follows, took
    64 s for 2,000 sheet entries over 200,000 empty records (808 KB). Deliberate: only positions at a record boundary of the
    stream are looked at — a sheet entry pointing into the middle of a record gets no chart warning
    (Pinned by: test_audit_spreadsheet.py::TestXlsChartScan::test_stream_is_walked_a_fixed_number_of_times).
    """
    charted: set[int] = set()
    waiting: list[int] = []  # positions passed since the last BOF or EOF
    open_sheets: list[list[int]] = []  # for every BOF still open, the positions whose sheet it starts
    with_chart = 0  # the first this many open BOFs have a chart BOF inside them

    for position, record_id, body in _biff_records(stream):
        if position in starts:
            waiting.append(position)
        if record_id == _BIFF_BOF:
            if body[2:4] == _BIFF_CHART_SUBSTREAM.to_bytes(2, "little"):
                with_chart = len(open_sheets)
            open_sheets.append(waiting)
            waiting = []
        elif record_id == _BIFF_EOF:
            waiting = []
            if open_sheets:
                closed = open_sheets.pop()
                if with_chart > len(open_sheets):
                    charted.update(closed)
                    with_chart = len(open_sheets)
    for closed in open_sheets[:with_chart]:
        charted.update(closed)
    return charted


def _xls_workbook_stream(data: bytes) -> bytes:
    """Extract the workbook stream (BIFF8 `Workbook`, BIFF5 and below `Book`) from the OLE container. If it cannot be extracted the
    document is rejected as corrupted.

    Stream names are case-insensitive (`ole.exists` is — the same rule as detection `detect._detect_ole`,
    Pinned by: test_detect.py::TestXlsStreamNameCase). When both exist (Excel 97 dual format) `Workbook` comes first.
    """
    if not data.startswith(olefile.MAGIC):
        return data  # a BIFF stream not wrapped in OLE. xlrd reads it as is, too

    try:
        with olefile.OleFileIO(BytesIO(data)) as ole:
            for name in ("Workbook", "Book"):
                if ole.exists(name) and (stream := ole.openstream(name).read()):
                    return stream  # xlrd rejects an empty stream as 'no file name', so it is sent on below
    except (OSError, ValueError, struct.error) as exc:  # olefile: OSError (OleFileError) for structural damage, ValueError for bad header values
        raise CorruptedDocumentError("The XLS document structure could not be read.") from exc
    raise CorruptedDocumentError("The XLS document structure could not be read.")


def _scan_xls_drawings(stream: bytes, max_inflated_bytes: int) -> list[list[str | bytes | None]]:
    """Scan BIFF8 records in order. One shape is written as MSODRAWING (shape properties) → OBJ (object type) → [MSODRAWING → TXO →
    CONTINUE (text) → CONTINUE (character formatting)].

    - Text: read from the CONTINUE records that follow, for as many characters as the TXO says. In each record a first byte of 1
      means UTF-16LE, 0 means single-byte characters.
    - Cell comments (object type 0x19) use TXO too. They are not added as text boxes — comments are placed at the end of the sheet
      with their cell, from xlrd's NOTE (`cell_note_map`).
    - Pictures: looked up in the global picture store (MSODRAWINGGROUP) by the picture index (pib) of the shape properties. The
      same picture is extracted only once.
    - Excel writes the properties of the shape following a shape with text in a CONTINUE, not an MSODRAWING. A CONTINUE after OBJ
      or TXO is read as shape properties (15 pictures of a real Excel file had this shape).
    - Chart sheets and macro sheets are not counted as sheets by xlrd, so they are not counted here either. The inside of a chart
      placed on a sheet (the span where BOFs nest) is skipped. Old non-BIFF8 files (Excel 95 and below) have a different structure
      and are not read.
    """
    sheets: list[list[str | bytes | None]] = []
    sheet_offsets: dict[int, int] = {}  # worksheet BOF position → sheet index
    store = _XlsPictureStore(max_inflated_bytes)
    current: list[str | bytes | None] | None = None
    depth = previous = text_left = runs_left = 0
    drawing, object_type, pieces, mark = bytearray(), None, [], ""

    for position, record_id, body in _biff_records(stream):
        if record_id == _BIFF_BOF:
            if position == 0 and struct.unpack_from("<H", body)[0] != _BIFF8:
                return []
            depth += 1
            if depth == 1:
                current = sheets[sheet_offsets[position]] if position in sheet_offsets else None
        elif record_id == _BIFF_EOF:
            depth = max(depth - 1, 0)
        elif record_id == _BIFF_BOUNDSHEET:
            offset, _, kind = struct.unpack_from("<IBB", body)
            if kind == 0:
                sheet_offsets[offset] = len(sheets)
                sheets.append([])
        elif record_id == _BIFF_DRAWING_GROUP or (record_id == _BIFF_CONTINUE and previous == _BIFF_DRAWING_GROUP):
            store.data += body
            record_id = _BIFF_DRAWING_GROUP  # the CONTINUE records that follow are picture store too
        elif current is None or depth != 1:
            pass
        elif record_id == _BIFF_DRAWING:
            drawing = bytearray(body)
        elif record_id == _BIFF_OBJECT:
            object_type = struct.unpack_from("<H", body, 4)[0]
            if object_type == _OBJECT_PICTURE:
                current.append(store.picture(_escher_picture_index(drawing)))
            mark = _xls_choice_mark(body) if object_type in _OBJECT_CHOICES else ""
            drawing = bytearray()
        elif record_id == _BIFF_TEXT_OBJECT:
            text_left, runs_left = struct.unpack_from("<HH", body, 10)
            pieces = []
        elif record_id == _BIFF_CONTINUE and text_left > 0:
            piece = body[1:].decode("utf-16-le" if body[0] & 1 else "latin-1", errors="replace")
            pieces.append(piece)
            text_left -= len(piece)
            if text_left <= 0:
                text = "".join(pieces).replace("\r\n", "\n").replace("\r", "\n").strip()
                if text and object_type != _OBJECT_NOTE:
                    current.append(f"{mark} {text}" if mark else text)
                object_type, mark = None, ""
            record_id = _BIFF_TEXT_OBJECT
        elif record_id == _BIFF_CONTINUE and runs_left > 0 and previous == _BIFF_TEXT_OBJECT:
            runs_left -= len(body)  # character-formatting runs. Not read
            record_id = _BIFF_TEXT_OBJECT
        elif record_id == _BIFF_CONTINUE and previous in (_BIFF_OBJECT, _BIFF_TEXT_OBJECT, _BIFF_DRAWING):
            # Drawing fragments are appended in place for each CONTINUE — creating new bytes each time made a 16MB chain take 2.2s
            # and 1.1GB
            # (Pinned by: test_spreadsheet_damaged_parts.py::TestLongDrawingContinuation)
            if previous == _BIFF_DRAWING:
                drawing += body
            else:
                drawing = bytearray(body)
            record_id = _BIFF_DRAWING
        previous = record_id
    return sheets


def _xls_choice_mark(body: bytes) -> str:
    """Checked state of a check box / option button OBJ → ☒ or ☐ (the same characters as XLSX `_form_controls`, DOCX and HWPX). The
    label is the text of the TXO that follows.

    xlrd does not hand out drawing objects and this scanner read only the label, so the chosen side was unknown
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestFormControlCheckBoxes::test_xls_check_boxes_keep_their_states).
    Sub-records follow one another as (type 2 bytes, length 2 bytes, content) and end at type 0 (ftEnd).
    """
    states: dict[int, int] = {}
    position = 0
    while position + 4 <= len(body):
        kind, length = struct.unpack_from("<HH", body, position)
        if kind == 0:
            break
        if kind in _OBJECT_CHECK_STATES and length >= 2 and position + 6 <= len(body):
            states.setdefault(kind, struct.unpack_from("<H", body, position + 4)[0])
        position += 4 + length
    state = next((states[kind] for kind in _OBJECT_CHECK_STATES if kind in states), 0)
    return _CHECKED if state == 1 else _UNCHECKED


@dataclass(slots=True)
class _XlsPictureStore:
    """The workbook-global picture store (the MSODRAWINGGROUP records joined). A picture is extracted only once, when first looked up.

    `budget` is the remaining bytes that compressed metafiles may inflate to (a per-document total — `max_uncompressed_bytes`).
    Measured per picture, a small file would use the limit once per picture.
    """

    budget: int
    data: bytearray = field(default_factory=bytearray)
    _entries: list[tuple[int, int, int, int] | None] | None = None
    _pictures: dict[int | None, bytes | None] = field(default_factory=dict)

    def picture(self, index: int | None) -> bytes | None:
        """Picture index (1-based) → picture file bytes. None when there is no index or it is not in the store."""
        if index not in self._pictures:
            if self._entries is None:
                self._entries = _escher_store_entries(self.data)
            entry = self._entries[index - 1] if index and index <= len(self._entries) else None
            picture = _escher_picture(self.data, entry, self.budget) if entry else None
            if picture and entry[0] in _ESCHER_METAFILES:
                self.budget -= len(picture)
            self._pictures[index] = picture
        return self._pictures[index]


def _biff_records(stream: bytes, start: int = 0) -> Iterator[tuple[int, int, bytes]]:
    """(record position, type, content). Reads from position `start`. A last record whose length runs past the end of the stream is
    given only as far as it exists."""
    position = start
    while position + 4 <= len(stream):
        record_id, length = struct.unpack_from("<HH", stream, position)
        yield position, record_id, stream[position + 4:position + 4 + length]
        position += 4 + length


def _escher_atoms(data: bytes | bytearray) -> Iterator[tuple[int, int, int, int]]:
    """The non-container drawing (Escher) records as (type, instance, content start, length). Container records (version 0xF) are
    entered.

    A sheet's MSODRAWING fragment holds only the front part of a container (the rest is in later records), so container lengths are
    not trusted and the records are walked in order.
    """
    position = 0
    while position + 8 <= len(data):
        version_instance, record_type, length = struct.unpack_from("<HHI", data, position)
        position += 8
        if version_instance & 0xF != 0xF:
            yield record_type, version_instance >> 4, position, length
            position += length


def _escher_picture_index(drawing: bytes | bytearray) -> int | None:
    """The picture index (pib) from one shape's MSODRAWING fragment."""
    for record_type, count, start, length in _escher_atoms(drawing):
        if record_type != _ESCHER_OPTIONS:
            continue
        for offset in range(start, min(start + 6 * count, start + length, len(drawing) - 5), 6):
            property_id, value = struct.unpack_from("<HI", drawing, offset)
            if property_id & 0x3FFF == _ESCHER_PICTURE_INDEX:
                return value
    return None


def _escher_store_entries(store: bytearray) -> list[tuple[int, int, int, int] | None]:
    """For each entry (BSE) of the global picture store, the picture record inside it (type, instance, content start, length). None
    for an entry without a picture."""
    entries: list[tuple[int, int, int, int] | None] = []
    for record_type, _, start, length in _escher_atoms(store):
        if record_type != _ESCHER_STORE_ENTRY:
            continue
        end = min(start + length, len(store))
        picture_at = start + 36 + (store[start + 33] if start + 33 < end else 0)  # 36-byte header + name
        if picture_at + 8 > end:
            entries.append(None)
            continue
        version_instance, picture_type, picture_length = struct.unpack_from("<HHI", store, picture_at)
        entries.append((picture_type, version_instance >> 4, picture_at + 8, min(picture_length, end - picture_at - 8)))
    return entries


def _escher_picture(store: bytearray, entry: tuple[int, int, int, int], max_bytes: int) -> bytes | None:
    """Picture record → picture file bytes. The leading header (1–2 identifiers + 1 marker byte; a 34-byte header for metafiles) is
    stripped.

    Metafiles (EMF, WMF) may be deflate-compressed. The inflated size is bounded by `max_bytes` (the document's remaining
    decompression limit). A DIB is a bitmap without a file header, so a BMP header is prepended. None when it cannot be read.
    """
    picture_type, instance, start, length = entry
    end = start + length
    start += 16 * (1 + (instance & 1))  # an odd instance value means two identifiers
    if picture_type in _ESCHER_METAFILES:
        if start + 34 > end:
            return None
        payload = bytes(store[start + 34:end])
        if store[start + 32] != 0:  # 0 = deflate, 0xFE = no compression
            return payload or None
        try:
            payload = zlib.decompressobj().decompress(payload, max_bytes + 1)
        except zlib.error:
            return None
        return payload if payload and len(payload) <= max_bytes else None
    if picture_type not in _ESCHER_BITMAPS or start + 1 >= end:
        return None

    payload = bytes(store[start + 1:end])
    if picture_type != _ESCHER_DIB:
        return payload
    if len(payload) < 40:
        return None
    (header_size,), (bit_count, compression), (colors_used,) = (
        struct.unpack_from("<I", payload), struct.unpack_from("<HI", payload, 14), struct.unpack_from("<I", payload, 32))
    palette = 4 * (colors_used or (1 << bit_count if bit_count <= 8 else 0))
    masks = 12 if compression == 3 and header_size == 40 else 0  # color masks of BI_BITFIELDS
    return b"BM" + struct.pack("<IHHI", 14 + len(payload), 0, 0, 14 + header_size + palette + masks) + payload


def _xls_drawing_blocks(items: list[str | bytes | None], sheet_name: str, extraction: Extraction,
                        options: ConvertOptions) -> list[str]:
    """Text box / shape text and pictures of one sheet → blocks to place after the table (same place as `_drawing_blocks` for XLSX).
    Text counts toward the character limit."""
    blocks: list[str] = []
    missing = 0
    for item in items:
        if isinstance(item, str):
            blocks.append(item)
            if extraction.count(solid_length(item), options):
                break
        elif options.extract_images and item:
            blocks.append(extraction.add_image(item))
        elif options.extract_images:
            missing += 1

    if missing:
        extraction.warnings.append(f"Sheet '{sheet_name}': {missing:,} picture(s) could not be read.")
    return blocks


def _xls_rich_text(text: str, runs: list[tuple[int, int]], escapement: Callable[[int], int | None]) -> str:
    """Rich text of an XLS cell. `runs` are (character position, font index) pairs; that font applies from that position to the
    next one (before the first position it is the cell's font). A font escapement of 1 is superscript and 2 is subscript
    (Pinned by: test_spreadsheet.py::TestSuperscriptInCells)."""
    runs = sorted(runs)
    pieces = [text[:runs[0][0]]] if runs else [text]
    for (start, font), (end, _) in zip(runs, [*runs[1:], (len(text), 0)], strict=True):
        mark = {1: SUPERSCRIPT, 2: SUBSCRIPT}.get(escapement(font))
        pieces.append(mark_script(text[start:end], mark) if mark else text[start:end])
    return resolve_scripts("".join(pieces))


def _xls_escapement(book: xlrd.book.Book, font_index: int) -> int | None:
    try:
        return book.font_list[font_index].escapement
    except (AttributeError, IndexError, TypeError):
        return None


def _xls_number_format(book: xlrd.book.Book, cell: xlrd.sheet.Cell) -> str | None:
    try:
        return _format_within_length(book.format_map[book.xf_list[cell.xf_index].format_key].format_str)
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _xls_cell_text(cell: xlrd.sheet.Cell, book: xlrd.book.Book) -> str:
    number_format = _xls_number_format(book, cell)
    return format_cell(_xls_value(cell, book.datemode, number_format), number_format)


def _xls_value(cell: xlrd.sheet.Cell, datemode: int, number_format: str | None) -> object:
    """XLS cell value. Among date cells, an elapsed-time format (`[h]:mm`) becomes a duration and a value below 1 a time only (same
    rule as the XML spreadsheet); error cells become the error text (#REF! and so on)
    (Pinned by: test_spreadsheet.py::TestXlsValues). A number cell with a weekday-only format is read as a date as well. So is a
    format consisting only of an elapsed section (`[ss]`, `[mm]`, `[h]`) — xlrd decides after removing bracket sections and hands
    it over as a number cell, so the serial number was emitted (XLSX gives the cumulative value; Pinned by:
    test_spreadsheet.py::TestXlsValues::test_number_cell_with_only_an_elapsed_section_is_a_duration)."""
    if cell.ctype == xlrd.XL_CELL_DATE and cell.value == 0 and number_format and _shows_date(number_format):
        # 0 under a date format is '1900-01-00' in Excel. Returned as a number, not the time '00:00:00'
        # (`_is_serial_zero_shown_as_date`)
        return cell.value
    elapsed = bool(number_format and _ELAPSED_FORMAT.search(number_format))
    if cell.ctype == xlrd.XL_CELL_DATE or cell.ctype == xlrd.XL_CELL_NUMBER and (
            elapsed and is_date_format(number_format) or _is_weekday_only(number_format)):
        try:
            # A value that does not fit in a duration (a billion days or more) raises OverflowError. The whole document was rejected
            # as 'corrupted'. The original value is shown
            # (Pinned by: test_spreadsheet.py::TestXlsValues::test_elapsed_time_value_too_large_for_a_duration_is_shown_as_the_number).
            if elapsed:
                return timedelta(days=cell.value)
            if cell.value < 0:
                return cell.value  # a negative serial number is not a date. Its displayed shape: `format_cell` (`_negative_serial_text`)
            value = xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except (xlrd.xldate.XLDateError, OverflowError, ValueError):
            return cell.value
        return value.time() if 0 <= cell.value < 1 else value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERR!")
    if cell.ctype in (xlrd.XL_CELL_BLANK, xlrd.XL_CELL_EMPTY):
        return None
    return cell.value
