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


# --- /list ---------------------------------------------------------------

# Default depth for ``-r``, counted from the starting folder: level 1 is the
# folder's own entries, level 2 adds each immediate subfolder's entries. An
# explicit depth on the command line overrides this, up to _LIST_MAX_DEPTH.
_LIST_RECURSE_DEPTH = 2
_LIST_MAX_DEPTH = 6

# Cap on entry lines so a dense workspace can't dump a wall of text onto the
# phone; send_chunks would otherwise deliver all of it.
_LIST_LINE_LIMIT = 5000

# Directories /list never descends into. They still show as a name, but their
# contents are noise (a git object store, a virtualenv, node_modules).
_LIST_EXCLUDED_DIRS = frozenset(
    {".git", "venv", ".venv", "node_modules", "__pycache__", ".pytest_cache"}
)

def _parse_list_args(args: list[str]) -> tuple[str | None, bool, int, str | None]:
    """Parse ``/list`` arguments into ``(subdir, recursive, depth, error)``.

    ``-r`` may appear in either position, and ``depth`` (a bare integer)
    follows it when present. Any other flag, more than one positional
    argument, or a non-integer depth is an error.
    """
    subdir = None
    recursive = False
    depth = _LIST_RECURSE_DEPTH
    for arg in args:
        if arg == "-r":
            recursive = True
        elif arg.startswith("-"):
            return None, False, depth, "Unknown flag. Usage: /list [-r] [subdir]"
        elif arg.isdigit():
            # A bare integer is a depth override. It only applies to -r; the
            # handler clamps it and validates it against _LIST_MAX_DEPTH.
            depth = int(arg)
        elif subdir is None:
            subdir = arg
        else:
            return None, False, depth, "Only one subfolder can be listed at a time."
    return subdir, recursive, depth, None

def _resolve_list_target(subdir: str | None, active_dir: str) -> tuple[str | None, str | None]:
    """Resolve a /list target to an absolute path inside ``active_dir``.

    Returns ``(path, error)`` with exactly one of the two set. The target is
    confined to the active workspace via realpath, so a symlink pointing
    outside it is rejected rather than walked.
    """
    target = active_dir if subdir is None else os.path.abspath(os.path.join(active_dir, subdir))
    real_active = os.path.realpath(active_dir)
    real_target = os.path.realpath(target)
    try:
        within = (
            os.path.normcase(os.path.commonpath([real_target, real_active]))
            == os.path.normcase(real_active)
        )
    except ValueError:
        within = False
    if not within:
        return None, "Invalid path: listing is confined to the active workspace."
    if not os.path.isdir(real_target):
        return None, "That path is not a folder in the active workspace."
    return real_target, None

def _list_entries(path: str) -> tuple[list[str], list[str]]:
    """Return ``(dir_names, file_names)`` inside ``path``, each sorted.

    Symlinks are not followed: a symlink to a directory is listed as a file
    rather than descended into, so a link pointing outside the workspace can't
    pull the walk off-root.
    """
    dirs: list[str] = []
    files: list[str] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(entry.name)
                    else:
                        files.append(entry.name)
                except OSError:
                    continue
    except OSError as e:
        log.warning("Could not list %s: %s", path, e)
    dirs.sort()
    files.sort()
    return dirs, files

def _build_listing(root: str, depth: int, line_limit: int) -> str:
    """Build a text listing of ``root``, recursing up to ``depth`` levels.

    Depth 1 lists only ``root``'s entries; depth 2 also lists each immediate
    subfolder's entries. Directories are marked with a trailing slash and
    listed before files, so the layout reads top to bottom like ``tree``.
    """
    body: list[str] = []
    truncated = [False]

    def _append(line: str) -> bool:
        if len(body) >= line_limit:
            truncated[0] = True
            return False
        body.append(line)
        return True

    def _walk(path: str, remaining: int, indent: str) -> None:
        dirs, files = _list_entries(path)
        for d in dirs:
            if not _append(f"{indent}{d}/"):
                return
            if remaining > 1 and d not in _LIST_EXCLUDED_DIRS:
                _walk(os.path.join(path, d), remaining - 1, indent + "  ")
                if truncated[0]:
                    return
        for f in files:
            if not _append(f"{indent}{f}"):
                return

    _walk(root, depth, "")
    if truncated[0]:
        body.append(f"[... truncated after {line_limit} lines]")
    return "\n".join([f"📁 {root}"] + body)

def make_list_command(state: AppState):
    async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        subdir, recursive, depth, error = _parse_list_args(context.args or [])
        if error:
            await update.message.reply_text(error)
            return

        # A depth is only meaningful with -r; without it the top level is
        # listed regardless. Clamp out-of-range values rather than erroring on
        # a harmless typo.
        if not recursive:
            depth = 1
        else:
            depth = max(1, min(depth, _LIST_MAX_DEPTH))

        target, error = _resolve_list_target(subdir, state.active_dir)
        if error:
            await update.message.reply_text(f"⛔ {error}")
            return

        text = _build_listing(target, depth, _LIST_LINE_LIMIT)
        await telegram_io.send_chunks(update, text)

    return list_command

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
    "• <code>/list</code> - List files in the active workspace.\n"
    "• <code>/list &lt;subdir&gt;</code> - List a subfolder of the active workspace.\n"
    "• <code>/list -r [n]</code> - List recursively (n levels down, default 2).\n"
    "• <code>/code</code> - Send the active workspace as a ZIP.\n"
    "• <code>/code &lt;file&gt;</code> - Send a single file from the active workspace.\n"
    "• <code>/help</code> - Display this command menu.\n\n"
    "💬 <b>Prompting:</b> Send plain text to instruct Claude Code (context is saved per workspace).\n\n"
    "📎 <b>Uploads:</b> Send any file/document to upload it directly to your active workspace."
)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not authorized(update):
        await update.message.reply_text("⛔ Unauthorized")
        return
    await telegram_io.send_html(update, HELP_HTML)


# --- /code ---------------------------------------------------------------

# Directories never included in a workspace snapshot. _LIST_EXCLUDED_DIRS is
# the /list prune set; the ZIP also drops editor and tool caches.
_CODE_EXCLUDED_DIRS = _LIST_EXCLUDED_DIRS | frozenset(
    {".mypy_cache", ".ruff_cache", ".idea", ".vscode"}
)

# Files never shipped by /code, at any depth. Secrets and runtime state only;
# the template .env.example is left in on purpose so a fresh clone can copy it.
_CODE_EXCLUDED_FILES = frozenset(
    {
        ".env",
        "sessions.json",
        "sessions-test.json",
        "active_model.txt",
        "active_permission.txt",
    }
)

# Size cap. Telegram's document limit is 50 MB, so 20 MB leaves headroom; a
# snapshot that big almost always means something bulky slipped through.
_CODE_ZIP_SIZE_LIMIT = 20 * 1024 * 1024


def _is_excluded_file(name: str) -> bool:
    """True for secret / runtime files and the backup rotations, matching
    ``.gitignore`` (``.env*`` is handled by exact name, not a blanket pattern,
    so the safe ``.env.example`` still ships)."""
    if name in _CODE_EXCLUDED_FILES:
        return True
    return ".bak-" in name or name.endswith(".bak")


def _collect_workspace_files(root: str) -> list[tuple[str, str]]:
    """Return ``(absolute_path, arcname)`` pairs for a workspace snapshot.

    ``arcname`` is relative to ``root`` so the archive extracts into the same
    layout. Excluded directories are pruned before walking into them, excluded
    files are skipped, and symlinks are never followed - a link pointing
    outside the workspace must not pull external files into the ZIP.
    """
    entries: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _CODE_EXCLUDED_DIRS
            and not os.path.islink(os.path.join(dirpath, d))
        )
        for fname in sorted(filenames):
            abs_path = os.path.join(dirpath, fname)
            if os.path.islink(abs_path):
                continue
            if _is_excluded_file(fname):
                continue
            entries.append((abs_path, os.path.relpath(abs_path, root)))
    entries.sort(key=lambda e: e[1])
    return entries


def _build_workspace_zip(entries: list[tuple[str, str]]) -> bytes:
    """Build the /code snapshot ZIP in memory from collected entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for abs_path, arcname in entries:
            zf.write(abs_path, arcname=arcname)
    return buf.getvalue()


def _resolve_code_file(root: str, arg: str) -> tuple[str | None, str | None]:
    """Map a ``/code <arg>`` request to an absolute path inside the workspace.

    Returns ``(path, error)`` with exactly one set. The path is confined to
    the workspace via realpath, symlinks are refused, and excluded files stay
    blocked even when named explicitly.
    """
    requested = arg.strip()
    if not requested or requested in (".", ".."):
        return None, "No such file."

    target = os.path.abspath(os.path.join(root, requested))
    real_root = os.path.realpath(root)
    real_target = os.path.realpath(target)
    try:
        within = (
            os.path.normcase(os.path.commonpath([real_target, real_root]))
            == os.path.normcase(real_root)
        )
    except ValueError:
        within = False
    if not within:
        return None, "Outside the active workspace."

    if os.path.islink(target):
        return None, "That path is a symlink."
    if not os.path.isfile(target):
        return None, "Not a regular file in the workspace."
    if _is_excluded_file(os.path.basename(target)):
        return None, "That file is excluded."
    return target, None


def make_code_command(state: AppState):
    async def code_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not authorized(update):
            await update.message.reply_text("⛔ Unauthorized")
            return

        root = state.active_dir
        if not os.path.isdir(root):
            await update.message.reply_text(
                f"📂 Active workspace {root} not found."
            )
            return

        # /code <file> - single-file variant, confined to the active workspace.
        if context.args:
            requested = context.args[0]
            abs_path, error = _resolve_code_file(root, requested)
            if error:
                await update.message.reply_text(f"❌ '{requested}': {error}")
                return
            filename = os.path.basename(abs_path)
            await telegram_io.send_html(update, f"📄 Sending <code>{filename}</code>...")
            try:
                with open(abs_path, "rb") as fh:
                    await update.message.reply_document(
                        document=fh,
                        filename=filename,
                        caption=f"{filename} from {root}.",
                    )
            except OSError as e:
                log.exception("code_command: file read failed")
                await update.message.reply_text(f"❌ Could not read {filename}: {e}")
            return

        # /code - full workspace ZIP built in-memory.
        entries = _collect_workspace_files(root)
        if not entries:
            await update.message.reply_text("📂 No files in the active workspace.")
            return

        try:
            payload = _build_workspace_zip(entries)
        except OSError as e:
            log.exception("code_command: zip build failed")
            await update.message.reply_text(f"❌ Could not build workspace ZIP: {e}")
            return

        if len(payload) > _CODE_ZIP_SIZE_LIMIT:
            await update.message.reply_text(
                f"❌ Workspace ZIP is unexpectedly large ({len(payload)} bytes); "
                "refusing to send."
            )
            return

        # Timestamp so the operator can tell snapshots apart on their phone.
        stamp = time.strftime("%Y%m%d-%H%M%S")
        zip_name = f"{os.path.basename(os.path.normpath(root))}-{stamp}.zip"

        await telegram_io.send_html(
            update,
            f"📦 Sending workspace snapshot as <code>{zip_name}</code>...",
        )
        await update.message.reply_document(
            document=io.BytesIO(payload),
            filename=zip_name,
            caption=f"Snapshot of {root}.",
        )

    return code_command
