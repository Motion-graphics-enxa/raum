"""Live draft translation text (both modes). Hooked into ``room.py``.

While the speaker talks, the stable part of each partial transcript is translated for every listener language
and shown in place as a draft, so the listener reads the gist before the speaker stops::

    translation.draft  {target_lang, text, seq[, seg]}     utt = the utterance; seq grows per (utt, target)

The app replaces the previous draft of that utterance and language in place; ``translation.final`` (or, in
simultaneous mode, the segment that covers it) takes its place.

- **Stable part**: the word-aligned longest common prefix of the utterance's last two partials; with one partial
  so far, that partial minus its last word (still being heard). From ``pipeline.draft_after_ms`` (1.5 s) of speech
  on; the same stable text again (per target) is not translated again.
- **Newest wins**: one draft MT in flight per (utt, target). A newer stable text waits and replaces any older one
  still waiting; when the MT in flight finishes, its draft goes out only if the newest stable text still starts
  with its source and covers the same committed segments (else the ASR revised it, or a segment took it: dropped).
  One worker per (utt, target) publishes in order, so an older draft never follows a newer one.
- **Spacing**: draft MTs of one (utt, target) start at least ``pipeline.draft_every_ms`` (0.6 s) apart.
- **Priority**: ``voicegate.quiet(SPEC_DEFER_S)`` first (live voice first, in any room), then the scheduler's
  ``background`` slot. The same engine and request as the turn (``RoomPipeline._request``), collected, not streamed.
- **Simultaneous mode** (simul.py): once a target language has committed segments, its draft covers only the
  uncommitted tail after them (aligned like simul.py aligns the tail at the end), as a segment request with the
  earlier segments as chat history, and carries ``seg`` = the index that tail will have. A segment >= ``seg``
  arriving in the app covers it; the next draft follows that segment.
- **Stops** when the utterance ends (``speech_end``: the final translation replaces it), when it is dropped (off the
  record, burn, close) and at session end; in flight MTs are cancelled and nothing more of it goes out.
- **Never voiced**: no TTS, no voice lane, no fact guard, flags or read-back from a draft, and it is not a content
  event (not stored). Off with ``pipeline.draft_text: false``, and while the voice lock (F-10) is on: it admits an
  utterance only after its final ASR, so nothing of it is translated before.

Stages (``pipeline.timing`` ``stages``): ``draft.collect`` (each draft MT, wall), ``draft.mt`` (the engine's own,
Ollama), ``draft.slot.background``, ``draft.voice.defer``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from raum.pipeline import stats, voicegate
from raum.pipeline._util import lang_code
from raum.pipeline.simul import align

if TYPE_CHECKING:
    from raum.pipeline.base import AsrResult, TranslateRequest
    from raum.pipeline.room import RoomPipeline, _Utt

log = logging.getLogger("raum.pipeline.draft")

DRAFT_AFTER_MS = 1500.0
DRAFT_EVERY_MS = 600.0


def stable_prefix(prev: list[str] | None, cur: list[str]) -> list[str]:
    """The words of ``cur`` the ASR has settled: the longest common word prefix with the partial before it; with
    no partial before, ``cur`` minus its last word (still being heard)."""
    if prev is None:
        return cur[:-1]
    n = 0
    for a, b in zip(prev, cur, strict=False):
        if a != b:
            break
        n += 1
    return cur[:n]


def _norm(text: str) -> list[str]:
    return text.casefold().split()


@dataclass
class _Want:
    src: str  # source text to translate (glossary spelling)
    seg: int | None  # simultaneous mode: the index of the tail segment it drafts; None: the whole utterance
    earlier: list[tuple[str, str]] = field(default_factory=list)  # committed segments (source, translation)

    @property
    def key(self) -> tuple[str, int | None]:
        return (" ".join(_norm(self.src)), self.seg)

    def extends(self, older: _Want) -> bool:
        """This (newer) text still says what ``older`` said, word for word: a draft of ``older`` is not stale."""
        a, b = _norm(older.src), _norm(self.src)
        return self.seg == older.seg and b[:len(a)] == a


@dataclass
class _Lane:
    """One target language of one utterance."""

    want: _Want | None = None  # the newest stable text
    sent: tuple[str, int | None] | None = None  # key of the last text an MT was started for
    seq: int = 0
    started: float = float("-inf")  # monotonic s, the last MT's start
    task: asyncio.Task[None] | None = None


@dataclass
class _UttDraft:
    utt: str
    prev: list[str] | None = None  # the partial before (words)
    lang: str | None = None
    lanes: dict[str, _Lane] = field(default_factory=dict)
    closed: bool = False


class Drafts:
    """Owned by ``RoomPipeline`` (``self.draft``). Hooks: ``on_partial`` (after ``simul.on_partial``), ``drop``
    (the utterance ended or was dropped), ``clear`` (session end)."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self._utts: dict[str, _UttDraft] = {}
        self.published = 0  # drafts sent (tests / glass box)

    def _opt(self, name: str, default: Any) -> Any:
        return getattr(getattr(self.p.cfg, "pipeline", None), name, default)

    @property
    def enabled(self) -> bool:
        locked = bool(getattr(getattr(self.p, "voicelock", None), "enabled", False))  # F-10: admitted after the final
        return bool(self._opt("draft_text", True)) and not locked

    @property
    def after_ms(self) -> float:
        return float(self._opt("draft_after_ms", DRAFT_AFTER_MS))

    @property
    def every_s(self) -> float:
        return float(self._opt("draft_every_ms", DRAFT_EVERY_MS)) / 1000.0

    # ------------------------------------------------------------ hooks

    def on_partial(self, u: _Utt, res: AsrResult, speech_s: float) -> None:
        """A partial transcript of ``u`` (``speech_s`` of audio so far): draft its stable part for every target."""
        if not self.enabled or u.closed:
            return
        tokens = res.text.split()
        if not tokens:
            return
        lang = lang_code(res.lang) or u.hint or self.p._default_lang(u.role)
        st = self._utts.setdefault(u.id, _UttDraft(utt=u.id))
        prev = st.prev if st.lang == lang else None  # another language: start over
        st.prev, st.lang = tokens, lang
        if speech_s * 1000.0 < self.after_ms:
            return
        if getattr(res, "has_conf", True) and float(res.conf) < self.p.cfg.asr_min_conf:
            return  # V-03: a turn heard this unsure is not translated (its final asks to say it again)
        stable = stable_prefix(prev, tokens)
        if not stable:
            return
        rec = SimpleNamespace(utt=u.id, speaker=u.speaker, role=u.role, lang=lang, text="", turn=u.turn)
        lanes = self.p.simul.lanes(u.id)  # simul.py: committed segments per target
        for tgt in self.p._targets(rec):  # type: ignore[arg-type]
            want = self._want(stable, lang, lanes.get(tgt))
            if want is None:
                continue
            lane = st.lanes.setdefault(tgt, _Lane())
            if lane.want is not None and lane.want.key == want.key:
                continue  # the stable text did not change
            lane.want = want
            if lane.task is None or lane.task.done():
                lane.task = self.p._spawn(self._run(st, u, lang, tgt, lane))

    def drop(self, utt: str) -> None:
        """The utterance ended (its final translation replaces the draft), was dropped or burned: no more drafts."""
        st = self._utts.pop(utt, None)
        if st is None:
            return
        st.closed = True
        for lane in st.lanes.values():
            if lane.task is not None and not lane.task.done() and lane.task is not asyncio.current_task():
                lane.task.cancel()

    def clear(self) -> None:
        """Session end / burn / close."""
        for utt in list(self._utts):
            self.drop(utt)

    # ------------------------------------------------------------ internals

    def _want(self, stable: list[str], lang: str, seg_lane: Any) -> _Want | None:
        """What to draft for one target: the stable text, or with committed segments only the tail after them."""
        segs = list(getattr(seg_lane, "segs", None) or []) if seg_lane is not None else []
        if not segs:
            src = self.p.fix_terms(" ".join(stable), lang)
            return _Want(src=src, seg=None) if src.strip() else None
        done = [w for s in segs for w in str(getattr(s, "src", "") or "").split()]
        tail = stable[align(done, stable):]
        if not tail:
            return None  # the committed segments cover all the stable text
        src = self.p.fix_terms(" ".join(tail), lang)
        earlier = [(str(s.src), str(s.text)) for s in segs if getattr(s, "text", "")]
        return _Want(src=src, seg=len(segs), earlier=earlier) if src.strip() else None

    def _request(self, u: _Utt, lang: str, tgt: str, want: _Want) -> TranslateRequest:
        rec = SimpleNamespace(utt=u.id, speaker=u.speaker, role=u.role, lang=lang, text=want.src, turn=u.turn)
        req = self.p._request(rec, tgt, self.p._context())  # type: ignore[arg-type]
        if want.seg is not None:  # the tail after committed segments: a segment request, as simul.py builds them
            req = replace(req, segment=True, earlier=list(want.earlier))
        return req

    async def _run(self, st: _UttDraft, u: _Utt, lang: str, tgt: str, lane: _Lane) -> None:
        """The (utt, target) worker: one MT at a time, the newest stable text each time, drafts in order."""
        from raum.pipeline.room import MT_TIMEOUT_S, SPEC_DEFER_S

        with stats.scope("draft"):
            while not st.closed:
                want = lane.want
                if want is None or want.key == lane.sent:
                    return
                gap = lane.started + self.every_s - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
                    continue  # a newer text may have come meanwhile
                with stats.timed("voice.defer"):
                    await voicegate.quiet(SPEC_DEFER_S)  # live voice first, in any room
                async with self.p.sched.slot("background"):
                    if st.closed:
                        return
                    want = lane.want or want  # the newest, at the moment its MT starts
                    lane.sent, lane.started = want.key, time.monotonic()
                    text = await self._translate(self._request(u, lang, tgt, want), MT_TIMEOUT_S)
                newest = lane.want or want
                if st.closed or not text or not newest.extends(want):
                    continue  # ended, failed, or stale: the ASR revised it / a segment took it
                lane.seq += 1
                self.published += 1
                data: dict[str, Any] = {"target_lang": tgt, "text": text, "seq": lane.seq}
                if want.seg is not None:
                    data["seg"] = want.seg
                await self.p.emit("translation.draft", data, utt=st.utt)

    async def _translate(self, req: TranslateRequest, timeout_s: float) -> str:
        parts: list[str] = []

        async def run() -> None:
            async for d in self.p.engines.translate.stream(req):
                if d:
                    parts.append(d)

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(run(), timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001  a draft is optional: the final translation comes anyway
            log.debug("draft translation failed: %s: %s", e.__class__.__name__, e)
            stats.note("collect", failed=1)
            return ""
        stats.note("collect", wall_ms=(time.monotonic() - t0) * 1000.0)  # any engine; Ollama adds draft.mt
        return "".join(parts).strip()
