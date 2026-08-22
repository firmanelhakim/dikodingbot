"""Tests for the /code command - workspace snapshot collection, secret and
backup exclusion, symlink refusal, and single-file traversal confinement."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


def _fresh_handlers(base_dir: str, **env):
    base = {
        "BOT_TOKEN": "t",
        "ALLOWED_USER_ID": "1",
        "BASE_DIR": base_dir,
    }
    base.update(env)
    return fresh_import("handlers", base)


class CollectWorkspaceFilesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handlers = _fresh_handlers(self.tmp.name)
        self.root = self.tmp.name

    def _make(self, relpath: str) -> str:
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        return path

    def test_collects_regular_files_recursively(self):
        self._make("a.txt")
        self._make("sub/b.py")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = sorted(arc for _abs, arc in entries)
        self.assertEqual(rels, ["a.txt", "sub/b.py"])

    def test_excludes_secrets_and_runtime_state(self):
        for name in (
            ".env",
            "sessions.json",
            "sessions-test.json",
            "active_model.txt",
            "active_permission.txt",
        ):
            self._make(name)
        self._make("README.md")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, ["README.md"])

    def test_excludes_backup_rotations(self):
        self._make("bot.py.bak-20260101-120000")
        self._make("bot.py.bak")
        self._make("bot.py")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, ["bot.py"])

    def test_keeps_env_example(self):
        self._make(".env")
        self._make(".env.example")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, [".env.example"])

    def test_prunes_excluded_dirs(self):
        self._make("venv/site.py")
        self._make("node_modules/x/index.js")
        self._make(".git/config")
        self._make("src/main.py")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, ["src/main.py"])

    def test_does_not_follow_symlinked_files(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.write(b"secret")
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        os.symlink(outside.name, os.path.join(self.root, "link.txt"))
        self._make("real.txt")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, ["real.txt"])

    def test_does_not_follow_symlinked_dirs(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, ignore_errors=True)
        with open(os.path.join(outside, "escaped.txt"), "w") as f:
            f.write("x")
        os.symlink(outside, os.path.join(self.root, "escape"))
        self._make("real.txt")
        entries = self.handlers._collect_workspace_files(self.root)
        rels = [arc for _abs, arc in entries]
        self.assertEqual(rels, ["real.txt"])


class ResolveCodeFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handlers = _fresh_handlers(self.tmp.name)
        self.root = self.tmp.name

    def _make(self, relpath: str) -> str:
        path = os.path.join(self.root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("x")
        return path

    def test_resolves_file_inside(self):
        p = self._make("runner.py")
        resolved, error = self.handlers._resolve_code_file(self.root, "runner.py")
        self.assertEqual(resolved, p)
        self.assertIsNone(error)

    def test_parent_traversal_rejected(self):
        resolved, error = self.handlers._resolve_code_file(self.root, "../secret.txt")
        self.assertIsNone(resolved)
        self.assertIn("Outside", error)

    def test_absolute_path_outside_rejected(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        resolved, error = self.handlers._resolve_code_file(self.root, outside.name)
        self.assertIsNone(resolved)
        self.assertIn("Outside", error)

    def test_symlink_outside_rejected(self):
        outside = tempfile.NamedTemporaryFile(delete=False)
        outside.write(b"x")
        outside.close()
        self.addCleanup(os.unlink, outside.name)
        os.symlink(outside.name, os.path.join(self.root, "link.txt"))
        resolved, error = self.handlers._resolve_code_file(self.root, "link.txt")
        self.assertIsNone(resolved)
        # The realpath confinement check fires before the symlink check, so a
        # link pointing outside is rejected as "Outside" - either message is a
        # refusal, which is all that matters.
        self.assertIn("Outside", error)

    def test_symlink_inside_rejected(self):
        target = os.path.join(self.root, "real.txt")
        with open(target, "w") as f:
            f.write("x")
        os.symlink(target, os.path.join(self.root, "link.txt"))
        resolved, error = self.handlers._resolve_code_file(self.root, "link.txt")
        self.assertIsNone(resolved)
        self.assertIn("symlink", error)

    def test_missing_file_rejected(self):
        resolved, error = self.handlers._resolve_code_file(self.root, "nope.txt")
        self.assertIsNone(resolved)
        self.assertIn("Not a regular file", error)

    def test_excluded_file_rejected_even_when_named(self):
        self._make(".env")
        resolved, error = self.handlers._resolve_code_file(self.root, ".env")
        self.assertIsNone(resolved)
        self.assertIn("excluded", error)

    def test_backup_rejected_even_when_named(self):
        self._make("bot.py.bak-20260101-120000")
        resolved, error = self.handlers._resolve_code_file(
            self.root, "bot.py.bak-20260101-120000"
        )
        self.assertIsNone(resolved)
        self.assertIn("excluded", error)

    def test_env_example_is_allowed(self):
        p = self._make(".env.example")
        resolved, error = self.handlers._resolve_code_file(self.root, ".env.example")
        self.assertEqual(resolved, p)
        self.assertIsNone(error)


if __name__ == "__main__":
    unittest.main()
