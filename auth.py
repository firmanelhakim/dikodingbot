"""Single-operator authorization gate."""

from __future__ import annotations

from telegram import Update

import config


def authorized(update: Update) -> bool:
    """True only for the single allowed operator.

    Returns False when ``ALLOWED_USER_ID`` is 0 (fail-safe) or when the update
    has no user (Telegram sometimes delivers system messages that lack one).
    """
    user = update.effective_user
    return user is not None and user.id == config.ALLOWED_USER_ID
