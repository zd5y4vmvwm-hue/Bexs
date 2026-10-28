"""Offline-only recovery regression checks; never poll or send Telegram."""
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import Config, main


class RecoveryCLITests(unittest.TestCase):
    def test_chat_ignores_incomplete_telegram_settings(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict("os.environ", {"ALLOWED_CHAT_IDS": "not-configured",
                                           "AUTO_REPLY_CHANCE": "not-configured",
                                           "COOLDOWN_SECONDS": "not-configured"}, clear=True), \
                patch("sys.argv", ["bot.py", "--chat", "--data-dir", directory]), \
                patch("builtins.input", return_value="/quit"), \
                patch("sys.stdout", new_callable=io.StringIO) as output, \
                patch("bot.Telegram") as api:
            self.assertEqual(main(), 0)
            self.assertIn("local mode", output.getvalue())
            self.assertTrue((Path(directory) / "local.sqlite3").exists())
            self.assertFalse((Path(directory) / "telegram.sqlite3").exists())
            api.assert_not_called()

    def test_telegram_settings_remain_validated(self):
        with patch.dict("os.environ", {"AUTO_REPLY_CHANCE": "not-configured"}, clear=True):
            with self.assertRaises(ValueError):
                Config.from_env()
