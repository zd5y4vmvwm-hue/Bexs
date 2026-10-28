"""Storage invariants for explicitly taught Bexs facts and complete replies."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from teach_memory import (
    CapacityExceeded, MAX_HISTORY_PER_ITEM, MAX_ITEMS_PER_CHAT, MemoryItem,
    PermissionDenied, TeachMemory, TeachMemoryError, ValidationError,
    normalize_topic,
)


class TeachMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "telegram.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.memory = TeachMemory(self.db)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def remember(self, topic="plan", value="Friday", *, chat_id=-10,
                 kind="fact", actor_id=101, message_id=1, now=100,
                 can_override=False):
        return self.memory.remember(chat_id, kind, topic, value, actor_id,
                                    message_id, now, can_override)

    def test_committed_memory_and_provenance_survive_reopen(self):
        item = self.remember(now=100.9)
        self.assertIsInstance(item, MemoryItem)
        self.assertEqual(item.created_at, 100)
        self.assertTrue(self.db.in_transaction)
        self.db.commit()
        self.db.close()
        self.db = sqlite3.connect(self.path)
        self.memory = TeachMemory(self.db)
        self.assertEqual(self.memory.peek(-10, "fact", "plan"), item)
        self.assertEqual(self.memory.stats(-10)["created"], 1)

    def test_uncommitted_changes_are_not_published_or_committed_by_operations(self):
        self.remember()
        self.memory.get(-10, "fact", "plan")
        observer = sqlite3.connect(self.path)
        try:
            self.assertEqual(observer.execute(
                "SELECT COUNT(*) FROM bexs_teach_items").fetchone()[0], 0)
            self.assertEqual(observer.execute(
                "SELECT COUNT(*) FROM bexs_teach_counters").fetchone()[0], 0)
        finally:
            observer.close()
        self.db.rollback()
        self.assertIsNone(self.memory.peek(-10, "fact", "plan"))
        self.assertEqual(self.memory.stats(-10)["lookups"], 0)

    def test_operational_changes_share_and_rollback_with_telegram_offset(self):
        self.db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        self.db.execute("INSERT INTO meta VALUES ('offset', '2')")
        original = self.remember()
        self.remember("hello", "Hello crew.", kind="reply")
        self.db.commit()
        before = self.memory.stats(-10)

        self.db.execute("UPDATE meta SET value='3' WHERE key='offset'")
        self.remember(value="Saturday", message_id=2, now=200)
        self.memory.find_reply(-10, "hello")
        self.memory.forget(-10, "reply", "hello", 101, 3, 201)
        self.remember("other", "Another item", message_id=4, now=202)
        self.db.rollback()

        self.assertEqual(self.memory.peek(-10, "fact", "plan"), original)
        self.assertIsNotNone(self.memory.peek(-10, "reply", "hello"))
        self.assertIsNone(self.memory.peek(-10, "fact", "other"))
        self.assertEqual(self.memory.stats(-10), before)
        self.assertEqual(self.db.execute(
            "SELECT value FROM meta WHERE key='offset'").fetchone()[0], "2")

    def test_failed_operation_rolls_back_its_partial_history_only(self):
        original = self.remember()
        self.db.execute("CREATE TABLE unrelated (value INTEGER)")
        self.db.execute("INSERT INTO unrelated VALUES (1)")
        self.db.execute("""
            CREATE TRIGGER reject_teach_update BEFORE UPDATE ON bexs_teach_items
            BEGIN SELECT RAISE(ABORT, 'intentional test failure'); END
        """)
        self.db.commit()
        before = self.memory.stats(-10)
        self.db.execute("UPDATE unrelated SET value=2")
        with self.assertRaises(sqlite3.IntegrityError):
            self.remember(value="Saturday", message_id=2, now=200)
        self.assertTrue(self.db.in_transaction)
        self.assertEqual(self.memory.peek(-10, "fact", "plan"), original)
        self.assertEqual(self.memory.stats(-10), before)
        self.assertEqual(self.db.execute("SELECT value FROM unrelated").fetchone()[0], 2)
        self.db.commit()
        self.assertEqual(self.db.execute("SELECT value FROM unrelated").fetchone()[0], 2)

    def test_chat_and_kind_are_separate_scopes(self):
        self.remember(value="first chat fact")
        self.remember(value="second chat fact", chat_id=-20)
        self.remember(value="Complete reply.", kind="reply")
        self.assertEqual(self.memory.get(-10, "fact", "plan").value, "first chat fact")
        self.assertEqual(self.memory.get(-20, "fact", "plan").value, "second chat fact")
        self.assertEqual(self.memory.find_reply(-10, "plan").value, "Complete reply.")
        self.assertIsNone(self.memory.get(101, "fact", "plan"))
        self.assertIsNone(self.memory.find_reply(-20, "plan"))
        self.assertEqual(self.memory.stats(-20)["active"], 1)

    def test_only_creator_or_explicit_override_may_change_or_forget(self):
        original = self.remember()
        before = self.memory.stats(-10)
        with self.assertRaises(PermissionDenied):
            self.remember(value="Hijacked", actor_id=202)
        with self.assertRaises(PermissionDenied):
            self.memory.update(-10, "fact", "plan", "Hijacked", 202, 2, 200)
        with self.assertRaises(PermissionDenied):
            self.memory.forget(-10, "fact", "plan", 202, 2, 200)
        with self.assertRaises(PermissionDenied):
            self.remember(actor_id=202)  # Same content does not bypass ownership.
        self.assertEqual(self.memory.peek(-10, "fact", "plan"), original)
        self.assertEqual(self.memory.stats(-10), before)

        self.remember(value="Admin correction", actor_id=202, can_override=True)
        self.assertEqual(self.memory.peek(-10, "fact", "plan").created_by, 101)
        with self.assertRaises(PermissionDenied):
            self.remember(value="Admin no longer has override", actor_id=202)
        self.assertTrue(self.memory.forget(-10, "fact", "plan", 202, 3, 300,
                                          can_override=True))

    def test_corrections_keep_original_creator_and_both_message_sources(self):
        original = self.remember(message_id=7, now=120)
        corrected = self.remember(value="Saturday", actor_id=202,
                                  message_id=8, now=150, can_override=True)
        self.assertEqual(corrected.created_by, 101)
        self.assertEqual(corrected.created_at, 120)
        self.assertEqual(corrected.created_message_id, 7)
        self.assertEqual(corrected.updated_by, 202)
        self.assertEqual(corrected.updated_at, 150)
        self.assertEqual(corrected.updated_message_id, 8)
        self.assertEqual(corrected.revision, 2)
        history = self.db.execute("""
            SELECT value, created_by, updated_by, created_at, updated_at,
                   revision, created_message_id, updated_message_id
            FROM bexs_teach_history
        """).fetchone()
        self.assertEqual(history, (original.value, 101, 101, 120, 120, 1, 7, 7))

    def test_identical_value_is_an_authorized_noop(self):
        original = self.remember()
        self.assertEqual(self.remember(message_id=2, now=200), original)
        self.assertEqual(self.remember(actor_id=202, message_id=3, now=300,
                                       can_override=True), original)
        stats = self.memory.stats(-10)
        self.assertEqual((stats["created"], stats["updated"], stats["history_entries"]), (1, 0, 0))

    def test_unicode_case_and_whitespace_normalization_is_consistent(self):
        cases = (
            ("  ＭＥＥＴＩＮＧ\tＤＡＴＥ  ", "meeting date"),
            ("Straße", "STRASSE"),
            ("cafe\u0301", "CAFÉ"),
            ("next\u00a0\u2003steps", "next steps"),
        )
        for index, (written, read) in enumerate(cases):
            with self.subTest(written=written):
                item = self.remember(written, str(index), kind="reply")
                self.assertEqual(self.memory.find_reply(-10, read), item)
                self.assertEqual(normalize_topic(written), normalize_topic(read))
        self.assertEqual(self.memory.stats(-10)["active"], len(cases))

    def test_reply_matching_is_exact_and_facts_never_become_automatic_replies(self):
        item = self.remember("What is the plan?", "The complete taught answer.", kind="reply")
        self.remember("unused fact", "Never an automatic reply")
        self.assertEqual(self.memory.find_reply(-10, "  WHAT is the plan?  "), item)
        for prompt in ("What is the plan", "Please what is the plan?", "the plan?", "unused fact"):
            with self.subTest(prompt=prompt):
                self.assertIsNone(self.memory.find_reply(-10, prompt))

    def test_empty_and_long_regular_prompts_are_counted_misses_without_storage(self):
        for prompt in ("", " \t\n", "x" * 4096, "\x00"):
            self.assertIsNone(self.memory.find_reply(-10, prompt))
        stats = self.memory.stats(-10)
        self.assertEqual((stats["lookups"], stats["hits"], stats["misses"]), (4, 0, 4))
        self.assertEqual(stats["active"], 0)
        self.assertNotIn("x" * 4096, "\n".join(self.db.iterdump()))

    def test_stats_distinguish_changes_and_retrievals_and_inspection_is_readonly(self):
        self.assertFalse(self.db.in_transaction)
        self.assertIsNone(self.memory.peek(-10, "fact", "unknown"))
        self.assertEqual(self.memory.list_items(-10), [])
        self.assertEqual(set(self.memory.stats(-10).values()), {0})
        self.assertFalse(self.db.in_transaction)
        self.remember()
        self.remember()  # No correction.
        self.remember(value="Saturday")
        self.remember("hello", "Hello!", kind="reply")
        self.memory.get(-10, "fact", "plan")
        self.memory.get(-10, "fact", "unknown")
        self.memory.find_reply(-10, "hello")
        self.memory.find_reply(-10, "another prompt")
        self.memory.forget(-10, "fact", "plan", 101, 2, 200)
        self.assertFalse(self.memory.forget(-10, "fact", "plan", 101, 3, 300))
        expected = {
            "created": 2, "updated": 1, "forgotten": 1,
            "lookups": 4, "hits": 2, "misses": 2,
            "active": 1, "facts": 0, "replies": 1, "history_entries": 0,
        }
        self.assertEqual(self.memory.stats(-10), expected)
        self.db.commit()
        self.memory.list_items(-10)
        self.memory.peek(-10, "reply", "hello")
        self.assertEqual(self.memory.stats(-10), expected)
        self.assertFalse(self.db.in_transaction)
        self.assertNotIn("success", expected)

    def test_active_capacity_is_per_chat_and_corrections_do_not_consume_slots(self):
        for index in range(MAX_ITEMS_PER_CHAT):
            self.remember(f"item {index}", "value",
                          kind="fact" if index % 2 else "reply")
        before = self.memory.stats(-10)
        with self.assertRaises(CapacityExceeded):
            self.remember("one too many", "value")
        self.assertEqual(self.memory.stats(-10), before)
        self.remember("item 0", "corrected", kind="reply")
        self.remember("independent", "other chat", chat_id=-20)
        self.assertEqual(self.memory.stats(-10)["active"], MAX_ITEMS_PER_CHAT)
        self.memory.forget(-10, "reply", "item 0", 101, 2, 200)
        self.remember("new slot", "new value")
        self.assertEqual(self.memory.stats(-10)["active"], MAX_ITEMS_PER_CHAT)
        self.assertEqual(self.memory.stats(-20)["active"], 1)

    def test_history_is_bounded_to_five_previous_revisions(self):
        self.remember(value="version 0")
        for version in range(1, 21):
            self.remember(value=f"version {version}", message_id=version + 1,
                          now=100 + version)
        self.assertEqual(self.memory.peek(-10, "fact", "plan").revision, 21)
        rows = self.db.execute(
            "SELECT revision, value FROM bexs_teach_history ORDER BY revision"
        ).fetchall()
        self.assertEqual(len(rows), MAX_HISTORY_PER_ITEM)
        self.assertEqual(rows, [(n, f"version {n - 1}") for n in range(16, 21)])
        self.assertEqual(self.memory.stats(-10)["updated"], 20)

    def test_forget_removes_topic_values_and_all_history_without_resurrection(self):
        topic = "unique-secret-topic-239"
        self.remember(topic, "old-secret-value-318", kind="reply")
        self.remember(topic, "new-secret-value-846", kind="reply")
        self.db.commit()
        self.assertTrue(self.memory.forget(-10, "reply", topic, 101, 3, 300))
        self.db.commit()
        self.assertIsNone(self.memory.peek(-10, "reply", topic))
        self.assertIsNone(self.memory.find_reply(-10, topic))
        self.assertEqual(self.memory.list_items(-10), [])
        dump = "\n".join(self.db.iterdump())
        for value in (topic, "old-secret-value-318", "new-secret-value-846"):
            self.assertNotIn(value, dump)
        self.assertEqual(self.memory.stats(-10)["history_entries"], 0)
        self.assertEqual(self.memory.stats(-10)["forgotten"], 1)
        recreated = self.remember(topic, "fresh value", kind="reply", actor_id=202)
        self.assertEqual((recreated.revision, recreated.created_by), (1, 202))
        self.assertEqual(self.memory.stats(-10)["history_entries"], 0)

    def test_forget_one_kind_or_chat_does_not_delete_another(self):
        self.remember()
        self.remember(kind="reply")
        self.remember(chat_id=-20)
        self.memory.forget(-10, "fact", "plan", 101, 2, 200)
        self.assertIsNotNone(self.memory.peek(-10, "reply", "plan"))
        self.assertIsNotNone(self.memory.peek(-20, "fact", "plan"))

    def test_bounded_list_is_stable_and_kind_filtered(self):
        for topic in ("c", "a", "b"):
            self.remember(topic, topic)
        self.remember("a", "reply", kind="reply")
        self.assertEqual([item.topic for item in self.memory.list_items(-10, "fact", 2)], ["a", "b"])
        self.assertEqual([item.kind for item in self.memory.list_items(-10, "reply")], ["reply"])
        self.assertEqual(len(self.memory.list_items(-10, limit=500)), 4)
        for limit in (0, -1, 501, True, 2.5, "2"):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                self.memory.list_items(-10, limit=limit)

    def test_invalid_inputs_cannot_change_data_or_counters(self):
        invalid = (
            {"topic": ""}, {"topic": " \t\n"}, {"topic": "x" * 121},
            {"topic": None}, {"topic": "a\x00b"}, {"topic": "\ud800"},
            {"value": ""}, {"value": " \n\t"}, {"value": "x" * 1201},
            {"value": None}, {"value": "a\x00b"}, {"value": "\ud800"},
            {"kind": "unknown"}, {"kind": []}, {"chat_id": 0},
            {"chat_id": True}, {"chat_id": 1 << 63}, {"chat_id": -(1 << 63) - 1},
            {"actor_id": 0}, {"actor_id": -1}, {"actor_id": True},
            {"actor_id": 1 << 63}, {"message_id": 0}, {"message_id": True},
            {"now": -0.5}, {"now": float("nan")}, {"now": float("inf")},
            {"now": True}, {"now": "100"}, {"now": float(1 << 63)},
            {"can_override": 1}, {"can_override": "false"},
        )
        for values in invalid:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                self.remember(**values)
        self.assertEqual(set(self.memory.stats(-10).values()), {0})
        self.assertFalse(self.db.in_transaction)

    def test_boundary_lengths_and_sql_payloads_remain_literal_data(self):
        item = self.remember("x" * 120, "y" * 1200)
        self.assertEqual(len(item.topic), 120)
        self.assertEqual(len(item.value), 1200)
        topic = "x'); DROP TABLE bexs_teach_items; --"
        value = "Literal reply: '); DELETE FROM chats; --"
        item = self.remember(topic, value, kind="reply")
        self.assertEqual(self.memory.find_reply(-10, topic), item)
        self.assertEqual(self.memory.stats(-10)["active"], 2)

    def test_constructor_is_additive_and_refuses_pending_transaction(self):
        self.db.execute("CREATE TABLE original_data (value TEXT)")
        self.db.execute("INSERT INTO original_data VALUES ('untouched seed state')")
        self.db.commit()
        TeachMemory(self.db)
        self.assertEqual(self.db.execute("SELECT value FROM original_data").fetchone()[0],
                         "untouched seed state")
        self.db.execute("UPDATE original_data SET value='pending change'")
        with self.assertRaises(TeachMemoryError):
            TeachMemory(self.db)
        self.assertTrue(self.db.in_transaction)
        self.db.rollback()
        self.assertEqual(self.db.execute("SELECT value FROM original_data").fetchone()[0],
                         "untouched seed state")

    def test_constructor_rejects_readonly_even_when_tables_already_exist(self):
        self.remember()
        self.db.commit()
        readonly = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                TeachMemory(readonly)
            self.assertFalse(readonly.in_transaction)
        finally:
            readonly.close()

    def test_explicit_transactions_work_with_autocommit_isolation_mode(self):
        automatic = sqlite3.connect(":memory:", isolation_level=None)
        try:
            memory = TeachMemory(automatic)
            memory.remember(-10, "fact", "plan", "Friday", 101, 1, 100)
            self.assertTrue(automatic.in_transaction)
            automatic.rollback()
            self.assertIsNone(memory.peek(-10, "fact", "plan"))
            self.assertEqual(memory.stats(-10)["created"], 0)
        finally:
            automatic.close()

    def test_shared_connection_custom_row_factory_is_preserved(self):
        def dictionary_rows(cursor, row):
            return dict(zip((c[0] for c in cursor.description), row))

        self.db.row_factory = dictionary_rows
        item = self.remember()
        self.assertEqual(self.memory.get(-10, "fact", "plan"), item)
        self.assertEqual(self.memory.list_items(-10), [item])
        self.assertEqual(self.memory.stats(-10)["active"], 1)
        self.assertIs(self.db.row_factory, dictionary_rows)
        self.assertEqual(self.db.execute("SELECT 1 AS original_factory").fetchone(),
                         {"original_factory": 1})


if __name__ == "__main__":
    unittest.main()
