"""Simultaneous mode (UN-style interpreting). Hooked into ``room.py``.

While the speaker is still talking, each partial transcript is compared with the one before. A prefix that
is identical in two consecutive partials and ends at a clause boundary is **committed** as a segment:

- ``. ? !`` and ``;``; ``,`` once the clause has at least 6 words. A German source cuts at a comma / ``;``
  only after a verb-complete clause (incomplete.py ``unfinished`` gives no reason: "Sie dürfen das
  Metformin," never goes out before its infinitive; once a clause since the last cut is open, only a
  sentence end cuts) and only with a stable word after the comma (whisper flips "an." <-> "an, das").
- AND the clause is complete: the room's text turn model (turn.py, ``RoomPipeline.eot``) rates the text
  up to that boundary as a finished utterance (``p_text >= eot.fast``, the threshold of its own "end now"
  path); without the model, or for a language it lacks, only sentence ends pass. Nothing is anticipated:
  a German clause never goes out before its finite verb or particle.
- Abbreviations and ordinals ("Dr.", "12.") and an ellipsis never end a clause.
- Only partials with ``asr_conf >= asr_min_conf`` commit (V-03), in one source language; only once the turn
  has ``simultaneous_after_ms`` of speech (per mode: the Sprechzimmer 1.5 s). Pauses inside
  the turn are turn.py's (semantic end of turn): speech after a pause continues the same utterance, and
  its lanes go on.

Per target language, a committed segment is at once translated (the utterance's earlier segments as chat
history behind the same fixed system prefix), checked by the fact guard (it gates the voice) and spoken.
A language's segments are translated and spoken strictly in order. At the end of the utterance only the
uncommitted tail is translated; it is the last segment.

When: per target language, unless ``pipeline.simultaneous`` (raum.yaml) or the mode's is ``off`` (``plan``):

- every participant of that language (other than the speaker) on headphones (``ParticipantInfo.audio_out``):
  text and voice while the speaker talks;
- someone plays it aloud (speaker / room): the mode's ``pipeline.loudspeaker``. ``live`` (gs, sz, kz): text
  and voice while the speaker talks (C-14 pauses the listener's own mic while it plays), except where the
  speaker's own mic would hear it (echo.py ``paused_for``: a room speakerphone): there **held**. ``held``:
  always held. ``consecutive`` (the default): not simultaneous. Held: the text shows while the speaker talks
  and each segment's voice is made ahead (speculate ``ahead``), played from the cache after the end of turn.

Every other language stays consecutive (``RoomPipeline._translate``, unchanged). A language that stops
qualifying mid-utterance (or switches between held and live) commits nothing more; its tail comes at the end.

Final-revision guard (``_revise``, at the end of the utterance): each committed segment is compared with the
final transcript's words at its place. A segment the final rewrote (another word, not just case or
punctuation) takes the final's words and is translated again, its ``translation.final`` sent again (the app
keys segments by ``seg``); its voice waits for that, so the listener hears the final's version, never the
stale one (audio made ahead is keyed by the old text: never claimed, forgotten). Each segment is voiced once,
in order. One already spoken (a live lane) is corrected on screen only, and a correction flag follows the
check. No usable final (ASR failed, low confidence): a held lane's voice never plays.

Events, per target language (``seg`` 0-based; consecutive mode sends no ``seg``)::

    translation.delta  {target_lang, delta, seq, seg}       seq restarts at 1 in every segment
    translation.final  {target_lang, text, seg[, segs]}     this segment's text; the last final has segs
    verify.facts       {target_lang, seg, facts, red, ms}             fact guard, per segment
    flag               {..., id: "<utt>-<tgt>-mismatch"}       a segment's guard was red: one read-back (readback.py)
    verify.result      {target_lang, similarity, badge, ...}   NO seg: one check of the whole utterance
    flag               {..., id: "<utt>-<tgt>-noted|confirm"}  the room's facts: noted (all green) or read back
    flag               {..., id: "<utt>-<tgt>-correction"}     that check came back red (already spoken)
    pipeline.timing    one per utterance and language after the check (+ simultaneous, segs);
                       mt_first_ms / tts_first_ms are negative when they came before the speaker stopped

Segments start before ``utterance.end`` / ``transcript.final``. When nothing is left at the end (the last
commit took the whole utterance), the last segment's ``translation.final`` is sent again, now with ``segs``.

Voice: only committed segments are spoken, never drafts, and nothing waits for a tap. A segment whose fact
guard is red is not spoken (the wrong figure never reaches the listener's ear); after the utterance, the read-back
of what was said takes its place ("Nur zur Sicherheit: 400 mg?"). Segments go through the room's voice lane for
their language (room.py), so utterances into one language are spoken one after the other, the read-back after.

Second-model check: ONE per utterance and language, on the joined segments, after the tail (gpt-oss takes
p50 2.7 s on the GX10 with one check slot: a check per segment would queue). Its fact-guard input is the
segments' guard results merged (guard red still wins). The utterance's facts then go the consecutive way
(room.py ``_fact_flags`` / ``_settle_facts``): a guard mismatch is read back before the check, otherwise the
check decides noted or read back. Red with nothing read back -> a correction flag (``needs_confirm``): the
text was shown and spoken already.

Both modes: if the last partial was stable (same as the one before), ended < 300 ms before the endpoint
and carries ``asr_conf >= 0.85``, it is the final transcript; the final ASR pass is skipped.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import numpy as np

from raum.pipeline import stats, voicegate
from raum.pipeline import texts as T
from raum.pipeline._util import lang_code
from raum.pipeline.base import AsrResult, TurnContext
from raum.pipeline.incomplete import unfinished
from raum.pipeline.voice import speech_seconds
from raum.verify import guard
from raum.verify.backtranslate import second_check

if TYPE_CHECKING:
    from collections.abc import Awaitable, Iterable

    from raum.pipeline.room import RoomPipeline, _Turn, _Utt

log = logging.getLogger("raum.pipeline.simul")

SAMPLE_RATE = 16000
MIN_COMMA_WORDS = 6
# verb-final languages: a comma cuts only after a verb-complete clause (incomplete.py ``unfinished`` has no reason)
# and only with a stable word after it (whisper flips "an." <-> "an, das" at the end of what it heard)
COMMA_RULE_LANGS = frozenset({"de"})
REUSE_GAP_MS = 300.0  # a stable partial this close to the endpoint stands in for the final
REUSE_MIN_CONF = 0.85
CATCHUP_RATE = 1.1  # a live lane whose next segment already waits speaks a little faster (no growing lag)

_SENTENCE = frozenset(".!?。！？؟")
_CLAUSE = frozenset(";؛")
_COMMA = frozenset(",،，")
_CLOSERS = "\"'»«”’)]"
_ABBREV = re.compile(r"^(?:Dr|Fr|Hr|Prof|Nr|ca|bzw|z\.B|u\.a|St|Mr|Mrs|Ms|No|vs|[^\W\d_])\.$|^\d+\.$",
                     re.IGNORECASE)


# ---------------------------------------------------------------- commit rules (pure functions)


def boundary(token: str) -> str | None:
    """The clause boundary a word ends with: ``"sentence"``, ``"clause"``, ``"comma"`` or None."""
    t = token.rstrip(_CLOSERS)
    if not t or t.endswith(("...", "…")):
        return None
    c = t[-1]
    if c in _SENTENCE:
        return None if c == "." and _ABBREV.match(t) else "sentence"
    if c in _CLAUSE:
        return "clause"
    if c in _COMMA:
        return "comma"
    return None


def commit_points(prev: list[str], cur: list[str], lang: str | None) -> list[int]:
    """Where ``cur`` may be cut (word counts, ascending): within the prefix that ``prev`` (the partial
    before) has too, at a valid clause boundary. Both lists start after the already committed words.
    German (``COMMA_RULE_LANGS``): a comma or ``;`` only after a verb-complete clause (incomplete.py), with
    the word after it stable too."""
    stable = 0
    for a, b in zip(prev, cur):
        if a != b:
            break
        stable += 1
    rules = (lang or "") in COMMA_RULE_LANGS
    out: list[int] = []
    start = 0
    open_ = False  # German: a clause since the last cut was not verb-complete; only a sentence end closes it
    for i in range(stable):
        kind = boundary(cur[i])
        if kind is None:
            continue
        if rules and kind != "sentence":
            if unfinished(" ".join(cur[start:i + 1]), lang):
                open_ = True  # "Sie dürfen das Metformin," (no infinitive yet): no later comma either
            if open_ or i + 1 >= stable:
                continue  # ... / "an," at the very end of what was heard (whisper: "an." <-> "an, das")
        if kind == "comma" and i + 1 - start < MIN_COMMA_WORDS:
            continue
        out.append(i + 1)
        start = i + 1
        open_ = False
    return out


def commit_len(prev: list[str], cur: list[str], lang: str | None) -> int:
    """The longest valid cut by punctuation alone (0 = none)."""
    points = commit_points(prev, cur, lang)
    return points[-1] if points else 0


def speaker_voice(p: RoomPipeline, speaker: str, text: str | None = None, seconds: float | None = None) -> Any:
    """The speaker's voice args for voice made before the turn's own ``_voice_args`` (segments,
    pre-synthesis): the voice sample so far, and the pace as known now, or (``text``/``seconds`` given) the
    pace this turn will report, computed like room.py ``_voice_args`` without keeping it."""
    import copy

    from raum.pipeline.voice import SpeakingRate, VoiceArgs

    rate = getattr(p, "_rates", {}).get(speaker)
    hint = rate.hint if rate is not None else None
    if text and seconds:
        probe = copy.copy(rate) if rate is not None else SpeakingRate(getattr(p, "base_wps", 2.5))
        hint = probe.update(text, seconds)
    return VoiceArgs(ref=getattr(p, "_voice_refs", {}).get(speaker), rate=hint)


def _since(ms: float | None, t0: float) -> float | None:
    return round(ms - t0 * 1000.0, 1) if ms is not None else None


def _turn_model_covers(lang: str | None) -> bool:
    try:
        from raum.pipeline.turn import LK_FLOOR
    except Exception:  # noqa: BLE001  the turn extra is not installed
        return False
    return (lang or "") in LK_FLOOR


def _norm(token: str) -> str:
    return re.sub(r"[\W_]+", "", token.casefold())


def _same_words(a: list[str], b: list[str]) -> bool:
    """The same words, whatever the case and punctuation ("day," / "day.")."""
    return [x for x in map(_norm, a) if x] == [x for x in map(_norm, b) if x]


def align(done: list[str], tokens: list[str]) -> int:
    """Index in ``tokens`` (a later transcript) where the words after ``done`` (committed) begin. Exact
    prefix in the normal case; otherwise the best word alignment (the ASR revised an earlier word)."""
    n = len(done)
    if n == 0:
        return 0
    if tokens[:n] == done:
        return n
    a, b = [_norm(t) for t in done], [_norm(t) for t in tokens]
    if b[:n] == a:
        return n
    blocks = [m for m in SequenceMatcher(None, a, b, autojunk=False).get_matching_blocks() if m.size]
    if not blocks:
        return min(n, len(tokens))
    m = max(blocks, key=lambda m: m.a + m.size)
    return max(0, min(len(tokens), m.b + m.size + (n - m.a - m.size)))


# ---------------------------------------------------------------- state


@dataclass
class _Partial:
    tokens: list[str]
    lang: str
    conf: float
    has_conf: bool
    samples: int  # audio position the partial covered


@dataclass
class _Seg:
    index: int
    src: str
    text: str = ""
    skip: bool = False  # its fact guard was red: not spoken (the read-back after the utterance says it right)
    segs_sent: bool = False
    gres: guard.GuardResult | None = None
    guard_ok: bool = False
    # the final-revision guard (``_revise``): the final transcript rewrote ``src`` -> ``rev`` + 1, translated again
    rev: int = 0
    redo: bool = False  # translated again from the final's words (no deltas: the app shows its first text till then)
    started: bool = False  # its translation began
    ready: asyncio.Event = field(default_factory=asyncio.Event)  # translated (as it now stands) and guarded
    passed: bool = False  # the voice worker is past it (spoken or not spoken): a later rewrite is text only
    spoken: bool = False


@dataclass
class _SegTurn:
    """What ``RoomPipeline._request`` / ``_fact_flags`` / ``_settle_facts`` read from a turn record."""

    utt: str
    speaker: str
    role: str
    lang: str
    text: str
    turn: TurnContext
    conf: float | None = None  # the final transcript's ASR confidence (None: no final)


@dataclass(eq=False)  # kept in a set (Simultaneous._held)
class _Lane:
    """One target language of one utterance in simultaneous mode."""

    utt: str
    tgt: str
    src_lang: str
    speaker: str
    role: str
    turn: TurnContext
    context: str
    covered: int = 0  # committed source words this lane has as segments
    segs: list[_Seg] = field(default_factory=list)
    queue: asyncio.Queue[_Seg | None] = field(default_factory=asyncio.Queue)
    grew: asyncio.Event = field(default_factory=asyncio.Event)  # a segment was added (or the lane closed)
    worker: asyncio.Task[None] | None = None
    voice: asyncio.Task[None] | None = None
    done: asyncio.Task[None] | None = None  # the utterance check + timing
    voice_args: Any = None  # the speaker's voice sample and pace (voice.py), for the segments' voice
    marks: dict[str, float] = field(default_factory=dict)  # first PCM / first published frame, monotonic ms
    voice_done: asyncio.Event = field(default_factory=asyncio.Event)
    held: bool = False  # played aloud next to the speaker's mic: voiced ahead while they talk, played at the end
    release: asyncio.Event = field(default_factory=asyncio.Event)  # held: set once the final was checked (_finish)
    discard: bool = False  # held, and the turn ended without a usable final: its voice never plays
    ahead: list[Any] = field(default_factory=list)  # held: speculate._Voice made ahead (forgotten if never played)
    ahead_carry: str = ""  # held: a segment too short to voice alone, made with the next one
    revised: int = 0  # segments the final transcript rewrote (pipeline.timing)
    misheard: bool = False  # one of them had been spoken already: a correction flag after the check
    total: int | None = None  # segment count, known once the utterance ended
    conf: float | None = None  # the final transcript's ASR confidence, once the utterance ended
    closed: bool = False
    first_delta: float | None = None  # monotonic s
    last_final: float | None = None
    first_voice_ms: float | None = None  # monotonic ms


@dataclass
class _UttState:
    last: _Partial | None = None
    stable: bool = False  # last == the partial before it
    done: list[str] = field(default_factory=list)  # committed source words
    src_lang: str | None = None
    lanes: dict[str, _Lane] = field(default_factory=dict)


# ---------------------------------------------------------------- the mode


class Simultaneous:
    """Owned by ``RoomPipeline`` (``self.simul``). Hooks: ``on_partial``, ``reuse_final``, ``finish_turn``,
    ``release``, ``drop``, ``settle`` (before the early final ASR); ``lanes`` for speculate.py and merge.py,
    ``stop_lanes`` for merge.py."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self._utts: dict[str, _UttState] = {}
        self._held: set[_Lane] = set()  # held lanes whose voice has not played yet (burn / session end stop them)

    @property
    def enabled(self) -> bool:
        box = getattr(getattr(self.p.cfg, "pipeline", None), "simultaneous", "auto")
        room = getattr(getattr(self.p.mode, "pipeline", None), "simultaneous", "auto")
        # F-10 voice lock admits an utterance only after its final ASR: segments would go out before that
        locked = bool(getattr(getattr(self.p, "voicelock", None), "enabled", False))
        return box != "off" and room != "off" and not locked

    @property
    def after_ms(self) -> float:
        return float(getattr(getattr(self.p.mode, "pipeline", None), "simultaneous_after_ms", 0.0) or 0.0)

    def lanes(self, utt: str) -> dict[str, _Lane]:
        st = self._utts.get(utt)
        return st.lanes if st else {}

    @property
    def loudspeaker(self) -> str:
        return str(getattr(getattr(self.p.mode, "pipeline", None), "loudspeaker", "consecutive") or "consecutive")

    def plan(self, speaker: str, role: str, lang: str) -> dict[str, bool]:
        """Target languages that go simultaneous now -> held (True: the voice waits for the end of the turn).
        Every participant of the language on headphones: voiced while the speaker talks. Someone plays it aloud
        (speaker / room): the mode's ``pipeline.loudspeaker`` -- ``consecutive`` (not simultaneous), ``held``, or
        ``live``: voiced while the speaker talks unless the speaker's own mic would hear it (echo.py
        ``paused_for``: a room speakerphone), then held."""
        if not self.enabled:
            return {}
        from raum.pipeline.echo import paused_for

        loud = self.loudspeaker
        out: dict[str, bool] = {}
        for tgt in self.p._targets(SimpleNamespace(speaker=speaker, role=role, lang=lang)):  # type: ignore[arg-type]
            people = [x for x in self.p.participants.values()
                      if x.id != speaker and self.p._participant_lang(x) == tgt]
            if not people:
                continue
            if all(getattr(x, "audio_out", "speaker") == "headphones" for x in people):
                out[tgt] = False
            elif loud == "held":
                out[tgt] = True
            elif loud == "live":
                out[tgt] = speaker in paused_for(tgt, self.p.participants.values())
        return out

    def targets(self, speaker: str, role: str, lang: str) -> list[str]:
        """Target languages that go simultaneous now (``plan``)."""
        return list(self.plan(speaker, role, lang))

    # ------------------------------------------------------------ hooks

    async def complete(self, text: str, lang: str | None) -> bool:
        """Does ``text`` read as a finished utterance? The room's text turn model (turn.py) if it covers the
        language, else (and on any failure): it ends with sentence punctuation."""
        words = (text or "").split()
        if not words:
            return False
        eot = getattr(self.p, "eot", None)
        if unfinished(text, lang):  # incomplete.py: "Sie dürfen das Metformin ab morgen" is no clause to commit
            return False
        if eot is not None and _turn_model_covers(lang):
            floor = eot.commit_floor(lang) if hasattr(eot, "commit_floor") else float(getattr(eot, "fast", 0.1))
            try:
                return float(await eot.text_p(text)) >= floor
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.debug("turn model failed on a clause: %s", e)
        return boundary(words[-1]) == "sentence"

    async def on_partial(self, u: _Utt, res: AsrResult, audio: np.ndarray) -> None:
        """After every partial transcript: remember it; speculate on a stable one (speculate.py); commit a
        stable, complete clause as a segment."""
        st = self._utts.setdefault(u.id, _UttState())
        lang = lang_code(res.lang) or u.hint or self.p._default_lang(u.role)
        cur = _Partial(res.text.split(), lang, float(res.conf), bool(getattr(res, "has_conf", True)), len(audio))
        prev, st.last = st.last, cur
        st.stable = prev is not None and prev.tokens == cur.tokens and prev.lang == lang
        if prev is None or prev.lang != lang or st.src_lang not in (None, lang) or u.closed:
            return
        if cur.has_conf and cur.conf < self.p.cfg.asr_min_conf:
            return
        if self.p.spec.enabled and await self.complete(res.text, lang):
            if u.closed or st.last is not cur:
                return
            seconds = speech_seconds(len(audio) / SAMPLE_RATE, 0.0, res.words, getattr(self.p, "vad_pad_s", 0.5))
            self.p.spec.on_text(u, res.text, lang, seconds=seconds)  # a complete sentence: likely the final
        if len(audio) * 1000.0 / SAMPLE_RATE < self.after_ms:
            return
        want = self.plan(u.speaker, u.role, lang)
        if not want:
            return
        base = cur.tokens[align(st.done, cur.tokens):]
        for n in reversed(commit_points(prev.tokens[align(st.done, prev.tokens):], base, lang)[-3:]):
            if lang in COMMA_RULE_LANGS and boundary(base[n - 1]) != "sentence":
                complete = True  # a German comma: commit_points took it only after a verb-complete clause
            else:  # complete clauses only: the turn model must rate the text up to the cut a finished utterance
                complete = await self.complete(" ".join(st.done + base[:n]), lang)
            if u.closed or st.last is not cur:
                return
            if complete:
                st.done += base[:n]
                st.src_lang = lang
                break
        if not st.done:
            return
        for tgt, held in want.items():
            lane = st.lanes.get(tgt) or self._lane(st, u, tgt, lang, held=held)
            if lane.held != held:  # the listener switched how they hear it: no more segments (the tail at the end)
                continue
            if not lane.closed and lane.covered < len(st.done):
                self._enqueue(lane, st.done[lane.covered:])

    async def settle(self, u: _Utt, n_audio: int, timeout_s: float) -> None:
        """At a pause, before transcribing: a partial still running that covers the audio to < 300 ms before
        the end, after a partial that was a complete sentence, will most likely be the final. Wait for it
        (it started earlier than a new final pass would) so that ``reuse_final`` can take it. Only one that is
        transcribing already (room ``_partial`` sets ``u.partial_asr``): one still queued for its scheduler slot
        is cancelled, and the final goes ahead at once (box: 1.9 s waited for a partial that had not started)."""
        task, st = u.partial_task, self._utts.get(u.id)
        if task is None or task.done():
            return
        if getattr(u, "partial_asr", None) is not task:
            task.cancel()  # queued behind other background work: the final pass hears more audio anyway
            stats.note("asr.settle", queued=1)
            return
        if st is None or st.last is None or not st.last.tokens:
            return
        start = (u.next_partial_ms - self.p.partial_every_ms) * SAMPLE_RATE / 1000.0  # its audio end
        if (max(u.samples, n_audio) - start) * 1000.0 / SAMPLE_RATE >= REUSE_GAP_MS:
            return
        if not await self.complete(" ".join(st.last.tokens), st.last.lang):
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  timeout or a failed partial: transcribe as usual
            return

    def reuse_final(self, u: _Utt, n_audio: int) -> AsrResult | None:
        """The last partial as the final transcript, if it was stable, ended < 300 ms before the endpoint
        and carries a confidence >= 0.85. None: transcribe as usual."""
        st = self._utts.get(u.id)
        last = st.last if st else None
        if st is None or last is None or not st.stable or not last.tokens:
            return None
        if not last.has_conf or last.conf < REUSE_MIN_CONF:
            return None
        gap_ms = (max(u.samples, n_audio) - last.samples) * 1000.0 / SAMPLE_RATE
        if gap_ms >= REUSE_GAP_MS:
            return None
        log.debug("%s: stable partial %.0f ms before the endpoint reused as final", u.id, gap_ms)
        return AsrResult(text=" ".join(last.tokens), lang=last.lang, conf=last.conf, has_conf=True, from_partial=True)

    def finish_turn(self, u: _Utt, rec: _Turn, targets: list[str], t0: float,
                    timing: dict[str, Any]) -> tuple[list[Awaitable[None]], list[str]]:
        """At the end of the utterance: jobs that translate each lane's tail, and the targets that stay
        consecutive (no segment committed)."""
        st = self._utts.pop(u.id, None)
        if st is None or not st.lanes:
            return [], targets
        jobs = [self._finish(lane, rec, t0, dict(timing)) for lane in st.lanes.values()]
        return jobs, [t for t in targets if t not in st.lanes]

    def release(self, utt: str) -> None:
        """The turn ended without a usable final (ASR failed, low confidence): close what was committed."""
        st = self._utts.pop(utt, None)
        for lane in st.lanes.values() if st else ():
            lane.discard = lane.held  # held: nothing was checked against a final; it never plays
            lane.release.set()
            self._close(lane, "")
            lane.done = self.p._spawn(self._done(lane, None, {}))

    def drop(self, utt: str) -> None:
        """Off the record / burn / close mid-utterance: stop its segments at once."""
        st = self._utts.pop(utt, None)
        self.stop_lanes(st.lanes.values() if st else ())

    def stop_lanes(self, lanes: Iterable[_Lane]) -> None:
        """Stop these lanes at once, also after their turn ended (merge.py: the speaker went on)."""
        for lane in list(lanes):
            lane.closed = True
            lane.discard = True
            for task in (lane.worker, lane.voice, lane.done):
                if task is not None and not task.done():
                    task.cancel()
            lane.voice_done.set()
            self._forget_ahead(lane)

    def clear(self) -> None:
        """Burn / session end / close: held lanes never play the voice they made ahead (speculate ``clear``
        drops the audio itself)."""
        self.stop_lanes(list(self._held))

    # ------------------------------------------------------------ lanes

    def _lane(self, st: _UttState, u: _Utt, tgt: str, lang: str, *, held: bool = False) -> _Lane:
        lane = _Lane(utt=u.id, tgt=tgt, src_lang=lang, speaker=u.speaker, role=u.role, turn=u.turn,
                     context=self.p._context(), held=held)
        lane.voice_args = speaker_voice(self.p, u.speaker)
        lane.worker = self.p._spawn(self._translate_worker(lane))
        if self.p._tts_enabled():
            if held:
                self._held.add(lane)
            lane.voice = self.p._spawn(self._voice_worker(lane))
        else:
            lane.voice_done.set()
        st.lanes[tgt] = lane
        return lane

    def _enqueue(self, lane: _Lane, words: list[str]) -> None:
        seg = _Seg(index=len(lane.segs), src=self.p.fix_terms(" ".join(words), lane.src_lang))  # "Medformin"
        lane.segs.append(seg)
        lane.covered += len(words)
        lane.queue.put_nowait(seg)
        lane.grew.set()

    def _close(self, lane: _Lane, tail: str) -> None:
        if lane.closed:
            return
        lane.closed = True
        if tail.strip():
            seg = _Seg(index=len(lane.segs), src=self.p.fix_terms(tail.strip(), lane.src_lang))
            lane.segs.append(seg)
            lane.queue.put_nowait(seg)
        lane.total = len(lane.segs)
        lane.queue.put_nowait(None)
        lane.grew.set()

    def _revise(self, lane: _Lane, words: list[str]) -> int:
        """The final-revision guard, at the end of the utterance: each committed segment against the final
        transcript's words at its place (``align`` per segment end). A segment the final rewrote (another word,
        not just case or punctuation) takes the final's words and is translated again; until then its voice
        waits (``ready``), so what plays is the final's version, never the stale one (its audio made ahead is
        keyed by the old text: never claimed, forgotten after the voice). One already spoken (a live lane) is
        corrected on screen and gets a correction flag after the check: nothing is spoken twice. Returns where
        the uncommitted tail starts in ``words``."""
        pos, done = 0, []
        for seg in list(lane.segs):
            own = seg.src.split()
            done += own
            end = max(pos, align(done, words))
            new, pos = words[pos:end], end
            if _same_words(own, new):
                continue
            lane.revised += 1
            log.info("%s-%s-s%d: the final rewrote a committed segment (%d -> %d words): %s", lane.utt, lane.tgt,
                     seg.index, len(own), len(new), "corrected on screen (spoken already)" if seg.spoken
                     else "the final's version is voiced")
            lane.misheard = lane.misheard or seg.spoken
            seg.rev += 1
            seg.src = " ".join(new)
            if seg.started:  # translated (or being translated) from the old words: again (still queued: as is)
                seg.redo = True
                seg.ready.clear()
                lane.queue.put_nowait(seg)
        return pos

    async def _finish(self, lane: _Lane, rec: _Turn, t0: float, timing: dict[str, Any]) -> None:
        words = rec.text.split()
        self._close(lane, " ".join(words[self._revise(lane, words):]))
        lane.conf = rec.conf
        lane.release.set()  # held: the voice plays now, each segment as the final has it
        if lane.worker is not None:
            await asyncio.gather(lane.worker, return_exceptions=True)
        rec.translations[lane.tgt] = self._joined(lane)[1]
        lane.done = self.p._spawn(self._done(lane, t0, timing))

    @staticmethod
    def _joined(lane: _Lane) -> tuple[str, str]:
        """(source, translation) of the utterance: the segments joined."""
        segs = [s for s in lane.segs if s.text]
        return " ".join(s.src for s in segs), " ".join(s.text for s in segs)

    async def _done(self, lane: _Lane, t0: float | None, timing: dict[str, Any]) -> None:
        """After the tail: the one second-model check of the utterance, then (after the voice) its timing."""
        if lane.worker is not None:
            await asyncio.gather(lane.worker, return_exceptions=True)
        check_ms = await self._check(lane) if any(s.text for s in lane.segs) else None
        if lane.voice is not None:
            await asyncio.gather(lane.voice, return_exceptions=True)
        echo = await self.p.echo_check.settle(lane.marks)  # echo_check.py: echo_ms, echo_check
        if t0 is None:
            return

        def rel(ts: float | None) -> float | None:
            return round((ts - t0) * 1000.0, 1) if ts is not None else None

        timing.update(target_lang=lane.tgt, mt_first_ms=rel(lane.first_delta), mt_full_ms=rel(lane.last_final),
                      tts_first_ms=round(lane.first_voice_ms - t0 * 1000.0, 1) if lane.first_voice_ms else None,
                      check_ms=check_ms, simultaneous=True, segs=len(lane.segs),
                      tts_synth_first_ms=_since(lane.marks.get("synth"), t0),
                      tts_publish_first_ms=_since(lane.marks.get("publish"), t0), **echo)
        if lane.held:
            timing["held"] = True  # voiced ahead while the speaker talked, played after the end of turn
        if lane.revised:
            timing["revised"] = lane.revised  # segments the final transcript rewrote (the final's version voiced)
        if any(f"voice_{k}" in lane.marks for k in ("spec", "ahead", "live")):  # speculate.py: where the audio
            timing["voice_clauses"] = {k: int(lane.marks.get(f"voice_{k}", 0)) for k in ("spec", "ahead", "live")}
        st = stats.current()  # stats.py: per stage, what queued and what worked (as room._after_final)
        if st is not None:
            timing["stages"] = st.snapshot()
        await self.p.emit("pipeline.timing", timing, utt=lane.utt)

    # ------------------------------------------------------------ translation, guard, check

    async def _translate_worker(self, lane: _Lane) -> None:
        while True:
            seg = await lane.queue.get()
            if seg is None:
                break
            rev = seg.rev
            try:
                await self._segment(lane, seg)
            finally:
                if seg.rev == rev:  # else the final rewrote it meanwhile: it is queued again
                    seg.ready.set()
            if seg.rev == rev and lane.held and not lane.release.is_set():
                self._ahead(lane, seg)
        last = lane.segs[-1] if lane.segs else None
        if last is not None and not last.segs_sent:  # nothing was left after the last commit
            await self._final(lane, last, again=True)

    async def _final(self, lane: _Lane, seg: _Seg, *, again: bool = False) -> None:
        data: dict[str, Any] = {"target_lang": lane.tgt, "text": seg.text, "seg": seg.index}
        if lane.total is not None and seg.index == lane.total - 1:
            data["segs"] = lane.total
            seg.segs_sent = True
        if not again:
            lane.last_final = time.monotonic()
        await self.p.emit("translation.final", data, utt=lane.utt)

    async def _segment(self, lane: _Lane, seg: _Seg) -> None:
        from raum.pipeline.room import MT_TIMEOUT_S, _guard_call

        p = self.p
        rev, src, redo = seg.rev, seg.src, seg.redo
        seg.started = True
        parts: list[str] = []
        if src.strip():
            rec = _SegTurn(utt=lane.utt, speaker=lane.speaker, role=lane.role, lang=lane.src_lang, text=src,
                           turn=lane.turn)
            req = replace(p._request(rec, lane.tgt, lane.context),  # type: ignore[arg-type]
                          segment=True, earlier=[(s.src, s.text) for s in lane.segs[:seg.index]])

            async def run() -> None:
                async for delta in p.engines.translate.stream(req):
                    if not delta:
                        continue
                    if lane.first_delta is None:
                        lane.first_delta = time.monotonic()
                    parts.append(delta)
                    if not redo:  # a redo replaces the segment with its final only
                        await p.emit("translation.delta", {"target_lang": lane.tgt, "delta": delta,
                                                           "seq": len(parts), "seg": seg.index}, utt=lane.utt)

            try:
                async with p.sched.slot("live"):
                    await asyncio.wait_for(run(), MT_TIMEOUT_S)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                log.warning("segment %s/%d -> %s failed: %s: %s", lane.utt, seg.index, lane.tgt,
                            e.__class__.__name__, e)
        if seg.rev != rev:
            return  # the final rewrote it meanwhile: translated again from the final's words (queued)
        seg.text = "".join(parts).strip()
        seg.gres, seg.guard_ok, seg.skip = None, False, False
        await self._final(lane, seg)
        if not seg.text:
            if src.strip():
                await p.emit("verify.facts", {"target_lang": lane.tgt, "seg": seg.index, "facts": [], "red": False,
                                              "error": "translation failed"}, utt=lane.utt)
            return

        # fact guard: code, ~0.2 ms; it gates this segment's voice
        res, ok = _guard_call(guard.check, src, lane.src_lang, seg.text, lane.tgt, kinds=p.mode.guard.kinds,
                              confirm_on=p.mode.guard.confirm_on, glossary=p.glossary)
        seg.gres, seg.guard_ok = res, ok
        facts: dict[str, Any] = {"target_lang": lane.tgt, "seg": seg.index, "facts": [], "red": False}
        if res is not None:
            facts.update(facts=[f.model_dump() for f in res.facts], red=bool(res.red), ms=round(res.ms, 2))
        else:
            facts["error"] = "fact guard unavailable"
        await p.emit("verify.facts", facts, utt=lane.utt)
        seg.skip = bool(res is not None and res.red)  # a wrong figure is never spoken; nothing waits for a tap

    async def _check(self, lane: _Lane) -> float:
        """One second-model check of the whole utterance (joined segments, merged guard results).
        ``verify.result`` without ``seg``; red and no guard prompt raised -> correction flag."""
        from raum.pipeline.room import CHECK_TIMEOUT_S, ask_badge

        p = self.p
        t = lane.last_final or time.monotonic()  # check_ms runs from the last translation.final, as today
        src, text = self._joined(lane)
        done = [s for s in lane.segs if s.text]
        ran = [s.gres for s in done if s.gres is not None]
        gres = guard.GuardResult(facts=[f for g in ran for f in g.facts], red=any(g.red for g in ran),
                                 confirm=[f for g in ran for f in g.confirm],
                                 ms=sum(g.ms for g in ran)) if ran else None
        guard_ok = bool(done) and all(s.guard_ok for s in done)
        rec = _SegTurn(utt=lane.utt, speaker=lane.speaker, role=lane.role, lang=lane.src_lang, text=src,
                       turn=lane.turn, conf=lane.conf)
        flagged = await p._fact_flags(rec, lane.tgt, gres)  # a guard mismatch: read back now, before the check
        verifier, why = p.checker_for(lane.role, lane.src_lang, lane.tgt)

        async def run() -> Any:
            res = await second_check(src, lane.src_lang, text, lane.tgt, verifier=verifier,
                                     embedder=p.engines.embed, thresholds=p.cfg.thresholds, guard=gres,
                                     guard_available=guard_ok, turn=lane.turn, timeout_s=CHECK_TIMEOUT_S)
            if why:
                res.reasons = [why if r.startswith("second check off") else r for r in res.reasons]
            return res

        if verifier is None:
            result = await run()
        else:
            from raum.pipeline.room import CHECK_DEFER_S
            with stats.timed("voice.defer.check"):
                await voicegate.quiet(CHECK_DEFER_S)  # gpt-oss between clauses, not under the voice
            async with p.sched.slot("check"):
                result = await run()
        check_ms = round((time.monotonic() - t) * 1000.0, 1)
        await p.emit("verify.result", {"target_lang": lane.tgt, "similarity": result.similarity,
                                       "badge": result.badge, "reasons": result.reasons,
                                       "back_translation": result.back_translation}, utt=lane.utt)
        if not flagged:  # the room's facts: noted (all green) or read back once (room.py, readback.py)
            flagged = await p._settle_facts(rec, lane.tgt, gres, ask_badge(result))
        # already shown and spoken; or the final rewrote a segment the listener had heard (corrected on screen)
        if (result.badge == "red" or lane.misheard) and not flagged:
            texts = T.all_langs(T.CORRECTION, lane.tgt)
            await p.emit("flag", {"kind": "mismatch", "text": T.pick(texts, lane.tgt), "needs_confirm": True,
                                  "id": f"{lane.utt}-{lane.tgt}-correction",
                                  "confirm_by": p._listener_role(lane.tgt, lane.role), "target_lang": lane.tgt,
                                  "texts": texts}, utt=lane.utt)
        return check_ms

    # ------------------------------------------------------------ voice

    async def _voice_worker(self, lane: _Lane) -> None:
        """Speak the lane's segments in index order, each once it is translated as it now stands (``ready``:
        a segment the final rewrote waits for its new translation). Holds the room's voice lane for this
        language (room.py ``_voice_lanes``) for the whole utterance, like ``_speak_stream``: other utterances
        into this language speak before or after it, never in between, and the track has one writer. A held
        lane first waits for the end of the turn (``release``, after the final-revision guard)."""
        from raum.pipeline.room import VOICE_IDLE_S

        p = self.p
        try:
            if lane.held:
                await lane.release.wait()  # played aloud next to the speaker's mic: never over them
                if lane.discard:
                    return
            p.echo_check.track(lane.marks, lane.utt, lane.tgt)  # echo_check.py: segments with figures heard back
            async with p._voice_lanes.setdefault(lane.tgt, asyncio.Lock()):
                try:
                    await self._play(lane)
                except TimeoutError:
                    log.warning("voice %s: no segment for %.0f s, releasing the lane", lane.tgt, VOICE_IDLE_S)
                    return
                await p.echo_check.flush(lane.tgt, lane.marks)  # echo_check.py: a repeat already due
        finally:
            lane.voice_done.set()
            self._forget_ahead(lane)

    async def _play(self, lane: _Lane) -> None:
        from raum.pipeline.room import VOICE_IDLE_S

        p = self.p
        i = 0
        carry: list[_Seg] = []  # segments too short for the cloned voice go with the next one
        while True:
            while i >= len(lane.segs) and not lane.closed:
                lane.grew.clear()
                await asyncio.wait_for(lane.grew.wait(), VOICE_IDLE_S)
            if i >= len(lane.segs):
                break
            seg = lane.segs[i]
            await asyncio.wait_for(seg.ready.wait(), VOICE_IDLE_S)
            i += 1
            if seg.skip:
                seg.passed = True
                log.info("%s-%s-s%d: fact guard red, not spoken (read back after)", lane.utt, lane.tgt, seg.index)
                continue
            if not seg.text or p._voice_paused(lane.tgt) or p._closed:
                seg.passed = True
                continue
            group = [*carry, seg]
            floor = p.min_voice_words(lane.speaker)
            if floor and len(" ".join(s.text for s in group).split()) < floor:
                carry = group
                continue
            carry = []
            await self._say_group(lane, group, i)
        if carry and not p._voice_paused(lane.tgt) and not p._closed:
            await self._say_group(lane, carry, i)

    async def _say_group(self, lane: _Lane, group: list[_Seg], nxt_at: int) -> None:
        from raum.pipeline.room import VOICE_IDLE_S

        for s in group:  # a carried segment the final rewrote meanwhile: its new translation
            await asyncio.wait_for(s.ready.wait(), VOICE_IDLE_S)
        group = [s for s in group if s.text and not s.skip]
        text = " ".join(s.text for s in group)
        for s in group:
            s.passed = s.spoken = True
        if not text:
            return
        nxt = self._peek(lane, nxt_at)
        voice = self._voice(lane, behind=nxt is not None)
        tts = self.p.engines.tts
        if tts is not None:  # speculate.py: this one from the cache (held: made ahead), the next one made meanwhile
            self.p.spec.prepare(tts, lane.tgt, text, nxt, self.p._voice_kwargs(tts, voice), utt=lane.utt)
        await self._say(lane, text, voice)

    def _peek(self, lane: _Lane, i: int) -> str | None:
        """What the voice says after segment ``i - 1``, if it is translated already (None: not yet / nothing)."""
        floor = self.p.min_voice_words(lane.speaker)
        parts: list[str] = []
        for s in lane.segs[i:]:
            if not s.ready.is_set():
                return None
            if s.skip or not s.text:
                continue
            parts.append(s.text)
            if not floor or len(" ".join(parts).split()) >= floor:
                return " ".join(parts)
        return " ".join(parts) if parts and lane.closed else None

    def _voice(self, lane: _Lane, *, behind: bool) -> Any:
        """The lane's voice args; a live lane whose next segment already waits catches up a little."""
        voice = lane.voice_args
        if behind and not lane.held and voice is not None:
            voice = replace(voice, rate=max(float(voice.rate or 1.0), CATCHUP_RATE))
        return voice

    def _ahead(self, lane: _Lane, seg: _Seg) -> None:
        """A held lane's segment, translated while the speaker still talks: its voice is made now in the
        background (speculate ``ahead``, after the one before), cut as the voice worker will cut it, so that it
        plays from the cache at the end of the turn."""
        if lane.voice is None or seg.skip or not seg.text:
            return
        text = f"{lane.ahead_carry} {seg.text}".strip()
        floor = self.p.min_voice_words(lane.speaker)
        if floor and len(text.split()) < floor:
            lane.ahead_carry = text
            return
        lane.ahead_carry = ""
        v = self.p.spec.ahead(lane.tgt, text, lane.voice_args, after=lane.ahead[-1] if lane.ahead else None)
        if v is not None:
            lane.ahead.append(v)

    def _forget_ahead(self, lane: _Lane) -> None:
        """The lane is done (or stopped): voice made ahead that never played goes (a segment the final rewrote)."""
        self._held.discard(lane)
        if lane.ahead:
            self.p.spec.forget(lane.ahead)
            lane.ahead = []

    async def _say(self, lane: _Lane, text: str, voice: Any = None) -> None:
        first = await self.p._speak(lane.tgt, text, 0.0, voice if voice is not None else lane.voice_args,
                                    lane.marks, utt=lane.utt)  # t0 0: monotonic ms
        if lane.first_voice_ms is None and first is not None:
            lane.first_voice_ms = first
