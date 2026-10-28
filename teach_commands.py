"""Explicit, scoped teaching commands; no model calls or Telegram I/O."""
from __future__ import annotations

import re

from teach_memory import TeachMemoryError


TEACH_COMMANDS = frozenset({
    "/remember", "/correct", "/recall", "/forget", "/memories",
    "/teach", "/unteach", "/memory_stats", "/memory_help",
})
TEACH_HELP = (
    "Teach Bexs — memories belong to this chat.\n"
    "/remember topic = saved answer\n"
    "/correct topic = updated answer\n"
    "/recall topic\n"
    "/forget topic\n"
    "/teach prompt = preferred reply\n"
    "/unteach prompt\n"
    "/memories [fact|reply] [page]\n"
    "/memory_stats\n\n"
    "You can update your own lessons; chat admins can update any lesson. "
    "Learning must be on to save lessons. Forgetting remains available. "
    "Address commands to @your_bot in groups if needed."
)


def teaching_alias(text: str, username: str):
    """Recognize only explicit 'bexs <action>' or '@ownbot <action>' forms."""
    parts = text.strip().split(maxsplit=2)
    if len(parts) < 2 or parts[0].casefold().rstrip(",:") not in {
        "bexs", "@" + username.casefold()
    }:
        return None
    command = "/" + parts[1].casefold()
    if command not in TEACH_COMMANDS:
        return None
    return command, parts[2].strip() if len(parts) > 2 else ""


def reply_prompt(text: str, username: str) -> str:
    """Remove a direct salutation, preserving the rest for exact matching."""
    return re.sub(
        r"^(?:bexs|@" + re.escape(username) + r")(?=[\s,:!?]|$)[\s,:!?]*",
        "", text.strip(), count=1, flags=re.IGNORECASE,
    ).strip()


def identified_sender(message: dict) -> bool:
    sender = message.get("from", {})
    actor = sender.get("id")
    if message.get("sender_chat") or sender.get("is_bot") or type(actor) is not int or actor <= 0:
        return False
    if message["chat"]["type"] == "private" and actor != message["chat"]["id"]:
        return False
    return True


def allowed_text(text: str, memory) -> bool:
    return bool(text.strip()) and all(memory.allowed(token) for token in text.split())


class TeachCommands:
    def __init__(self, bot):
        self.bot = bot

    def handle(self, command: str, argument: str, message: dict, memory, now: float) -> str:
        if not identified_sender(message):
            return "Use your own Telegram account to access this chat's saved memories."
        store = self.bot.store.teach
        chat_id = message["chat"]["id"]
        actor = message["from"]["id"]
        source_id = message["message_id"]
        try:
            if command == "/memory_help":
                return TEACH_HELP.replace("@your_bot", "@" + self.bot.identity["username"])
            if command == "/memory_stats":
                stats = store.stats(chat_id)
                return (
                    f"This chat's Teach Bexs memory\n"
                    f"Saved facts: {stats['facts']}\nTaught replies: {stats['replies']}\n"
                    f"Updates: {stats['updated']}\nForgotten items: {stats['forgotten']}\n"
                    f"Lookup matches: {stats['hits']} of {stats['lookups']}\n"
                    "Matches count stored answers found; they are not quality ratings."
                )
            if command == "/memories":
                parts = argument.split()
                kind, page = None, 1
                if parts and parts[0] in {"fact", "reply"}:
                    kind = parts.pop(0)
                if parts:
                    if len(parts) != 1 or not parts[0].isascii() or not parts[0].isdigit():
                        return "Use /memories [fact|reply] [page]."
                    page = int(parts[0])
                if not 1 <= page <= 50:
                    return "Choose a page from 1 to 50."
                items = store.list_items(chat_id, kind=kind, limit=500)
                if not items:
                    return "No saved memories in this chat yet. Use /remember or /teach."
                pages = (len(items) + 9) // 10
                if page > pages:
                    return f"There are {pages} pages. Use /memories {kind or ''} {pages}."
                lines = [f"Memories in this chat — page {page}/{pages}"]
                for item in items[(page - 1) * 10:page * 10]:
                    lines.append(f"• [{item.kind}] {item.topic} (revision {item.revision})")
                lines.append("Use /recall topic for a fact, or /chat prompt for a taught reply.")
                return "\n".join(lines)
            if command in {"/remember", "/correct", "/teach"}:
                if not memory.data.get("is_learning", True):
                    return "Learning is paused in this chat. An admin can use /learn on before saving lessons."
                topic, separator, value = argument.partition("=")
                topic, value = topic.strip(), value.strip()
                if not separator or not topic or not value:
                    return f"Use {command} {'prompt = preferred reply' if command == '/teach' else 'topic = saved answer'}."
                kind = "reply" if command == "/teach" else "fact"
                if kind == "reply":
                    topic = reply_prompt(topic, self.bot.identity["username"])
                if not allowed_text(topic, memory) or not allowed_text(value, memory):
                    return "That lesson contains text excluded by this chat's existing reply filter."
                previous = store.peek(chat_id, kind, topic)
                if command == "/correct" and previous is None:
                    return "No saved memory has that topic. Use /remember to create it."
                can_override = bool(previous and previous.created_by != actor and self.bot.admin(message))
                item = store.remember(chat_id, kind, topic, value, actor, source_id, now,
                                      can_override=can_override)
                label = "Taught reply" if kind == "reply" else "Saved memory"
                return f"{label}: {item.topic}\n{item.value}\nRevision {item.revision} — saved for this chat."
            if command == "/recall":
                if not argument.strip():
                    return "Use /recall topic."
                item = store.get(chat_id, "fact", argument)
                if item is None:
                    return "I don't have a saved memory for that topic in this chat."
                if not allowed_text(item.topic, memory) or not allowed_text(item.value, memory):
                    return "That saved memory is excluded by this chat's current reply filter."
                return f"{item.topic}: {item.value}\nSaved revision {item.revision}; use /correct to update it."
            if command in {"/forget", "/unteach"}:
                if not argument.strip():
                    return f"Use {command} {'prompt' if command == '/unteach' else 'topic'}."
                kind = "reply" if command == "/unteach" else "fact"
                topic = reply_prompt(argument, self.bot.identity["username"]) if kind == "reply" else argument
                previous = store.peek(chat_id, kind, topic)
                can_override = bool(previous and previous.created_by != actor and self.bot.admin(message))
                deleted = store.forget(chat_id, kind, topic, actor, source_id, now,
                                       can_override=can_override)
                if not deleted:
                    return "There is no saved item with that topic in this chat."
                return "Forgotten from this chat's active memory and lesson history."
        except TeachMemoryError as error:
            return str(error)
        return "Use /memory_help for Teach Bexs commands."
