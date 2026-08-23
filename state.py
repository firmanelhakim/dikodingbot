"""In-flight subprocess records and per-folder locks.

``runs`` maps a workspace directory to the Claude process running there, so
``/status`` and ``/cancel`` can inspect a run without acquiring its lock. One
lock per directory lets two topics work in parallel while two prompts aimed at
the same folder still serialize.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass
class RunState:
    """Fields describing the currently running Claude subprocess.

    All fields are None when no run is active.
    """

    proc: "asyncio.subprocess.Process | None" = None
    pid: "int | None" = None
    prompt: "str | None" = None
    run_dir: "str | None" = None
    start_time: "float | None" = None

    def clear(self) -> None:
        self.proc = None
        self.pid = None
        self.prompt = None
        self.run_dir = None
        self.start_time = None

    def set(
        self,
        proc: "asyncio.subprocess.Process",
        prompt: str,
        run_dir: str,
        start_time: float,
    ) -> None:
        self.proc = proc
        self.pid = proc.pid
        self.prompt = prompt
        self.run_dir = run_dir
        self.start_time = start_time


# Shared instances. runner writes to ``runs``; status/cancel handlers read it.
# Each folder gets its own slot and lock so different topics run concurrently.
runs: dict[str, RunState] = {}

# Per-folder locks. Locks are created lazily and never removed, which is fine:
# there is one per workspace directory, not per run.
_locks: dict[str, asyncio.Lock] = {}


def get_lock(run_dir: str) -> asyncio.Lock:
    """Return the lock for ``run_dir``, creating it on first use."""
    lock = _locks.get(run_dir)
    if lock is None:
        lock = _locks[run_dir] = asyncio.Lock()
    return lock


def get_run(run_dir: str) -> RunState:
    """Return the (possibly empty) run record for ``run_dir``."""
    run = runs.get(run_dir)
    if run is None:
        run = runs[run_dir] = RunState()
    return run
