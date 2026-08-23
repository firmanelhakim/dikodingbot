"""Configuration loading - env vars and derived constants.

Imported first by everything else. Reads the environment once at import time
and exposes typed module-level constants. Bad values raise at startup with a
clear message, so misconfiguration shows up immediately instead of deep inside
a handler.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

log = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var, raising a clear error on bad input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"❌ {name} must be an integer, got {raw!r}")


def _float_env(name: str, default: float) -> float:
    """Parse a float env var, raising a clear error on bad input."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"❌ {name} must be a number, got {raw!r}")


# --- Paths ---------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_FILE = os.environ.get("SESSION_FILE", os.path.join(SCRIPT_DIR, "sessions.json"))
# Persisted /model selection. One line, atomic write via tmp-and-rename.
MODEL_FILE = os.environ.get("MODEL_FILE", os.path.join(SCRIPT_DIR, "active_model.txt"))

# Persisted /perm selection (one of the permission modes below). Works the
# same way as MODEL_FILE, so a runtime switch survives a restart.
PERMISSION_FILE = os.environ.get("PERMISSION_FILE", os.path.join(SCRIPT_DIR, "active_permission.txt"))

# Persisted /bind selection: {thread_id: folder path}. One entry per topic,
# written atomically the same way as SESSION_FILE. Empty until the operator
# binds a topic.
TOPICS_FILE = os.environ.get("TOPICS_FILE", os.path.join(SCRIPT_DIR, "topics.json"))

# Persisted per-folder permission modes: {folder path: mode}. Only folders
# that were given a mode via ``/perm`` in a topic appear here; every other
# folder falls back to the global default from PERMISSION_FILE.
PERMISSIONS_FILE = os.environ.get("PERMISSIONS_FILE", os.path.join(SCRIPT_DIR, "permissions.json"))

# Persisted per-folder model overrides: {folder path: model id}. Only folders
# that were given a model via ``/model`` in a topic appear here; every other
# folder falls back to the global selection from MODEL_FILE / ANTHROPIC_MODEL.
MODELS_FILE = os.environ.get("MODELS_FILE", os.path.join(SCRIPT_DIR, "models.json"))

# --- Required ------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
ALLOWED_USER_ID = _int_env("ALLOWED_USER_ID", 0)

# --- Optional (with defaults) --------------------------------------------

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
BASE_DIR = os.path.expanduser(os.environ.get("BASE_DIR", "~/workspace"))

# 0/unset = no timeout; set to N seconds to cap hung calls.
CLAUDE_TIMEOUT = _int_env("CLAUDE_TIMEOUT", 0)

# Default upload cap is 25 MiB.
MAX_UPLOAD_BYTES = _int_env("MAX_UPLOAD_BYTES", 25 * 1024 * 1024)

# Safe chunk size for a single Telegram message.
MAX_CHUNK = 3900

# --- Telegram rate limiting ----------------------------------------------
#
# Telegram allows roughly 20 messages per minute per chat, and editMessageText
# counts against the same quota as sendMessage. The live preview therefore
# competes with the final answer: edit too often during a long run and there
# is no budget left to deliver the result, which fails with 429 "Flood control
# exceeded" and loses the output.

# Seconds between live-preview edits. At 4.0 the preview uses about 15 edits a
# minute, leaving headroom for the final message. Lower values look smoother
# but risk starving the delivery that actually matters.
LIVE_EDIT_INTERVAL = _float_env("LIVE_EDIT_INTERVAL", 4.0)

# How many times to retry a message that Telegram rejected with 429. Each
# retry waits for the retry_after value the API returns.
SEND_MAX_RETRIES = _int_env("SEND_MAX_RETRIES", 3)

# Upper bound (seconds) on a single retry wait. Telegram usually asks for
# 10-30s; anything longer than this means we give up and fall back to sending
# the output as a file.
SEND_MAX_RETRY_WAIT = _float_env("SEND_MAX_RETRY_WAIT", 60.0)

# Pause between consecutive chunks of a long reply, to avoid tripping the rate
# limit with our own burst.
SEND_CHUNK_DELAY = _float_env("SEND_CHUNK_DELAY", 0.4)

# --- Shared outbound pacing -----------------------------------------------
#
# The per-chat flood limit (~20 messages/minute) is shared by every topic in a
# group, so with two runs streaming in parallel the previews alone can exhaust
# the budget. The token bucket below is global per chat: sends and edits from
# every run funnel through it, throttling under contention but leaving a single
# run alone.

# Tokens per minute for the shared bucket. At 19 the bucket still leaves
# headroom for the 20/minute ceiling; one run at the default 4s edit interval
# (~15/min) stays under it, while two runs sharing the chat are paced down.
SEND_RATE_LIMIT = _float_env("SEND_RATE_LIMIT", 19.0)

# Burst allowance above the sustained rate, so a short run or a multi-chunk
# answer can go out immediately before settling into the rate.
SEND_RATE_BURST = _float_env("SEND_RATE_BURST", 5.0)

# Buffer cap for a single line of Claude's stream-json output. asyncio's own
# default is 64 KiB, which one large tool result (a fetched web page, a big
# file read) can exceed - readline() then raises "Separator is not found, and
# chunk exceed the limit" and the run dies. Research prompts hit this often;
# observed lines run past 1 MB. The buffer only grows as needed, so a high cap
# costs nothing on ordinary runs.
CLAUDE_STREAM_LIMIT = _int_env("CLAUDE_STREAM_LIMIT", 10 * 1024 * 1024)

# How long to wait between SIGTERM and SIGKILL when cancelling a run. Claude
# needs a moment to flush its last stream-json events; too short a wait loses
# the end of the response.
CANCEL_GRACE_SECONDS = 1.5

# --- 9Router / model selection ------------------------------------------

# Router base URL (OpenAI-compatible /v1/models endpoint). This is the same
# variable the Claude CLI reads, so /model and the CLI always agree. The
# trailing slash is stripped so we can append "/v1/models".
ROUTER_BASE_URL = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
ROUTER_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# In-process cache TTL for the model list. Keeps /model fast without querying
# the router on every call; 60s is short enough that new models show up soon
# after a router reload, with no bot restart needed.
MODEL_CACHE_TTL = _int_env("MODEL_CACHE_TTL", 60)

# --- /perm (permission mode) --------------------------------------------

# The six --permission-mode values the Claude CLI accepts, in the order its
# help lists them. Only three (bypassPermissions, dontAsk, plan) work for an
# unattended bot - the others ask for approval, which would hang ``claude -p``.
# That check lives in ``handlers.make_permission_command``.
PERMISSION_MODES = (
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
)

# Default for a fresh bot. Deliberately not bypassPermissions - dontAsk runs
# normal file and bash work without asking, but refuses the most destructive
# actions unless they are allowlisted. Set PERMISSION_MODE=bypassPermissions
# in the environment to get the old full-power behavior.
DEFAULT_PERMISSION_MODE = "dontAsk"


def validate() -> None:
    """Verify required config is present. Called from bot startup."""
    if not BOT_TOKEN:
        raise ValueError("❌ BOT_TOKEN is missing! Please check your .env file.")

    if not ALLOWED_USER_ID:
        log.warning(
            "ALLOWED_USER_ID is unset or 0 - every message will be rejected as unauthorized."
        )
