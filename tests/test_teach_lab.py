"""Offline privacy and snapshot lifecycle checks for the future lab adapter."""
import contextlib
from dataclasses import asdict
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import teach_lab
from teach_lab import (
    MAX_INSPECT_REVISIONS, TeachLabUnavailable, clone_teach_chat,
    inspect_teach_memory, public_teach_stats,
)
from teach_memory import TeachMemory


GROUP = -100123
OTHER_GROUP = -100456
PRIVATE = 731
OWNER = 11
ADMIN = 22


class TeachLabTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Literal URL punctuation exercises correct SQLite URI escaping.
        self.path = self.root / "live?#memory.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.memory = TeachMemory(self.db)
        self.message = 0
        self.time = 1000
        self.save(GROUP, "fact", "Game night", "SCOPED-SUPERSEDED-FACT")
        self.current = self.save(GROUP, "fact", "Game night", "SCOPED-ACTIVE-FACT",
                                 actor=ADMIN, can_override=True)
        self.save(GROUP, "reply", "hello", "SCOPED-ACTIVE-REPLY")
        self.save(GROUP, "fact", "removed", "SCOPED-FORGOTTEN-FACT")
        self.memory.forget(GROUP, "fact", "removed", OWNER, 400, 2000)
        self.memory.get(GROUP, "fact", "game night")
        self.memory.get(GROUP, "fact", "not saved")
        self.save(OTHER_GROUP, "fact", "Game night", "OTHER-GROUP-OLD")
        self.save(OTHER_GROUP, "fact", "Game night", "OTHER-GROUP-ACTIVE")
        self.save(PRIVATE, "fact", "Game night", "PRIVATE-CHAT-FACT")
        self.save(PRIVATE, "reply", "hello", "PRIVATE-CHAT-REPLY")
        self.db.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, state TEXT)")
        self.db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT INTO chats VALUES (?, ?)", (GROUP, "LEGACY-WORD-MEMORY"))
        self.db.execute("INSERT INTO meta VALUES ('token', 'TOKEN-SENTINEL-NEVER-COPY')")
        self.db.execute("INSERT INTO meta VALUES ('offset', '987654')")
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def save(self, chat, kind, topic, value, *, actor=OWNER, can_override=False):
        self.message += 1
        self.time += 1
        return self.memory.remember(chat, kind, topic, value, actor, self.message,
                                    self.time, can_override=can_override)

    def source_state(self):
        # The main database and WAL are the durable source content. SQLite may
        # update shared-memory reader bookkeeping even for a read-only client.
        files = {}
        for path in (self.path, Path(str(self.path) + "-wal")):
            if path.exists():
                files[path.name] = (hashlib.sha256(path.read_bytes()).hexdigest(),
                                    path.stat().st_mtime_ns)
        return files, tuple(self.db.iterdump())

    def test_inspection_is_scoped_json_safe_and_preserves_attribution(self):
        result = inspect_teach_memory(self.path, GROUP)
        self.assertTrue(result["supported"])
        self.assertEqual(result["chat_id"], GROUP)
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(len(result["revisions"]), 1)
        self.assertEqual(result["counts"], self.memory.stats(GROUP))
        self.assertFalse(result["revisions_truncated"])
        fact = next(item for item in result["items"] if item["kind"] == "fact")
        self.assertEqual(fact, asdict(self.current))
        self.assertEqual(fact["created_by"], OWNER)
        self.assertEqual(fact["updated_by"], ADMIN)
        self.assertEqual(fact["revision"], 2)
        self.assertEqual(result["revisions"][0]["value"], "SCOPED-SUPERSEDED-FACT")
        for row in result["items"] + result["revisions"]:
            self.assertEqual(row["chat_id"], GROUP)
        encoded = json.dumps(result, allow_nan=False)
        for forbidden in ("PRIVATE-CHAT", "OTHER-GROUP", "SCOPED-FORGOTTEN-FACT",
                          "TOKEN-SENTINEL", "LEGACY-WORD-MEMORY"):
            self.assertNotIn(forbidden, encoded)

    def test_inspection_and_summary_cannot_write_or_increment_lookups(self):
        before = self.source_state()
        statements = []
        connect = sqlite3.connect

        def tracked_connect(*args, **kwargs):
            self.assertTrue(kwargs.get("uri"))
            self.assertIn("mode=ro", args[0])
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        with patch("teach_lab.sqlite3.connect", side_effect=tracked_connect):
            self.assertTrue(inspect_teach_memory(self.path, GROUP)["supported"])
            self.assertTrue(public_teach_stats(self.path, (GROUP,))["supported"])
        self.assertEqual(before, self.source_state())
        self.assertEqual(sum(sql == "BEGIN" for sql in statements), 2)
        self.assertEqual(sum(sql == "PRAGMA query_only=ON" for sql in statements), 2)
        self.assertFalse(any(sql.lstrip().split()[0].upper() in
                             {"INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "DROP"}
                             for sql in statements))
        with teach_lab._read_snapshot(self.path) as readonly:
            self.assertEqual(readonly.execute("PRAGMA query_only").fetchone()[0], 1)
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("UPDATE meta SET value='changed' WHERE key='offset'")
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("CREATE TEMP TABLE write_attempt (value INTEGER)")

    def test_missing_legacy_partial_or_corrupt_source_creates_and_migrates_nothing(self):
        missing = self.root / "missing-parent" / "missing.sqlite3"
        legacy = self.root / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.execute("CREATE TABLE chats (id INTEGER PRIMARY KEY, state TEXT)")
            db.execute("INSERT INTO chats VALUES (1, 'KEEP-LEGACY')")
        partial = self.root / "partial.sqlite3"
        with sqlite3.connect(partial) as db:
            db.execute("CREATE TABLE bexs_teach_items (chat_id INTEGER)")
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a database, do not replace it")
        for path in (missing, legacy, partial, corrupt):
            with self.subTest(path=path.name):
                before = path.read_bytes() if path.exists() else None
                result = inspect_teach_memory(path, GROUP)
                self.assertFalse(result["supported"])
                self.assertEqual(result["items"], [])
                self.assertEqual(result["revisions"], [])
                self.assertEqual(result["counts"], {})
                self.assertEqual(public_teach_stats(path, (GROUP,)),
                                 {"supported": False, "counts": {}})
                if before is not None:
                    self.assertEqual(path.read_bytes(), before)
                else:
                    self.assertFalse(path.exists())
                    self.assertFalse(path.parent.exists())
                destination = self.root / (path.name + ".clone")
                with self.assertRaises(TeachLabUnavailable):
                    clone_teach_chat(path, destination, GROUP)
                self.assertFalse(destination.exists())

    def test_chat_id_must_be_an_exact_nonzero_in_range_integer_before_io(self):
        class IntSubclass(int):
            pass

        invalid = (True, False, 0, "-100123", -100123.0, None, 1 << 63,
                   -(1 << 63) - 1, IntSubclass(GROUP))
        missing = self.root / "must-stay-absent.sqlite3"
        for chat in invalid:
            with self.subTest(chat=chat):
                with self.assertRaises(ValueError):
                    inspect_teach_memory(missing, chat)
                with self.assertRaises(ValueError):
                    clone_teach_chat(self.path, missing, chat)
                self.assertFalse(missing.exists())
        for chat in (-(1 << 63), (1 << 63) - 1, PRIVATE):
            with self.subTest(valid=chat):
                self.assertTrue(inspect_teach_memory(self.path, chat)["supported"])

    def test_unknown_chat_is_empty_and_revision_output_is_bounded(self):
        empty = inspect_teach_memory(self.path, -100999)
        self.assertTrue(empty["supported"])
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["revisions"], [])
        self.assertTrue(all(value == 0 for value in empty["counts"].values()))
        for topic in range(22):
            for revision in range(7):
                self.save(GROUP, "fact", f"bounded {topic}", f"version {revision}")
        self.db.commit()
        result = inspect_teach_memory(self.path, GROUP)
        self.assertEqual(len(result["revisions"]), MAX_INSPECT_REVISIONS)
        self.assertGreater(result["counts"]["history_entries"], MAX_INSPECT_REVISIONS)
        self.assertTrue(result["revisions_truncated"])
        self.assertTrue(all(row["chat_id"] == GROUP for row in result["revisions"]))

    def test_public_summary_defaults_to_zero_and_excludes_private_and_unselected_groups(self):
        result = public_teach_stats(self.path)
        self.assertTrue(result["supported"])
        self.assertTrue(all(value == 0 for value in result["counts"].values()))
        result = public_teach_stats(self.path, (GROUP, GROUP))
        expected = self.memory.stats(GROUP)
        self.assertEqual(result["counts"],
                         {key: value for key, value in expected.items() if key != "history_entries"})
        self.assertEqual(set(result), {"supported", "counts"})
        self.assertEqual(result["counts"]["facts"], 1)
        self.assertEqual(result["counts"]["replies"], 1)
        self.assertEqual(result["counts"]["lookups"], 2)
        encoded = json.dumps(result)
        for forbidden in ("chat_id", "user", "topic", "value", "created_by", "history",
                          "timestamp", str(GROUP), str(OTHER_GROUP), str(PRIVATE),
                          "SCOPED", "PRIVATE", "OTHER-GROUP"):
            self.assertNotIn(forbidden, encoded)
        both = public_teach_stats(self.path, [GROUP, OTHER_GROUP])["counts"]
        self.assertEqual(both["active"], 3)
        self.assertEqual(both["facts"], 2)
        self.assertEqual(both["replies"], 1)

    def test_public_eligibility_rejects_private_and_invalid_ids(self):
        for groups in ([PRIVATE], [GROUP, PRIVATE], [0], [True], [str(GROUP)],
                       [float(GROUP)], [-(1 << 63) - 1], None):
            with self.subTest(groups=groups):
                with self.assertRaises(ValueError):
                    public_teach_stats(self.path, groups)

    def test_clone_copies_only_active_scoped_items_and_retains_exact_attribution(self):
        before = self.source_state()
        destination = self.root / "experiment.sqlite3"
        result = clone_teach_chat(self.path, destination, GROUP)
        self.assertEqual(result, {"copied_items": 2, "history_copied": 0, "counters_copied": 0})
        self.assertEqual(self.source_state(), before)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        copied = inspect_teach_memory(destination, GROUP)
        self.assertEqual(copied["items"], inspect_teach_memory(self.path, GROUP)["items"])
        self.assertEqual(copied["revisions"], [])
        self.assertEqual(copied["counts"]["active"], 2)
        for key in ("created", "updated", "forgotten", "lookups", "hits", "misses", "history_entries"):
            self.assertEqual(copied["counts"][key], 0)
        with sqlite3.connect(destination) as experiment:
            tables = {row[0] for row in experiment.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            self.assertEqual(tables, {"bexs_teach_items", "bexs_teach_history", "bexs_teach_counters"})
            self.assertEqual(experiment.execute("SELECT COUNT(*) FROM bexs_teach_counters").fetchone()[0], 0)
        self.assertEqual(inspect_teach_memory(destination, OTHER_GROUP)["items"], [])
        self.assertEqual(inspect_teach_memory(destination, PRIVATE)["items"], [])
        content = destination.read_bytes()
        for forbidden in (b"SCOPED-SUPERSEDED-FACT", b"SCOPED-FORGOTTEN-FACT",
                          b"OTHER-GROUP", b"PRIVATE-CHAT", b"TOKEN-SENTINEL", b"LEGACY-WORD-MEMORY"):
            self.assertNotIn(forbidden, content)

    def test_clone_refuses_existing_file_or_symlink_without_changing_them(self):
        existing = self.root / "existing.sqlite3"
        existing.write_bytes(b"DO-NOT-OVERWRITE")
        symlink = self.root / "existing-link.sqlite3"
        symlink.symlink_to(existing)
        source_before = self.source_state()
        for path in (existing, symlink, self.path):
            with self.subTest(path=path.name):
                with self.assertRaises(FileExistsError):
                    clone_teach_chat(self.path, path, GROUP)
                self.assertEqual(existing.read_bytes(), b"DO-NOT-OVERWRITE")
                self.assertTrue(symlink.is_symlink())
                self.assertEqual(self.source_state(), source_before)

    def test_failed_clone_cleans_its_new_file_without_affecting_source(self):
        destination = self.root / "failed-experiment.sqlite3"
        before = self.source_state()
        with patch("teach_lab.TeachMemory", side_effect=RuntimeError("simulated disk failure")):
            with self.assertRaisesRegex(RuntimeError, "simulated disk failure"):
                clone_teach_chat(self.path, destination, GROUP)
        self.assertFalse(destination.exists())
        self.assertEqual(self.source_state(), before)

    def test_future_clones_omit_forgotten_and_superseded_content(self):
        first = self.root / "first-experiment.sqlite3"
        second = self.root / "second-experiment.sqlite3"
        clone_teach_chat(self.path, first, GROUP)
        first_bytes = first.read_bytes()
        self.memory.forget(GROUP, "reply", "hello", OWNER, 401, 2001)
        self.save(GROUP, "fact", "game night", "NEW-CURRENT-ACTIVE-FACT")
        self.db.commit()
        clone_teach_chat(self.path, second, GROUP)
        copied = inspect_teach_memory(second, GROUP)
        self.assertEqual(len(copied["items"]), 1)
        self.assertEqual(copied["items"][0]["value"], "NEW-CURRENT-ACTIVE-FACT")
        self.assertEqual(copied["revisions"], [])
        for forbidden in (b"SCOPED-ACTIVE-REPLY", b"SCOPED-ACTIVE-FACT", b"SCOPED-SUPERSEDED-FACT"):
            self.assertNotIn(forbidden, second.read_bytes())
        # The adapter makes no false promise to rewrite older snapshots.
        self.assertEqual(first.read_bytes(), first_bytes)
        with sqlite3.connect(second) as experiment:
            isolated = TeachMemory(experiment)
            isolated.remember(GROUP, "fact", "game night", "EXPERIMENT-ONLY", OWNER, 402, 2002)
            experiment.commit()
        self.assertEqual(self.memory.peek(GROUP, "fact", "game night").value, "NEW-CURRENT-ACTIVE-FACT")

    def test_inspection_and_clone_use_consistent_read_snapshots_during_live_writes(self):
        old = inspect_teach_memory(self.path, GROUP)
        has_schema = teach_lab._has_schema

        def write_after_snapshot_is_open(reader):
            supported = has_schema(reader)
            self.save(GROUP, "fact", "concurrent lesson", "AFTER-SNAPSHOT-VALUE")
            self.db.commit()
            return supported

        with patch("teach_lab._has_schema", side_effect=write_after_snapshot_is_open):
            snapshot = inspect_teach_memory(self.path, GROUP)
        self.assertEqual(snapshot, old)
        self.assertIn("AFTER-SNAPSHOT-VALUE", json.dumps(inspect_teach_memory(self.path, GROUP)))

        destination = self.root / "concurrent-experiment.sqlite3"

        def correct_after_snapshot_is_open(reader):
            supported = has_schema(reader)
            self.save(GROUP, "fact", "concurrent lesson", "NEWER-COMMITTED-VALUE")
            self.db.commit()
            return supported

        with patch("teach_lab._has_schema", side_effect=correct_after_snapshot_is_open):
            clone_teach_chat(self.path, destination, GROUP)
        self.assertIn("AFTER-SNAPSHOT-VALUE", json.dumps(inspect_teach_memory(destination, GROUP)))
        self.assertNotIn("NEWER-COMMITTED-VALUE", destination.read_bytes().decode("latin1"))
        self.assertEqual(self.memory.peek(GROUP, "fact", "concurrent lesson").value,
                         "NEWER-COMMITTED-VALUE")

    def test_cli_modes_are_read_only_and_summary_needs_explicit_group_selection(self):
        before = self.source_state()

        def invoke(arguments):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                status = teach_lab.main([str(self.path), *arguments])
            return status, json.loads(output.getvalue())

        status, scoped = invoke(["--chat", str(GROUP)])
        self.assertEqual(status, 0)
        self.assertEqual(scoped["counts"]["active"], 2)
        status, default = invoke(["--summary"])
        self.assertEqual(status, 0)
        self.assertTrue(all(value == 0 for value in default["counts"].values()))
        status, selected = invoke(["--summary", "--public-group", str(GROUP),
                                   "--public-group", str(OTHER_GROUP)])
        self.assertEqual(status, 0)
        self.assertEqual(selected["counts"]["active"], 3)
        for arguments in (["--summary", "--public-group", str(PRIVATE)],
                          ["--chat", str(GROUP), "--public-group", str(GROUP)],
                          ["--chat", "0"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    invoke(arguments)
                self.assertEqual(error.exception.code, 2)
        self.assertEqual(before, self.source_state())


if __name__ == "__main__":
    unittest.main()
