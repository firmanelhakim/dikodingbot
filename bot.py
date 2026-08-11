#!/usr/bin/env python3
"""dikodingbot - Telegram bridge to Claude Code CLI.

Entry point. Wires configuration, session storage, and handlers into a
python-telegram-bot application and starts long-polling.
"""

from __future__ import annotations

import logging
import os
import sys

# Support ``python bot.py`` from any directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import config
import handlers
import models
import session_store
from handlers import AppState

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)


def _setup_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=getattr(logging, level, logging.INFO),
    )


def main() -> None:
    _setup_logging()
    log = logging.getLogger("dikodingbot")

    config.validate()

    state = AppState(
        active_dir=config.BASE_DIR,
        sessions=session_store.load_sessions(),
        active_model=models.load_active_model(),
        permission_mode=handlers.load_active_permission(),
    )

    # Startup banner. bypassPermissions gives Claude full local execution
    # authority, so it gets a loud warning. The other modes still execute code
    # locally, just with limits, so they get a shorter note.
    if state.permission_mode == "bypassPermissions":
        log.warning(
            "⚠️  Running claude with --permission-mode bypassPermissions - "
            "Claude has full local execution authority. Only expose this bot to a "
            "trusted operator on a trusted network."
        )
    else:
        log.warning(
            "Running claude with --permission-mode %s. Claude can still execute "
            "code locally - only expose this bot to a trusted operator.",
            state.permission_mode,
        )

    app = ApplicationBuilder().token(config.BOT_TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("projects", handlers.make_projects_command(state)))
    app.add_handler(CommandHandler("switch", handlers.make_switch_command(state)))
    app.add_handler(CommandHandler("reset", handlers.make_reset_command(state)))
    app.add_handler(CommandHandler("model", handlers.make_model_command(state)))
    app.add_handler(CommandHandler("perm", handlers.make_permission_command(state)))
    app.add_handler(CommandHandler("status", handlers.make_status_command(state), block=False))
    app.add_handler(CommandHandler("cancel", handlers.make_cancel_command(), block=False))
    app.add_handler(CommandHandler("help", handlers.help_command))
    app.add_handler(CommandHandler("start", handlers.help_command))
    app.add_handler(CommandHandler("code", handlers.send_code))
    app.add_handler(MessageHandler(filters.Document.ALL, handlers.make_document_handler(state)))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.make_message_handler(state))
    )

    log.info(
        "🚀 dikodingbot started. Active workspace: %s. Model: %s. Permission mode: %s",
        state.active_dir,
        state.active_model or "(CLI default)",
        state.permission_mode,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
