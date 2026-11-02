"""Voice in flight: a box-wide signal that some room's live voice is being synthesised right now.

Every room runs in its own agent process (JobExecutorType.PROCESS), so each has its own ``Scheduler``; the
GPU is one. While a live clause is synthesised, partial ASR, the second check (gpt-oss), the echo check and
speculative MT time-slice the same GPU and the voice's per-token step goes from ~8 ms to ~26 ms (stalls,
late first audio). This module lets those jobs wait briefly until no voice is in flight, in any room:

    async with voicegate.speaking():          # around one live synthesis request (tts_http)
        ...
    await voicegate.quiet(max_s)              # before deferrable GPU work: returns at once when no voice runs

One file per synthesis in ``/dev/shm/raum-voice`` (RAM, no disk I/O); mtime is refreshed while it streams and
a file older than STALE_S counts as gone (a killed process never blocks the others). ``RAUM_VOICE_GATE=0``
turns every wait into a no-op. Waits are bounded: deferred work is delayed, never dropped.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import time
from collections.abc import AsyncIterator

DIR = os.environ.get("RAUM_VOICE_GATE_DIR", "/dev/shm/raum-voice")
ENABLED = os.environ.get("RAUM_VOICE_GATE", "1") not in ("0", "false", "off")
STALE_S = 6.0      # a clause synthesises in < 3 s; refreshed every chunk, so only a dead writer goes stale
POLL_S = 0.025
_n = itertools.count()


def busy(now: float | None = None) -> bool:
    """True while any room's live voice is being synthesised (any fresh file in DIR)."""
    if not ENABLED:
        return False
    now = time.time() if now is None else now
    try:
        with os.scandir(DIR) as it:
            for e in it:
                try:
                    if now - e.stat().st_mtime < STALE_S:
                        return True
                except FileNotFoundError:
                    continue
    except FileNotFoundError:
        return False
    return False


async def quiet(max_s: float) -> float:
    """Wait until no voice is in flight, at most ``max_s``. Returns the seconds waited (0.0 at once if quiet)."""
    if not ENABLED or max_s <= 0 or not busy():
        return 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < max_s:
        await asyncio.sleep(POLL_S)
        if not busy():
            break
    return time.monotonic() - t0


class _Mark:
    def __init__(self) -> None:
        self.path = os.path.join(DIR, f"{os.getpid()}-{next(_n)}")
        self._t = 0.0

    def touch(self) -> None:
        now = time.time()
        if now - self._t < 0.5:
            return
        self._t = now
        try:
            os.utime(self.path)
        except FileNotFoundError:
            pass


@contextlib.asynccontextmanager
async def speaking() -> AsyncIterator[_Mark]:
    """Marks one live synthesis as in flight for its whole duration; ``mark.touch()`` per chunk keeps it fresh."""
    m = _Mark()
    if ENABLED:
        try:
            os.makedirs(DIR, mode=0o700, exist_ok=True)  # ours only (the GX10 is shared)
            os.close(os.open(m.path, os.O_CREAT | os.O_WRONLY, 0o600))  # tmpfs: microseconds
        except OSError:
            pass  # no /dev/shm: the gate is simply off
    try:
        yield m
    finally:
        with contextlib.suppress(OSError):
            os.unlink(m.path)
