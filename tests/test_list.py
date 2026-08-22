"""Tests for the /list command - directory listing, recursion depth, pruning,
truncation, and traversal confinement."""

from __future__ import annotations

import os
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


class ParseListArgsTests(unittest.TestCase):
    def setUp(self):
        self.handlers = _fresh_handlers(tempfile.gettempdir())

    def test_no_args(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args([])
        self.assertEqual((subdir, recursive, depth, error), (None, False, 2, None))

    def test_recursive_flag_only(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["-r"])
        self.assertEqual((subdir, recursive, depth, error), (None, True, 2, None))

    def test_subdir_only(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["src"])
        self.assertEqual((subdir, recursive, depth, error), ("src", False, 2, None))

    def test_subdir_then_recursive(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["src", "-r"])
        self.assertEqual((subdir, recursive, depth, error), ("src", True, 2, None))

    def test_recursive_then_subdir(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["-r", "src"])
        self.assertEqual((subdir, recursive, depth, error), ("src", True, 2, None))

    def test_depth_override(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["-r", "3"])
        self.assertEqual((subdir, recursive, depth, error), (None, True, 3, None))

    def test_depth_override_with_subdir(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["src", "-r", "4"])
        self.assertEqual((subdir, recursive, depth, error), ("src", True, 4, None))

    def test_unknown_flag_rejected(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["--foo"])
        self.assertIsNone(subdir)
        self.assertFalse(recursive)
        self.assertIn("Unknown flag", error)

    def test_two_subdirs_rejected(self):
        subdir, recursive, depth, error = self.handlers._parse_list_args(["a", "b"])
        self.assertIsNone(subdir)
        self.assertFalse(recursive)
        self.assertIn("Only one subfolder", error)


class ResolveListTargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handlers = _fresh_handlers(self.tmp.name)

    def test_no_subdir_returns_active_dir(self):
        target, error = self.handlers._resolve_list_target(None, self.tmp.name)
        self.assertEqual(target, os.path.realpath(self.tmp.name))
        self.assertIsNone(error)

    def test_subdir_resolves_inside(self):
        os.mkdir(os.path.join(self.tmp.name, "project-a"))
        target, error = self.handlers._resolve_list_target("project-a", self.tmp.name)
        self.assertEqual(target, os.path.join(os.path.realpath(self.tmp.name), "project-a"))
        self.assertIsNone(error)

    def test_missing_subdir_is_an_error(self):
        target, error = self.handlers._resolve_list_target("nope", self.tmp.name)
        self.assertIsNone(target)
        self.assertIn("not a folder", error)

    def test_parent_traversal_rejected(self):
        target, error = self.handlers._resolve_list_target("..", self.tmp.name)
        self.assertIsNone(target)
        self.assertIn("confined", error)

    def test_symlink_escaping_rejected(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(os.rmdir, outside)
        link = os.path.join(self.tmp.name, "escape")
        os.symlink(outside, link, target_is_directory=True)
        target, error = self.handlers._resolve_list_target("escape", self.tmp.name)
        self.assertIsNone(target)
        self.assertIn("confined", error)


class BuildListingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handlers = _fresh_handlers(self.tmp.name)

    def test_top_level_lists_dirs_before_files(self):
        os.mkdir(os.path.join(self.tmp.name, "zzz-dir"))
        open(os.path.join(self.tmp.name, "aaa.txt"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 1, 5000)
        lines = text.splitlines()[1:]
        self.assertEqual(lines, ["zzz-dir/", "aaa.txt"])

    def test_depth_one_does_not_descend(self):
        sub = os.path.join(self.tmp.name, "sub")
        os.mkdir(sub)
        open(os.path.join(sub, "inner.txt"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 1, 5000)
        self.assertIn("sub/", text)
        self.assertNotIn("inner.txt", text)

    def test_depth_two_lists_one_level_down(self):
        sub = os.path.join(self.tmp.name, "sub")
        os.mkdir(sub)
        open(os.path.join(sub, "inner.txt"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 2, 5000)
        self.assertIn("sub/", text)
        self.assertIn("  inner.txt", text)

    def test_depth_two_does_not_reach_third_level(self):
        sub = os.path.join(self.tmp.name, "sub")
        deep = os.path.join(sub, "deep")
        os.makedirs(deep)
        open(os.path.join(deep, "leaf.txt"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 2, 5000)
        # deep/ shows as a bare name, but leaf.txt is not reached.
        self.assertIn("deep/", text)
        self.assertNotIn("leaf.txt", text)

    def test_excluded_dirs_not_descended_but_listed(self):
        sub = os.path.join(self.tmp.name, "venv")
        os.mkdir(sub)
        open(os.path.join(sub, "site.py"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 2, 5000)
        self.assertIn("venv/", text)
        self.assertNotIn("site.py", text)

    def test_truncation_marker(self):
        for i in range(5):
            open(os.path.join(self.tmp.name, f"f{i}.txt"), "w").close()
        text = self.handlers._build_listing(self.tmp.name, 1, 2)
        self.assertIn("[... truncated after 2 lines]", text)


if __name__ == "__main__":
    unittest.main()
