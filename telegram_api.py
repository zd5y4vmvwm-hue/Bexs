"""Small Telegram Bot API client. Error text never includes the token or URL."""
import json
import re
import urllib.error
import urllib.request


class APIError(Exception):
    def __init__(self, code: int, retry_after: int = 0):
        self.code = code
        self.retry_after = max(0, int(retry_after))
        super().__init__(f"Telegram API error {code}")


class Telegram:
    def __init__(self, token: str):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("Set TELEGRAM_BOT_TOKEN to a valid token from BotFather.")
        self._root = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, **params):
        request = urllib.request.Request(
            self._root + method,
            data=json.dumps(params).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            try:
                payload = json.loads(error.read())
            except (ValueError, OSError):
                payload = {}
            raise APIError(error.code, payload.get("parameters", {}).get("retry_after", 0)) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise APIError(0) from None
        if not payload.get("ok"):
            raise APIError(payload.get("error_code", 0),
                           payload.get("parameters", {}).get("retry_after", 0))
        return payload["result"]
