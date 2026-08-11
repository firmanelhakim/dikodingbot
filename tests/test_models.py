"""Tests for the models module - the persisted /model selection.

The network path (``fetch_models``) is integration-level and stays out of the
unit tests; here we only cover the file/env fallback rules.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


ROOT_DIR = pathlib.Path(__file__).resolve().parents[1]

BASE_ENV = {
    "BOT_TOKEN": "123456789:test-token",
    "ALLOWED_USER_ID": "123456",
    "BASE_DIR": str(ROOT_DIR),
    "SESSION_FILE": str(ROOT_DIR / "sessions-test.json"),
}


def _fresh_models(env_overrides: dict):
    """Re-import ``models`` with ``env_overrides`` merged into ``BASE_ENV``."""
    env = dict(BASE_ENV)
    env.update(env_overrides)
    # ``fresh_import`` re-imports ``config`` too, which we need because models
    # reads MODEL_FILE from config when called.
    fresh_import("config", env)
    return fresh_import("models", env)


class LoadActiveModelTests(unittest.TestCase):
    def test_defaults_to_env_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_file = os.path.join(tmp, "active_model.txt")
            mod = _fresh_models({
                "MODEL_FILE": model_file,
                "ANTHROPIC_MODEL": "foo",
            })
            # ``load_active_model`` re-reads os.environ when called; the
            # ambient .env may have set ANTHROPIC_MODEL before the test ran,
            # so pin it here.
            with patch.dict(os.environ, {"ANTHROPIC_MODEL": "foo"}, clear=False):
                self.assertEqual(mod.load_active_model(), "foo")

    def test_returns_none_when_no_file_and_no_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_file = os.path.join(tmp, "active_model.txt")
            mod = _fresh_models({
                "MODEL_FILE": model_file,
                "ANTHROPIC_MODEL": "",
            })
            with patch.dict(os.environ, {"ANTHROPIC_MODEL": ""}, clear=False):
                self.assertIsNone(mod.load_active_model())

    def test_file_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_file = os.path.join(tmp, "active_model.txt")
            with open(model_file, "w") as f:
                f.write("bar\n")  # trailing whitespace is stripped
            mod = _fresh_models({
                "MODEL_FILE": model_file,
                "ANTHROPIC_MODEL": "foo",
            })
            with patch.dict(os.environ, {"ANTHROPIC_MODEL": "foo"}, clear=False):
                self.assertEqual(mod.load_active_model(), "bar")

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            model_file = os.path.join(tmp, "active_model.txt")
            mod = _fresh_models({
                "MODEL_FILE": model_file,
                "ANTHROPIC_MODEL": "",
            })
            mod.save_active_model("gh/claude-sonnet-5")
            self.assertFalse(os.path.exists(model_file + ".tmp"))
            self.assertEqual(mod.load_active_model(), "gh/claude-sonnet-5")


if __name__ == "__main__":
    unittest.main()
