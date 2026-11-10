#!/usr/bin/env python3
"""Precompute the offline dictionary lookups into a SQLite database.

The StarDict dictionaries (eng_rus + Macmillan) are slow at runtime: Python's
``gzip`` ignores the dictzip block index, so every ``.dict.dz`` fetch
decompresses the stream from the start (O(offset) per word). This script pays
that cost once, offline, and stores the *parsed* result per word so the running
app can serve a lookup with a single indexed ``SELECT`` and no gzip/parsing.

Stored (one row per source+word, ``word`` lowercased for case-insensitive
lookup):

* ``eng_rus``   -> ``{"translations": ["...", ...]}`` (the chips shown in the UI)
* ``macmillan`` -> the ``parse_article`` structure; the app renders HTML from it
  at request time (rendering is cheap, so CSS/markup can change without a
  rebuild).

Run: ``python build_dictionaries_db.py [--output dictionaries.db]``
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from pystardict import Dictionary

import translate as tr
from lookup_parse import parse_article, parse_thesaurus_article


def _raw_to_str(raw):
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def iter_entries(dict_path, transform):
    """Yield (word_lower, payload_json) for every non-empty entry."""
    dictionary = Dictionary(str(dict_path), in_memory=True)
    for word in dictionary.keys():
        try:
            raw = _raw_to_str(dictionary[word])
        except Exception:
            continue
        if not raw.strip():
            continue
        payload = transform(raw)
        if payload is None:
            continue
        # Store the exact StarDict headword (case preserved). The runtime lookup
        # tries word / lower / capitalize in the same order as the StarDict path,
        # so case variants like "house" vs "House" stay distinct instead of
        # collapsing to one arbitrary entry.
        yield word.strip(), json.dumps(payload, ensure_ascii=False)


def eng_rus_payload(raw):
    translations = tr.parse_translations(raw)
    if not translations:
        return None
    return {"translations": translations}


def macmillan_payload(raw):
    parsed = parse_article(raw)
    if not parsed or not parsed.get("entries"):
        return None
    return parsed


def thesaurus_payload(raw):
    parsed = parse_thesaurus_article(raw)
    if not parsed or not parsed.get("items"):
        return None
    return parsed


def build(output_path):
    output_path = Path(output_path)
    tmp_path = output_path.with_name(output_path.name + ".tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    connection = sqlite3.connect(str(tmp_path))
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute(
            "CREATE TABLE entries (source TEXT NOT NULL, word TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        # Unique index up front: it is both the lookup index and dedupe for any
        # exact-duplicate headwords (INSERT OR IGNORE keeps the first).
        connection.execute(
            "CREATE UNIQUE INDEX ix_entries_source_word ON entries (source, word)"
        )

        sources = (
            ("eng_rus", tr.FULL_DICT, eng_rus_payload),
            ("macmillan", tr.MACMILLAN_DICT_PATH, macmillan_payload),
            ("thesaurus", tr.THESAURUS_DICT_PATH, thesaurus_payload),
        )
        for source, dict_path, transform in sources:
            if not Path(str(dict_path) + ".idx").exists():
                raise SystemExit(f"StarDict index missing for {source}: {dict_path}.idx")
            print(f"Building {source} from {dict_path} ...", flush=True)
            start = time.time()
            count = 0
            batch = []
            for word, payload in iter_entries(dict_path, transform):
                batch.append((source, word, payload))
                if len(batch) >= 2000:
                    connection.executemany(
                        "INSERT OR IGNORE INTO entries (source, word, payload) VALUES (?, ?, ?)",
                        batch,
                    )
                    count += len(batch)
                    batch.clear()
            if batch:
                connection.executemany(
                    "INSERT OR IGNORE INTO entries (source, word, payload) VALUES (?, ?, ?)",
                    batch,
                )
                count += len(batch)
            print(f"  {source}: {count} rows in {round(time.time() - start, 1)}s", flush=True)

        connection.commit()
        connection.execute("VACUUM")
        connection.commit()
    finally:
        connection.close()

    tmp_path.replace(output_path)
    size_mb = round(output_path.stat().st_size / (1024 * 1024), 1)
    print(f"Wrote {output_path} ({size_mb} MB)", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default=str(tr.ROOT_DIR / "dictionaries.db"),
        help="output SQLite path (default: <root>/dictionaries.db)",
    )
    args = parser.parse_args(argv)
    build(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
