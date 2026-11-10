"""SQLModel table definitions for the dict application."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, UniqueConstraint
from sqlmodel import Field, SQLModel

# Existing SQLite timestamps are naive UTC. Declare their SQL type explicitly:
# newer SQLModel versions otherwise infer UTCDateTime and reject naive values.


class User(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str = Field(index=True)
    default_collection_number: int = Field(default=1)
    default_lang: str = Field(default="EN", index=True)
    all_lang: str = Field(default="EN,RU")


class Collection(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "number", name="uq_collection_user_number"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    number: int = Field(index=True)
    title: str = Field(default="words")
    lang: str = Field(default="EN", index=True)
    kind: str = Field(default="words", index=True)


class CollectionSequence(SQLModel, table=True):
    __tablename__ = "collection_sequence"

    user_id: str = Field(foreign_key="user.id", primary_key=True)
    next_number: int = Field(default=1, index=True)


class Word(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    learned: bool = Field(default=False, index=True)
    in_sprint: bool = Field(default=True, index=True)
    name: str = Field(index=True)
    description: str
    status: str = Field(default="default", index=True)


class KnownWord(SQLModel, table=True):
    __tablename__ = "known_word"
    __table_args__ = (
        UniqueConstraint("user_id", "lang", "name", name="uq_known_word_user_lang_name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    lang: str = Field(index=True)
    name: str = Field(index=True)
    source_collection_number: int = Field(index=True)
    source_collection_title: str = Field(default="")
    created_at: datetime = Field(sa_type=DateTime(timezone=False), default_factory=lambda: datetime.now(UTC), index=True)


class Meaning(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    learned: bool = Field(default=False, index=True)
    name: str = Field(index=True)
    description: str
    status: str = Field(default="meanings", index=True)


class StopWord(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_stop_word_user_name"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    name: str = Field(index=True)
    reason: str


class Reminder(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "collection_number",
            "name",
            name="uq_reminder_user_collection_name",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    name: str = Field(index=True)
    inserted_at: datetime = Field(sa_type=DateTime(timezone=False), index=True)
    remind_at: datetime = Field(sa_type=DateTime(timezone=False), index=True)
    progression_model: str = Field(default="default", index=True)
    progression_index: int = Field(default=0, index=True)
    progression_label: str = Field(default="30m", index=True)


class BookContent(SQLModel, table=True):
    __tablename__ = "book_content"

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    words_collection_number: int = Field(index=True)
    title: str = Field(index=True)
    file_name: str
    content: str
    chunks_json: str
    chunk_count: int = Field(default=0, index=True)
    created_at: datetime = Field(sa_type=DateTime(timezone=False), default_factory=lambda: datetime.now(UTC), index=True)
    translate_enabled: bool = Field(default=False, index=True)
    translate_target_lang: str = Field(default="RU")
    translate_ahead: int = Field(default=10)


class BookChunkTranslation(SQLModel, table=True):
    """Cached offline translation of a single book chunk.

    A present row means "chunk N of this book is translated into target_lang".
    Deleting the row (e.g. when the chunk text is edited) invalidates the cache
    and the background worker will re-translate it on a later tick.
    """

    __tablename__ = "book_chunk_translation"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "collection_number",
            "chunk_number",
            "target_lang",
            name="uq_book_chunk_translation",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    chunk_number: int = Field(index=True)
    target_lang: str = Field(default="RU", index=True)
    translation: str
    updated_at: datetime = Field(sa_type=DateTime(timezone=False), default_factory=lambda: datetime.now(UTC), index=True)


class TranslationLock(SQLModel, table=True):
    """Single-row advisory lock so only one process translates at a time.

    ``locked_until`` is an epoch-seconds float (not a datetime) so the atomic
    ``UPDATE ... WHERE locked_until < :now`` acquire compares plain numbers and
    avoids SQLite datetime string/timezone comparison pitfalls. A lock is held
    with a TTL, so a crashed worker's lock expires instead of blocking forever.
    """

    __tablename__ = "translation_lock"

    name: str = Field(primary_key=True)
    owner: str = Field(default="")
    locked_until: float = Field(default=0.0)


class BookProgress(SQLModel, table=True):
    __tablename__ = "book_progress"
    __table_args__ = (
        UniqueConstraint("user_id", "collection_number", name="uq_book_progress_user_collection"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    current_chunk_index: int = Field(default=0, index=True)
    completed_chunks: int = Field(default=0, index=True)
    read_chunks_json: str = Field(default="[]")
    updated_at: datetime = Field(sa_type=DateTime(timezone=False), default_factory=lambda: datetime.now(UTC), index=True)


class BookQuote(SQLModel, table=True):
    __tablename__ = "book_quote"
    __table_args__ = (
        UniqueConstraint("user_id", "collection_number", "number", name="uq_book_quote_number"),
    )

    id: int | None = Field(default=None, primary_key=True)
    user_id: str = Field(foreign_key="user.id", index=True)
    collection_number: int = Field(index=True)
    lang: str = Field(default="EN", index=True)
    number: int = Field(index=True)
    chunk_number: int = Field(default=1, index=True)
    quote: str
    muted: bool = Field(default=False, index=True)
    created_at: datetime = Field(sa_type=DateTime(timezone=False), default_factory=lambda: datetime.now(UTC), index=True)
