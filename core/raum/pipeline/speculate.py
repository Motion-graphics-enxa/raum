"""Speculative translation (both modes). Hooked into ``room.py``.

While the speaker is finishing, the latest candidate text is translated in the background, at low priority
(scheduler ``background`` slot): a partial that reads as a finished utterance (``simul.complete``; usually
the partial before the stable one, which is the final), or the early final transcript of a pause while the
semantic end of turn (turn.py) waits out the silence. One speculation per utterance and language; a newer text replaces it. Nothing of it is
ever emitted. When the turn's translation
starts and the final transcript equals that text (whitespace and case normalised; same direction, context
and glossary), the finished (or still streaming) speculative translation is replayed as the live deltas:
MT ~ 0 ms after the endpoint. Otherwise it is discarded.

The first phrase of a speculative translation (``room.take_phrase``) is also synthesised ahead; the buffered
audio plays only when the live translation produces exactly that phrase, i.e. once the turn ended and the
final matched. Nothing is published before the end-of-turn decision. A resume (``speech_start`` during the
end-of-turn wait) drops both.
Simultaneous lanes (simul.py) are not speculated: their tail is short.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np

from raum.pipeline.base import TranslateRequest

if TYPE_CHECKING:
    from raum.pipeline.room import RoomPipeline, _Utt

log = logging.getLogger("raum.pipeline.speculate")

_MAX_VOICES = 16


def _same_voice(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Pre-synthesised audio fits a live call: same voice sample, pace within 15 %."""
    if a.get("voice_ref") != b.get("voice_ref"):
        return False
    ra, rb = float(a.get("rate") or 1.0), float(b.get("rate") or 1.0)
    return abs(ra - rb) <= 0.15 * max(ra, rb)


def _key(req: TranslateRequest) -> tuple[Any, ...]:
    return (" ".join(req.text.split()).casefold(), req.src_lang, req.tgt_lang, req.context, req.glossary,
            req.register, req.lang_a, req.lang_b)


@dataclass
class _Spec:
    key: tuple[Any, ...]
    parts: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    started: bool = False
    done: bool = False
    failed: bool = False
    voice: tuple[str, str] | None = None  # (lang, phrase) pre-synthesised
    speaker: str = ""
    slot: str = "background"
    text: str = ""  # the source text, and how long it took to say: the pace for the pre-synthesis
    seconds: float | None = None


@dataclass
class _Voice:
    kw: dict[str, Any] = field(default_factory=dict)  # voice_ref / rate it was made with
    chunks: list[np.ndarray] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    failed: bool = False
    task: asyncio.Task[None] | None = None


class Speculator:
    """Owned by ``RoomPipeline`` (``self.spec``). Hooks: ``on_text``, ``stream``, ``synthesize``, ``drop``."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self._specs: dict[tuple[str, str], _Spec] = {}  # (utt, target lang) -> speculation
        self._voices: dict[tuple[str, str], _Voice] = {}  # (lang, clause) -> pre-synthesised audio
        self.hits = 0  # replayed speculations (glass box / tests)

    @property
    def enabled(self) -> bool:
        return bool(getattr(getattr(self.p.cfg, "pipeline", None), "speculate", True))

    # ------------------------------------------------------------ hooks

    def on_text(self, u: _Utt, text: str, lang: str, *, slot: str = "background",
                seconds: float | None = None) -> None:
        """A candidate text for utterance ``u``: translate it ahead for every consecutive target language.
        ``slot``: ``background`` for partials; ``live`` for the early final ASR of a pause (it is the live
        path whenever the verdict ends the turn). ``seconds``: how long the speaker spoke (voice.py
        ``speech_seconds``), so the pre-synthesised phrase gets the pace the live call will use."""
        if not self.enabled or not text.strip() or u.closed:
            return
        text = self.p.fix_terms(text, lang)  # as room._turn will correct the final, or the key never matches
        lanes = self.p.simul.lanes(u.id)
        rec = SimpleNamespace(utt=u.id, speaker=u.speaker, role=u.role, lang=lang, text=text.strip(), turn=u.turn)
        context = self.p._context()
        for tgt in self.p._targets(rec):  # type: ignore[arg-type]
            if tgt in lanes:
                continue
            req = self.p._request(rec, tgt, context)  # type: ignore[arg-type]
            old = self._specs.get((u.id, tgt))
            if old is not None and old.key == _key(req) and (old.started or old.slot == slot):
                continue  # the same text is being translated already
            if old is not None:
                self._cancel(old)
            spec = _Spec(key=_key(req), speaker=u.speaker, slot=slot, text=text.strip(), seconds=seconds)
            spec.task = self.p._spawn(self._run(spec, req, slot))
            self._specs[(u.id, tgt)] = spec

    def stream(self, utt: str, tgt: str, req: TranslateRequest) -> AsyncIterator[str]:
        """The turn's translation stream: the speculation if it was for exactly this text, else the engine."""
        spec = self._specs.pop((utt, tgt), None)
        if spec is not None and spec.key == _key(req) and spec.started and not spec.failed:
            self.hits += 1
            return self._replay(spec, req)
        if spec is not None:
            self._cancel(spec)
        return self.p.engines.translate.stream(req)

    async def synthesize(self, tts: Any, text: str, lang: str, **kw: Any) -> AsyncIterator[np.ndarray]:
        """The voice of one clause: pre-synthesised audio if this exact clause was made ahead with the same
        voice args (``voice_ref`` / ``rate``, see room.py ``_voice_kwargs``), else the engine."""
        v = self._voices.pop((lang, text), None)
        if v is not None and _same_voice(v.kw, kw):
            await v.done.wait()
            if not v.failed:
                for c in v.chunks:
                    yield c
                return
        elif v is not None and v.task is not None and not v.task.done():
            v.task.cancel()
        async for c in tts.synthesize(text, lang, **kw):
            yield c

    def drop(self, utt: str) -> None:
        for key in [k for k in self._specs if k[0] == utt]:
            self._cancel(self._specs.pop(key))

    # ------------------------------------------------------------ internals

    def _cancel(self, spec: _Spec) -> None:
        spec.failed = True
        if spec.task is not None and not spec.task.done():
            spec.task.cancel()
        v = self._voices.pop(spec.voice, None) if spec.voice else None
        if v is not None and v.task is not None and not v.task.done():
            v.task.cancel()

    async def _run(self, spec: _Spec, req: TranslateRequest, slot: str = "background") -> None:
        from raum.pipeline.room import MT_TIMEOUT_S, take_phrase

        pending = ""

        async def run() -> None:
            nonlocal pending
            async for d in self.p.engines.translate.stream(req):
                if not d:
                    continue
                spec.parts.append(d)
                spec.changed.set()
                if spec.voice is None and self.p._tts_enabled():
                    pending += d
                    # the voice's first phrase, cut as _translate cuts it (same floor for a cloned voice)
                    floor = self.p.min_voice_words(spec.speaker)
                    clause, pending = take_phrase(pending, floor, len(spec.text.split()) if floor else None)
                    if clause:
                        self._presynth(spec, req.tgt_lang, clause)

        try:
            async with self.p.sched.slot(slot):
                spec.started = True
                await asyncio.wait_for(run(), MT_TIMEOUT_S)
            if spec.voice is None and pending.strip() and self.p._tts_enabled():
                self._presynth(spec, req.tgt_lang, pending.strip())  # one clause only: the whole text
        except asyncio.CancelledError:
            spec.failed = True
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("speculative translation failed: %s", e)
            spec.failed = True
        finally:
            spec.done = True
            spec.changed.set()

    async def _replay(self, spec: _Spec, req: TranslateRequest) -> AsyncIterator[str]:
        i = 0
        while True:
            spec.changed.clear()
            while i < len(spec.parts):
                yield spec.parts[i]
                i += 1
            if spec.done:
                if spec.failed:
                    if i == 0:  # nothing came: the live engine after all
                        async for d in self.p.engines.translate.stream(req):
                            yield d
                        return
                    raise RuntimeError("speculative translation stopped midway")
                return
            if i < len(spec.parts):
                continue
            await spec.changed.wait()

    def _presynth(self, spec: _Spec, lang: str, clause: str) -> None:
        tts = self.p.engines.tts
        if tts is None:
            return
        from raum.pipeline.simul import speaker_voice

        spec.voice = (lang, clause)
        v = _Voice(kw=self.p._voice_kwargs(tts, speaker_voice(self.p, spec.speaker, spec.text, spec.seconds)))
        self._voices[spec.voice] = v
        while len(self._voices) > _MAX_VOICES:
            old = self._voices.pop(next(iter(self._voices)))
            if old.task is not None and not old.task.done():
                old.task.cancel()

        async def synth() -> None:
            try:
                async for c in tts.synthesize(clause, lang, **v.kw):
                    v.chunks.append(c)
            except asyncio.CancelledError:
                v.failed = True
                raise
            except Exception:  # noqa: BLE001  the live call reports it
                v.failed = True
            finally:
                v.done.set()

        v.task = self.p._spawn(synth())
