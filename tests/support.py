"""Shared test scaffolding.

The bot modules import ``telegram`` at import time. Tests stub the library so
they can run without installing python-telegram-bot.

``fresh_import`` re-imports a bot module under a controlled environment. Every
cached bot module is dropped before re-importing, so ``import config`` inside
the reloaded module picks up the test environment.
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from unittest.mock import patch


# Bot modules that need a fresh import during tests.
_BOT_MODULES = (
    "config",
    "session_store",
    "state",
    "telegram_io",
    "auth",
    "runner",
    "models",
    "handlers",
)


def install_telegram_stubs() -> None:
    """Provide minimal ``telegram`` / ``telegram.ext`` modules for tests."""
    if "telegram" not in sys.modules:
        telegram = types.ModuleType("telegram")

        class Update:  # pragma: no cover - lightweight import stub
            pass

        telegram.Update = Update
        # Mark it as a package so ``telegram.error`` below is importable.
        telegram.__path__ = []
        sys.modules["telegram"] = telegram

    if "telegram.error" not in sys.modules:
        telegram_error = types.ModuleType("telegram.error")

        class TelegramError(Exception):
            pass

        class RetryAfter(TelegramError):
            """Flood-control rejection. Real PTB v22.2+ gives a timedelta."""

            def __init__(self, retry_after):
                super().__init__(f"Flood control exceeded. Retry in {retry_after}")
                self.retry_after = retry_after

        telegram_error.TelegramError = TelegramError
        telegram_error.RetryAfter = RetryAfter
        sys.modules["telegram.error"] = telegram_error
        sys.modules["telegram"].error = telegram_error

    if "telegram.ext" not in sys.modules:
        telegram_ext = types.ModuleType("telegram.ext")

        class _DummyBuilder:
            def token(self, *_args, **_kwargs):
                return self

            def concurrent_updates(self, *_args, **_kwargs):
                return self

            def build(self):
                return self

            def add_handler(self, *_args, **_kwargs):
                return None

            def run_polling(self):
                return None

        class _Dummy:
            def __init__(self, *_args, **_kwargs):
                pass

        class _Filters:
            TEXT = object()
            COMMAND = object()

            class Document:
                ALL = object()

        class _ContextTypes:
            DEFAULT_TYPE = object

        telegram_ext.ApplicationBuilder = _DummyBuilder
        telegram_ext.CommandHandler = _Dummy
        telegram_ext.ContextTypes = _ContextTypes
        telegram_ext.MessageHandler = _Dummy
        telegram_ext.filters = _Filters
        sys.modules["telegram.ext"] = telegram_ext


def flush_bot_modules() -> None:
    """Drop every already-loaded bot module so the next import re-runs."""
    for name in _BOT_MODULES:
        sys.modules.pop(name, None)


def fresh_import(module_name: str, env: dict[str, str] | None = None):
    """Import a bot module under a controlled environment.

    ``patch.dict`` applies the env for the duration of the import, which is
    when ``config`` reads its constants.
    """
    flush_bot_modules()
    env = env or {}
    with patch.dict(os.environ, env, clear=False):
        return importlib.import_module(module_name)


def make_fake_update(user_id: int | None):
    """Create a minimal Update-like object for authorization tests."""

    class _User:
        def __init__(self, uid):
            self.id = uid

    class _Update:
        def __init__(self, uid):
            self.effective_user = _User(uid) if uid is not None else None

    return _Update(user_id)
