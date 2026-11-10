"""Static application configuration: paths, version and starter assets.

This module holds only immutable, import-time constants. Mutable database
runtime state (engine, active URL) lives in :mod:`dictapp.db` so it can be
rebound safely at runtime without stale-binding surprises.
"""

import os
import sys
from pathlib import Path

APP_VERSION = "0.4.2"


def configure_utf8_stdio():
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if not reconfigure:
            continue
        try:
            reconfigure(encoding="utf-8", errors="backslashreplace")
        except (OSError, ValueError):
            pass


configure_utf8_stdio()


def default_root_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    # config.py lives in <root>/dictapp/, so the project root is two levels up.
    return Path(__file__).resolve().parent.parent


ROOT_DIR = Path(os.environ.get("DICT_ROOT_DIR", default_root_dir())).resolve()
DATA_DIR = Path(os.environ.get("DICT_DATA_DIR", ROOT_DIR)).expanduser().resolve()
DB_PATH = DATA_DIR / "dict.sqlite3"
USER_SETTINGS_PATH = Path(
    os.environ.get("DICT_USER_SETTINGS_PATH", DATA_DIR / "user_settings.json")
).resolve()
VENDOR_DIR = ROOT_DIR / "vendor"
DEPENDENCIES_MANIFEST_PATH = VENDOR_DIR / "dependencies.json"
MANUAL_DIR = ROOT_DIR / "docs" / "manual"
README_PDF_PATH = MANUAL_DIR / "readme.pdf"
README_MD_PATH = MANUAL_DIR / "README.md"
README_RU_MD_PATH = MANUAL_DIR / "README.ru.md"
STARTER_WORD_LISTS = (
    ("\u2605", ROOT_DIR / "1_star.txt"),
    ("\u2605\u2605", ROOT_DIR / "2_star.txt"),
    ("\u2605\u2605\u2605", ROOT_DIR / "3_star.txt"),
)
STARTER_BOOK_PATH = ROOT_DIR / "zen.txt"
STARTER_BOOK_TITLE = "Zen"

STARTER_BOOKS = (
    (STARTER_BOOK_PATH, STARTER_BOOK_TITLE),
    (ROOT_DIR / "thinking-fast-and-slow.txt", "Thinking, Fast and Slow"),
)
