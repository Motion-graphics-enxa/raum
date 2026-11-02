"""voicegate.py: a box-wide 'live voice in flight' signal (files in a RAM dir) that deferrable GPU work waits on."""

from __future__ import annotations

import asyncio
import os
import time

from raum.pipeline import voicegate


async def test_quiet_waits_while_a_voice_is_in_flight_and_never_longer_than_its_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(voicegate, "DIR", str(tmp_path / "gate"))
    monkeypatch.setattr(voicegate, "ENABLED", True)
    assert not voicegate.busy() and await voicegate.quiet(1.0) == 0.0  # nothing in flight: at once

    async def voice(s: float) -> None:
        async with voicegate.speaking() as mark:
            await asyncio.sleep(s)
            mark.touch()

    v = asyncio.create_task(voice(0.15))
    await asyncio.sleep(0.02)
    assert voicegate.busy()
    t = time.monotonic()
    waited = await voicegate.quiet(1.0)
    assert 0.08 < waited < 0.4 and time.monotonic() - t < 0.4  # until the voice was done
    await v
    assert not voicegate.busy() and os.listdir(tmp_path / "gate") == []
    v = asyncio.create_task(voice(1.0))
    await asyncio.sleep(0.02)
    assert 0.09 < await voicegate.quiet(0.1) < 0.2  # bounded: delayed, never dropped
    v.cancel()


async def test_a_stale_mark_from_a_dead_process_does_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(voicegate, "DIR", str(tmp_path))
    monkeypatch.setattr(voicegate, "ENABLED", True)
    p = tmp_path / "999-0"
    p.write_text("")
    old = time.time() - voicegate.STALE_S - 1
    os.utime(p, (old, old))
    assert not voicegate.busy()


async def test_off_and_without_the_dir_it_is_a_no_op(tmp_path, monkeypatch):
    monkeypatch.setattr(voicegate, "DIR", str(tmp_path / "missing" / "deeper"))
    monkeypatch.setattr(voicegate, "ENABLED", False)
    async with voicegate.speaking():
        assert not voicegate.busy() and await voicegate.quiet(1.0) == 0.0
