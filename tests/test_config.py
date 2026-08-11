"""Tests for the config module - env parsing and validation."""

from __future__ import annotations

import os
import pathlib
import unittest
from unittest.mock import patch

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]

BASE_ENV = {
    "BOT_TOKEN": "123456789:test-token",
    "ALLOWED_USER_ID": "123456",
    "CLAUDE_TIMEOUT": "17",
    "BASE_DIR": str(ROOT_DIR),
    "SESSION_FILE": str(ROOT_DIR / "sessions-test.json"),
}


def _fresh_config(overrides: dict | None = None):
    env = dict(BASE_ENV)
    if overrides:
        env.update(overrides)
    return fresh_import("config", env)


class ConfigTests(unittest.TestCase):
    def test_int_env_uses_default_when_unset(self):
        cfg = _fresh_config()
        with patch.dict(os.environ, {"SOME_NUMBER": ""}, clear=False):
            self.assertEqual(cfg._int_env("SOME_NUMBER", 42), 42)

    def test_invalid_allowed_user_id_raises(self):
        with self.assertRaises(ValueError):
            _fresh_config({"ALLOWED_USER_ID": "not-a-number"})

    def test_timeout_loaded_from_env(self):
        cfg = _fresh_config({"CLAUDE_TIMEOUT": "99"})
        self.assertEqual(cfg.CLAUDE_TIMEOUT, 99)

    def test_validate_raises_when_bot_token_missing(self):
        cfg = _fresh_config()
        with patch.object(cfg, "BOT_TOKEN", None):
            with self.assertRaises(ValueError):
                cfg.validate()

    def test_validate_warns_but_does_not_raise_when_no_user_id(self):
        cfg = _fresh_config()
        with patch.object(cfg, "ALLOWED_USER_ID", 0):
            # Should log a warning, not raise.
            cfg.validate()


if __name__ == "__main__":
    unittest.main()
