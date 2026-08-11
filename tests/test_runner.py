"""Runner tests - the stream-json event parser.

``_StreamState`` reads NDJSON events from Claude's stdout and assembles the
final text. These tests feed it one JSON event at a time and check the
accumulated result.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


def _fresh_runner():
    return fresh_import("runner", {"BOT_TOKEN": "t", "ALLOWED_USER_ID": "1"})


class _FakeStatusMsg:
    async def edit_text(self, *_a, **_k):
        return None

    async def delete(self):
        return None


class StreamParserTests(unittest.TestCase):
    def setUp(self):
        runner = _fresh_runner()
        # ``proc`` is unused by _handle_line/_append, so a plain object is fine.
        self.state = runner._StreamState(
            proc=object(),
            status_msg=_FakeStatusMsg(),
            task_start=0.0,
        )

    def test_content_block_delta_appends_text(self):
        self.state._handle_line(json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Hello, "},
        }))
        self.state._handle_line(json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "world!"},
        }))
        self.assertEqual(self.state.text(), "Hello, world!")

    def test_stream_event_wrapper_is_unwrapped(self):
        self.state._handle_line(json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "wrapped"},
            },
        }))
        self.assertEqual(self.state.text(), "wrapped")

    def test_result_fallback_only_when_no_deltas(self):
        # No deltas yet → result event fills in.
        self.state._handle_line(json.dumps({"type": "result", "result": "fallback"}))
        self.assertEqual(self.state.text(), "fallback")

        # Once there's text, another result event must not append again.
        self.state._handle_line(json.dumps({"type": "result", "result": "again"}))
        self.assertEqual(self.state.text(), "fallback")

    def test_tool_use_updates_activity_label(self):
        self.state._handle_line(json.dumps({"type": "tool_use", "name": "Bash"}))
        self.assertEqual(self.state.current_activity, "Executing tool: Bash")

    def test_invalid_json_is_prefixed_not_silently_appended(self):
        # Output we couldn't decode stays distinguishable from real Claude text.
        self.state._handle_line("this is not JSON")
        self.assertIn("[raw]", self.state.text())

    def test_punctuation_spacing_between_chunks(self):
        # If a chunk ends with sentence punctuation and the next chunk starts
        # with a non-space character, a space is inserted so words don't run
        # together.
        self.state._handle_line(json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "First."},
        }))
        self.state._handle_line(json.dumps({
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "Second"},
        }))
        self.assertEqual(self.state.text(), "First. Second")


class LivePreviewRenderTests(unittest.TestCase):
    """The live-message body is what makes streaming visible in Telegram."""

    def setUp(self):
        runner = _fresh_runner()
        self.state = runner._StreamState(
            proc=object(),
            status_msg=_FakeStatusMsg(),
            task_start=0.0,
        )

    def test_render_header_only_when_no_text_yet(self):
        # Nothing streamed → just the activity + elapsed line, no <pre> block.
        body = self.state._render()
        self.assertIn("<b>Thinking...</b>", body)
        self.assertNotIn("<pre>", body)

    def test_render_includes_pre_block_once_text_arrives(self):
        self.state._append("Hello from Claude.")
        body = self.state._render()
        self.assertIn("<pre>", body)
        self.assertIn("Hello from Claude.", body)

    def test_render_escapes_html_special_chars_in_preview(self):
        self.state._append("if x < 3 && y > 0: pass")
        body = self.state._render()
        self.assertIn("&lt;", body)
        self.assertIn("&gt;", body)
        self.assertIn("&amp;", body)
        # Raw special characters must not appear in the preview text - if they
        # did, Telegram would reject the HTML message.
        self.assertNotIn("< 3", body)

    def test_render_truncates_head_when_text_exceeds_tail_cap(self):
        # Build text longer than the tail cap and check that only the tail
        # shows, prefixed with an ellipsis so the user knows text was dropped.
        cap = self.state._PREVIEW_TAIL_CHARS
        long_text = ("A" * cap) + "TAIL_MARKER"
        self.state._append(long_text)
        body = self.state._render()
        self.assertIn("TAIL_MARKER", body)
        self.assertIn("…", body)
        # And the whole rendered body must fit within Telegram's 4096-char
        # per-message ceiling.
        self.assertLess(len(body), 4096)

    def test_render_escapes_activity_label(self):
        self.state.current_activity = "Executing tool: <weird&name>"
        body = self.state._render()
        self.assertIn("&lt;weird&amp;name&gt;", body)
        self.assertNotIn("<weird", body)


class OversizedLineTests(unittest.TestCase):
    """A single stream-json line larger than the buffer must not kill the run.

    asyncio's StreamReader raises ValueError ("Separator is not found, and
    chunk exceed the limit") when one line exceeds its buffer. One big tool
    result used to end the whole run; now the event is dropped and reading
    continues.
    """

    def setUp(self):
        self.runner = _fresh_runner()

    def _make_state(self, reader):
        proc = type("_P", (), {"stdout": reader, "returncode": None})()
        return self.runner._StreamState(
            proc=proc,
            status_msg=_FakeStatusMsg(),
            task_start=0.0,
        )

    def test_read_loop_survives_oversized_line(self):
        # Reader yields: a good line, an overflow, another good line, then EOF.
        events = [
            json.dumps({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "before"},
            }).encode() + b"\n",
            ValueError("Separator is not found, and chunk exceed the limit"),
            json.dumps({
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "after"},
            }).encode() + b"\n",
            b"",
        ]

        class _Reader:
            def __init__(self):
                self.i = 0

            async def readline(self):
                item = events[self.i]
                self.i += 1
                if isinstance(item, Exception):
                    raise item
                return item

        state = self._make_state(_Reader())
        asyncio.run(state._read_loop())

        text = state.text()
        # Text on both sides of the overflow survives.
        self.assertIn("before", text)
        self.assertIn("after", text)
        # And the gap is visible rather than silent.
        self.assertIn("dropped an oversized event", text)

    def test_repeated_overflow_reports_once(self):
        # One very long line makes readline raise several times in a row. The
        # user should see a single marker, not one per raise.
        overflow = ValueError("Separator is not found, and chunk exceed the limit")
        events = [overflow, overflow, overflow, b""]

        class _Reader:
            def __init__(self):
                self.i = 0

            async def readline(self):
                item = events[self.i]
                self.i += 1
                if isinstance(item, Exception):
                    raise item
                return item

        state = self._make_state(_Reader())
        asyncio.run(state._read_loop())

        self.assertEqual(state.text().count("dropped an oversized event"), 1)
        self.assertEqual(state._overflow_count, 3)

    def test_stream_limit_default_is_above_asyncio_default(self):
        # asyncio's own cap is 64 KiB; ours must be well above it or the fix
        # does nothing.
        self.assertGreater(self.runner.config.CLAUDE_STREAM_LIMIT, 65536)


class DeliveryOrderTests(unittest.TestCase):
    """The live preview is the only copy of the answer until the send lands.

    Deleting it first means a send that hits flood control leaves the user
    with nothing, which is how long responses used to disappear.
    """

    def setUp(self):
        self.runner = _fresh_runner()

    def _run_deliver(self, send_result):
        events = []

        class _Status:
            async def delete(self):
                events.append("delete")

        async def _fake_send(_update, text):
            events.append(("send", text))
            return send_result

        self.runner.telegram_io.send_chunks = _fake_send
        asyncio.run(self.runner._deliver(object(), _Status(), "the answer"))
        return events

    def test_preview_is_deleted_only_after_a_successful_send(self):
        events = self._run_deliver(send_result=True)
        self.assertEqual(events, [("send", "the answer"), "delete"])

    def test_preview_survives_a_failed_send(self):
        events = self._run_deliver(send_result=False)
        self.assertEqual(events, [("send", "the answer")])

    def test_delete_failure_is_swallowed(self):
        # A preview that can't be removed is cosmetic; it must not turn a
        # delivered answer into an error reply.
        class _Status:
            async def delete(self):
                raise RuntimeError("message too old to delete")

        async def _fake_send(_update, _text):
            return True

        self.runner.telegram_io.send_chunks = _fake_send
        asyncio.run(self.runner._deliver(object(), _Status(), "answer"))


if __name__ == "__main__":
    unittest.main()
