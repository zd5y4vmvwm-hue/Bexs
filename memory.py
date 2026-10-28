"""A bounded first-order Markov generator for the supplied memory format."""
from __future__ import annotations

import copy
import json
import random
import re
import time
from pathlib import Path

STOPWORDS = set("i a an the you it is are to of in on and or that this what how do we".split())
IDENTIFIER = re.compile(
    r"(?:https?://|www\.|@|0x[0-9a-fA-F]{40}|[1-9A-HJ-NP-Za-km-z]{32,})"
)


class Memory:
    def __init__(self, data: dict, blocked: set[str] | None = None):
        if not isinstance(data, dict) or not isinstance(data.get("model"), dict):
            raise ValueError("Memory must contain a model object.")
        for key, values in data["model"].items():
            if not isinstance(key, str) or not isinstance(values, list):
                raise ValueError("Model entries must be token-to-list mappings.")
            if any(not isinstance(v, str) and not (type(v) is int and v == 0) for v in values):
                raise ValueError("Successors must be strings or the integer 0.")
        self.data = copy.deepcopy(data)
        self.model = self.data["model"]
        self.blocked = blocked or set()
        self.index: dict[str, str] = {}
        for key, values in self.model.items():
            canonical = key.casefold()
            old = self.index.get(canonical)
            if old is None or len(values) > len(self.model[old]):
                self.index[canonical] = key

    @classmethod
    def load(cls, path: Path, blocked: set[str] | None = None) -> "Memory":
        return cls(json.loads(path.read_text(encoding="utf-8")), blocked)

    def allowed(self, token: str) -> bool:
        # Output filtering only. The imported source file stays byte-for-byte intact.
        words = re.findall(r"[\w']+", token.casefold())
        return (0 < len(token) <= 80 and not IDENTIFIER.search(token)
                and not any(w in self.blocked for w in words))

    def successors(self, token: str) -> list:
        if self.model.get(token):
            return self.model[token]
        key = self.index.get(token.casefold())
        return self.model.get(key, [])

    def generate(self, prompt: str = "", rng: random.Random | None = None,
                 max_words: int = 30) -> str:
        rng = rng or random.SystemRandom()
        topics = [t for t in prompt.split()[:80] if t.casefold() not in STOPWORDS
                  and self.allowed(t) and self.successors(t)]
        starts = [t for t in self.model.get("", []) if isinstance(t, str) and self.allowed(t)]
        if not starts:
            starts = [k for k in self.model if self.allowed(k)]
        if not starts:
            return "Still learning. Say something and try again."
        best: list[str] = []
        for _ in range(16):
            word = rng.choice(topics or starts)
            words = [word]
            for _ in range(max(0, max_words - 1)):
                options = [v for v in self.successors(word)
                           if v == 0 or (isinstance(v, str) and self.allowed(v))]
                if not options:
                    break
                nxt = rng.choice(options)  # Repetitions preserve learned weights.
                if nxt == 0:
                    break
                words.append(nxt)
                word = nxt
                if len(words) > 5 and len(set(words[-4:])) == 1:
                    break
            if len(words) > len(best):
                best = words
            if len(words) >= 3:
                best = words
                break
        result = " ".join(best)
        return re.sub(r"\s+([,.;!?])", r"\1", result)[:1800]

    def learn(self, text: str) -> None:
        if not self.data.get("is_learning", True) or text.lstrip().startswith("/"):
            return
        previous = ""
        for token in text.split()[:80]:
            if not self.allowed(token):
                if previous:
                    self._append(previous, 0)
                previous = ""
                continue
            self._append(previous, token)
            previous = token.casefold()
            if previous not in self.model and len(self.model) < 50000:
                self.model[previous] = []
                self.index[previous] = previous
        if previous:
            self._append(previous, 0)
        self.data["last_update"] = time.time()

    def _append(self, key: str, successor: str | int) -> None:
        if key not in self.model:
            if len(self.model) >= 50000:
                return
            self.model[key] = []
        values = self.model[key]
        values.append(successor)
        del values[:-256]
        self.index[key.casefold()] = key

    def learn_media(self, kind: str, file_id: str) -> None:
        if self.data.get("is_learning", True):
            values = self.data.setdefault(kind, [])
            if file_id not in values:
                values.append(file_id)
                del values[:-500]

    def stats(self) -> str:
        return (f"Vocabulary: {len(self.model):,} entries\n"
                f"Associations: {sum(map(len, self.model.values())):,}\n"
                f"Learning: {'on' if self.data.get('is_learning', True) else 'off'}\n"
                f"Usable stickers: {len(self.data.get('stickers', []))}\n"
                f"Usable GIFs: {len(self.data.get('animations', []))}")
