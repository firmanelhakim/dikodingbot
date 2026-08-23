"""Topic routing - map a forum topic to a workspace folder.

dikodingbot treats a forum topic as a project: each topic is bound (via
``/bind``) to one folder under ``BASE_DIR``, and messages in that topic run
Claude there. The General topic and private chats have no ``message_thread_id``,
so they fall back to the single active directory chosen with ``/switch``.

The mapping is ``{thread_id: folder_path}``, persisted the same way as
``session_store`` (atomic tmp-then-rename). Thread ids are integers on the wire
but stored as strings, since JSON object keys must be strings.
"""

from __future__ import annotations

import json
import logging
import os
import re

import config

log = logging.getLogger(__name__)


def load_topics() -> dict[str, str]:
    """Load the topic map from disk on startup.

    Missing or corrupt files give an empty map rather than an error, so the
    bot still starts and the operator can re-run ``/bind``.
    """
    if not os.path.exists(config.TOPICS_FILE):
        return {}
    try:
        with open(config.TOPICS_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                log.warning("%s did not contain a dict; ignoring.", config.TOPICS_FILE)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s: %s", config.TOPICS_FILE, e)
        return {}


def save_topics(topics: dict[str, str]) -> None:
    """Atomically persist the topic map."""
    tmp = config.TOPICS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(topics, f, indent=2)
        os.replace(tmp, config.TOPICS_FILE)
    except OSError as e:
        log.error("Error saving topics: %s", e)


def load_permissions() -> dict[str, str]:
    """Load the per-folder permission map from disk on startup.

    Same shape and error tolerance as :func:`load_topics`: missing or corrupt
    files give an empty map.
    """
    if not os.path.exists(config.PERMISSIONS_FILE):
        return {}
    try:
        with open(config.PERMISSIONS_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                log.warning("%s did not contain a dict; ignoring.", config.PERMISSIONS_FILE)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s: %s", config.PERMISSIONS_FILE, e)
        return {}


def save_permissions(permissions: dict[str, str]) -> None:
    """Atomically persist the per-folder permission map."""
    tmp = config.PERMISSIONS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(permissions, f, indent=2)
        os.replace(tmp, config.PERMISSIONS_FILE)
    except OSError as e:
        log.error("Error saving permissions: %s", e)


def confined_to(base: str, target: str) -> bool:
    """True if ``target`` resolves inside ``base``, symlinks followed.

    Uses ``realpath`` so a symlink placed inside ``base`` that points outside
    it is rejected, not walked. A ``commonpath`` check on the unresolved path
    would miss that. This is the same check ``/list`` and ``/code`` use.
    """
    real_base = os.path.realpath(base)
    real_target = os.path.realpath(target)
    try:
        common = os.path.normcase(os.path.commonpath([real_target, real_base]))
    except ValueError:
        # Different drives on Windows raise here; treat as rejection.
        return False
    return common == os.path.normcase(real_base)


def slugify(name: str) -> str:
    """Map a topic name to a safe folder name for later auto-provision.

    Lowercase, replace any run of non-alphanumerics with one dash, and strip
    leading/trailing dashes. A name that is all punctuation yields ``topic``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "topic"


def resolve_dir(update, state) -> tuple[str | None, str | None]:
    """Return ``(dir, error)`` for the workspace this update should use.

    A message in a forum topic (non-None ``message_thread_id``) resolves to
    that topic's bound folder; an unbound topic is an error, so a message never
    silently runs in the wrong folder. The General topic and private chats have
    no thread id and resolve to ``state.active_dir``.
    """
    msg = getattr(update, "message", None)
    thread_id = getattr(msg, "message_thread_id", None) if msg is not None else None
    if thread_id is None:
        return state.active_dir, None

    folder = state.topics.get(str(thread_id))
    if folder is None:
        return None, "This topic is not bound to a folder. Use /bind <folder> first."
    return folder, None
