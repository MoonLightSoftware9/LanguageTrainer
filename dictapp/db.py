"""Database engine, connection tuning and runtime database selection.

``engine`` and ``SQLITE_URL`` are module-level state that :func:`configure_database`
rebinds when the CLI points the app at a different database file. Callers must
therefore reference them as attributes (``db.engine``), never import the bound
names directly, so they always observe the current values.
"""

import os
from pathlib import Path

from sqlalchemy import event
from sqlmodel import create_engine

from dictapp.config import DB_PATH

SQLITE_URL = os.environ.get("DICT_DATABASE_URL", f"sqlite:///{DB_PATH}")


def _configure_sqlite_connection(dbapi_connection, connection_record):
    """Apply performance/concurrency PRAGMAs to every new SQLite connection.

    WAL + a busy timeout let the ThreadingHTTPServer's reader/writer threads work
    concurrently instead of serializing on the default rollback-journal lock, and
    synchronous=NORMAL avoids an fsync on every commit while staying crash-safe
    under WAL. These are no-ops for :memory: databases.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA temp_store=MEMORY")
        cursor.execute("PRAGMA cache_size=-16000")  # ~16 MB page cache
        cursor.execute("PRAGMA mmap_size=134217728")  # 128 MB memory-mapped I/O
    finally:
        cursor.close()


def make_engine(url):
    new_engine = create_engine(url, connect_args={"check_same_thread": False})
    event.listen(new_engine, "connect", _configure_sqlite_connection)
    return new_engine


engine = make_engine(SQLITE_URL)


def sqlite_url_from_path(path):
    return f"sqlite:///{Path(path).expanduser().resolve()}"


def sqlite_path_from_url(url):
    if url == "sqlite:///:memory:" or not url.startswith("sqlite:///"):
        return None
    path = url.removeprefix("sqlite:///")
    if not path:
        return None
    return Path(path)


def configure_database(database_path=None, database_url=None):
    global SQLITE_URL, engine

    new_url = database_url or (sqlite_url_from_path(database_path) if database_path else SQLITE_URL)
    database_file = sqlite_path_from_url(new_url)
    if database_file and database_file != Path(":memory:"):
        database_file.expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    if new_url != SQLITE_URL:
        engine.dispose()
        SQLITE_URL = new_url
        engine = make_engine(SQLITE_URL)
