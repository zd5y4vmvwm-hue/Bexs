#!/usr/bin/env python3
"""Run Teach Bexs locally through the same Bot.process path, without Telegram."""
from __future__ import annotations

import argparse
import json
import random
import tempfile
from pathlib import Path

from bot import Bot, Config
from storage import Store


DEMO_SEED = {"model": {"": ["Hello"], "hello": ["there"], "there": [0]},
             "is_learning": True, "stickers": [], "animations": []}
DEMO_IDENTITY = {"id": 99, "username": "bexs_demo_bot"}
DEMO_CHAT = 17


class OfflineAPI:
    def call(self, method, **payload):
        if method != "getChatMember":
            raise RuntimeError("This demo cannot contact Telegram.")
        return {"status": "administrator"}


class Demo:
    def __init__(self, path: Path):
        self.path = path
        self.now = 1_800_000_000
        self.open()

    def open(self):
        self.store = Store(self.path, DEMO_SEED, set(), chance=0)
        self.bot = Bot(OfflineAPI(), self.store, Config(allowed_chats={DEMO_CHAT}, chance=0),
                       dict(DEMO_IDENTITY), random.Random(5), clock=lambda: self.now)

    def send(self, text):
        update_id = self.store.offset() or 1
        self.now += 5
        event = {"update_id": update_id, "message": {
            "message_id": update_id, "chat": {"id": DEMO_CHAT, "type": "private"},
            "from": {"id": DEMO_CHAT, "is_bot": False}, "text": text,
        }}
        response = self.bot.process(event)
        return response[1]["text"] if response else "(No reply.)"

    def restart(self):
        self.store.close()
        self.open()

    def close(self):
        self.store.close()


def run_script(path):
    demo = Demo(path)
    steps = []
    def send(command):
        reply = demo.send(command)
        steps.append({"command": command, "reply": reply})
        return reply
    try:
        send("/remember game night = Friday at 8 pm")
        if "Friday at 8 pm" not in send("/recall game night"):
            raise RuntimeError("Initial recall failed")
        send("/correct game night = Saturday at 8 pm")
        demo.restart()
        steps.append({"action": "Closed and reopened the SQLite connection"})
        after_restart = send("/recall game night")
        if "Saturday at 8 pm" not in after_restart or "Friday at 8 pm" in after_restart:
            raise RuntimeError("Corrected recall after restart failed")
        send("/teach are we ready = Ready when the group is.")
        if send("/chat are we ready") != "Ready when the group is.":
            raise RuntimeError("Taught reply was not used")
        send("/forget game night")
        demo.restart()
        if "Saturday at 8 pm" in send("/recall game night"):
            raise RuntimeError("Forgotten value returned after restart")
        send("/unteach are we ready")
        send("/memory_stats")
        return {"mode": "offline", "telegram_requests": 0, "passed": True, "steps": steps}
    finally:
        demo.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--data-dir", type=Path, help="Persistent directory for the interactive demo")
    parser.add_argument("--json", action="store_true", help="Print the scripted result as JSON")
    args = parser.parse_args()
    if args.data_dir and not args.interactive:
        parser.error("--data-dir is only for --interactive; the scripted demo always uses temporary data")
    with tempfile.TemporaryDirectory(prefix="bexs-teach-demo-") as temporary:
        path = (args.data_dir or Path(temporary)) / "teach-demo.sqlite3"
        if not args.interactive:
            report = run_script(path)
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print("Teach Bexs — verified offline memory lifecycle\n")
                for step in report["steps"]:
                    if "command" in step:
                        print("You:", step["command"])
                        print("Bexs:", step["reply"], "\n")
                    else:
                        print("[" + step["action"] + "]\n")
            return 0
        demo = Demo(path)
        print("Teach Bexs — offline. /memory_help lists commands; /restart reopens storage; /quit exits.")
        print("This session uses temporary data." if not args.data_dir else "This session saves to the chosen demo directory.")
        try:
            while True:
                try:
                    text = input("You: ").strip()
                except EOFError:
                    break
                if text == "/quit":
                    break
                if text == "/restart":
                    demo.restart()
                    print("Bexs: Demo storage reopened.")
                elif text:
                    print("Bexs:", demo.send(text))
        finally:
            demo.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
