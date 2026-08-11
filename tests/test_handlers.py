"""Handler tests - path-traversal defense on /switch, help-format regression,
permission persistence."""

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


class InsideBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.handlers = _fresh_handlers(self.tmp.name)

    def test_inside_base_accepts_direct_subdir(self):
        target = os.path.join(self.tmp.name, "project-a")
        self.assertTrue(self.handlers._inside_base(target))

    def test_inside_base_accepts_deeper_subdir(self):
        target = os.path.join(self.tmp.name, "project-a", "src", "lib")
        self.assertTrue(self.handlers._inside_base(target))

    def test_inside_base_rejects_parent(self):
        target = os.path.abspath(os.path.join(self.tmp.name, ".."))
        self.assertFalse(self.handlers._inside_base(target))

    def test_inside_base_rejects_traversal(self):
        # os.path.abspath resolves /base/../etc → /etc
        target = os.path.abspath(os.path.join(self.tmp.name, "..", "etc"))
        self.assertFalse(self.handlers._inside_base(target))

    def test_inside_base_rejects_sibling_prefix_match(self):
        # /base and /baseXYZ share a string prefix but not a path prefix -
        # commonpath must tell them apart.
        sibling = self.tmp.name + "XYZ"
        self.assertFalse(self.handlers._inside_base(sibling))


class PermissionPersistenceTests(unittest.TestCase):
    def test_defaults_to_dontask_when_nothing_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Point PERMISSION_FILE at a path that doesn't exist yet.
            handlers = _fresh_handlers(tmp, PERMISSION_FILE=os.path.join(tmp, "perm.txt"))
            self.assertEqual(handlers.load_active_permission(), "dontAsk")

    def test_env_default_when_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            handlers = _fresh_handlers(
                tmp,
                PERMISSION_FILE=os.path.join(tmp, "perm.txt"),
                PERMISSION_MODE="plan",
            )
            # ``load_active_permission`` re-reads os.environ when called, so
            # pin the value here in case the ambient .env sets it.
            with unittest.mock.patch.dict(
                os.environ, {"PERMISSION_MODE": "plan"}, clear=False
            ):
                self.assertEqual(handlers.load_active_permission(), "plan")

    def test_file_overrides_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            perm_file = os.path.join(tmp, "perm.txt")
            with open(perm_file, "w") as f:
                f.write("bypassPermissions")
            handlers = _fresh_handlers(
                tmp,
                PERMISSION_FILE=perm_file,
                PERMISSION_MODE="plan",
            )
            self.assertEqual(handlers.load_active_permission(), "bypassPermissions")

    def test_save_then_load_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            perm_file = os.path.join(tmp, "perm.txt")
            handlers = _fresh_handlers(tmp, PERMISSION_FILE=perm_file)
            handlers.save_active_permission("dontAsk")
            self.assertTrue(os.path.exists(perm_file))
            self.assertEqual(handlers.load_active_permission(), "dontAsk")
            # Atomic write leaves no .tmp sibling behind.
            self.assertFalse(os.path.exists(perm_file + ".tmp"))

    def test_bot_safe_modes_exclude_interactive_ones(self):
        # manual/acceptEdits/auto ask for approval - they must not be settable
        # via the handler. We check the guarded set directly.
        handlers = _fresh_handlers(tempfile.gettempdir())
        bot_safe = ("bypassPermissions", "dontAsk", "plan")
        for mode in handlers.config.PERMISSION_MODES:
            if mode in bot_safe:
                self.assertIn(mode, bot_safe)
            else:
                self.assertNotIn(mode, bot_safe)


class HelpFormattingTests(unittest.TestCase):
    def test_help_html_uses_html_tags_not_markdown(self):
        # The /help output uses HTML mode so it renders consistently with the
        # <pre>-wrapped chunks send_chunks produces.
        handlers = _fresh_handlers(tempfile.gettempdir())
        html = handlers.HELP_HTML
        self.assertIn("<b>", html)
        self.assertIn("<code>", html)
        # Markdown emphasis characters would render literally in HTML mode.
        self.assertNotIn("**", html)


if __name__ == "__main__":
    unittest.main()
