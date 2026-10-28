"""Offline behavioral checks for Teach Bexs through Bot.process.

Each test owns a temporary SQLite database. The fake Telegram client permits
only membership lookups; no test can dispatch a Telegram message or poll updates.
"""
import copy
import hashlib
import random
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import Bot, Config, ROOT
from storage import Store
from telegram_api import APIError


SEED = {
    "model": {"": ["Good"], "Good": [], "good": ["morning"],
              "morning": ["crew"], "crew": [0]},
    "is_learning": True,
    "torrent_level": 8,
    "restricted_mode": False,
    "stickers": [],
    "animations": [],
}
PRIVATE = 17
GROUP = -100123
OTHER_GROUP = -100456
IDENTITY = {"id": 99, "username": "bexs_example_bot"}


class MembershipOnlyAPI:
    def __init__(self):
        self.roles = {}
        self.calls = []
        self.fail = False

    def call(self, method, **payload):
        if method != "getChatMember":
            raise AssertionError("Offline test attempted Telegram method " + method)
        self.calls.append((method, payload))
        if self.fail:
            raise APIError(503)
        return {"status": self.roles.get(payload["user_id"], "member")}


class TeachBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "telegram.sqlite3"
        self.seed = copy.deepcopy(SEED)
        self.api = MembershipOnlyAPI()
        self.config = Config(allowed_chats={PRIVATE, GROUP, OTHER_GROUP},
                             chance=0, cooldown=3)
        self.now = 1000
        self.uid = 0
        self._open()

    def _open(self):
        self.store = Store(self.path, self.seed, set(), chance=0)
        self.bot = Bot(self.api, self.store, self.config, dict(IDENTITY),
                       random.Random(5), clock=lambda: self.now)

    def restart(self):
        self.store.close()
        self._open()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def event(self, text, *, chat=PRIVATE, user=PRIVATE, advance=5, **fields):
        self.uid += 1
        self.now += advance
        return {
            "update_id": self.uid,
            "message": {
                "message_id": self.uid,
                "chat": {"id": chat,
                         "type": "private" if chat > 0 else "supergroup"},
                "from": {"id": user, "is_bot": False},
                "text": text,
                **fields,
            },
        }

    def send(self, text, **kwargs):
        return self.bot.process(self.event(text, **kwargs))

    @staticmethod
    def text(response):
        return response[1].get("text", "") if response else ""

    def assert_reply(self, response):
        self.assertIsNotNone(response)
        self.assertEqual(response[0], "sendMessage")
        self.assertTrue(response[1]["text"])

    def model(self, chat=PRIVATE):
        return copy.deepcopy(self.store.get(chat)[0].model)

    def test_fact_lifecycle_aliases_correction_restart_and_forget(self):
        self.assert_reply(self.send("bexs remember Game Night = Friday at 8 pm"))
        self.assertIn("Friday at 8 pm", self.text(self.send("bexs recall game night")))
        self.assert_reply(self.send("/remember GAME NIGHT = Saturday at 8 pm"))
        self.restart()
        recalled = self.text(self.send("/recall game   night"))
        self.assertIn("Saturday at 8 pm", recalled)
        self.assertNotIn("Friday at 8 pm", recalled)
        self.assert_reply(self.send("bexs forget GAME NIGHT"))
        self.restart()
        self.assertNotIn("Saturday at 8 pm", self.text(self.send("/recall game night")))
        self.assertNotIn("game night", self.text(self.send("/memories fact")).casefold())

    def test_replayed_updates_neither_overwrite_nor_delete_newer_memory(self):
        first = self.event("/remember launch = FIRST-VALUE")
        self.assert_reply(self.bot.process(first))
        self.send("/remember launch = SECOND-VALUE")
        self.assertIsNone(self.bot.process(first))
        self.assertIn("SECOND-VALUE", self.text(self.send("/recall launch")))
        deleted = self.event("/forget launch")
        self.assert_reply(self.bot.process(deleted))
        self.send("/remember launch = THIRD-VALUE")
        self.restart()
        self.assertIsNone(self.bot.process(deleted))
        self.assertIn("THIRD-VALUE", self.text(self.send("/recall launch")))
        self.assertEqual(self.store.offset(), self.uid + 1)

    def test_facts_and_taught_replies_stay_with_their_chat(self):
        for chat, value in ((PRIVATE, "PRIVATE-SENTINEL"),
                            (GROUP, "GROUP-ONE-SENTINEL"),
                            (OTHER_GROUP, "GROUP-TWO-SENTINEL")):
            self.send("/remember shared topic = " + value, chat=chat)
            self.send("/teach shared prompt = " + value, chat=chat)
        self.restart()
        for chat, value in ((PRIVATE, "PRIVATE-SENTINEL"),
                            (GROUP, "GROUP-ONE-SENTINEL"),
                            (OTHER_GROUP, "GROUP-TWO-SENTINEL")):
            with self.subTest(chat=chat):
                self.assertIn(value, self.text(self.send("/recall shared topic", chat=chat)))
                self.assertEqual(value, self.text(self.send("/chat shared prompt", chat=chat)))
                for other in {"PRIVATE-SENTINEL", "GROUP-ONE-SENTINEL", "GROUP-TWO-SENTINEL"} - {value}:
                    self.assertNotIn(other, self.text(self.send("/memories", chat=chat)))

    def test_disallowed_chat_cannot_read_create_or_use_taught_memory(self):
        self.send("/remember sensitive = ALLOWED-FACT")
        self.send("/teach greeting = ALLOWED-REPLY")
        commands = ("/recall sensitive", "/memories", "/memory_stats",
                    "/remember sensitive = OUTSIDE-FACT", "/chat greeting",
                    "bexs teach greeting = OUTSIDE-REPLY")
        for command in commands:
            with self.subTest(command=command):
                self.assertIsNone(self.send(command, chat=18, user=18))
        self.assertIn("ALLOWED-FACT", self.text(self.send("/recall sensitive")))
        self.assertEqual("ALLOWED-REPLY", self.text(self.send("/chat greeting")))

    def test_members_can_create_and_edit_own_items_but_admin_is_needed_for_others(self):
        for create, read, remove in (("remember", "recall", "forget"),
                                     ("teach", "chat", "unteach")):
            with self.subTest(kind=create):
                key = create + " ownership"
                self.api.roles.clear()
                self.assert_reply(self.send(f"/{create} {key} = OWNER-FIRST", chat=GROUP))
                self.send(f"/{create} {key} = OWNER-SECOND", chat=GROUP)
                self.assertIn("OWNER-SECOND", self.text(self.send(f"/{read} {key}", chat=GROUP)))
                self.assert_reply(self.send(f"/{create} {key} = UNAUTHORIZED", chat=GROUP, user=23))
                self.assertIn("OWNER-SECOND", self.text(self.send(f"/{read} {key}", chat=GROUP)))
                self.assert_reply(self.send(f"/{remove} {key}", chat=GROUP, user=23))
                self.assertIn("OWNER-SECOND", self.text(self.send(f"/{read} {key}", chat=GROUP)))
                self.api.roles[23] = "administrator"
                self.send(f"/{create} {key} = ADMIN-REVISION", chat=GROUP, user=23)
                self.assertIn("ADMIN-REVISION", self.text(self.send(f"/{read} {key}", chat=GROUP)))
                self.send(f"/{remove} {key}", chat=GROUP, user=23)
                self.assertNotIn("ADMIN-REVISION", self.text(self.send(f"/{read} {key}", chat=GROUP)))

    def test_failed_admin_lookup_does_not_authorize_overwrite_or_delete(self):
        self.send("/remember meeting = KEEP-ORIGINAL", chat=GROUP)
        self.api.fail = True
        self.assert_reply(self.send("/remember meeting = NOT-AUTHORIZED", chat=GROUP, user=23))
        self.assert_reply(self.send("/forget meeting", chat=GROUP, user=23))
        self.api.fail = False
        self.assertIn("KEEP-ORIGINAL", self.text(self.send("/recall meeting", chat=GROUP)))

    def test_explicit_commands_acknowledge_inside_normal_reply_cooldown(self):
        self.assert_reply(self.send("ordinary greeting"))
        commands = ("/remember urgent = TIME-SENTINEL", "/recall urgent", "/memories fact",
                    "/memory_stats", "/teach ready = READY-SENTINEL", "/memories reply",
                    "/unteach ready", "/forget urgent")
        for command in commands:
            with self.subTest(command=command):
                self.assert_reply(self.send(command, advance=0))
        self.assertIsNone(self.send("ordinary followup", advance=0))

    def test_explicit_commands_and_saved_values_never_enter_markov_or_seed(self):
        before = self.model()
        seed_before = copy.deepcopy(self.seed)
        original_path = ROOT / "seed/original-memory.json"
        source_before = hashlib.sha256(original_path.read_bytes()).hexdigest()
        commands = ("/remember vault = uniquefactneverlearned words",
                    "bexs teach weather = uniquereplyneverlearned words",
                    "@bexs_example_bot recall vault", "/chat weather",
                    "/memories", "/memory_stats", "bexs forget vault",
                    "bexs unteach weather")
        for command in commands:
            self.send(command)
        self.assertEqual(self.model(), before)
        self.assertEqual(self.seed, seed_before)
        self.assertEqual(hashlib.sha256(original_path.read_bytes()).hexdigest(), source_before)
        self.restart()
        self.assertEqual(self.model(), before)

    def test_own_addressed_commands_parse_and_other_bot_targets_are_ignored(self):
        self.assert_reply(self.send("/remember@BEXS_EXAMPLE_BOT target = OWN-TARGET"))
        for command in ("/remember@otherbot target = WRONG-TARGET",
                        "/forget@otherbot target", "/recall@otherbot target",
                        "/teach@otherbot topic = WRONG-REPLY"):
            with self.subTest(command=command):
                self.assertIsNone(self.send(command))
        self.assertIn("OWN-TARGET", self.text(self.send("/recall@bexs_example_bot target")))
        self.assertNotEqual("WRONG-REPLY", self.text(self.send("/chat topic")))

    def test_anonymous_sender_cannot_read_write_or_use_new_memory(self):
        self.send("/remember hidden = FACT-PRIVATE-SENTINEL", chat=GROUP)
        self.send("/teach signal = REPLY-PRIVATE-SENTINEL", chat=GROUP)
        fields = {"sender_chat": {"id": GROUP, "type": "supergroup"}}
        commands = ("/recall hidden", "/memories", "/memories fact", "/memories reply",
                    "/memory_stats", "/chat signal", "bexs recall hidden",
                    "/remember hidden = ANONYMOUS-WRITE", "/forget hidden",
                    "/teach signal = ANONYMOUS-WRITE", "/unteach signal",
                    "bexs remember anonymous = ANONYMOUS-WRITE")
        for command in commands:
            with self.subTest(command=command):
                result = self.text(self.send(command, chat=GROUP, **fields))
                self.assertNotIn("FACT-PRIVATE-SENTINEL", result)
                self.assertNotIn("REPLY-PRIVATE-SENTINEL", result)
        self.assertIn("FACT-PRIVATE-SENTINEL", self.text(self.send("/recall hidden", chat=GROUP)))
        self.assertEqual("REPLY-PRIVATE-SENTINEL", self.text(self.send("/chat signal", chat=GROUP)))
        self.assertNotIn("ANONYMOUS-WRITE", self.text(self.send("/recall anonymous", chat=GROUP)))
        result = self.send("@bexs_example_bot signal", chat=GROUP, **fields)
        self.assertNotEqual("REPLY-PRIVATE-SENTINEL", self.text(result))

    def test_bot_senders_are_ignored_even_for_memory_commands(self):
        before = self.model()
        for command in ("/remember robot = BOT-FACT", "bexs teach robot = BOT-REPLY",
                        "/memories", "/memory_stats"):
            event = self.event(command)
            event["message"]["from"]["is_bot"] = True
            self.assertIsNone(self.bot.process(event))
        self.assertEqual(self.model(), before)
        self.assertNotIn("BOT-FACT", self.text(self.send("/recall robot")))
        self.assertNotEqual("BOT-REPLY", self.text(self.send("/chat robot")))

    def test_learning_pause_blocks_creates_and_updates_but_allows_reads_and_deletion(self):
        self.send("/remember kept = BEFORE-PAUSE-FACT")
        self.send("/teach kept = BEFORE-PAUSE-REPLY")
        self.send("/learn off")
        self.assertFalse(self.store.get(PRIVATE)[0].data["is_learning"])
        for command in ("/remember kept = AFTER-PAUSE", "/remember new = NEW-PAUSED",
                        "/teach kept = AFTER-PAUSE", "bexs teach new = NEW-PAUSED"):
            self.assert_reply(self.send(command))
        self.assertIn("BEFORE-PAUSE-FACT", self.text(self.send("/recall kept")))
        self.assertEqual("BEFORE-PAUSE-REPLY", self.text(self.send("/chat kept")))
        self.assertNotIn("NEW-PAUSED", self.text(self.send("/recall new")))
        self.assertNotEqual("NEW-PAUSED", self.text(self.send("/chat new")))
        self.assert_reply(self.send("/forget kept"))
        self.assert_reply(self.send("/unteach kept"))
        self.restart()
        self.assertNotIn("BEFORE-PAUSE-FACT", self.text(self.send("/recall kept")))
        self.assertNotEqual("BEFORE-PAUSE-REPLY", self.text(self.send("/chat kept")))

    def test_malformed_commands_are_handled_without_learning_their_text(self):
        before = self.model()
        commands = ("/remember", "/remember topic", "/remember = value",
                    "/remember topic =", "/teach", "/teach prompt", "/teach = reply",
                    "/teach prompt =", "/recall", "/forget", "/unteach",
                    "/memories invalid-kind", "bexs remember", "bexs teach prompt",
                    "  /remember\tmissing-separator", "\n/teach\nmissing-separator")
        for command in commands:
            with self.subTest(command=command):
                self.assert_reply(self.send(command))
        self.assertEqual(self.model(), before)
        self.assertEqual(self.store.offset(), self.uid + 1)

    def test_whitespace_and_case_normalization_preserve_exact_taught_matching(self):
        self.assert_reply(self.send("  /teach\tMarket   Status = EXACT-REPLY-SENTINEL"))
        for prompt in ("market status", "MARKET   STATUS", "bexs market status",
                       "@bexs_example_bot market status", "@BEXS_EXAMPLE_BOT MARKET STATUS"):
            with self.subTest(prompt=prompt):
                self.assertEqual("EXACT-REPLY-SENTINEL", self.text(self.send("/chat " + prompt)))
        for prompt in ("market", "market status please", "market statuz",
                       "@someone_else market status", "my bexs market status"):
            with self.subTest(nonmatch=prompt):
                self.assertNotEqual("EXACT-REPLY-SENTINEL", self.text(self.send("/chat " + prompt)))
        self.assert_reply(self.send("@bexs_example_bot\nremember\tlaunch = WHITESPACE-FACT"))
        self.assertIn("WHITESPACE-FACT", self.text(self.send("/recall\tlaunch")))

    def test_ordinary_taught_replies_still_require_trigger_and_cooldown(self):
        self.send("/teach launch status = GROUP-TAUGHT-REPLY", chat=GROUP)
        self.assertIsNone(self.send("launch status", chat=GROUP))
        self.assertEqual("GROUP-TAUGHT-REPLY",
                         self.text(self.send("@bexs_example_bot launch status", chat=GROUP)))
        self.assertIsNone(self.send("@bexs_example_bot launch status", chat=GROUP, advance=0))
        result = self.send("bexs launch status", chat=GROUP,
                           reply_to_message={"from": {"id": IDENTITY["id"]}},
                           message_thread_id=42)
        self.assertEqual("GROUP-TAUGHT-REPLY", self.text(result))
        self.assertEqual(result[1]["message_thread_id"], 42)
        self.send("/teach launch status = PRIVATE-TAUGHT-REPLY")
        self.assertEqual("PRIVATE-TAUGHT-REPLY", self.text(self.send("launch status")))

    def test_removing_taught_reply_survives_restart_and_stops_exact_reuse(self):
        self.send("bexs teach all ready = REMOVABLE-TAUGHT-REPLY")
        self.assertEqual("REMOVABLE-TAUGHT-REPLY", self.text(self.send("/chat all ready")))
        self.send("bexs unteach all ready")
        self.restart()
        self.assertNotEqual("REMOVABLE-TAUGHT-REPLY", self.text(self.send("/chat all ready")))
        self.assertNotEqual("REMOVABLE-TAUGHT-REPLY", self.text(self.send("all ready")))

    def test_store_save_failure_rolls_back_command_data_and_update_cursor(self):
        self.send("/remember existing = INITIAL-FACT")
        self.send("/teach existing = INITIAL-REPLY")
        commands = ("/remember new = ROLLBACK-FACT", "/teach new = ROLLBACK-REPLY",
                    "/remember existing = REVISED-FACT", "/teach existing = REVISED-REPLY",
                    "/forget existing", "/unteach existing")
        for command in commands:
            with self.subTest(command=command):
                event = self.event(command)
                before = tuple(self.store.db.iterdump())
                offset_before = self.store.offset()
                original_save = self.store.save

                def fail_after_save(*args, **kwargs):
                    original_save(*args, **kwargs)
                    raise sqlite3.OperationalError("simulated persistence failure")

                with patch.object(self.store, "save", side_effect=fail_after_save):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.bot.process(event)
                self.assertEqual(self.store.offset(), offset_before)
                self.assertEqual(tuple(self.store.db.iterdump()), before)
                self.restart()
                self.assertEqual(self.store.offset(), offset_before)
                self.assertEqual(tuple(self.store.db.iterdump()), before)
                self.assert_reply(self.bot.process(event))
                self.assertEqual(self.store.offset(), event["update_id"] + 1)


if __name__ == "__main__":
    unittest.main()
