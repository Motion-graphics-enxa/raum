"""Voice echo check (P1 safety): the box listens to its own voice say the figures of a clause.

Why: a voice-cloning service can say another figure than the text. In the GX10 TTS bake-off, Chatterbox
said "50 mg" for "500 mg" in a Russian clause, after the translation and the fact guard were already right:
the error was in the spoken audio only.

``tts.options.echo_check``: ``facts`` (the GX10 profile) or ``off`` (the default, and the mock profile). Never
with a mock or simulated voice (no audio) or a mock ASR (canned lines).

- A clause the room's voice speaks (room.py ``_speak``: consecutive ``_speak_stream`` and simultaneous
  ``simul._voice_worker``) whose text has figures of the kinds dose, number, date, time (the fact guard's
  ``extract``; amounts, percentages and durations count as their number) is collected as its PCM goes to the
  track. Once synthesized, it is resampled to the ASR's 16 kHz float input and transcribed in the target
  language by the room's ASR on the scheduler's **background** slot (``sched.slot("background")``, like the
  partials): it never starts while live work waits for a slot, so a final ASR never queues behind it. It runs
  beside the playback, never in front of it.
- The fact guard compares the clause with what was heard (``guard.check`` with the same language on both
  sides, so its normalisation applies: "500 миллиграмм" = "500 мг", "five hundred milligrams" = "500 mg").
  A figure of the clause heard with another value is a **mismatch**; a figure not heard, or only in part, is
  **unclear** (``pipeline.timing`` only: no event, no repeat).
- Mismatch: one ``voice.echo`` event per figure ``{target_lang, kind, spoken, written, clause, text}`` (the
  listener's card notes "spoken: 50 mg · written: 500 mg"; core writes the audit line ``voice.echo_mismatch``
  with the kind only), and the clause is said ONCE more in the service's standard voice (no voice sample),
  after a short "I repeat:" (texts.py ``REPEAT``), through the language's voice lane (one writer per track) and
  echo control (C-14): right before the utterance's next clause, at the end of its voice, or else as soon as
  the lane is free (after an utterance already waiting for it). A repeat is never checked again.
- ``pipeline.timing`` of the utterance and language gains ``echo_ms`` (the slowest checked clause:
  synthesized -> heard and compared) and ``echo_check`` (``match`` | ``unclear`` | ``mismatch`` | ``skipped``).
- ASR busy (no background slot within ``ECHO_WAIT_S``), slow (``ECHO_TIMEOUT_S``) or failing: the clause is
  not checked (debug log); the voice never waits for any of it.

What counts as "another value" is strict, because a false repeat costs trust: the heard value must be written
in digits by the ASR, or in English / German words standing alone (the guard reads their compounds in full;
elsewhere it reads 0-20 and tens only, so "двадцать пять" would read as "пять"), and a date heard with less
than was written (a month without its day, a weekday for a date) is unclear.

PRIVACY: logs carry languages, fact kinds and counts, never a figure or the clause. The PCM stays in memory
until the check is done.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from raum.pipeline import stats, voicegate
from raum.pipeline import texts as T
from raum.pipeline._util import int16_to_float
from raum.pipeline.base import TurnContext
from raum.pipeline.voice import VoiceArgs
from raum.verify import guard

if TYPE_CHECKING:
    from raum.glossary import Glossary
    from raum.pipeline.room import RoomPipeline

log = logging.getLogger("raum.pipeline.echo_check")

ECHO_KINDS = ("dose", "number", "date", "time")
ASR_RATE = 16000
ECHO_WAIT_S = 3.0  # longest wait for the background slot; then the clause is not checked
ECHO_TIMEOUT_S = 5.0  # the transcription itself (whisper.cpp /inference: ~250 ms on the GX10)
SETTLE_S = ECHO_WAIT_S + ECHO_TIMEOUT_S + 0.5  # the timing line waits this long at most for the checks
MAX_CLAUSE_S = 30.0  # a longer clause is not checked (memory)
MAX_GROUPS = 64  # utterance x language voices tracked at once (stale ones go first)
ECHO_SPEAKER = "raum-voice"  # TurnContext.speaker of an echo transcription
WORD_LANGS = frozenset({"en", "de"})  # the guard reads their number words in full (compounds up to 999 999)
_DIGIT = re.compile(r"\d")
_RANK = {"skipped": 0, "match": 1, "unclear": 2, "mismatch": 3}


def enabled_in(options: dict[str, Any] | None) -> bool:
    """``tts.options.echo_check``: ``facts`` (or YAML ``on``) turns it on; anything else (default ``off``,
    which YAML reads as false) leaves it off."""
    v = (options or {}).get("echo_check", "off")
    return v is True or str(v).strip().lower() == "facts"


# ---------------------------------------------------------------- the comparison (pure)


@dataclass(frozen=True)
class EchoFact:
    kind: str
    spoken: str  # as the box heard its voice say it (the echo transcript's surface)
    written: str  # as written in the clause


@dataclass
class EchoVerdict:
    figures: int = 0  # figures of the clause that were compared
    heard: int = 0  # heard with the same value
    unclear: list[str] = field(default_factory=list)  # kinds not heard, or only in part
    mismatches: list[EchoFact] = field(default_factory=list)

    @property
    def status(self) -> str:
        return "mismatch" if self.mismatches else "unclear" if self.unclear else "match"


def figures(text: str, lang: str, glossary: Glossary | None = None) -> list[guard.Fact]:
    """The figures of a clause the echo check compares (empty when the guard is unavailable)."""
    try:
        return guard.extract(text, lang, kinds=ECHO_KINDS, glossary=glossary)
    except Exception:  # the guard missing or failing (logged): no check
        log.debug("voice echo: fact guard unavailable", exc_info=True)
        return []


def _first(surface: str, lang: str, kind: str) -> str | None:
    """The normalised value of one surface form, read on its own."""
    try:
        found = guard.extract(surface, lang, kinds=ECHO_KINDS)
    except Exception:  # noqa: BLE001
        return None
    for f in found:
        if f.kind == kind:
            return f.value
    return found[0].value if found else None


def _date_says_less(written: str | None, heard: str | None) -> bool:
    """A heard date that says less than the written one (month only, weekday for a date): not a mismatch."""
    if written is None or heard is None:
        return True
    if written.startswith("wd:") != heard.startswith("wd:"):
        return True
    return len(re.findall(r"\d+", heard)) < len(re.findall(r"\d+", written))


def _stands_alone(surface: str, heard_text: str, lang: str) -> bool:
    """A figure the ASR wrote in words is read in full only if no other number word touches it ("two point
    five", "fünfhundert fünfzig"): the guard would read just the last part."""
    i = heard_text.find(surface)
    if i < 0:
        return False
    before = heard_text[:i].split()[-2:]
    after = heard_text[i + len(surface):].split()[:2]
    near = " ".join(before + after)
    if _DIGIT.search(near):
        return False
    try:
        return not guard.extract(near, lang, kinds=("number", "dose", "time", "date", "amount", "percent",
                                                    "duration", "frequency"))
    except Exception:  # noqa: BLE001
        return False


def compare(clause: str, heard: str, lang: str, glossary: Glossary | None = None) -> EchoVerdict:
    """The clause as written against what the voice was heard to say (both in ``lang``)."""
    res = guard.check(clause, lang, heard or "", lang, kinds=ECHO_KINDS, glossary=glossary)
    v = EchoVerdict()
    for row in res.facts:
        if row.src is None:
            continue  # heard but not in the clause (ASR noise): nothing to compare it with
        v.figures += 1
        if row.match:
            v.heard += 1
            continue
        spoken = (row.tgt or "").strip()
        if not spoken:
            v.unclear.append(row.kind)  # not heard at all
            continue
        if _DIGIT.search(spoken):
            sure = True
        else:
            sure = lang in WORD_LANGS and _stands_alone(spoken, heard, lang)
        if sure and row.kind == "date":
            sure = not _date_says_less(_first(row.src, lang, "date"), _first(spoken, lang, "date"))
        if sure:
            v.mismatches.append(EchoFact(kind=row.kind, spoken=spoken, written=row.src))
        else:
            v.unclear.append(row.kind)
    return v


def to_asr_input(chunks: Iterable[np.ndarray], rate: int) -> np.ndarray:
    """The voice's int16 PCM -> mono float32 at 16 kHz (the ASR's input; soxr like the worker's sink)."""
    parts = [np.asarray(c, dtype=np.int16).reshape(-1) for c in chunks]
    parts = [c for c in parts if c.size]
    if not parts:
        return np.zeros(0, dtype=np.float32)
    audio = int16_to_float(np.concatenate(parts))
    if rate != ASR_RATE:
        import soxr

        audio = soxr.resample(audio, rate, ASR_RATE).astype(np.float32, copy=False)
    return audio


# ---------------------------------------------------------------- state


@dataclass(eq=False)
class _Group:
    """One utterance's voice in one language (keyed by its ``marks`` dict, which room.py / simul.py already
    pass to every ``_speak`` of that voice)."""

    marks: dict[str, float]
    utt: str
    tgt: str
    epoch: int = 0  # EchoCheck.epoch when it started (a burn moves it on)
    clauses: int = 0  # clauses spoken so far (the clause index of the next one)
    tasks: list[asyncio.Task[None]] = field(default_factory=list)
    status: str | None = None  # worst result so far
    echo_ms: float | None = None  # slowest check so far

    def note(self, status: str, ms: float | None = None) -> None:
        if self.status is None or _RANK[status] > _RANK[self.status]:
            self.status = status
        if ms is not None:
            self.echo_ms = ms if self.echo_ms is None else max(self.echo_ms, ms)


@dataclass(eq=False)
class _Repeat:
    group: _Group
    tgt: str
    text: str
    clause: int


@dataclass(eq=False)
class Probe:
    """Collects one clause's PCM while it plays; ``done`` starts its check in the background."""

    owner: EchoCheck
    group: _Group
    tgt: str
    text: str
    clause: int
    chunks: list[np.ndarray] = field(default_factory=list)
    samples: int = 0
    overflow: bool = False
    synthesized: float = 0.0  # monotonic s: the clause's last PCM went to the track (echo_ms runs from here)

    def add(self, chunk: np.ndarray) -> None:
        if self.overflow:
            return
        self.chunks.append(chunk)
        self.samples += len(chunk)

    def done(self, rate: int) -> None:
        if self.samples > MAX_CLAUSE_S * max(1, rate):
            self.overflow = True
        if self.overflow or not self.samples:
            self.chunks = []
            return
        self.synthesized = time.monotonic()
        self.group.tasks.append(self.owner.p._spawn(self.owner._check(self, rate)))


# ---------------------------------------------------------------- the check


class EchoCheck:
    """Owned by ``RoomPipeline`` (``self.echo_check``). Hooks: ``track`` (a voice starts), ``probe`` and
    ``flush`` (each clause in ``_speak``), ``flush`` (the voice's end), ``settle`` (its timing line), ``clear``
    (burn)."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self.on = enabled_in(p.cfg.models.tts.options)
        self._groups: dict[int, _Group] = {}
        self._pending: dict[str, list[_Repeat]] = {}
        self.epoch = 0  # +1 on burn: checks still running then repeat nothing
        self._dropped: dict[str, None] = {}  # utterances whose voice merge.py stopped: never said again
        self.checks = 0  # transcriptions run (tests, glass box)
        self.repeats = 0  # clauses said again

    @property
    def enabled(self) -> bool:
        """On, with a real voice (audio to hear) and a real ASR."""
        tts, asr = self.p.engines.tts, self.p.engines.asr
        if not self.on or tts is None:
            return False
        if getattr(tts, "is_mock", False) or getattr(tts, "simulated", False):
            return False
        return not getattr(asr, "is_mock", False)

    def track(self, marks: dict[str, float], utt: str, tgt: str) -> None:
        """An utterance's voice into ``tgt`` starts; ``marks`` is the dict its clauses' ``_speak`` calls get."""
        if not self.enabled:
            return
        self._groups[id(marks)] = _Group(marks=marks, utt=utt, tgt=tgt, epoch=self.epoch)
        while len(self._groups) > MAX_GROUPS:
            self._groups.pop(next(iter(self._groups)))

    def _group(self, marks: dict[str, float] | None) -> _Group | None:
        g = self._groups.get(id(marks)) if marks is not None else None
        return g if g is not None and g.marks is marks else None

    def probe(self, tgt: str, text: str, marks: dict[str, float] | None) -> Probe | None:
        """A collector for this clause's PCM, or None: not a tracked voice (a repeat, a read-back), off, or a
        clause without figures."""
        g = self._group(marks)
        if g is None:
            return None
        idx = g.clauses
        g.clauses += 1
        if not self.enabled or not figures(text, tgt, self.p.glossary):
            return None
        return Probe(owner=self, group=g, tgt=tgt, text=text, clause=idx)

    async def _check(self, pr: Probe, rate: int) -> None:
        p = self.p
        g = pr.group
        audio = to_asr_input(pr.chunks, rate)
        pr.chunks = []
        if not audio.size:
            return
        turn = TurnContext(room=p.room, mode=p.mode.id, speaker=ECHO_SPEAKER, role="observer",
                           utt=f"{g.utt}-echo{pr.clause}", speech_ms=audio.size * 1000.0 / ASR_RATE)
        from raum.pipeline.room import CHECK_DEFER_S
        with stats.timed("voice.defer.echo"):
            await voicegate.quiet(CHECK_DEFER_S)  # hear the clause back once the next one is synthesised
        try:
            with stats.timed("echo.slot.background"):  # pipeline.timing stages
                await asyncio.wait_for(p.sched.acquire("background"), ECHO_WAIT_S)
        except TimeoutError:
            log.debug("voice echo %s: ASR busy (no background slot in %.0f s): clause not checked", pr.tgt,
                      ECHO_WAIT_S)
            g.note("skipped")
            return
        try:
            self.checks += 1
            with stats.scope("echo"):  # the engine notes "echo.asr", not the utterance's own "asr"
                res = await asyncio.wait_for(p.engines.asr.transcribe(audio, lang_hint=pr.tgt, partial=False,
                                                                      turn=turn), ECHO_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  the ASR slow or down: this clause goes unchecked
            log.debug("voice echo %s: ASR failed (%s): clause not checked", pr.tgt, e.__class__.__name__)
            g.note("skipped")
            return
        finally:
            p.sched.release("background")
        try:
            v = compare(pr.text, res.text or "", pr.tgt, p.glossary)
        except Exception as e:  # noqa: BLE001
            log.debug("voice echo %s: compare failed (%s)", pr.tgt, e.__class__.__name__)
            g.note("skipped")
            return
        g.note(v.status, (time.monotonic() - pr.synthesized) * 1000.0)
        if v.unclear:
            log.info("voice echo %s %s clause %d: %s not heard clearly", pr.tgt, g.utt, pr.clause,
                     ", ".join(v.unclear))
        if not v.mismatches or p._closed or p.off_record or g.epoch != self.epoch or g.utt in self._dropped:
            return
        log.warning("voice echo %s %s clause %d: the voice said another %s than written: saying it again in "
                    "the standard voice", pr.tgt, g.utt, pr.clause, ", ".join(m.kind for m in v.mismatches))
        for m in v.mismatches:
            await p.emit("voice.echo", {"target_lang": pr.tgt, "kind": m.kind, "spoken": m.spoken,
                                        "written": m.written, "clause": pr.clause, "text": pr.text}, utt=g.utt)
        r = _Repeat(group=g, tgt=pr.tgt, text=pr.text, clause=pr.clause)
        self._pending.setdefault(pr.tgt, []).append(r)
        p._spawn(self._repeat_when_free(r))

    # ------------------------------------------------------------ the repeat (standard voice, same lane)

    async def flush(self, tgt: str, marks: dict[str, float] | None) -> None:
        """Called by the holder of ``tgt``'s voice lane (before each clause, and at the end of its voice):
        say this voice's due repeats now, in order."""
        g = self._group(marks)
        items = self._pending.get(tgt)
        if g is None or not items:
            return
        for r in [r for r in items if r.group is g]:
            if r in items:
                items.remove(r)
                await self._say_again(r)

    async def _repeat_when_free(self, r: _Repeat) -> None:
        """The voice that said it has moved on or ended: take the lane in turn (FIFO, after any utterance
        already waiting for it) and say the repeat unless the voice's own ``flush`` did."""
        async with self.p._voice_lanes.setdefault(r.tgt, asyncio.Lock()):
            items = self._pending.get(r.tgt) or []
            if r not in items:
                return
            items.remove(r)
            await self._say_again(r)

    async def _say_again(self, r: _Repeat) -> None:
        p = self.p
        if p._closed or p.off_record or p._voice_paused(r.tgt) or r.group.epoch != self.epoch \
                or r.group.utt in self._dropped:
            return
        prefix = T.REPEAT.get(r.tgt)
        text = f"{prefix} {r.text}" if prefix else r.text
        self.repeats += 1
        # VoiceArgs(): no voice sample -> the service's standard voice; no marks -> never checked again
        await p._speak(r.tgt, text, time.monotonic(), VoiceArgs())

    # ------------------------------------------------------------ timing, burn

    async def settle(self, marks: dict[str, float] | None) -> dict[str, Any]:
        """The voice is done: wait (bounded) for its checks; ``{echo_ms, echo_check}`` for ``pipeline.timing``
        when a check ran (else nothing)."""
        g = self._group(marks)
        if g is None:
            return {}
        self._groups.pop(id(marks), None)
        if g.tasks:
            await asyncio.wait(g.tasks, timeout=SETTLE_S)
        out: dict[str, Any] = {}
        if g.status is not None:
            out["echo_check"] = g.status
        if g.echo_ms is not None:
            out["echo_ms"] = round(g.echo_ms, 1)
        return out

    def drop(self, utt: str) -> None:
        """merge.py stopped this utterance's voice (a sentence cut at a pause, merged into the next one): its
        repeats are not said, and its checks still running repeat nothing."""
        self._dropped[utt] = None
        while len(self._dropped) > MAX_GROUPS:
            self._dropped.pop(next(iter(self._dropped)))
        for items in self._pending.values():
            items[:] = [r for r in items if r.group.utt != utt]

    def clear(self) -> None:
        """Burn: repeats not yet said are dropped, and checks still running report nothing."""
        self.epoch += 1
        self._pending.clear()
        self._groups.clear()
