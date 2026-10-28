"""Read-only Teach Bexs inspection and isolated, active-only experiment copies.

This is an adapter for a future integration with the existing labs, not a web
server or a live-sync implementation. The caller must authorize access to the
requested chat before using the scoped inspector or clone function.

Public aggregates require an explicit server-configured set of eligible group
IDs. Never populate that set from untrusted public request parameters. Private
chats are ineligible; the default set is empty. There are no opt-in leaderboards
or public memory examples in this adapter.

Inspection uses a SQLite read transaction, mode=ro, and query_only. It neither
initializes TeachMemory on the source nor updates retrieval counters. Cloning
creates only the TeachMemory tables and copies current items for one chat;
history, counters, legacy word memory, credentials, and bot offsets are omitted.
Existing experiment files and old filesystem backups are not altered.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import fields
import json
import os
from pathlib import Path
import sqlite3
from typing import Iterable, Iterator

from teach_memory import MemoryItem, TeachMemory


MAX_INSPECT_REVISIONS = 100
_ITEM_FIELDS = tuple(field.name for field in fields(MemoryItem))
_ITEM_COLUMNS = ", ".join(_ITEM_FIELDS)
_COUNTER_FIELDS = ("created", "updated", "forgotten", "lookups", "hits", "misses")
_PUBLIC_FIELDS = ("active", "facts", "replies", *_COUNTER_FIELDS)
_EXPECTED_COLUMNS = {
    "bexs_teach_items": frozenset(_ITEM_FIELDS),
    "bexs_teach_history": frozenset(_ITEM_FIELDS),
    "bexs_teach_counters": frozenset(("chat_id", *_COUNTER_FIELDS)),
}
_SQLITE_MIN_INT = -(1 << 63)
_SQLITE_MAX_INT = (1 << 63) - 1


class TeachLabUnavailable(RuntimeError):
    """A source is unavailable or does not contain the supported Teach schema."""


def _chat_id(value: int, *, group_only: bool = False) -> int:
    if (type(value) is not int or value == 0 or
            not _SQLITE_MIN_INT <= value <= _SQLITE_MAX_INT):
        raise ValueError("Chat ID must be an exact nonzero SQLite-range integer.")
    if group_only and value >= 0:
        raise ValueError("Public statistics accept only eligible negative group IDs.")
    return value


def _eligible_groups(values: Iterable[int]) -> tuple[int, ...]:
    try:
        return tuple(sorted({_chat_id(value, group_only=True) for value in values}))
    except TypeError as error:
        raise ValueError("Eligible group IDs must be an iterable of negative integers.") from error


@contextmanager
def _read_snapshot(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    # as_uri escapes '?' and '#' in ordinary filenames. Do not use immutable=1:
    # it could ignore committed, uncheckpointed WAL data in a live source.
    uri = Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"
    db = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        yield db
    finally:
        if db.in_transaction:
            db.rollback()
        db.close()


def _has_schema(db: sqlite3.Connection) -> bool:
    names = tuple(_EXPECTED_COLUMNS)
    marks = ", ".join("?" for _ in names)
    tables = {
        row["name"]: row["type"]
        for row in db.execute(
            f"SELECT name, type FROM sqlite_master WHERE name IN ({marks})", names
        )
    }
    for table, columns in _EXPECTED_COLUMNS.items():
        if tables.get(table) != "table":
            return False
        # Table identifiers are fixed above, never supplied by a caller.
        present = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if not columns <= present:
            return False
    return True


def _record(row: sqlite3.Row) -> dict:
    result = {column: row[column] for column in _ITEM_FIELDS}
    # Reject malformed/foreign data rather than emitting bytes or non-JSON
    # numeric values from SQLite's permissive storage types.
    for column, value in result.items():
        expected = str if column in {"kind", "topic", "value"} else int
        if type(value) is not expected:
            raise TeachLabUnavailable("Teach memory contains unsupported value types.")
    return result


def _counts(db: sqlite3.Connection, chat_ids: tuple[int, ...], *, history: bool) -> dict:
    result = dict.fromkeys(_PUBLIC_FIELDS, 0)
    if history:
        result["history_entries"] = 0
    # Small batches also support SQLite builds with the historical 999 bound
    # parameter limit. All batches share the same read transaction/snapshot.
    for start in range(0, len(chat_ids), 900):
        batch = chat_ids[start:start + 900]
        marks = ", ".join("?" for _ in batch)
        active = db.execute(
            "SELECT COUNT(*) AS active, "
            "COALESCE(SUM(kind='fact'), 0) AS facts, "
            "COALESCE(SUM(kind='reply'), 0) AS replies "
            f"FROM bexs_teach_items WHERE chat_id IN ({marks})", batch,
        ).fetchone()
        for name in ("active", "facts", "replies"):
            result[name] += active[name]
        counter_columns = ", ".join(
            f"COALESCE(SUM({name}), 0) AS {name}" for name in _COUNTER_FIELDS
        )
        counters = db.execute(
            f"SELECT {counter_columns} FROM bexs_teach_counters "
            f"WHERE chat_id IN ({marks})", batch,
        ).fetchone()
        for name in _COUNTER_FIELDS:
            result[name] += counters[name]
        if history:
            # An orphan history row is not made visible even in a damaged DB.
            result["history_entries"] += db.execute(
                "SELECT COUNT(*) FROM bexs_teach_history h "
                "JOIN bexs_teach_items i USING (chat_id, kind, topic) "
                f"WHERE h.chat_id IN ({marks})", batch,
            ).fetchone()[0]
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise TeachLabUnavailable("Teach memory contains unsupported counter values.")
    return result


def _unsupported(chat_id: int | None = None) -> dict:
    result = {"supported": False, "counts": {}}
    if chat_id is not None:
        result.update(chat_id=chat_id, items=[], revisions=[],
                      revision_limit=MAX_INSPECT_REVISIONS, revisions_truncated=False)
    return result


def inspect_teach_memory(db_path: str | os.PathLike[str], chat_id: int) -> dict:
    """Inspect one caller-authorized chat without writes or schema migration.

    Returns active items, at most MAX_INSPECT_REVISIONS prior revisions, and
    counts from one SQLite snapshot. Missing, unreadable, incompatible, or
    malformed sources return supported:false with empty content. Invalid IDs
    raise ValueError before the database is opened. A retrieval hit is not a
    quality rating; counters retain that distinction from TeachMemory.
    """
    chat_id = _chat_id(chat_id)
    try:
        with _read_snapshot(db_path) as db:
            if not _has_schema(db):
                return _unsupported(chat_id)
            items = [
                _record(row) for row in db.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM bexs_teach_items "
                    "WHERE chat_id=? ORDER BY kind, topic", (chat_id,),
                )
            ]
            history_columns = ", ".join(f"h.{column}" for column in _ITEM_FIELDS)
            revisions = [
                _record(row) for row in db.execute(
                    f"SELECT {history_columns} FROM bexs_teach_history h "
                    "JOIN bexs_teach_items i USING (chat_id, kind, topic) "
                    "WHERE h.chat_id=? "
                    "ORDER BY h.updated_at DESC, h.kind, h.topic, h.revision DESC LIMIT ?",
                    (chat_id, MAX_INSPECT_REVISIONS),
                )
            ]
            counts = _counts(db, (chat_id,), history=True)
            return {
                "supported": True, "chat_id": chat_id, "items": items,
                "revisions": revisions, "counts": counts,
                "revision_limit": MAX_INSPECT_REVISIONS,
                "revisions_truncated": counts["history_entries"] > len(revisions),
            }
    except (OSError, sqlite3.DatabaseError, TeachLabUnavailable):
        return _unsupported(chat_id)


def public_teach_stats(db_path: str | os.PathLike[str],
                       eligible_group_chat_ids: Iterable[int] = ()) -> dict:
    """Return aggregates only for explicitly eligible groups; default is none.

    Eligibility must come from trusted server configuration, not a public
    request. Positive/private IDs are rejected. No IDs, topics, values, users,
    timestamps, histories, or leaderboard entries are returned. Opt-in
    leaderboards and public examples are not implemented here.
    """
    groups = _eligible_groups(eligible_group_chat_ids)
    try:
        with _read_snapshot(db_path) as db:
            if not _has_schema(db):
                return _unsupported()
            return {"supported": True, "counts": _counts(db, groups, history=False)}
    except (OSError, sqlite3.DatabaseError, TeachLabUnavailable):
        return _unsupported()


def clone_teach_chat(source_path: str | os.PathLike[str],
                     destination_path: str | os.PathLike[str], chat_id: int) -> dict:
    """Create a new Teach-only database containing one chat's current lessons.

    All attribution, source-message IDs, timestamps, and current revision
    numbers are preserved exactly. Historical revisions and event/lookup
    counters start empty. Reading never initializes or migrates the source.
    Existing destinations are refused with FileExistsError; unsupported sources
    raise TeachLabUnavailable before creating a destination. The caller owns
    authorization, experiment registration, and retention of older snapshots.
    """
    chat_id = _chat_id(chat_id)
    try:
        with _read_snapshot(source_path) as source:
            if not _has_schema(source):
                raise TeachLabUnavailable("Source does not contain supported Teach memory.")
            items = [
                _record(row) for row in source.execute(
                    f"SELECT {_ITEM_COLUMNS} FROM bexs_teach_items "
                    "WHERE chat_id=? ORDER BY kind, topic", (chat_id,),
                )
            ]
    except (OSError, sqlite3.DatabaseError) as error:
        raise TeachLabUnavailable("Cannot read the Teach memory source.") from error

    destination = Path(destination_path).expanduser()
    # Reserve a new, private file atomically. No SQLite connection is allowed to
    # silently open an existing destination, including a symlink.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    identity = os.fstat(fd)
    os.close(fd)
    try:
        with sqlite3.connect(destination) as experiment:
            TeachMemory(experiment)  # Constructor is used only on this new file.
            marks = ", ".join("?" for _ in _ITEM_FIELDS)
            experiment.executemany(
                f"INSERT INTO bexs_teach_items ({_ITEM_COLUMNS}) VALUES ({marks})",
                [tuple(item[column] for column in _ITEM_FIELDS) for item in items],
            )
        # The connection context manager commits but does not close.
        experiment.close()
    except BaseException:
        if "experiment" in locals():
            experiment.close()
        try:
            current = destination.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                destination.unlink()
        except FileNotFoundError:
            pass
        raise
    return {"copied_items": len(items), "history_copied": 0, "counters_copied": 0}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=("Read-only Teach Bexs inspection. Public output contains only "
                     "explicitly selected groups' aggregate counts; no leaderboards."),
    )
    parser.add_argument("database", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--chat", type=int, help="Inspect one authorized chat ID locally.")
    mode.add_argument("--summary", action="store_true", help="Print public-safe aggregates.")
    parser.add_argument(
        "--public-group", type=int, action="append", default=[], metavar="ID",
        help="Eligible negative group ID for --summary; repeat for multiple groups.",
    )
    args = parser.parse_args(argv)
    if args.public_group and not args.summary:
        parser.error("--public-group can only be used with --summary")
    try:
        if args.summary:
            result = public_teach_stats(args.database, args.public_group)
        else:
            result = inspect_teach_memory(args.database, args.chat)
    except ValueError as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
    return 0 if result["supported"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
