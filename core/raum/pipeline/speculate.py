"""Speculative translation (both modes). Hooked into ``room.py``.

While the speaker is finishing, the latest candidate text is translated in the background, at low priority
(scheduler ``background`` slot): a partial that reads as a finished utterance (``simul.complete``; usually
the partial before the stable one, which is the final), or the early final transcript of a pause while the
semantic end of turn (turn.py) waits out the silence. One speculation per utterance and language; a newer text replaces it. Nothing of it is
ever emitted. When the turn's translation
starts and the final transcript equals that text (whitespace and case normalised; same direction, context
and glossary), the finished (or still streaming) speculative translation is replayed as the live deltas:
MT ~ 0 ms after the endpoint. Otherwise it is discarded.

The voice is made ahead too, for the whole utterance: every clause of a speculative translation, cut exactly as
``room._translate`` cuts the live one (``take_phrase`` first, then ``take_clause``), is synthesised in order
(at most ``SPEC_CLAUSES`` clauses / ``SPEC_AUDIO_S`` of audio) into a cache keyed by (language, clause text
normalised); the audio plays only when a live clause is that text with the same voice args (``_same_voice``),
i.e. once the turn ended. A final that differs only in its tail still plays its unchanged clauses from the cache.
Nothing is published before the end-of-turn decision. A resume (``speech_start`` during the end-of-turn wait)
drops the utterance's speculation and its audio; burn / session end drop every cached audio (the speaker's
cloned voice).

The live voice also goes through this cache: ``prepare`` (room ``_speak_stream``) starts the clause about to
play and, as soon as its synthesis is done, the next one already queued ("ahead"), so clauses play back to back.
Speculative synthesis never slows the live voice: it waits while a live clause is being synthesised in the
room, stops between chunks when one starts, and asks the engine for ``background`` priority if it has one (the
voice service then rejects it instead of queueing it before a live request). Each played clause counts as
``spec`` (made ahead of the end of turn), ``ahead`` (made while the one before played) or ``live``:
``pipeline.timing`` ``voice_clauses``.
Simultaneous lanes (simul.py) are not speculated: their tail is short.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np

from raum.pipeline.base import TranslateRequest

if TYPE_CHECKING:
    from raum.pipeline.room import RoomPipeline, _Utt

log = logging.getLogger("raum.pipeline.speculate")

_MAX_VOICES = 32
SPEC_CLAUSES = 4  # clauses of one speculation synthesised ahead
SPEC_AUDIO_S = 15.0  # ... or this much audio, whichever comes first
_VOICE_TTL_S = 60.0  # an unclaimed cached clause older than this is dropped


def _vkey(lang: str, text: str) -> tuple[str, str]:
    return (lang, " ".join(text.split()).casefold())


def _takes(fn: Any, name: str) -> bool:
    import inspect

    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


async def _either(a: asyncio.Event, b: asyncio.Event) -> None:
    if a.is_set() or b.is_set():
        return
    waits = [asyncio.ensure_future(a.wait()), asyncio.ensure_future(b.wait())]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for w in waits:
            w.cancel()


class _Yield(Exception):
    """A speculative synthesis stops: a live one started in the room."""


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
class _Voice:
    kw: dict[str, Any] = field(default_factory=dict)  # voice_ref / rate it was made with
    chunks: list[np.ndarray] = field(default_factory=list)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    changed: asyncio.Event = field(default_factory=asyncio.Event)  # a chunk arrived (or done)
    failed: bool = False
    error: BaseException | None = None
    task: asyncio.Task[None] | None = None
    kind: str = "spec"  # spec (ahead of the end of turn) | ahead (while the clause before plays) | live
    utt: str | None = None
    claimed: bool = False  # the live path plays it: never stopped for another live clause
    go: asyncio.Event = field(default_factory=asyncio.Event)  # claimed: stop waiting for the room's live voice
    samples: int = 0
    rate: int = 0
    at: float = 0.0


@dataclass
class _Spec:
    key: tuple[Any, ...]
    parts: list[str] = field(default_factory=list)
    changed: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    started: bool = False
    done: bool = False
    failed: bool = False
    voices: list[_Voice] = field(default_factory=list)  # its clauses synthesised ahead, in order
    utt: str = ""
    speaker: str = ""
    slot: str = "background"
    text: str = ""  # the source text, and how long it took to say: the pace for the pre-synthesis
    seconds: float | None = None




class Speculator:
    """Owned by ``RoomPipeline`` (``self.spec``). Hooks: ``on_text``, ``stream``, ``synthesize``, ``drop``."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self._specs: dict[tuple[str, str], _Spec] = {}  # (utt, target lang) -> speculation
        self._voices: dict[tuple[str, str], _Voice] = {}  # (lang, clause normalised) -> audio, made or being made
        self.hits = 0  # replayed speculations (glass box / tests)
        self.voice_hits = 0  # clauses played from speculative audio (glass box / tests)
        self._live = 0  # live syntheses running in this room: speculative ones wait / stop for them
        self._live_idle = asyncio.Event()
        self._live_idle.set()

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
            spec = _Spec(key=_key(req), utt=u.id, speaker=u.speaker, slot=slot, text=text.strip(), seconds=seconds)
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

    async def synthesize(self, tts: Any, text: str, lang: str, *, marks: dict[str, Any] | None = None,
                         **kw: Any) -> AsyncIterator[np.ndarray]:
        """The voice of one live clause: its cached audio (made ahead, or being made) if it was made for this text
        with the same voice args, streamed as it comes; else synthesised now. ``marks`` counts where it came from
        (``voice_spec`` / ``voice_ahead`` / ``voice_live``)."""
        key = _vkey(lang, text)
        v = self._voices.pop(key, None)
        if v is not None and (not _same_voice(v.kw, kw) or (v.done.is_set() and v.failed)):
            self._stop(v)
            v = None
        if v is None:
            v = self._start(tts, text, lang, kw, kind="live")
        v.claimed = True
        v.go.set()
        if marks is not None:
            marks[f"voice_{v.kind}"] = marks.get(f"voice_{v.kind}", 0) + 1
        if v.kind == "spec":
            self.voice_hits += 1
        i = 0
        while True:
            v.changed.clear()
            while i < len(v.chunks):
                yield v.chunks[i]
                i += 1
            if v.done.is_set():
                if not v.failed:
                    return
                if v.kind == "live":
                    raise v.error or RuntimeError("voice failed")
                # made ahead and failed (stopped, rejected): the clause live after all (from its start if some of
                # it played: a repeated beginning is better than a lost dose)
                async for c in self._live_now(tts, text, lang, kw):
                    yield c
                return
            await v.changed.wait()

    def prepare(self, tts: Any, lang: str, text: str, nxt: str | None, kw: dict[str, Any], *,
                utt: str | None = None) -> None:
        """The live voice lane is about to play ``text``: start it now unless it is cached, and the clause
        queued after it (``nxt``) as soon as this one is made, so the second plays right after the first."""
        if tts is None:
            return
        cur = self._cached(lang, text, kw)
        if cur is None:
            cur = self._start(tts, text, lang, kw, kind="live", utt=utt)
            self._voices[_vkey(lang, text)] = cur
        if nxt and nxt.strip() and self._cached(lang, nxt, kw) is None:
            self._voices[_vkey(lang, nxt)] = self._start(tts, nxt, lang, kw, kind="ahead", utt=utt, after=cur)
        self._evict()

    def drop(self, utt: str) -> None:
        for key in [k for k in self._specs if k[0] == utt]:
            self._cancel(self._specs.pop(key))
        for key in [k for k, v in self._voices.items() if v.utt == utt and not v.claimed]:
            self._stop(self._voices.pop(key))

    def clear(self) -> None:
        """Burn / session end / close: every cached audio goes (it may be the speaker's cloned voice)."""
        for spec in self._specs.values():
            self._cancel(spec)
        self._specs.clear()
        for v in self._voices.values():
            self._stop(v)
        self._voices.clear()

    # ------------------------------------------------------------ internals

    def _cancel(self, spec: _Spec) -> None:
        spec.failed = True
        if spec.task is not None and not spec.task.done():
            spec.task.cancel()
        for v in spec.voices:  # what was made stays cached (a newer text may say the same clause); unfinished stops
            if not v.done.is_set() and not v.claimed:
                self._stop(v)

    @staticmethod
    def _stop(v: _Voice) -> None:
        if v.task is not None and not v.task.done() and not v.claimed:
            v.task.cancel()

    def _cached(self, lang: str, text: str, kw: dict[str, Any]) -> _Voice | None:
        v = self._voices.get(_vkey(lang, text))
        if v is None or not _same_voice(v.kw, kw) or (v.done.is_set() and v.failed):
            return None
        return v

    def _evict(self) -> None:
        now = time.monotonic()
        for key in [k for k, v in self._voices.items() if not v.claimed and now - v.at > _VOICE_TTL_S]:
            self._stop(self._voices.pop(key))
        while len(self._voices) > _MAX_VOICES:
            self._stop(self._voices.pop(next(iter(self._voices))))

    def _start(self, tts: Any, text: str, lang: str, kw: dict[str, Any], *, kind: str, utt: str | None = None,
               after: _Voice | None = None, budget: _Spec | None = None) -> _Voice:
        v = _Voice(kw=dict(kw), kind=kind, utt=utt, rate=int(getattr(tts, "sample_rate", 0) or 0),
                   at=time.monotonic())
        v.task = self.p._spawn(self._synth(v, tts, text, lang, after, budget))
        return v

    async def _synth(self, v: _Voice, tts: Any, text: str, lang: str, after: _Voice | None,
                     budget: _Spec | None) -> None:
        background = v.kind == "spec"
        live = False
        try:
            if after is not None:
                await after.done.wait()  # one clause after the other on a lane: no two requests at once
                if after.failed and v.kind == "ahead":
                    raise _Yield("the clause before failed")  # the live call tries again, once, in its turn
            if background:
                await _either(self._live_idle, v.go)  # never beside a live clause, unless a listener waits for it
                if not v.claimed and budget is not None and self._audio_s(budget) >= SPEC_AUDIO_S:
                    raise _Yield("enough audio ahead")
            else:
                live = True
                self._live += 1
                self._live_idle.clear()
            kw = dict(v.kw)
            if background and _takes(tts.synthesize, "background"):
                kw["background"] = True  # the voice service: rejected, never queued before a live request
            async for c in tts.synthesize(text, lang, **kw):
                v.chunks.append(c)
                v.samples += len(c)
                v.changed.set()
                if background and self._live and not v.claimed:
                    raise _Yield("a live clause started")
        except asyncio.CancelledError:
            v.failed = True
            raise
        except Exception as e:  # noqa: BLE001  ahead: the live call makes it; live: _speak reports it
            v.failed, v.error = True, e
            if not isinstance(e, _Yield):
                log.debug("voice %s %s failed: %s: %s", v.kind, lang, e.__class__.__name__, e)
        finally:
            if live:
                self._live -= 1
                if not self._live:
                    self._live_idle.set()
            v.done.set()
            v.changed.set()

    @staticmethod
    def _audio_s(spec: _Spec) -> float:
        return sum(v.samples / v.rate for v in spec.voices if v.rate)

    async def _live_now(self, tts: Any, text: str, lang: str, kw: dict[str, Any]) -> AsyncIterator[np.ndarray]:
        v = self._start(tts, text, lang, kw, kind="live")
        v.claimed = True
        v.go.set()
        i = 0
        while True:
            v.changed.clear()
            while i < len(v.chunks):
                yield v.chunks[i]
                i += 1
            if v.done.is_set():
                if v.failed:
                    raise v.error or RuntimeError("voice failed")
                return
            await v.changed.wait()

    async def _run(self, spec: _Spec, req: TranslateRequest, slot: str = "background") -> None:
        from raum.pipeline.room import MT_TIMEOUT_S, _words, take_clause, take_phrase

        pending = ""
        # the voice's clauses, cut exactly as _translate cuts the live ones (same floor for a cloned voice)
        floor = self.p.min_voice_words(spec.speaker)
        expect = _words(spec.text) if floor else None

        async def run() -> None:
            nonlocal pending
            async for d in self.p.engines.translate.stream(req):
                if not d:
                    continue
                spec.parts.append(d)
                spec.changed.set()
                if self.p._tts_enabled() and len(spec.voices) < SPEC_CLAUSES:
                    pending += d
                    clause, pending = (take_clause(pending, floor, expect) if spec.voices
                                       else take_phrase(pending, floor, expect))
                    if clause:
                        self._presynth(spec, req.tgt_lang, clause)

        try:
            async with self.p.sched.slot(slot):
                spec.started = True
                await asyncio.wait_for(run(), MT_TIMEOUT_S)
            if pending.strip() and self.p._tts_enabled() and len(spec.voices) < SPEC_CLAUSES:
                self._presynth(spec, req.tgt_lang, pending.strip())  # the tail, as _translate says it last
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
                if spec.failed:  # cancelled or failed midway: the live engine after all, from where it stopped
                    async for d in self._rest(req, "".join(spec.parts[:i])):
                        yield d
                return
            if i < len(spec.parts):
                continue
            await spec.changed.wait()

    async def _rest(self, req: TranslateRequest, sent: str) -> AsyncIterator[str]:
        """The live translation of ``req`` without its first ``len(sent)`` characters (already streamed from the
        speculation; the same request at temperature 0 gives the same start). Before: a speculation stopped
        midway ended the turn's translation there ("... Метформин по")."""
        seen = ""
        async for d in self.p.engines.translate.stream(req):
            if len(seen) >= len(sent):
                yield d
                continue
            seen += d
            if len(seen) > len(sent):
                if not seen.startswith(sent):
                    log.info("speculation stopped midway and the live translation starts otherwise: continued")
                yield seen[len(sent):]

    def _presynth(self, spec: _Spec, lang: str, clause: str) -> None:
        """The next clause of a speculation, made after the one before it (background, waits for live ones)."""
        tts = self.p.engines.tts
        if tts is None:
            return
        from raum.pipeline.simul import speaker_voice

        kw = self.p._voice_kwargs(tts, speaker_voice(self.p, spec.speaker, spec.text, spec.seconds))
        v = self._cached(lang, clause, kw)
        if v is None:
            prev = spec.voices[-1] if spec.voices else None
            v = self._start(tts, clause, lang, kw, kind="spec", utt=spec.utt, after=prev, budget=spec)
            self._voices[_vkey(lang, clause)] = v
            self._evict()
        spec.voices.append(v)
