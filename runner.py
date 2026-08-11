"""Claude CLI subprocess runner.

Runs ``claude`` in a chosen workspace, streams events back to Telegram, and
saves the session UUID on success. All state changes go through
``state.current_run`` so ``/status`` and ``/cancel`` can read an active
process without taking the lock, which would block them behind the run.
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import signal
import time
import uuid

from telegram import Update

import config
import session_store
import telegram_io
from state import claude_lock, current_run

log = logging.getLogger(__name__)


def _fmt_duration(seconds: int) -> str:
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return (f"{hours}h " if hours else "") + (f"{minutes}m " if minutes else "") + f"{secs}s"


# Some models emit ``<think>...</think>`` blocks (often empty, e.g.
# ``<think></think>``) in their streamed text deltas. Those aren't part of the
# answer, so we strip them from both the live preview and the final message.
# The regexes run over the whole accumulated buffer, not each delta: a single
# delta can split a tag in half, so only the assembled text is safe to match.
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.IGNORECASE | re.DOTALL)
# Also drop a trailing unclosed ``<think>...`` while the model is still
# thinking - otherwise the preview shows a raw ``<think>`` until the closing
# tag arrives. The pass above already removed balanced pairs, so any opener
# left here has no closer, and everything after it (including a partial ``</``)
# can go too.
_TRAILING_OPEN_THINK_RE = re.compile(r"<think\b[^>]*>.*$", re.IGNORECASE | re.DOTALL)
# One step earlier: the stream sometimes shows just ``<t``, ``<th``, ``<thi``,
# ``<thin``, or ``<think`` (no closing ``>`` yet). Strip these trailing
# fragments too. It only matches at the end of the text and needs the ``<t``
# start, so a real ``<`` (or ``<table``) in the response body is left alone.
_TRAILING_PARTIAL_THINK_RE = re.compile(r"<t(?:h(?:i(?:nk?)?)?)?$", re.IGNORECASE)


def _strip_think(text: str) -> str:
    text = _THINK_BLOCK_RE.sub("", text)
    text = _TRAILING_OPEN_THINK_RE.sub("", text)
    text = _TRAILING_PARTIAL_THINK_RE.sub("", text)
    # Collapse the blank lines the removals leave behind.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def session_exists_on_disk(session_id: str) -> bool:
    """Check whether the Claude CLI still has a transcript for this session UUID.

    Claude Code stores per-project session state under ``~/.claude/projects/``
    so we look there first, then fall back to a single non-recursive check at
    the config root for older layouts.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")
    claude_dir = os.path.expanduser(config_dir)

    scoped_root = os.path.join(claude_dir, "projects")
    if os.path.isdir(scoped_root):
        # A two-level glob is much cheaper than "**" over the whole config tree.
        pattern = os.path.join(scoped_root, "*", f"*{session_id}*")
        if glob.glob(pattern):
            return True

    # Fallback for non-standard layouts.
    return bool(glob.glob(os.path.join(claude_dir, f"*{session_id}*")))


def _pick_session(run_dir: str, sessions: dict[str, str]) -> tuple[str, bool]:
    """Return ``(session_id, is_new)`` for the given workspace.

    If we have a recorded UUID for this workspace and its transcript still
    exists on disk, we resume it. Otherwise we generate a fresh UUID for a new
    session.
    """
    recorded_id = sessions.get(run_dir)
    if recorded_id and session_exists_on_disk(recorded_id):
        return recorded_id, False
    return str(uuid.uuid4()), True


async def _kill_process_group(proc: "asyncio.subprocess.Process") -> None:
    """SIGKILL the process group and reap it.

    ``start_new_session=True`` made the child a group leader, so killing the
    group takes any tools it spawned with it. Used on timeout and on an
    unexpected failure, where leaving claude running would orphan it.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as e:
        log.debug("Could not kill process group %s: %s", proc.pid, e)
    try:
        await proc.wait()
    except Exception as e:
        log.debug("Could not reap PID %s: %s", proc.pid, e)


async def _delete_status(status_msg) -> None:
    """Remove the live-preview message. Failure here is never worth reporting."""
    try:
        await status_msg.delete()
    except Exception as e:
        log.debug("Could not delete status message: %s", e)


async def _deliver(update: Update, status_msg, text: str) -> None:
    """Send the final output, then remove the live preview.

    Order matters. The preview holds the tail of the response, so it is the
    only copy the user has until the real message lands. Deleting it first
    means a send that fails to flood control leaves them with nothing. If the
    send fails outright the preview stays on screen as a partial answer.
    """
    if await telegram_io.send_chunks(update, text):
        await _delete_status(status_msg)
    else:
        log.warning("Final message undeliverable; keeping the live preview in place.")


async def run_claude(
    update: Update,
    prompt: str,
    sessions: dict[str, str],
    active_dir: str,
    run_dir: str | None = None,
    active_model: str | None = None,
    permission_mode: str = config.DEFAULT_PERMISSION_MODE,
) -> None:
    """Run the Claude CLI in a workspace with live progress streaming.

    Sends the final answer itself rather than returning it, because the live
    preview must not be deleted until delivery has actually succeeded.

    Args:
        update: Telegram update the run is answering.
        prompt: Text passed to Claude via ``-p``.
        sessions: Mutable ``{workspace: uuid}`` map, updated on successful runs.
        active_dir: The bot's currently selected workspace, used when
            ``run_dir`` is None. Uploads pass an explicit ``run_dir`` so a
            ``/switch`` arriving mid-run can't redirect them.
        run_dir: Optional override for the workspace to run in.
        active_model: If set, passed as ``ANTHROPIC_MODEL`` to the subprocess
            so a ``/model`` choice applies on the next run without a bot
            restart. None leaves whatever the parent env already has.
        permission_mode: The ``--permission-mode`` value for this run.
            Switchable at runtime via ``/perm`` without a bot restart; a run
            always keeps the mode it started with.
    """
    async with claude_lock:
        if run_dir is None:
            run_dir = active_dir

        session_id, is_new_session = _pick_session(run_dir, sessions)
        session_flag = "--session-id" if is_new_session else "--resume"

        status_msg = await update.message.reply_text("🤖 Starting Claude Code task...")

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["FORCE_COLOR"] = "0"
        # Runtime /model override. The Claude CLI reads its model choice from
        # ANTHROPIC_MODEL, so setting it here makes a live switch apply on the
        # next run, with no bot restart.
        if active_model:
            env["ANTHROPIC_MODEL"] = active_model

        proc = await asyncio.create_subprocess_exec(
            config.CLAUDE_BIN,
            "--permission-mode", permission_mode,
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--include-hook-events",
            "--verbose",
            session_flag, session_id,
            "-p", prompt,
            cwd=run_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            # Raise the per-line buffer cap well above asyncio's 64 KiB
            # default. One stream-json line carries a whole tool result, and a
            # web fetch or large file read easily exceeds 64 KiB.
            limit=config.CLAUDE_STREAM_LIMIT,
            start_new_session=True,
        )

        task_start = time.time()
        current_run.set(proc=proc, prompt=prompt, run_dir=run_dir, start_time=task_start)

        streamer = _StreamState(proc=proc, status_msg=status_msg, task_start=task_start)

        try:
            if config.CLAUDE_TIMEOUT > 0:
                await asyncio.wait_for(streamer.run(), timeout=config.CLAUDE_TIMEOUT)
            else:
                await streamer.run()

        except asyncio.TimeoutError:
            await _kill_process_group(proc)
            dur = _fmt_duration(int(time.time() - task_start))
            await _deliver(update, status_msg, f"⏱️ Claude timed out after {dur} and was terminated.")
            return

        except BaseException:
            # Any other failure in the read loop (including cancellation) would
            # otherwise leave claude running: start_new_session=True detaches
            # it, and the finally block below clears current_run, so /cancel
            # can no longer see it. Kill the group before propagating.
            log.exception("Claude run failed; terminating PID %s", proc.pid)
            await _kill_process_group(proc)
            await _delete_status(status_msg)
            raise

        finally:
            current_run.clear()

        dur_txt = _fmt_duration(int(time.time() - task_start))
        out = streamer.text().strip()

        if proc.returncode is not None and proc.returncode < 0:
            await _deliver(update, status_msg, f"🛑 Task (PID {proc.pid}) was cancelled after {dur_txt}.")
            return
        if proc.returncode != 0:
            await _deliver(
                update,
                status_msg,
                f"⚠️ Claude exited with code {proc.returncode} (took {dur_txt}):\n{out}",
            )
            return

        # Only save the session UUID on a clean exit - a crashed run shouldn't
        # leave a ``--resume`` target that doesn't work.
        if is_new_session or run_dir not in sessions:
            sessions[run_dir] = session_id
            session_store.save_sessions(sessions)
        await _deliver(update, status_msg, f"{out}\n\n⏱️ Task completed in {dur_txt}")


class _StreamState:
    """Track the streaming state for one run.

    Reads NDJSON lines from Claude's stdout, appends token text to a buffer,
    and edits a single Telegram message on a fixed interval to show:

    * the current activity label ("Thinking...", "Executing tool: Bash", …),
    * the elapsed time,
    * a preview of the response text as it streams in.

    The preview is the *tail* of the accumulated text, capped so the edit stays
    under Telegram's 4096-character message limit. The full output is sent
    after the run finishes via :func:`telegram_io.send_chunks`.
    """

    _PUNCT = ".!?:;"
    # Keep the preview well below Telegram's 4096-char per-message ceiling so
    # header + <pre> tags + escaped chars all fit even after HTML expansion.
    _PREVIEW_TAIL_CHARS = 3200
    # How often we re-render the live message. Telegram counts these edits
    # against the same per-chat quota as the final reply, so editing too
    # eagerly can leave nothing left to deliver the result with. See
    # config.LIVE_EDIT_INTERVAL.
    _TICK_SECONDS = config.LIVE_EDIT_INTERVAL

    def __init__(
        self,
        proc: "asyncio.subprocess.Process",
        status_msg,
        task_start: float,
    ) -> None:
        self.proc = proc
        self.status_msg = status_msg
        self.task_start = task_start
        self.accumulated: list[str] = []
        self.current_activity = "Thinking..."
        self._last_rendered: str | None = None
        # How many times readline() overflowed. Only the first one is
        # reported; see _read_loop.
        self._overflow_count = 0

    def text(self) -> str:
        return _strip_think("".join(self.accumulated))

    def _append(self, text: str) -> None:
        """Append text ensuring spacing between discrete JSON event payloads."""
        if not text:
            return
        if self.accumulated:
            last_chunk = self.accumulated[-1]
            if last_chunk and last_chunk[-1] in self._PUNCT and not text[0].isspace():
                self.accumulated.append(" ")
        self.accumulated.append(text)

    def _render(self) -> str:
        """Build the current live-message body (header + optional preview)."""
        elapsed_fmt = _fmt_duration(int(time.time() - self.task_start))
        header = (
            f"🤖 <b>{telegram_io.escape_html(self.current_activity)}</b> "
            f"({elapsed_fmt})"
        )

        text = self.text()
        if not text:
            return header

        # Show the tail so the user sees the newest tokens as they arrive.
        # Prepend an ellipsis when the start has been cut off.
        if len(text) > self._PREVIEW_TAIL_CHARS:
            preview_source = "…" + text[-self._PREVIEW_TAIL_CHARS:]
        else:
            preview_source = text
        preview = telegram_io.escape_html(preview_source)
        return f"{header}\n\n<pre>{preview}</pre>"

    async def _timer_loop(self) -> None:
        """Re-render the live message on a fixed cadence while the run is active."""
        while self.proc.returncode is None:
            await asyncio.sleep(self._TICK_SECONDS)
            body = self._render()
            # Skip if nothing changed - Telegram rejects identical edits with
            # "message is not modified" and we'd log noise. The elapsed time in
            # the header changes every tick, so this rarely fires.
            if body == self._last_rendered:
                continue
            try:
                await self.status_msg.edit_text(body, parse_mode="HTML")
                self._last_rendered = body
            except Exception as e:
                # Common benign causes: message too old (48h), rate limit,
                # no change since last edit. Debug-log and keep streaming.
                log.debug("Live status edit failed (usually benign): %s", e)

    async def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        while True:
            try:
                line_bytes = await self.proc.stdout.readline()
            except ValueError as e:
                # A single line was longer than CLAUDE_STREAM_LIMIT. readline
                # clears only what it had buffered, so one very long line
                # raises repeatedly as the rest of it arrives. Collapse that
                # into a single warning and a single marker, and keep reading -
                # losing one event beats losing the whole run.
                self._overflow_count += 1
                if self._overflow_count == 1:
                    log.warning(
                        "Dropped an oversized stream-json line (over %d bytes): %s. "
                        "Raise CLAUDE_STREAM_LIMIT if this repeats.",
                        config.CLAUDE_STREAM_LIMIT,
                        e,
                    )
                    self._append("\n[stream] dropped an oversized event\n")
                continue

            if not line_bytes:
                break

            line_str = line_bytes.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            self._handle_line(line_str)

    def _handle_line(self, line_str: str) -> None:
        try:
            event = json.loads(line_str)
        except json.JSONDecodeError:
            # Prefix it so output we couldn't decode stays distinguishable
            # from real Claude text instead of running into it.
            self.accumulated.append(f"[raw] {line_str}\n")
            return

        if event.get("type") == "stream_event":
            event = event.get("event", event)

        event_type = event.get("type")

        # Capture streaming text tokens as continuous chunks.
        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                self._append(delta.get("text", ""))

        # Fall back to result/text events if no streaming deltas were received.
        elif event_type == "result" and "result" in event:
            if not self.accumulated:
                self._append(str(event["result"]))
        elif event_type == "text" and "text" in event:
            if not self.accumulated:
                self._append(event["text"])

        # Update status label based on what Claude is doing.
        if event_type in ("tool_use", "tool_call", "hook_event"):
            tool_name = event.get("name") or event.get("tool") or "tool"
            self.current_activity = f"Executing tool: {tool_name}"
        elif event_type in ("status", "thinking", "progress"):
            msg = event.get("status") or event.get("message") or "Thinking..."
            self.current_activity = msg

    async def run(self) -> None:
        timer_task = asyncio.create_task(self._timer_loop())
        try:
            await self._read_loop()
            await self.proc.wait()
        finally:
            timer_task.cancel()
