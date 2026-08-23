"""Tests for topic routing - load/save, confinement, slugging, and the
resolve_dir fallback between a forum topic and the private-chat/General path."""

from __future__ import annotations

import os
import tempfile
import unittest

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


def _fresh_topics(**env):
    base = {
        "BOT_TOKEN": "t",
        "ALLOWED_USER_ID": "1",
    }
    base.update(env)
    return fresh_import("topics", base)


class ConfinedToTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.topics = _fresh_topics()

    def test_direct_subdir_is_confined(self):
        target = os.path.join(self.tmp.name, "project-a")
        self.assertTrue(self.topics.confined_to(self.tmp.name, target))

    def test_parent_is_rejected(self):
        target = os.path.abspath(os.path.join(self.tmp.name, ".."))
        self.assertFalse(self.topics.confined_to(self.tmp.name, target))

    def test_symlink_escaping_is_rejected(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, outside)
        link = os.path.join(self.tmp.name, "escape")
        os.symlink(outside, link, target_is_directory=True)
        self.assertFalse(self.topics.confined_to(self.tmp.name, link))


class SlugifyTests(unittest.TestCase):
    def setUp(self):
        self.topics = _fresh_topics()

    def test_lowercases_and_dashes_spaces(self):
        self.assertEqual(self.topics.slugify("Project One"), "project-one")

    def test_strips_punctuation(self):
        self.assertEqual(self.topics.slugify("Auth / Stripe!"), "auth-stripe")

    def test_all_punctuation_falls_back(self):
        self.assertEqual(self.topics.slugify("!!!"), "topic")


class LoadSaveTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "topics.json")
            topics = _fresh_topics(TOPICS_FILE=path)
            mapping = {"123": "/base/project-a"}
            topics.save_topics(mapping)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(topics.load_topics(), mapping)
            # Atomic write leaves no .tmp sibling behind.
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            topics = _fresh_topics(TOPICS_FILE=os.path.join(tmp, "nope.json"))
            self.assertEqual(topics.load_topics(), {})

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "topics.json")
            with open(path, "w") as f:
                f.write("{not json")
            topics = _fresh_topics(TOPICS_FILE=path)
            self.assertEqual(topics.load_topics(), {})


class PermissionsPersistenceTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "permissions.json")
            topics = _fresh_topics(PERMISSIONS_FILE=path)
            mapping = {"/base/a": "bypassPermissions"}
            topics.save_permissions(mapping)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(topics.load_permissions(), mapping)
            self.assertFalse(os.path.exists(path + ".tmp"))

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            topics = _fresh_topics(PERMISSIONS_FILE=os.path.join(tmp, "nope.json"))
            self.assertEqual(topics.load_permissions(), {})

    def test_corrupt_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "permissions.json")
            with open(path, "w") as f:
                f.write("{not json")
            topics = _fresh_topics(PERMISSIONS_FILE=path)
            self.assertEqual(topics.load_permissions(), {})


class ResolveDirTests(unittest.TestCase):
    def _update(self, thread_id):
        msg = type("_Msg", (), {"message_thread_id": thread_id})()
        return type("_Upd", (), {"message": msg})()

    def _state(self, active_dir, topics):
        return type("_State", (), {"active_dir": active_dir, "topics": topics})()

    def setUp(self):
        self.topics = _fresh_topics()

    def test_bound_topic_resolves_to_folder(self):
        update = self._update(123)
        state = self._state("/base", {"123": "/base/project-a"})
        folder, error = self.topics.resolve_dir(update, state)
        self.assertEqual(folder, "/base/project-a")
        self.assertIsNone(error)

    def test_unbound_topic_is_an_error(self):
        update = self._update(456)
        state = self._state("/base", {})
        folder, error = self.topics.resolve_dir(update, state)
        self.assertIsNone(folder)
        self.assertIn("not bound", error)

    def test_no_thread_id_falls_back_to_active_dir(self):
        update = self._update(None)
        state = self._state("/base", {})
        folder, error = self.topics.resolve_dir(update, state)
        self.assertEqual(folder, "/base")
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
