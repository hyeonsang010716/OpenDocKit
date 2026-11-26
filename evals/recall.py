"""Is every word a document holds in the conversion? Compare OpenDocKit's output with the document's own XML.

    uv run python -m evals.recall --documents <dir> [--baseline <file>] [--update-baseline] [--show <n>]

The other scorecards need a reference somebody wrote. This one needs none: a DOCX, PPTX, XLSX or HWPX states its text in
its XML (`w:t`, `a:t`, the strings of the cells, `hp:t`) and an HWP 5 file in the text records of its paragraphs, so a
word of that text that is not in the Markdown was lost on the way, and two words the Markdown writes as one were joined by
something that was dropped between them. Each loss is listed with the chain of elements (for HWP 5, of controls) the word
sits in, so the losses of a folder group by their cause.

The source is read here on its own, not with the reader that is measured: every text element of the XML, every text
record of the stream, whatever it hangs under. A paragraph inside a control the reader does not know is still counted.

What it counts, per document:

- `words`: the words of the source text;
- `missing`: those that are nowhere in the output, not even with a mark inside them (`CO<sub>2</sub>`, a footnote mark);
- `joined`: pairs of neighbouring words of one paragraph that the output writes as one word while the source never does —
  the dot of `미국·영국` kept in a private-use character and removed with it leaves `미국영국`, and no word is missing;
- `private_use`: the private-use characters of the source text. They are symbols of a font (a bullet, a dot, a check box),
  no words, so neither count above sees one that stands alone and is dropped: the number says where to look.

Not every missing word is a defect: the prompt of an empty content control, the page number of a footer and the
letters of an equation written as LaTeX are left out or rewritten on purpose (`evals/README.md`). So a run is compared
with a baseline, and ends with status 1 when a document misses or joins more than it did.
"""

import argparse
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import json
from pathlib import Path
import re
import struct
import sys
import unicodedata
import zipfile
import zlib

from lxml import etree
import olefile

from opendockit import ConversionError, ConvertOptions, convert


# Everything a document can hold is asked for, and nothing is written around its words. The limits stay the default ones:
# with the character limit and the column limit taken away, one workbook of a public test collection took 17 GB before it
# was stopped. A document cut at the character limit is not compared (`_recall`), and a cell beyond the column limit counts
# as missing, which it is.
OPTIONS = ConvertOptions(include_headers_footers=True, include_hidden_sheets=True, include_speaker_notes=True,
                         include_footnotes=True, keep_emphasis=False, keep_links=False, extract_images=False,
                         remove_repeated_page_lines=False, page_markers=False)
WORKERS = 4
PART_BYTES = 40 * 1024 * 1024  # a larger part is not read as a reference
_WORD = re.compile(r"[^\W_]+")
# Marks the output may write inside a word of the source
_INNER_MARKS = re.compile(r"</?(?:sub|sup|br|del|u)\s*/?>|\[\^[^\]]*\]")
_NOT_WORD = re.compile(r"[\W_]+")
# Between two words, these alone are no reason for a space: a soft hyphen, a zero-width space, joiners, a word joiner
_INVISIBLE = frozenset(map(chr, (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF)))
_JOINED_WORD_LETTERS = 2  # a pair is looked at when each of its words has this many letters
_PRIVATE_USE = re.compile(f"[{chr(0xE000)}-{chr(0xF8FF)}{chr(0xF0000)}-{chr(0xFFFFD)}{chr(0x100000)}-{chr(0x10FFFD)}]")

Unit = tuple[str, list[tuple[str, str]]]  # (part, the pieces of one paragraph: (text, chain of elements))


def _local(node: etree._Element) -> str:
    return node.tag.rsplit("}", 1)[-1] if isinstance(node.tag, str) else ""


def _chain(node: etree._Element) -> str:
    return "/".join(reversed([_local(node), *(_local(above) for above in node.iterancestors())]))


def _parts(archive: zipfile.ZipFile, pattern: str):
    parser = etree.XMLParser(recover=True, resolve_entities=False, huge_tree=False)
    for info in archive.infolist():
        if re.fullmatch(pattern, info.filename) and info.file_size <= PART_BYTES:
            root = etree.fromstring(archive.read(info), parser)
            if root is not None:
                yield info.filename, root


def _node_text(node: etree._Element, breaks: frozenset[str]) -> str:
    """The text of a text element; a line break or a tab inside it is a space."""
    pieces = [node.text or ""]
    for child in node:
        pieces.append(" " if _local(child) in breaks else "")
        pieces.append(child.tail or "")
    return "".join(pieces)


def _paragraphs(root: etree._Element, part: str, *, text: str, breaks: frozenset[str], skip: frozenset[str] = frozenset()):
    """One unit per paragraph: its text elements in order, a break between them as a space. A paragraph inside another (a
    text box, a table in a cell) is a unit of its own."""
    for paragraph in root.iter("{*}p"):
        pieces: list[tuple[str, str]] = []
        for node in paragraph.iter():
            name = _local(node)
            if name != text and name not in breaks:
                continue
            above = [_local(element) for element in node.iterancestors()]
            if skip.intersection(above):
                continue
            if next((element for element in node.iterancestors() if _local(element) == "p"), None) is not paragraph:
                continue
            if name == text and text not in above:
                pieces.append((_node_text(node, breaks), _chain(node)))
            elif name in breaks and text not in above:
                pieces.append((" ", ""))
        if any(piece.strip() for piece, _ in pieces):
            yield part, pieces


_WORD_BREAKS = frozenset({"tab", "br", "cr", "ptab", "noBreakHyphen"})
# The copy of a drawing kept for older programs repeats the text of the drawing
_FALLBACK = frozenset({"Fallback"})


def _docx(archive: zipfile.ZipFile):
    for part, root in _parts(archive, r"word/(?:document|footnotes|endnotes|comments|header\d*|footer\d*)\.xml"):
        yield from _paragraphs(root, part, text="t", breaks=_WORD_BREAKS, skip=_FALLBACK)


def _pptx(archive: zipfile.ZipFile):
    for part, root in _parts(archive, r"ppt/(?:slides|notesSlides)/[^/]+\.xml|ppt/diagrams/data[^/]*\.xml"):
        yield from _paragraphs(root, part, text="t", breaks=frozenset({"br", "tab"}), skip=_FALLBACK)


def _hwpx(archive: zipfile.ZipFile):
    # A master page is drawn on every page of its section: text of the document as much as a header is
    for part, root in _parts(archive, r"Contents/(?:section|masterpage)\d+\.xml"):
        yield from _paragraphs(root, part, text="t", breaks=frozenset({"lineBreak", "tab", "nbSpace", "fwSpace", "hwSpace"}))


def _string(item: etree._Element) -> str:
    """The text of a shared or inline string, without its phonetic guide."""
    return "".join((node.text or "") for node in item.iter("{*}t")
                   if not any(_local(above) == "rPh" for above in node.iterancestors()))


def _xlsx(archive: zipfile.ZipFile):
    strings = [_string(item) for _, root in _parts(archive, r"xl/sharedStrings\.xml") for item in root.iter("{*}si")]
    for part, root in _parts(archive, r"xl/worksheets/[^/]+\.xml"):
        for cell in root.iter("{*}c"):
            kind, value = cell.get("t"), cell.find("{*}v")
            if kind == "s" and value is not None and (value.text or "").strip().isdigit():
                index = int(value.text)
                text = strings[index] if index < len(strings) else ""
            elif kind == "inlineStr":
                text = _string(cell)
            elif kind == "str" and value is not None:
                text = value.text or ""
            else:
                continue
            if text.strip():
                yield part, [(text, f"c[t={kind}]")]


# HWP 5 (the record layout of Hancom's published format description, revision 1.3): a section stream is a run of records,
# each with a tag, a level and a size; the text of a paragraph is a PARA_TEXT record of UTF-16 units, in which a unit
# below 32 is a control.
_HWP_PARA_HEADER, _HWP_PARA_TEXT, _HWP_CTRL_HEADER = 66, 67, 71
_HWP_ONE_UNIT = frozenset({0, 10, 13, *range(24, 32)})  # a control of one unit; every other one takes eight
# A tab, a line break, the end of the paragraph, a hyphen, a non-breaking and a fixed-width space part two words. A control
# that stands for an object or opens and closes a field does not: `가[field]나` is written `가나` by Hangul too.
_HWP_SPACES = frozenset({9, 10, 13, 24, 30, 31})
_HWP_PASSWORD, _HWP_DISTRIBUTION = 0x2, 0x4


def _hwp_records(data: bytes):
    """(tag, level, payload) of each record of a section stream."""
    position = 0
    while position + 4 <= len(data):
        header, = struct.unpack_from("<I", data, position)
        size = header >> 20
        position += 4
        if size == 0xFFF:
            if position + 4 > len(data):
                return
            size, = struct.unpack_from("<I", data, position)
            position += 4
        yield header & 0x3FF, (header >> 10) & 0x3FF, data[position:position + size]
        position += size


def _hwp_text(payload: bytes) -> str:
    """The characters of a PARA_TEXT record, a control that parts words as a space and any other as nothing."""
    units = struct.unpack_from(f"<{len(payload) // 2}H", payload)
    kept: list[int] = []
    position = 0
    while position < len(units):
        unit = units[position]
        if unit >= 32:
            kept.append(unit)
        elif unit in _HWP_SPACES:
            kept.append(32)
        position += 1 if unit >= 32 or unit in _HWP_ONE_UNIT else 8
    return struct.pack(f"<{len(kept)}H", *kept).decode("utf-16-le", "replace")


def _hwp(path: Path):
    """One unit per paragraph of the body streams: its text, with the controls it lies under (`tbl`, `gso`, `secd` …).
    A file whose body is encrypted (a password, a distribution document) states no text to compare with."""
    with olefile.OleFileIO(str(path)) as ole:
        flags, = struct.unpack_from("<I", ole.openstream("FileHeader").read(40), 36)
        if flags & (_HWP_PASSWORD | _HWP_DISTRIBUTION):
            raise ValueError("the body is encrypted")
        sections = sorted((entry for entry in ole.listdir() if len(entry) == 2 and entry[0] == "BodyText"
                           and re.fullmatch(r"Section\d+", entry[1])), key=lambda entry: int(entry[1][7:]))
        for entry in sections:
            data = ole.openstream(entry).read(PART_BYTES + 1)
            if flags & 0x1:
                inflater = zlib.decompressobj(-15)
                data = inflater.decompress(data, PART_BYTES + 1)
            if len(data) > PART_BYTES:
                continue
            above: list[tuple[int, str]] = []  # (level, name) of the controls the record being read lies under
            for tag, level, payload in _hwp_records(data):
                if tag in (_HWP_PARA_HEADER, _HWP_CTRL_HEADER):
                    above = [(at, name) for at, name in above if at < level]
                if tag == _HWP_CTRL_HEADER:
                    above.append((level, payload[:4][::-1].decode("latin-1").strip()))
                elif tag == _HWP_PARA_TEXT:
                    text = _hwp_text(payload)
                    if text.strip():
                        yield "/".join(entry), [(text, "/".join(name for at, name in above if at < level) or "body")]


READERS = {".docx": _docx, ".docm": _docx, ".pptx": _pptx, ".pptm": _pptx, ".xlsx": _xlsx, ".xlsm": _xlsx, ".hwpx": _hwpx}
SUFFIXES = frozenset({*READERS, ".hwp"})


def source_units(path: Path) -> list[Unit]:
    """The text a document states in its own XML or records, a paragraph or a cell at a time."""
    if path.suffix.lower() == ".hwp":
        return list(_hwp(path))
    with zipfile.ZipFile(path) as archive:
        return list(READERS[path.suffix.lower()](archive))


def compare_text(units: list[Unit], output: str) -> dict:
    """Count the words of `units`, those missing from `output` and the pairs `output` joined; name each loss with its place."""
    written = unicodedata.normalize("NFC", output)
    words = set(_WORD.findall(written))
    squeezed = _NOT_WORD.sub("", _INNER_MARKS.sub("", written))
    source = "\n".join(unicodedata.normalize("NFC", "".join(piece for piece, _ in pieces)) for _, pieces in units)
    total, missing, joined = 0, [], []
    for part, pieces in units:
        text = unicodedata.normalize("NFC", "".join(piece for piece, _ in pieces))
        ends, position = [], 0
        for piece, chain in pieces:
            position += len(unicodedata.normalize("NFC", piece))
            ends.append((position, chain))
        before = None
        for match in _WORD.finditer(text):
            word = match.group()
            total += 1
            where = next((chain for end, chain in ends if match.start() < end), "")
            if word not in words and word not in squeezed:
                missing.append([word, part, where])
            elif before is not None and _dropped_between(before, match, text, written, source):
                joined.append([before.group() + text[before.end():match.start()] + word, part, where])
            before = match
    return {"words": total, "missing": missing, "joined": joined, "private_use": len(_PRIVATE_USE.findall(source))}


def _dropped_between(first: re.Match[str], second: re.Match[str], text: str, written: str, source: str) -> bool:
    """Whether the output writes the two words as one although something visible stands between them in the source, and
    the source writes them as one nowhere."""
    between = text[first.end():second.start()]
    if not between or _INVISIBLE.issuperset(between):
        return False
    one, other = first.group(), second.group()
    if len(one) < _JOINED_WORD_LETTERS or len(other) < _JOINED_WORD_LETTERS or one.isdigit() or other.isdigit():
        return False
    pair = one + other
    return pair in written and pair not in source and (one + between + other) not in written


def _recall(path: str) -> tuple[str, dict]:
    document = Path(path)
    try:
        units = source_units(document)
    except (zipfile.BadZipFile, etree.XMLSyntaxError, KeyError, OSError, ValueError, struct.error, zlib.error):
        return document.name, {"status": "source not read"}
    try:
        result = convert(document.read_bytes(), document.name, OPTIONS)
    except ConversionError as error:
        return document.name, {"status": f"refused ({error.code})"}
    if result.exceeded:
        return document.name, {"status": "cut at the character limit"}
    return document.name, {"status": "converted", **compare_text(units, result.text)}


def run(documents: Path) -> dict:
    """The recall of every document of the folder this module reads. Returns the card."""
    paths = sorted(str(path) for path in documents.iterdir() if path.suffix.lower() in SUFFIXES and not path.name.startswith("~$"))
    with ProcessPoolExecutor(max_workers=WORKERS) as pool:
        results = dict(pool.map(_recall, paths, chunksize=4))
    converted = [entry for entry in results.values() if entry["status"] == "converted"]
    totals = {"documents": len(results), "converted": len(converted),
              "words": sum(entry["words"] for entry in converted),
              "missing": sum(len(entry["missing"]) for entry in converted),
              "joined": sum(len(entry["joined"]) for entry in converted),
              "private_use": sum(entry["private_use"] for entry in converted)}
    return {"totals": totals, "documents": results}


def _counts(card: dict) -> dict:
    """The card as a baseline keeps it: the counts of each document, not its words (a baseline holds no text of a document)."""
    documents = {name: ({"status": entry["status"], "words": entry["words"], "missing": len(entry["missing"]),
                         "joined": len(entry["joined"]), "private_use": entry["private_use"]}
                        if entry["status"] == "converted" else {"status": entry["status"]})
                 for name, entry in card["documents"].items()}
    return {"totals": card["totals"], "documents": documents}


_NAMES_SHOWN = 40  # how many documents of the baseline that are not in a run are named


def compare(card: dict, baseline: dict) -> tuple[list[str], bool]:
    """Lines saying what changed against `baseline`, and whether a document loses more than it did."""
    lines, worse = [], False
    for key in ("documents", "converted", "words", "missing", "joined", "private_use"):
        lines.append(f"{key:<12}{baseline['totals'].get(key, 0):>10}{card['totals'][key]:>10}")
    # a folder fetched again without a document the source no longer serves has smaller totals than the baseline's, which
    # do not say why: the documents are named (each of the others is still compared on its own line below)
    absent = sorted(baseline["documents"].keys() - card["documents"].keys())
    if absent:
        shown = ", ".join(absent[:_NAMES_SHOWN]) + (f" and {len(absent) - _NAMES_SHOWN} more" if len(absent) > _NAMES_SHOWN else "")
        lines.append(f"documents of the baseline not in this run: {len(absent)} ({shown})")
    for name, entry in sorted(card["documents"].items()):
        was = baseline["documents"].get(name)
        if was is None:
            continue
        if entry["status"] != was["status"]:
            lines.append(f"  {name}: {was['status']} -> {entry['status']}")
            worse = worse or was["status"] == "converted"
            continue
        if entry["status"] != "converted":
            continue
        for key in ("missing", "joined"):
            now, then = len(entry[key]), was[key]
            if now != then:
                lines.append(f"  {name}: {key} {then} -> {now}")
                worse = worse or now > then
    return lines, worse


def _groups(card: dict, key: str, shown: int) -> list[str]:
    """The losses of the folder grouped by the elements they sit in, the place found in most documents first."""
    documents: dict[str, set[str]] = {}
    count: Counter[str] = Counter()
    examples: dict[str, list[str]] = {}
    for name, entry in card["documents"].items():
        for word, part, chain in entry.get(key, ()):
            place = re.sub(r"\d+", "N", part.rsplit("/", 1)[-1]) + " " + "/".join(chain.split("/")[-6:])
            count[place] += 1
            documents.setdefault(place, set()).add(name)
            if len(examples.setdefault(place, [])) < 5:
                examples[place].append(word)
    places = sorted(count, key=lambda place: (-len(documents[place]), -count[place]))[:shown]
    return [f"  {count[place]:>5} in {len(documents[place]):>3} documents  {place}  {examples[place]}  "
            f"e.g. {sorted(documents[place])[0]}" for place in places]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--documents", type=Path, required=True, help="folder of DOCX, PPTX, XLSX, HWPX and HWP documents")
    parser.add_argument("--baseline", type=Path, help="counts to compare with")
    parser.add_argument("--update-baseline", action="store_true", help="write this run to --baseline instead of comparing")
    parser.add_argument("--show", type=int, default=0, help="how many places of losses to list, with the words lost there")
    arguments = parser.parse_args(argv)

    card = run(arguments.documents)
    totals = card["totals"]
    print(f"{totals['converted']} of {totals['documents']} documents converted, {totals['words']} words: "
          f"{totals['missing']} missing, {totals['joined']} joined; {totals['private_use']} private-use characters in the sources")
    if arguments.show:
        for key in ("missing", "joined"):
            print(f"{key}, by place:")
            print("\n".join(_groups(card, key, arguments.show)) or "  none")
    if arguments.baseline is None:
        return 0
    if arguments.update_baseline:
        arguments.baseline.write_text(json.dumps(_counts(card), ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    lines, worse = compare(card, json.loads(arguments.baseline.read_text(encoding="utf-8")))
    print(f"{'':<12}{'baseline':>10}{'now':>10}")
    print("\n".join(lines))
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
