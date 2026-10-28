#!/usr/bin/env python3
"""Bexs: run locally with --chat, or start Telegram polling."""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from memory import Memory
from storage import Store
from telegram_api import APIError, Telegram
from teach_commands import (TEACH_COMMANDS, TeachCommands, allowed_text,
                            identified_sender, reply_prompt, teaching_alias)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("bexs")
HELP = ("Bexs — learned crypto-group banter.\n"
        "/chat [topic] — generate a reply\n/id — show chat and user IDs\n"
        "/stats — show memory size\n/sticker or /gif — send learned media\n"
        "/memory_help — teach, recall, correct, or forget a memory\n"
        "Admin: /learn on|off, /chance 0–100\n"
        "Replies combine learned words and may be nonsense. /learn off pauses learning.")


def load_env(path: Path):
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            if not sep or not key.strip().replace("_", "").isalnum():
                raise ValueError("Invalid .env entry. Use KEY=value lines.")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def parse_ids(value: str) -> set[int]:
    return {int(item.strip()) for item in value.split(",") if item.strip()}


@dataclass
class Config:
    allowed_chats: set[int] = field(default_factory=set)
    admins: set[int] = field(default_factory=set)
    chance: float = .08
    cooldown: float = 3
    reuse_media: bool = False

    @classmethod
    def from_env(cls):
        result = cls(parse_ids(os.getenv("ALLOWED_CHAT_IDS", "")),
                     parse_ids(os.getenv("ADMIN_USER_IDS", "")),
                     float(os.getenv("AUTO_REPLY_CHANCE", "0.08")),
                     float(os.getenv("COOLDOWN_SECONDS", "3")),
                     os.getenv("REUSE_IMPORTED_MEDIA", "false").lower() == "true")
        if not math.isfinite(result.chance) or not 0 <= result.chance <= 1:
            raise ValueError("AUTO_REPLY_CHANCE must be between 0 and 1.")
        if not math.isfinite(result.cooldown) or result.cooldown < 1:
            raise ValueError("COOLDOWN_SECONDS must be at least 1.")
        return result


class Bot:
    def __init__(self, api, store: Store, config: Config, identity: dict,
                 rng=None, clock=time.time):
        self.api, self.store, self.config = api, store, config
        self.identity = identity
        self.rng = rng or random.SystemRandom()
        self.clock = clock
        self.teaching = TeachCommands(self)

    def admin(self, message: dict) -> bool:
        sender = message.get("from", {})
        if message.get("sender_chat") or sender.get("is_bot"):
            return False
        user_id, chat = sender.get("id"), message["chat"]
        if user_id in self.config.admins:
            return True
        if chat["type"] == "private":
            return user_id == chat["id"]
        if not user_id:
            return False
        try:
            member = self.api.call("getChatMember", chat_id=chat["id"], user_id=user_id)
            return member.get("status") in {"creator", "administrator"}
        except APIError:
            return False

    def outbound(self, message, text=None, method="sendMessage", **extra):
        result = {"chat_id": message["chat"]["id"],
                  "reply_parameters": {"message_id": message["message_id"],
                                       "allow_sending_without_reply": True}, **extra}
        if message.get("message_thread_id") is not None:
            result["message_thread_id"] = message["message_thread_id"]
        if text is not None:
            result.update(text=text, link_preview_options={"is_disabled": True})
        return method, result

    def process(self, update: dict):
        """Compute and commit once, then return an optional message for dispatch."""
        with self.store.transaction():
            return self._process(update)

    def reply_text(self, message: dict, prompt: str, memory):
        if identified_sender(message):
            candidate = reply_prompt(prompt, self.identity["username"])
            if candidate:
                lesson = self.store.teach.find_reply(message["chat"]["id"], candidate)
                if lesson is not None and allowed_text(lesson.value, memory):
                    return lesson.value
        return memory.generate(prompt, self.rng)

    def _process(self, update: dict):
        update_id = update["update_id"]
        offset = self.store.offset()
        if offset is not None and update_id < offset:
            return None
        message = update.get("message")
        if not message or message.get("from", {}).get("is_bot"):
            self.store.acknowledge(update_id)
            return None
        chat_id = message["chat"]["id"]
        text = message.get("text", "")
        command, argument = "", ""
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            head, argument = parts[0], parts[1] if len(parts) > 1 else ""
            command, _, target = head.partition("@")
            if target and target.casefold() != self.identity["username"].casefold():
                self.store.acknowledge(update_id)
                return None
            command = command.lower()
            argument = argument.strip()
        else:
            alias = teaching_alias(stripped, self.identity["username"])
            if alias is not None:
                command, argument = alias
        if command == "/id":
            self.store.acknowledge(update_id)
            return self.outbound(message, f"Chat ID: {chat_id}\nUser ID: {message.get('from', {}).get('id', 'unknown')}")
        if chat_id not in self.config.allowed_chats:
            self.store.acknowledge(update_id)
            if command in {"/start", "/help"}:
                return self.outbound(message, "Setup: send /id, then add this chat ID to ALLOWED_CHAT_IDS and restart the bot.")
            return None
        memory, state = self.store.get(chat_id)
        now, response = self.clock(), None
        explicit_memory_command = command in TEACH_COMMANDS
        if explicit_memory_command:
            response = self.outbound(message, self.teaching.handle(command, argument, message, memory, now))
        elif command in {"/start", "/help"}:
            response = self.outbound(message, HELP)
        elif command == "/stats":
            response = self.outbound(message, memory.stats() + f"\nGroup reply chance: {state['chance']:.0%}")
        elif command in {"/learn", "/chance"}:
            if not self.admin(message):
                response = self.outbound(message, "Only chat admins can change these settings.")
            elif command == "/learn":
                if argument not in {"on", "off"}:
                    response = self.outbound(message, "Use /learn on or /learn off.")
                else:
                    memory.data["is_learning"] = argument == "on"
                    response = self.outbound(message, f"Learning {argument} for this chat.")
            else:
                try:
                    chance = float(argument) / 100
                    if not math.isfinite(chance) or not 0 <= chance <= 1:
                        raise ValueError
                    state["chance"] = chance
                    response = self.outbound(message, f"Group reply chance: {chance:.0%}. Mentions and replies still work.")
                except ValueError:
                    response = self.outbound(message, "Use /chance with a number from 0 to 100.")
        elif command in {"/sticker", "/gif"}:
            kind = "stickers" if command == "/sticker" else "animations"
            files = memory.data.get(kind, [])
            if files:
                field = "sticker" if kind == "stickers" else "animation"
                method = "sendSticker" if kind == "stickers" else "sendAnimation"
                response = self.outbound(message, method=method, **{field: self.rng.choice(files)})
            else:
                response = self.outbound(message, "Send me a sticker or GIF while learning is on so I can reuse it here.")
        elif command == "/chat":
            response = self.outbound(message, self.reply_text(message, argument, memory))
        elif not command:
            if text:
                # Generate before learning to avoid immediately parroting a new message.
                mention = "@" + self.identity["username"].casefold()
                is_reply = message.get("reply_to_message", {}).get("from", {}).get("id") == self.identity["id"]
                triggered = (message["chat"]["type"] == "private" or mention in text.casefold()
                             or is_reply or self.rng.random() < state["chance"])
                if triggered and now - state["last_reply"] >= self.config.cooldown:
                    response = self.outbound(message, self.reply_text(message, text, memory))
                memory.learn(text)
            for field, kind in (("sticker", "stickers"), ("animation", "animations")):
                if message.get(field, {}).get("file_id"):
                    memory.learn_media(kind, message[field]["file_id"])
        if response:
            if not explicit_memory_command and now - state["last_reply"] < self.config.cooldown:
                response = None
            else:
                state["last_reply"] = now
        self.store.save(chat_id, memory, state, update_id)
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--chat", action="store_true", help="Offline terminal chat; no credentials needed")
    mode.add_argument("--sample", type=int, metavar="N", help="Generate N offline example replies")
    mode.add_argument("--stats", action="store_true", help="Inspect the original memory")
    parser.add_argument("--seed", type=Path, default=ROOT / "seed/original-memory.json")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--remove-webhook", action="store_true", help="Explicitly remove a previous webhook")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    store = None
    try:
        load_env(ROOT / ".env")
        blocked = {s.strip().casefold() for s in (ROOT / "blocked_words.txt").read_text().splitlines()
                   if s.strip() and not s.startswith("#")}
        seed = Memory.load(args.seed, blocked)
        if args.stats:
            print(seed.stats().replace("Usable stickers", "Imported sticker references")
                  .replace("Usable GIFs", "Imported GIF references"))
            return 0
        if args.sample is not None:
            if not 1 <= args.sample <= 100:
                raise ValueError("--sample must be between 1 and 100.")
            rng = random.Random(42)
            for _ in range(args.sample):
                print(seed.generate(rng=rng))
            return 0
        # Offline chat has no Telegram permissions, cooldown, or group settings.
        # Incomplete Telegram setup must not prevent restoring local memory/chat.
        config = Config() if args.chat else Config.from_env()
        data_dir = args.data_dir or Path(os.getenv("DATA_DIR", str(ROOT / "data")))
        store = Store(data_dir / ("local.sqlite3" if args.chat else "telegram.sqlite3"),
                      seed.data, blocked, config.chance, config.reuse_media)
        if args.chat:
            memory, state = store.get(0)
            print("Bexs — local mode. /quit exits; /stats; /learn on|off.")
            while True:
                try:
                    text = input("You: ").strip()
                except EOFError:
                    break
                if text == "/quit":
                    break
                if text == "/stats":
                    print(memory.stats())
                    continue
                if text in {"/learn on", "/learn off"}:
                    memory.data["is_learning"] = text.endswith("on")
                    store.save(0, memory, state)
                    continue
                if text:
                    print("Bexs:", memory.generate(text))
                    memory.learn(text)
                    store.save(0, memory, state)
            return 0
        api = Telegram(os.getenv("TELEGRAM_BOT_TOKEN", ""))
        identity = api.call("getMe")
        store.bind_bot(identity["id"])
        if api.call("getWebhookInfo").get("url"):
            if not args.remove_webhook:
                raise ValueError("A webhook is active. Stop the old bot, then use --remove-webhook if switching to this runner.")
            api.call("deleteWebhook", drop_pending_updates=False)
        stop = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())
        bot = Bot(api, store, config, identity)
        LOG.info("Connected as @%s. Allowed chats: %d", identity["username"], len(config.allowed_chats))
        if not config.allowed_chats:
            LOG.info("Discovery mode: send /id in Telegram, configure ALLOWED_CHAT_IDS, then restart.")
        backoff = 2
        while not stop.is_set():
            try:
                updates = api.call("getUpdates", offset=store.offset(), timeout=25,
                                   allowed_updates=["message"])
                backoff = 2
                for update in updates:
                    if stop.is_set():
                        break
                    response = bot.process(update)
                    if response:
                        try:
                            api.call(response[0], **response[1])
                        except APIError as error:
                            if error.code in {401, 409}:
                                raise
                            LOG.warning("Reply not confirmed (API code %d); continuing.", error.code)
                            # Do not retry an ambiguous send: the server may have delivered it.
                            if error.retry_after:
                                stop.wait(error.retry_after)
            except APIError as error:
                if error.code in {401, 409}:
                    raise
                LOG.warning("Polling failed (API code %d); reconnecting.", error.code)
                stop.wait(max(backoff, error.retry_after))
                backoff = min(60, backoff * 2)
        return 0
    except KeyboardInterrupt:
        return 0
    except (ValueError, OSError, APIError) as error:
        LOG.error("%s", error)
        return 1
    finally:
        if store is not None:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
