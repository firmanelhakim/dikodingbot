"""Delivery tests - flood control, retries, and the file fallback.

Telegram counts live-preview edits against the same per-chat quota as the
final reply, so a long run can arrive at the end with no budget left. These
tests cover what happens then: retry while the API says to wait, fall back to
a file upload when it won't relent, and report whether the user actually got
the text.
"""

from __future__ import annotations

import asyncio
import datetime as dtm
import unittest
from unittest.mock import patch

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()

from telegram.error import RetryAfter, TelegramError  # noqa: E402  (needs the stubs)


def _fresh_io(env=None):
    base = {"BOT_TOKEN": "t", "ALLOWED_USER_ID": "1"}
    base.update(env or {})
    return fresh_import("telegram_io", base)


class _FakeMessage:
    """Records sends and raises whatever the test queued up.

    ``send_effects`` is consumed one entry per reply_text call: an exception
    instance is raised, anything else counts as a successful send.
    """

    def __init__(self, send_effects=None, document_effect=None):
        self.send_effects = list(send_effects or [])
        self.document_effect = document_effect
        self.sent: list[tuple[str, str | None]] = []
        self.documents: list[bytes] = []

    async def reply_text(self, text, parse_mode=None):
        effect = self.send_effects.pop(0) if self.send_effects else None
        if isinstance(effect, Exception):
            raise effect
        self.sent.append((text, parse_mode))

    async def reply_document(self, document, filename=None, caption=None):
        if isinstance(self.document_effect, Exception):
            raise self.document_effect
        self.documents.append(document.getvalue())


class _FakeUpdate:
    def __init__(self, message):
        self.message = message


def _no_sleep(test_case, io_mod):
    """Patch out the retry waits so tests don't actually sleep.

    ``io_mod.asyncio`` is the real asyncio module, so this has to be a scoped
    patch that gets undone - assigning to it directly would leave every later
    test with a broken ``asyncio.sleep``.
    """
    real_sleep = asyncio.sleep
    patcher = patch.object(io_mod.asyncio, "sleep", lambda _s: real_sleep(0))
    patcher.start()
    test_case.addCleanup(patcher.stop)


class RetrySecondsTests(unittest.TestCase):
    """``retry_after`` is an int on older PTB and a timedelta from v22.2."""

    def setUp(self):
        self.io = _fresh_io()

    def test_int_retry_after(self):
        self.assertEqual(self.io._retry_seconds(RetryAfter(15)), 15.0)

    def test_timedelta_retry_after(self):
        err = RetryAfter(dtm.timedelta(seconds=12.5))
        self.assertEqual(self.io._retry_seconds(err), 12.5)


class SendChunksTests(unittest.TestCase):
    def setUp(self):
        self.io = _fresh_io()
        _no_sleep(self, self.io)

    def test_successful_send_reports_delivered(self):
        msg = _FakeMessage()
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "hello"))
        self.assertTrue(ok)
        self.assertEqual(len(msg.sent), 1)
        self.assertIn("<pre>hello</pre>", msg.sent[0][0])

    def test_empty_text_still_sends_a_placeholder(self):
        msg = _FakeMessage()
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), ""))
        self.assertTrue(ok)
        self.assertIn("(no output)", msg.sent[0][0])

    def test_flood_control_is_retried_then_succeeds(self):
        # First attempt is rejected; the second goes through.
        msg = _FakeMessage(send_effects=[RetryAfter(1)])
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "delayed"))
        self.assertTrue(ok)
        self.assertEqual(len(msg.sent), 1)
        self.assertEqual(msg.documents, [])

    def test_persistent_flood_control_falls_back_to_a_file(self):
        # Every message attempt is refused, so the answer goes out as a upload
        # instead of being lost.
        msg = _FakeMessage(send_effects=[RetryAfter(1)] * 10)
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "important output"))
        self.assertTrue(ok)
        self.assertEqual(msg.sent, [])
        self.assertEqual(msg.documents, [b"important output"])

    def test_retry_gives_up_when_the_wait_is_too_long(self):
        # A wait beyond SEND_MAX_RETRY_WAIT is not worth sitting through.
        msg = _FakeMessage(send_effects=[RetryAfter(600)] * 10)
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "output"))
        self.assertTrue(ok)
        self.assertEqual(msg.documents, [b"output"])
        # Only one attempt: the first wait already exceeded the cap.
        self.assertEqual(len(msg.send_effects), 9)

    def test_file_fallback_carries_the_whole_text_not_just_the_failed_chunk(self):
        long_text = "A" * (self.io.config.MAX_CHUNK * 2 + 50)
        # First chunk sends, the rest are refused.
        msg = _FakeMessage(send_effects=[None] + [RetryAfter(1)] * 10)
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), long_text))
        self.assertTrue(ok)
        self.assertEqual(msg.documents, [long_text.encode()])

    def test_total_failure_reports_undelivered(self):
        # Messages refused and the upload fails too - the caller must learn
        # about it so it can keep the live preview on screen.
        msg = _FakeMessage(
            send_effects=[RetryAfter(1)] * 10,
            document_effect=TelegramError("upload broken"),
        )
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "output"))
        self.assertFalse(ok)

    def test_html_parse_failure_falls_back_to_plain_text(self):
        # A formatting rejection is not a rate limit; resending unformatted is
        # the right response.
        msg = _FakeMessage(send_effects=[TelegramError("can't parse entities")])
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), "5 > 3"))
        self.assertTrue(ok)
        self.assertEqual(msg.sent[0], ("5 > 3", None))

    def test_long_text_is_split_into_chunks(self):
        long_text = "B" * (self.io.config.MAX_CHUNK * 2 + 10)
        msg = _FakeMessage()
        ok = asyncio.run(self.io.send_chunks(_FakeUpdate(msg), long_text))
        self.assertTrue(ok)
        self.assertEqual(len(msg.sent), 3)


class RateLimitConfigTests(unittest.TestCase):
    def test_live_edit_interval_stays_under_the_per_chat_quota(self):
        # Telegram allows roughly 20 messages a minute per chat. The preview
        # must leave room for the final answer, so it may not use all of them.
        io_mod = _fresh_io()
        edits_per_minute = 60.0 / io_mod.config.LIVE_EDIT_INTERVAL
        self.assertLess(edits_per_minute, 20)

    def test_overrides_are_read_from_the_environment(self):
        io_mod = _fresh_io({"LIVE_EDIT_INTERVAL": "6.5", "SEND_MAX_RETRIES": "5"})
        self.assertEqual(io_mod.config.LIVE_EDIT_INTERVAL, 6.5)
        self.assertEqual(io_mod.config.SEND_MAX_RETRIES, 5)


if __name__ == "__main__":
    unittest.main()
