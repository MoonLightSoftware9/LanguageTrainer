#!/usr/bin/env python3

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import threading
from pathlib import Path

from pystardict import Dictionary

from lookup_parse import DICT_DIR as MACMILLAN_DICT_DIR
from lookup_parse import parse_article, parse_thesaurus_article


def default_root_dir():
    if root_dir := os.environ.get("DICT_ROOT_DIR"):
        return Path(root_dir).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


ROOT_DIR = default_root_dir()
FULL_DICT = ROOT_DIR / "stardict-eng_rus_full-2.4.2" / "eng_rus_full"
MACMILLAN_BASE = ROOT_DIR / MACMILLAN_DICT_DIR.parent
MACMILLAN_DICT_PATH = ROOT_DIR / MACMILLAN_DICT_DIR
MACMILLAN_RES_DIR = MACMILLAN_BASE / "res"
THESAURUS_DICT_PATH = (
    ROOT_DIR / "stardict-Macmillan_English_Thesaurus-2.4.2" / "Macmillan_English_Thesaurus"
)

# Prebuilt SQLite cache of parsed lookups (see build_dictionaries_db.py). When
# present it replaces the slow StarDict gzip path with a single indexed SELECT.
# The StarDict path below remains as a fallback when the file is absent.
DICTIONARIES_DB = ROOT_DIR / "dictionaries.db"

_dictionary = None
_macmillan_dictionary = None
_thesaurus_dictionary = None
_entries_db_present = None
_entries_thread_local = threading.local()


def entries_db_available():
    global _entries_db_present
    if _entries_db_present is None:
        _entries_db_present = DICTIONARIES_DB.is_file()
    return _entries_db_present


def _entries_connection():
    connection = getattr(_entries_thread_local, "connection", None)
    if connection is None:
        connection = sqlite3.connect(
            f"file:{DICTIONARIES_DB}?mode=ro", uri=True, check_same_thread=False
        )
        _entries_thread_local.connection = connection
    return connection


def db_lookup(source, word):
    """Return the parsed payload for a word, or None if the db has no entry.

    Tries the same case candidates, in the same order, as the StarDict lookup
    (``lookup_raw`` / ``lookup_macmillan_raw``) so results match exactly.
    """
    base = (word or "").strip()
    if not base:
        return None
    candidates = []
    for candidate in (base, base.lower(), base.capitalize()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    connection = _entries_connection()
    for candidate in candidates:
        row = connection.execute(
            "SELECT payload FROM entries WHERE source = ? AND word = ? LIMIT 1",
            (source, candidate),
        ).fetchone()
        if not row:
            continue
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None
    return None
SECTION_RE = re.compile(
    r"(?<!\S)(?:\d+\.\s+)?(?:сущ|гл|прил|нареч|предл|мест|союз|межд|числ)\."
)


def get_dictionary():
    global _dictionary
    if _dictionary is None:
        _dictionary = Dictionary(str(FULL_DICT))
    return _dictionary


def get_macmillan_dictionary():
    global _macmillan_dictionary
    if _macmillan_dictionary is None:
        _macmillan_dictionary = Dictionary(str(MACMILLAN_DICT_PATH))
    return _macmillan_dictionary


def get_thesaurus_dictionary():
    global _thesaurus_dictionary
    if _thesaurus_dictionary is None:
        _thesaurus_dictionary = Dictionary(str(THESAURUS_DICT_PATH))
    return _thesaurus_dictionary


def clean_text(value):
    return re.sub(r"\s+", " ", value).strip()


def lookup_raw(word):
    dictionary = get_dictionary()
    candidates = []
    for candidate in (word, word.lower(), word.capitalize()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            raw = dictionary[candidate]
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = str(raw)
        if raw.strip():
            return raw
    return ""


def lookup_macmillan_raw(word):
    dictionary = get_macmillan_dictionary()
    candidates = []
    for candidate in (word, word.lower(), word.capitalize()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            raw = dictionary[candidate]
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = str(raw)
        if raw.strip():
            return raw
    return ""


def lookup_thesaurus_raw(word):
    dictionary = get_thesaurus_dictionary()
    candidates = []
    for candidate in (word, word.lower(), word.capitalize()):
        if candidate and candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        try:
            raw = dictionary[candidate]
        except Exception:
            continue
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        raw = str(raw)
        if raw.strip():
            return raw
    return ""


def strip_stardict_header(raw):
    body = re.sub(r"<k>.*?</k>\s*", "", raw, flags=re.DOTALL)
    body = re.sub(r"<tr>.*?</tr>\s*", "", body, count=1, flags=re.DOTALL)
    body = re.sub(r"<[^>]+>", " ", body)
    return clean_text(html.unescape(body))


def split_numbered_values(text):
    matches = list(re.finditer(r"(?<!\w)(\d+\))", text))
    if not matches:
        return []

    prefix = re.sub(r"^\d+\.\s+", "", clean_text(text[: matches[0].start()]))
    rows = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = clean_text(text[match.start() : end])
        if prefix:
            value = f"{prefix} {value}"
        rows.append(value)
    return rows


def split_semicolon_outside_parentheses(text):
    rows = []
    start = 0
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char == ";" and depth == 0:
            rows.append(text[start:index])
            start = index + 1
    rows.append(text[start:])
    return rows


def split_unnumbered_values(text):
    text = re.sub(r"^\d+\.\s+", "", clean_text(text))
    if not text:
        return []

    pieces = []
    for chunk in re.split(r"\s+~\s+(?=[А-Яа-я]|attr\.)", text):
        pieces.extend(split_semicolon_outside_parentheses(chunk))

    rows = [clean_text(piece.strip(" -~")) for piece in pieces]
    return [row for row in rows if row]


def parse_translations(raw):
    paragraphs = [
        strip_stardict_header(paragraph)
        for paragraph in re.split(r"\n\s*\n", html.unescape(str(raw)))
    ]
    paragraphs = [paragraph for paragraph in paragraphs if paragraph]

    if not paragraphs:
        return []

    translations = []
    for paragraph in paragraphs:
        section_starts = [match.start() for match in SECTION_RE.finditer(paragraph)]
        if len(section_starts) > 1:
            section_starts.append(len(paragraph))
            sections = [
                clean_text(paragraph[section_starts[index] : section_starts[index + 1]])
                for index in range(len(section_starts) - 1)
            ]
        else:
            sections = [paragraph]

        for section in sections:
            numbered_values = split_numbered_values(section)
            if numbered_values:
                translations.extend(numbered_values)
            else:
                translations.extend(split_unnumbered_values(section))

    return translations


def translate_word(word):
    if entries_db_available():
        entry = db_lookup("eng_rus", word.strip())
        translations = entry.get("translations", []) if entry else []
    else:
        raw = lookup_raw(word.strip())
        translations = parse_translations(raw) if raw else []
    macmillan = lookup_macmillan(word)
    return {
        "word": word,
        "found": bool(translations),
        "translations": translations,
        "macmillan": macmillan,
    }


def escape(value):
    return html.escape(str(value), quote=True)


def render_inline_items(items, class_name):
    if not items:
        return ""
    links = "".join(f'<span class="{class_name}">{escape(item)}</span>' for item in items)
    return links


def render_macmillan_html(parsed):
    if not parsed or not parsed.get("entries"):
        return ""

    rows = [f'<article class="macmillan-entry"><h3>{escape(parsed.get("word") or "")}</h3>']
    for entry in parsed["entries"]:
        headword = entry.get("headword") or parsed.get("word") or ""
        stars = "★" * int(entry.get("stars") or 0)
        rows.append('<section class="macmillan-entry-block">')
        rows.append('<div class="macmillan-header">')
        if entry.get("homograph"):
            rows.append(f'<span class="macmillan-homograph">{escape(entry["homograph"])}</span>')
        rows.append(f'<strong class="macmillan-headword">{escape(headword)}</strong>')
        for pronunciation in entry.get("pronunciations", []):
            region = pronunciation.get("region")
            ipa = pronunciation.get("ipa")
            if region:
                rows.append(f'<span class="macmillan-region">{escape(region)}</span>')
            if ipa:
                rows.append(f'<span class="macmillan-ipa">{escape(ipa)}</span>')
        if entry.get("part_of_speech"):
            rows.append(f'<span class="macmillan-pos">{escape(entry["part_of_speech"])}</span>')
        for label in entry.get("labels", []):
            rows.append(f'<span class="macmillan-label">{escape(label)}</span>')
        if stars:
            rows.append(f'<span class="macmillan-stars">{escape(stars)}</span>')
        rows.append("</div>")

        for sense in entry.get("senses", []):
            rows.append('<div class="macmillan-sense">')
            if sense.get("number"):
                rows.append(f'<span class="macmillan-number">{escape(sense["number"])}</span>')
            for label in sense.get("labels", []):
                rows.append(f'<span class="macmillan-label">{escape(label)}</span>')
            rows.append(f'<span class="macmillan-definition">{escape(sense.get("definition") or "")}</span>')
            for example in sense.get("examples", []):
                rows.append(f'<div class="macmillan-example">{escape(example)}</div>')
            for pattern in sense.get("patterns", []):
                rows.append(f'<div class="macmillan-pattern">{escape(pattern)}</div>')
            if sense.get("thesaurus"):
                rows.append('<div class="macmillan-related"><span>Thesaurus:</span> ')
                rows.append(
                    " ".join(
                        # Clickable: opens the Thesaurus tab for this topic. The
                        # target is the Thesaurus dictionary headword (data-thesaurus).
                        f'<span class="macmillan-kref macmillan-thesaurus-link" '
                        f'role="button" tabindex="0" '
                        f'data-thesaurus="{escape(item.get("target") or "")}">'
                        f'{escape(item.get("target") or "")}</span>'
                        + (
                            f'<span class="macmillan-relation">{escape(item.get("relation"))}</span>'
                            if item.get("relation")
                            else ""
                        )
                        for item in sense["thesaurus"]
                    )
                )
                rows.append("</div>")
            rows.append("</div>")

        if entry.get("phrases"):
            rows.append(f'<div class="macmillan-related"><span>Phrases:</span> {render_inline_items(entry["phrases"], "macmillan-kref")}</div>')
        if entry.get("phrasal_verbs"):
            rows.append(f'<div class="macmillan-related"><span>Phrasal verbs:</span> {render_inline_items(entry["phrasal_verbs"], "macmillan-kref")}</div>')
        if entry.get("see_also"):
            rows.append(f'<div class="macmillan-related"><span>See also:</span> {render_inline_items(entry["see_also"], "macmillan-kref")}</div>')
        rows.append("</section>")
    rows.append("</article>")
    return "".join(rows)


def render_thesaurus_html(parsed):
    if not parsed or not parsed.get("items"):
        return ""

    title = parsed.get("title") or parsed.get("topic") or ""
    rows = ['<article class="thesaurus-entry">']
    header = f'<h3>{escape(title)}</h3>'
    rows.append(f'<div class="thesaurus-topic-header">{header}</div>')
    for item in parsed["items"]:
        rows.append('<div class="thesaurus-item">')
        rows.append('<div class="thesaurus-item-head">')
        rows.append(f'<strong class="thesaurus-word">{escape(item.get("word") or "")}</strong>')
        if item.get("pos"):
            rows.append(f'<span class="thesaurus-pos">{escape(item["pos"])}</span>')
        for label in item.get("labels", []):
            rows.append(f'<span class="thesaurus-label">{escape(label)}</span>')
        rows.append("</div>")
        if item.get("definition"):
            rows.append(f'<div class="thesaurus-definition">{escape(item["definition"])}</div>')
        rows.append("</div>")
    rows.append("</article>")
    return "".join(rows)


def lookup_thesaurus(word):
    parsed = None
    if entries_db_available():
        parsed = db_lookup("thesaurus", word.strip())
    else:
        raw = lookup_thesaurus_raw(word.strip())
        parsed = parse_thesaurus_article(raw) if raw else None

    if not parsed or not parsed.get("items"):
        return {"found": False, "html": "", "title": None}

    return {
        "found": True,
        "html": render_thesaurus_html(parsed),
        "title": parsed.get("title") or parsed.get("topic"),
    }


def macmillan_audio_url(audio):
    if not audio:
        return None
    name = Path(audio).name
    path = MACMILLAN_RES_DIR / name
    if not path.is_file():
        return None
    return f"/api/macmillan/audio/{name}"


def lookup_macmillan(word):
    if entries_db_available():
        parsed = db_lookup("macmillan", word.strip())
    else:
        raw = lookup_macmillan_raw(word.strip())
        parsed = parse_article(raw) if raw else None

    if not parsed:
        return {
            "found": False,
            "html": "",
            "pronunciations": [],
        }

    pronunciations = []
    seen = set()
    for entry in parsed.get("entries", []):
        for pronunciation in entry.get("pronunciations", []):
            region = pronunciation.get("region")
            audio = pronunciation.get("audio")
            url = macmillan_audio_url(audio)
            key = (region, url, pronunciation.get("ipa"))
            if not region or key in seen:
                continue
            seen.add(key)
            pronunciations.append(
                {
                    "region": region,
                    "ipa": pronunciation.get("ipa"),
                    "audio": audio,
                    "url": url,
                    "available": bool(url),
                }
            )

    return {
        "found": True,
        "html": render_macmillan_html(parsed),
        "pronunciations": pronunciations,
    }


def format_translation(result):
    if not result["found"]:
        return f"Translation not found: {result['word']}"

    return "\n\n".join(result["translations"])


def main(argv=None):
    parser = argparse.ArgumentParser(description="Look up a word in English-Russian StarDict.")
    parser.add_argument("word", help="English word to translate")
    parser.add_argument("--json", action="store_true", help="print structured JSON")
    args = parser.parse_args(argv)

    result = translate_word(args.word)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_translation(result))

    return 0 if result["found"] else 1


if __name__ == "__main__":
    sys.exit(main())
