"""Does a damaged document end the reader, hang it, or raise something that is no conversion error? Damage real ones and see.

    uv run python -m evals.damage --documents <dir> [--rounds <n>] [--timeout <seconds>] [--keep <dir>]

A reader is written against the documents a program saves. What reaches a server is also the upload that stopped half
way, the file a disk or a mail gateway changed, and the file somebody made to break a reader. This takes every document
of a folder, damages it in the ways below, and converts each damaged copy in a worker process of `IsolatedConverter`,
which is how a server runs the reader. A copy may be converted or refused; it may not

- end the worker (a crash of a C library, or memory over the worker's limit where the system enforces one);
- take longer than the time limit;
- raise anything but a `ConversionError`.

The run ends with status 1 when a copy does one of these, and `--keep` writes those copies to a folder, to convert again.

The damage, the same on every run (the places depend on the name of the file and on nothing else):

- the file itself: cut at a tenth, a half, nine tenths, 99% and one byte before its end; bits flipped at one, five and nine
  places; 64 bytes set to zero;
- a part of a ZIP package (HWPX, DOCX, PPTX, XLSX …), for its three largest XML parts, with the package written again
  around it so that the damage reaches the reader of the part and not the check sum of the archive: bits flipped, a
  number of an attribute set to 0, -1, 65535 or 99999999, the part cut in half, the part left out;
- a stream of an HWP 5 file, for `DocInfo` and the first sections of `BodyText`, decompressed, damaged and compressed
  again in a container built again around it: bits flipped, two or four bytes set to a small or a very large number (a
  count, a size, an index), the stream cut; and the file without its body.

`--rounds` repeats the damage at other places, for a deeper look than a pull request needs. What it does not see: memory
on macOS, where a process cannot be given a limit; and a damaged copy that is read wrongly but read.
"""

import argparse
import asyncio
from collections import Counter
from collections.abc import Iterator
from concurrent.futures.process import BrokenProcessPool
import hashlib
import io
from pathlib import Path
import re
import sys
import time
import zipfile
import zlib

import olefile

from opendockit import ConversionError, IsolatedConverter


WORKERS = 4
TIMEOUT = 30.0  # seconds a copy may take; the slowest whole document of the Hangul samples takes two
CUTS = (0.1, 0.5, 0.9, 0.99)
FLIPS = 6  # copies with flipped bits, of the file and of each part
PARTS = 3  # the parts of a package, or the streams of an HWP file, that are damaged
_NUMBER = re.compile(rb'="(\d{1,4})"')
_NUMBERS = (b"0", b"-1", b"65535", b"99999999")
_VALUES = (b"\xff\xff", b"\xff\x7f", b"\xff\xff\xff\x7f", b"\xff\xff\xff\xff", b"\x00\x00\x00\x00", b"\x00\x00\x01\x00")
FAILURES = ("died", "hung", "raised")


class Dice:
    """Numbers that depend on the names given and on nothing else: every run, on every system, damages the same places."""

    def __init__(self, *names: str) -> None:
        self._seed = "\0".join(names).encode()
        self._drawn = 0

    def below(self, limit: int) -> int:
        self._drawn += 1
        return int.from_bytes(hashlib.sha256(self._seed + self._drawn.to_bytes(8, "big")).digest()[:8], "big") % limit


def flipped(data: bytes, dice: Dice, count: int) -> bytes:
    """`data` with one bit changed at each of `count` places."""
    body = bytearray(data)
    for _ in range(count if body else 0):
        body[dice.below(len(body))] ^= 1 << dice.below(8)
    return bytes(body)


def with_value(data: bytes, dice: Dice) -> bytes:
    """`data` with two or four bytes set to a small or a very large number."""
    if len(data) < 8:
        return data
    value = _VALUES[dice.below(len(_VALUES))]
    at = dice.below(len(data) - 4)
    return data[:at] + value + data[at + len(value):]


def _of_the_file(data: bytes, dice: Dice) -> Iterator[tuple[str, bytes]]:
    for cut in CUTS:
        yield f"cut at {cut}", data[:int(len(data) * cut)]
    yield "cut one byte short", data[:-1]
    for index in range(FLIPS):
        yield f"bits flipped {index}", flipped(data, dice, 1 + index % 3 * 4)
    start = dice.below(max(1, len(data) - 64))
    yield "zeros", data[:start] + bytes(64) + data[start + 64:]


def _of_a_package(data: bytes, dice: Dice) -> Iterator[tuple[str, bytes]]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            parts = {info.filename: archive.read(info) for info in archive.infolist()}
            dates = {info.filename: info.date_time for info in archive.infolist()}
    except Exception:  # noqa: BLE001 - no package this module can take apart: the damage of the file is all it gets
        return

    def packed(changed: dict[str, bytes]) -> bytes:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            for name, body in changed.items():
                # Dated as the document dates the part: given a name, `writestr` dates it now, and the same damage gave
                # other bytes once a two-second tick of the ZIP clock had passed
                # (Pinned by: test_evals_damage.py::TestDamaged::test_the_copies_are_the_same_at_any_time).
                part = zipfile.ZipInfo(name, date_time=dates[name])
                part.external_attr = 0o600 << 16   # what `writestr` gives a part it is handed by name
                archive.writestr(part, body, zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED)
        return buffer.getvalue()

    for name in sorted((name for name in parts if name.lower().endswith(".xml")), key=lambda name: (-len(parts[name]), name))[:PARTS]:
        body = parts[name]
        for index in range(FLIPS):
            yield f"{name}: bits flipped {index}", packed({**parts, name: flipped(body, dice, 1 + index)})
        numbers = list(_NUMBER.finditer(body))
        for index in range(4 if numbers else 0):
            number = numbers[dice.below(len(numbers))]
            changed = body[:number.start(1)] + _NUMBERS[dice.below(len(_NUMBERS))] + body[number.end(1):]
            yield f"{name}: number {index}", packed({**parts, name: changed})
        yield f"{name}: cut", packed({**parts, name: body[:len(body) // 2]})
        yield f"{name}: left out", packed({other: value for other, value in parts.items() if other != name})


def _of_an_hwp(data: bytes, dice: Dice) -> Iterator[tuple[str, bytes]]:
    # The container is built again by the builder of the tests, which writes streams of any size; olefile writes a stream
    # only in place and only outside the mini stream, where the sections of most documents are.
    from tests.builder import ole_file

    try:
        with olefile.OleFileIO(io.BytesIO(data)) as ole:
            streams = {"/".join(entry): ole.openstream(entry).read() for entry in ole.listdir()}
            storages = tuple(entry[0] for entry in ole.listdir(streams=False, storages=True) if len(entry) == 1)
        compressed = bool(streams["FileHeader"][36] & 1)
    except Exception:  # noqa: BLE001 - no HWP 5 container (HWP 3, a damaged original): the damage of the file is all it gets
        return

    for name in [name for name in sorted(streams) if name == "DocInfo" or name.startswith("BodyText/")][:PARTS]:
        try:
            plain = zlib.decompress(streams[name], -15) if compressed else streams[name]
        except zlib.error:
            continue

        def packed(changed: bytes, name: str = name) -> bytes:
            if compressed:
                packer = zlib.compressobj(6, zlib.DEFLATED, -15)
                changed = packer.compress(changed) + packer.flush()
            return ole_file({**streams, name: changed}, storages=storages)

        for index in range(FLIPS):
            yield f"{name}: bits flipped {index}", packed(flipped(plain, dice, 1 + index))
        for index in range(FLIPS):
            changed = plain
            for _ in range(1 + index % 3):
                changed = with_value(changed, dice)
            yield f"{name}: value {index}", packed(changed)
        yield f"{name}: cut", packed(plain[:len(plain) // 2])
        yield f"{name}: cut inside a record", packed(plain[:len(plain) * 2 // 3 + 1])
    yield "without its body", ole_file({name: body for name, body in streams.items() if not name.startswith("BodyText/")},
                                       storages=storages)


def damaged(name: str, data: bytes, round_: int = 0) -> Iterator[tuple[str, bytes]]:
    """The damaged copies of a document, each as (what was done, the bytes). The first round also gives the document whole."""
    dice = Dice(name, str(round_))
    if round_ == 0:
        yield "whole", data
    yield from _of_the_file(data, dice)
    if data.startswith(b"PK"):
        yield from _of_a_package(data, dice)
    elif data.startswith(olefile.MAGIC) and name.lower().endswith(".hwp"):
        yield from _of_an_hwp(data, dice)


def _outcome(error: BaseException | None) -> str:
    if error is None:
        return "read"
    if not isinstance(error, ConversionError):
        return "raised"
    # `IsolatedConverter` reports a worker that ran over its time or ended as one error, with what happened as its cause
    if isinstance(error.__cause__, TimeoutError):
        return "hung"
    if isinstance(error.__cause__, BrokenProcessPool):
        return "died"
    return "refused"


async def run(documents: Path, *, rounds: int = 1, timeout: float = TIMEOUT, keep: Path | None = None) -> list[dict]:
    """Convert every damaged copy of every file of the folder. One entry for each: the file, the damage, the outcome
    (`read`, `refused`, `died`, `hung`, `raised`) and the seconds it took."""
    results: list[dict] = []
    free = asyncio.Semaphore(WORKERS)
    tasks: set[asyncio.Task[None]] = set()

    async def convert(converter: IsolatedConverter, path: Path, damage: str, data: bytes) -> None:
        start = time.perf_counter()
        try:
            await converter.convert(data, path.name)
            error = None
        except Exception as raised:  # noqa: BLE001 - what is raised is what is measured
            error = raised
        finally:
            free.release()
        outcome = _outcome(error)
        results.append({"file": path.name, "damage": damage, "outcome": outcome, "seconds": round(time.perf_counter() - start, 3)})
        if keep is not None and outcome in FAILURES:
            keep.mkdir(parents=True, exist_ok=True)
            (keep / f"{len(results):05}-{outcome}-{path.name}").write_bytes(data)

    async with IsolatedConverter(workers=WORKERS, timeout=timeout) as converter:
        for round_ in range(rounds):
            for path in sorted(item for item in documents.iterdir() if item.is_file()):
                for damage, data in damaged(path.name, path.read_bytes(), round_):
                    # a copy is made when a worker is free for it: the copies of one large document are not held together
                    await free.acquire()
                    task = asyncio.create_task(convert(converter, path, damage if rounds == 1 else f"round {round_}, {damage}", data))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
        await asyncio.gather(*tasks)
    return results


def report(results: list[dict], shown: int = 20) -> tuple[list[str], bool]:
    """The lines of the report, and whether a copy ended a worker, ran over the time or raised another error."""
    counts = Counter(entry["outcome"] for entry in results)
    lines = [f"{len(results)} conversions of {len({entry['file'] for entry in results})} documents: "
             + ", ".join(f"{counts[outcome]} {outcome}" for outcome in ("read", "refused", *FAILURES))]
    failures = [entry for entry in results if entry["outcome"] in FAILURES]
    lines.extend(f"  {entry['outcome'].upper()}  {entry['file']}  {entry['damage']}  {entry['seconds']} s" for entry in failures[:shown])
    if len(failures) > shown:
        lines.append(f"  ... and {len(failures) - shown} more")
    slowest = max(results, key=lambda entry: entry["seconds"], default=None)
    if slowest is not None:
        lines.append(f"slowest: {slowest['seconds']} s  {slowest['file']}  {slowest['damage']}")
    return lines, bool(failures)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--documents", type=Path, required=True, help="folder of documents to damage")
    parser.add_argument("--rounds", type=int, default=1, help="how many times to damage each document, at other places each time")
    parser.add_argument("--timeout", type=float, default=TIMEOUT, help="seconds a damaged copy may take")
    parser.add_argument("--keep", type=Path, help="folder to write the copies to that ended a worker, hung or raised")
    arguments = parser.parse_args(argv)
    if not arguments.documents.is_dir():
        parser.error(f"no such folder: {arguments.documents}")
    if arguments.rounds < 1:
        parser.error("--rounds is at least 1")

    results = asyncio.run(run(arguments.documents, rounds=arguments.rounds, timeout=arguments.timeout, keep=arguments.keep))
    lines, failed = report(results)
    print("\n".join(lines))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
