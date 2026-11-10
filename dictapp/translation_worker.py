"""Background worker that pre-translates book chunks offline.

Design (see also the notes in dict.py's offline-translation section):

* A single daemon thread wakes on a fixed interval and, for every book with
  ``translate_enabled``, ensures all chunks in ``[0 .. current_chunk_index +
  translate_ahead]`` (0-based, inclusive) have a cached translation. The range
  starts at 0, so everything up to the page being read is translated by
  definition; lowering ``translate_ahead`` simply stops taking new far-ahead
  chunks — already cached translations are kept.

* Books are translated round-robin, not one at a time: each tick takes a single
  pending chunk from every enabled book in turn and repeats, so all books' status
  bars advance together instead of the first book finishing before the next one
  starts.

* Concurrency is guarded by a single-row TTL lock in ``translation_lock``
  (epoch-seconds ``locked_until``). Acquire is an atomic
  ``UPDATE ... WHERE locked_until < now``; only the process whose UPDATE
  affected the row proceeds, so overlapping ticks or a second process (CLI,
  Docker restart, a stray server) never double-translate. The lock has a TTL and
  is renewed between chunks, so a crashed worker's lock expires instead of
  wedging the queue forever.

* SQLite allows one writer at a time. Translation (slow) runs OUTSIDE any
  transaction; only the short cache INSERT is transactional, keeping request
  threads from stalling on the writer.

* There is no persistent queue: each tick recomputes what is missing from the
  current settings, so an edited chunk (whose cache row was deleted) is picked
  up again and a shrunk ``translate_ahead`` takes effect immediately.
"""

import json
import logging
import os
import socket
import threading
import time
from datetime import UTC, datetime

from sqlalchemy import text
from sqlmodel import Session, select

from dictapp import db
from dictapp.models import BookChunkTranslation, BookContent, BookProgress

LOCK_NAME = "book_translation"
DEFAULT_INTERVAL_SECONDS = 20.0
DEFAULT_LOCK_TTL_SECONDS = 180.0

logger = logging.getLogger("dictapp.translation_worker")

# Set on shutdown so the loop stops between ticks (and between chunks) instead of
# being killed mid-translation. A daemon thread abruptly torn down while inside a
# native translate call (ctranslate2) makes the interpreter std::terminate on
# exit ("terminate called without an active exception" / core dump).
_stop_event = threading.Event()


def start_translation_worker(
    translate_fn,
    interval=DEFAULT_INTERVAL_SECONDS,
    lock_ttl=DEFAULT_LOCK_TTL_SECONDS,
):
    """Start the daemon worker thread and return it.

    ``translate_fn`` takes an English chunk string and returns its Russian
    translation (it must handle text longer than the argos per-call limit).
    """
    _stop_event.clear()
    owner = f"{socket.gethostname()}:{os.getpid()}"
    thread = threading.Thread(
        target=_run_loop,
        args=(translate_fn, float(interval), float(lock_ttl), owner),
        name="translation-worker",
        daemon=True,
    )
    thread.start()
    return thread


def stop_translation_worker(thread=None, timeout=5.0):
    """Ask the worker to stop and (optionally) wait for it.

    Returns True if the thread finished within ``timeout`` (or no thread was
    given). A False return means the worker is still inside a native translate
    call; the caller should exit hard rather than let a normal interpreter
    teardown abort on the live C++ thread.
    """
    _stop_event.set()
    if thread is None:
        return True
    thread.join(timeout)
    return not thread.is_alive()


def _run_loop(translate_fn, interval, lock_ttl, owner):
    while not _stop_event.is_set():
        try:
            if _acquire_lock(owner, lock_ttl):
                try:
                    _process_all_books(translate_fn, owner, lock_ttl)
                finally:
                    _release_lock(owner)
        except Exception:  # pragma: no cover - worker must never die
            logger.exception("translation worker tick failed")
        # Interruptible sleep: wake immediately on shutdown instead of idling out
        # the full interval.
        _stop_event.wait(interval)


# --- lock -------------------------------------------------------------------


def _acquire_lock(owner, ttl):
    now = time.time()
    until = now + ttl
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT OR IGNORE INTO translation_lock (name, owner, locked_until) "
                "VALUES (:name, '', 0)"
            ),
            {"name": LOCK_NAME},
        )
        result = connection.execute(
            text(
                "UPDATE translation_lock SET owner = :owner, locked_until = :until "
                "WHERE name = :name AND locked_until < :now"
            ),
            {"owner": owner, "until": until, "now": now, "name": LOCK_NAME},
        )
        return result.rowcount == 1


def _renew_lock(owner, ttl):
    until = time.time() + ttl
    with db.engine.begin() as connection:
        result = connection.execute(
            text(
                "UPDATE translation_lock SET locked_until = :until "
                "WHERE name = :name AND owner = :owner"
            ),
            {"until": until, "owner": owner, "name": LOCK_NAME},
        )
        return result.rowcount == 1


def _release_lock(owner):
    with db.engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE translation_lock SET locked_until = 0 "
                "WHERE name = :name AND owner = :owner"
            ),
            {"name": LOCK_NAME, "owner": owner},
        )


# --- work -------------------------------------------------------------------


# Per-chunk outcome codes for _translate_one_chunk / the round-robin loop.
_OK = "ok"
_LOCK_LOST = "lock_lost"
_FAIL = "fail"


def _process_all_books(translate_fn, owner, lock_ttl):
    """Translate every enabled book round-robin: one chunk per book per round.

    Books are not drained one at a time — each round takes a single pending chunk
    from each book in turn, so all books advance toward full translation together
    instead of the first book finishing before the next one starts. Pending lists
    are computed once per tick (as before); the reader-moved / edited-chunk cases
    are picked up on the next tick.
    """
    with Session(db.engine) as session:
        book_keys = session.exec(
            select(BookContent.user_id, BookContent.collection_number).where(
                BookContent.translate_enabled == True  # noqa: E712
            )
        ).all()

    # One live queue per book: [user_id, collection_number, target_lang, iterator].
    queues = []
    for user_id, collection_number in book_keys:
        with Session(db.engine) as session:
            plan = _pending_for_book(session, user_id, collection_number)
        if not plan:
            continue
        target_lang, pending = plan
        if pending:
            queues.append([user_id, collection_number, target_lang, iter(pending)])

    # Round-robin until every queue is exhausted (or the lock is lost). A book is
    # dropped from the rotation when its iterator runs out (done for this tick) or
    # a chunk fails (matches the old per-book "stop on first failure" behavior).
    while queues:
        if _stop_event.is_set():
            return  # shutting down; stop between chunks, not mid-translation
        if not _renew_lock(owner, lock_ttl):
            return  # lost the lock; another worker took over
        survivors = []
        for user_id, collection_number, target_lang, pending_iter in queues:
            try:
                chunk_number, source_text = next(pending_iter)
            except StopIteration:
                continue  # book fully translated this tick; drop it
            status = _translate_one_chunk(
                user_id,
                collection_number,
                target_lang,
                chunk_number,
                source_text,
                translate_fn,
                owner,
                lock_ttl,
            )
            if status == _LOCK_LOST:
                return
            if status == _FAIL:
                continue  # drop this book for this tick; retry on the next tick
            survivors.append([user_id, collection_number, target_lang, pending_iter])
        queues = survivors


def _normalize_lang(value):
    lang = str(value or "RU").strip().upper()
    return lang or "RU"


def _pending_for_book(session, user_id, collection_number):
    book = session.exec(
        select(BookContent)
        .where(BookContent.user_id == user_id)
        .where(BookContent.collection_number == collection_number)
    ).first()
    if not book or not book.translate_enabled:
        return None
    try:
        chunks = json.loads(book.chunks_json)
    except (json.JSONDecodeError, TypeError):
        chunks = []
    if not isinstance(chunks, list) or not chunks:
        return None

    total = len(chunks)
    target_lang = _normalize_lang(book.translate_target_lang)
    ahead = max(0, int(book.translate_ahead or 0))
    progress = session.exec(
        select(BookProgress)
        .where(BookProgress.user_id == user_id)
        .where(BookProgress.collection_number == collection_number)
    ).first()
    current_index = int(progress.current_chunk_index) if progress else 0
    current_index = max(0, min(current_index, total - 1))
    target_index = min(total - 1, current_index + ahead)  # 0-based inclusive

    existing = {
        int(number)
        for number in session.exec(
            select(BookChunkTranslation.chunk_number)
            .where(BookChunkTranslation.user_id == user_id)
            .where(BookChunkTranslation.collection_number == collection_number)
            .where(BookChunkTranslation.target_lang == target_lang)
        ).all()
    }
    # Order matters: translate from the current position forward first (the
    # pages about to be read), then backfill the pages already behind, nearest
    # first. A reader who jumped deep into the book gets the upcoming pages
    # cached promptly instead of waiting for chunk 0.. to be translated first.
    forward = range(current_index, target_index + 1)
    behind = range(current_index - 1, -1, -1)
    pending = [
        (index + 1, str(chunks[index]))
        for index in (*forward, *behind)
        if (index + 1) not in existing
    ]
    return target_lang, pending


def _chunk_is_cached(session, user_id, collection_number, chunk_number, target_lang):
    return (
        session.exec(
            select(BookChunkTranslation.id)
            .where(BookChunkTranslation.user_id == user_id)
            .where(BookChunkTranslation.collection_number == collection_number)
            .where(BookChunkTranslation.chunk_number == chunk_number)
            .where(BookChunkTranslation.target_lang == target_lang)
        ).first()
        is not None
    )


def _chunk_still_planned(session, user_id, collection_number, target_lang, chunk_number):
    """Is this chunk still within the book's current planned window?

    The tick's plan is computed once (see _pending_for_book), but the book's
    settings can change while the tick drains it — most notably shrinking
    translate_ahead (e.g. switching from "translate in full" to "ahead N").
    Re-checking here lets a shrunk window take effect within one chunk instead
    of finishing the whole stale plan, and mirrors the status page's planned set
    exactly: chunks 1..(current_index + ahead + 1), which also covers the pages
    behind the current one.
    """
    book = session.exec(
        select(BookContent)
        .where(BookContent.user_id == user_id)
        .where(BookContent.collection_number == collection_number)
    ).first()
    if not book or not book.translate_enabled:
        return False
    if _normalize_lang(book.translate_target_lang) != target_lang:
        return False  # target language changed under us
    total = int(book.chunk_count or 0)
    if total <= 0:
        try:
            total = len(json.loads(book.chunks_json))
        except (json.JSONDecodeError, TypeError):
            total = 0
    if total <= 0:
        return False
    ahead = max(0, int(book.translate_ahead or 0))
    progress = session.exec(
        select(BookProgress)
        .where(BookProgress.user_id == user_id)
        .where(BookProgress.collection_number == collection_number)
    ).first()
    current_index = int(progress.current_chunk_index) if progress else 0
    current_index = max(0, min(current_index, total - 1))
    target_index = min(total - 1, current_index + ahead)  # 0-based inclusive
    return 1 <= chunk_number <= target_index + 1


def _translate_one_chunk(
    user_id,
    collection_number,
    target_lang,
    chunk_number,
    source_text,
    translate_fn,
    owner,
    lock_ttl,
):
    """Translate and cache a single chunk. Returns _OK, _LOCK_LOST or _FAIL."""
    if not _renew_lock(owner, lock_ttl):
        return _LOCK_LOST
    with Session(db.engine) as session:
        # The plan may be stale: translate_ahead can be shrunk mid-tick (e.g.
        # switching from "in full" to "ahead N"), so re-check the window before
        # spending time translating a chunk that is no longer planned.
        if not _chunk_still_planned(session, user_id, collection_number, target_lang, chunk_number):
            return _OK
        # A reader viewing this page may have translated and cached it between the
        # plan and now; re-check so the same chunk is never translated twice (the
        # on-demand reader path also writes to the same cache).
        if _chunk_is_cached(session, user_id, collection_number, chunk_number, target_lang):
            return _OK
    try:
        translation = translate_fn(source_text)
    except Exception:
        # Most likely argos not installed / model missing. Skip this book for
        # this tick to avoid log spam; retry on the next tick.
        logger.exception(
            "translation failed (book collection %s, chunk %s); skipping rest this tick",
            collection_number,
            chunk_number,
        )
        return _FAIL
    _store_translation(
        user_id, collection_number, chunk_number, target_lang, source_text, translation
    )
    return _OK


def _store_translation(
    user_id, collection_number, chunk_number, target_lang, source_text, translation
):
    with Session(db.engine) as session:
        book = session.exec(
            select(BookContent)
            .where(BookContent.user_id == user_id)
            .where(BookContent.collection_number == collection_number)
        ).first()
        if not book or not book.translate_enabled:
            return  # disabled or removed while translating
        # Guard against a race with a chunk edit: if the chunk text changed while
        # we were translating, skip caching a now-stale translation.
        try:
            chunks = json.loads(book.chunks_json)
        except (json.JSONDecodeError, TypeError):
            return
        index = chunk_number - 1
        if index < 0 or index >= len(chunks) or str(chunks[index]) != source_text:
            return
        already = session.exec(
            select(BookChunkTranslation)
            .where(BookChunkTranslation.user_id == user_id)
            .where(BookChunkTranslation.collection_number == collection_number)
            .where(BookChunkTranslation.chunk_number == chunk_number)
            .where(BookChunkTranslation.target_lang == target_lang)
        ).first()
        if already:
            return
        session.add(
            BookChunkTranslation(
                user_id=user_id,
                collection_number=collection_number,
                chunk_number=chunk_number,
                target_lang=target_lang,
                translation=translation,
                updated_at=datetime.now(UTC),
            )
        )
        session.commit()
