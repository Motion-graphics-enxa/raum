"""Wait vs work per stage of one utterance: ``pipeline.timing`` ``stages`` (where a slow utterance queued).

One :class:`Stages` per utterance rides in a ContextVar, so an engine records what it measured without new
parameters: the room sets it at the top of each utterance task (``use``), tasks spawned from there inherit it
(asyncio copies the context at ``create_task``), and an engine call adds its numbers with ``note``:

    stats.note("mt", wall_ms=412.0, ttft_ms=180.2, load_ms=0.0, prompt_ms=61.3, eval_ms=220.4)   # translate.py
    stats.note("slot.live", wait_ms=35.0)                                                         # a queue

Values add up per stage (``n`` counts the calls); ``mark`` keeps the first time something happened, in ms after
the utterance's t0 (the speaker's stop). ``scope("spec")`` prefixes every stage noted inside it ("spec.mt",
"echo.asr"), so the same engine call is told apart by who made it. No Stages set: every call is a no-op.
Only numbers, never text: timing events reach the glass box and the logs.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

__all__ = ["Stages", "current", "mark", "note", "scope", "timed", "use"]


class Stages:
    __slots__ = ("rec", "t0")

    def __init__(self, t0: float | None = None) -> None:
        self.t0 = time.monotonic() if t0 is None else t0
        self.rec: dict[str, dict[str, float]] = {}

    def add(self, stage: str, **ms: float | None) -> None:
        d = self.rec.setdefault(stage, {})
        d["n"] = d.get("n", 0) + 1
        for k, v in ms.items():
            if v is not None:
                d[k] = round(d.get(k, 0.0) + float(v), 1)

    def mark(self, stage: str, key: str = "at_ms", t: float | None = None) -> None:
        d = self.rec.setdefault(stage, {})
        if key not in d:  # the first time counts
            d[key] = round(((time.monotonic() if t is None else t) - self.t0) * 1000.0, 1)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {k: dict(v) for k, v in sorted(self.rec.items())}


_cur: ContextVar[Stages | None] = ContextVar("raum_stages", default=None)
_prefix: ContextVar[str] = ContextVar("raum_stage_prefix", default="")


def current() -> Stages | None:
    return _cur.get()


def use(s: Stages | None) -> Token[Stages | None]:
    """This task (and what it spawns from now on) records into ``s``."""
    return _cur.set(s)


def _name(stage: str) -> str:
    p = _prefix.get()
    return f"{p}.{stage}" if p else stage


def note(stage: str, **ms: float | None) -> None:
    s = _cur.get()
    if s is not None:
        s.add(_name(stage), **ms)


def mark(stage: str, key: str = "at_ms", t: float | None = None) -> None:
    s = _cur.get()
    if s is not None:
        s.mark(_name(stage), key, t)


@contextmanager
def scope(prefix: str) -> Iterator[None]:
    tok = _prefix.set(_name(prefix))
    try:
        yield
    finally:
        _prefix.reset(tok)


@contextmanager
def timed(stage: str, key: str = "wait_ms") -> Iterator[None]:
    """``with stats.timed("slot.live"): await sched.acquire("live")``: the time inside, added to ``key``."""
    t = time.monotonic()
    try:
        yield
    finally:
        note(stage, **{key: (time.monotonic() - t) * 1000.0})
