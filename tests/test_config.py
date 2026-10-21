"""
tests/test_config.py — Validate configuration file consistency and Telegram settings.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import telegram_channel


class TestConfigConsistency(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.temp_dir.name) / "config"
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.telegram_json = self.config_dir / "telegram.json"
        self.config_json = self.config_dir / "config.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_single_real_config_telegram_json(self):
        """Verify config/telegram.json is the real single config source."""
        with patch.object(telegram_channel, "_CONFIG_PATH", self.telegram_json):
            telegram_channel.set_allowed_user_ids([123456789, 987654321])
            self.assertTrue(self.telegram_json.exists())
            
            data = json.loads(self.telegram_json.read_text(encoding="utf-8"))
            self.assertEqual(data.get("allowed_user_ids"), [123456789, 987654321])
            self.assertTrue(data.get("enabled"))
            
            loaded = telegram_channel.get_allowed_user_ids()
            self.assertEqual(loaded, {123456789, 987654321})

    def test_telegram_fallback_config_json(self):
        """Verify backwards compatibility with config/config.json if telegram.json is missing."""
        self.config_json.write_text(
            json.dumps({
                "telegram": {
                    "allowed_user_ids": [555444333],
                    "enabled": True
                }
            }),
            encoding="utf-8"
        )
        with patch.object(telegram_channel, "_CONFIG_PATH", self.telegram_json):
            # telegram.json does not exist yet
            loaded = telegram_channel.get_allowed_user_ids()
            self.assertEqual(loaded, {555444333})


if __name__ == "__main__":
    unittest.main()
