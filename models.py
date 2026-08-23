"""Model discovery + persisted /model selection.

The Claude CLI reads its model choice from ``ANTHROPIC_MODEL``. This module
lets the bot override that per-run by:

1. persisting the operator's ``/model <name>`` choice to :data:`config.MODEL_FILE`,
2. reading it back at startup (falling back to the env var, then to None),
3. listing models available on the configured 9Router
   (``${ANTHROPIC_BASE_URL}/v1/models``) with a short in-process cache.

Uses only the standard library (``urllib.request``) so ``requirements.txt``
doesn't grow just for one endpoint.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request

import config

log = logging.getLogger(__name__)


# Cache the router's model list so /model doesn't make a network call every
# time. ``(fetched_at, models)``; None = never fetched.
_model_list_cache: "tuple[float, list[dict]] | None" = None


def load_active_model() -> str | None:
    """Return the persisted model choice, else the env default, else None.

    None means "let the CLI decide" - we won't inject ``ANTHROPIC_MODEL`` into
    the subprocess env and the CLI's own default takes over.
    """
    if os.path.exists(config.MODEL_FILE):
        try:
            with open(config.MODEL_FILE, "r") as f:
                val = f.read().strip()
                if val:
                    return val
        except OSError as e:
            # A missing or unreadable file must not crash startup - log why
            # and fall through to env / None.
            log.warning("Could not read %s: %s", config.MODEL_FILE, e)

    env_val = os.environ.get("ANTHROPIC_MODEL", "").strip()
    return env_val or None


def save_active_model(model: str) -> None:
    """Atomically persist ``model`` to :data:`config.MODEL_FILE`.

    Writes to a ``.tmp`` sibling and renames, the same way ``save_sessions``
    does, so a crash mid-write can't leave a half-written file.
    """
    tmp = config.MODEL_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(model)
        os.replace(tmp, config.MODEL_FILE)
    except OSError as e:
        log.error("Failed to save model to %s: %s", config.MODEL_FILE, e)


def load_model_overrides() -> dict[str, str]:
    """Load the per-folder model map from disk on startup.

    Same shape and error tolerance as the other JSON maps: missing or corrupt
    files give an empty dict.
    """
    if not os.path.exists(config.MODELS_FILE):
        return {}
    try:
        with open(config.MODELS_FILE, "r") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                log.warning("%s did not contain a dict; ignoring.", config.MODELS_FILE)
                return {}
            return data
    except (OSError, json.JSONDecodeError) as e:
        log.warning("Could not read %s: %s", config.MODELS_FILE, e)
        return {}


def save_model_overrides(overrides: dict[str, str]) -> None:
    """Atomically persist the per-folder model map."""
    tmp = config.MODELS_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(overrides, f, indent=2)
        os.replace(tmp, config.MODELS_FILE)
    except OSError as e:
        log.error("Failed to save model overrides to %s: %s", config.MODELS_FILE, e)


def fetch_models(force: bool = False) -> tuple[list[dict], str | None]:
    """Return ``(models, error)`` from the router.

    ``models`` is a list of ``{"id": ..., "owned_by": ...}`` dicts (OpenAI
    shape). ``error`` is None on success, or a short readable string when the
    fetch failed. On network failure we return the last cached list (possibly
    empty) *and* the error - the caller decides whether to show it.
    """
    global _model_list_cache

    now = time.time()
    if (
        not force
        and _model_list_cache is not None
        and (now - _model_list_cache[0]) < config.MODEL_CACHE_TTL
    ):
        return _model_list_cache[1], None

    if not config.ROUTER_BASE_URL:
        stale = _model_list_cache[1] if _model_list_cache else []
        return stale, "ANTHROPIC_BASE_URL is unset - no router to query."

    url = f"{config.ROUTER_BASE_URL}/v1/models"
    req = urllib.request.Request(url)
    if config.ROUTER_API_KEY:
        req.add_header("Authorization", f"Bearer {config.ROUTER_API_KEY}")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.load(resp)
        models = payload.get("data", []) or []
        _model_list_cache = (now, models)
        return models, None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        # Return the last known list so the operator can still see what is
        # selected when the router is down.
        stale = _model_list_cache[1] if _model_list_cache else []
        return stale, f"Router error: {e}"
