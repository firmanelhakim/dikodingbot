"""Telegram output helpers.

The bot uses HTML mode everywhere. Markdown is a poor fit because Claude
output often contains triple-backticks that would close a Markdown code fence
early. Keeping the escaping and formatting in one place means handlers don't
repeat it.

Delivery is rate-limit aware. Telegram counts message edits and new messages
against the same per-chat quota, so a long run that streams a live preview can
arrive at the end with no budget left. When that happens the API answers 429
with a ``retry_after`` value, and the only correct response is to wait that
long and try again. If the text still won't go through, we send it as a file
instead: one upload beats a dozen rejected messages.
"""

from __future__ import annotations

import asyncio
import datetime as dtm
import io
import logging

from telegram import Update
from telegram.error import RetryAfter, TelegramError

import config

log = logging.getLogger(__name__)


def escape_html(text: str) -> str:
    """Escape the three characters Telegram's HTML parse mode requires."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _now() -> float:
    """Monotonic-ish wall clock in seconds. Split out so tests can control it."""
    return dtm.datetime.now().timestamp()


class _TokenBucket:
    """A per-chat token bucket shared by every send and edit.

    One token is consumed per outbound API call (a message or a live-preview
    edit; the file-upload fallback is rare and left outside the budget). The
    bucket refills at a fixed rate up to a burst ceiling, so a single run that
    sends slowly never waits, while two runs in the same chat are throttled
    toward a shared sustainable rate rather than tripping 429.
    """

    def __init__(self, rate: float, burst: float) -> None:
        self._rate = rate / 60.0  # tokens per second
        self._burst = burst
        self._tokens = burst
        self._updated = _now()
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        self._tokens = min(self._burst, self._tokens + (now - self._updated) * self._rate)
        self._updated = now

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        async with self._lock:
            while True:
                now = _now()
                self._refill(now)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                # Sleep until the next token would arrive, then re-check.
                await asyncio.sleep((1.0 - self._tokens) / self._rate)


# One bucket per chat_id. Lazy so tests and single-chat runs create exactly one.
_buckets: dict[int, _TokenBucket] = {}


def get_bucket(chat_id: int) -> _TokenBucket:
    """Return the pacing bucket for ``chat_id``, creating it on first use."""
    bucket = _buckets.get(chat_id)
    if bucket is None:
        bucket = _buckets[chat_id] = _TokenBucket(
            config.SEND_RATE_LIMIT, config.SEND_RATE_BURST
        )
    return bucket


def _chat_id_of(update: Update) -> int | None:
    """Best-effort chat id for an update, or None when it cannot be found.

    ``update.message.chat_id`` is the normal path. Some update shapes
    (callback queries, edits) lack a plain message; pacing degrades to a
    per-update bucket rather than failing the send.
    """
    msg = getattr(update, "effective_message", None)
    if msg is not None:
        return getattr(msg, "chat_id", None)
    return None


async def _pace(update: Update) -> None:
    """Acquire a token for this update's chat before an outbound call."""
    chat_id = _chat_id_of(update)
    if chat_id is None:
        return
    await get_bucket(chat_id).acquire()


def _retry_seconds(err: RetryAfter) -> float:
    """Seconds to wait for a RetryAfter error.

    ``retry_after`` is an int on older python-telegram-bot and a timedelta from
    v22.2 onward, so accept both.
    """
    value = err.retry_after
    if isinstance(value, dtm.timedelta):
        return value.total_seconds()
    return float(value)


async def _send_with_retry(update: Update, text: str, parse_mode: str | None) -> bool:
    """Send one message, waiting out any flood-control rejections.

    Returns True if the message was delivered. Retries only on 429: the API
    tells us how long to wait, so waiting is the fix. Other errors are the
    caller's problem and are re-raised.

    Takes a token from the chat's shared bucket first, so parallel topics in
    one group do not collectively overrun the per-chat quota.
    """
    await _pace(update)
    for attempt in range(config.SEND_MAX_RETRIES + 1):
        try:
            await update.message.reply_text(text, parse_mode=parse_mode)
            return True
        except RetryAfter as e:
            wait = _retry_seconds(e)
            if attempt >= config.SEND_MAX_RETRIES or wait > config.SEND_MAX_RETRY_WAIT:
                log.warning(
                    "Giving up after %d flood-control retries (last wait %.1fs)",
                    attempt,
                    wait,
                )
                return False
            # Small cushion so we come back just after the window opens.
            wait += 0.5
            log.info(
                "Flood control: waiting %.1fs before retry %d/%d",
                wait,
                attempt + 1,
                config.SEND_MAX_RETRIES,
            )
            await asyncio.sleep(wait)
    return False


async def _send_as_file(update: Update, text: str, reason: str) -> bool:
    """Last resort: deliver the text as a .txt upload.

    A single document upload is one API call instead of one per chunk, so it
    usually succeeds when message sends are being throttled.
    """
    try:
        payload = io.BytesIO(text.encode("utf-8"))
        await update.message.reply_document(
            document=payload,
            filename="response.txt",
            caption=f"⚠️ Sent as a file: {reason}",
        )
        log.info("Delivered %d chars as a file (%s)", len(text), reason)
        return True
    except (TelegramError, OSError) as e:
        log.error("File fallback failed: %s", e)
        return False


async def send_chunks(update: Update, text: str) -> bool:
    """Reply with text split into safe chunks, each wrapped in <pre>.

    Chunks that hit flood control are retried. If any chunk is still
    undeliverable, the whole response goes out as a file so the output isn't
    lost. Returns True if the user received the text one way or the other,
    which callers use to decide whether the live preview can be removed.
    """
    if not text:
        text = "(no output)"

    # Slice the raw text first, then escape each chunk. Escaping before
    # slicing could split an entity like ``&amp;`` across a chunk boundary and
    # produce broken HTML.
    chunks = [
        text[i:i + config.MAX_CHUNK]
        for i in range(0, len(text), config.MAX_CHUNK)
    ]

    for index, chunk in enumerate(chunks):
        if index and config.SEND_CHUNK_DELAY > 0:
            # Space out our own burst so a multi-chunk reply doesn't trip the
            # rate limit by itself.
            await asyncio.sleep(config.SEND_CHUNK_DELAY)

        escaped = escape_html(chunk)
        try:
            delivered = await _send_with_retry(
                update, f"<pre>{escaped}</pre>", "HTML"
            )
        except TelegramError as e:
            # A parse/formatting problem, not a rate limit. Plain text is the
            # right fallback here.
            log.warning("HTML reply failed for chunk %d, trying plain: %s", index, e)
            try:
                delivered = await _send_with_retry(update, chunk, None)
            except TelegramError as e2:
                log.warning("Plain-text reply also failed for chunk %d: %s", index, e2)
                delivered = False

        if not delivered:
            # Stop sending chunks: the remaining ones would fail the same way,
            # and a half-delivered answer is worse than one complete file.
            log.warning(
                "Chunk %d/%d undeliverable; falling back to a file upload.",
                index + 1,
                len(chunks),
            )
            if await _send_as_file(update, text, "Telegram rate limit"):
                return True
            log.error("Could not deliver the response at all (%d chars).", len(text))
            return False

    return True


async def send_html(update: Update, html: str) -> bool:
    """Send a single HTML message (not wrapped in <pre>). Use for menus."""
    return await _send_with_retry(update, html, "HTML")
