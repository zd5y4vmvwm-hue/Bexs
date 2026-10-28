"""Explicit, bounded facts and replies alongside Bexs's existing word memory.

TeachMemory shares its caller's SQLite connection. Initialization is the only
operation that commits. All subsequent changes, including lookup counters, stay
in the current transaction so the caller can commit them with the Telegram
update offset. A failed operation rolls back its own savepoint, not other work.

Only deliberately supplied facts/replies belong here. This module does not mine
messages, infer facts, or change the original seed. Chat membership and Telegram
sender authenticity must be checked by the adapter before passing an actor ID.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import sqlite3
import unicodedata
from typing import Iterator


MAX_TOPIC_LENGTH = 120
MAX_VALUE_LENGTH = 1200
MAX_ITEMS_PER_CHAT = 500
MAX_HISTORY_PER_ITEM = 5
_SQLITE_MAX_INT = (1 << 63) - 1
_KINDS = frozenset(("fact", "reply"))
_COUNTERS = ("created", "updated", "forgotten", "lookups", "hits", "misses")
_FIELDS = (
    "chat_id", "kind", "topic", "value", "created_by", "updated_by",
    "created_at", "updated_at", "revision", "created_message_id",
    "updated_message_id",
)
_COLUMNS = ", ".join(_FIELDS)
_COLUMN_DEFINITIONS = """
    chat_id INTEGER NOT NULL CHECK (chat_id != 0),
    kind TEXT NOT NULL CHECK (kind IN ('fact', 'reply')),
    topic TEXT NOT NULL CHECK (length(topic) BETWEEN 1 AND 120),
    value TEXT NOT NULL CHECK (length(value) BETWEEN 1 AND 1200),
    created_by INTEGER NOT NULL CHECK (created_by > 0),
    updated_by INTEGER NOT NULL CHECK (updated_by > 0),
    created_at INTEGER NOT NULL CHECK (created_at >= 0),
    updated_at INTEGER NOT NULL CHECK (updated_at >= 0),
    revision INTEGER NOT NULL CHECK (revision > 0),
    created_message_id INTEGER NOT NULL CHECK (created_message_id > 0),
    updated_message_id INTEGER NOT NULL CHECK (updated_message_id > 0)
"""


class TeachMemoryError(Exception):
    """An explicit-memory operation cannot be completed."""


class ValidationError(TeachMemoryError, ValueError):
    """An input does not meet the bounded memory contract."""


class PermissionDenied(TeachMemoryError, PermissionError):
    """Only the original creator or a verified override may mutate this item."""


class CapacityExceeded(TeachMemoryError):
    """The chat has reached its active-item limit."""


@dataclass(frozen=True)
class MemoryItem:
    chat_id: int
    kind: str
    topic: str
    value: str
    created_by: int
    updated_by: int
    created_at: int
    updated_at: int
    revision: int
    created_message_id: int
    updated_message_id: int


def _text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{label} must be text.")
    # NUL has different length semantics in SQLite, and isolated surrogates
    # cannot be encoded as SQLite text. Ordinary whitespace remains supported.
    if any(unicodedata.category(c) == "Cs" or
           (unicodedata.category(c) == "Cc" and not c.isspace())
           for c in value):
        raise ValidationError(f"{label} contains unsupported control characters.")
    return value


def normalize_topic(topic: str) -> str:
    """Return a bounded NFKC/casefold/whitespace key, preserving punctuation."""
    topic = _text(topic, "Topic")
    topic = unicodedata.normalize("NFKC", topic)
    topic = unicodedata.normalize("NFKC", topic.casefold())
    topic = " ".join(topic.split())
    if not topic or len(topic) > MAX_TOPIC_LENGTH:
        raise ValidationError(f"Topic must contain 1 to {MAX_TOPIC_LENGTH} characters.")
    return topic


def _integer(value: int, label: str, *, positive: bool) -> int:
    if type(value) is not int:
        raise ValidationError(f"{label} must be an integer.")
    lower = 1 if positive else -_SQLITE_MAX_INT - 1
    if not lower <= value <= _SQLITE_MAX_INT or value == 0:
        raise ValidationError(f"{label} is outside the supported range.")
    return value


def _kind(kind: str) -> str:
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValidationError("Kind must be 'fact' or 'reply'.")
    return kind


def _timestamp(now: int | float) -> int:
    if type(now) not in (int, float):
        raise ValidationError("Timestamp must be a nonnegative Unix time.")
    if isinstance(now, float) and not math.isfinite(now):
        raise ValidationError("Timestamp must be a finite Unix time.")
    if now < 0 or now > _SQLITE_MAX_INT:
        raise ValidationError("Timestamp is outside the supported range.")
    result = int(now)
    if result > _SQLITE_MAX_INT:
        raise ValidationError("Timestamp is outside the supported range.")
    return result


class TeachMemory:
    """Chat-scoped explicit memory; operational methods never commit."""

    normalize_topic = staticmethod(normalize_topic)

    def __init__(self, connection: sqlite3.Connection):
        if not isinstance(connection, sqlite3.Connection):
            raise TypeError("TeachMemory requires a sqlite3.Connection.")
        if connection.in_transaction:
            raise TeachMemoryError(
                "Initialize TeachMemory before starting an operational transaction."
            )
        self.db = connection
        self._savepoint_number = 0
        # Do not use executescript: it can implicitly commit unrelated work.
        self.db.execute("BEGIN")
        try:
            self.db.execute(f"""
                CREATE TABLE IF NOT EXISTS bexs_teach_items (
                    {_COLUMN_DEFINITIONS},
                    PRIMARY KEY (chat_id, kind, topic)
                ) WITHOUT ROWID
            """)
            self.db.execute(f"""
                CREATE TABLE IF NOT EXISTS bexs_teach_history (
                    {_COLUMN_DEFINITIONS},
                    PRIMARY KEY (chat_id, kind, topic, revision),
                    FOREIGN KEY (chat_id, kind, topic)
                        REFERENCES bexs_teach_items (chat_id, kind, topic)
                        ON DELETE CASCADE
                ) WITHOUT ROWID
            """)
            self.db.execute("""
                CREATE TABLE IF NOT EXISTS bexs_teach_counters (
                    chat_id INTEGER PRIMARY KEY CHECK (chat_id != 0),
                    created INTEGER NOT NULL DEFAULT 0 CHECK (created >= 0),
                    updated INTEGER NOT NULL DEFAULT 0 CHECK (updated >= 0),
                    forgotten INTEGER NOT NULL DEFAULT 0 CHECK (forgotten >= 0),
                    lookups INTEGER NOT NULL DEFAULT 0 CHECK (lookups >= 0),
                    hits INTEGER NOT NULL DEFAULT 0 CHECK (hits >= 0),
                    misses INTEGER NOT NULL DEFAULT 0 CHECK (misses >= 0)
                )
            """)
            # Existing tables make CREATE IF NOT EXISTS a no-op even on some
            # read-only connections. Verify writability without changing data.
            self.db.execute("UPDATE bexs_teach_counters SET created=created WHERE 0")
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _one(self, sql: str, parameters: tuple = ()) -> tuple | None:
        cursor = self.db.cursor()
        cursor.row_factory = None
        try:
            return cursor.execute(sql, parameters).fetchone()
        finally:
            cursor.close()

    def _all(self, sql: str, parameters: tuple = ()) -> list[tuple]:
        cursor = self.db.cursor()
        cursor.row_factory = None
        try:
            return cursor.execute(sql, parameters).fetchall()
        finally:
            cursor.close()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        # BEGIN is explicit even for isolation_level=None. Releasing our
        # savepoint therefore cannot commit the enclosing update transaction.
        if not self.db.in_transaction:
            self.db.execute("BEGIN")
        self._savepoint_number += 1
        name = f"bexs_teach_op_{id(self):x}_{self._savepoint_number}"
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            try:
                if self.db.in_transaction:
                    self.db.execute(f"ROLLBACK TO SAVEPOINT {name}")
                    self.db.execute(f"RELEASE SAVEPOINT {name}")
            except sqlite3.Error:
                # Preserve the original failure if SQLite itself has already
                # invalidated the transaction. The caller still owns recovery.
                pass
            raise
        else:
            self.db.execute(f"RELEASE SAVEPOINT {name}")

    @staticmethod
    def _key(chat_id: int, kind: str, topic: str) -> tuple[int, str, str]:
        return (_integer(chat_id, "Chat ID", positive=False),
                _kind(kind), normalize_topic(topic))

    def _item(self, key: tuple[int, str, str]) -> MemoryItem | None:
        row = self._one(
            f"SELECT {_COLUMNS} FROM bexs_teach_items "
            "WHERE chat_id=? AND kind=? AND topic=?", key,
        )
        return MemoryItem(*row) if row else None

    def _increment(self, chat_id: int, *, created: int = 0, updated: int = 0,
                   forgotten: int = 0, lookups: int = 0, hits: int = 0,
                   misses: int = 0) -> None:
        self.db.execute("""
            INSERT INTO bexs_teach_counters
                (chat_id, created, updated, forgotten, lookups, hits, misses)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                created = created + excluded.created,
                updated = updated + excluded.updated,
                forgotten = forgotten + excluded.forgotten,
                lookups = lookups + excluded.lookups,
                hits = hits + excluded.hits,
                misses = misses + excluded.misses
        """, (chat_id, created, updated, forgotten, lookups, hits, misses))

    @staticmethod
    def _actor(actor_id: int, message_id: int, now: int | float,
               can_override: bool) -> tuple[int, int, int]:
        actor_id = _integer(actor_id, "Actor ID", positive=True)
        message_id = _integer(message_id, "Message ID", positive=True)
        if type(can_override) is not bool:
            raise ValidationError("can_override must be a boolean.")
        return actor_id, message_id, _timestamp(now)

    @staticmethod
    def _authorize(item: MemoryItem, actor_id: int, can_override: bool) -> None:
        if actor_id != item.created_by and not can_override:
            raise PermissionDenied("Only this memory's creator or a chat admin can change it.")

    def peek(self, chat_id: int, kind: str, topic: str) -> MemoryItem | None:
        """Inspect one item without recording a lookup or starting a write."""
        return self._item(self._key(chat_id, kind, topic))

    def get(self, chat_id: int, kind: str, topic: str) -> MemoryItem | None:
        """Retrieve one exact key and count a retrieval hit or miss."""
        key = self._key(chat_id, kind, topic)
        with self._operation():
            item = self._item(key)
            self._increment(chat_id, lookups=1, hits=int(item is not None),
                            misses=int(item is None))
            return item

    def remember(self, chat_id: int, kind: str, topic: str, value: str,
                 actor_id: int, message_id: int, now: int | float,
                 can_override: bool = False) -> MemoryItem:
        """Create or replace an item, retaining its original creator.

        Identical values are authorized no-ops: they do not create a revision,
        history entry, or correction count. Five previous revisions are kept.
        """
        key = self._key(chat_id, kind, topic)
        value = _text(value, "Value").strip()
        if not value or len(value) > MAX_VALUE_LENGTH:
            raise ValidationError(f"Value must contain 1 to {MAX_VALUE_LENGTH} characters.")
        actor_id, message_id, now = self._actor(actor_id, message_id, now, can_override)
        with self._operation():
            existing = self._item(key)
            if existing is not None:
                self._authorize(existing, actor_id, can_override)
                if existing.value == value:
                    return existing
                self.db.execute(
                    f"INSERT INTO bexs_teach_history ({_COLUMNS}) "
                    f"SELECT {_COLUMNS} FROM bexs_teach_items "
                    "WHERE chat_id=? AND kind=? AND topic=?", key,
                )
                self.db.execute("""
                    UPDATE bexs_teach_items
                    SET value=?, updated_by=?, updated_at=?,
                        updated_message_id=?, revision=revision+1
                    WHERE chat_id=? AND kind=? AND topic=?
                """, (value, actor_id, now, message_id, *key))
                self.db.execute("""
                    DELETE FROM bexs_teach_history
                    WHERE chat_id=? AND kind=? AND topic=?
                    AND revision NOT IN (
                        SELECT revision FROM bexs_teach_history
                        WHERE chat_id=? AND kind=? AND topic=?
                        ORDER BY revision DESC LIMIT ?
                    )
                """, (*key, *key, MAX_HISTORY_PER_ITEM))
                self._increment(chat_id, updated=1)
            else:
                count = self._one(
                    "SELECT COUNT(*) FROM bexs_teach_items WHERE chat_id=?", (chat_id,)
                )[0]
                if count >= MAX_ITEMS_PER_CHAT:
                    raise CapacityExceeded(
                        f"This chat has {MAX_ITEMS_PER_CHAT} saved items. Forget one before adding another."
                    )
                self.db.execute(
                    f"INSERT INTO bexs_teach_items ({_COLUMNS}) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*key, value, actor_id, actor_id, now, now, 1,
                     message_id, message_id),
                )
                self._increment(chat_id, created=1)
            return self._item(key)

    def update(self, chat_id: int, kind: str, topic: str, value: str,
               actor_id: int, message_id: int, now: int | float,
               can_override: bool = False) -> MemoryItem:
        """Alias for remember's authorized create-or-replace operation."""
        return self.remember(chat_id, kind, topic, value, actor_id, message_id,
                             now, can_override)

    def forget(self, chat_id: int, kind: str, topic: str, actor_id: int,
               message_id: int, now: int | float, can_override: bool = False) -> bool:
        """Remove the active item and every stored revision; retain only counts.

        Deletion is logical SQLite deletion. Existing filesystem backups and
        previously written SQLite journals are outside this method's scope.
        """
        key = self._key(chat_id, kind, topic)
        actor_id, _, _ = self._actor(actor_id, message_id, now, can_override)
        with self._operation():
            existing = self._item(key)
            if existing is None:
                return False
            self._authorize(existing, actor_id, can_override)
            # Explicit history deletion works even if the shared connection's
            # foreign_keys setting is off; no connection-wide setting changes.
            self.db.execute(
                "DELETE FROM bexs_teach_history WHERE chat_id=? AND kind=? AND topic=?", key,
            )
            self.db.execute(
                "DELETE FROM bexs_teach_items WHERE chat_id=? AND kind=? AND topic=?", key,
            )
            self._increment(chat_id, forgotten=1)
            return True

    def list_items(self, chat_id: int, kind: str | None = None,
                   limit: int = 50) -> list[MemoryItem]:
        """List exact saved items in stable kind/topic order without counters."""
        chat_id = _integer(chat_id, "Chat ID", positive=False)
        if kind is not None:
            kind = _kind(kind)
        if type(limit) is not int or not 1 <= limit <= MAX_ITEMS_PER_CHAT:
            raise ValidationError(f"Limit must be 1 to {MAX_ITEMS_PER_CHAT}.")
        if kind is None:
            rows = self._all(
                f"SELECT {_COLUMNS} FROM bexs_teach_items WHERE chat_id=? "
                "ORDER BY kind, topic LIMIT ?", (chat_id, limit),
            )
        else:
            rows = self._all(
                f"SELECT {_COLUMNS} FROM bexs_teach_items WHERE chat_id=? AND kind=? "
                "ORDER BY topic LIMIT ?", (chat_id, kind, limit),
            )
        return [MemoryItem(*row) for row in rows]

    def stats(self, chat_id: int) -> dict[str, int]:
        """Return content-free counts. A hit means retrieval, not proven quality."""
        chat_id = _integer(chat_id, "Chat ID", positive=False)
        row = self._one(
            "SELECT created, updated, forgotten, lookups, hits, misses "
            "FROM bexs_teach_counters WHERE chat_id=?", (chat_id,),
        )
        result = dict(zip(_COUNTERS, row or (0,) * len(_COUNTERS)))
        facts, replies = self._one("""
            SELECT COALESCE(SUM(kind='fact'), 0), COALESCE(SUM(kind='reply'), 0)
            FROM bexs_teach_items WHERE chat_id=?
        """, (chat_id,))
        result.update(active=facts + replies, facts=facts, replies=replies,
                      history_entries=self._one(
                          "SELECT COUNT(*) FROM bexs_teach_history WHERE chat_id=?",
                          (chat_id,),
                      )[0])
        return result

    def find_reply(self, chat_id: int, prompt: str) -> MemoryItem | None:
        """Look up only an exact normalized taught reply, with no fuzzy match.

        Ordinary prompts that cannot fit a topic produce a counted miss rather
        than an error. Neither matched nor unmatched prompt text is logged.
        """
        chat_id = _integer(chat_id, "Chat ID", positive=False)
        if not isinstance(prompt, str):
            raise ValidationError("Prompt must be text.")
        try:
            topic = normalize_topic(prompt)
        except ValidationError:
            topic = None
        with self._operation():
            item = self._item((chat_id, "reply", topic)) if topic is not None else None
            self._increment(chat_id, lookups=1, hits=int(item is not None),
                            misses=int(item is None))
            return item
