"""GPU priority scheduler (plan §6.12, F-01).

Three classes with their own slot counts from ``raum.yaml`` (``scheduler:``):

    live        finals, translation              (live_slots)
    check       back-translation, embeddings     (check_slots)
    background  partials, summaries, vision      (background_slots)

**Live always preempts:** a lower class may only start while no higher class is waiting for a
slot, so a saturated GPU pauses the background queue first (§6.15). Running work is never
interrupted; ordering applies to what starts next.

    sched = Scheduler.shared(cfg)          # one per event loop (= per agent process / job thread)
    async with sched.slot("live"):
        ...
"""

from __future__ import annotations

import asyncio
import weakref
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any, ClassVar, TypeVar

from raum.config import RaumConfig, SchedulerConfig

T = TypeVar("T")

PRIORITIES = ("live", "check", "background")


class Scheduler:
    def __init__(self, live_slots: int = 2, check_slots: int = 1, background_slots: int = 1) -> None:
        # 0 slots (e.g. lite: check_slots 0, verify off) would deadlock any work that still arrives
        self.slots = {"live": max(1, live_slots), "check": max(1, check_slots),
                      "background": max(1, background_slots)}
        self.running = {p: 0 for p in PRIORITIES}
        self.waiting: dict[str, deque[asyncio.Future[None]]] = {p: deque() for p in PRIORITIES}
        self.completed = {p: 0 for p in PRIORITIES}

    @classmethod
    def from_config(cls, cfg: RaumConfig | SchedulerConfig) -> Scheduler:
        s = cfg.scheduler if isinstance(cfg, RaumConfig) else cfg
        return cls(s.live_slots, s.check_slots, s.background_slots)

    _shared: ClassVar[weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Scheduler]] = \
        weakref.WeakKeyDictionary()

    @classmethod
    def shared(cls, cfg: RaumConfig) -> Scheduler:
        """The scheduler of the running event loop (all rooms on this loop share the GPU queue)."""
        loop = asyncio.get_running_loop()
        s = cls._shared.get(loop)
        if s is None:
            s = cls._shared[loop] = cls.from_config(cfg)
        return s

    # ---------------------------------------------------------------- core

    def _blocked_by_higher(self, prio: str) -> bool:
        for p in PRIORITIES:
            if p == prio:
                return False
            if self.waiting[p]:
                return True
        return False

    def _can_start(self, prio: str) -> bool:
        return self.running[prio] < self.slots[prio] and not self._blocked_by_higher(prio)

    def _wake(self) -> None:
        for p in PRIORITIES:
            q = self.waiting[p]
            while q and self.running[p] < self.slots[p]:
                fut = q.popleft()
                if fut.done():
                    continue
                self.running[p] += 1
                fut.set_result(None)
            if q:
                return  # a higher class is still waiting: lower classes stay paused

    async def acquire(self, prio: str) -> None:
        if prio not in self.slots:
            raise ValueError(f"unknown priority: {prio!r}")
        if not self.waiting[prio] and self._can_start(prio):
            self.running[prio] += 1
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.waiting[prio].append(fut)
        try:
            await fut
        except asyncio.CancelledError:
            if fut.done() and not fut.cancelled():
                self.release(prio)  # granted, then cancelled before use: give it back
            else:
                try:
                    self.waiting[prio].remove(fut)
                except ValueError:
                    pass
                self._wake()
            raise

    def release(self, prio: str) -> None:
        self.running[prio] -= 1
        self.completed[prio] += 1
        self._wake()

    @asynccontextmanager
    async def slot(self, prio: str) -> AsyncIterator[None]:
        await self.acquire(prio)
        try:
            yield
        finally:
            self.release(prio)

    async def run(self, prio: str, fn: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        async with self.slot(prio):
            return await fn(*args, **kwargs)

    def stats(self) -> dict[str, Any]:
        """For the glass box: running / waiting / slots per class."""
        return {p: {"running": self.running[p], "waiting": len(self.waiting[p]), "slots": self.slots[p],
                    "done": self.completed[p]} for p in PRIORITIES}
