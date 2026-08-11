"""Persistent per-workspace Claude session mapping.

The mapping is ``{workspace_absolute_path: claude_session_uuid}``. It is
written atomically (tmp-then-rename) so a crash mid-write never leaves a
half-written file. Loading handles both a missing and a corrupt file: either
one gives an empty map instead of an error.
"""

from __future__ import annotations

import json
import logging
import os

import config

log = logging.getLogger(__name__)


def load_sessions() -> dict[str, str]:
    """Load the session mapping from disk on startup."""
    if not os.path.exists(config.SESSION_FILE):
        return {}
    try:
        with open(config.SESSION_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                log.warning("%s did not contain a dict; ignoring.", config.SESSION_FILE)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s: %s", config.SESSION_FILE, e)
        return {}


def save_sessions(sessions: dict[str, str]) -> None:
    """Atomically persist the session mapping."""
    tmp = config.SESSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(sessions, f, indent=2)
        # os.replace is atomic on POSIX; never leaves a half-written file.
        os.replace(tmp, config.SESSION_FILE)
    except OSError as e:
        log.error("Error saving sessions: %s", e)
