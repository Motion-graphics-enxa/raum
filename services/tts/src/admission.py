"""GPU admission for raum-tts: one synthesis at a time, two request priorities (API.md, "Priorities").

Pure Python (threading, time; anyio only when a live request really waits), so it is tested without the model:
``services/tts/tests/``.

- ``live`` (the default; RAUM's spoken clauses): waits at most ``admit_wait_s`` for the GPU, else it is refused at
  once (HTTP 503) and RAUM speaks that clause with Piper. Only live requests count as waiting. A live request
  that arrives while a background job holds the GPU preempts it: the job's ``Ticket.preempted`` is set, the job
  stops at its next token step (cb_engine polls ``should_stop`` every token, ~20 ms; a vocode already running
  finishes first) and frees the GPU for the live request.
- ``background`` (speculative clauses made before the speaker's end of turn): admitted only if the GPU is free
  right now and no live request is waiting, else refused at once (HTTP 409). Never queued, never counted as
  waiting, so it never pushes a live request towards a 503.
"""
from __future__ import annotations

import builtins
import logging
import threading
import time
from collections.abc import Awaitable, Callable

LIVE, BACKGROUND = "live", "background"
PRIORITIES = (LIVE, BACKGROUND)


def priority_of(value) -> str | None:
    """A request's priority: missing or empty -> live; anything else unknown -> None (the caller answers 422)."""
    p = str(value or LIVE).strip().lower()
    return p if p in PRIORITIES else None


class Ticket:
    """The GPU, held by one request. ``preempted`` is only ever set on a background ticket."""

    def __init__(self, priority: str, expected_s: float, now: float) -> None:
        self.priority = priority
        self.t0 = now
        self.expected_end = now + max(0.0, expected_s)  # for the live admission's predicted wait
        self.preempted = threading.Event()

    @property
    def background(self) -> bool:
        return self.priority == BACKGROUND


async def _thread_acquire(lock: threading.Lock, timeout: float) -> bool:
    import anyio  # lazy: only a waiting live request needs it. run_sync is shielded, so a cancelled
    # request can never leave the lock taken by an orphaned thread
    return await anyio.to_thread.run_sync(lock.acquire, True, timeout)


class Gate:
    """Who may use the GPU next. All methods except ``release`` run on the event loop; ``release`` may run on the
    job's worker thread."""

    def __init__(self, *, admit_wait_s: float = 1.0, grace_s: float = 0.25, bg_max_chars: int = 400,
                 acquire: Callable[[threading.Lock, float], Awaitable[bool]] | None = None,
                 clock: Callable[[], float] = time.perf_counter) -> None:
        self.lock = threading.Lock()
        self.admit_wait_s, self.grace_s, self.bg_max_chars = admit_wait_s, grace_s, bg_max_chars
        self._acquire = acquire or _thread_acquire
        self.clock = clock
        self.live_waiting = 0
        self.holder: Ticket | None = None
        self.preemptions = 0

    def predicted_wait(self) -> float:
        h = self.holder
        if h is None or h.preempted.is_set():  # a preempted job lets go within a token step
            return 0.0
        return max(0.0, h.expected_end - self.clock())

    def _hold(self, priority: str, expected_s: float) -> Ticket:
        t = Ticket(priority, expected_s, self.clock())
        self.holder = t
        return t

    def _preempt(self) -> None:
        h = self.holder
        if h is not None and h.background and not h.preempted.is_set():
            h.preempted.set()
            self.preemptions += 1

    async def admit_live(self, expected_s: float) -> tuple[Ticket | None, str | None]:
        """(ticket, None), or (None, why) when the predicted wait is over ``admit_wait_s`` or another live request
        is already waiting. Never waits longer than ``admit_wait_s``."""
        self.live_waiting += 1
        self._preempt()
        try:
            # short grace first: a job whose listener just hung up, or a preempted background job, lets go fast
            ok = await self._acquire(self.lock, self.grace_s)
            if not ok:
                wait = self.predicted_wait()
                if self.live_waiting > 1 or wait > self.admit_wait_s - self.grace_s:
                    return None, f"busy: predicted wait {wait:.1f}s, {self.live_waiting - 1} waiting"
                ok = await self._acquire(self.lock, self.admit_wait_s - self.grace_s)
        finally:
            self.live_waiting -= 1
        if not ok:
            return None, f"busy: no GPU within {self.admit_wait_s:.1f}s"
        return self._hold(LIVE, expected_s), None

    def try_background(self, expected_s: float, chars: int = 0) -> tuple[Ticket | None, str | None]:
        """Never waits: (ticket, None) only if the GPU is free right now and no live request waits for it."""
        if chars > self.bg_max_chars:
            return None, f"too long: {chars} chars, background max {self.bg_max_chars}"
        if self.live_waiting:  # the lock may be free for an instant before the waiting live request takes it
            return None, f"busy: {self.live_waiting} live waiting"
        if not self.lock.acquire(blocking=False):
            return None, "busy: GPU in use"
        return self._hold(BACKGROUND, expected_s), None

    def release(self, ticket: Ticket) -> None:
        if self.holder is ticket:
            self.holder = None
        self.lock.release()


# ---------------- a background stream cut mid-clause ----------------
class StreamCut(Exception):
    """Raised inside a background response body when a live request preempted it after audio went out. Uvicorn
    then closes the connection without the final chunk, so the client gets an incomplete body
    (``httpx.RemoteProtocolError``) and cannot take a cut clause for a whole one."""


_GROUP = getattr(builtins, "BaseExceptionGroup", ())  # Python >= 3.11


def is_cut(e: BaseException | None) -> bool:
    if isinstance(e, StreamCut):
        return True
    if isinstance(e, _GROUP):  # some Starlette versions wrap errors of the body task in a group
        return all(is_cut(x) for x in e.exceptions)
    return False


class QuietCuts(logging.Filter):
    """Drops uvicorn's "Exception in ASGI application" traceback for an intentional cut (the server logs its own
    one-line "preempted" message instead)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and is_cut(record.exc_info[1]))
