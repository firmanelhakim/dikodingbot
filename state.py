"""In-flight subprocess record.

``current_run`` holds a reference to the running Claude process so ``/status``
and ``/cancel`` can inspect it without acquiring ``claude_lock`` (which is held
for the entire duration of a run).
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


# Shared single instance. runner writes it; status/cancel handlers read it.
current_run = RunState()

# Only one claude run at a time, so concurrent updates can't start two
# ``--resume`` calls on the same session or change workspace mid-run.
claude_lock = asyncio.Lock()
