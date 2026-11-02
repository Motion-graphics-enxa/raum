"""Per-room turn pipeline (plan §6.4). Runs inside raum-agent; testable without LiveKit.

CONTRACT (implemented by the pipeline workstream; the worker in raum.agent calls exactly this):

    p = RoomPipeline(room, mode, engines, emit, config)
    p.set_participants([...ParticipantInfo])          # whenever LiveKit membership/metadata changes
    p.set_state(SessionState)                          # from core (off_record etc.)
    utt = await p.speech_start(speaker_id)             # VAD START_OF_SPEECH  -> utterance.start
    await p.speech_audio(speaker_id, frame_16k)        # while speaking; partial ASR every ~1 s
    await p.speech_end(speaker_id, audio_16k, vad_ms)  # VAD END_OF_SPEECH -> utterance.end, final ASR,
                                                       # streamed translation per target language,
                                                       # fact guard, async second-model check, timings
    await p.handle_control(Control)                    # repeat_back, explain_simply, correct_term, burn
    await p.aclose()

``emit`` is ``async def emit(env: Envelope) -> None``: the worker publishes it on the data
channel (topic raum.events) and forwards it to core over the link.

Rules (§6.4): checks never block display; only red interrupts (flag with needs_confirm);
partials at lower priority than finals; off_record drops audio entirely.

Additions beyond the contract (all optional):

- ``await p.drain()`` waits for background work (second check, voice, timings); tests use it.
- ``p.tts_sink = async fn(lang, pcm_int16, sample_rate)`` receives voice output (the worker
  publishes it as track ``tts-<lang>``).
- ``p.denoise_ms[speaker] = ms`` lets the worker report its cleaning time in ``pipeline.timing``.
- ``speech_end`` detaches the utterance before its first ``await``, so the worker may run it as a
  task and keep reading VAD events; one ``await asyncio.sleep(0)`` after ``create_task`` suffices.

Timing (``pipeline.timing``, one per utterance and target language, after the check): ``vad_ms``
is the endpoint delay; ``asr_ms`` the final transcription; ``mt_first_ms``, ``mt_full_ms`` and
``tts_first_ms`` are measured from the moment the speaker stopped (= endpoint - vad_ms), the
numbers of the §3 table; ``check_ms`` is translation.final -> verify.result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, get_args

import numpy as np

from raum.config import RaumConfig
from raum.events import (
    Control,
    Envelope,
    FlagKind,
    ParticipantInfo,
    SessionState,
    make_event,
    now_ms,
)
from raum.glossary import Glossary
from raum.modes.schema import ModeConfig
from raum.pipeline import texts as T
from raum.pipeline._util import int16_to_float, lang_code, lang_name, load_prompt, render_prompt
from raum.pipeline.base import Engines, TranslateRequest, TurnContext
from raum.scheduler import Scheduler
from raum.verify import guard
from raum.verify.backtranslate import CheckResult, second_check

Emit = Callable[[Envelope], Awaitable[None]]
TtsSink = Callable[[str, np.ndarray, int], Awaitable[None]]

log = logging.getLogger("raum.pipeline.room")

SAMPLE_RATE = 16000
ASR_TIMEOUT_S = 15.0
PARTIAL_TIMEOUT_S = 5.0
MT_TIMEOUT_S = 30.0
CHECK_TIMEOUT_S = 20.0
_FLAG_KINDS = set(get_args(FlagKind))

_NAME_PATTERNS = [
    re.compile(r"\b(?:Herr|Frau|Dr\.|Prof\.|Mr\.?|Mrs\.?|Ms\.?|Rechtsanwältin|Rechtsanwalt|Bay|Bayan)"
               r"(?:\s+Dr\.)?\s+[A-ZÄÖÜÇĞİŞ][\w'\-]+"),
    re.compile(r"\b[A-ZÇĞİÖŞÜ][\wçğıöşü]+\s+(?:Hanım|Bey)\b"),
]
_DIGITS = re.compile(r"\d+(?:[.,]\d+)*")

_guard_warned = False


def _guard_call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, bool]:
    """Call the fact guard; ``(None, False)`` if it is not implemented yet or fails (logged once)."""
    global _guard_warned
    try:
        return fn(*args, **kwargs), True
    except NotImplementedError:
        if not _guard_warned:
            log.warning("fact guard not implemented yet: continuing without facts")
            _guard_warned = True
        return None, False
    except Exception:
        log.exception("fact guard failed: continuing without facts")
        return None, False


def _role_from_identity(identity: str) -> str:
    head = identity.split("-", 1)[0]
    return head if head in ("a", "b") else "b"


@dataclass
class _Utt:
    id: str
    speaker: str
    role: str
    lang: str  # for events
    hint: str | None  # for ASR
    turn: TurnContext
    frames: list[np.ndarray] = field(default_factory=list)
    samples: int = 0
    seq: int = 0
    next_partial_ms: float = 0.0
    partial_task: asyncio.Task[None] | None = None
    closed: bool = False

    def audio(self) -> np.ndarray:
        return np.concatenate(self.frames) if self.frames else np.zeros(0, dtype=np.float32)


@dataclass
class _Turn:
    utt: str
    speaker: str
    role: str
    lang: str
    text: str
    conf: float
    turn: TurnContext
    ts: int = field(default_factory=now_ms)
    translations: dict[str, str] = field(default_factory=dict)


_CLAUSE_END = re.compile(r"[.!?;:…。！？،؛؟](?=\s)|,(?=\s)")
_ABBREV = re.compile(r"(?:\b(?:Dr|Fr|Hr|Prof|Nr|ca|bzw|z\.B|u\.a|St|Mr|Mrs|Ms|No|vs)\.|\d\.)$", re.IGNORECASE)
MIN_CLAUSE_CHARS = 16


_QUESTION_END = re.compile(r"[?？؟]\s*$")


def _is_question(text: str) -> bool:
    return bool(_QUESTION_END.search(text or ""))


def take_clause(buf: str) -> tuple[str, str]:
    """Split off the longest speakable prefix that ends at a clause boundary (sentence end, or a comma
    once the clause is long enough). Ordinals and abbreviations ("12.", "Dr.") never end a clause."""
    cut = -1
    for m in _CLAUSE_END.finditer(buf):
        end = m.end()
        head = buf[:end]
        if len(head.strip()) < MIN_CLAUSE_CHARS:
            continue
        if m.group(0) == "," and len(head.strip()) < 2 * MIN_CLAUSE_CHARS:
            continue
        if m.group(0) == "." and _ABBREV.search(head):
            continue
        cut = end
    if cut < 0:
        return "", buf
    return buf[:cut].strip(), buf[cut:]


class RoomPipeline:
    def __init__(self, room: str, mode: ModeConfig, engines: Engines, emit: Emit, config: RaumConfig) -> None:
        self.room = room
        self.mode = mode
        self.engines = engines
        self._emit = emit
        self.cfg = config
        self.glossary = self._load_glossary()
        self.participants: dict[str, ParticipantInfo] = {}
        self.state: SessionState | None = None
        self.off_record = False
        self.partial_every_ms = float(config.models.asr.options.get("partial_every_ms", 1000))
        self.tts_sink: TtsSink | None = None
        self.denoise_ms: dict[str, float] = {}
        self.scheduler: Scheduler | None = None
        self._utt_n = 0
        self._turn_index = 0
        self._active: dict[str, _Utt] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._turns: deque[_Turn] = deque(maxlen=64)  # conversation memory (V-04), cleared on burn
        self._names: list[str] = []
        self._last_instruction: _Turn | None = None
        self._repeat_back = False
        self._drafts: dict[str, tuple[str, str]] = {}  # utt -> (target_lang, text)
        self._tts_disabled = False
        self._closed = False

    # ---------------------------------------------------------------- setup

    def _load_glossary(self) -> Glossary:
        try:
            return Glossary.load(self.mode.glossary)
        except Exception:
            log.exception("glossary %s failed to load", self.mode.glossary)
            return Glossary()

    @property
    def sched(self) -> Scheduler:
        if self.scheduler is None:
            self.scheduler = Scheduler.shared(self.cfg)
        return self.scheduler

    def _spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("pipeline task failed", exc_info=task.exception())

    async def emit(self, type: str, data: Any, utt: str | None = None) -> Envelope | None:
        try:
            env = make_event(type, self.room, data, utt=utt)
        except Exception:
            log.exception("invalid %s event", type)
            return None
        try:
            await self._emit(env)
        except Exception:
            log.exception("emit %s failed", type)
        return env

    # ---------------------------------------------------------------- membership and state

    def set_participants(self, participants: list[ParticipantInfo]) -> None:
        self.participants = {p.id: p for p in participants}

    def set_state(self, state: SessionState) -> None:
        self.state = state
        self._set_off_record(bool(state.off_record))

    def _set_off_record(self, on: bool) -> None:
        if on and not self.off_record:
            try:
                asyncio.get_running_loop()
                running = True
            except RuntimeError:
                running = False
            for u in list(self._active.values()):
                self._drop(u)
                if running:  # close the bubble on the devices; no content
                    self._spawn(self.emit("utterance.end", {"speaker": u.speaker, "lang": u.lang}, utt=u.id))
            self._active.clear()
        self.off_record = on

    def _drop(self, u: _Utt) -> None:
        u.closed = True
        u.frames.clear()
        if u.partial_task and not u.partial_task.done():
            u.partial_task.cancel()

    def _participant_lang(self, p: ParticipantInfo | None) -> str | None:
        code = lang_code(p.lang) if p else None
        return None if code in (None, "auto", "und") else code

    def _role_lang(self, role: str) -> str | None:
        for p in self.participants.values():
            if p.role == role:
                code = self._participant_lang(p)
                if code:
                    return code
        return None

    def _default_lang(self, role: str) -> str:
        rc = self.mode.roles.get(role) if role in ("a", "b") else None  # type: ignore[call-overload]
        code = lang_code(rc.default_lang) if rc else None
        if code and code != "auto":
            return code
        return "de" if role == "a" else "en"

    # ---------------------------------------------------------------- speech

    async def speech_start(self, speaker: str) -> str | None:
        if self.off_record or self._closed:
            return None
        u = self._active.get(speaker)
        if u is not None:
            return u.id
        p = self.participants.get(speaker)
        role = p.role if p else _role_from_identity(speaker)
        hint = self._participant_lang(p)
        lang = hint or (self._default_lang(role) if role == "a" else "und")
        self._utt_n += 1
        uid = f"u-{self._utt_n:04d}"
        turn = TurnContext(room=self.room, mode=self.mode.id, speaker=speaker, role=role, utt=uid,
                           speech_ms=0.0, turn_index=self._turn_index)
        self._turn_index += 1
        u = _Utt(id=uid, speaker=speaker, role=role, lang=lang, hint=hint, turn=turn,
                 next_partial_ms=self.partial_every_ms)
        self._active[speaker] = u
        await self.emit("utterance.start", {"speaker": speaker, "lang": lang}, utt=uid)
        return uid

    async def speech_audio(self, speaker: str, frame: np.ndarray) -> None:
        if self.off_record:
            return
        u = self._active.get(speaker)
        if u is None or u.closed:
            return
        f = np.asarray(frame)
        f = int16_to_float(f) if f.dtype == np.int16 else f.astype(np.float32, copy=False).reshape(-1)
        u.frames.append(f)
        u.samples += len(f)
        speech_ms = u.samples * 1000.0 / SAMPLE_RATE
        if speech_ms >= u.next_partial_ms and (u.partial_task is None or u.partial_task.done()):
            u.next_partial_ms = speech_ms + self.partial_every_ms
            u.partial_task = self._spawn(self._partial(u))

    async def _partial(self, u: _Utt) -> None:
        audio = u.audio()
        turn = replace(u.turn, speech_ms=len(audio) * 1000.0 / SAMPLE_RATE)
        try:
            async with self.sched.slot("background"):
                if u.closed:
                    return
                res = await asyncio.wait_for(
                    self.engines.asr.transcribe(audio, lang_hint=u.hint, partial=True, turn=turn), PARTIAL_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.debug("partial asr failed: %s", e)
            return
        if u.closed or self.off_record or not res.text.strip():
            return
        u.seq += 1
        await self.emit("transcript.partial", {"speaker": u.speaker, "lang": lang_code(res.lang) or u.lang,
                                               "text": res.text.strip(), "seq": u.seq}, utt=u.id)

    async def speech_end(self, speaker: str, audio: np.ndarray | None, vad_ms: float) -> None:
        # detach synchronously (before any await): a new speech_start for this speaker starts a new utterance
        t_detect = time.monotonic()
        u = self._active.pop(speaker, None)
        if self.off_record or self._closed:
            if u:
                self._drop(u)
            return
        if u is None:
            if audio is None or len(audio) == 0:
                return
            if await self.speech_start(speaker) is None:
                return
            u = self._active.pop(speaker, None)
            if u is None:
                return
        u.closed = True
        if u.partial_task and not u.partial_task.done():
            u.partial_task.cancel()
        if audio is not None and len(audio):
            a = np.asarray(audio)
            final_audio = int16_to_float(a) if a.dtype == np.int16 else a.astype(np.float32, copy=False).reshape(-1)
        else:
            final_audio = u.audio()
        u.frames.clear()
        t0 = t_detect - max(0.0, float(vad_ms)) / 1000.0  # the moment the speaker stopped
        await self._turn(u, final_audio, float(vad_ms), t0)

    # ---------------------------------------------------------------- the live path

    async def _turn(self, u: _Utt, audio: np.ndarray, vad_ms: float, t0: float) -> None:
        await self.emit("utterance.end", {"speaker": u.speaker, "lang": u.lang}, utt=u.id)
        turn = replace(u.turn, speech_ms=len(audio) * 1000.0 / SAMPLE_RATE)
        timing: dict[str, Any] = {"vad_ms": round(vad_ms, 1), "models": dict(self.engines.names)}
        if u.speaker in self.denoise_ms:
            timing["denoise_ms"] = round(self.denoise_ms[u.speaker], 2)

        t_asr = time.monotonic()
        try:
            async with self.sched.slot("live"):
                res = await asyncio.wait_for(
                    self.engines.asr.transcribe(audio, lang_hint=u.hint, partial=False, turn=turn), ASR_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("final asr failed for %s: %s: %s", u.id, e.__class__.__name__, e)
            await self._say_again(u, u.lang if u.lang != "und" else None)
            return
        timing["asr_ms"] = round((time.monotonic() - t_asr) * 1000.0, 1)
        src_lang = lang_code(res.lang) or u.hint or self._default_lang(u.role)
        text = res.text.strip()
        await self.emit("transcript.final", {"speaker": u.speaker, "lang": src_lang, "text": text,
                                             "asr_conf": round(float(res.conf), 3)}, utt=u.id)
        if not text:
            return
        if res.conf < self.cfg.asr_min_conf:  # V-03: not translated
            await self._say_again(u, src_lang)
            await self.emit("pipeline.timing", timing, utt=u.id)
            return

        rec = _Turn(utt=u.id, speaker=u.speaker, role=u.role, lang=src_lang, text=text, conf=res.conf, turn=turn)
        context = self._context()  # names from earlier turns, before this one is remembered
        self._remember(rec)
        if self._repeat_back and rec.role == "b" and self._last_instruction is not None:
            self._repeat_back = False
            self._spawn(self._repeat_back_check(self._last_instruction, rec))
        if rec.role == "a":
            self._last_instruction = rec

        targets = self._targets(rec)
        await asyncio.gather(*(self._translate(u, rec, tgt, t0, dict(timing), context) for tgt in targets))
        if "offer_tracker" in self.mode.buttons:
            await self._offers(rec)

    def _targets(self, rec: _Turn) -> list[str]:
        """Every other participant's language (deduped, minus the source language). No listener yet:
        the other role's default language."""
        langs: list[str] = []
        listeners = [p for p in self.participants.values() if p.id != rec.speaker and p.role != "observer"]
        for p in listeners:
            code = self._participant_lang(p)
            if code and code != rec.lang and code not in langs:
                langs.append(code)
        if not listeners:
            other = "b" if rec.role == "a" else "a"
            code = self._default_lang(other)
            if code != rec.lang:
                langs.append(code)
        return langs

    def _listener_role(self, tgt: str, speaker_role: str) -> str:
        roles = [p.role for p in self.participants.values()
                 if p.role in ("a", "b") and self._participant_lang(p) == tgt]
        other = "b" if speaker_role == "a" else "a"
        if other in roles or not roles:
            return other
        return roles[0]

    def _request(self, rec: _Turn, tgt: str, context: str) -> TranslateRequest:
        lang_a = self._role_lang("a") or (rec.lang if rec.role == "a" else tgt)
        lang_b = self._role_lang("b") or (rec.lang if rec.role == "b" else tgt)
        return TranslateRequest(
            text=rec.text, src_lang=rec.lang, tgt_lang=tgt,
            room_name=self.mode.display_name("en"), role_a=self.mode.label("a", "en"),
            role_b=self.mode.label("b", "en"), lang_a=lang_a, lang_b=lang_b, register=self.mode.register,
            glossary=self.glossary.prompt_lines(rec.lang, tgt), context=context, turn=rec.turn,
        )

    async def _translate(self, u: _Utt, rec: _Turn, tgt: str, t0: float, timing: dict[str, Any],
                         context: str) -> None:
        req = self._request(rec, tgt, context)
        parts: list[str] = []
        first_ms: float | None = None
        # C-13: voice starts on the first clause while the rest of the sentence still streams
        voice_q: asyncio.Queue[str | None] | None = asyncio.Queue() if self._tts_enabled() else None
        tts_task = self._spawn(self._speak_stream(tgt, voice_q, t0)) if voice_q is not None else None
        pending = ""

        async def run() -> None:
            nonlocal first_ms, pending
            async for delta in self.engines.translate.stream(req):
                if not delta:
                    continue
                if first_ms is None:
                    first_ms = (time.monotonic() - t0) * 1000.0
                parts.append(delta)
                await self.emit("translation.delta", {"target_lang": tgt, "delta": delta, "seq": len(parts)},
                                utt=u.id)
                if voice_q is not None:
                    pending += delta
                    clause, pending = take_clause(pending)
                    if clause:
                        voice_q.put_nowait(clause)

        try:
            async with self.sched.slot("live"):
                await asyncio.wait_for(run(), MT_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("translation %s -> %s failed for %s: %s: %s", rec.lang, tgt, u.id, e.__class__.__name__, e)
            if not parts:
                if voice_q is not None:
                    voice_q.put_nowait(None)
                await self.emit("pipeline.timing", {**timing, "target_lang": tgt}, utt=u.id)
                return
        if voice_q is not None:
            if pending.strip():
                voice_q.put_nowait(pending.strip())
            voice_q.put_nowait(None)
        text = "".join(parts).strip()
        timing["mt_first_ms"] = round(first_ms, 1) if first_ms is not None else None
        timing["mt_full_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        await self.emit("translation.final", {"target_lang": tgt, "text": text}, utt=u.id)
        rec.translations[tgt] = text
        t_final = time.monotonic()

        # fact guard: code, < 5 ms, on the live path
        res, ok = _guard_call(guard.check, rec.text, rec.lang, text, tgt, kinds=self.mode.guard.kinds,
                              confirm_on=self.mode.guard.confirm_on, glossary=self.glossary)
        facts: dict[str, Any] = {"target_lang": tgt, "facts": [], "red": False}
        if res is not None:
            facts.update(facts=[f.model_dump() for f in res.facts], red=bool(res.red), ms=round(res.ms, 2))
        else:
            facts["error"] = "fact guard unavailable"
        await self.emit("verify.facts", facts, utt=u.id)
        flagged = await self._fact_flags(rec, tgt, res)

        # never blocks the live path: second check + voice, then the timing line
        self._spawn(self._after_final(rec, tgt, text, res, ok, flagged, t0, t_final, timing, tts_task))

    async def _after_final(self, rec: _Turn, tgt: str, text: str, gres: guard.GuardResult | None, guard_ok: bool,
                           flagged: bool, t0: float, t_final: float, timing: dict[str, Any],
                           tts_task: asyncio.Task[float | None] | None = None) -> None:
        check_ms: float | None = None
        try:
            check_ms = await self._check(rec, tgt, text, gres, guard_ok, flagged, t_final)
        finally:
            tts_first = await tts_task if tts_task else None
            timing.update(target_lang=tgt, check_ms=check_ms, tts_first_ms=tts_first)
            await self.emit("pipeline.timing", timing, utt=rec.utt)

    async def _check(self, rec: _Turn, tgt: str, text: str, gres: guard.GuardResult | None, guard_ok: bool,
                     flagged: bool, t_final: float) -> float:
        verifier = self.engines.verify

        async def run() -> CheckResult:
            return await second_check(rec.text, rec.lang, text, tgt, verifier=verifier, embedder=self.engines.embed,
                                      thresholds=self.cfg.thresholds, guard=gres, guard_available=guard_ok,
                                      turn=rec.turn, timeout_s=CHECK_TIMEOUT_S)

        if verifier is None:  # guard only: no model, no slot
            result = await run()
        else:
            async with self.sched.slot("check"):
                result = await run()
        check_ms = round((time.monotonic() - t_final) * 1000.0, 1)
        await self.emit("verify.result", {"target_lang": tgt, "similarity": result.similarity, "badge": result.badge,
                                          "reasons": result.reasons, "back_translation": result.back_translation},
                        utt=rec.utt)
        if result.badge == "red" and not flagged:  # only red interrupts
            texts = T.all_langs(T.UNSURE, tgt)
            await self.emit("flag", {"kind": "mismatch", "text": T.pick(texts, tgt), "needs_confirm": True,
                                     "id": f"{rec.utt}-{tgt}-check",
                                     "confirm_by": self._listener_role(tgt, rec.role), "target_lang": tgt,
                                     "texts": texts}, utt=rec.utt)
        return check_ms

    def _tts_enabled(self) -> bool:
        return self.engines.tts is not None and not self._tts_disabled

    async def _speak_stream(self, tgt: str, q: asyncio.Queue[str | None], t0: float) -> float | None:
        """Speak clauses in order as they arrive; returns ms from the speaker's stop to the first audio."""
        first: float | None = None
        while True:
            clause = await q.get()
            if clause is None:
                return first
            if self._tts_disabled:
                continue
            f = await self._speak(tgt, clause, t0)
            if first is None:
                first = f

    async def _speak(self, tgt: str, text: str, t0: float) -> float | None:
        tts = self.engines.tts
        if tts is None or not text:
            return None
        first: float | None = None
        try:
            async for chunk in tts.synthesize(text, tgt):
                if first is None:
                    first = round((time.monotonic() - t0) * 1000.0, 1)
                if self.tts_sink is not None and not self.off_record:
                    await self.tts_sink(tgt, chunk, tts.sample_rate)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  §6.15: text continues, voice off for the session
            log.warning("voice output failed (%s: %s): disabled for this session", e.__class__.__name__, e)
            self._tts_disabled = True
            return None
        if first is None and getattr(tts, "simulated", False):
            first = round((time.monotonic() - t0) * 1000.0, 1)
        return first

    # ---------------------------------------------------------------- flags

    async def _say_again(self, u: _Utt, lang: str | None) -> None:
        texts = T.all_langs(T.SAY_AGAIN, lang)
        await self.emit("flag", {"kind": "uncertain", "text": T.pick(texts, lang), "needs_confirm": False,
                                 "id": f"{u.id}-uncertain", "target_lang": lang, "texts": texts}, utt=u.id)

    async def _fact_flags(self, rec: _Turn, tgt: str, res: guard.GuardResult | None) -> bool:
        """Confirm prompt on the listener's device: red mismatches, and ``confirm_on`` facts (§6.8.4)."""
        if res is None:
            return False
        if res.red:
            facts = [f for f in res.facts if not f.match]
            kind = "mismatch"
            template = T.MISMATCH
        elif res.confirm and not _is_question(rec.text):
            # a question ("Are you allergic to anything?") states nothing to confirm; and a confirm_on
            # fact without a concrete value (allergy "present") is not worth an interruption
            facts = [f for f in res.confirm if (f.src or "").strip().lower() not in ("", "present")]
            if not facts:
                return False
            if any(f.kind == "dose" for f in facts):
                facts += [f for f in res.facts if f.kind == "frequency" and f not in facts]
            kind = next((f.kind for f in facts if f.kind in _FLAG_KINDS), "number")
            template = None
        else:
            return False
        texts: dict[str, str] = {}
        for lang in dict.fromkeys([*T.LANGS, tgt]):
            joined = ", ".join(T.fact_text(f.kind, f.src or f.tgt, lang) for f in facts)
            texts[lang] = (T.pick(template, lang).format(facts=joined) if template
                           else f"{joined}. {T.pick(T.CORRECT, lang)}")
        await self.emit("flag", {"kind": kind, "text": texts[tgt], "needs_confirm": True,
                                 "id": f"{rec.utt}-{tgt}-{'mismatch' if res.red else 'confirm'}",
                                 "confirm_by": self._listener_role(tgt, rec.role), "target_lang": tgt,
                                 "texts": texts}, utt=rec.utt)
        return True

    # ---------------------------------------------------------------- memory (V-04)

    def _remember(self, rec: _Turn) -> None:
        self._turns.append(rec)
        found: list[str] = []
        for pat in _NAME_PATTERNS:
            found += [m.group(0).strip() for m in pat.finditer(rec.text)]
        facts, _ = _guard_call(guard.extract, rec.text, rec.lang, kinds=["name"], glossary=self.glossary)
        found += [f.surface for f in facts or [] if getattr(f, "kind", "") == "name"]
        for name in found:
            if name and name not in self._names:
                self._names.append(name)
        del self._names[:-12]

    def _context(self) -> str:
        return ("Names: " + ", ".join(self._names)) if self._names else "(none)"

    def _find_turn(self, utt: str | None) -> _Turn | None:
        for rec in reversed(self._turns):
            if utt is None or rec.utt == utt:
                return rec
        return None

    # ---------------------------------------------------------------- offers (N-01)

    async def _offers(self, rec: _Turn) -> None:
        facts, ok = _guard_call(guard.extract, rec.text, rec.lang, kinds=["amount", "currency"], glossary=self.glossary)
        if not ok or not facts:
            return
        currency = next((f.value.strip().upper() for f in facts if f.kind == "currency"), None)
        for f in facts:
            if f.kind != "amount":
                continue
            m = re.match(r"^\s*(-?\d+(?:\.\d+)?)\s*([A-Za-z€$£]{1,3})?\s*$", f.value)
            if not m:
                continue
            cur = (m.group(2) or currency or "EUR").upper().replace("€", "EUR").replace("$", "USD").replace("£", "GBP")
            await self.emit("offer.update", {"amount": float(m.group(1)), "currency": cur, "by": rec.role,
                                             "ts": rec.ts, "text": f.surface or rec.text}, utt=rec.utt)

    # ---------------------------------------------------------------- repeat-back (S-02)

    async def _repeat_back_check(self, inst: _Turn, rest: _Turn) -> None:
        verifier = self.engines.verify
        result: dict[str, Any] | None = None
        if verifier is not None and not getattr(verifier, "is_mock", False):
            prompt = render_prompt(load_prompt(self.mode.prompts.repeat_back or "prompts/repeat-back.md"),
                                   lang_a=lang_name(inst.lang), lang_b=lang_name(rest.lang),
                                   instruction=inst.text, restatement=rest.text)
            try:
                async with self.sched.slot("check"):
                    j = await asyncio.wait_for(verifier.complete_json(prompt, purpose="repeat_back"), CHECK_TIMEOUT_S)
                understood = str(j.get("understood", "partly")).lower()
                result = {"understood": understood if understood in ("yes", "partly", "no") else "partly",
                          "missing": [str(x) for x in j.get("missing") or []],
                          "wrong": [str(x) for x in j.get("wrong") or []]}
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("repeat-back model failed (%s): comparing facts", e.__class__.__name__)
        if result is None:
            if verifier is not None and getattr(verifier, "is_mock", False):
                await verifier.complete_text("", purpose="repeat_back")  # mock latency only
            result = self._compare_facts(inst, rest)
        result["instruction_utt"] = inst.utt
        await self.emit("repeat_back.result", result, utt=rest.utt)

    def _compare_facts(self, inst: _Turn, rest: _Turn) -> dict[str, Any]:
        kinds = self.mode.guard.kinds
        a, ok_a = _guard_call(guard.extract, inst.text, inst.lang, kinds=kinds, glossary=self.glossary)
        b, ok_b = _guard_call(guard.extract, rest.text, rest.lang, kinds=kinds, glossary=self.glossary)
        if ok_a and ok_b:
            want = Counter((f.kind, f.value) for f in a)
            got = Counter((f.kind, f.value) for f in b)
            surface = {(f.kind, f.value): f.surface for f in a}
            missing = [surface.get(k) or k[1] for k in (want - got).elements()]
            want_kinds = {k for k, _ in want}
            wrong = [f.surface or f.value for f in b if f.kind in want_kinds and (f.kind, f.value) not in want]
            total = sum(want.values())
        else:  # guard not available yet: compare the numbers only
            want_n = Counter(_DIGITS.findall(inst.text))
            got_n = Counter(_DIGITS.findall(rest.text))
            missing = list((want_n - got_n).elements())
            wrong = []
            total = sum(want_n.values())
        if not missing and not wrong:
            understood = "yes"
        elif total and len(missing) >= total:
            understood = "no"
        else:
            understood = "partly"
        return {"understood": understood, "missing": missing, "wrong": wrong}

    # ---------------------------------------------------------------- controls

    async def handle_control(self, control: Control) -> None:
        action = control.action
        try:
            if action == "off_record":
                self._set_off_record(bool(control.value))
            elif action == "repeat_back":
                self._repeat_back = True
            elif action == "explain_simply":
                await self._explain(control)
            elif action == "explain_approve":
                await self._explain_approve(control)
            elif action == "correct_term":
                self._correct_term(control.value)
            elif action == "burn":
                self._burn()
            # confirm, human_interpreter, end, approve_summary, mute: handled by core / the devices
        except Exception:
            log.exception("control %s failed", action)

    def _explain_target(self, control: Control) -> str:
        v = control.value
        if isinstance(v, dict) and v.get("lang"):
            return str(lang_code(str(v["lang"])))
        if isinstance(v, str) and v:
            return str(lang_code(v))
        return self._role_lang("b") or "en"

    async def _explain(self, control: Control) -> None:
        rec = self._find_turn(control.utt) if control.utt else next(
            (r for r in reversed(self._turns) if r.role == "a"), None)
        if rec is None:
            log.info("explain_simply: utterance %s not found", control.utt)
            return
        tgt = self._explain_target(control)
        verifier = self.engines.verify
        simple = getattr(verifier, "simple_version", None)
        if callable(simple):
            text = await simple(rec.text, rec.lang, tgt, self.mode.id)
        elif verifier is not None:
            prompt = render_prompt(load_prompt(self.mode.prompts.explain_simply or "prompts/explain-simply.md"),
                                   lang_a=lang_name(rec.lang), lang_b=lang_name(tgt), text=rec.text)
            async with self.sched.slot("background"):
                text = await asyncio.wait_for(verifier.complete_text(prompt, purpose="explain_simply"),
                                              CHECK_TIMEOUT_S)
        else:
            text = rec.translations.get(tgt, "")
        self._drafts[rec.utt] = (tgt, text)
        await self.emit("explain.draft", {"source_utt": rec.utt, "target_lang": tgt, "text": text}, utt=rec.utt)

    async def _explain_approve(self, control: Control) -> None:
        utt = control.utt
        draft = self._drafts.pop(utt, None) if utt else None
        if draft is None and not control.text:
            log.info("explain_approve: no draft for %s", utt)
            return
        tgt = draft[0] if draft else self._explain_target(control)
        text = control.text or (draft[1] if draft else "")
        await self.emit("explain.sent", {"source_utt": utt or "", "target_lang": tgt, "text": text}, utt=utt)

    def _correct_term(self, value: Any) -> None:
        if not isinstance(value, dict) or not value.get("src") or not value.get("tgt") or not value.get("lang"):
            log.info("correct_term: expected {src, tgt, lang}, got %r", value)
            return
        src_lang = str(value.get("src_lang") or self._default_lang("a"))
        self.glossary.add_correction(str(value["src"]), str(value["tgt"]), str(value["lang"]), src_lang=src_lang)

    def _burn(self) -> None:
        """Forget the conversation: memory, names, drafts, session glossary corrections."""
        for u in list(self._active.values()):
            self._drop(u)
        self._active.clear()
        self._turns.clear()
        self._names.clear()
        self._last_instruction = None
        self._repeat_back = False
        self._drafts.clear()
        self.glossary = self._load_glossary()
        reset = getattr(self.engines.asr, "reset", None)
        if callable(reset):
            reset(self.room)

    # ---------------------------------------------------------------- lifecycle

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until all background work (checks, voice, timings) has finished."""
        async def _wait() -> None:
            while True:
                pending = [t for t in self._tasks if not t.done()]
                if not pending:
                    break
                await asyncio.wait(pending)
            await asyncio.sleep(0)  # let done-callbacks run

        await asyncio.wait_for(_wait(), timeout)

    async def aclose(self) -> None:
        self._closed = True
        for u in list(self._active.values()):
            self._drop(u)
        self._active.clear()
        tasks = list(self._tasks)
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ---------------------------------------------------------------- introspection

    @property
    def names(self) -> list[str]:
        return list(self._names)

    def memory(self) -> list[dict[str, Any]]:
        return [{"utt": r.utt, "role": r.role, "lang": r.lang, "text": r.text} for r in self._turns]
