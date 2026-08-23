"""Tests for per-folder concurrency - one lock and one run record per
workspace directory, so two topics can run in parallel while one folder
serializes."""

from __future__ import annotations

import asyncio
import unittest

from tests.support import fresh_import, install_telegram_stubs

install_telegram_stubs()


def _fresh_state():
    return fresh_import("state", {"BOT_TOKEN": "t", "ALLOWED_USER_ID": "1"})


class GetLockTests(unittest.TestCase):
    def setUp(self):
        self.state = _fresh_state()

    def test_same_folder_returns_same_lock(self):
        self.assertIs(self.state.get_lock("/base/a"), self.state.get_lock("/base/a"))

    def test_different_folders_return_different_locks(self):
        self.assertIsNot(self.state.get_lock("/base/a"), self.state.get_lock("/base/b"))

    def test_lock_is_actually_usable(self):
        # A lock from get_lock must acquire and release like any asyncio.Lock.
        lock = self.state.get_lock("/base/a")

        async def _use():
            async with lock:
                return "acquired"

        self.assertEqual(asyncio.run(_use()), "acquired")


class GetRunTests(unittest.TestCase):
    def setUp(self):
        self.state = _fresh_state()

    def test_same_folder_returns_same_record(self):
        self.assertIs(self.state.get_run("/base/a"), self.state.get_run("/base/a"))

    def test_different_folders_return_different_records(self):
        a = self.state.get_run("/base/a")
        b = self.state.get_run("/base/b")
        self.assertIsNot(a, b)
        a.pid = 1
        self.assertIsNone(b.pid)


class ParallelismTests(unittest.TestCase):
    """Two folders run concurrently; one folder serializes. This is the
    property the whole feature exists for, expressed without network or
    subprocesses."""

    def setUp(self):
        self.state = _fresh_state()

    def _acquire(self, folder, start_event, entered, release_event):
        async def _inner():
            lock = self.state.get_lock(folder)
            async with lock:
                entered.append(folder)
                start_event.set()
                await release_event.wait()

        return asyncio.create_task(_inner())

    def test_different_folders_do_not_block_each_other(self):
        # Both locks must be acquirable at once: folder A entering must not
        # wait for folder B.
        start_a, start_b = asyncio.Event(), asyncio.Event()
        release_a, release_b = asyncio.Event(), asyncio.Event()
        entered: list[str] = []

        async def _run():
            ta = self._acquire("/base/a", start_a, entered, release_a)
            tb = self._acquire("/base/b", start_b, entered, release_b)
            await asyncio.wait_for(start_a.wait(), timeout=1)
            await asyncio.wait_for(start_b.wait(), timeout=1)
            self.assertEqual(set(entered), {"/base/a", "/base/b"})
            # Clean up.
            release_a.set()
            release_b.set()
            await asyncio.gather(ta, tb)

        asyncio.run(_run())

    def test_same_folder_serializes(self):
        # A second acquire on the same folder must block until the first
        # releases, so only one holder is ever inside.
        start = asyncio.Event()
        release = asyncio.Event()
        entered: list[str] = []

        async def _run():
            t1 = self._acquire("/base/a", start, entered, release)
            await asyncio.wait_for(start.wait(), timeout=1)
            t2 = self._acquire("/base/a", start, entered, release)
            # t1 holds the lock; t2 must not have entered yet.
            await asyncio.sleep(0)
            self.assertEqual(entered, ["/base/a"])
            release.set()
            await asyncio.gather(t1, t2)

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
