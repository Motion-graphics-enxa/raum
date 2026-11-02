"""GPU priority scheduler (plan §6.12): slots per class, live preempts lower classes."""

from __future__ import annotations

import asyncio

import pytest

from raum.config import load_config
from raum.scheduler import Scheduler


async def test_slots_limit_concurrency():
    s = Scheduler(live_slots=2)
    running = peak = 0

    async def job():
        nonlocal running, peak
        async with s.slot("live"):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1

    await asyncio.gather(*(job() for _ in range(6)))
    assert peak == 2
    assert s.stats()["live"] == {"running": 0, "waiting": 0, "slots": 2, "done": 6}


async def test_live_waiting_pauses_background():
    s = Scheduler(live_slots=1, check_slots=1, background_slots=1)
    order: list[str] = []
    release_first = asyncio.Event()

    async def hold_live():
        async with s.slot("live"):
            order.append("live1")
            await release_first.wait()

    async def job(prio, name):
        async with s.slot(prio):
            order.append(name)

    t1 = asyncio.create_task(hold_live())
    await asyncio.sleep(0)
    t2 = asyncio.create_task(job("live", "live2"))  # waits for the live slot
    await asyncio.sleep(0)
    t3 = asyncio.create_task(job("background", "bg"))  # its own slot is free, but live is waiting
    t4 = asyncio.create_task(job("check", "check"))
    await asyncio.sleep(0.01)
    assert order == ["live1"]
    assert s.stats()["background"]["waiting"] == 1
    release_first.set()
    await asyncio.gather(t1, t2, t3, t4)
    assert order[:2] == ["live1", "live2"]
    assert order.index("check") < order.index("bg")


async def test_cancelled_waiter_unblocks_lower_classes():
    s = Scheduler(live_slots=1, background_slots=1)
    gate = asyncio.Event()
    ran: list[str] = []

    async def hold():
        async with s.slot("live"):
            await gate.wait()

    async def bg():
        async with s.slot("background"):
            ran.append("bg")

    holder = asyncio.create_task(hold())
    await asyncio.sleep(0)
    waiter = asyncio.create_task(s.acquire("live"))
    await asyncio.sleep(0)
    b = asyncio.create_task(bg())
    await asyncio.sleep(0.01)
    assert ran == []
    waiter.cancel()
    await asyncio.sleep(0.01)
    assert ran == ["bg"]
    gate.set()
    await asyncio.gather(holder, b)
    assert s.running == {"live": 0, "check": 0, "background": 0}


async def test_run_helper_unknown_priority_and_zero_slots():
    s = Scheduler(live_slots=1, check_slots=0, background_slots=0)
    assert s.slots["check"] == 1 and s.slots["background"] == 1  # 0 would deadlock

    async def add(a, b):
        return a + b

    assert await s.run("check", add, 1, 2) == 3
    with pytest.raises(ValueError):
        await s.acquire("urgent")


async def test_shared_per_event_loop():
    cfg = load_config()
    a = Scheduler.shared(cfg)
    assert Scheduler.shared(cfg) is a
    assert a.slots["live"] == cfg.scheduler.live_slots
