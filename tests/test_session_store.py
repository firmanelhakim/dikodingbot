"""Session store tests - atomic persistence + round-trip."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


def _fresh_store(session_file: str):
    return fresh_import("session_store", {
        "BOT_TOKEN": "t",
        "ALLOWED_USER_ID": "1",
        "SESSION_FILE": session_file,
    })


class SessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sessions.json")

    def test_load_missing_file_returns_empty(self):
        store = _fresh_store(self.path)
        self.assertEqual(store.load_sessions(), {})

    def test_save_then_load_roundtrip(self):
        store = _fresh_store(self.path)
        data = {"/foo/bar": "uuid-1", "/qux/baz": "uuid-2"}
        store.save_sessions(data)
        self.assertEqual(store.load_sessions(), data)

    def test_load_ignores_non_dict_content(self):
        with open(self.path, "w") as f:
            f.write('["not", "a", "dict"]')
        store = _fresh_store(self.path)
        self.assertEqual(store.load_sessions(), {})

    def test_load_tolerates_corrupt_json(self):
        with open(self.path, "w") as f:
            f.write("{not valid json")
        store = _fresh_store(self.path)
        # Should not raise - should return empty.
        self.assertEqual(store.load_sessions(), {})

    def test_save_is_atomic_via_tmp_file(self):
        store = _fresh_store(self.path)
        store.save_sessions({"a": "b"})
        # No stray tmp file left behind after a successful save.
        self.assertFalse(os.path.exists(self.path + ".tmp"))
        with open(self.path) as f:
            self.assertEqual(json.load(f), {"a": "b"})


if __name__ == "__main__":
    unittest.main()
