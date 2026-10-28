import copy
import io
import json
import random
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from bot import Bot, Config, ROOT
from memory import Memory
from storage import Store
from telegram_api import APIError, Telegram


SEED = {"model": {"": ["Good"], "Good": [], "good": ["morning"],
                  "morning": ["crew"], "crew": [0]},
        "is_learning": True, "torrent_level": 8, "restricted_mode": False,
        "stickers": ["old-bot-sticker"], "animations": ["old-bot-gif"]}


class FakeAPI:
    def __init__(self):
        self.status = "member"
        self.calls = []

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return {"status": self.status}


class MemoryTests(unittest.TestCase):
    def test_original_schema_and_generation(self):
        memory = Memory.load(ROOT / "seed/original-memory.json")
        self.assertEqual(len(memory.model), 9921)
        self.assertEqual(sum(map(len, memory.model.values())), 56799)
        rng = random.Random(42)
        for _ in range(100):
            line = memory.generate("solana", rng)
            self.assertTrue(line)
            self.assertLessEqual(len(line), 1800)
            self.assertLessEqual(len(line.split()), 30)

    def test_uppercase_start_falls_back_to_learned_lowercase(self):
        self.assertEqual(Memory(SEED).generate(rng=random.Random(0)), "Good morning crew")

    def test_learning_uses_end_markers_and_bounded_weights(self):
        memory = Memory(SEED)
        for _ in range(300):
            memory.learn("fresh green candles")
        self.assertEqual(memory.model["candles"], [0] * 256)
        self.assertEqual(memory.model["fresh"], ["green"] * 256)
        self.assertLessEqual(len(memory.model[""]), 256)

    def test_commands_and_paused_learning_do_not_change_model(self):
        memory = Memory(SEED)
        original = copy.deepcopy(memory.model)
        memory.learn("/chat unexpected")
        memory.data["is_learning"] = False
        memory.learn("other words")
        self.assertEqual(memory.model, original)

    def test_filter_applies_to_learning_and_generation(self):
        seed = copy.deepcopy(SEED)
        seed["model"][""] = ["badword", "@someone", "https://example.com", "Good"]
        memory = Memory(seed, {"badword"})
        memory.learn("badword @secret 0x" + "a" * 40)
        for _ in range(20):
            line = memory.generate(rng=random.Random(_))
            self.assertEqual(line, "Good morning crew")
        self.assertNotIn("badword", memory.model)
        self.assertNotIn("@secret", memory.model)

    def test_invalid_memory_rejected(self):
        for value in ({}, {"model": []}, {"model": {"a": [1]}}, {"model": {"a": [False]}}):
            with self.assertRaises(ValueError):
                Memory(value)


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.store = Store(self.path, SEED, set(), chance=0)
        self.api = FakeAPI()
        self.config = Config(allowed_chats={17, -100123}, cooldown=3)
        self.now = 1000
        self.bot = Bot(self.api, self.store, self.config,
                       {"id": 99, "username": "bexs_example_bot"}, random.Random(5),
                       clock=lambda: self.now)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def update(self, uid, text="hello", chat_id=17, user=17, **fields):
        self.now += 5
        return {"update_id": uid, "message": {
            "message_id": uid, "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": user, "is_bot": False}, "text": text, **fields}}

    def test_disallowed_chat_cannot_read_seed_or_learn(self):
        self.assertIsNone(self.bot.process(self.update(1, "secretunseen", chat_id=18)))
        self.assertIsNone(self.bot.process(self.update(2, "/chat", chat_id=18)))
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM chats").fetchone()[0], 0)
        result = self.bot.process(self.update(3, "/id", chat_id=18))
        self.assertIn("Chat ID: 18", result[1]["text"])

    def test_learn_is_isolated_and_survives_restart(self):
        self.bot.process(self.update(1, "violet rockets rise"))
        self.assertIn("violet", self.store.get(17)[0].model)
        self.assertNotIn("violet", self.store.get(-100123)[0].model)
        self.store.close()
        self.store = Store(self.path, SEED, set())
        self.assertIn("violet", self.store.get(17)[0].model)
        self.assertEqual(self.store.offset(), 2)

    def test_duplicate_update_is_not_replied_to_or_learned_twice(self):
        event = self.update(1, "fresh candles")
        self.assertIsNotNone(self.bot.process(event))
        before = copy.deepcopy(self.store.get(17)[0].model)
        self.assertIsNone(self.bot.process(event))
        self.assertEqual(self.store.get(17)[0].model, before)

    def test_group_requires_admin_for_settings(self):
        result = self.bot.process(self.update(1, "/learn off", chat_id=-100123))
        self.assertIn("Only chat admins", result[1]["text"])
        self.assertTrue(self.store.get(-100123)[0].data["is_learning"])
        self.api.status = "administrator"
        self.bot.process(self.update(2, "/learn off", chat_id=-100123))
        self.assertFalse(self.store.get(-100123)[0].data["is_learning"])

    def test_private_pause_stops_learning(self):
        self.bot.process(self.update(1, "/learn off"))
        self.bot.process(self.update(2, "brandnewphrase here"))
        self.assertNotIn("brandnewphrase", self.store.get(17)[0].model)

    def test_other_bot_commands_and_bot_messages_ignored(self):
        self.assertIsNone(self.bot.process(self.update(1, "/chat@otherbot")))
        event = self.update(2, "botword")
        event["message"]["from"]["is_bot"] = True
        self.assertIsNone(self.bot.process(event))
        self.assertNotIn("botword", self.store.get(17)[0].model)

    def test_mentions_and_replies_trigger_in_quiet_group(self):
        self.assertIsNone(self.bot.process(self.update(1, "ordinary words", chat_id=-100123)))
        self.assertIsNotNone(self.bot.process(self.update(2, "@bexs_example_bot good", chat_id=-100123)))
        result = self.bot.process(self.update(3, "good", chat_id=-100123,
                                            reply_to_message={"from": {"id": 99}},
                                            message_thread_id=42))
        self.assertEqual(result[1]["message_thread_id"], 42)

    def test_media_relearned_for_current_bot(self):
        self.assertEqual(self.store.get(17)[0].data["stickers"], [])
        self.bot.process(self.update(1, "", sticker={"file_id": "new-bot-sticker"}))
        result = self.bot.process(self.update(2, "/sticker"))
        self.assertEqual(result[0], "sendSticker")
        self.assertEqual(result[1]["sticker"], "new-bot-sticker")

    def test_cooldown_still_learns(self):
        first = self.update(1, "one here")
        self.assertIsNotNone(self.bot.process(first))
        event = self.update(2, "twonewword here")
        self.now -= 5
        self.assertIsNone(self.bot.process(event))
        self.assertIn("twonewword", self.store.get(17)[0].model)

    def test_invalid_chance_does_not_poison_state(self):
        for uid, value in enumerate(["nan", "inf", "-5", "101", "oops"], 1):
            result = self.bot.process(self.update(uid, "/chance " + value))
            self.assertIn("number from 0 to 100", result[1]["text"])
            self.assertEqual(self.store.get(17)[1]["chance"], 0)

    def test_wrong_bot_cannot_reuse_database(self):
        self.store.bind_bot(99)
        self.store.bind_bot(99)
        with self.assertRaises(ValueError):
            self.store.bind_bot(100)


class APIClientTests(unittest.TestCase):
    def test_post_payload_and_result(self):
        api = Telegram("123:" + "A" * 25)
        response = io.BytesIO(json.dumps({"ok": True, "result": {"id": 123}}).encode())
        with patch("urllib.request.urlopen", return_value=response) as opening:
            self.assertEqual(api.call("getMe"), {"id": 123})
            request = opening.call_args.args[0]
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(json.loads(request.data), {})

    def test_error_redacts_credentials_and_keeps_retry_after(self):
        token = "123:" + "A" * 25
        api = Telegram(token)
        error = urllib.error.HTTPError("https://api.telegram.org/bot" + token,
                                       429, "token " + token, {},
                                       io.BytesIO(b'{"parameters":{"retry_after":7}}'))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(APIError) as caught:
                api.call("getUpdates")
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(caught.exception.retry_after, 7)


if __name__ == "__main__":
    unittest.main()
