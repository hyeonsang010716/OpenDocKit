"""The streams of an OLE (Compound File Binary) container, read by HWP, XLS and the `.doc` / `.ppt` encryption check.

olefile reads a whole stream as soon as it is opened, as many sectors as the directory entry declares, following the FAT chain
and only noting a defect when the chain loops (`nb_sectors > len(fat)` is not fatal). A 26.5 KB HWP whose Section0 chain
pointed at itself with a declared size of 2 GB made 5.4 GB in memory and converted 'successfully'; a 5.5 KB `.doc` made 2.5 GB
in the encryption check. A stream cannot hold more than the file, so every declared size — of the streams, of the mini stream
(the size of the root entry) and of the mini FAT (a header field) — is cut to the size of the file before anything is read
(Pinned by: test_hwp_robustness.py::TestStreamsLargerThanTheFile, test_legacy.py::TestEncryption::
test_stream_that_loops_on_itself_is_read_no_further_than_the_file, test_spreadsheet.py::TestXlsOleContainer::
test_workbook_stream_that_loops_on_itself_is_read_no_further_than_the_file). The streams of a real file are within it, so its
reading does not change.

olefile also finds a stream by walking the children of each storage with `.lower()` on every name, so opening n streams of one
storage took the square of n (8,000 sections of an HWP: 3.4 s, 16,000: 27 s). The names are looked up once here, case-insensitive
as olefile does: the first of two children with the same name in olefile's (sorted) order wins, as with olefile's own search
(Pinned by: test_hwp_robustness.py::TestManyDirectoryEntries). Opening the container itself was quadratic too: olefile checks
that no two streams start at the same sector by looking each start up in a list, a third of the time of those 8,000 sections,
in detection and again in the extractor. `OleFile` keeps the starts in a set as well.
"""

from __future__ import annotations

import olefile


_NOT_A_STREAM_START = frozenset((olefile.DIFSECT, olefile.FATSECT, olefile.ENDOFCHAIN, olefile.FREESECT))


class OleFile(olefile.OleFileIO):
    """`olefile.OleFileIO` whose check for two streams that start at the same sector takes constant time per stream. The check and
    its outcome (a defect olefile only notes) are olefile's: the starts still go into its lists, which `open` makes anew."""

    def _check_duplicate_stream(self, first_sect: int, minifat: bool = False) -> None:
        if not minifat and first_sect in _NOT_A_STREAM_START:
            return
        used = self._used_streams_minifat if minifat else self._used_streams_fat
        seen = self.__dict__.setdefault("_opendockit_starts", {})
        starts = seen.get(minifat)
        if starts is None or not used:  # the first stream since `open` made the list
            starts = seen[minifat] = set()
        if first_sect in starts:
            self._raise_defect(olefile.DEFECT_INCORRECT, "Stream referenced twice")
        else:
            starts.add(first_sect)
            used.append(first_sect)


class OleStreams:
    """The streams of an open `olefile.OleFileIO` of `file_size` bytes, by name (`a/b`, case-insensitive)."""

    def __init__(self, ole: olefile.OleFileIO, file_size: int) -> None:
        self._ole = ole
        cutoff = ole.minisectorcutoff
        # A size of the mini stream area or larger stays one: cut below it, a regular stream would be read from the mini stream
        largest = max(file_size, cutoff)
        ole.root.size = min(ole.root.size, file_size)
        ole.num_mini_fat_sectors = min(ole.num_mini_fat_sectors, file_size // ole.sector_size + 1)
        self._entries: dict[str, olefile.olefile.OleDirectoryEntry] = {}
        pending = [("", ole.root)]
        while pending:
            prefix, node = pending.pop()
            for kid in node.kids:
                path = prefix + kid.name.lower()
                if path in self._entries:
                    continue
                if kid.size >= cutoff:
                    kid.size = min(kid.size, largest)
                self._entries[path] = kid
                if kid.kids:
                    pending.append((path + "/", kid))

    def _entry(self, name: str) -> olefile.olefile.OleDirectoryEntry | None:
        return self._entries.get(name.lower())

    def exists(self, name: str) -> bool:
        """Whether a stream or storage of that name is in the container (as `OleFileIO.exists`)."""
        return self._entry(name) is not None

    def size(self, name: str) -> int:
        """The size of the stream, cut to the size of the file. OSError when there is no such stream."""
        entry = self._entry(name)
        if entry is None or entry.entry_type != olefile.STGTY_STREAM:
            raise OSError("no such stream")
        return entry.size

    def read(self, name: str) -> bytes:
        """The bytes of the stream, at most the size of the file. OSError when there is no such stream (as `OleFileIO.openstream`)."""
        size = self.size(name)
        entry = self._entry(name)
        return self._ole._open(entry.isectStart, size).read()  # `openstream` without its name search
