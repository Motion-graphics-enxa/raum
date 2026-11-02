"""raum-tts GPU admission (src/admission.py): live vs background priority, with a real lock and fake jobs.

Plain pytest (each test runs its own event loop), no model: ``cd core && uv run pytest ../services/tts/tests``
(also part of core's default run, see core/pyproject.toml ``testpaths``)."""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import threading
import time
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"


def _load_admission():
    if "admission" in sys.modules:  # server.py imports it under this name, so a server test may have loaded it
        return sys.modules["admission"]
    spec = importlib.util.spec_from_file_location("admission", SRC / "admission.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["admission"] = mod
    spec.loader.exec_module(mod)
    return mod


adm = _load_admission()


def gate(**kw):
    return adm.Gate(admit_wait_s=0.4, grace_s=0.1, bg_max_chars=50, **kw)


def fake_job(g, ticket, *, release_after_s=0.0):
    """A worker that polls its stop flag every 10 ms "token" (as cb_engine polls should_stop) and frees the GPU."""
    done = threading.Event()

    def run():
        while not ticket.preempted.wait(0.01):
            pass
        time.sleep(release_after_s)  # e.g. a vocode that was already running
        g.release(ticket)
        done.set()

    threading.Thread(target=run, daemon=True).start()
    return done


def test_priority_of():
    assert adm.priority_of(None) == adm.priority_of("") == adm.priority_of("live") == adm.LIVE
    assert adm.priority_of(" Background ") == adm.BACKGROUND
    assert adm.priority_of("low") is None and adm.priority_of(1) is None


def test_background_only_on_a_free_gpu_and_never_queued():
    g = gate()
    t, why = g.try_background(1.0, chars=10)
    assert why is None and t.background and g.holder is t and g.lock.locked()
    assert g.try_background(1.0) == (None, "busy: GPU in use")  # another background request: refused at once
    g.release(t)
    assert g.holder is None and not g.lock.locked()
    t, why = g.try_background(1.0, chars=51)
    assert t is None and why.startswith("too long") and not g.lock.locked()


def test_background_refused_while_a_live_request_speaks():
    async def main():
        g = gate()
        live, why = await g.admit_live(5.0)
        assert why is None and live.priority == adm.LIVE
        t0 = time.perf_counter()
        assert g.try_background(1.0) == (None, "busy: GPU in use")
        assert time.perf_counter() - t0 < 0.01 and g.live_waiting == 0  # no wait, not counted as waiting
        g.release(live)

    asyncio.run(main())


def test_a_waiting_live_request_keeps_background_off_even_a_free_gpu():
    async def main():
        go = asyncio.Event()

        async def acquire(lock, timeout):  # the waiting live request's thread, not yet woken
            await go.wait()
            return lock.acquire(False)

        g = gate(acquire=acquire)
        task = asyncio.create_task(g.admit_live(1.0))
        await asyncio.sleep(0)
        assert g.live_waiting == 1 and not g.lock.locked()  # the instant between one job and the next
        assert g.try_background(1.0) == (None, "busy: 1 live waiting")
        go.set()
        live, why = await task
        assert why is None and g.holder is live and g.live_waiting == 0
        g.release(live)

    asyncio.run(main())


def test_live_preempts_background_and_takes_the_gpu():
    async def main():
        g = gate()
        bg, _ = g.try_background(30.0)  # a long background clause: predicted 30 s, would mean 503 for a live one
        done = fake_job(g, bg, release_after_s=0.03)
        t0 = time.perf_counter()
        live, why = await g.admit_live(1.0)
        waited = time.perf_counter() - t0
        assert why is None and live.priority == adm.LIVE and g.holder is live
        assert bg.preempted.is_set() and done.is_set() and g.preemptions == 1
        assert waited < 0.1  # one token step plus the running vocode, inside the grace period
        assert g.try_background(1.0) == (None, "busy: GPU in use")
        g.release(live)

    asyncio.run(main())


def test_a_slow_preempted_job_is_still_waited_for_within_the_admit_window():
    async def main():
        g = gate()
        bg, _ = g.try_background(30.0)
        fake_job(g, bg, release_after_s=0.2)  # longer than the 0.1 s grace, shorter than the 0.4 s window
        live, why = await g.admit_live(1.0)
        assert why is None and live is g.holder  # a preempted job predicts no wait: no 503
        g.release(live)

    asyncio.run(main())


def test_live_admission_is_unchanged_between_live_requests():
    async def main():
        g = gate()
        long_live, _ = await g.admit_live(30.0)
        t0 = time.perf_counter()
        nxt, why = await g.admit_live(1.0)
        assert nxt is None and why.startswith("busy: predicted wait") and time.perf_counter() - t0 < 0.3
        assert not long_live.preempted.is_set() and g.preemptions == 0  # live never preempts live
        g.release(long_live)

        short, _ = await g.admit_live(0.05)  # predicted to end soon: the next live request waits for it
        threading.Timer(0.3, g.release, args=(short,)).start()  # after the third's grace, within the second's wait
        second = asyncio.create_task(g.admit_live(1.0))
        await asyncio.sleep(0.12)  # the second is past its grace, now waiting
        third, why = await g.admit_live(1.0)
        assert third is None and why == "busy: predicted wait 0.0s, 1 waiting"  # one waiter at most
        t, why = await second
        assert why is None and g.holder is t
        g.release(t)

    asyncio.run(main())


def test_quiet_cuts_drops_only_the_intentional_cut():
    f = adm.QuietCuts()

    def rec(exc):
        info = (type(exc), exc, None) if exc is not None else None
        return logging.LogRecord("uvicorn.error", logging.ERROR, __file__, 1, "Exception in ASGI application",
                                 None, info)

    assert not f.filter(rec(adm.StreamCut("tts bg")))
    assert not f.filter(rec(ExceptionGroup("body", [adm.StreamCut("tts bg")])))
    assert f.filter(rec(RuntimeError("a real bug"))) and f.filter(rec(None))
    assert f.filter(rec(ExceptionGroup("body", [adm.StreamCut("x"), RuntimeError("y")])))
