"""Telegram command / message handlers.

Each handler is a thin function that:

1. checks authorization,
2. validates its inputs,
3. calls into ``runner`` / ``session_store``,
4. formats output through ``telegram_io``.

Cross-handler mutable state (the current workspace, the sessions map, the
active model and permission mode) is bundled in :class:`AppState` and passed
in - no module-level globals shared between handlers.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import signal
import time
import zipfile
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import ContextTypes

import config
import models
import runner
import session_store
import telegram_io
from auth import authorized
from state import claude_lock, current_run

log = logging.getLogger(__name__)


@dataclass
class AppState:
    """Bundle of per-process mutable state passed to each handler."""

    active_dir: str
    sessions: dict[str, str] = field(default_factory=dict)
    # None = defer to the CLI (or ANTHROPIC_MODEL from the environment).
    # Set by /model; persisted via ``models.save_active_model``.
    active_model: str | None = None
    # The --permission-mode for future runs. Set by /perm; persisted via
    # ``save_active_permission``. Defaults to dontAsk, not bypassPermissions.
    permission_mode: str = config.DEFAULT_PERMISSION_MODE


def _fmt_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return (f"{hours}h " if hours else "") + f"{minutes}m {secs}s"


def _inside_base(target: str) -> bool:
    """True if ``target`` resolves inside ``BASE_DIR``."""
    try:
        common = os.path.normcase(os.path.commonpath([target, config.BASE_DIR]))
    except ValueError:
        # Different drives on Windows raise here; treat as rejection.
        return False
    return common == os.path.normcase(config.BASE_DIR)


# --- Handler factories ---------------------------------------------------
# python-telegram-bot passes only ``(update, context)`` to handlers, so we
# build closures that capture ``AppState``. That keeps the state explicit and
# easy to test, instead of reaching for module globals.


def make_projects_command(state: AppState):
    async def projects_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        try:
            if not os.path.isdir(config.BASE_DIR):
                await update.message.reply_text(f"📂 Base directory {config.BASE_DIR} not found.")
                return

            dirs = sorted(
                d for d in os.listdir(config.BASE_DIR)
                if os.path.isdir(os.path.join(config.BASE_DIR, d))
            )

            if not dirs:
                await update.message.reply_text(f"📂 No directories in {config.BASE_DIR}")
                return

            lines = [f"📂 Projects in {config.BASE_DIR}:\n"]
            for d in dirs:
                marker = "✅" if os.path.join(config.BASE_DIR, d) == state.active_dir else "  •"
                lines.append(f"{marker} {d}")

            await update.message.reply_text("\n".join(lines))
        except Exception as e:
            log.exception("projects_command failed")
            await update.message.reply_text(f"❌ Error: {e}")

    return projects_command


def make_switch_command(state: AppState):
    async def switch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        if not context.args:
            await update.message.reply_text("Usage: /switch <folder>")
            return

        folder = context.args[0]
        target_dir = os.path.abspath(os.path.join(config.BASE_DIR, folder))

        if not _inside_base(target_dir):
            await update.message.reply_text("⛔ Invalid path: Cannot switch outside base workspace.")
            return

        try:
            os.makedirs(target_dir, exist_ok=True)
            state.active_dir = target_dir
            # No parse_mode: a path with a stray backtick/underscore would make
            # Telegram reject Markdown and raise here.
            await update.message.reply_text(f"✅ Switched to {state.active_dir}")
        except Exception as e:
            log.exception("switch_command failed")
            await update.message.reply_text(f"❌ Error: {e}")

    return switch_command


def make_reset_command(state: AppState):
    async def reset_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        # Take the lock so we don't delete a session while run_claude is using
        # it.
        async with claude_lock:
            if state.active_dir in state.sessions:
                del state.sessions[state.active_dir]
                session_store.save_sessions(state.sessions)
                fresh = False
            else:
                fresh = True

        if fresh:
            await update.message.reply_text("🔄 Session is already fresh.")
        else:
            await update.message.reply_text(
                "🔄 Active session reset! Next message will start a fresh conversation."
            )

    return reset_command


def load_active_permission() -> str:
    """Return the persisted /perm choice, else the env default, else config default.

    Mirrors ``models.load_active_model``. The file wins, then
    ``PERMISSION_MODE`` env, then ``DEFAULT_PERMISSION_MODE``. Invalid values
    are caught by the caller when the value is used to spawn a run.
    """
    if os.path.exists(config.PERMISSION_FILE):
        try:
            with open(config.PERMISSION_FILE, "r") as f:
                val = f.read().strip()
                if val:
                    return val
        except OSError as e:
            log.warning("Could not read %s: %s", config.PERMISSION_FILE, e)

    env_val = os.environ.get("PERMISSION_MODE", "").strip()
    return env_val or config.DEFAULT_PERMISSION_MODE


def save_active_permission(mode: str) -> None:
    """Atomically persist ``mode`` to :data:`config.PERMISSION_FILE`."""
    tmp = config.PERMISSION_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            f.write(mode)
        os.replace(tmp, config.PERMISSION_FILE)
    except OSError as e:
        log.error("Failed to save permission mode to %s: %s", config.PERMISSION_FILE, e)


def make_permission_command(state: AppState):
    async def permission_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        # The modes that let ``claude -p`` run unattended. manual, acceptEdits
        # and auto ask for approval - with nobody at the terminal those would
        # hang the run, so we refuse them.
        bot_safe = ("bypassPermissions", "dontAsk", "plan")

        # No argument → show current mode + what each option means.
        if not context.args:
            lines: list[str] = [f"⚙️ Current permission mode: {state.permission_mode}\n"]
            lines.append("Available modes:")
            for mode in config.PERMISSION_MODES:
                marker = "✅" if mode == state.permission_mode else "  •"
                hint = _PERMISSION_HINTS.get(mode, "")
                if mode not in bot_safe:
                    hint += " ⚠️ (prompts - will hang a bot run)"
                lines.append(f"{marker} <code>{mode}</code> - {hint}")
            await telegram_io.send_html(update, "\n".join(lines))
            return

        requested = context.args[0].strip()
        if requested not in config.PERMISSION_MODES:
            await update.message.reply_text(
                f"❌ Unknown permission mode '{requested}'. "
                "Run /perm to see the list."
            )
            return
        if requested not in bot_safe:
            await update.message.reply_text(
                f"❌ '{requested}' raises interactive permission prompts. "
                "With no human at the terminal, runs would hang. "
                "Use one of: bypassPermissions, dontAsk, plan."
            )
            return

        async with claude_lock:
            state.permission_mode = requested
            save_active_permission(requested)

        await update.message.reply_text(
            f"✅ Permission mode set to {requested}. Next prompt will use it."
        )

    return permission_command


_PERMISSION_HINTS = {
    "acceptEdits": "auto-allow file edits; asks before bash - prompts hang a bot.",
    "auto": "classifier decides per action - may prompt, hanging a bot run.",
    "bypassPermissions": "full execution authority; no prompts, everything allowed.",
    "manual": "asks before every action - unusable unattended.",
    "dontAsk": "autonomous for normal ops; blocks destructive actions unless allowlisted.",
    "plan": "read-only: no edits or bash; analysis and plans only.",
}


def make_model_command(state: AppState):
    async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        available, err = models.fetch_models()

        # No argument → show the current selection and the list.
        if not context.args:
            lines: list[str] = []
            current = state.active_model or "(CLI default)"
            lines.append(f"🧠 Current model: {current}")
            if err:
                lines.append(f"⚠️ {err}")
            lines.append("")
            if not available:
                lines.append("(No models available from router.)")
            else:
                lines.append(f"📋 Available models ({len(available)}):")
                for m in available:
                    mid = m.get("id", "?")
                    owner = m.get("owned_by") or ""
                    marker = "✅" if mid == state.active_model else "  •"
                    suffix = f"  [{owner}]" if owner else ""
                    lines.append(f"{marker} {mid}{suffix}")
            # send_chunks - the list can exceed one Telegram message.
            await telegram_io.send_chunks(update, "\n".join(lines))
            return

        # With argument → validate against the router list, then switch.
        requested = context.args[0].strip()
        if not available:
            # No cache and router unreachable - refuse the switch rather than
            # commit to an unverifiable ID.
            reason = err or "no models returned by router"
            await update.message.reply_text(
                f"❌ Cannot switch: {reason}. Fix the router and try again."
            )
            return

        known_ids = {m.get("id") for m in available}
        if requested not in known_ids:
            await update.message.reply_text(
                f"❌ Unknown model '{requested}'. Run /model to see the list."
            )
            return

        # Wait for any running claude subprocess so we don't swap the persisted
        # model mid-run. A subprocess already running keeps the env it started
        # with; the switch applies to the *next* run.
        async with claude_lock:
            state.active_model = requested
            models.save_active_model(requested)

        await update.message.reply_text(
            f"✅ Model switched to {requested}. Next prompt will use it."
        )

    return model_command


def make_message_handler(state: AppState):
    async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        prompt = (update.message.text or "").strip()
        if not prompt:
            return

        # If a task is already running, say so instead of quietly queueing
        # behind the lock, which could block for the whole length of a long
        # run.
        if claude_lock.locked():
            await update.message.reply_text(
                f"⏳ A task is already in progress (PID: {current_run.pid}).\n\n"
                "Use /status to check progress or /cancel to stop it."
            )
            return

        try:
            # run_claude sends the answer itself so it can keep the live
            # preview on screen until delivery succeeds.
            await runner.run_claude(
                update,
                prompt,
                state.sessions,
                state.active_dir,
                active_model=state.active_model,
                permission_mode=state.permission_mode,
            )
        except Exception as e:
            log.exception("handle_message failed")
            await update.message.reply_text(f"❌ Error: {e}")

    return handle_message


def make_document_handler(state: AppState):
    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        document = update.message.document

        if claude_lock.locked():
            await update.message.reply_text(
                f"⏳ A task is already in progress (PID: {current_run.pid}).\n\n"
                "Use /status to check progress or /cancel to stop it."
            )
            return

        if document.file_size and document.file_size > config.MAX_UPLOAD_BYTES:
            limit_mb = config.MAX_UPLOAD_BYTES / (1024 * 1024)
            await update.message.reply_text(
                f"⛔ File too large ({document.file_size} bytes); limit is {limit_mb:.0f} MiB."
            )
            return

        # Snapshot the workspace now and run Claude in that same directory, so
        # a /switch arriving meanwhile can't put the file in one workspace and
        # run in another.
        target_dir = state.active_dir

        # Strip any directory components from the client-supplied name so an
        # upload can't write outside the workspace.
        filename = os.path.basename(document.file_name or "uploaded_file") or "uploaded_file"
        file_path = os.path.join(target_dir, filename)

        # Refuse to silently clobber an existing file.
        if os.path.exists(file_path):
            await update.message.reply_text(
                f"⛔ '{filename}' already exists in the workspace; rename it and re-send."
            )
            return

        caption = update.message.caption or (
            f"I uploaded '{filename}' to the workspace. Please review or analyze it."
        )

        try:
            await update.message.reply_text(f"📥 Downloading '{filename}' to active workspace...")

            file = await context.bot.get_file(document.file_id)
            await file.download_to_drive(custom_path=file_path)

            await runner.run_claude(
                update, caption, state.sessions, state.active_dir,
                run_dir=target_dir,
                active_model=state.active_model,
                permission_mode=state.permission_mode,
            )

        except Exception as e:
            log.exception("handle_document failed")
            await update.message.reply_text(f"❌ Error processing file: {e}")

    return handle_document


def make_status_command(state: AppState):
    async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        # If nothing is set yet but the lock is held, the task is starting up.
        if current_run.proc is None and claude_lock.locked():
            await update.message.reply_text("⏳ Task is currently starting up / initializing process...")
            return

        proc = current_run.proc
        start_time = current_run.start_time
        if proc is None or start_time is None:
            await update.message.reply_text("💤 No task is currently running.")
            return

        elapsed_txt = _fmt_duration(int(time.time() - start_time))

        preview = (current_run.prompt or "").strip().replace("\n", " ")
        if len(preview) > 200:
            preview = preview[:200] + "…"

        # Plain text (no Markdown): a prompt containing _ or * would otherwise
        # make Telegram reject the message with a 400 and crash this handler.
        await update.message.reply_text(
            "🤖 Task in progress\n\n"
            f"PID: {current_run.pid}\n"
            f"Workspace: {current_run.run_dir or state.active_dir}\n"
            f"Elapsed: {elapsed_txt}\n"
            f"Prompt: {preview}\n\n"
            "Use /cancel to stop it."
        )

    return status_command


def make_cancel_command():
    async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        proc = current_run.proc
        pid = current_run.pid
        if proc is None or pid is None:
            await update.message.reply_text("✅ No task to cancel.")
            return

        try:
            # start_new_session=True made the child a group leader, so
            # pid == pgid. SIGTERM first so Claude can flush stream-json events,
            # then escalate to SIGKILL if it hasn't exited within the grace
            # window.
            os.killpg(pid, signal.SIGTERM)
            await asyncio.sleep(config.CANCEL_GRACE_SECONDS)
            if proc.returncode is None:
                os.killpg(pid, signal.SIGKILL)
            await update.message.reply_text(f"🛑 Terminated task (PID {pid}).")
        except ProcessLookupError:
            await update.message.reply_text(f"⚠️ Process (PID {pid}) already exited.")
        except Exception as e:
            log.exception("cancel_command failed")
            await update.message.reply_text(f"❌ Error cancelling task: {e}")

    return cancel_command


# The /help output. HTML formatting keeps it consistent with send_chunks.
HELP_HTML = (
    "🤖 <b>dikodingbot Command Menu</b>\n\n"
    "• <code>/projects</code> - List all folders inside <code>BASE_DIR</code> and highlight active folder.\n"
    "• <code>/switch &lt;folder&gt;</code> - Switch active workspace to <code>BASE_DIR/&lt;folder&gt;</code>.\n"
    "• <code>/reset</code> - Clear active conversation memory for a fresh start.\n"
    "• <code>/model [name]</code> - Show or switch the active Claude model via 9Router.\n"
    "• <code>/perm [mode]</code> - Show or switch the Claude permission mode (default <code>dontAsk</code>).\n"
    "• <code>/status</code> - Show the running task's PID, elapsed time, and prompt.\n"
    "• <code>/cancel</code> - Stop the currently running task.\n"
    "• <code>/code</code> - Send the whole source tree as a ZIP.\n"
    "• <code>/code &lt;file&gt;</code> - Send a single source file (e.g. <code>/code runner.py</code>).\n"
    "• <code>/help</code> - Display this command menu.\n\n"
    "💬 <b>Prompting:</b> Send plain text to instruct Claude Code (context is saved per workspace).\n\n"
    "📎 <b>Uploads:</b> Send any file/document to upload it directly to your active workspace."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Unauthorized")
        return
    await telegram_io.send_html(update, HELP_HTML)


# The directory that holds this module - the bot's source root.
_SRC_ROOT = os.path.abspath(os.path.dirname(__file__))

# Explicit allowlist for /code: what counts as "the source" and is safe to
# send. Anything not listed - .env, sessions.json, active_model.txt, *.bak-*,
# __pycache__/, .github/, venv/ - never enters the ZIP.
#
# Root-level items are file names; ``tests`` is a directory walked recursively
# but limited to ``*.py``. Missing entries are skipped without complaint so a
# partial clone (no LICENSE, say) still works.
_CODE_ROOT_FILES = (
    # Python sources.
    "bot.py",
    "config.py",
    "handlers.py",
    "runner.py",
    "session_store.py",
    "state.py",
    "telegram_io.py",
    "auth.py",
    "models.py",
    # Docs / config templates.
    "requirements.txt",
    "README.md",
    "LICENSE",
    ".env.example",
    ".gitignore",
)
_CODE_TESTS_DIR = "tests"

# Size cap. The ZIP is normally around 30 KB, so anything near 20 MB means
# something unexpected got included; fail loudly instead of sending it.
_CODE_ZIP_SIZE_LIMIT = 20 * 1024 * 1024


def _collect_code_files() -> list[tuple[str, str]]:
    """Return ``(absolute_path, arcname)`` pairs for the /code snapshot.

    ``arcname`` is the path inside the ZIP, relative to the project root, so
    the archive extracts into the same layout.
    """
    entries: list[tuple[str, str]] = []
    for name in _CODE_ROOT_FILES:
        abs_path = os.path.join(_SRC_ROOT, name)
        if os.path.isfile(abs_path):
            entries.append((abs_path, name))

    tests_dir = os.path.join(_SRC_ROOT, _CODE_TESTS_DIR)
    if os.path.isdir(tests_dir):
        for dirpath, _dirnames, filenames in os.walk(tests_dir):
            # Skip caches even if they were somehow untracked.
            if os.path.basename(dirpath) == "__pycache__":
                continue
            for fname in sorted(filenames):
                if not fname.endswith(".py"):
                    continue
                abs_path = os.path.join(dirpath, fname)
                rel = os.path.relpath(abs_path, _SRC_ROOT)
                entries.append((abs_path, rel))
    return entries


def _build_source_zip() -> bytes:
    """Build the /code snapshot ZIP in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in _collect_code_files():
            zf.write(abs_path, arcname=arcname)
    return buf.getvalue()


def _resolve_single_code_file(arg: str) -> str | None:
    """Map a ``/code <arg>`` request to an absolute path inside the source root.

    Returns None when the request is malformed, points outside the root, or
    isn't on the allowlist. ``os.path.basename`` handles ``../`` and absolute
    paths before we touch the filesystem.
    """
    # ``basename`` drops any directory part - /code ../../.env becomes just
    # ".env", which the allowlist then rejects.
    safe = os.path.basename(arg.strip())
    if not safe or safe in (".", ".."):
        return None

    if safe in _CODE_ROOT_FILES:
        candidate = os.path.join(_SRC_ROOT, safe)
        return candidate if os.path.isfile(candidate) else None

    # Also accept "tests/<name>.py" and a bare "test_foo.py" for convenience.
    if safe.endswith(".py"):
        tests_dir = os.path.join(_SRC_ROOT, _CODE_TESTS_DIR)
        candidate = os.path.join(tests_dir, safe)
        if os.path.isfile(candidate):
            return candidate

    return None


async def send_code(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Unauthorized")
        return

    # /code <file> - single-file variant.
    if context.args:
        requested = context.args[0]
        abs_path = _resolve_single_code_file(requested)
        if abs_path is None:
            await update.message.reply_text(
                f"❌ '{requested}' is not part of the source tree. "
                "Use /code (no arg) to get the full ZIP, or one of the "
                "listed source files (see /help)."
            )
            return
        filename = os.path.basename(abs_path)
        await telegram_io.send_html(update, f"📄 Sending <code>{filename}</code>...")
        with open(abs_path, "rb") as fh:
            await update.message.reply_document(
                document=fh,
                filename=filename,
                caption=f"{filename} from dikodingbot source.",
            )
        return

    # /code - full source ZIP built in-memory.
    try:
        payload = _build_source_zip()
    except OSError as e:
        log.exception("send_code: zip build failed")
        await update.message.reply_text(f"❌ Could not build source ZIP: {e}")
        return

    if len(payload) > _CODE_ZIP_SIZE_LIMIT:
        # Something unexpected got in - report it instead of sending an
        # archive this big.
        await update.message.reply_text(
            f"❌ Source ZIP is unexpectedly large ({len(payload)} bytes); refusing to send. "
            "Check for stray non-source files in the project root."
        )
        return

    # Timestamp so the operator can tell snapshots apart on their phone.
    stamp = time.strftime("%Y%m%d-%H%M%S")
    zip_name = f"dikodingbot-{stamp}.zip"

    await telegram_io.send_html(
        update,
        f"📦 Sending source snapshot as <code>{zip_name}</code>...",
    )
    await update.message.reply_document(
        document=io.BytesIO(payload),
        filename=zip_name,
        caption="Full dikodingbot source tree.",
    )
