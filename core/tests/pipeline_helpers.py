"""Shared helpers for the pipeline tests: fast mock config, a harness with a collecting emit, and a
tiny fake fact guard (so these tests do not depend on the real guard's normalisation rules)."""

from __future__ import annotations

import asyncio
import re

import numpy as np

from raum.config import RaumConfig, load_config
from raum.events import Envelope, FactCheck, ParticipantInfo
from raum.modes import get_mode
from raum.pipeline.engines import build_engines
from raum.pipeline.room import RoomPipeline
from raum.verify.guard import Fact, GuardResult


def fast_config() -> RaumConfig:
    cfg = load_config()
    m = cfg.models
    for stage in ("asr", "translate", "verify", "embed", "tts"):
        getattr(m, stage).engine = "mock"
    m.denoise.engine = "passthrough"
    m.voicelock.engine = "off"  # F-10: tests turn it on themselves (a box may have it on in raum.yaml)
    m.asr.options = {"latency_ms": 2, "partial_latency_ms": 1, "partial_every_ms": 300}
    m.translate.options = {"first_token_ms": 3, "token_ms": 0}
    m.verify.options = {"latency_ms": 3}
    m.tts.options = {"first_audio_ms": 1}
    cfg.scheduler.live_slots, cfg.scheduler.check_slots, cfg.scheduler.background_slots = 4, 2, 2
    return cfg


DOC = ParticipantInfo(id="a-doc1", role="a", lang="de")
PAT = ParticipantInfo(id="b-k3x9", role="b", lang="ar")


class Harness:
    def __init__(self, mode: str = "sprechzimmer", room: str = "sz-1000",
                 participants: list[ParticipantInfo] | None = None, cfg: RaumConfig | None = None) -> None:
        self.cfg = cfg or fast_config()
        self.engines = build_engines(self.cfg, seed=0)
        self.events: list[Envelope] = []

        async def emit(env: Envelope) -> None:
            self.events.append(env)

        self.p = RoomPipeline(room, get_mode(mode), self.engines, emit, self.cfg)
        self.p.set_participants(participants if participants is not None else [DOC, PAT])

    async def say(self, speaker: str, seconds: float = 1.0, vad_ms: float = 350.0) -> str | None:
        utt = await self.p.speech_start(speaker)
        frames = int(seconds * 50)  # 20 ms frames
        for i in range(frames):
            await self.p.speech_audio(speaker, np.zeros(320, dtype=np.float32))
            if i % 10 == 9:
                await asyncio.sleep(0.01)  # let partial ASR run
        await self.p.speech_end(speaker, None, vad_ms)
        return utt

    def of(self, type: str, utt: str | None = None) -> list[Envelope]:
        return [e for e in self.events if e.type == type and (utt is None or e.utt == utt)]

    def one(self, type: str, utt: str | None = None) -> dict:
        found = self.of(type, utt)
        assert len(found) == 1, (type, [e.data for e in found])
        return found[0].data

    def types(self, utt: str | None = None) -> list[str]:
        return [e.type for e in self.events if utt is None or e.utt == utt]

    def set_next_line(self, role: str, index: int) -> None:
        """Point the mock ASR cursor at the role's ``index``-th line."""
        self.engines.asr._cursor[(self.p.room, role)] = index  # type: ignore[attr-defined]


# ---------------------------------------------------------------- fake fact guard (doses only)

_DOSE = re.compile(r"(\d+)\s*(?:mg|мг|ملغ)")


def fake_extract(text, lang, *, kinds=None, glossary=None):
    out = [Fact(kind="dose", value=f"{m.group(1)} mg", surface=m.group(0), span=m.span()) for m in _DOSE.finditer(text)]
    return [f for f in out if kinds is None or f.kind in kinds]


def fake_check(src, src_lang, tgt, tgt_lang, *, kinds=None, confirm_on=(), glossary=None):
    a = fake_extract(src, src_lang)
    rest = [f.value for f in fake_extract(tgt, tgt_lang)]
    facts = []
    for f in a:
        if f.value in rest:
            rest.remove(f.value)
            facts.append(FactCheck(kind=f.kind, src=f.value, tgt=f.value, match=True))
        else:
            facts.append(FactCheck(kind=f.kind, src=f.value, tgt=rest.pop(0) if rest else None, match=False))
    facts += [FactCheck(kind="dose", src=None, tgt=v, match=False) for v in rest]
    confirm = [f for f in facts if f.kind in set(confirm_on) and f.src]
    return GuardResult(facts=facts, red=any(not f.match for f in facts), confirm=confirm, ms=0.05)


def not_implemented(*_a, **_k):
    raise NotImplementedError
