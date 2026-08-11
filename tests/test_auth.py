"""Authorization tests - the single-operator gate."""

from __future__ import annotations

import unittest

from tests.support import fresh_import, install_telegram_stubs, make_fake_update

install_telegram_stubs()


def _fresh_auth(allowed_id: str = "42"):
    return fresh_import("auth", {
        "BOT_TOKEN": "t",
        "ALLOWED_USER_ID": allowed_id,
    })


class AuthTests(unittest.TestCase):
    def test_authorized_matches_configured_id(self):
        auth = _fresh_auth("42")
        self.assertTrue(auth.authorized(make_fake_update(42)))

    def test_rejects_other_user(self):
        auth = _fresh_auth("42")
        self.assertFalse(auth.authorized(make_fake_update(43)))

    def test_rejects_missing_user(self):
        auth = _fresh_auth("42")
        self.assertFalse(auth.authorized(make_fake_update(None)))

    def test_rejects_real_users_when_allowed_id_is_zero(self):
        # Fail-safe: ALLOWED_USER_ID=0 rejects every real Telegram user
        # (Telegram IDs are strictly positive; id==0 doesn't exist).
        auth = _fresh_auth("0")
        self.assertFalse(auth.authorized(make_fake_update(1)))
        self.assertFalse(auth.authorized(make_fake_update(999999999)))


if __name__ == "__main__":
    unittest.main()
