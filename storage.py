"""SQLite snapshots: memory isolation and update cursors survive restarts."""
import copy
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from memory import Memory
from teach_memory import TeachMemory


class Store:
    def __init__(self, path: Path, seed: dict, blocked: set[str], chance: float = .08,
                 reuse_media: bool = False):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS chats (id INTEGER PRIMARY KEY, state TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        self._transaction_serial = 0
        self.teach = TeachMemory(self.db)
        self.seed = seed
        self.blocked = blocked
        self.chance = chance
        self.reuse_media = reuse_media

    @contextmanager
    def transaction(self):
        """Nest safely so a lesson, chat state, and update cursor commit together."""
        self._transaction_serial += 1
        name = f"bexs_update_{self._transaction_serial}"
        self.db.execute(f"SAVEPOINT {name}")
        try:
            yield
        except BaseException:
            self.db.execute(f"ROLLBACK TO SAVEPOINT {name}")
            self.db.execute(f"RELEASE SAVEPOINT {name}")
            raise
        else:
            self.db.execute(f"RELEASE SAVEPOINT {name}")

    def get(self, chat_id: int) -> tuple[Memory, dict]:
        row = self.db.execute("SELECT state FROM chats WHERE id=?", (chat_id,)).fetchone()
        if row:
            state = json.loads(row[0])
        else:
            data = copy.deepcopy(self.seed)
            if not self.reuse_media:
                data["stickers"] = []
                data["animations"] = []
            state = {"memory": data, "chance": self.chance, "last_reply": 0}
        return Memory(state["memory"], self.blocked), state

    def save(self, chat_id: int, memory: Memory, state: dict, update_id: int | None = None):
        state["memory"] = memory.data
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO chats VALUES (?, ?)",
                            (chat_id, json.dumps(state, ensure_ascii=False)))
            if update_id is not None:
                self._offset(update_id + 1)

    def _offset(self, value: int):
        self.db.execute("INSERT OR REPLACE INTO meta VALUES ('offset', ?)", (str(value),))

    def acknowledge(self, update_id: int):
        with self.transaction():
            self._offset(update_id + 1)

    def offset(self) -> int | None:
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        return int(row[0]) if row else None

    def bind_bot(self, bot_id: int):
        row = self.db.execute("SELECT value FROM meta WHERE key='bot_id'").fetchone()
        if row and row[0] != str(bot_id):
            raise ValueError("This data directory belongs to another bot. Choose a new DATA_DIR.")
        with self.transaction():
            self.db.execute("INSERT OR REPLACE INTO meta VALUES ('bot_id', ?)", (str(bot_id),))

    def close(self):
        self.db.close()
