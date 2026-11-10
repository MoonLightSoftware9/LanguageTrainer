#!/usr/bin/env python3

import argparse
import bisect
import mmap
import os
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def default_root_dir():
    if root_dir := os.environ.get("DICT_ROOT_DIR"):
        return Path(root_dir).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


DEFAULT_ARCHIVE = default_root_dir() / "SoundEn.lsa"
WAV_SUFFIX = ".wav".encode("utf-16le") + "\r\n".encode("utf-16le")
DATA_SIGNATURE = b"OggS"
OGG_CRC_LOOKUP = []
for value in range(256):
    register = value << 24
    for _ in range(8):
        if register & 0x80000000:
            register = ((register << 1) ^ 0x04C11DB7) & 0xFFFFFFFF
        else:
            register = (register << 1) & 0xFFFFFFFF
    OGG_CRC_LOOKUP.append(register)


@dataclass(frozen=True)
class SoundEntry:
    index: int
    word: str
    filename: str
    offset: int | None
    size: int | None


@dataclass(frozen=True)
class OggPage:
    start: int
    size: int
    granule: int
    header_type: int


def is_name_char(codepoint):
    if codepoint < 32:
        return False
    if codepoint in {0xFFFE, 0xFFFF}:
        return False
    if 0xD800 <= codepoint <= 0xDFFF:
        return False
    return True


def read_codepoint(data, offset):
    if offset < 0 or offset + 2 > len(data):
        return None
    return struct.unpack_from("<H", data, offset)[0]


def find_name_start(data, end):
    pos = end - 2
    while pos >= 0:
        codepoint = read_codepoint(data, pos)
        if codepoint is None or not is_name_char(codepoint):
            break
        pos -= 2
    return pos + 2


def parse_fields(data, name_start):
    if name_start >= 10 and data[name_start - 10] == 0xFF and data[name_start - 5] == 0xFF:
        offset = struct.unpack_from("<I", data, name_start - 9)[0]
        size = struct.unpack_from("<I", data, name_start - 4)[0]
        return offset, size

    if name_start >= 5 and data[name_start - 5] == 0xFF:
        offset = struct.unpack_from("<I", data, name_start - 4)[0]
        return offset, None

    return None, None


def repair_name(value):
    try:
        repaired = value.encode("cp866").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value

    if "\ufffd" in repaired:
        return value
    return repaired


def map_archive(path):
    """Memory-map the archive read-only so the OS pages it in on demand.

    The archive is ~138 MB; mapping it (instead of ``read_bytes``) keeps it out
    of process-resident RAM — only the pages actually touched are faulted in,
    and they live in the shared, reclaimable page cache. Returns ``b""`` for an
    empty/missing archive. Callers that don't retain the result should
    ``.close()`` it; the long-lived ``get_ogg_pages`` cache keeps its map open
    for the process lifetime.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        return b""
    if size == 0:
        return b""
    with open(path, "rb") as handle:
        return mmap.mmap(handle.fileno(), size, access=mmap.ACCESS_READ)


def parse_archive(path):
    data = map_archive(path)
    try:
        return _parse_archive(data)
    finally:
        if isinstance(data, mmap.mmap):
            data.close()


def _parse_archive(data):
    index_end = data.find(DATA_SIGNATURE)
    if index_end == -1:
        index_end = len(data)

    records = []
    seen = set()
    position = 0
    while True:
        suffix_start = data.find(WAV_SUFFIX, position, index_end)
        if suffix_start == -1:
            break

        name_start = find_name_start(data, suffix_start)
        raw_name = data[name_start : suffix_start + len(".wav".encode("utf-16le"))]
        try:
            filename = raw_name.decode("utf-16le")
        except UnicodeDecodeError:
            position = suffix_start + 2
            continue

        filename = filename.strip()
        filename = repair_name(filename)
        word = filename[:-4].strip()
        if word and filename not in seen:
            offset, size = parse_fields(data, name_start)
            seen.add(filename)
            records.append((word, filename, offset, size))

        position = suffix_start + len(WAV_SUFFIX)

    entries = []
    for index, (word, filename, _, _) in enumerate(records):
        if index + 1 < len(records):
            offset, size = records[index + 1][2], records[index + 1][3]
        else:
            offset, size = parse_fields(data, index_end)
        entries.append(
            SoundEntry(
                index=index + 1,
                word=word,
                filename=filename,
                offset=offset,
                size=size,
            )
        )

    return entries


@lru_cache(maxsize=4)
def get_entries(path=DEFAULT_ARCHIVE):
    return tuple(parse_archive(Path(path)))


@lru_cache(maxsize=4)
def get_entry_map(path=DEFAULT_ARCHIVE):
    entries = get_entries(Path(path))
    return {entry.word.casefold(): entry for entry in entries}


def find_entry(word, path=DEFAULT_ARCHIVE):
    return get_entry_map(Path(path)).get(word.casefold())


def ogg_crc(page):
    register = 0
    for byte in page:
        register = ((register << 8) & 0xFFFFFFFF) ^ OGG_CRC_LOOKUP[((register >> 24) & 0xFF) ^ byte]
    return register


def renumber_page(page, sequence):
    patched = bytearray(page)
    struct.pack_into("<I", patched, 18, sequence)
    patched[22:26] = b"\x00\x00\x00\x00"
    struct.pack_into("<I", patched, 22, ogg_crc(patched))
    return bytes(patched)


def iter_ogg_pages(data, start):
    position = start
    while position + 27 <= len(data) and data[position : position + 4] == DATA_SIGNATURE:
        segment_count = data[position + 26]
        segment_table_start = position + 27
        segment_table_end = segment_table_start + segment_count
        if segment_table_end > len(data):
            break
        size = 27 + segment_count + sum(data[segment_table_start:segment_table_end])
        if position + size > len(data):
            break
        yield OggPage(
            start=position,
            size=size,
            granule=struct.unpack_from("<q", data, position + 6)[0],
            header_type=data[position + 5],
        )
        position += size


@lru_cache(maxsize=1)
def get_ogg_pages(path=DEFAULT_ARCHIVE):
    data = map_archive(path)
    start = data.find(DATA_SIGNATURE)
    if start == -1:
        if isinstance(data, mmap.mmap):
            data.close()
        return b"", tuple()
    return data, tuple(iter_ogg_pages(data, start))


def extract_ogg(entry, path=DEFAULT_ARCHIVE):
    clip = extract_ogg_clip(entry, path=path)
    if clip is None:
        return None
    return clip["data"]


def extract_ogg_clip(entry, path=DEFAULT_ARCHIVE):
    if entry.offset is None or entry.size is None:
        return None

    data, pages = get_ogg_pages(Path(path))
    if not pages:
        return None

    start_sample = entry.offset
    end_sample = entry.offset + entry.size
    header_pages = list(pages[:2])

    # Pages carry monotonically increasing granule positions across the whole
    # archive, so the first page of the clip is found with a binary search
    # (skipping the two header pages) instead of scanning every preceding page.
    start_index = bisect.bisect_left(pages, start_sample, 2, key=lambda page: page.granule)
    if start_index >= len(pages):
        return None
    previous_granule = pages[start_index - 1].granule if start_index > 0 else 0

    data_pages = []
    for page in pages[start_index:]:
        data_pages.append(page)
        if page.granule >= end_sample:
            break

    if not data_pages:
        return None

    output = bytearray()
    sequence = 0
    for page in header_pages + data_pages:
        raw_page = data[page.start : page.start + page.size]
        output.extend(renumber_page(raw_page, sequence))
        sequence += 1
    return {
        "data": bytes(output),
        "offset_seconds": max(0, start_sample - previous_granule) / 48000,
        "duration_seconds": max(0, end_sample - start_sample) / 48000,
    }


def print_entries(entries):
    print("index\tword\tfilename")
    for entry in entries:
        print(f"{entry.index}\t{entry.word}\t{entry.filename}")


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Inspect GoldenDict/Lingvo sound archives.")
    parser.add_argument(
        "-l",
        "--list",
        action="store_true",
        help="list words and archive indexes with available pronunciations",
    )
    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE,
        help=f"sound archive path, default: {DEFAULT_ARCHIVE}",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv or sys.argv[1:])
    if not args.list:
        print("usage: get_sounds.py -l [--archive SoundEn.lsa]", file=sys.stderr)
        return 1

    entries = parse_archive(args.archive)
    try:
        print_entries(entries)
    except BrokenPipeError:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
