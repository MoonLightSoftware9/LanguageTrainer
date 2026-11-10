#!/usr/bin/env python3

import argparse
import gzip
import json
import mimetypes
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import urlopen

from sqlalchemy import delete, func, literal, text
from sqlmodel import Session, SQLModel, select

from get_sounds import extract_ogg_clip, find_entry
from translate import MACMILLAN_RES_DIR, lookup_macmillan, lookup_thesaurus, translate_word


from dictapp import db
from dictapp.config import (
    APP_VERSION,
    DATA_DIR,
    DB_PATH,
    DEPENDENCIES_MANIFEST_PATH,
    README_MD_PATH,
    README_PDF_PATH,
    README_RU_MD_PATH,
    ROOT_DIR,
    STARTER_BOOK_PATH,
    STARTER_BOOKS,
    STARTER_BOOK_TITLE,
    STARTER_WORD_LISTS,
    USER_SETTINGS_PATH,
    VENDOR_DIR,
)
from dictapp.models import (
    BookChunkTranslation,
    BookContent,
    BookProgress,
    BookQuote,
    Collection,
    CollectionSequence,
    KnownWord,
    Meaning,
    Reminder,
    StopWord,
    TranslationLock,
    User,
    Word,
)
from dictapp.translation_worker import start_translation_worker, stop_translation_worker

ARGOS_EN_RU_READY = False
ARGOS_ENV_LOCK = threading.Lock()
USER_SETTINGS_LOCK = threading.RLock()


ALEMBIC_HEAD_REVISION = "0003_make_learned_and_reminder_exclusive"
ELRU_LANGS = {"CY", "EL"}
GREEK_ENABLED = os.environ.get("DICT_ENABLE_GREEK", "0").lower() in {"1", "true", "yes"}
RU_LANGS = {"RU"}
DEFAULT_ELRU_DB_PATH = ROOT_DIR / "elru.db"
AUDIO_EXTENSIONS = (".ogg", ".mp3", ".wav", ".m4a", ".aac")
ELRU_AUDIO_DIRS = (
    ROOT_DIR / "audio" / "el",
    ROOT_DIR / "sounds" / "el",
    ROOT_DIR / "pronunciations" / "el",
    ROOT_DIR / "tmp" / "audio" / "el",
)


def elru_candidate_terms(term):
    candidates = [term]
    lowered = term.lower()
    if lowered.endswith("άω") and len(term) > 2:
        candidates.append(term[:-2] + "ώ")
    if lowered.endswith("αω") and len(term) > 2:
        candidates.append(term[:-2] + "ω")
        candidates.append(term[:-2] + "ώ")
    return list(dict.fromkeys(candidates))


def readonly_elru_connection(path):
    db_path = Path(path).resolve()
    if not db_path.is_file():
        raise SystemExit(f"Database not found: {db_path}")

    uri = f"file:{db_path}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA query_only = ON")
    connection.row_factory = sqlite3.Row
    return connection


def elru_audio_status(term):
    candidates = []
    for directory in ELRU_AUDIO_DIRS:
        for extension in AUDIO_EXTENSIONS:
            candidates.append(directory / f"{term}{extension}")
            candidates.append(directory / term.lower() / f"audio{extension}")

    for path in candidates:
        if path.is_file():
            return f"found: {path.relative_to(ROOT_DIR)}"
    return "missing"


def find_elru_words(connection, term, lang_id, limit):
    terms = elru_candidate_terms(term)

    for candidate in terms:
        exact = connection.execute(
            """
            SELECT id, name, langId, transcription
            FROM word
            WHERE langId = ? AND lower(name) = lower(?)
            ORDER BY length(name), name
            LIMIT ?
            """,
            (lang_id, candidate, limit),
        ).fetchall()
        if exact:
            match_type = "exact" if candidate == term else f"exact as {candidate}"
            return exact, match_type

    for candidate in terms:
        prefix = connection.execute(
            """
            SELECT id, name, langId, transcription
            FROM word
            WHERE langId = ? AND lower(name) LIKE lower(?)
            ORDER BY length(name), name
            LIMIT ?
            """,
            (lang_id, f"{candidate}%", limit),
        ).fetchall()
        if prefix:
            match_type = "prefix" if candidate == term else f"prefix as {candidate}"
            return prefix, match_type

    for candidate in terms:
        contains = connection.execute(
            """
            SELECT id, name, langId, transcription
            FROM word
            WHERE langId = ? AND lower(name) LIKE lower(?)
            ORDER BY length(name), name
            LIMIT ?
            """,
            (lang_id, f"%{candidate}%", limit),
        ).fetchall()
        if contains:
            match_type = "contains" if candidate == term else f"contains as {candidate}"
            return contains, match_type

    return [], "missing"


def elru_translations_for_word(connection, word_id, target_lang_id, limit):
    return connection.execute(
        """
        SELECT DISTINCT
            target.name AS name,
            target.transcription AS transcription,
            category.name AS category
        FROM translation
        JOIN word AS target ON target.id = translation.idTranslation
        LEFT JOIN category ON category.id = translation.idCategory
        WHERE translation.idWord = ? AND target.langId = ?
        ORDER BY target.name
        LIMIT ?
        """,
        (word_id, target_lang_id, limit),
    ).fetchall()


def reverse_elru_translations_for_word(connection, word_id, target_lang_id, limit):
    return connection.execute(
        """
        SELECT DISTINCT
            source.name AS name,
            source.transcription AS transcription,
            category.name AS category
        FROM translation
        JOIN word AS source ON source.id = translation.idWord
        LEFT JOIN category ON category.id = translation.idCategory
        WHERE translation.idTranslation = ? AND source.langId = ?
        ORDER BY source.name
        LIMIT ?
        """,
        (word_id, target_lang_id, limit),
    ).fetchall()


def describe_elru_word(word):
    transcription = (word["transcription"] or "").strip()
    if transcription:
        return f"{word['name']} [{transcription}]"
    return word["name"]


def print_elru_results(connection, term, source_lang_id, target_lang_id, mode, limit):
    words, match_type = find_elru_words(connection, term, source_lang_id, limit)
    print(f"query: {term}")
    print(f"direction: {mode}")
    print(f"database: {'found' if words else 'missing'}")
    print(f"match: {match_type}")
    print(f"audio: {elru_audio_status(term)}")

    if not words:
        return 1

    for index, word in enumerate(words, start=1):
        print()
        print(f"{index}. {describe_elru_word(word)}")
        print(f"id: {word['id']}")
        print("description:")

        translations = elru_translations_for_word(
            connection,
            word["id"],
            target_lang_id,
            limit,
        )
        if not translations:
            translations = reverse_elru_translations_for_word(
                connection,
                word["id"],
                target_lang_id,
                limit,
            )
        if not translations:
            print("  translation data missing")
            continue

        for item in translations:
            category = (item["category"] or "").strip()
            details = describe_elru_word(item)
            if category:
                details = f"{details} ({category})"
            print(f"  - {details}")

    return 0


def lookup_elru_description_from_connection(connection, term, limit=20):
    words, match_type = find_elru_words(connection, term, 0, 1)
    if not words:
        return None

    word = words[0]
    translations = elru_translations_for_word(connection, word["id"], 1, limit)
    if not translations:
        translations = reverse_elru_translations_for_word(connection, word["id"], 1, limit)
    if not translations:
        return None

    lines = [
        f"{describe_elru_word(word)} - Russian translations",
        f"match: {match_type}",
    ]
    for item in translations:
        category = (item["category"] or "").strip()
        details = describe_elru_word(item)
        if category:
            details = f"{details} ({category})"
        lines.append(f"- {details}")
    return "\n".join(lines)


def lookup_elru_description(term, db_path=DEFAULT_ELRU_DB_PATH, limit=20):
    with readonly_elru_connection(db_path) as connection:
        return lookup_elru_description_from_connection(connection, term, limit=limit)

DEFAULT_REMINDER_PROGRESSION = ["30m", "1h", "3h", "9h", "18h", "34h", "5d", "12d"]
DEFAULT_USER_SETTINGS = {
    "users": {
        "1": {
            "sprint_word_count": 18,
            "meanings_word_count": 3,
            "reminder_word_count": 40,
            "global_control_page_size": 20,
            "quote_text_size": 650,
            "quotes_random": False,
            "reminder_progression": DEFAULT_REMINDER_PROGRESSION,
            "meanings_disabled_langs": ["CY"],
            "strict_mode": True,
            "screen_keyboard": False,
            "import_default_lang": "EN",
            "book_min_chars": 400,
            "book_max_chars": 650,
            "translate_books_by_default": True,
            "translate_default_ahead": 10,
        },
        "2": {
            "sprint_word_count": 10,
            "meanings_word_count": 1,
            "reminder_word_count": 40,
            "global_control_page_size": 20,
            "quote_text_size": 650,
            "quotes_random": False,
            "reminder_progression": DEFAULT_REMINDER_PROGRESSION,
            "meanings_disabled_langs": ["CY"],
            "strict_mode": False,
            "screen_keyboard": False,
            "import_default_lang": "EN",
            "book_min_chars": 400,
            "book_max_chars": 650,
            "translate_books_by_default": True,
            "translate_default_ahead": 10,
        },
    }
}


# The single-page frontend (HTML + CSS + Vue app) lives in web/index.html so it
# can be edited with real tooling and served/cached as a static asset. It is read
# once at import, mirroring the previous module-level string constant.
INDEX_HTML = (ROOT_DIR / "web" / "index.html").read_text(encoding="utf-8")


def split_words(text):
    return re.findall(r"\S+", text)


def book_type_words(text):
    return [
        token["value"]
        for token in split_book_text_tokens(text)
        if token.get("type") == "word"
    ]


def is_book_number_line(value):
    return bool(re.fullmatch(r"\s*\d+\s*", value or ""))


def split_book_text_tokens(text):
    tokens = []
    word_index = 0
    position = 0
    pattern = re.compile(r"(''|[\[\]\"“”„«»]|--+|[^\s\[\]\"“”„«»-]+(?:-(?!-)[^\s\[\]\"“”„«»-]+)*|\n)")
    for match in pattern.finditer(text):
        if match.start() > position:
            gap = text[position:match.start()]
            if gap:
                tokens.append(
                    {
                        "type": "skip",
                        "value": gap,
                        "start": position,
                        "end": match.start(),
                    }
                )
        value = match.group(0)
        position = match.end()
        if value == "\n":
            tokens.append({"type": "newline", "start": match.start(), "end": match.end()})
        elif re.fullmatch(r"(''|[\[\]\"“”„«»]|--+)", value):
            tokens.append(
                {
                    "type": "skip",
                    "value": value,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
        else:
            tokens.append(
                {
                    "type": "word",
                    "value": value,
                    "wordIndex": word_index,
                    "start": match.start(),
                    "end": match.end(),
                }
            )
            word_index += 1
    if position < len(text):
        tokens.append(
            {
                "type": "skip",
                "value": text[position:],
                "start": position,
                "end": len(text),
            }
        )

    line_start = 0
    while line_start < len(tokens):
        line_end = line_start
        while line_end < len(tokens) and tokens[line_end].get("type") != "newline":
            line_end += 1
        line_value = "".join(token.get("value", "") for token in tokens[line_start:line_end])
        if is_book_number_line(line_value):
            for token in tokens[line_start:line_end]:
                token["type"] = "skip"
                token.pop("wordIndex", None)
        line_start = line_end + 1

    compacted = []
    word_index = 0
    for token in tokens:
        if token.get("type") == "word":
            token["wordIndex"] = word_index
            word_index += 1
        compacted.append(token)
    return compacted


def split_text_tokens(text):
    tokens = []
    word_index = 0
    for match in re.finditer(r"\S+|\n", text):
        value = match.group(0)
        if value == "\n":
            tokens.append({"type": "newline"})
        else:
            tokens.append(
                {
                    "type": "word",
                    "value": value,
                    "wordIndex": word_index,
                }
            )
            word_index += 1
    return tokens


def spacy_lang_code(lang):
    normalized = (lang or "EN").upper()
    return {
        "CY": "el",
        "EL": "el",
        "EN": "en",
        "RU": "ru",
    }.get(normalized, normalized.lower())


def spacy_sentences(text, lang="EN"):
    try:
        import spacy
    except ImportError:
        return [
            text[start:end].strip()
            for start, end in sentence_spans_fallback(text)
            if text[start:end].strip()
        ]

    try:
        nlp = spacy.blank(spacy_lang_code(lang))
        if "sentencizer" not in nlp.pipe_names:
            nlp.add_pipe("sentencizer")
        doc = nlp(text)
        return [sentence.text.strip() for sentence in doc.sents if sentence.text.strip()]
    except Exception:
        return [
            text[start:end].strip()
            for start, end in sentence_spans_fallback(text)
            if text[start:end].strip()
        ]


def spacy_sentence_spans(text, lang="EN"):
    try:
        import spacy
    except ImportError:
        return []

    try:
        nlp = spacy.blank(spacy_lang_code(lang))
        if "sentencizer" not in nlp.pipe_names:
            nlp.add_pipe("sentencizer")
        doc = nlp(text)
    except Exception:
        return []
    return [
        (sentence.start_char, sentence.end_char)
        for sentence in doc.sents
        if sentence.text.strip()
    ]


def sentence_spans_fallback(text):
    spans = []
    start = 0
    for match in re.finditer(r"[.!?…]+(?:[\"'”»)\]]+)?(?:\s+|$)", text):
        end = match.end()
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if start < len(text) and text[start:].strip():
        spans.append((start, len(text)))
    return spans or [(0, len(text))]


def expand_selection_to_sentence(text, start_offset, end_offset, lang="EN"):
    if not text:
        return ""
    start = max(0, min(int(start_offset or 0), len(text)))
    end = max(0, min(int(end_offset or 0), len(text)))
    if end < start:
        start, end = end, start
    if end == start:
        return ""

    spans = spacy_sentence_spans(text, lang=lang) or sentence_spans_fallback(text)
    selected = [
        (span_start, span_end)
        for span_start, span_end in spans
        if span_start < end and span_end > start
    ]
    if not selected:
        return text[start:end].strip()

    quote_start = min(span_start for span_start, _ in selected)
    quote_end = max(span_end for _, span_end in selected)
    return text[quote_start:quote_end].strip()


def normalize_book_text(text):
    return re.sub(r"\n{3,}", "\n\n", text)


def split_book_chunks(text, min_chars=400, max_chars=650, lang="EN", use_spacy=True):
    if min_chars <= 0 or max_chars <= 0:
        raise SystemExit("Book chunk sizes must be positive.")
    if min_chars > max_chars:
        raise SystemExit("--book-min-chars cannot be greater than --book-max-chars.")

    normalized = normalize_book_text(text).strip()
    if not normalized:
        return []

    sentence_spans = spacy_sentence_spans(normalized, lang=lang) if use_spacy else []
    sentence_ends = {end for _, end in (sentence_spans or sentence_spans_fallback(normalized))}
    chunks = []
    start = 0
    while start < len(normalized):
        remaining = len(normalized) - start
        if remaining <= max_chars:
            chunks.append(normalized[start:])
            break

        limit = start + max_chars
        minimum = start + min_chars
        cuts = [end for end in sentence_ends if minimum <= end <= limit]
        if cuts:
            cut = max(cuts)
        else:
            whitespace_ends = [
                start + match.end()
                for match in re.finditer(r"\s+", normalized[start:limit])
                if start + match.end() >= minimum
            ]
            cut = max(whitespace_ends, default=limit)

        chunks.append(normalized[start:cut])
        start = cut

    return [chunk.strip() for chunk in chunks if chunk.strip()]


def import_error(message, line=1, column=1, field="content", context=""):
    payload = {
        "message": str(message),
        "line": max(1, int(line or 1)),
        "column": max(1, int(column or 1)),
        "field": field,
    }
    if context:
        payload["context"] = str(context)
    return payload


def text_position(text, offset):
    safe_offset = max(0, min(len(text), int(offset or 0)))
    before = text[:safe_offset]
    return before.count("\n") + 1, safe_offset - before.rfind("\n")


def import_error_context(content, line, column, radius=56):
    lines = str(content or "").split("\n")
    if not lines:
        return ""
    line_index = max(0, min(len(lines) - 1, int(line or 1) - 1))
    text_line = lines[line_index]
    column_index = max(0, min(len(text_line), int(column or 1) - 1))
    start = max(0, column_index - radius)
    end = min(len(text_line), column_index + radius)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text_line) else ""
    return prefix + text_line[start:end] + suffix


def add_import_error_context(content, errors):
    for error in errors:
        if error.get("field", "content") != "content" or error.get("context"):
            continue
        error["context"] = import_error_context(
            content,
            error.get("line", 1),
            error.get("column", 1),
        )
    return errors


def allowed_dictionary_character(char):
    if char == " ":
        return True
    if char in "-'’‐‑‒–—":
        return True
    return char.isalpha() or unicodedata.category(char).startswith("M")


def validate_import_content(text, kind="words"):
    content = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    control_errors = []
    for offset, char in enumerate(content):
        if ord(char) < 32 and char not in {"\n", "\t"}:
            line, column = text_position(content, offset)
            control_errors.append(
                import_error(f"Unsupported control character U+{ord(char):04X}", line, column)
            )
    if control_errors:
        return content, [], add_import_error_context(content, control_errors)

    if kind == "book":
        normalized = normalize_book_text(content).strip()
        if not normalized:
            return normalized, [], [import_error("Text is required")]
        return normalized, [], []

    words = []
    errors = []
    seen = {}
    for line_number, raw_line in enumerate(content.split("\n"), start=1):
        word = re.sub(r" +", " ", raw_line.strip())
        if not word:
            continue
        invalid_columns = [
            index + 1
            for index, char in enumerate(raw_line)
            if not allowed_dictionary_character(char)
        ]
        if invalid_columns:
            for column in invalid_columns:
                char = raw_line[column - 1]
                label = f"U+{ord(char):04X}" if ord(char) < 32 else repr(char)
                errors.append(
                    import_error(
                        f"Unsupported dictionary character {label}",
                        line_number,
                        column,
                    )
                )
            continue
        if "\t" in raw_line:
            errors.append(
                import_error(
                    "Use spaces instead of tabs inside a phrase",
                    line_number,
                    raw_line.index("\t") + 1,
                )
            )
            continue
        key = word.casefold()
        if key in seen:
            errors.append(
                import_error(
                    f"Duplicate word; first entered on line {seen[key]}",
                    line_number,
                    max(1, raw_line.find(word) + 1),
                )
            )
            continue
        seen[key] = line_number
        words.append({"word": word, "line": line_number, "phrase": " " in word})

    if not words and not errors:
        errors.append(import_error("Enter at least one word"))
    return content, words, add_import_error_context(content, errors)


def user_import_word_state(session, user_id):
    normalize = lambda value: re.sub(r"\s+", " ", str(value).strip()).casefold()
    known_names = {
        normalize(name)
        for name in session.exec(
            select(KnownWord.name).where(KnownWord.user_id == str(user_id))
        ).all()
    }
    known_names.update(
        normalize(name)
        for name in session.exec(
            select(Word.name)
            .where(Word.user_id == str(user_id))
            .where(Word.learned == True)  # noqa: E712
            .where(Word.status != "relics")
        ).all()
    )
    studying_names = {
        normalize(name)
        for name in session.exec(
            select(Meaning.name).where(Meaning.user_id == str(user_id))
        ).all()
    }
    studying_names.update(
        normalize(name)
        for name in session.exec(
            select(Reminder.name).where(Reminder.user_id == str(user_id))
        ).all()
    )
    return known_names, studying_names - known_names


def preview_collection_import(user_id, payload):
    kind = "book" if str(payload.get("kind", "words")) == "book" else "words"
    lang = normalized_lang(payload.get("lang", "EN"))
    content, words, errors = validate_import_content(payload.get("content", ""), kind=kind)
    result = {
        "kind": kind,
        "lang": lang,
        "errors": errors,
        "wordCount": len(words),
        "knownCount": 0,
        "studyingCount": 0,
        "newCount": len(words),
        "phraseCount": sum(1 for item in words if item.get("phrase")),
        "chunkCount": 0,
        "chunks": [],
    }
    if errors and kind == "book":
        return result

    with Session(db.engine) as session:
        if not session.get(User, str(user_id)):
            return None
        if kind == "words":
            known_names, studying_names = user_import_word_state(session, user_id)
            input_names = [item["word"].casefold() for item in words]
            result["knownCount"] = sum(
                1 for name in input_names if name in known_names
            )
            result["studyingCount"] = sum(
                1 for name in input_names if name not in known_names and name in studying_names
            )
            result["newCount"] = (
                len(words) - result["knownCount"] - result["studyingCount"]
            )
            return result

    try:
        min_chars = int(payload.get("minChars", 400))
        max_chars = int(payload.get("maxChars", 650))
        if min_chars < 100:
            raise SystemExit("Minimum chunk size must be at least 100 characters; 150 or more is recommended.")
        chunks = split_book_chunks(
            content,
            min_chars=min_chars,
            max_chars=max_chars,
            lang=lang,
            use_spacy=False,
        )
    except (TypeError, ValueError):
        result["errors"] = [import_error("Chunk sizes must be whole numbers", field="chunkSizes")]
        return result
    except SystemExit as exc:
        result["errors"] = [import_error(str(exc), field="chunkSizes")]
        return result
    result["chunks"] = [
        {"number": index, "text": chunk, "chars": len(chunk)}
        for index, chunk in enumerate(chunks, start=1)
    ]
    result["chunkCount"] = len(chunks)
    return result


def create_collection_import(user_id, payload):
    title = str(payload.get("title", "")).strip()
    if not title:
        return {"errors": [import_error("Collection name is required", field="title")]}

    preview = preview_collection_import(user_id, payload)
    if preview is None:
        return None
    if preview["errors"]:
        return preview

    kind = preview["kind"]
    lang = preview["lang"]
    content, words, _ = validate_import_content(payload.get("content", ""), kind=kind)
    with Session(db.engine) as session:
        user = session.get(User, str(user_id))
        if not user:
            return None
        had_collections = session.exec(
            select(func.count()).select_from(Collection).where(Collection.user_id == user.id)
        ).one()
        collection_number = allocate_collection_number(session, user.id)
        collection = Collection(
            user_id=user.id,
            number=collection_number,
            title=title,
            lang=lang,
            kind=kind,
        )

        if kind == "words":
            known_names, studying_names = user_import_word_state(session, user.id)
            excluded_names = known_names | studying_names
            import_words = [
                item for item in words if item["word"].casefold() not in excluded_names
            ]
            descriptions = []
            errors = []
            skipped = []
            elru_connection = (
                readonly_elru_connection(DEFAULT_ELRU_DB_PATH)
                if lang in ELRU_LANGS
                else None
            )
            try:
                for item in import_words:
                    if item.get("phrase"):
                        skipped.append(
                            {
                                "line": item["line"],
                                "term": item["word"],
                                "reason": "Multi-word phrase; dictionary lookup deferred",
                            }
                        )
                        continue
                    try:
                        description = lookup_collection_text(
                            item["word"],
                            lang,
                            elru_connection=elru_connection,
                        )
                    except Exception as exc:
                        errors.append(
                            import_error(
                                str(exc),
                                item["line"],
                                1,
                                context=import_error_context(content, item["line"], 1),
                            )
                        )
                        continue
                    if not description:
                        skipped.append(
                            {
                                "line": item["line"],
                                "term": item["word"],
                                "reason": f"No {dictionary_name_for_lang(lang)} entry",
                            }
                        )
                        continue
                    descriptions.append((item["word"], description))
            finally:
                if elru_connection is not None:
                    elru_connection.close()
            if errors:
                return {**preview, "errors": errors}

            session.add(collection)
            session.flush()
            for item in skipped:
                add_stop_word(
                    session,
                    user.id,
                    item["term"].casefold(),
                    f"Import line {item['line']}: {item['reason']}",
                )
            for word, description in descriptions:
                session.add(
                    Word(
                        user_id=user.id,
                        collection_number=collection_number,
                        learned=False,
                        name=word,
                        description=description,
                    )
                )
            words_collection_number = None
        else:
            session.add(collection)
            session.flush()
            words_collection_number = allocate_collection_number(session, user.id)
            session.add(
                Collection(
                    user_id=user.id,
                    number=words_collection_number,
                    title=f"{title} words",
                    lang=lang,
                    kind="words",
                )
            )
            chunks = [item["text"] for item in preview["chunks"]]
            try:
                translate_ahead = max(0, int(payload.get("translateAhead", 10)))
            except (TypeError, ValueError):
                translate_ahead = 10
            session.add(
                BookContent(
                    user_id=user.id,
                    collection_number=collection_number,
                    words_collection_number=words_collection_number,
                    title=title,
                    file_name=str(payload.get("fileName", "pasted-text.txt")) or "pasted-text.txt",
                    content=content,
                    chunks_json=json.dumps(chunks, ensure_ascii=False),
                    chunk_count=len(chunks),
                    created_at=utc_now(),
                    translate_enabled=bool(payload.get("translateEnabled", False)),
                    translate_target_lang=normalize_target_lang(payload.get("targetLang")),
                    translate_ahead=translate_ahead,
                )
            )
            session.add(
                BookProgress(
                    user_id=user.id,
                    collection_number=collection_number,
                    current_chunk_index=0,
                    completed_chunks=0,
                    read_chunks_json="[]",
                    updated_at=utc_now(),
                )
            )

        if had_collections == 0:
            user.default_collection_number = collection_number
        session.commit()

    return {
        **preview,
        "collectionId": collection_number,
        "wordsCollectionId": words_collection_number,
        "title": title,
        "addedCount": len(descriptions) if kind == "words" else preview["chunkCount"],
        "excludedCount": (
            len(words) - len(import_words) if kind == "words" else 0
        ),
        "skipped": skipped if kind == "words" else [],
        "skippedCount": len(skipped) if kind == "words" else 0,
        "errors": [],
    }


def book_chunk_payload(book):
    try:
        chunks = json.loads(book.chunks_json)
    except json.JSONDecodeError:
        chunks = []
    if not isinstance(chunks, list):
        return []
    return [str(chunk) for chunk in chunks]


def is_single_normalized_word(value):
    return bool(re.fullmatch(r"[a-z]+(?:'[a-z]+)?", value or ""))


def normalize_candidate_word(value, lang="EN"):
    raw = str(value or "").strip().lower()
    cleaned = re.sub(r"^[^a-z]+|[^a-z]+$", "", raw)
    if not cleaned:
        return "", "Empty word"
    if not is_single_normalized_word(cleaned):
        return "", "Candidate is not a single normalized word"
    if (lang or "EN").upper() != "EN":
        return cleaned, ""

    try:
        from nltk.corpus import wordnet as wn
    except ImportError:
        return cleaned, ""

    try:
        forms = {
            lemma
            for pos in ("n", "v", "a", "r")
            for lemma in [wn.morphy(cleaned, pos)]
            if lemma
        }
    except LookupError:
        return cleaned, ""
    normalized = sorted(forms, key=lambda item: (len(item), item))[0] if forms else cleaned
    normalized = normalized.lower().replace("_", " ").strip()
    if not is_single_normalized_word(normalized):
        return "", "Candidate normalizes to more than one word"
    return normalized, ""


def extract_candidate_words(text, lang="EN"):
    words = set()
    for match in re.finditer(r"[A-Za-z][A-Za-z'-]*", text):
        word, error = normalize_candidate_word(match.group(0), lang=lang)
        if word and not error:
            words.add(word)
    return sorted(words)


def lookup_wordnet_description(word):
    from nltk.corpus import wordnet as wn

    results = []
    for syn in wn.synsets(word):
        results.append(
            {
                "word": word,
                "definition": syn.definition(),
                "examples": syn.examples(),
            }
        )

    results_with_examples = [item for item in results if item["examples"]]
    chosen = (results_with_examples or results)[:1]
    if not chosen:
        return None

    lines = []
    for item in chosen:
        lines.append(f"{item['word']} - {item['definition']}")
        lines.extend(item["examples"])
    return "\n".join(lines)


def lookup_wordnet_text(word, show_all=False):
    from nltk.corpus import wordnet as wn

    results = []
    for syn in wn.synsets(word):
        results.append(
            {
                "word": word,
                "definition": syn.definition(),
                "examples": syn.examples(),
            }
        )

    if not show_all:
        results_with_examples = [item for item in results if item["examples"]]
        results = (results_with_examples or results)[:1]

    lines = []
    for index, item in enumerate(results[:5]):
        if index:
            lines.append("")
        lines.append(f"{item['word']} - {item['definition']}")
        lines.extend(item["examples"])
    return "\n".join(lines)


def dictionary_name_for_lang(lang):
    if lang.upper() in ELRU_LANGS:
        return "El-Ru"
    return "WordNet"


def lookup_collection_text(word, lang, show_all=False, elru_connection=None):
    if lang.upper() in ELRU_LANGS:
        if elru_connection is not None:
            return lookup_elru_description_from_connection(elru_connection, word)
        return lookup_elru_description(word)
    return lookup_wordnet_text(word, show_all=show_all)


def parse_reminder_interval(label):
    match = re.fullmatch(r"(\d+)([mhd])", str(label).strip())
    if not match:
        return timedelta(minutes=30)

    value = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=value)
    if unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def utc_now():
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_reminder_progression(value):
    if not isinstance(value, list):
        return DEFAULT_REMINDER_PROGRESSION

    progression = [str(item).strip() for item in value if str(item).strip()]
    return progression or DEFAULT_REMINDER_PROGRESSION


def normalize_lang_list(value):
    if not isinstance(value, list):
        return []
    return [str(item).strip().upper() for item in value if str(item).strip()]


def load_user_settings():
    if not USER_SETTINGS_PATH.exists():
        return json.loads(json.dumps(DEFAULT_USER_SETTINGS))

    try:
        return json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return json.loads(json.dumps(DEFAULT_USER_SETTINGS))


def _normalize_stored_user_settings(user_id):
    settings = load_user_settings()
    users = settings.setdefault("users", {})
    user_key = str(user_id)
    stored = users.setdefault(user_key, {})
    defaults = DEFAULT_USER_SETTINGS["users"].get(
        user_key, DEFAULT_USER_SETTINGS["users"]["1"]
    )
    changed = False
    for key, value in defaults.items():
        if key not in stored:
            stored[key] = json.loads(json.dumps(value))
            changed = True

    if "selected_word_collections" not in stored or "selected_book_collections" not in stored:
        try:
            with Session(db.engine) as session:
                collections = session.exec(
                    select(Collection).where(Collection.user_id == user_key)
                ).all()
            if "selected_word_collections" not in stored:
                stored["selected_word_collections"] = sorted(
                    item.number for item in collections if item.kind == "words"
                )
                changed = True
            if "selected_book_collections" not in stored:
                stored["selected_book_collections"] = sorted(
                    item.number for item in collections if item.kind == "book"
                )
                changed = True
        except Exception:
            # Database setup may still be in progress; selection defaults are added later.
            pass

    if changed or not USER_SETTINGS_PATH.exists():
        USER_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        USER_SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return settings


def normalize_stored_user_settings(user_id):
    with USER_SETTINGS_LOCK:
        return _normalize_stored_user_settings(user_id)


def get_user_settings(user_id):
    settings = normalize_stored_user_settings(user_id)
    user_settings = settings.get("users", {}).get(str(user_id), {})
    reminder_progression = normalize_reminder_progression(
        user_settings.get(
            "reminder_progression",
            DEFAULT_USER_SETTINGS["users"]["1"]["reminder_progression"],
        )
    )
    return {
        "sprintWordCount": int(
            user_settings.get(
                "sprint_word_count",
                DEFAULT_USER_SETTINGS["users"]["1"]["sprint_word_count"],
            )
        ),
        "meaningsWordCount": int(
            user_settings.get(
                "meanings_word_count",
                DEFAULT_USER_SETTINGS["users"]["1"]["meanings_word_count"],
            )
        ),
        "reminderWordCount": int(
            user_settings.get(
                "reminder_word_count",
                DEFAULT_USER_SETTINGS["users"]["1"]["reminder_word_count"],
            )
        ),
        "globalControlPageSize": int(
            user_settings.get(
                "global_control_page_size",
                DEFAULT_USER_SETTINGS["users"]["1"]["global_control_page_size"],
            )
        ),
        "quoteTextSize": int(
            user_settings.get(
                "quote_text_size",
                DEFAULT_USER_SETTINGS["users"]["1"]["quote_text_size"],
            )
        ),
        "quotesRandom": bool(user_settings.get("quotes_random", False)),
        "selectedWordCollections": [
            int(value) for value in user_settings.get("selected_word_collections", [])
            if str(value).isdigit()
        ],
        "wordCollectionSelectionSaved": "selected_word_collections" in user_settings,
        "selectedBookCollections": [
            int(value) for value in user_settings.get("selected_book_collections", [])
            if str(value).isdigit()
        ],
        "bookCollectionSelectionSaved": "selected_book_collections" in user_settings,
        "reminderProgression": reminder_progression,
        "meaningsDisabledLangs": normalize_lang_list(
            user_settings.get(
                "meanings_disabled_langs",
                DEFAULT_USER_SETTINGS["users"]["1"].get("meanings_disabled_langs", []),
            )
        ),
        "strictMode": bool(user_settings.get("strict_mode", False)),
        "screenKeyboard": bool(user_settings.get("screen_keyboard", False)),
        "importDefaultLang": str(user_settings.get("import_default_lang", "EN")).upper(),
        "bookMinChars": int(user_settings.get("book_min_chars", 400)),
        "bookMaxChars": int(user_settings.get("book_max_chars", 650)),
        "translateBooksByDefault": bool(user_settings.get("translate_books_by_default", False)),
        "translateDefaultAhead": int(user_settings.get("translate_default_ahead", 10)),
    }


def _save_user_settings(user_id, updates):
    settings = load_user_settings()
    settings.setdefault("users", {})
    user_settings = settings["users"].setdefault(str(user_id), {})
    for key in ("strict_mode", "screen_keyboard", "translate_books_by_default"):
        if key in updates:
            user_settings[key] = bool(updates[key])
    if "translate_default_ahead" in updates:
        try:
            user_settings["translate_default_ahead"] = max(0, int(updates["translate_default_ahead"]))
        except (TypeError, ValueError):
            pass
    if "import_default_lang" in updates:
        lang = str(updates["import_default_lang"] or "EN").strip().upper()
        user_settings["import_default_lang"] = lang or "EN"
    if "quotes_random" in updates:
        user_settings["quotes_random"] = bool(updates["quotes_random"])
    for key in ("selected_word_collections", "selected_book_collections"):
        if key in updates and isinstance(updates[key], list):
            user_settings[key] = sorted(
                {int(value) for value in updates[key] if str(value).isdigit()}
            )
    for key in ("book_min_chars", "book_max_chars"):
        if key in updates:
            try:
                value = int(updates[key])
            except (TypeError, ValueError):
                continue
            if value > 0:
                user_settings[key] = value

    USER_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return get_user_settings(user_id)


def save_user_settings(user_id, updates):
    with USER_SETTINGS_LOCK:
        return _save_user_settings(user_id, updates)


def backup_database_file(reason="backup"):
    database_file = db.sqlite_path_from_url(db.SQLITE_URL)
    if not database_file or not database_file.exists():
        return None

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = database_file.with_name(
        f"{database_file.name}.backup-{reason}-{timestamp}"
    )
    shutil.copy2(database_file, backup_path)
    return backup_path


def ensure_database():
    SQLModel.metadata.create_all(db.engine)
    ensure_schema_columns()
    stamp_database_revision()
    with Session(db.engine) as session:
        migrate_seed_user_ids(session)
        ensure_user_default_collections(session)
        ensure_collection_sequences(session)
        ensure_forgotten_collections(session)
        ensure_known_words_from_learned(session)
        ensure_initial_reminders(session)
        ensure_learned_reminder_exclusive(session)
        ensure_thinking_starter_book(session)


def ensure_schema_columns():
    with db.engine.begin() as connection:
        columns = connection.execute(text('PRAGMA table_info("user")')).all()
        column_names = {column[1] for column in columns}
        if "default_collection_number" not in column_names:
            connection.execute(
                text(
                    'ALTER TABLE "user" ADD COLUMN '
                    "default_collection_number INTEGER NOT NULL DEFAULT 1"
                )
            )
        if "default_lang" not in column_names:
            connection.execute(
                text('ALTER TABLE "user" ADD COLUMN default_lang VARCHAR NOT NULL DEFAULT "EN"')
            )
        if "all_lang" not in column_names:
            connection.execute(
                text(
                    'ALTER TABLE "user" ADD COLUMN all_lang '
                    'VARCHAR NOT NULL DEFAULT "EN,RU"'
                )
            )

        collection_columns = connection.execute(
            text('PRAGMA table_info("collection")')
        ).all()
        collection_column_names = {column[1] for column in collection_columns}
        if "lang" not in collection_column_names:
            connection.execute(
                text('ALTER TABLE "collection" ADD COLUMN lang VARCHAR NOT NULL DEFAULT "EN"')
            )
        if "kind" not in collection_column_names:
            connection.execute(
                text('ALTER TABLE "collection" ADD COLUMN kind VARCHAR NOT NULL DEFAULT "words"')
            )

        book_progress_columns = connection.execute(
            text('PRAGMA table_info("book_progress")')
        ).all()
        book_progress_column_names = {column[1] for column in book_progress_columns}
        if book_progress_columns and "read_chunks_json" not in book_progress_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "book_progress" ADD COLUMN '
                    "read_chunks_json VARCHAR NOT NULL DEFAULT '[]'"
                )
            )

        book_content_columns = connection.execute(
            text('PRAGMA table_info("book_content")')
        ).all()
        book_content_column_names = {column[1] for column in book_content_columns}
        if book_content_columns and "translate_enabled" not in book_content_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "book_content" ADD COLUMN '
                    "translate_enabled BOOLEAN NOT NULL DEFAULT 0"
                )
            )
        if book_content_columns and "translate_target_lang" not in book_content_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "book_content" ADD COLUMN '
                    "translate_target_lang VARCHAR NOT NULL DEFAULT 'RU'"
                )
            )
        if book_content_columns and "translate_ahead" not in book_content_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "book_content" ADD COLUMN '
                    "translate_ahead INTEGER NOT NULL DEFAULT 10"
                )
            )

        book_quote_columns = connection.execute(
            text('PRAGMA table_info("book_quote")')
        ).all()
        book_quote_column_names = {column[1] for column in book_quote_columns}
        if book_quote_columns and "chunk_number" not in book_quote_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "book_quote" ADD COLUMN '
                    "chunk_number INTEGER NOT NULL DEFAULT 1"
                )
            )

        word_columns = connection.execute(text('PRAGMA table_info("word")')).all()
        word_column_names = {column[1] for column in word_columns}
        if "status" not in word_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "word" ADD COLUMN '
                    "status VARCHAR NOT NULL DEFAULT 'default'"
                )
            )
        if "in_sprint" not in word_column_names:
            connection.execute(
                text(
                    'ALTER TABLE "word" ADD COLUMN '
                    "in_sprint BOOLEAN NOT NULL DEFAULT 1"
                )
            )
            connection.execute(
                text("UPDATE word SET in_sprint = 0 WHERE learned = 1 OR status = 'relics'")
            )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_word_in_sprint ON word (in_sprint)")
        )


def migrate_seed_user_ids(session):
    seed_ids = (("jack_id", "1"), ("julia_id", "2"))
    changed = False
    for old_id, new_id in seed_ids:
        old_user = session.get(User, old_id)
        new_user = session.get(User, new_id)
        if not old_user or new_user:
            continue

        old_user.id = new_id
        for collection in session.exec(
            select(Collection).where(Collection.user_id == old_id)
        ).all():
            collection.user_id = new_id
        for word in session.exec(select(Word).where(Word.user_id == old_id)).all():
            word.user_id = new_id
        for meaning in session.exec(
            select(Meaning).where(Meaning.user_id == old_id)
        ).all():
            meaning.user_id = new_id
        for reminder in session.exec(
            select(Reminder).where(Reminder.user_id == old_id)
        ).all():
            reminder.user_id = new_id
        changed = True

    if changed:
        session.commit()


def ensure_user_default_collections(session):
    changed = False
    for user in session.exec(select(User)).all():
        default_collection = session.exec(
            select(Collection)
            .where(Collection.user_id == user.id)
            .where(Collection.number == user.default_collection_number)
            .where(Collection.lang == user.default_lang)
        ).first()
        if default_collection:
            continue

        first_collection = session.exec(
            select(Collection)
            .where(Collection.user_id == user.id)
            .where(Collection.lang == user.default_lang)
            .order_by(Collection.number)
        ).first()
        if not first_collection:
            first_collection = session.exec(
                select(Collection)
                .where(Collection.user_id == user.id)
                .order_by(Collection.number)
            ).first()
        if first_collection:
            user.default_collection_number = first_collection.number
            changed = True

    if changed:
        session.commit()


def ensure_collection_sequences(session):
    changed = False
    for user in session.exec(select(User)).all():
        max_number = session.exec(
            select(func.max(Collection.number)).where(Collection.user_id == user.id)
        ).one()
        desired_next = (max_number or 0) + 1
        sequence = session.get(CollectionSequence, user.id)
        if not sequence:
            session.add(CollectionSequence(user_id=user.id, next_number=desired_next))
            changed = True
        elif sequence.next_number < desired_next:
            sequence.next_number = desired_next
            changed = True
    if changed:
        session.commit()


def allocate_collection_number(session, user_id):
    sequence = session.get(CollectionSequence, user_id)
    if not sequence:
        max_number = session.exec(
            select(func.max(Collection.number)).where(Collection.user_id == user_id)
        ).one()
        sequence = CollectionSequence(user_id=user_id, next_number=(max_number or 0) + 1)
        session.add(sequence)
        session.flush()

    number = sequence.next_number
    sequence.next_number += 1
    session.flush()
    return number


def get_collection_lang(session, user_id, collection_number):
    collection = get_collection(session, user_id, collection_number)
    return collection.lang if collection else "EN"


def get_or_create_forgotten_collection(session, user_id, lang):
    normalized_lang = (lang or "EN").upper()
    collection = session.exec(
        select(Collection)
        .where(Collection.user_id == user_id)
        .where(Collection.lang == normalized_lang)
        .where(Collection.kind == "words")
        .where(Collection.title == "Forgotten")
    ).first()
    if collection:
        return collection

    collection = Collection(
        user_id=user_id,
        number=allocate_collection_number(session, user_id),
        title="Forgotten",
        lang=normalized_lang,
        kind="words",
    )
    session.add(collection)
    session.flush()
    return collection


def ensure_forgotten_collections(session):
    changed = False
    for user in session.exec(select(User)).all():
        languages = {"EN", "CY"} if GREEK_ENABLED else {"EN"}
        for lang in sorted(languages):
            before = session.exec(
                select(Collection)
                .where(Collection.user_id == user.id)
                .where(Collection.lang == lang)
                .where(Collection.kind == "words")
                .where(Collection.title == "Forgotten")
            ).first()
            if before:
                continue
            get_or_create_forgotten_collection(session, user.id, lang)
            changed = True
    if changed:
        session.commit()


def add_known_word(session, word, collection=None, cleanup_forgotten=True):
    collection = collection or get_collection(session, word.user_id, word.collection_number)
    lang = collection.lang if collection else "EN"
    normalized_name = word.name.lower()
    forgotten_collection = find_forgotten_collection(session, word.user_id, lang)
    if cleanup_forgotten and forgotten_collection:
        forgotten_words = session.exec(
            select(Word)
            .where(Word.user_id == word.user_id)
            .where(Word.collection_number == forgotten_collection.number)
            .where(Word.name == normalized_name)
        ).all()
        for forgotten_word in forgotten_words:
            delete_reminder_for_word(session, forgotten_word)
            delete_meanings_for_word(session, forgotten_word)
            session.delete(forgotten_word)

    existing = session.exec(
        select(KnownWord)
        .where(KnownWord.user_id == word.user_id)
        .where(KnownWord.lang == lang)
        .where(KnownWord.name == normalized_name)
    ).first()
    if existing:
        existing.source_collection_number = word.collection_number
        existing.source_collection_title = collection.title if collection else ""
        return existing

    known_word = KnownWord(
        user_id=word.user_id,
        lang=lang,
        name=normalized_name,
        source_collection_number=word.collection_number,
        source_collection_title=collection.title if collection else "",
        created_at=utc_now(),
    )
    session.add(known_word)
    return known_word


def remove_known_word(session, user_id, lang, name):
    for known_word in session.exec(
        select(KnownWord)
        .where(KnownWord.user_id == user_id)
        .where(KnownWord.lang == (lang or "EN").upper())
        .where(KnownWord.name == name.lower())
    ).all():
        session.delete(known_word)


def ensure_known_words_from_learned(session):
    # Backfill: every learned, non-relic word should have a known_word row. This
    # runs on every startup, so it must be cheap and idempotent — two set-based
    # queries and a diff, not a per-word existence check + upsert (which was
    # O(learned words) round-trips and dominated startup at several seconds).
    # Ongoing learned -> known sync is handled at runtime by add_known_word;
    # this only catches rows that predate that path or arrived out of band.
    rows = session.exec(
        select(
            Word.user_id,
            Word.collection_number,
            Word.name,
            Collection.title,
            Collection.lang,
        )
        .outerjoin(
            Collection,
            (Collection.user_id == Word.user_id)
            & (Collection.number == Word.collection_number),
        )
        .where(Word.learned == True)  # noqa: E712
        .where(Word.status != "relics")
    ).all()
    if not rows:
        return

    existing = {
        tuple(row)
        for row in session.exec(
            select(KnownWord.user_id, KnownWord.lang, KnownWord.name)
        ).all()
    }

    seen = set()
    added = False
    for user_id, collection_number, name, title, lang in rows:
        lang = lang or "EN"
        normalized = name.lower()
        key = (user_id, lang, normalized)
        if key in existing or key in seen:
            continue
        seen.add(key)
        session.add(
            KnownWord(
                user_id=user_id,
                lang=lang,
                name=normalized,
                source_collection_number=collection_number,
                source_collection_title=title or "",
                created_at=utc_now(),
            )
        )
        added = True
    if added:
        session.commit()


def ensure_initial_reminders(session):
    marker_name = "0002_seed_learned_reminders"
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_migration_marker "
                "(name VARCHAR(128) NOT NULL PRIMARY KEY)"
            )
        )
        exists = connection.execute(
            text("SELECT name FROM app_migration_marker WHERE name = :name"),
            {"name": marker_name},
        ).scalar_one_or_none()
        if exists:
            return

        inserted_at = utc_now() - timedelta(minutes=40)
        remind_at = inserted_at + timedelta(minutes=30)
        connection.execute(
            text(
                """
                INSERT OR IGNORE INTO reminder (
                    user_id,
                    collection_number,
                    name,
                    inserted_at,
                    remind_at,
                    progression_model,
                    progression_index,
                    progression_label
                )
                SELECT
                    user_id,
                    collection_number,
                    lower(name),
                    :inserted_at,
                    :remind_at,
                    'default',
                    0,
                    '30m'
                FROM word
                WHERE learned = 1
                """
            ),
            {"inserted_at": inserted_at, "remind_at": remind_at},
        )
        connection.execute(
            text("INSERT INTO app_migration_marker (name) VALUES (:name)"),
            {"name": marker_name},
        )


def ensure_learned_reminder_exclusive(session):
    marker_name = "0003_make_learned_and_reminder_exclusive"
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS app_migration_marker "
                "(name VARCHAR(128) NOT NULL PRIMARY KEY)"
            )
        )
        exists = connection.execute(
            text("SELECT name FROM app_migration_marker WHERE name = :name"),
            {"name": marker_name},
        ).scalar_one_or_none()
        if exists:
            return

        inserted_at = utc_now() - timedelta(minutes=40)
        remind_at = inserted_at + timedelta(minutes=30)
        connection.execute(
            text(
                """
                UPDATE reminder
                SET
                    inserted_at = :inserted_at,
                    remind_at = :remind_at,
                    progression_model = 'default',
                    progression_index = 0,
                    progression_label = '30m'
                WHERE EXISTS (
                    SELECT 1
                    FROM word
                    WHERE word.user_id = reminder.user_id
                      AND word.collection_number = reminder.collection_number
                      AND lower(word.name) = reminder.name
                      AND word.learned = 1
                )
                """
            ),
            {"inserted_at": inserted_at, "remind_at": remind_at},
        )
        connection.execute(
            text(
                """
                INSERT OR IGNORE INTO reminder (
                    user_id,
                    collection_number,
                    name,
                    inserted_at,
                    remind_at,
                    progression_model,
                    progression_index,
                    progression_label
                )
                SELECT
                    user_id,
                    collection_number,
                    lower(name),
                    :inserted_at,
                    :remind_at,
                    'default',
                    0,
                    '30m'
                FROM word
                WHERE learned = 1
                """
            ),
            {"inserted_at": inserted_at, "remind_at": remind_at},
        )
        connection.execute(text("UPDATE word SET learned = 0 WHERE learned = 1"))
        connection.execute(
            text("INSERT INTO app_migration_marker (name) VALUES (:name)"),
            {"name": marker_name},
        )


def stamp_database_revision():
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
            )
        )
        current_revision = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one_or_none()
        if not current_revision:
            connection.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
                {"revision": ALEMBIC_HEAD_REVISION},
            )


def get_users():
    with Session(db.engine) as session:
        users = session.exec(select(User).order_by(User.name)).all()
        return [
            {
                "id": user.id,
                "name": user.name,
                "default_collection_number": user.default_collection_number,
                "default_lang": user.default_lang if GREEK_ENABLED or user.default_lang not in ELRU_LANGS else "EN",
                "all_lang": [lang.strip() for lang in user.all_lang.split(",")
                             if lang.strip() and (GREEK_ENABLED or lang.strip() not in ELRU_LANGS)],
            }
            for user in users
        ]


def get_collection(session, user_id, collection_number):
    statement = (
        select(Collection)
        .where(Collection.user_id == user_id)
        .where(Collection.number == collection_number)
    )
    return session.exec(statement).first()


def word_collection_overview_stats(session, user_id, collection_number):
    total = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
    ).one()
    learned = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.learned == True)  # noqa: E712
    ).one()
    meanings = session.exec(
        select(func.count())
        .select_from(Meaning)
        .where(Meaning.user_id == user_id)
        .where(Meaning.collection_number == collection_number)
    ).one()
    reminders = session.exec(
        select(func.count())
        .select_from(Reminder)
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
    ).one()
    return {
        "totalWords": total,
        "remainingWords": max(0, total - learned),
        "learnedWords": learned,
        "studyingWords": meanings + reminders,
        "meaningsWords": meanings,
        "reminderWords": reminders,
    }


def book_collection_overview_stats(session, user_id, book):
    progress = session.exec(
        select(BookProgress)
        .where(BookProgress.user_id == user_id)
        .where(BookProgress.collection_number == book.collection_number)
    ).first()
    quotes = session.exec(
        select(BookQuote).where(BookQuote.user_id == user_id).where(
            BookQuote.collection_number == book.collection_number
        )
    ).all()
    words_stats = word_collection_overview_stats(
        session,
        user_id,
        book.words_collection_number,
    )
    pages_read = min(book.chunk_count, progress.completed_chunks if progress else 0)
    return {
        "pageCount": book.chunk_count,
        "pagesRead": pages_read,
        "progressPercent": round((pages_read / book.chunk_count) * 100, 1)
        if book.chunk_count
        else 0,
        "addedWords": words_stats["totalWords"],
        "studyingWords": words_stats["studyingWords"],
        "learnedWords": words_stats["learnedWords"],
        "quotes": len(quotes),
        "activeQuotes": sum(1 for quote in quotes if not quote.muted),
    }


def get_collections(user_id, collection_number, lang=None):
    with Session(db.engine) as session:
        user = session.get(User, user_id)
        if not user:
            return None

        books = session.exec(
            select(BookContent).where(BookContent.user_id == user_id)
        ).all()
        book_by_number = {book.collection_number: book for book in books}
        parent_book_by_words_number = {
            book.words_collection_number: book for book in books
        }
        collections = session.exec(
            select(Collection)
            .where(Collection.user_id == user_id)
            .order_by(Collection.title, Collection.lang, Collection.number)
        ).all()
        payload = []
        for collection in collections:
            if not GREEK_ENABLED and collection.lang.upper() in ELRU_LANGS:
                continue
            item = {
                "number": collection.number,
                "title": collection.title,
                "lang": collection.lang,
                "kind": collection.kind,
            }
            book = book_by_number.get(collection.number)
            if book:
                item["words_collection_number"] = book.words_collection_number
                item["stats"] = book_collection_overview_stats(session, user_id, book)
            else:
                item["stats"] = word_collection_overview_stats(
                    session,
                    user_id,
                    collection.number,
                )
            parent_book = parent_book_by_words_number.get(collection.number)
            if parent_book:
                item["parent_book_number"] = parent_book.collection_number
                item["parent_book_title"] = parent_book.title
            payload.append(item)
        return payload


def find_user_by_name(session, name):
    return session.exec(select(User).where(User.name.ilike(name))).first()


def require_user(session, name):
    user = find_user_by_name(session, name)
    if not user:
        raise SystemExit(f"User not found: {name}")
    return user


def next_user_id(session):
    ids = [user.id for user in session.exec(select(User)).all()]
    numeric_ids = [int(value) for value in ids if str(value).isdigit()]
    return str((max(numeric_ids) if numeric_ids else 0) + 1)


def require_starter_assets():
    missing = [
        str(path)
        for _, path in STARTER_WORD_LISTS
        if not path.is_file()
    ]
    missing.extend(str(path) for path, _ in STARTER_BOOKS if not path.is_file())
    if missing:
        raise SystemExit(f"Starter assets are missing: {', '.join(missing)}")


def add_starter_words(session, user_id, collection, path):
    names = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    inserted = 0
    skipped = 0
    for name in names:
        try:
            description = lookup_collection_text(name, collection.lang)
        except (ImportError, LookupError) as err:
            raise RuntimeError(
                "Starter WordNet data is unavailable; install nltk and its wordnet corpus"
            ) from err
        except Exception as err:
            print(f"{name} starter_load_error {err}", file=sys.stderr)
            skipped += 1
            continue
        if not description:
            skipped += 1
            continue
        session.add(
            Word(
                user_id=user_id,
                collection_number=collection.number,
                learned=False,
                name=name,
                description=description,
            )
        )
        inserted += 1
    return inserted, skipped


def add_starter_book(session, user_id, path=STARTER_BOOK_PATH, title=STARTER_BOOK_TITLE):
    content = normalize_book_text(path.read_text(encoding="utf-8"))
    chunks = split_book_chunks(content, lang="EN")
    book_number = allocate_collection_number(session, user_id)
    words_number = allocate_collection_number(session, user_id)
    session.add(
        Collection(
            user_id=user_id,
            number=book_number,
            title=title,
            lang="EN",
            kind="book",
        )
    )
    session.add(
        Collection(
            user_id=user_id,
            number=words_number,
            title=f"{title} words",
            lang="EN",
            kind="words",
        )
    )
    session.add(
        BookContent(
            user_id=user_id,
            collection_number=book_number,
            words_collection_number=words_number,
            title=title,
            file_name=path.name,
            content=content,
            chunks_json=json.dumps(chunks, ensure_ascii=False),
            chunk_count=len(chunks),
            created_at=utc_now(),
        )
    )
    session.add(
        BookProgress(
            user_id=user_id,
            collection_number=book_number,
            current_chunk_index=0,
            completed_chunks=0,
            read_chunks_json="[]",
            updated_at=utc_now(),
        )
    )
    return book_number


def ensure_thinking_starter_book(session):
    """Add the new starter book once to existing users, preserving imported copies."""
    marker = "starter_thinking_fast_slow_v1"
    if session.execute(
        text("SELECT name FROM app_migration_marker WHERE name = :name"),
        {"name": marker},
    ).first():
        return
    path, title = STARTER_BOOKS[1]
    for user in session.exec(select(User)).all():
        books = session.exec(select(BookContent).where(BookContent.user_id == user.id)).all()
        existing = any(
            book.file_name == path.name
            or re.sub(r"[^a-z]", "", book.title.lower()) in {
                "thinkingfastslow", "thinkingfastandslow"
            }
            for book in books
        )
        if not existing:
            add_starter_book(session, user.id, path, title)
    session.execute(
        text("INSERT INTO app_migration_marker (name) VALUES (:name)"),
        {"name": marker},
    )
    session.commit()


def add_starter_collections(session, user):
    require_starter_assets()
    session.add(CollectionSequence(user_id=user.id, next_number=1))
    session.flush()

    word_counts = []
    for title, path in STARTER_WORD_LISTS:
        collection = Collection(
            user_id=user.id,
            number=allocate_collection_number(session, user.id),
            title=title,
            lang="EN",
            kind="words",
        )
        session.add(collection)
        session.flush()
        inserted, skipped = add_starter_words(session, user.id, collection, path)
        word_counts.append((title, inserted, skipped))

    user.default_collection_number = 1
    for path, title in STARTER_BOOKS:
        add_starter_book(session, user.id, path, title)
    get_or_create_forgotten_collection(session, user.id, "EN")
    if GREEK_ENABLED:
        get_or_create_forgotten_collection(session, user.id, "CY")
    return word_counts


def create_user(name, initialize_database=True):
    if initialize_database:
        ensure_database()

    new_name = (name or "").strip()
    if not new_name:
        raise SystemExit("--create-user requires non-empty --name")

    with Session(db.engine) as session:
        if find_user_by_name(session, new_name):
            raise SystemExit(f"User already exists: {new_name}")

        user_id = next_user_id(session)
        session.add(
            User(
                id=user_id,
                name=new_name,
                default_collection_number=1,
                default_lang="EN",
                all_lang="EN,CY,RU" if GREEK_ENABLED else "EN,RU",
            )
        )
        session.flush()
        word_counts = add_starter_collections(session, session.get(User, user_id))
        session.commit()
        print(f"Created user {new_name} (id {user_id}).")
        for title, inserted, skipped in word_counts:
            print(f"Loaded {title}: {inserted} words, {skipped} skipped.")
        return user_id


def database_is_initialized():
    database_file = db.sqlite_path_from_url(db.SQLITE_URL)
    if not database_file or not database_file.is_file():
        return False
    try:
        with sqlite3.connect(database_file) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            required_tables = {"user", "collection", "word", "book_content"}
            if not required_tables.issubset(tables):
                return False
            user_columns = {
                row[1] for row in connection.execute('PRAGMA table_info("user")')
            }
            collection_columns = {
                row[1] for row in connection.execute('PRAGMA table_info("collection")')
            }
            word_columns = {
                row[1] for row in connection.execute('PRAGMA table_info("word")')
            }
            book_progress_columns = {
                row[1]
                for row in connection.execute('PRAGMA table_info("book_progress")')
            }
            book_quote_columns = {
                row[1] for row in connection.execute('PRAGMA table_info("book_quote")')
            }
            return {
                "default_collection_number",
                "default_lang",
                "all_lang",
            }.issubset(user_columns) and {"lang", "kind"}.issubset(
                collection_columns
            ) and {"status"}.issubset(word_columns) and {
                "read_chunks_json"
            }.issubset(book_progress_columns) and {
                "chunk_number"
            }.issubset(book_quote_columns)
    except sqlite3.DatabaseError:
        return False


def init_database(name=None):
    database_file = db.sqlite_path_from_url(db.SQLITE_URL)
    needs_initialization = not database_is_initialized()
    backup_path = (
        backup_database_file("init")
        if needs_initialization and database_file and database_file.exists()
        else None
    )

    ensure_database()
    with Session(db.engine) as session:
        existing_user = session.exec(select(User).order_by(User.id)).first()

    if existing_user:
        print(f"Using existing database with user {existing_user.name} (id {existing_user.id}).")
    elif name:
        create_user(name, initialize_database=False)
    else:
        print("Database initialized without users.")

    if backup_path:
        print(f"Database backup: {backup_path}")
    if database_file:
        print(f"Database ready: {database_file}")


def list_users():
    ensure_database()

    with Session(db.engine) as session:
        rows = []
        users = session.exec(select(User).order_by(User.id)).all()
        for user in users:
            collections_count = session.exec(
                select(func.count()).select_from(Collection).where(Collection.user_id == user.id)
            ).one()
            known_count = session.exec(
                select(func.count()).select_from(KnownWord).where(KnownWord.user_id == user.id)
            ).one()
            rows.append(
                (
                    user.id,
                    user.name,
                    user.default_lang,
                    user.default_collection_number,
                    collections_count,
                    known_count,
                )
            )

        print_rows(
            ("id", "name", "lang", "default_collection", "collections", "known"),
            rows,
        )


def require_user_id(session, user_id):
    user = session.get(User, str(user_id))
    if not user:
        raise SystemExit(f"User id not found: {user_id}")
    return user


def rename_user(user_id, new_name):
    ensure_database()

    renamed_name = (new_name or "").strip()
    if not renamed_name:
        raise SystemExit("--rename-user requires non-empty --name")

    with Session(db.engine) as session:
        user = require_user_id(session, user_id)
        existing = find_user_by_name(session, renamed_name)
        if existing and existing.id != user.id:
            raise SystemExit(f"User already exists: {renamed_name}")

        old_name = user.name
        user.name = renamed_name
        session.commit()
        print(f"Renamed user {old_name} -> {renamed_name}.")


def delete_user(user_id):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user_id(session, user_id)
        user_count = session.exec(select(func.count()).select_from(User)).one()
        if user_count <= 1:
            raise SystemExit("Cannot delete the last user.")

        collections_count = session.exec(
            select(func.count()).select_from(Collection).where(Collection.user_id == user.id)
        ).one()
        known_count = session.exec(
            select(func.count()).select_from(KnownWord).where(KnownWord.user_id == user.id)
        ).one()
        confirmation = input(
            f"Delete user {user.name} with {collections_count} collections "
            f"and {known_count} globally known words? Type yes to confirm: "
        )
        if confirmation != "yes":
            print("Cancelled.")
            return

        deleted_user_id = user.id
        for model in (
            Word,
            KnownWord,
            Meaning,
            StopWord,
            Reminder,
            BookContent,
            BookProgress,
            BookQuote,
            Collection,
            CollectionSequence,
        ):
            session.exec(delete(model).where(model.user_id == deleted_user_id))
        session.delete(user)
        session.commit()

    settings = load_user_settings()
    users_settings = settings.get("users", {})
    if str(deleted_user_id) in users_settings:
        users_settings.pop(str(deleted_user_id), None)
        USER_SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    print(f"Deleted user id {deleted_user_id}.")


def ensure_control_collection(session, user_id, collection_id, lang="EN", kind="words"):
    collection = get_collection(session, user_id, collection_id)
    if collection:
        return collection

    sequence = session.get(CollectionSequence, user_id)
    if sequence and sequence.next_number <= collection_id:
        sequence.next_number = collection_id + 1
    collection = Collection(user_id=user_id, number=collection_id, title="words", lang=lang, kind=kind)
    session.add(collection)
    session.flush()
    return collection


def load_words(path, user_name, collection_id, show_all=False, lang=None, debug=False):
    ensure_database()

    words = [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    started_at = time.perf_counter()
    with Session(db.engine) as session:
        user = require_user(session, user_name)

        collection = ensure_control_collection(
            session,
            user.id,
            collection_id,
            lang=(lang or user.default_lang).upper(),
        )
        if lang:
            collection.lang = lang.upper()

        dictionary_name = dictionary_name_for_lang(collection.lang)
        elru_connection = None
        if collection.lang.upper() in ELRU_LANGS:
            elru_connection = readonly_elru_connection(DEFAULT_ELRU_DB_PATH)

        if debug:
            print(
                f"debug: load_words path={path} user={user.name} "
                f"collection={collection_id} lang={collection.lang} "
                f"dictionary={dictionary_name} total={len(words)}",
                file=sys.stderr,
                flush=True,
            )

        inserted = 0
        skipped = 0
        try:
            for index, word in enumerate(words, start=1):
                word_started_at = time.perf_counter()
                if debug:
                    print(
                        f"debug: [{index}/{len(words)}] lookup start word={word}",
                        file=sys.stderr,
                        flush=True,
                    )
                try:
                    description = lookup_collection_text(
                        word,
                        collection.lang,
                        show_all=show_all,
                        elru_connection=elru_connection,
                    )
                except Exception as err:
                    print(
                        f"{word} load_error {json.dumps(str(err), ensure_ascii=False)}",
                        file=sys.stderr,
                    )
                    if debug:
                        print(
                            f"debug: [{index}/{len(words)}] lookup failed "
                            f"elapsed={time.perf_counter() - word_started_at:.3f}s",
                            file=sys.stderr,
                            flush=True,
                        )
                    skipped += 1
                    continue

                if not description:
                    print(
                        f"{word} load_error "
                        f"{json.dumps(f'no {dictionary_name} output', ensure_ascii=False)}",
                        file=sys.stderr,
                    )
                    if debug:
                        print(
                            f"debug: [{index}/{len(words)}] lookup empty "
                            f"elapsed={time.perf_counter() - word_started_at:.3f}s",
                            file=sys.stderr,
                            flush=True,
                        )
                    skipped += 1
                    continue

                if debug:
                    print(
                        f"debug: [{index}/{len(words)}] lookup ok "
                        f"chars={len(description)} "
                        f"elapsed={time.perf_counter() - word_started_at:.3f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                session.add(
                    Word(
                        user_id=user.id,
                        collection_number=collection_id,
                        learned=False,
                        name=word,
                        description=description,
                    )
                )
                inserted += 1

            if debug:
                print("debug: commit start", file=sys.stderr, flush=True)
            session.commit()
        finally:
            if elru_connection is not None:
                elru_connection.close()

        if debug:
            print(
                f"debug: commit ok inserted={inserted} skipped={skipped} "
                f"elapsed={time.perf_counter() - started_at:.3f}s",
                file=sys.stderr,
                flush=True,
            )

    return inserted, skipped


def load_book(
    path,
    user_name,
    collection_id,
    lang=None,
    min_chars=400,
    max_chars=650,
    debug=False,
):
    ensure_database()
    content = normalize_book_text(path.read_text(encoding="utf-8"))
    now = utc_now()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        book_collection = get_collection(session, user.id, collection_id)
        if not book_collection:
            raise SystemExit(f"Collection not found: {collection_id}")
        if book_collection.kind != "book":
            raise SystemExit(
                f"Collection {collection_id} is {book_collection.kind}, expected book"
            )
        if lang:
            book_collection.lang = lang.upper()
        collection_lang = book_collection.lang
        chunks = split_book_chunks(
            content,
            min_chars=min_chars,
            max_chars=max_chars,
            lang=collection_lang,
        )
        title = book_collection.title
        existing_book = get_book_content(session, user.id, collection_id)
        words_collection = None
        if existing_book:
            words_collection = get_collection(
                session,
                user.id,
                existing_book.words_collection_number,
            )
        if not words_collection:
            words_number = allocate_collection_number(session, user.id)
            words_collection = Collection(
                user_id=user.id,
                number=words_number,
                title=f"{title} words",
                lang=collection_lang,
                kind="words",
            )
            session.add(words_collection)
            session.flush()
        else:
            words_collection.lang = collection_lang

        if existing_book:
            session.delete(existing_book)
            session.flush()
        session.add(
            BookContent(
                user_id=user.id,
                collection_number=collection_id,
                words_collection_number=words_collection.number,
                title=title,
                file_name=path.name,
                content=content,
                chunks_json=json.dumps(chunks, ensure_ascii=False),
                chunk_count=len(chunks),
                created_at=now,
            )
        )
        progress = get_or_create_book_progress(session, user.id, collection_id)
        progress.current_chunk_index = 0
        progress.completed_chunks = 0
        write_book_chunks(progress, set())
        progress.updated_at = now
        words_number = words_collection.number
        session.commit()

    if debug:
        print(
            f"debug: load_book file={path} user={user_name} "
            f"book_collection={collection_id} words_collection={words_number} "
            f"chunks={len(chunks)} chars={len(content)} "
            f"min_chars={min_chars} max_chars={max_chars}",
            file=sys.stderr,
            flush=True,
        )
    print(
        f"Loaded book into collection {collection_id}: {title} "
        f"({collection_lang}), words collection {words_number}, chunks {len(chunks)}."
    )
    return collection_id, words_number, len(chunks)


def show_db_word(word, user_name, collection_id):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        row = session.exec(
            select(Word)
            .where(Word.user_id == user.id)
            .where(Word.collection_number == collection_id)
            .where(Word.name == word)
            .order_by(Word.id.desc())
        ).first()
        if not row:
            raise SystemExit(
                f"Word not found: {word} for {user.name}, collection {collection_id}"
            )

        print(row.description)
        print()
        print(f"learned: {str(row.learned).lower()}")


def create_collection(user_name, title, lang=None, kind="words"):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        collection_lang = (lang or user.default_lang).upper()
        had_collections = session.exec(
            select(func.count()).select_from(Collection).where(Collection.user_id == user.id)
        ).one()
        number = allocate_collection_number(session, user.id)
        session.add(
            Collection(
                user_id=user.id,
                number=number,
                title=title,
                lang=collection_lang,
                kind=kind,
            )
        )
        if had_collections == 0:
            user.default_collection_number = number
        session.commit()
        print(f"Created collection {number}: {title} ({collection_lang}, {kind})")
        return number


def collection_counts(session, user_id, collection_number):
    total = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
    ).one()
    learned = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.learned == True)  # noqa: E712
    ).one()
    meanings = session.exec(
        select(func.count())
        .select_from(Meaning)
        .where(Meaning.user_id == user_id)
        .where(Meaning.collection_number == collection_number)
    ).one()
    reminders = session.exec(
        select(func.count())
        .select_from(Reminder)
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
    ).one()
    return total, learned, total - learned, meanings, reminders


def print_rows(headers, rows):
    if not rows:
        print("(none)")
        return

    widths = [
        max(len(str(header)), *[len(str(row[index])) for row in rows])
        for index, header in enumerate(headers)
    ]
    print(
        "  ".join(
            f"{header:>{widths[index]}}" if index else f"{header:<{widths[index]}}"
            for index, header in enumerate(headers)
        )
    )
    for row in rows:
        print(
            "  ".join(
                f"{value:>{widths[index]}}" if index else f"{value:<{widths[index]}}"
                for index, value in enumerate(row)
            )
        )


def book_collection_stats(session, user_id, collection_number):
    book = get_book_content(session, user_id, collection_number)
    if not book:
        return 0, 0, 0, 0

    total_chars = len(book.content)
    pages_total = book.chunk_count
    avg_chars = round(total_chars / pages_total) if pages_total else 0
    progress = session.exec(
        select(BookProgress)
        .where(BookProgress.user_id == user_id)
        .where(BookProgress.collection_number == collection_number)
    ).first()
    pages_read = progress.completed_chunks if progress else 0
    return total_chars, avg_chars, pages_total, pages_read


def list_collections(user_name):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        collections = session.exec(
            select(Collection)
            .where(Collection.user_id == user.id)
            .order_by(Collection.number)
        ).all()

        word_rows = []
        book_rows = []
        for collection in collections:
            if collection.kind == "book":
                total_chars, avg_chars, pages_total, pages_read = book_collection_stats(
                    session,
                    user.id,
                    collection.number,
                )
                book_rows.append(
                    (
                        collection.title,
                        collection.number,
                        collection.lang,
                        total_chars,
                        avg_chars,
                        pages_total,
                        pages_read,
                    )
                )
            else:
                total, learned, not_learned, meanings, reminders = collection_counts(
                    session,
                    user.id,
                    collection.number,
                )
                word_rows.append(
                    (
                        collection.title,
                        collection.number,
                        collection.lang,
                        total,
                        learned,
                        not_learned,
                        meanings,
                        reminders,
                    )
                )

        print("Word collections")
        print_rows(
            ("name", "id", "lang", "total", "learned", "not_learned", "meanings", "reminders"),
            word_rows,
        )
        print()
        print("Book collections")
        print_rows(
            ("name", "id", "lang", "chars", "avg/page", "pages", "read"),
            book_rows,
        )


def delete_collection(user_name, collection_id):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        collections = session.exec(
            select(Collection)
            .where(Collection.user_id == user.id)
            .order_by(Collection.number)
        ).all()
        if len(collections) <= 1:
            raise SystemExit("Cannot delete the last collection for this user.")

        collection = get_collection(session, user.id, collection_id)
        if not collection:
            raise SystemExit(f"Collection not found: {collection_id}")

        total, learned, not_learned, _, _ = collection_counts(
            session, user.id, collection_id
        )
        confirmation = input(
            f"Delete collection {collection.number} ({collection.title}) "
            f"with {total} words ({learned} learned, {not_learned} not learned)? "
            "Type yes to confirm: "
        )
        if confirmation != "yes":
            print("Cancelled.")
            return

        if user.default_collection_number == collection_id:
            next_collection = next(
                (
                    item
                    for item in collections
                    if item.number != collection_id and item.lang == user.default_lang
                ),
                next(item for item in collections if item.number != collection_id),
            )
            user.default_collection_number = next_collection.number

        session.exec(
            delete(Word)
            .where(Word.user_id == user.id)
            .where(Word.collection_number == collection_id)
        )
        session.exec(
            delete(Meaning)
            .where(Meaning.user_id == user.id)
            .where(Meaning.collection_number == collection_id)
        )
        session.delete(collection)
        session.commit()
        print(f"Deleted collection {collection_id}.")


def remove_user_collections(user_id, collection_numbers):
    requested = {int(value) for value in collection_numbers if str(value).isdigit()}
    with Session(db.engine) as session:
        user = session.get(User, user_id)
        if not user:
            return None
        owned = {
            item.number: item for item in session.exec(
                select(Collection).where(Collection.user_id == user_id)
            ).all()
        }
        requested &= set(owned)
        expanded = set(requested)
        books = session.exec(
            select(BookContent).where(BookContent.user_id == user_id)
        ).all()
        for book in books:
            if book.collection_number in requested or book.words_collection_number in requested:
                expanded.update((book.collection_number, book.words_collection_number))
        book_numbers = {
            book.collection_number for book in books
            if book.collection_number in expanded or book.words_collection_number in expanded
        }
        if expanded:
            for model in (Word, Meaning, Reminder):
                session.exec(
                    delete(model).where(model.user_id == user_id).where(
                        model.collection_number.in_(expanded)
                    )
                )
            for model in (BookProgress, BookQuote):
                session.exec(
                    delete(model).where(model.user_id == user_id).where(
                        model.collection_number.in_(book_numbers)
                    )
                )
            session.exec(
                delete(BookContent).where(BookContent.user_id == user_id).where(
                    BookContent.collection_number.in_(book_numbers)
                )
            )
            session.exec(
                delete(Collection).where(Collection.user_id == user_id).where(
                    Collection.number.in_(expanded)
                )
            )
            remaining = session.exec(
                select(Collection).where(Collection.user_id == user_id).order_by(Collection.number)
            ).all()
            if user.default_collection_number in expanded:
                user.default_collection_number = remaining[0].number if remaining else 0
            session.commit()

    settings = load_user_settings()
    user_settings = settings.get("users", {}).get(str(user_id), {})
    changed = False
    for key in ("selected_word_collections", "selected_book_collections"):
        if key in user_settings:
            user_settings[key] = [
                number for number in user_settings[key]
                if not str(number).isdigit() or int(number) not in expanded
            ]
            changed = True
    if changed:
        USER_SETTINGS_PATH.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {"removed": sorted(expanded), "collections": get_collections(user_id, 0) or []}


def remove_user_from_web(user_id, entered_name):
    with Session(db.engine) as session:
        user = session.get(User, user_id)
        if not user:
            return None, "No such user."
        exact_other = session.exec(select(User).where(User.name == entered_name)).first()
        if entered_name != user.name:
            if exact_other and exact_other.id != user.id:
                return None, "Cannot delete another user's account."
            return None, "No such user. Enter the current user name exactly."
        deleted_user_id = user.id
        for model in (
            Word, KnownWord, Meaning, StopWord, Reminder, BookContent,
            BookProgress, BookQuote, Collection, CollectionSequence,
        ):
            session.exec(delete(model).where(model.user_id == deleted_user_id))
        session.delete(user)
        session.commit()
        next_user = session.exec(select(User).order_by(User.name)).first()

    settings = load_user_settings()
    settings.get("users", {}).pop(str(deleted_user_id), None)
    USER_SETTINGS_PATH.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"removedUserId": deleted_user_id, "nextUserId": next_user.id if next_user else None}, None


def rename_collection(user_name, collection_id, name):
    ensure_database()

    new_name = (name or "").strip()
    if not new_name:
        raise SystemExit("--rename-collection requires non-empty --name")

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        collection = get_collection(session, user.id, collection_id)
        if not collection:
            raise SystemExit(f"Collection not found: {collection_id}")
        old_name = collection.title
        collection.title = new_name
        session.commit()
        print(f"Renamed collection {collection_id}: {old_name} -> {new_name}")


def show_collection(user_name, collection_id):
    ensure_database()

    with Session(db.engine) as session:
        user = require_user(session, user_name)
        collection = get_collection(session, user.id, collection_id)
        if not collection:
            raise SystemExit(f"Collection not found: {collection_id}")

        words = session.exec(
            select(Word)
            .where(Word.user_id == user.id)
            .where(Word.collection_number == collection_id)
            .order_by(Word.name)
        ).all()

        rows = [(word.name, str(word.learned).lower()) for word in words]
        headers = ("word", "learned")
        word_width = max([len(headers[0]), *[len(row[0]) for row in rows]])
        learned_width = max([len(headers[1]), *[len(str(row[1])) for row in rows]])

        header = f"{headers[0]:<{word_width}}  {headers[1]:<{learned_width}}"
        print(header)
        print("_" * len(header))
        for word, learned in rows:
            print(f"{word:<{word_width}}  {learned:<{learned_width}}")


def has_sound(name):
    return bool(name) and find_entry(name) is not None


def get_reminder_state(session, user_id, collection_number, name):
    reminder = session.exec(
        select(Reminder)
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
        .where(Reminder.name == name.lower())
    ).first()
    if not reminder:
        return {"active": False, "index": None, "label": ""}
    progression = get_user_settings(user_id)["reminderProgression"]
    index = reminder.progression_index
    if reminder.progression_label in progression:
        index = progression.index(reminder.progression_label)
    return {
        "active": True,
        "index": index,
        "label": reminder.progression_label,
        "remindAt": reminder.remind_at.isoformat(),
    }


def serialize_word_action(word, session=None):
    reminder = (
        get_reminder_state(session, word.user_id, word.collection_number, word.name)
        if session
        else {"active": False, "index": None, "label": ""}
    )
    return {
        "id": word.id,
        "collectionNumber": word.collection_number,
        "name": word.name,
        "learned": word.learned and not reminder["active"],
        "status": word.status,
        "inSprint": (
            not word.learned
            and word.in_sprint
            and word.status != "relics"
        ),
        "reminder": reminder,
        "disabled": False,
        "hasSound": has_sound(word.name),
        "error": "",
    }


def serialize_global_forgotten_word_action(word, session=None, collection=None):
    item = serialize_word_action(word, session=session)
    item["globalKnown"] = True
    item["forgotten"] = True
    item["sourceCollectionNumber"] = word.collection_number
    item["sourceCollectionTitle"] = collection.title if collection else "Forgotten"
    item["lang"] = collection.lang if collection else get_collection_lang(
        session,
        word.user_id,
        word.collection_number,
    )
    return item


def serialize_word_action_for_collection(word, collection=None, session=None):
    collection = collection or get_collection(session, word.user_id, word.collection_number)
    if collection and collection.title == "Forgotten":
        return serialize_global_forgotten_word_action(
            word,
            session=session,
            collection=collection,
        )
    return serialize_word_action(word, session=session)


def serialize_meaning_action(meaning):
    return {
        "id": meaning.id,
        "name": meaning.name,
        "learned": meaning.learned,
    }


def serialize_candidate_action(word=None, name=None, error="", session=None):
    if word:
        return serialize_word_action(word, session=session)

    return {
        "id": None,
        "name": name,
        "learned": False,
        "status": "default",
        "inSprint": False,
        "reminder": {"active": False, "index": None, "label": ""},
        "disabled": bool(error),
        "hasSound": has_sound(name),
        "error": error,
    }


def serialize_known_word_action(known_word):
    return {
        "id": known_word.id,
        "name": known_word.name,
        "learned": True,
        "status": "default",
        "inSprint": False,
        "reminder": {"active": False, "index": None, "label": ""},
        "disabled": False,
        "hasSound": has_sound(known_word.name),
        "error": "",
        "globalKnown": True,
        "forgotten": False,
        "sourceCollectionNumber": known_word.source_collection_number,
        "sourceCollectionTitle": known_word.source_collection_title,
        "lang": known_word.lang,
    }


def known_word_names(session, user_id, lang):
    return {
        item.name.lower()
        for item in session.exec(
            select(KnownWord)
            .where(KnownWord.user_id == user_id)
            .where(KnownWord.lang == (lang or "EN").upper())
        ).all()
    }


def find_forgotten_collection(session, user_id, lang):
    return session.exec(
        select(Collection)
        .where(Collection.user_id == user_id)
        .where(Collection.lang == (lang or "EN").upper())
        .where(Collection.kind == "words")
        .where(Collection.title == "Forgotten")
    ).first()


def copy_reminder_state(session, source_word, target_word):
    delete_reminder_for_word(session, target_word)
    source_reminder = session.exec(
        select(Reminder)
        .where(Reminder.user_id == source_word.user_id)
        .where(Reminder.collection_number == source_word.collection_number)
        .where(Reminder.name == source_word.name.lower())
    ).first()
    if not source_reminder:
        return

    reminder = Reminder(
        user_id=target_word.user_id,
        collection_number=target_word.collection_number,
        name=target_word.name.lower(),
        inserted_at=source_reminder.inserted_at,
        remind_at=source_reminder.remind_at,
        progression_model=source_reminder.progression_model,
        progression_index=source_reminder.progression_index,
        progression_label=source_reminder.progression_label,
    )
    session.add(reminder)


def sync_collection_words_with_global_state(session, user_id, collection_number, names=None):
    collection = get_collection(session, user_id, collection_number)
    if not collection or collection.title == "Forgotten":
        return []

    known_names = known_word_names(session, user_id, collection.lang)
    forgotten_collection = find_forgotten_collection(session, user_id, collection.lang)
    forgotten_words = {}
    if forgotten_collection:
        forgotten_words = {
            word.name.lower(): word
            for word in session.exec(
                select(Word)
                .where(Word.user_id == user_id)
                .where(Word.collection_number == forgotten_collection.number)
            ).all()
        }

    if names is not None:
        requested = {str(name).lower() for name in names if str(name).strip()}
        known_names &= requested
        forgotten_words = {
            name: word
            for name, word in forgotten_words.items()
            if name in requested
        }

    global_names = known_names | set(forgotten_words)
    if not global_names:
        return []

    words = session.exec(
        select(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.name.in_(global_names))
    ).all()
    for word in words:
        forgotten_word = forgotten_words.get(word.name.lower())
        if forgotten_word:
            word.learned = False
            word.status = forgotten_word.status
            word.in_sprint = forgotten_word.status != "relics"
            delete_meanings_for_word(session, word)
            if forgotten_word.status == "meanings":
                delete_reminder_for_word(session, word)
                ensure_meaning_for_word(session, word)
            else:
                copy_reminder_state(session, forgotten_word, word)
            continue

        if word.name.lower() in known_names:
            word.learned = True
            word.in_sprint = False
            word.status = "default"
            delete_reminder_for_word(session, word)
            delete_meanings_for_word(session, word)
    if words:
        session.flush()
    return words


def get_or_create_word_from_wordnet(session, user_id, collection_number, name):
    collection_lang = get_collection_lang(session, user_id, collection_number)
    normalized_name, normalize_error = normalize_candidate_word(name, lang=collection_lang)
    if normalize_error:
        return None, normalize_error

    existing = session.exec(
        select(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.name == normalized_name)
    ).first()
    if existing:
        return existing, None

    description = lookup_wordnet_description(normalized_name)
    if not description:
        reason = "WordNet description not found; added to user stop list."
        add_stop_word(session, user_id, normalized_name, reason)
        return None, reason

    word = Word(
        user_id=user_id,
        collection_number=collection_number,
        learned=False,
        name=normalized_name,
        description=description,
        status="default",
    )
    session.add(word)
    session.flush()
    return word, None


def get_or_create_forgotten_word(session, user_id, lang, name):
    collection = get_or_create_forgotten_collection(session, user_id, lang)
    word, error = get_or_create_word_from_wordnet(
        session,
        user_id,
        collection.number,
        name,
    )
    return collection, word, error


def move_known_word_to_forgotten(session, known_word, mode="reminder", step_index=0):
    collection, word, error = get_or_create_forgotten_word(
        session,
        known_word.user_id,
        known_word.lang,
        known_word.name,
    )
    if error:
        return serialize_known_word_action(known_word) | {"disabled": True, "error": error}

    session.delete(known_word)
    word.learned = False
    if mode == "relics":
        set_word_relic_status(session, word, True)
    elif mode == "meanings":
        delete_reminder_for_word(session, word)
        word.status = "meanings"
        word.in_sprint = True
        ensure_meaning_for_word(session, word)
    elif mode == "reminder":
        delete_meanings_for_word(session, word)
        word.status = "default"
        word.in_sprint = True
        get_or_create_reminder(session, word, step_index)
    else:
        delete_reminder_for_word(session, word)
        delete_meanings_for_word(session, word)
        word.status = "default"
        word.in_sprint = True
    session.flush()
    return serialize_global_forgotten_word_action(word, session=session, collection=collection)


def move_forgotten_word_to_known(session, word):
    collection = get_collection(session, word.user_id, word.collection_number)
    if not collection or collection.title != "Forgotten":
        return None

    known_word = add_known_word(session, word, collection=collection, cleanup_forgotten=False)
    delete_reminder_for_word(session, word)
    delete_meanings_for_word(session, word)
    session.delete(word)
    session.flush()
    return known_word


def add_stop_word(session, user_id, name, reason):
    existing = session.exec(
        select(StopWord)
        .where(StopWord.user_id == user_id)
        .where(StopWord.name == name)
    ).first()
    if existing:
        existing.reason = reason
        return existing

    stop_word = StopWord(user_id=user_id, name=name, reason=reason)
    session.add(stop_word)
    return stop_word


def ensure_meaning_for_word(session, word):
    existing = session.exec(
        select(Meaning)
        .where(Meaning.user_id == word.user_id)
        .where(Meaning.collection_number == word.collection_number)
        .where(Meaning.name == word.name)
    ).first()
    if existing:
        return existing

    meaning = Meaning(
        user_id=word.user_id,
        collection_number=word.collection_number,
        learned=False,
        name=word.name,
        description=word.description,
        status="meanings",
    )
    session.add(meaning)
    return meaning


def delete_meanings_for_word(session, word):
    for meaning in session.exec(
        select(Meaning)
        .where(Meaning.user_id == word.user_id)
        .where(Meaning.collection_number == word.collection_number)
        .where(Meaning.name == word.name)
    ).all():
        session.delete(meaning)


def set_word_relic_status(session, word, active):
    if active:
        collection = get_collection(session, word.user_id, word.collection_number)
        if collection:
            remove_known_word(session, word.user_id, collection.lang, word.name)
        word.status = "relics"
        word.learned = False
        word.in_sprint = False
        delete_reminder_for_word(session, word)
        delete_meanings_for_word(session, word)
    elif word.status == "relics":
        word.status = "default"


def set_word_sprint_status(session, word, collection=None):
    collection = collection or get_collection(
        session,
        word.user_id,
        word.collection_number,
    )
    if collection:
        remove_known_word(session, word.user_id, collection.lang, word.name)
    word.learned = False
    if word.status != "relics":
        word.in_sprint = True


def get_or_create_reminder(session, word, step_index, now=None):
    settings = get_user_settings(word.user_id)
    progression = settings["reminderProgression"]
    safe_index = max(0, min(int(step_index), len(progression) - 1))
    label = progression[safe_index]
    now = now or utc_now()
    if word.status == "relics":
        word.status = "default"
    collection = get_collection(session, word.user_id, word.collection_number)
    if collection:
        remove_known_word(session, word.user_id, collection.lang, word.name)
    word.learned = False
    word.in_sprint = True
    reminder = session.exec(
        select(Reminder)
        .where(Reminder.user_id == word.user_id)
        .where(Reminder.collection_number == word.collection_number)
        .where(Reminder.name == word.name.lower())
    ).first()
    if not reminder:
        reminder = Reminder(
            user_id=word.user_id,
            collection_number=word.collection_number,
            name=word.name.lower(),
            inserted_at=now,
            remind_at=now + parse_reminder_interval(label),
            progression_model="default",
            progression_index=safe_index,
            progression_label=label,
        )
        session.add(reminder)
        return reminder

    reminder.inserted_at = now
    reminder.remind_at = now + parse_reminder_interval(label)
    reminder.progression_model = "default"
    reminder.progression_index = safe_index
    reminder.progression_label = label
    return reminder


def delete_reminder_for_word(session, word):
    for reminder in session.exec(
        select(Reminder)
        .where(Reminder.user_id == word.user_id)
        .where(Reminder.collection_number == word.collection_number)
        .where(Reminder.name == word.name.lower())
    ).all():
        session.delete(reminder)


def due_reminder_count(session, user_id, collection_number, now=None):
    now = now or utc_now()
    return session.exec(
        select(func.count())
        .select_from(Reminder)
        .join(
            Word,
            (Word.user_id == Reminder.user_id)
            & (Word.collection_number == Reminder.collection_number)
            & (func.lower(Word.name) == Reminder.name),
        )
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
        .where(Reminder.remind_at <= now)
        .where(Word.status != "relics")
    ).one()


def get_control_stats(session, user_id, collection_number):
    progression = get_user_settings(user_id)["reminderProgression"]
    now = utc_now()
    total_words = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
    ).one()
    learned_count = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.learned == True)  # noqa: E712
        .where(Word.status != "relics")
    ).one()
    reminder_count = session.exec(
        select(func.count())
        .select_from(Reminder)
        .join(
            Word,
            (Word.user_id == Reminder.user_id)
            & (Word.collection_number == Reminder.collection_number)
            & (func.lower(Word.name) == Reminder.name),
        )
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
        .where(Word.status != "relics")
    ).one()
    meanings_count = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.status == "meanings")
    ).one()
    relics_count = session.exec(
        select(func.count())
        .select_from(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.status == "relics")
    ).one()
    ready_to_remind = due_reminder_count(session, user_id, collection_number, now=now)

    def stat(count):
        return {
            "count": count,
            "percent": round((count / total_words) * 100, 1) if total_words else 0,
        }

    counts = {label: 0 for label in progression}
    rows = session.exec(
        select(Reminder.progression_label, func.count())
        .join(
            Word,
            (Word.user_id == Reminder.user_id)
            & (Word.collection_number == Reminder.collection_number)
            & (func.lower(Word.name) == Reminder.name),
        )
        .where(Reminder.user_id == user_id)
        .where(Reminder.collection_number == collection_number)
        .where(Word.status != "relics")
        .group_by(Reminder.progression_label)
    ).all()
    for label, count in rows:
        counts[label] = count

    relics = session.exec(
        select(Word)
        .where(Word.user_id == user_id)
        .where(Word.collection_number == collection_number)
        .where(Word.status == "relics")
        .order_by(func.lower(Word.name))
    ).all()

    return {
        "dictionary": {
            "total": stat(total_words),
            "learned": stat(learned_count),
            "reminder": stat(reminder_count),
            "meanings": stat(meanings_count),
            "relics": stat(relics_count),
        },
        "reminderStages": [
            {"label": label, "count": counts.get(label, 0)}
            for label in progression
        ],
        "readyToRemind": ready_to_remind,
        "relics": [serialize_word_action(word, session=session) for word in relics],
    }


def get_global_control_stats(session, user_id, collection_numbers):
    progression = get_user_settings(user_id)["reminderProgression"]
    combined = {
        "reminderStages": [{"label": label, "count": 0} for label in progression],
        "readyToRemind": 0,
        "relics": [],
    }
    stage_counts = {label: 0 for label in progression}
    for collection_number in collection_numbers:
        stats = get_control_stats(session, user_id, collection_number)
        combined["readyToRemind"] += stats["readyToRemind"]
        combined["relics"].extend(stats["relics"])
        for row in stats["reminderStages"]:
            stage_counts[row["label"]] = stage_counts.get(row["label"], 0) + row["count"]
    combined["reminderStages"] = [
        {"label": label, "count": stage_counts.get(label, 0)} for label in progression
    ]
    combined["relics"].sort(key=lambda item: item["name"].lower())
    return combined


def get_book_content(session, user_id, collection_number):
    return session.exec(
        select(BookContent)
        .where(BookContent.user_id == user_id)
        .where(BookContent.collection_number == collection_number)
    ).first()


def get_or_create_book_progress(session, user_id, collection_number):
    progress = session.exec(
        select(BookProgress)
        .where(BookProgress.user_id == user_id)
        .where(BookProgress.collection_number == collection_number)
    ).first()
    if progress:
        return progress

    progress = BookProgress(
        user_id=user_id,
        collection_number=collection_number,
        current_chunk_index=0,
        completed_chunks=0,
        updated_at=utc_now(),
    )
    session.add(progress)
    session.flush()
    return progress


def read_book_chunks(progress):
    try:
        values = json.loads(progress.read_chunks_json or "[]")
    except json.JSONDecodeError:
        return set()
    return {int(value) for value in values if isinstance(value, int) and value > 0}


def write_book_chunks(progress, values):
    progress.read_chunks_json = json.dumps(sorted(values), ensure_ascii=False)


def sequential_book_count(read_chunks, page_count):
    count = 0
    for page in range(1, page_count + 1):
        if page not in read_chunks:
            break
        count += 1
    return count


def get_book_control_stats(session, user_id, collection_number):
    book = get_book_content(session, user_id, collection_number)
    if not book:
        return {
            "stats": {
                "totalChars": 0,
                "pageCount": 0,
                "avgCharsPerPage": 0,
                "completedChunks": 0,
                "nextSequentialPage": 1,
            },
            "chunks": [],
            "translation": None,
        }

    chunks = book_chunk_payload(book)
    progress = get_or_create_book_progress(session, user_id, collection_number)
    page_count = len(chunks)
    total_chars = len(book.content)
    completed = sequential_book_count(read_book_chunks(progress), page_count)
    if progress.completed_chunks != completed:
        progress.completed_chunks = completed
        progress.current_chunk_index = min(completed, max(0, page_count - 1))
        progress.updated_at = utc_now()
        session.commit()
    return {
        "stats": {
            "totalChars": total_chars,
            "pageCount": page_count,
            "avgCharsPerPage": round(total_chars / page_count) if page_count else 0,
            "completedChunks": completed,
            "nextSequentialPage": min(completed + 1, page_count) if page_count else 1,
        },
        "chunks": [
            {
                "number": index + 1,
                "text": chunk,
            }
            for index, chunk in enumerate(chunks)
        ],
        "translation": book_translation_status(session, user_id, collection_number),
    }


def get_book_quote_rows(session, user_id, collection_number):
    quotes = session.exec(
        select(BookQuote)
        .where(BookQuote.user_id == user_id)
        .where(BookQuote.collection_number == collection_number)
        .order_by(BookQuote.chunk_number, BookQuote.number)
    ).all()
    return [serialize_book_quote(quote) for quote in quotes]


def get_book_payload(session, user_id, collection, page=None):
    book = get_book_content(session, user_id, collection.number)
    if not book:
        return None

    chunks = book_chunk_payload(book)
    if not chunks:
        return None

    progress = get_or_create_book_progress(session, user_id, collection.number)
    read_chunks = read_book_chunks(progress)
    completed = sequential_book_count(read_chunks, len(chunks))
    if progress.completed_chunks != completed:
        progress.completed_chunks = completed
        progress.current_chunk_index = min(completed, len(chunks) - 1)
        progress.updated_at = utc_now()
        session.commit()

    if page is None:
        index = max(0, min(completed, len(chunks) - 1))
    else:
        index = max(0, min(page - 1, len(chunks) - 1))
    return {
        "book": book,
        "text": chunks[index],
        "page": index + 1,
        "pageCount": len(chunks),
        "completedChunks": completed,
        "nextSequentialPage": min(completed + 1, len(chunks)),
        "sequential": index == completed,
        "percent": round((completed / len(chunks)) * 100, 1) if chunks else 0,
    }


def serialize_book_quote(quote):
    return {
        "id": quote.id,
        "number": quote.number,
        "chunkNumber": quote.chunk_number,
        "quote": quote.quote,
        "muted": quote.muted,
        "lang": quote.lang,
    }


def get_book_quotes_batch(session, user_id, collection_number, max_chars, random_order=False):
    safe_max = max(1, int(max_chars or 650))
    order_clause = func.random() if random_order else BookQuote.number
    quotes = session.exec(
        select(BookQuote)
        .where(BookQuote.user_id == user_id)
        .where(BookQuote.collection_number == collection_number)
        .where(BookQuote.muted == False)  # noqa: E712
        .order_by(order_clause)
    ).all()
    batch = []
    total = 0
    for quote in quotes:
        quote_len = len(quote.quote)
        if batch and total + 1 + quote_len > safe_max:
            break
        batch.append(quote)
        total += quote_len + (1 if len(batch) > 1 else 0)
        if quote_len >= safe_max:
            break
    return batch


def save_book_quote(user_id, collection_number, page, start_offset, end_offset):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        payload = get_book_payload(session, user_id, collection, page=page)
        if not payload:
            return None

        quote_text = expand_selection_to_sentence(
            payload["text"],
            start_offset,
            end_offset,
            lang=collection.lang,
        )
        if not quote_text:
            return {"quote": None}

        existing = session.exec(
            select(BookQuote)
            .where(BookQuote.user_id == user_id)
            .where(BookQuote.collection_number == collection_number)
            .where(BookQuote.quote == quote_text)
        ).first()
        if existing:
            return {"quote": serialize_book_quote(existing)}

        next_number = session.exec(
            select(func.max(BookQuote.number))
            .where(BookQuote.user_id == user_id)
            .where(BookQuote.collection_number == collection_number)
        ).one()
        quote = BookQuote(
            user_id=user_id,
            collection_number=collection_number,
            lang=collection.lang,
            number=(next_number or 0) + 1,
            chunk_number=payload["page"],
            quote=quote_text,
            muted=False,
        )
        session.add(quote)
        session.commit()
        session.refresh(quote)
        return {"quote": serialize_book_quote(quote)}


def set_book_quote_muted(quote_id, muted):
    with Session(db.engine) as session:
        quote = session.get(BookQuote, quote_id)
        if not quote:
            return None
        quote.muted = bool(muted)
        session.commit()
        session.refresh(quote)
        return serialize_book_quote(quote)


def delete_book_quote(quote_id):
    with Session(db.engine) as session:
        quote = session.get(BookQuote, quote_id)
        if not quote:
            return None
        payload = serialize_book_quote(quote)
        session.delete(quote)
        session.commit()
        return payload


def complete_book_chunk(user_id, collection_number, page):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        book = get_book_content(session, user_id, collection_number)
        if not book:
            return None
        chunks = book_chunk_payload(book)
        if not chunks:
            return None
        progress = get_or_create_book_progress(session, user_id, collection_number)
        completed_index = max(0, min(int(page or 1) - 1, len(chunks) - 1))
        read_chunks = read_book_chunks(progress)
        read_chunks.add(completed_index + 1)
        write_book_chunks(progress, read_chunks)
        progress.completed_chunks = sequential_book_count(read_chunks, len(chunks))
        progress.current_chunk_index = min(progress.completed_chunks, len(chunks) - 1)
        progress.updated_at = utc_now()
        session.commit()
        return {
            "page": progress.current_chunk_index + 1,
            "pageCount": len(chunks),
            "completedChunks": progress.completed_chunks,
            "nextSequentialPage": min(progress.completed_chunks + 1, len(chunks)),
            "sequential": completed_index + 1 <= progress.completed_chunks,
            "percent": round((progress.completed_chunks / len(chunks)) * 100, 1),
        }


def set_book_sequential_reads(user_id, collection_number, count):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        book = get_book_content(session, user_id, collection_number)
        if not book:
            return None
        chunks = book_chunk_payload(book)
        page_count = len(chunks)
        safe_count = max(0, min(int(count or 0), page_count))
        progress = get_or_create_book_progress(session, user_id, collection_number)
        write_book_chunks(progress, set(range(1, safe_count + 1)))
        progress.completed_chunks = safe_count
        progress.current_chunk_index = min(safe_count, max(0, page_count - 1))
        progress.updated_at = utc_now()
        session.commit()
        return {
            "page": min(progress.current_chunk_index + 1, page_count) if page_count else 1,
            "pageCount": page_count,
            "completedChunks": safe_count,
            "nextSequentialPage": min(safe_count + 1, page_count) if page_count else 1,
            "sequential": True,
            "percent": round((safe_count / page_count) * 100, 1) if page_count else 0,
        }


def update_book_chunk(user_id, collection_number, chunk_number, text_value):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        book = get_book_content(session, user_id, collection_number)
        if not book:
            return None
        chunks = book_chunk_payload(book)
        index = int(chunk_number or 0) - 1
        if index < 0 or index >= len(chunks):
            return None
        chunks[index] = str(text_value or "").strip()
        book.chunks_json = json.dumps(chunks, ensure_ascii=False)
        book.chunk_count = len(chunks)
        # Editing a chunk invalidates its cached translation so the worker (or
        # the next on-demand lookup) re-translates the new text.
        invalidate_chunk_translation(session, user_id, collection_number, index + 1)
        session.commit()
        stats_payload = get_book_control_stats(session, user_id, collection_number)
        return {
            "chunk": {
                "number": index + 1,
                "text": chunks[index],
            },
            "bookStats": {**stats_payload["stats"], "translation": stats_payload["translation"]},
        }


def book_translation_status(session, user_id, collection_number):
    """Progress summary for a book's offline translation (for the UI bar)."""
    book = get_book_content(session, user_id, collection_number)
    if not book:
        return None
    total = int(book.chunk_count or 0)
    target_lang = normalize_target_lang(book.translate_target_lang)
    ahead = max(0, int(book.translate_ahead or 0))
    progress = get_or_create_book_progress(session, user_id, collection_number)
    current_page = int(progress.current_chunk_index or 0) + 1
    # Everything up to (current + ahead) should be translated, 1-based inclusive.
    target_count = min(total, current_page + ahead) if total else 0
    translated = translated_chunk_numbers(session, user_id, collection_number, target_lang)
    translated_count = len(translated)
    target_translated = len({n for n in translated if 1 <= n <= target_count})
    return {
        "enabled": bool(book.translate_enabled),
        "targetLang": target_lang,
        "ahead": ahead,
        "totalChunks": total,
        "translatedChunks": translated_count,
        "targetChunks": target_count,
        "targetTranslatedChunks": target_translated,
        "currentPage": current_page,
        "supportedLangs": list(SUPPORTED_TRANSLATION_LANGS),
    }


def set_book_translation_settings(
    user_id, collection_number, enabled=None, target_lang=None, ahead=None
):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        book = get_book_content(session, user_id, collection_number)
        if not book:
            return None
        if enabled is not None:
            book.translate_enabled = bool(enabled)
        if target_lang is not None:
            book.translate_target_lang = normalize_target_lang(target_lang)
        if ahead is not None:
            try:
                book.translate_ahead = max(0, int(ahead))
            except (TypeError, ValueError):
                pass
        session.commit()
        return book_translation_status(session, user_id, collection_number)


def set_translation_default(user_id, enabled=None, ahead=None):
    """Persist the per-user translation default and apply it to every book.

    This is the master switch: flipping it on enables offline translation on all
    of the user's existing books (target Russian, with the default ahead) and
    makes new books inherit it; flipping it off disables it everywhere.
    """
    updates = {}
    if enabled is not None:
        updates["translate_books_by_default"] = bool(enabled)
    if ahead is not None:
        updates["translate_default_ahead"] = ahead
    if updates:
        save_user_settings(user_id, updates)

    settings = get_user_settings(user_id)
    eff_enabled = settings["translateBooksByDefault"]
    eff_ahead = settings["translateDefaultAhead"]

    with Session(db.engine) as session:
        books = session.exec(
            select(BookContent).where(BookContent.user_id == user_id)
        ).all()
        for book in books:
            book.translate_enabled = eff_enabled
            book.translate_target_lang = "RU"
            book.translate_ahead = eff_ahead
        session.commit()
        books_updated = len(books)

    return {
        "translateBooksByDefault": eff_enabled,
        "translateDefaultAhead": eff_ahead,
        "booksUpdated": books_updated,
    }


def enable_full_translation_all_books(user_id):
    """Turn on offline translation for every book AND translate them in full.

    The one-click "translate everything" action behind the dashboard button:
    unlike ``set_translation_default`` (which only reaches ``translate_ahead``
    pages past the reader), this sets each book's ``translate_ahead`` to its own
    chunk count, so the worker's planned range covers the whole book regardless
    of the current page. Existing cached translations are kept; only missing
    chunks are filled on the next worker ticks.
    """
    with Session(db.engine) as session:
        books = session.exec(
            select(BookContent).where(BookContent.user_id == user_id)
        ).all()
        for book in books:
            try:
                total = len(json.loads(book.chunks_json))
            except (json.JSONDecodeError, TypeError):
                total = int(book.chunk_count or 0)
            book.translate_enabled = True
            book.translate_target_lang = "RU"
            # ahead >= total guarantees the plan reaches the last chunk even from
            # page 1; keep any larger existing value.
            book.translate_ahead = max(total, int(book.translate_ahead or 0))
        session.commit()
        books_enabled = len(books)
    return {"booksEnabled": books_enabled}


def get_book_chunk_translation(user_id, collection_number, chunk_number, target_lang=None):
    """Return a chunk's translation, using the cache first and caching misses."""
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.kind != "book":
            return None
        book = get_book_content(session, user_id, collection_number)
        if not book:
            return None
        chunks = book_chunk_payload(book)
        index = int(chunk_number or 0) - 1
        if index < 0 or index >= len(chunks):
            return None
        lang = normalize_target_lang(target_lang or book.translate_target_lang)
        cached = get_cached_chunk_translation(
            session, user_id, collection_number, index + 1, lang
        )
        if cached:
            return {"translation": cached.translation, "cached": True, "targetLang": lang}
        source_text = chunks[index]

    # Translate outside the DB transaction (it can be slow), then cache it.
    translation = translate_en_to_ru_segmented(source_text)
    with Session(db.engine) as session:
        store_chunk_translation(
            session, user_id, collection_number, index + 1, lang, translation
        )
        session.commit()
    return {"translation": translation, "cached": False, "targetLang": lang}


def advance_reminder_after_run(session, word, now=None):
    now = now or utc_now()
    reminder = session.exec(
        select(Reminder)
        .where(Reminder.user_id == word.user_id)
        .where(Reminder.collection_number == word.collection_number)
        .where(Reminder.name == word.name.lower())
    ).first()
    if not reminder:
        return

    progression = get_user_settings(word.user_id)["reminderProgression"]
    next_index = min(reminder.progression_index + 1, len(progression) - 1)
    label = progression[next_index]
    reminder.inserted_at = now
    reminder.remind_at = now + parse_reminder_interval(label)
    reminder.progression_index = next_index
    reminder.progression_label = label


def build_meaning_candidates(
    session,
    user_id,
    collection_number,
    text,
    explained_names,
    include_known=False,
):
    collection_lang = get_collection_lang(session, user_id, collection_number)
    explained = {
        normalized
        for name in explained_names
        for normalized, error in [normalize_candidate_word(name, lang=collection_lang)]
        if normalized and not error
    }
    candidate_names = set(extract_candidate_words(text, lang=collection_lang))

    learned_names = {
        word.name.lower()
        for word in session.exec(
            select(Word)
            .where(Word.user_id == user_id)
            .where(Word.collection_number == collection_number)
            .where(Word.learned == True)  # noqa: E712
        ).all()
    }
    learned_names = learned_names - explained
    global_known_names = known_word_names(session, user_id, collection_lang) - explained
    relic_names = {
        word.name.lower()
        for word in session.exec(
            select(Word)
            .where(Word.user_id == user_id)
            .where(Word.collection_number == collection_number)
            .where(Word.status == "relics")
        ).all()
    }
    stop_names = {
        stop_word.name.lower()
        for stop_word in session.exec(
            select(StopWord).where(StopWord.user_id == user_id)
        ).all()
    }
    existing_words = {
        word.name.lower(): word
        for word in session.exec(
            select(Word)
            .where(Word.user_id == user_id)
            .where(Word.collection_number == collection_number)
        ).all()
    }

    rows = []
    for name in sorted(candidate_names - learned_names - global_known_names - relic_names - stop_names):
        item = serialize_candidate_action(existing_words.get(name), name=name, session=session)
        item["collectionNumber"] = collection_number
        rows.append(item)

    known_rows = []
    if include_known:
        for name in sorted((candidate_names & global_known_names) - relic_names - stop_names):
            item = serialize_candidate_action(existing_words.get(name), name=name, session=session)
            item["collectionNumber"] = collection_number
            item["knownExcluded"] = True
            item["learned"] = True
            known_rows.append(item)
        return rows, known_rows

    return rows


def get_typing_text(user_id, collection_number, mode="sprint", lang="EN", page=None, global_run=False):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection or collection.lang != lang:
            return None

        user_settings = get_user_settings(user_id)
        selected_word_numbers = user_settings["selectedWordCollections"] if global_run else [collection_number]
        selected_book_numbers = user_settings["selectedBookCollections"] if global_run else [collection_number]
        if global_run:
            if not user_settings["wordCollectionSelectionSaved"]:
                selected_word_numbers = [item.number for item in session.exec(
                    select(Collection).where(Collection.user_id == user_id)
                    .where(Collection.kind == "words").where(Collection.lang == lang)
                ).all()]
            if not user_settings["bookCollectionSelectionSaved"]:
                selected_book_numbers = [item.number for item in session.exec(
                    select(Collection).where(Collection.user_id == user_id)
                    .where(Collection.kind == "book").where(Collection.lang == lang)
                ).all()]
            selected_word_numbers = [
                number for number in selected_word_numbers
                if (selected := get_collection(session, user_id, number))
                and selected.kind == "words" and selected.lang == lang
            ]
            selected_book_numbers = [
                number for number in selected_book_numbers
                if (selected := get_collection(session, user_id, number))
                and selected.kind == "book" and selected.lang == lang
            ]
        book_stats = None
        book_chunks = []
        book_quotes = []
        known_action_items = []
        action_collection_number = collection_number
        quote_items = []
        if collection.kind == "book" and mode not in ("control", "quotes"):
            mode = "read"
        elif mode == "meanings" and lang.upper() in user_settings["meaningsDisabledLangs"]:
            mode = "sprint"
        if collection.kind != "book" and mode not in ("read", "control"):
            if sync_collection_words_with_global_state(session, user_id, collection_number):
                session.commit()
        empty_message = ""
        reminder_stats = {"now": 0, "left": 0}
        control_stats = {"dictionary": {}, "reminderStages": [], "readyToRemind": 0, "relics": []}
        if collection.kind == "book" and mode == "control":
            text = ""
            action_items = []
            book_control = get_book_control_stats(session, user_id, collection_number)
            book_stats = {**book_control["stats"], "translation": book_control["translation"]}
            book_chunks = book_control["chunks"]
            book_quotes = get_book_quote_rows(session, user_id, collection_number)
        elif collection.kind == "book" and mode == "quotes":
            book = get_book_content(session, user_id, collection_number)
            if not book:
                text = ""
                action_items = []
                empty_message = "quotes_empty"
            else:
                quote_book_numbers = selected_book_numbers if global_run else [collection_number]
                quotes = []
                action_items = []
                known_action_items = []
                for book_number in quote_book_numbers:
                    selected_book = get_book_content(session, user_id, book_number)
                    if not selected_book:
                        continue
                    action_collection_number = selected_book.words_collection_number
                    sync_collection_words_with_global_state(session, user_id, action_collection_number)
                    quotes.extend(get_book_quotes_batch(
                        session,
                        user_id,
                        book_number,
                        user_settings["quoteTextSize"],
                        random_order=user_settings["quotesRandom"],
                    ))
                if user_settings["quotesRandom"]:
                    random.shuffle(quotes)
                session.commit()
                quote_items = [serialize_book_quote(quote) for quote in quotes]
                text = "\n".join(quote.quote for quote in quotes)
                for book_number in quote_book_numbers:
                    selected_book = get_book_content(session, user_id, book_number)
                    if not selected_book:
                        continue
                    book_text = "\n".join(
                        quote.quote for quote in quotes if quote.collection_number == book_number
                    )
                    rows, known_rows = build_meaning_candidates(
                        session, user_id, selected_book.words_collection_number,
                        book_text, [], include_known=True,
                    )
                    action_items.extend(rows)
                    known_action_items.extend(known_rows)
                if not quotes:
                    empty_message = "quotes_empty"
        elif mode == "read":
            book_payload = get_book_payload(session, user_id, collection, page=page)
            if not book_payload:
                text = ""
                action_items = []
                empty_message = "book_empty"
                book_stats = {"page": 1, "pageCount": 1, "completedChunks": 0, "percent": 0}
            else:
                text = book_payload["text"]
                book_stats = {
                    "page": book_payload["page"],
                    "pageCount": book_payload["pageCount"],
                    "completedChunks": book_payload["completedChunks"],
                    "nextSequentialPage": book_payload["nextSequentialPage"],
                    "sequential": book_payload["sequential"],
                    "percent": book_payload["percent"],
                }
                action_collection_number = book_payload["book"].words_collection_number
                if sync_collection_words_with_global_state(
                    session,
                    user_id,
                    action_collection_number,
                ):
                    session.commit()
                action_items, known_action_items = build_meaning_candidates(
                    session,
                    user_id,
                    action_collection_number,
                    text,
                    [],
                    include_known=True,
                )
        elif mode == "control":
            text = ""
            action_items = []
            control_stats = get_control_stats(session, user_id, collection_number)
        elif mode == "sprint":
            sprint_collection_numbers = selected_word_numbers if global_run else [collection_number]
            for number in sprint_collection_numbers:
                sync_collection_words_with_global_state(session, user_id, number)
            session.commit()
            items = session.exec(
                select(Word)
                .where(Word.user_id == user_id)
                .where(Word.collection_number.in_(sprint_collection_numbers))
                .where(Word.learned == False)  # noqa: E712
                .where(Word.in_sprint == True)  # noqa: E712
                .where(Word.status != "relics")
                .order_by(func.random())
                .limit(user_settings["sprintWordCount"])
            ).all()
            text = " ".join(word.name for word in items)
            action_items = [serialize_word_action(word, session=session) for word in items]
        elif mode == "reminder":
            now = utc_now()
            reminder_collection_numbers = selected_word_numbers if global_run else [collection_number]
            due_total = session.exec(
                select(func.count()).select_from(Reminder)
                .join(Word, (Word.user_id == Reminder.user_id)
                      & (Word.collection_number == Reminder.collection_number)
                      & (func.lower(Word.name) == Reminder.name))
                .where(Reminder.user_id == user_id)
                .where(Reminder.collection_number.in_(reminder_collection_numbers))
                .where(Reminder.remind_at <= now)
                .where(Word.status != "relics")
            ).one() if reminder_collection_numbers else 0
            reminders = session.exec(
                select(Reminder)
                .join(
                    Word,
                    (Word.user_id == Reminder.user_id)
                    & (Word.collection_number == Reminder.collection_number)
                    & (func.lower(Word.name) == Reminder.name),
                )
                .where(Reminder.user_id == user_id)
                .where(Reminder.collection_number.in_(reminder_collection_numbers))
                .where(Reminder.remind_at <= now)
                .where(Word.status != "relics")
                .order_by(func.random())
                .limit(user_settings["reminderWordCount"])
            ).all()
            item_by_key = {}
            if reminders:
                item_by_key = {
                    (word.collection_number, word.name.lower()): word
                    for word in session.exec(
                        select(Word)
                        .where(Word.user_id == user_id)
                        .where(Word.collection_number.in_(reminder_collection_numbers))
                        .where(Word.name.in_([reminder.name for reminder in reminders]))
                    ).all()
                }
            items = [
                item_by_key[(reminder.collection_number, reminder.name)]
                for reminder in reminders
                if (reminder.collection_number, reminder.name) in item_by_key
            ]
            text = " ".join(word.name for word in items)
            action_items = [serialize_word_action(word, session=session) for word in items]
            reminder_stats = {
                "now": len(items),
                "left": max(0, due_total - len(items)),
            }
            if not items:
                empty_message = "reminder_empty"
        else:
            mode = "meanings"
            meaning_collection_numbers = selected_word_numbers if global_run else [collection_number]
            items = session.exec(
                select(Meaning)
                .where(Meaning.user_id == user_id)
                .where(Meaning.collection_number.in_(meaning_collection_numbers))
                .where(Meaning.learned == False)  # noqa: E712
                .where(
                    ~Meaning.name.in_(
                        select(Word.name)
                        .where(Word.user_id == user_id)
                        .where(Word.collection_number.in_(meaning_collection_numbers))
                        .where(Word.status == "relics")
                    )
                )
                .order_by(func.random())
                .limit(user_settings["meaningsWordCount"])
            ).all()
            text = "\n\n".join(meaning.description for meaning in items)
            action_items = []
            known_action_items = []
            for source_number in meaning_collection_numbers:
                source_items = [item for item in items if item.collection_number == source_number]
                if not source_items:
                    continue
                rows, known_rows = build_meaning_candidates(
                    session, user_id, source_number,
                    "\n\n".join(item.description for item in source_items),
                    [item.name for item in source_items], include_known=True,
                )
                action_items.extend(rows)
                known_action_items.extend(known_rows)
            empty_message = ""
            if not items:
                empty_message = (
                    "meanings_empty"
                )

        return {
            "mode": mode,
            "collection": {
                "number": collection.number,
                "title": collection.title,
                "kind": collection.kind,
            },
            "actionCollectionNumber": action_collection_number,
            "bookStats": book_stats,
            "bookChunks": book_chunks,
            "bookQuotes": book_quotes,
            "text": text,
            "emptyMessage": empty_message if mode in ("meanings", "reminder", "read", "quotes") else "",
            "reminderStats": reminder_stats,
            "controlStats": control_stats,
            "words": book_type_words(text) if mode == "read" else split_words(text),
            "tokens": split_book_text_tokens(text) if mode == "read" else split_text_tokens(text),
            "items": action_items,
            "knownItems": known_action_items,
            "quotes": quote_items,
            "settings": user_settings,
        }


def toggle_word_learned(word_id):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        if word.status == "relics":
            return serialize_word_action_for_collection(word, session=session)
        collection = get_collection(session, word.user_id, word.collection_number)
        if collection and collection.title == "Forgotten":
            known_word = move_forgotten_word_to_known(session, word)
            session.commit()
            if known_word:
                session.refresh(known_word)
                return serialize_known_word_action(known_word)
            return serialize_candidate_action(name=word.name) | {"learned": True}

        word.learned = not word.learned
        if word.learned:
            word.in_sprint = False
            word.status = "default"
            delete_reminder_for_word(session, word)
            delete_meanings_for_word(session, word)
            add_known_word(session, word, collection=collection)
        elif collection:
            word.in_sprint = True
            remove_known_word(session, word.user_id, collection.lang, word.name)
        session.commit()
        session.refresh(word)
        return serialize_word_action_for_collection(word, collection=collection, session=session)


def complete_word_reminder(word_id):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        word.learned = True
        word.in_sprint = False
        word.status = "default"
        delete_reminder_for_word(session, word)
        add_known_word(session, word)
        session.commit()
        session.refresh(word)
        return serialize_word_action_for_collection(word, session=session)


def toggle_word_meanings(word_id):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        collection = get_collection(session, word.user_id, word.collection_number)
        if collection and collection.lang.upper() in get_user_settings(word.user_id)["meaningsDisabledLangs"]:
            return serialize_word_action(word, session=session)

        if word.status == "meanings":
            word.status = "default"
            for meaning in session.exec(
                select(Meaning)
                .where(Meaning.user_id == word.user_id)
                .where(Meaning.collection_number == word.collection_number)
                .where(Meaning.name == word.name)
            ).all():
                session.delete(meaning)
        else:
            if collection:
                remove_known_word(session, word.user_id, collection.lang, word.name)
            word.learned = False
            word.in_sprint = True
            word.status = "meanings"
            existing = session.exec(
                select(Meaning)
                .where(Meaning.user_id == word.user_id)
                .where(Meaning.collection_number == word.collection_number)
                .where(Meaning.name == word.name)
            ).first()
            if not existing:
                session.add(
                    Meaning(
                        user_id=word.user_id,
                        collection_number=word.collection_number,
                        learned=False,
                        name=word.name,
                        description=word.description,
                        status="meanings",
                    )
                )

        session.commit()
        session.refresh(word)
        return serialize_word_action_for_collection(word, collection=collection, session=session)


def toggle_meaning_learned(meaning_id):
    with Session(db.engine) as session:
        meaning = session.get(Meaning, meaning_id)
        if not meaning:
            return None
        meaning.learned = not meaning.learned
        session.commit()
        session.refresh(meaning)
        return serialize_meaning_action(meaning)


def toggle_word_relics(word_id):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        set_word_relic_status(session, word, word.status != "relics")
        collection = get_collection(session, word.user_id, word.collection_number)
        session.commit()
        session.refresh(word)
        return serialize_word_action_for_collection(word, collection=collection, session=session)


def move_word_to_sprint(word_id):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        collection = get_collection(session, word.user_id, word.collection_number)
        set_word_sprint_status(session, word, collection=collection)
        session.commit()
        session.refresh(word)
        return serialize_word_action_for_collection(
            word,
            collection=collection,
            session=session,
        )


def toggle_candidate_learned(user_id, collection_number, name):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None

        word, error = get_or_create_word_from_wordnet(
            session, user_id, collection_number, name
        )
        if error:
            session.commit()
            return serialize_candidate_action(name=name, error=error)
        if word.status == "relics":
            return serialize_candidate_action(word, session=session)

        word.learned = not word.learned
        if word.learned:
            word.in_sprint = False
            word.status = "default"
            delete_reminder_for_word(session, word)
            delete_meanings_for_word(session, word)
            add_known_word(session, word, collection=collection)
        elif collection:
            word.in_sprint = True
            remove_known_word(session, user_id, collection.lang, word.name)
        session.commit()
        session.refresh(word)
        return serialize_candidate_action(word, session=session)


def move_candidate_to_sprint(user_id, collection_number, name):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None

        word, error = get_or_create_word_from_wordnet(
            session,
            user_id,
            collection_number,
            name,
        )
        if error:
            session.commit()
            return serialize_candidate_action(name=name, error=error)

        set_word_sprint_status(session, word, collection=collection)
        session.commit()
        session.refresh(word)
        return serialize_candidate_action(word, session=session)


def toggle_word_reminder(word_id, step_index):
    with Session(db.engine) as session:
        word = session.get(Word, word_id)
        if not word:
            return None
        state = get_reminder_state(session, word.user_id, word.collection_number, word.name)
        if state["active"] and state["index"] == step_index:
            delete_reminder_for_word(session, word)
        else:
            get_or_create_reminder(session, word, step_index)
        session.commit()
        session.refresh(word)
        collection = get_collection(session, word.user_id, word.collection_number)
        return serialize_word_action_for_collection(word, collection=collection, session=session)


def toggle_candidate_reminder(user_id, collection_number, name, step_index):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None

        word, error = get_or_create_word_from_wordnet(
            session, user_id, collection_number, name
        )
        if error:
            session.commit()
            return serialize_candidate_action(name=name, error=error)

        state = get_reminder_state(session, word.user_id, word.collection_number, word.name)
        if state["active"] and state["index"] == step_index:
            delete_reminder_for_word(session, word)
        else:
            remove_known_word(session, user_id, collection.lang, word.name)
            word.in_sprint = True
            get_or_create_reminder(session, word, step_index)

        session.commit()
        session.refresh(word)
        return serialize_candidate_action(word, session=session)


def complete_reminder_run(user_id, collection_number, names, word_ids=None):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None

        safe_word_ids = [int(value) for value in (word_ids or []) if str(value).isdigit()]
        normalized_names = sorted(
            {
                normalized
                for name in names
                for normalized, error in [normalize_candidate_word(name, lang=collection.lang)]
                if normalized and not error
            }
        )
        if not normalized_names and not safe_word_ids:
            return {"items": [], "reminderStats": {"now": 0, "left": 0}}

        now = utc_now()
        statement = select(Word).where(Word.user_id == user_id)
        if safe_word_ids:
            statement = statement.where(Word.id.in_(safe_word_ids))
        else:
            statement = statement.where(Word.collection_number == collection_number).where(
                Word.name.in_(normalized_names)
            )
        words = session.exec(statement).all()
        for word in words:
            advance_reminder_after_run(session, word, now=now)

        session.commit()
        for word in words:
            session.refresh(word)

        collection_numbers = sorted({word.collection_number for word in words}) or [collection_number]
        due_total = sum(
            due_reminder_count(session, user_id, number, now=now)
            for number in collection_numbers
        )
        return {
            "items": [serialize_word_action(word, session=session) for word in words],
            "reminderStats": {"now": 0, "left": due_total},
        }


def toggle_candidate_meanings(user_id, collection_number, name):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None
        if collection.lang.upper() in get_user_settings(user_id)["meaningsDisabledLangs"]:
            normalized_name, normalize_error = normalize_candidate_word(name, lang=collection.lang)
            if normalize_error:
                return serialize_candidate_action(name=name, error=normalize_error)
            word = session.exec(
                select(Word)
                .where(Word.user_id == user_id)
                .where(Word.collection_number == collection_number)
                .where(Word.name == normalized_name)
            ).first()
            return serialize_candidate_action(word, name=normalized_name, session=session)

        word, error = get_or_create_word_from_wordnet(
            session, user_id, collection_number, name
        )
        if error:
            session.commit()
            return serialize_candidate_action(name=name, error=error)

        if word.status == "meanings":
            word.status = "default"
            for meaning in session.exec(
                select(Meaning)
                .where(Meaning.user_id == word.user_id)
                .where(Meaning.collection_number == word.collection_number)
                .where(Meaning.name == word.name)
            ).all():
                session.delete(meaning)
        else:
            remove_known_word(session, user_id, collection.lang, word.name)
            word.learned = False
            word.in_sprint = True
            word.status = "meanings"
            ensure_meaning_for_word(session, word)

        session.commit()
        session.refresh(word)
        return serialize_candidate_action(word, session=session)


def toggle_candidate_relics(user_id, collection_number, name):
    with Session(db.engine) as session:
        collection = get_collection(session, user_id, collection_number)
        if not collection:
            return None

        word, error = get_or_create_word_from_wordnet(
            session, user_id, collection_number, name
        )
        if error:
            session.commit()
            return serialize_candidate_action(name=name, error=error)

        remove_known_word(session, user_id, collection.lang, word.name)
        set_word_relic_status(session, word, word.status != "relics")
        session.commit()
        session.refresh(word)
        return serialize_candidate_action(word, session=session)


def get_known_words_payload(user_id, lang, page=1, page_size=10):
    safe_page = max(1, int(page or 1))
    safe_page_size = max(1, min(int(page_size or 10), 100))
    normalized_lang = (lang or "EN").upper()
    with Session(db.engine) as session:
        if not session.get(User, user_id):
            return None
        forgotten_collection = session.exec(
            select(Collection)
            .where(Collection.user_id == user_id)
            .where(Collection.lang == normalized_lang)
            .where(Collection.kind == "words")
            .where(Collection.title == "Forgotten")
        ).first()
        known_query = select(
            KnownWord.id.label("known_id"),
            literal(None).label("word_id"),
            KnownWord.name.label("name"),
            literal(False).label("forgotten"),
        ).where(KnownWord.user_id == user_id).where(KnownWord.lang == normalized_lang)
        forgotten_collection_number = forgotten_collection.number if forgotten_collection else -1
        if forgotten_collection:
            forgotten_name_query = (
                select(Word.name)
                .where(Word.user_id == user_id)
                .where(Word.collection_number == forgotten_collection.number)
            )
            known_query = known_query.where(~KnownWord.name.in_(forgotten_name_query))
        forgotten_query = select(
            literal(None).label("known_id"),
            Word.id.label("word_id"),
            Word.name.label("name"),
            literal(True).label("forgotten"),
        ).where(Word.user_id == user_id).where(
            Word.collection_number == forgotten_collection_number
        )
        unioned = known_query.union_all(forgotten_query).subquery()
        total = session.exec(
            select(func.count()).select_from(unioned)
        ).one()
        page_count = max(1, (total + safe_page_size - 1) // safe_page_size)
        safe_page = min(safe_page, page_count)

        page_rows = session.exec(
            select(
                unioned.c.known_id,
                unioned.c.word_id,
                unioned.c.forgotten,
            )
            .order_by(func.lower(unioned.c.name), unioned.c.forgotten)
            .offset((safe_page - 1) * safe_page_size)
            .limit(safe_page_size)
        ).all()
        known_ids = [known_id for known_id, _, forgotten in page_rows if not forgotten]
        word_ids = [word_id for _, word_id, forgotten in page_rows if forgotten]
        known_by_id = {
            item.id: item
            for item in session.exec(select(KnownWord).where(KnownWord.id.in_(known_ids))).all()
        } if known_ids else {}
        forgotten_by_id = {
            item.id: item
            for item in session.exec(select(Word).where(Word.id.in_(word_ids))).all()
        } if word_ids else {}

        items = []
        for known_id, word_id, forgotten in page_rows:
            if forgotten:
                word = forgotten_by_id.get(word_id)
                if word:
                    items.append(
                        serialize_global_forgotten_word_action(
                            word,
                            session=session,
                            collection=forgotten_collection,
                        )
                    )
            else:
                known_word = known_by_id.get(known_id)
                if known_word:
                    items.append(serialize_known_word_action(known_word))

        source_query = (
            select(
                KnownWord.source_collection_number,
                KnownWord.source_collection_title,
                func.count(),
            )
            .where(KnownWord.user_id == user_id)
            .where(KnownWord.lang == normalized_lang)
        )
        if forgotten_collection:
            source_query = source_query.where(~KnownWord.name.in_(forgotten_name_query))
        source_rows = session.exec(
            source_query
            .group_by(KnownWord.source_collection_number, KnownWord.source_collection_title)
            .order_by(func.count().desc(), KnownWord.source_collection_title)
        ).all()
        forgotten_count = (
            session.exec(
                select(func.count())
                .select_from(Word)
                .where(Word.user_id == user_id)
                .where(Word.collection_number == forgotten_collection.number)
            ).one()
            if forgotten_collection
            else 0
        )
        settings = get_user_settings(user_id)
        selected_numbers = settings["selectedWordCollections"]
        if not settings["wordCollectionSelectionSaved"]:
            selected_numbers = [
                item.number for item in session.exec(
                    select(Collection)
                    .where(Collection.user_id == user_id)
                    .where(Collection.lang == normalized_lang)
                    .where(Collection.kind == "words")
                ).all()
            ]
        else:
            selected_numbers = [
                number for number in selected_numbers
                if (item := get_collection(session, user_id, number))
                and item.lang == normalized_lang and item.kind == "words"
            ]
        return {
            "lang": normalized_lang,
            "page": safe_page,
            "pageSize": safe_page_size,
            "pageCount": page_count,
            "total": total,
            "sources": [
                {
                    "number": number,
                    "title": title or f"Collection {number}",
                    "count": count,
                }
                for number, title, count in source_rows
            ] + (
                [
                    {
                        "number": forgotten_collection.number,
                        "title": forgotten_collection.title,
                        "count": forgotten_count,
                    }
                ]
                if forgotten_collection and forgotten_count
                else []
            ),
            "items": items,
            "settings": settings,
            "globalControlStats": get_global_control_stats(
                session, user_id, selected_numbers
            ),
        }


def toggle_known_word_learned(known_id):
    with Session(db.engine) as session:
        known_word = session.get(KnownWord, known_id)
        if not known_word:
            return None
        payload = move_known_word_to_forgotten(session, known_word, mode="default")
        session.commit()
        return payload


def toggle_known_word_forgotten(known_id, mode="reminder", step_index=0):
    with Session(db.engine) as session:
        known_word = session.get(KnownWord, known_id)
        if not known_word:
            return None
        payload = move_known_word_to_forgotten(
            session,
            known_word,
            mode=mode,
            step_index=step_index,
        )
        session.commit()
        return payload


def setup_argos_environment():
    configured_root = os.environ.get("DICT_ARGOS_ROOT")
    if configured_root:
        argos_root = Path(configured_root)
    else:
        data_root = DATA_DIR / "argos-translate"
        project_data_root = ROOT_DIR / "data" / "argos-translate"
        data_root_ready = any((data_root / "packages").glob("*/metadata.json"))
        project_data_root_ready = any(
            (project_data_root / "packages").glob("*/metadata.json")
        )
        argos_root = (
            data_root
            if data_root_ready
            else project_data_root
            if project_data_root_ready
            else data_root
        )
    argos_root = argos_root.expanduser().resolve()
    argos_packages = argos_root / "packages"
    argos_cache_home = argos_root / "cache"
    argos_config_home = argos_root / "config"
    argos_data_home = argos_root / "data"
    for path in (argos_packages, argos_cache_home, argos_config_home, argos_data_home):
        path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("ARGOS_PACKAGES_DIR", str(argos_packages))
    os.environ.setdefault("XDG_CACHE_HOME", str(argos_cache_home))
    os.environ.setdefault("XDG_CONFIG_HOME", str(argos_config_home))
    os.environ.setdefault("XDG_DATA_HOME", str(argos_data_home))
    os.environ.setdefault("ARGOS_CHUNK_TYPE", "SPACY")

    spacy_cache = argos_cache_home / "argos-translate" / "spacy"
    spacy_cache_marker = spacy_cache / "senter" / "model"
    if not spacy_cache_marker.exists():
        with ARGOS_ENV_LOCK:
            if not spacy_cache_marker.exists():
                import spacy

                spacy_cache.parent.mkdir(parents=True, exist_ok=True)
                spacy.blank("xx").to_disk(spacy_cache)
                spacy_cache_marker.parent.mkdir(parents=True, exist_ok=True)
                spacy_cache_marker.touch()


def has_argos_en_ru_translation():
    setup_argos_environment()
    try:
        import argostranslate.translate
    except ImportError as exc:
        raise RuntimeError("argostranslate is not installed") from exc

    for language in argostranslate.translate.get_installed_languages():
        if language.code != "en":
            continue
        return any(translation.to_lang.code == "ru" for translation in language.translations_from)
    return False


def ensure_argos_en_ru_package():
    global ARGOS_EN_RU_READY
    if ARGOS_EN_RU_READY:
        return

    setup_argos_environment()
    try:
        import argostranslate.translate
    except ImportError as exc:
        raise RuntimeError("argostranslate is not installed") from exc

    if not has_argos_en_ru_translation():
        raise RuntimeError(
            "Argos en -> ru package is not installed locally; run "
            "./dict.py control --update to install it"
        )

    ARGOS_EN_RU_READY = True


def translate_en_to_ru_with_project_venv(text):
    python_path = ROOT_DIR / ".venv" / "bin" / "python"
    if not python_path.exists() or Path(sys.executable).resolve() == python_path.resolve():
        raise RuntimeError("argostranslate is not installed")

    script = """
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
sys.path.insert(0, str(root))

from dict import translate_en_to_ru

print("ARGOS_TRANSLATION_JSON " + json.dumps({"translation": translate_en_to_ru(sys.stdin.read())}, ensure_ascii=False))
"""
    result = subprocess.run(
        [str(python_path), "-c", script, str(ROOT_DIR)],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "argostranslate is not installed").strip()
        raise RuntimeError(message)

    json_line = ""
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("ARGOS_TRANSLATION_JSON "):
            json_line = line.removeprefix("ARGOS_TRANSLATION_JSON ")
            break
    if not json_line:
        raise RuntimeError(result.stdout.strip() or "Invalid translation response")

    try:
        payload = json.loads(json_line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(json_line or "Invalid translation response") from exc
    return payload.get("translation", "")


def translate_en_to_ru(text):
    if len(text) > 650:
        raise ValueError("Maximum 650 characters")

    try:
        ensure_argos_en_ru_package()
    except RuntimeError as exc:
        if "argostranslate is not installed" not in str(exc):
            raise
        return translate_en_to_ru_with_project_venv(text)

    import argostranslate.translate

    return argostranslate.translate.translate(text, "en", "ru")


TRANSLATION_SEGMENT_LIMIT = 650


def split_for_translation(value, limit=TRANSLATION_SEGMENT_LIMIT):
    """Split text into pieces of at most ``limit`` characters.

    Book chunks may exceed the argos per-call limit (``book_max_chars`` can be
    larger than 650), so long chunks are broken on sentence boundaries first,
    then on whitespace, then hard-split as a last resort. Each returned piece is
    safe to pass to :func:`translate_en_to_ru`.
    """
    value = (value or "").strip()
    if not value:
        return []
    if len(value) <= limit:
        return [value]

    # Sentence-ish units, keeping their trailing punctuation/space.
    units = re.findall(r".+?(?:[.!?…]+[\"')\]]*\s+|\n+|$)", value, re.DOTALL) or [value]
    pieces = []
    buffer = ""
    for unit in units:
        if len(unit) > limit:
            if buffer:
                pieces.append(buffer.strip())
                buffer = ""
            pieces.extend(_hard_split(unit, limit))
            continue
        if len(buffer) + len(unit) <= limit:
            buffer += unit
        else:
            if buffer:
                pieces.append(buffer.strip())
            buffer = unit
    if buffer.strip():
        pieces.append(buffer.strip())
    return [piece for piece in pieces if piece]


def _hard_split(value, limit):
    words = value.split()
    pieces = []
    buffer = ""
    for word in words:
        candidate = f"{buffer} {word}".strip()
        if len(candidate) <= limit:
            buffer = candidate
            continue
        if buffer:
            pieces.append(buffer)
        if len(word) <= limit:
            buffer = word
        else:
            for start in range(0, len(word), limit):
                pieces.append(word[start : start + limit])
            buffer = ""
    if buffer:
        pieces.append(buffer)
    return pieces


def translate_en_to_ru_segmented(text):
    """Translate arbitrarily long English text into Russian.

    Segments text over the argos limit and joins the pieces back together so a
    single oversized book chunk can be translated in one call.
    """
    pieces = split_for_translation(text)
    if not pieces:
        return ""
    return " ".join(translate_en_to_ru(piece) for piece in pieces)


# --- Offline book-translation cache ------------------------------------------
#
# Translations are cached per (user, book collection, 1-based chunk number,
# target language) in ``book_chunk_translation``. A present row means the chunk
# is translated; the background worker (see dictapp.translation_worker) fills
# rows ahead of the reader, and editing a chunk deletes its row so it is
# re-translated. The reading-mode dropdown reads the cache first and only
# translates live (and caches the result) on a miss.

SUPPORTED_TRANSLATION_LANGS = ("RU",)


def normalize_target_lang(value):
    lang = str(value or "RU").strip().upper()
    return lang if lang in SUPPORTED_TRANSLATION_LANGS else "RU"


def get_cached_chunk_translation(session, user_id, collection_number, chunk_number, target_lang):
    return session.exec(
        select(BookChunkTranslation)
        .where(BookChunkTranslation.user_id == user_id)
        .where(BookChunkTranslation.collection_number == collection_number)
        .where(BookChunkTranslation.chunk_number == chunk_number)
        .where(BookChunkTranslation.target_lang == target_lang)
    ).first()


def store_chunk_translation(session, user_id, collection_number, chunk_number, target_lang, translation):
    existing = get_cached_chunk_translation(
        session, user_id, collection_number, chunk_number, target_lang
    )
    if existing:
        existing.translation = translation
        existing.updated_at = utc_now()
    else:
        session.add(
            BookChunkTranslation(
                user_id=user_id,
                collection_number=collection_number,
                chunk_number=chunk_number,
                target_lang=target_lang,
                translation=translation,
                updated_at=utc_now(),
            )
        )


def invalidate_chunk_translation(session, user_id, collection_number, chunk_number):
    rows = session.exec(
        select(BookChunkTranslation)
        .where(BookChunkTranslation.user_id == user_id)
        .where(BookChunkTranslation.collection_number == collection_number)
        .where(BookChunkTranslation.chunk_number == chunk_number)
    ).all()
    for row in rows:
        session.delete(row)


def translated_chunk_numbers(session, user_id, collection_number, target_lang):
    rows = session.exec(
        select(BookChunkTranslation.chunk_number)
        .where(BookChunkTranslation.user_id == user_id)
        .where(BookChunkTranslation.collection_number == collection_number)
        .where(BookChunkTranslation.target_lang == target_lang)
    ).all()
    return {int(number) for number in rows}


_COMPRESSIBLE_TYPES = (
    "text/",
    "application/json",
    "application/javascript",
    "application/xml",
    "image/svg+xml",
)
_GZIP_MIN_BYTES = 1024


def _is_compressible(content_type):
    ct = (content_type or "").lower()
    return any(ct.startswith(prefix) for prefix in _COMPRESSIBLE_TYPES)


def _client_accepts_gzip(handler):
    return "gzip" in (handler.headers.get("Accept-Encoding") or "").lower()


def write_response(handler, status, body, content_type, extra_headers=None, compress=True):
    """Send a body with optional gzip and caller-supplied headers.

    Compresses text-like payloads above a small threshold when the client
    advertises gzip support, which sharply cuts transfer size for the HTML page,
    JS bundles and JSON API responses. Binary/already-compressed types are left
    untouched. Always advertises Vary: Accept-Encoding so caches stay correct.
    """
    headers = dict(extra_headers or {})
    if (
        compress
        and _is_compressible(content_type)
        and len(body) >= _GZIP_MIN_BYTES
        and _client_accepts_gzip(handler)
    ):
        body = gzip.compress(body, 6)
        headers["Content-Encoding"] = "gzip"
    headers.setdefault("Vary", "Accept-Encoding")

    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    for name, value in headers.items():
        handler.send_header(name, str(value))
    handler.end_headers()
    try:
        handler.wfile.write(body)
    except (BrokenPipeError, ConnectionResetError):
        pass


def json_response(handler, payload, status=200):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    write_response(handler, status, body, "application/json; charset=utf-8")


def send_file(handler, path, cache_control="no-cache"):
    if not path.is_file():
        handler.send_error(404, "Not found")
        return

    content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    stat = path.stat()
    etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'

    # Cheap revalidation: unchanged files return an empty 304 instead of the body.
    if (handler.headers.get("If-None-Match") or "") == etag:
        handler.send_response(304)
        handler.send_header("ETag", etag)
        handler.send_header("Cache-Control", cache_control)
        handler.end_headers()
        return

    body = path.read_bytes()
    write_response(
        handler,
        200,
        body,
        content_type,
        extra_headers={"ETag": etag, "Cache-Control": cache_control},
    )


def send_vendor_file(handler, relative_path):
    try:
        path = (VENDOR_DIR / relative_path).resolve()
        path.relative_to(VENDOR_DIR.resolve())
    except ValueError:
        handler.send_error(404, "Not found")
        return
    # Vendored runtime assets change only on control --update; allow caching with
    # ETag revalidation so repeat loads skip re-downloading the Vue runtime.
    send_file(handler, path, cache_control="public, max-age=3600")


def binary_response(handler, body, content_type, headers=None):
    write_response(handler, 200, body, content_type, extra_headers=headers)


def index_html_with_readmes():
    sources = {
        "en": README_MD_PATH.read_text(encoding="utf-8") if README_MD_PATH.is_file() else "",
        "ru": README_RU_MD_PATH.read_text(encoding="utf-8") if README_RU_MD_PATH.is_file() else "",
    }
    encoded = json.dumps(sources, ensure_ascii=False)
    encoded = encoded.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    debug_enabled = "true" if getattr(CURRENT_SERVER_DEBUG, "enabled", False) else "false"
    return (
        INDEX_HTML.replace("__README_SOURCE_BY_LANG__", encoded)
        .replace("__SERVER_DEBUG__", debug_enabled)
        .replace("__ENABLED_LANGUAGES__", json.dumps(["EN", "CY", "RU"] if GREEK_ENABLED else ["EN", "RU"]))
    )


CURRENT_SERVER_DEBUG = threading.local()


class TypingHandler(BaseHTTPRequestHandler):
    server_version = "TypingText/0.2"

    def handle_one_request(self):
        self._request_started_at = time.perf_counter()
        CURRENT_SERVER_DEBUG.enabled = bool(getattr(self.server, "debug_enabled", False))
        try:
            super().handle_one_request()
        finally:
            CURRENT_SERVER_DEBUG.enabled = False

    def end_headers(self):
        if (
            getattr(self.server, "debug_enabled", False)
            and getattr(self, "path", "").startswith("/api/")
        ):
            elapsed_ms = (time.perf_counter() - self._request_started_at) * 1000
            self.send_header("X-Server-Timing-Ms", f"{elapsed_ms:.3f}")
            self.send_header("Server-Timing", f'app;dur={elapsed_ms:.3f};desc="Python handler"')
        super().end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/health":
            json_response(
                self,
                {
                    "status": "ok",
                    "version": APP_VERSION,
                },
            )
            return

        if parsed.path == "/":
            self.send_html(index_html_with_readmes())
            return

        if parsed.path == "/readme.pdf":
            if README_PDF_PATH.is_file():
                send_file(self, README_PDF_PATH)
            else:
                send_file(self, README_MD_PATH)
            return

        if parsed.path == "/example.jpg":
            send_file(self, ROOT_DIR / "example.jpg")
            return

        if parsed.path == "/readme.md":
            send_file(self, README_MD_PATH)
            return

        if parsed.path == "/readme.ru.md":
            send_file(self, README_RU_MD_PATH)
            return

        vendor_match = re.fullmatch(r"/vendor/(.+)", parsed.path)
        if vendor_match:
            send_vendor_file(self, unquote(vendor_match.group(1)))
            return

        if parsed.path == "/api/users":
            json_response(self, get_users())
            return

        if parsed.path == "/api/translate":
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            json_response(self, translate_word(word))
            return

        if parsed.path == "/api/thesaurus":
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            result = lookup_thesaurus(word)
            json_response(self, {"word": word, **result})
            return

        if parsed.path == "/api/translation-status":
            json_response(self, translation_status_payload())
            return

        if parsed.path == "/api/translation-test":
            ok, detail = _translate_engine_check()
            json_response(
                self,
                {
                    "ok": ok,
                    "output": detail if ok else None,
                    "error": None if ok else detail,
                },
            )
            return

        sound_match = re.fullmatch(r"/api/sounds/([^/]+)", parsed.path)
        if sound_match:
            word = unquote(sound_match.group(1)).strip()
            entry = find_entry(word)
            if entry is None:
                json_response(self, {"error": "Sound not found"}, status=404)
                return
            clip = extract_ogg_clip(entry)
            if not clip:
                json_response(self, {"error": "Cannot extract sound"}, status=404)
                return
            binary_response(
                self,
                clip["data"],
                "audio/ogg",
                headers={
                    "X-Sound-Offset-Seconds": f"{clip['offset_seconds']:.6f}",
                    "X-Sound-Duration-Seconds": f"{clip['duration_seconds']:.6f}",
                },
            )
            return

        if parsed.path == "/api/macmillan/pronunciations":
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            macmillan = lookup_macmillan(word)
            json_response(
                self,
                {
                    "word": word,
                    "found": macmillan["found"],
                    "pronunciations": macmillan["pronunciations"],
                },
            )
            return

        audio_match = re.fullmatch(r"/api/macmillan/audio/([^/]+)", parsed.path)
        if audio_match:
            filename = Path(audio_match.group(1)).name
            send_file(self, MACMILLAN_RES_DIR / filename)
            return

        collections_match = re.fullmatch(r"/api/([^/]+)(?:/(\d+))?/collections", parsed.path)
        if collections_match:
            user_id = collections_match.group(1)
            collection_number = int(collections_match.group(2) or 0)
            collections = get_collections(user_id, collection_number)
            if collections is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, collections)
            return

        settings_match = re.fullmatch(r"/api/users/([^/]+)/settings", parsed.path)
        if settings_match:
            with Session(db.engine) as session:
                if not session.get(User, settings_match.group(1)):
                    json_response(self, {"error": "User not found"}, status=404)
                    return
            json_response(self, get_user_settings(settings_match.group(1)))
            return

        text_match = re.fullmatch(r"/api/([^/]+)/(\d+)/text", parsed.path)
        if text_match:
            user_id = text_match.group(1)
            collection_number = int(text_match.group(2))
            query = parse_qs(parsed.query)
            mode = query.get("mode", ["sprint"])[0]
            lang = query.get("lang", ["EN"])[0]
            raw_page = query.get("page", [None])[0]
            global_run = query.get("global", ["0"])[0] == "1"
            try:
                page = int(raw_page) if raw_page else None
            except ValueError:
                page = None
            payload = get_typing_text(
                user_id,
                collection_number,
                mode=mode,
                lang=lang,
                page=page,
                global_run=global_run,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        known_words_match = re.fullmatch(r"/api/([^/]+)/known-words", parsed.path)
        if known_words_match:
            query = parse_qs(parsed.query)
            lang = query.get("lang", ["EN"])[0]
            settings = get_user_settings(known_words_match.group(1))
            try:
                page = int(query.get("page", ["1"])[0])
                page_size = int(
                    query.get(
                        "pageSize",
                        [str(settings["globalControlPageSize"])],
                    )[0]
                )
            except ValueError:
                page = 1
                page_size = settings["globalControlPageSize"]
            payload = get_known_words_payload(
                known_words_match.group(1),
                lang,
                page=page,
                page_size=page_size,
            )
            if payload is None:
                json_response(self, {"error": "User not found"}, status=404)
                return
            json_response(self, payload)
            return

        if re.fullmatch(r"/[^/]+(?:/(?:\d+|info|control|remove|translation))?/?", parsed.path):
            self.send_html(index_html_with_readmes())
            return

        self.send_error(404, "Not found")

    def do_POST(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/users":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
                name = str(payload.get("name", "")).strip()
            except (json.JSONDecodeError, AttributeError):
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            if not name:
                json_response(self, {"error": "User name is required"}, status=400)
                return
            try:
                user_id = create_user(name)
            except SystemExit as exc:
                json_response(self, {"error": str(exc)}, status=409)
                return
            except Exception as exc:
                json_response(self, {"error": str(exc)}, status=500)
                return
            json_response(self, {"id": user_id, "name": name}, status=201)
            return

        settings_match = re.fullmatch(r"/api/users/([^/]+)/settings", parsed.path)
        if settings_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                updates = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return

            payload = save_user_settings(settings_match.group(1), updates)
            json_response(self, payload)
            return

        remove_collections_match = re.fullmatch(
            r"/api/users/([^/]+)/collections/remove", parsed.path
        )
        if remove_collections_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                request_payload = json.loads(raw_body.decode("utf-8"))
                numbers = request_payload.get("collectionNumbers", [])
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            if not isinstance(numbers, list) or not numbers:
                json_response(self, {"error": "Select at least one collection."}, status=400)
                return
            payload = remove_user_collections(remove_collections_match.group(1), numbers)
            if payload is None:
                json_response(self, {"error": "No such user."}, status=404)
                return
            json_response(self, payload)
            return

        remove_user_match = re.fullmatch(r"/api/users/([^/]+)/remove", parsed.path)
        if remove_user_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                request_payload = json.loads(raw_body.decode("utf-8"))
                entered_name = str(request_payload.get("name", ""))
            except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            payload, error = remove_user_from_web(remove_user_match.group(1), entered_name)
            if error:
                json_response(self, {"error": error}, status=409)
                return
            json_response(self, payload)
            return

        import_match = re.fullmatch(
            r"/api/users/([^/]+)/imports/(preview|create)", parsed.path
        )
        if import_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                request_payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                json_response(
                    self,
                    {"errors": [import_error("Invalid UTF-8 JSON document")]},
                    status=400,
                )
                return
            try:
                if import_match.group(2) == "preview":
                    payload = preview_collection_import(import_match.group(1), request_payload)
                else:
                    payload = create_collection_import(import_match.group(1), request_payload)
            except Exception as exc:
                json_response(
                    self,
                    {"errors": [import_error(str(exc))]},
                    status=500,
                )
                return
            if payload is None:
                json_response(self, {"error": "User not found"}, status=404)
                return
            status = 201 if import_match.group(2) == "create" and payload.get("collectionId") else 200
            json_response(self, payload, status=status)
            return

        known_word_learned_match = re.fullmatch(r"/api/known-words/(\d+)/learned", parsed.path)
        if known_word_learned_match:
            payload = toggle_known_word_learned(int(known_word_learned_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Known word not found"}, status=404)
                return
            json_response(self, payload)
            return

        known_word_forgotten_match = re.fullmatch(r"/api/known-words/(\d+)/forgotten", parsed.path)
        if known_word_forgotten_match:
            query = parse_qs(parsed.query)
            mode = query.get("mode", ["reminder"])[0]
            try:
                step = int(query.get("step", ["0"])[0])
            except ValueError:
                step = 0
            payload = toggle_known_word_forgotten(
                int(known_word_forgotten_match.group(1)),
                mode=mode,
                step_index=step,
            )
            if payload is None:
                json_response(self, {"error": "Known word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_learned_match = re.fullmatch(r"/api/words/(\d+)/learned", parsed.path)
        if word_learned_match:
            payload = toggle_word_learned(int(word_learned_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_reminder_learned_match = re.fullmatch(
            r"/api/words/(\d+)/reminder-learned", parsed.path
        )
        if word_reminder_learned_match:
            payload = complete_word_reminder(int(word_reminder_learned_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_meanings_match = re.fullmatch(r"/api/words/(\d+)/meanings", parsed.path)
        if word_meanings_match:
            payload = toggle_word_meanings(int(word_meanings_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_relics_match = re.fullmatch(r"/api/words/(\d+)/relics", parsed.path)
        if word_relics_match:
            payload = toggle_word_relics(int(word_relics_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_sprint_match = re.fullmatch(r"/api/words/(\d+)/sprint", parsed.path)
        if word_sprint_match:
            payload = move_word_to_sprint(int(word_sprint_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        word_reminder_match = re.fullmatch(r"/api/words/(\d+)/reminder", parsed.path)
        if word_reminder_match:
            query = parse_qs(parsed.query)
            step = int(query.get("step", ["0"])[0])
            payload = toggle_word_reminder(int(word_reminder_match.group(1)), step)
            if payload is None:
                json_response(self, {"error": "Word not found"}, status=404)
                return
            json_response(self, payload)
            return

        meaning_learned_match = re.fullmatch(
            r"/api/meanings/(\d+)/learned", parsed.path
        )
        if meaning_learned_match:
            payload = toggle_meaning_learned(int(meaning_learned_match.group(1)))
            if payload is None:
                json_response(self, {"error": "Meaning not found"}, status=404)
                return
            json_response(self, payload)
            return

        candidate_learned_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/candidates/learned", parsed.path
        )
        if candidate_learned_match:
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            payload = toggle_candidate_learned(
                candidate_learned_match.group(1),
                int(candidate_learned_match.group(2)),
                word,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        candidate_meanings_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/candidates/meanings", parsed.path
        )
        if candidate_meanings_match:
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            payload = toggle_candidate_meanings(
                candidate_meanings_match.group(1),
                int(candidate_meanings_match.group(2)),
                word,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        candidate_relics_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/candidates/relics", parsed.path
        )
        if candidate_relics_match:
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            payload = toggle_candidate_relics(
                candidate_relics_match.group(1),
                int(candidate_relics_match.group(2)),
                word,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        candidate_sprint_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/candidates/sprint", parsed.path
        )
        if candidate_sprint_match:
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            payload = move_candidate_to_sprint(
                candidate_sprint_match.group(1),
                int(candidate_sprint_match.group(2)),
                word,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        candidate_reminder_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/candidates/reminder", parsed.path
        )
        if candidate_reminder_match:
            query = parse_qs(parsed.query)
            word = query.get("word", [""])[0].strip()
            step = int(query.get("step", ["0"])[0])
            if not word:
                json_response(self, {"error": "Word is required"}, status=400)
                return
            payload = toggle_candidate_reminder(
                candidate_reminder_match.group(1),
                int(candidate_reminder_match.group(2)),
                word,
                step,
            )
            if payload is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, payload)
            return

        complete_reminders_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/reminders/complete", parsed.path
        )
        if complete_reminders_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = complete_reminder_run(
                complete_reminders_match.group(1),
                int(complete_reminders_match.group(2)),
                payload.get("words", []),
                payload.get("wordIds", []),
            )
            if result is None:
                json_response(self, {"error": "Collection not found"}, status=404)
                return
            json_response(self, result)
            return

        if parsed.path == "/api/book-translation":
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return

            text = str(payload.get("text", ""))
            try:
                translation = translate_en_to_ru(text)
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, status=400)
                return
            except Exception as exc:
                json_response(self, {"error": str(exc)}, status=500)
                return

            json_response(self, {"translation": translation})
            return

        book_quotes_match = re.fullmatch(r"/api/([^/]+)/(\d+)/book-quotes", parsed.path)
        if book_quotes_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = save_book_quote(
                book_quotes_match.group(1),
                int(book_quotes_match.group(2)),
                payload.get("page", 1),
                payload.get("startOffset", 0),
                payload.get("endOffset", 0),
            )
            if result is None:
                json_response(self, {"error": "Book not found"}, status=404)
                return
            json_response(self, result)
            return

        quote_mute_match = re.fullmatch(r"/api/book-quotes/(\d+)/mute", parsed.path)
        if quote_mute_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = set_book_quote_muted(
                int(quote_mute_match.group(1)),
                payload.get("muted", False),
            )
            if result is None:
                json_response(self, {"error": "Quote not found"}, status=404)
                return
            json_response(self, result)
            return

        quote_delete_match = re.fullmatch(r"/api/book-quotes/(\d+)/delete", parsed.path)
        if quote_delete_match:
            result = delete_book_quote(int(quote_delete_match.group(1)))
            if result is None:
                json_response(self, {"error": "Quote not found"}, status=404)
                return
            json_response(self, result)
            return

        book_chunk_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/book-chunks/(\d+)", parsed.path
        )
        if book_chunk_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = update_book_chunk(
                book_chunk_match.group(1),
                int(book_chunk_match.group(2)),
                int(book_chunk_match.group(3)),
                payload.get("text", ""),
            )
            if result is None:
                json_response(self, {"error": "Book chunk not found"}, status=404)
                return
            json_response(self, result)
            return

        set_book_progress_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/book-progress/set", parsed.path
        )
        if set_book_progress_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = set_book_sequential_reads(
                set_book_progress_match.group(1),
                int(set_book_progress_match.group(2)),
                payload.get("completedChunks", 0),
            )
            if result is None:
                json_response(self, {"error": "Book not found"}, status=404)
                return
            json_response(self, {"bookStats": result})
            return

        book_progress_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/book-progress", parsed.path
        )
        if book_progress_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = complete_book_chunk(
                book_progress_match.group(1),
                int(book_progress_match.group(2)),
                payload.get("page", 1),
            )
            if result is None:
                json_response(self, {"error": "Book not found"}, status=404)
                return
            json_response(self, {"bookStats": result})
            return

        book_translation_default_match = re.fullmatch(
            r"/api/([^/]+)/book-translation/default", parsed.path
        )
        if book_translation_default_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = set_translation_default(
                book_translation_default_match.group(1),
                enabled=payload.get("enabled"),
                ahead=payload.get("ahead"),
            )
            json_response(self, result)
            return

        book_translation_enable_all_match = re.fullmatch(
            r"/api/([^/]+)/book-translation/enable-all", parsed.path
        )
        if book_translation_enable_all_match:
            result = enable_full_translation_all_books(
                book_translation_enable_all_match.group(1)
            )
            json_response(self, result)
            return

        book_translation_settings_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/book-translation/settings", parsed.path
        )
        if book_translation_settings_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            result = set_book_translation_settings(
                book_translation_settings_match.group(1),
                int(book_translation_settings_match.group(2)),
                enabled=payload.get("enabled"),
                target_lang=payload.get("targetLang"),
                ahead=payload.get("ahead"),
            )
            if result is None:
                json_response(self, {"error": "Book not found"}, status=404)
                return
            json_response(self, {"translation": result})
            return

        book_chunk_translation_match = re.fullmatch(
            r"/api/([^/]+)/(\d+)/book-translation", parsed.path
        )
        if book_chunk_translation_match:
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except json.JSONDecodeError:
                json_response(self, {"error": "Invalid JSON"}, status=400)
                return
            try:
                result = get_book_chunk_translation(
                    book_chunk_translation_match.group(1),
                    int(book_chunk_translation_match.group(2)),
                    payload.get("chunkNumber", payload.get("page", 0)),
                    target_lang=payload.get("targetLang"),
                )
            except ValueError as exc:
                json_response(self, {"error": str(exc)}, status=400)
                return
            except Exception as exc:
                json_response(self, {"error": str(exc)}, status=500)
                return
            if result is None:
                json_response(self, {"error": "Book chunk not found"}, status=404)
                return
            json_response(self, result)
            return

        self.send_error(404, "Not found")

    def send_html(self, html):
        body = html.encode("utf-8")
        write_response(self, 200, body, "text/html; charset=utf-8")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}", file=sys.stderr)


def normalized_lang(lang):
    return (lang or "EN").upper()


def control_term_from_args(args):
    return " ".join(args.word or []).strip()


def resolve_control_collection_id(args, parser, command_name):
    if args.collection_id is not None:
        return args.collection_id

    term = control_term_from_args(args)
    if term:
        try:
            return int(term)
        except ValueError:
            parser.error(f"{command_name} collection id must be an integer")

    parser.error(f"{command_name} requires --collection_id or a positional collection id")


def run_elru_lookup(args, term, lang):
    source_is_ru = args.ruel or lang in RU_LANGS
    source_lang_id = 1 if source_is_ru else 0
    target_lang_id = 0 if source_is_ru else 1
    mode = "RU -> EL" if source_is_ru else "EL -> RU"

    with readonly_elru_connection(args.elru_db) as connection:
        return print_elru_results(
            connection,
            term,
            source_lang_id,
            target_lang_id,
            mode,
            args.limit,
        )


def run_wordnet_lookup(args, term):
    text_output = lookup_wordnet_text(term, show_all=args.all)
    if text_output:
        print(text_output)
        return 0
    return 1


def default_dependency_manifest():
    return {
        "dependencies": [
            {
                "id": "vue",
                "kind": "browser-js",
                "package": "vue",
                "version": "3.5.38",
                "local_path": "vendor/vue/vue.global.prod.js",
                "latest_url": "https://registry.npmjs.org/vue/latest",
                "download_url_template": "https://unpkg.com/vue@{version}/dist/vue.global.prod.js",
            },
            {
                "id": "argos-en-ru",
                "kind": "argos-translate-package",
                "from_code": "en",
                "to_code": "ru",
                "local_path": "tmp/argos-translate/packages",
                "managed_by": "argostranslate",
            },
        ]
    }


def load_dependency_manifest():
    if not DEPENDENCIES_MANIFEST_PATH.is_file():
        return default_dependency_manifest()
    try:
        return json.loads(DEPENDENCIES_MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_dependency_manifest()


def write_dependency_manifest(manifest):
    DEPENDENCIES_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEPENDENCIES_MANIFEST_PATH.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_manifest_dependency(manifest, dependency_id):
    for dependency in manifest.get("dependencies", []):
        if dependency.get("id") == dependency_id:
            return dependency
    dependency = next(
        item
        for item in default_dependency_manifest()["dependencies"]
        if item["id"] == dependency_id
    )
    manifest.setdefault("dependencies", []).append(dependency)
    return dependency


def prompt_yes_no(question, assume_yes=False):
    if assume_yes:
        print(f"{question} yes")
        return True
    if not sys.stdin.isatty():
        print(f"{question} no (non-interactive)")
        return False
    while True:
        answer = input(f"{question} [y/N] ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"", "n", "no"}:
            return False


def read_remote_json(url):
    try:
        with urlopen(url, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot fetch {url}: {exc}") from exc


def download_remote_file(url, target_path):
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(target_path.suffix + ".tmp")
    try:
        with urlopen(url, timeout=60) as response:
            tmp_path.write_bytes(response.read())
    except (OSError, URLError) as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        raise RuntimeError(f"Cannot download {url}: {exc}") from exc
    tmp_path.replace(target_path)


def update_vue_dependency(manifest, assume_yes=False):
    dependency = get_manifest_dependency(manifest, "vue")
    local_path = ROOT_DIR / dependency["local_path"]
    latest = read_remote_json(dependency["latest_url"])
    latest_version = str(latest.get("version") or "").strip()
    if not latest_version:
        raise RuntimeError("Cannot detect latest Vue version")

    current_version = str(dependency.get("version") or "unknown")
    if local_path.is_file() and current_version == latest_version:
        print(f"vue: local {current_version}; up to date")
        return False

    reason = "missing locally" if not local_path.is_file() else f"local {current_version}, latest {latest_version}"
    print(f"vue: {reason}")
    if not prompt_yes_no(f"Update vue to {latest_version}?", assume_yes=assume_yes):
        return False

    url = dependency["download_url_template"].format(version=latest_version)
    download_remote_file(url, local_path)
    dependency["version"] = latest_version
    print(f"vue: updated {dependency['local_path']}")
    return True


def update_argos_dependency(assume_yes=False):
    setup_argos_environment()
    try:
        import argostranslate.package
    except ImportError as exc:
        raise RuntimeError("argostranslate is not installed; install requirements first") from exc

    installed_package = next(
        (
            item
            for item in argostranslate.package.get_installed_packages()
            if item.from_code == "en" and item.to_code == "ru"
        ),
        None,
    )
    installed_version = getattr(installed_package, "package_version", "") if installed_package else ""

    argostranslate.package.update_package_index()
    available_packages = argostranslate.package.get_available_packages()
    available_package = next(
        (
            item
            for item in available_packages
            if item.from_code == "en" and item.to_code == "ru"
        ),
        None,
    )
    if available_package is None:
        raise RuntimeError("Argos en -> ru package is not available")
    available_version = getattr(available_package, "package_version", "")

    if installed_package and installed_version == available_version:
        print(f"argos-en-ru: local {installed_version}; up to date")
        return False

    if installed_package:
        print(f"argos-en-ru: local {installed_version or 'unknown'}, latest {available_version or 'unknown'}")
        question = f"Update Argos en -> ru package to {available_version or 'latest'}?"
    else:
        print(f"argos-en-ru: missing locally; latest {available_version or 'unknown'}")
        question = "Install Argos en -> ru package?"

    if not prompt_yes_no(question, assume_yes=assume_yes):
        return False

    if installed_package:
        argostranslate.package.uninstall(installed_package)
    argostranslate.package.install_from_path(available_package.download())
    print("argos-en-ru: installed locally")
    return True


def run_dependency_update(args):
    manifest = load_dependency_manifest()
    changed = False
    changed = update_vue_dependency(manifest, assume_yes=args.yes) or changed
    changed = update_argos_dependency(assume_yes=args.yes) or changed
    if changed or not DEPENDENCIES_MANIFEST_PATH.is_file():
        write_dependency_manifest(manifest)
        print(f"dependency manifest: {DEPENDENCIES_MANIFEST_PATH.relative_to(ROOT_DIR)}")
    return 0


def _translate_engine_check():
    """Run the offline translator once and report OK or the real error.

    This is the key signal for "why doesn't offline translation work": it drives
    the exact argos path the background worker uses and surfaces the exception
    inline, instead of losing it to the backend's invisible stderr.
    """
    sample = "This is a test."
    try:
        return True, translate_en_to_ru_segmented(sample)
    except Exception:  # noqa: BLE001 - report the traceback verbatim
        return False, traceback.format_exc()


def _collect_translate_status():
    """Snapshot every translate-enabled book's planned/translated chunks."""
    with Session(db.engine) as session:
        lock = session.get(TranslationLock, "book_translation")
        lock_owner = lock.owner if lock else ""
        lock_until = float(lock.locked_until) if lock else 0.0
        books = session.exec(
            select(BookContent).where(BookContent.translate_enabled == True)  # noqa: E712
        ).all()
        rows = []
        for book in books:
            try:
                chunks = json.loads(book.chunks_json)
            except (json.JSONDecodeError, TypeError):
                chunks = []
            total = len(chunks)
            target_lang = normalize_target_lang(book.translate_target_lang)
            ahead = max(0, int(book.translate_ahead or 0))
            progress = session.exec(
                select(BookProgress)
                .where(BookProgress.user_id == book.user_id)
                .where(BookProgress.collection_number == book.collection_number)
            ).first()
            current_index = int(progress.current_chunk_index) if progress else 0
            # Planned = chunks 1..(current + ahead), 1-based inclusive.
            target_index = min(total - 1, current_index + ahead) if total else -1
            planned = list(range(1, target_index + 2))
            translated = {
                int(number)
                for number in session.exec(
                    select(BookChunkTranslation.chunk_number)
                    .where(BookChunkTranslation.user_id == book.user_id)
                    .where(BookChunkTranslation.collection_number == book.collection_number)
                    .where(BookChunkTranslation.target_lang == target_lang)
                ).all()
            }
            rows.append(
                {
                    "user_id": book.user_id,
                    "collection": book.collection_number,
                    "title": book.title or f"collection {book.collection_number}",
                    "lang": target_lang,
                    "total": total,
                    "current_page": current_index + 1,
                    "ahead": ahead,
                    "planned": planned,
                    "translated": translated,
                }
            )
    return {"lock_owner": lock_owner, "lock_until": lock_until, "books": rows}


def translation_status_payload(max_chunks_per_book=300):
    """JSON snapshot of offline-translation progress for the in-app Translation
    page. Same data as the CLI ``--translate-status``, shaped for polling."""
    status = _collect_translate_status()
    now = time.time()
    remaining = status["lock_until"] - now
    books = []
    total_planned = 0
    total_done = 0
    for book in status["books"]:
        planned = book["planned"]
        translated = book["translated"]
        done = sum(1 for number in planned if number in translated)
        total_planned += len(planned)
        total_done += done
        books.append(
            {
                "userId": book["user_id"],
                "collection": book["collection"],
                "title": book["title"],
                "lang": book["lang"],
                "total": book["total"],
                "currentPage": book["current_page"],
                "ahead": book["ahead"],
                "planned": len(planned),
                "translated": done,
                "translatedTotal": len(translated),
                "chunksTruncated": len(planned) > max_chunks_per_book,
                "chunks": [
                    {"number": number, "done": number in translated}
                    for number in planned[:max_chunks_per_book]
                ],
            }
        )
    return {
        "worker": {
            "active": bool(status["lock_owner"]) and remaining > 0,
            "owner": status["lock_owner"],
            "expiresIn": max(0, round(remaining)) if remaining > 0 else 0,
        },
        "totals": {"planned": total_planned, "translated": total_done},
        "books": books,
    }


def _render_translate_status(status, engine, max_rows=50):
    now = time.time()
    lines = ["=== Offline translation status ==="]

    if engine is None:
        lines.append("Engine: (skipped)")
    elif engine[0]:
        lines.append(f'Engine: OK  "This is a test." -> {engine[1]!r}')
    else:
        lines.append("Engine: FAILED — offline translation is NOT working:")
        lines.extend("    " + line for line in engine[1].rstrip().splitlines())

    remaining = status["lock_until"] - now
    if status["lock_owner"] and remaining > 0:
        lines.append(
            f"Worker: ACTIVE  owner={status['lock_owner']}  lock expires in {remaining:.0f}s"
        )
    else:
        lines.append("Worker: idle  (no process is currently translating)")

    total_planned = sum(len(b["planned"]) for b in status["books"])
    total_done = sum(len(set(b["planned"]) & b["translated"]) for b in status["books"])
    lines.append(f"Planned chunks translated: {total_done} / {total_planned}")
    lines.append("")

    if not status["books"]:
        lines.append("No books have offline translation enabled.")
        return "\n".join(lines)

    head = f"{'book':30.30} {'lang':4} {'page':>9} {'ahead':>5} {'done/planned':>13} {'total':>6}"
    lines.append(head)
    lines.append("-" * len(head))
    for b in status["books"]:
        done = len(set(b["planned"]) & b["translated"])
        page = f"{b['current_page']}/{b['total']}"
        prog = f"{done}/{len(b['planned'])}"
        lines.append(
            f"{b['title']:30.30} {b['lang']:4} {page:>9} {b['ahead']:>5} {prog:>13} {b['total']:>6}"
        )

    lines.append("")
    lines.append(f"{'book':24.24} {'chunk':>6} {'status':>9}")
    lines.append("-" * 42)
    shown = 0
    for b in status["books"]:
        for number in b["planned"]:
            if shown >= max_rows:
                lines.append("  ... (more; raise --interval or narrow the book set)")
                return "\n".join(lines)
            state = "done" if number in b["translated"] else "PENDING"
            lines.append(f"{b['title']:24.24} {number:>6} {state:>9}")
            shown += 1
    return "\n".join(lines)


def show_translate_status(watch=False, interval=2.0, engine_check=True):
    engine = None
    if engine_check:
        print("Checking translation engine (loading argos, may take a moment)...", flush=True)
        engine = _translate_engine_check()
    while True:
        rendered = _render_translate_status(_collect_translate_status(), engine)
        if watch:
            sys.stdout.write("\033[H\033[2J")  # clear screen for the live view
        print(rendered, flush=True)
        if not watch:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print()
            return 0


def parse_control_args(argv):
    parser = argparse.ArgumentParser(
        description="Manage dictionary collections and language-specific lookups."
    )
    parser.add_argument("word", nargs="*", help="word or collection id")
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path; overrides DICT_DATABASE_URL for this command",
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL; overrides --database and DICT_DATABASE_URL",
    )
    parser.add_argument(
        "--lang",
        default="EN",
        help="language code for lookups, collection creation, and --load-words",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show application version",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="initialize the configured database; --name creates its first user",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="show definitions without examples too",
    )
    parser.add_argument(
        "--translate-status",
        "--translate_status",
        dest="translate_status",
        action="store_true",
        help="show offline book-translation status: engine health, worker state, "
        "and per-chunk planned/translated progress",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="with --translate-status, refresh live until interrupted",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="refresh interval in seconds for --watch (default: 2.0)",
    )
    parser.add_argument(
        "--no-engine-check",
        dest="engine_check",
        action="store_false",
        help="with --translate-status, skip the live translator self-test",
    )
    parser.add_argument("--user", help="user name for collection commands")
    parser.add_argument(
        "--create-user",
        "--create_user",
        action="store_true",
        help="create a user; requires --name",
    )
    parser.add_argument(
        "--delete-user",
        "--delete_user",
        action="store_true",
        help="delete a user and all owned data after confirmation; requires --user-id",
    )
    parser.add_argument(
        "--rename-user",
        "--rename_user",
        action="store_true",
        help="rename a user; requires --user-id and --name",
    )
    parser.add_argument(
        "--list-users",
        "--list_users",
        action="store_true",
        help="list users with ids and summary counts",
    )
    parser.add_argument(
        "--user-id",
        "--user_id",
        dest="user_id",
        help="user id for user administration commands",
    )
    parser.add_argument(
        "--collection_id",
        type=int,
        default=None,
        help="collection number for database and load commands",
    )
    parser.add_argument(
        "--db",
        action="store_true",
        help="show word description from the application database",
    )
    parser.add_argument(
        "--create_collection",
        "--create-collection",
        action="store_true",
        help="create a collection for the user; requires --name and --type",
    )
    parser.add_argument(
        "--type",
        choices=("book", "words"),
        help="collection type for --create-collection",
    )
    parser.add_argument(
        "--list_collections",
        "--list-collections",
        action="store_true",
        help="list user collections with word counts",
    )
    parser.add_argument(
        "--delete_collection",
        "--delete-collection",
        action="store_true",
        help="delete a collection by --collection_id after confirmation",
    )
    parser.add_argument(
        "--rename_collection",
        "--rename-collection",
        action="store_true",
        help="rename a collection by --collection_id; requires --name",
    )
    parser.add_argument(
        "--show_collection",
        "--show-collection",
        action="store_true",
        help="show all words in a collection with learned status",
    )
    parser.add_argument(
        "--load-words",
        type=Path,
        help="file with one word per line to load into the application database",
    )
    parser.add_argument(
        "--load-book",
        "--load_book",
        action="store_true",
        help="load a text file into an existing book collection",
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="book file for --load-book",
    )
    parser.add_argument(
        "--book-min-chars",
        type=int,
        default=400,
        help="minimum target characters per book page",
    )
    parser.add_argument(
        "--book-max-chars",
        type=int,
        default=650,
        help="maximum target characters per book page",
    )
    parser.add_argument(
        "--name",
        "--Name",
        dest="name",
        help="user, collection, or book title",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="print verbose load progress and timings",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="check and update local external dependencies after confirmation",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="answer yes to --update prompts",
    )
    direction = parser.add_mutually_exclusive_group()
    direction.add_argument(
        "--elru",
        action="store_true",
        help="force Greek to Russian lookup in elru.db",
    )
    direction.add_argument(
        "--ruel",
        action="store_true",
        help="force Russian to Greek lookup in elru.db",
    )
    parser.add_argument(
        "--elru-db",
        type=Path,
        default=ROOT_DIR / "elru.db",
        help="path to elru.db for Greek/Russian lookups",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum lookup rows to show",
    )
    return parser, parser.parse_args(argv)


def control_main(argv=None):
    parser, args = parse_control_args(sys.argv[1:] if argv is None else argv)
    db.configure_database(args.database, args.database_url)
    lang = normalized_lang(args.lang)
    collection_id = args.collection_id or 1

    if args.version:
        print(APP_VERSION)
        return 0

    if args.init:
        init_database(args.name)
        return 0

    if args.update:
        return run_dependency_update(args)

    if args.translate_status:
        return show_translate_status(
            watch=args.watch,
            interval=args.interval,
            engine_check=args.engine_check,
        )

    if args.create_user:
        if not args.name:
            parser.error("--create-user requires --name")
        create_user(args.name)
        return 0

    if args.list_users:
        list_users()
        return 0

    if args.delete_user:
        if not args.user_id:
            parser.error("--delete-user requires --user-id")
        delete_user(args.user_id)
        return 0

    if args.rename_user:
        if not args.user_id:
            parser.error("--rename-user requires --user-id")
        if not args.name:
            parser.error("--rename-user requires --name")
        rename_user(args.user_id, args.name)
        return 0

    if args.db:
        if not args.user:
            parser.error("--db requires --user")
        term = control_term_from_args(args)
        if not term:
            parser.error("--db requires a word")
        show_db_word(term, args.user, collection_id)
        return 0

    if args.create_collection:
        if not args.user:
            parser.error("--create_collection requires --user")
        if not args.name:
            parser.error("--create_collection requires --name")
        if not args.type:
            parser.error("--create_collection requires --type book|words")
        collection_kind = args.type
        create_collection(args.user, args.name, lang=lang, kind=collection_kind)
        return 0

    if args.list_collections:
        if not args.user:
            parser.error("--list_collections requires --user")
        list_collections(args.user)
        return 0

    if args.delete_collection:
        if not args.user:
            parser.error("--delete_collection requires --user")
        if args.collection_id is None:
            parser.error("--delete_collection requires --collection_id")
        delete_collection(args.user, args.collection_id)
        return 0

    if args.rename_collection:
        if not args.user:
            parser.error("--rename_collection requires --user")
        if args.collection_id is None:
            parser.error("--rename_collection requires --collection_id")
        if not args.name or not args.name.strip():
            parser.error("--rename_collection requires non-empty --name")
        rename_collection(args.user, args.collection_id, args.name)
        return 0

    if args.show_collection:
        if not args.user:
            parser.error("--show_collection requires --user")
        show_collection(
            args.user,
            resolve_control_collection_id(args, parser, "--show_collection"),
        )
        return 0

    if args.load_words:
        if not args.user:
            parser.error("--load-words requires --user")
        if args.collection_id is None:
            parser.error("--load-words requires --collection_id")

        inserted, skipped = load_words(
            args.load_words,
            args.user,
            collection_id,
            show_all=args.all,
            lang=lang,
            debug=args.debug,
        )
        print(f"Inserted {inserted} words into collection {collection_id}.")
        if skipped:
            print(f"Skipped {skipped} words without {lang} dictionary output.")
        return 0

    if args.load_book:
        if not args.user:
            parser.error("--load-book requires --user")
        if not args.file:
            parser.error("--load-book requires --file")
        if args.collection_id is None:
            parser.error("--load-book requires --collection_id")
        load_book(
            args.file,
            args.user,
            args.collection_id,
            lang=lang,
            min_chars=args.book_min_chars,
            max_chars=args.book_max_chars,
            debug=args.debug,
        )
        return 0

    term = control_term_from_args(args)
    if not term:
        parser.error("word is required unless a collection command is used")

    if args.elru or args.ruel or lang in ELRU_LANGS or lang in RU_LANGS:
        return run_elru_lookup(args, term, lang)

    return run_wordnet_lookup(args, term)


def parse_server_args(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite database path; overrides DICT_DATABASE_URL for this command",
    )
    parser.add_argument(
        "--database-url",
        help="SQLAlchemy database URL; overrides --database and DICT_DATABASE_URL",
    )
    parser.add_argument(
        "--init",
        action="store_true",
        help="initialize the database before starting the server",
    )
    parser.add_argument(
        "--name",
        help="first user name when --init is used with a database that has no users",
    )
    parser.add_argument("--version", action="store_true", help="show application version")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="show per-request server, fetch, parse, and render timings in the client",
    )
    return parser.parse_args(argv)


def create_server(host="127.0.0.1", port=8000, debug=False):
    mimetypes.init()
    server = ThreadingHTTPServer((host, port), TypingHandler)
    server.debug_enabled = bool(debug)
    return server


def server_ready_payload(server):
    host, port = server.server_address[:2]
    public_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return {
        "host": public_host,
        "port": port,
        "url": f"http://{public_host}:{port}/",
        "version": APP_VERSION,
    }


def server_main(argv=None):
    args = parse_server_args(sys.argv[1:] if argv is None else argv)
    db.configure_database(args.database, args.database_url)
    if args.version:
        print(APP_VERSION)
        return 0
    if args.init:
        init_database(args.name)
    else:
        ensure_database()

    worker_thread = start_translation_worker(translate_en_to_ru_segmented)

    server = create_server(args.host, args.port, debug=args.debug)
    ready = server_ready_payload(server)
    print("DICT_SERVER_READY " + json.dumps(ready, ensure_ascii=False), flush=True)
    print(f"Serving typing page at {ready['url']}", flush=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server.", file=sys.stderr)
        server.server_close()
        # Stop the background translator before exiting. It runs native code
        # (ctranslate2/OpenMP), and letting the interpreter tear down while any
        # of those threads are live aborts with "terminate called without an
        # active exception" (core dump). Ask it to stop, flush our own output,
        # then exit hard: even once our worker thread joins, ctranslate2 keeps an
        # internal thread pool alive that a normal exit would still abort on.
        # os._exit skips interpreter finalization entirely, so there is no window
        # for that abort.
        stop_translation_worker(worker_thread, timeout=5.0)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    finally:
        # Reached only if serve_forever() raised something other than Ctrl+C;
        # let that exception propagate (with its traceback and a non-zero exit)
        # instead of masking it.
        server.server_close()


def print_main_help():
    print("usage: dict.py [--version] {server,control} ...")
    print()
    print(f"version: {APP_VERSION}")
    print()
    print("commands:")
    print("  server   run the HTTP server")
    print("  control  manage collections and dictionary lookups")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in {"-h", "--help"}:
        print_main_help()
        return 0

    if argv[0] == "--version":
        print(APP_VERSION)
        return 0

    command, command_args = argv[0], argv[1:]
    if command == "server":
        return server_main(command_args)
    if command == "control":
        return control_main(command_args)
    raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
