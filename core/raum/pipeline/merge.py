"""A sentence cut at a pause, merged again (professional rooms). Hooked into ``room.py``.

The end-of-turn decision (turn.py) can still end a turn inside a sentence: "Sie dürfen das Metformin ab morgen
[800 ms] nicht mehr nehmen." went out as "You may take the Metformin?" and was spoken before "Do not take it
anymore." followed. This is the safety net. With the mode's ``pipeline.merge_continuation_ms`` > 0 (0 = off):
when the same speaker starts speaking again within that many ms of their utterance's end of turn, and that
utterance's voice has not finished playing (or it has none), then

1. its voice stops at once in every target language: the lane tasks writing it are cancelled, and the worker's
   ``p.tts_stop(lang)`` drops what the ``tts-<lang>`` AudioSource still holds (only languages it wrote to, so
   another utterance still waiting in that lane is not cut);
2. everything still running for it (translation, voice, second check, simultaneous segments) is cancelled and
   whatever it would still send is dropped (``RoomPipeline.emit``): no late ``verify.result`` or red flag; it
   leaves the conversation memory (V-04) and gives back a repeat-back it took (S-02);
3. ``utterance.continued`` ``{speaker, into}`` goes out for it (a content event, so the summary skips it);
4. the new utterance starts with its audio (``_Utt.prefix``): partials and the final ASR hear the whole
   sentence, which is translated, checked and spoken as one.

Memory only: the audio of the last turn per speaker is held until the window has passed (or burn, off the
record, session end, ``aclose``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import numpy as np

if TYPE_CHECKING:
    from raum.pipeline.room import RoomPipeline, _Turn, _Utt

log = logging.getLogger("raum.pipeline.merge")

_KEEP = 64  # utterance ids remembered (continued ones; voice play-out per utterance)
T = TypeVar("T")


@dataclass
class _Ended:
    """The last utterance of one speaker whose turn ended: may still be continued."""

    utt: str
    speaker: str
    at: float  # monotonic: its end of turn
    audio: np.ndarray
    echoed: bool
    turn: asyncio.Task[Any] | None  # its turn (``run``) while that still runs
    lanes: dict[str, Any]  # its simultaneous lanes (simul.py), which outlive the turn
    repeat_back: bool  # p._repeat_back before its turn (S-02)
    last_instruction: _Turn | None  # p._last_instruction before its turn
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)  # its voice and check (room.py _translate)
    expiry: asyncio.TimerHandle | None = None


class Continuations:
    """Owned by ``RoomPipeline`` (``self.merge``). Hooks: ``ended`` / ``run`` (speech_end), ``track`` (its voice
    and check tasks), ``played`` (each voice chunk), ``take`` (speech_start), ``silenced`` (emit), ``clear``."""

    def __init__(self, p: RoomPipeline) -> None:
        self.p = p
        self._ended: dict[str, _Ended] = {}  # speaker -> their last ended utterance
        self._silenced: OrderedDict[str, str] = OrderedDict()  # continued utt -> the utt it went on in
        self._voice: OrderedDict[str, dict[str, float]] = OrderedDict()  # utt -> lang -> monotonic play-out end
        self._clock: dict[str, float] = {}  # lang -> monotonic time the audio queued so far has played out

    @property
    def window_s(self) -> float:
        ms = getattr(getattr(self.p.mode, "pipeline", None), "merge_continuation_ms", 0.0) or 0.0
        return max(0.0, float(ms)) / 1000.0

    # ------------------------------------------------------------ hooks

    def ended(self, u: _Utt, audio: np.ndarray, at: float) -> None:
        """``u``'s turn ends now (before its final transcript, translation and voice)."""
        if self.window_s <= 0:
            return
        old = self._ended.pop(u.speaker, None)
        if old is not None and old.expiry is not None:
            old.expiry.cancel()
        e = _Ended(utt=u.id, speaker=u.speaker, at=at, audio=audio, echoed=u.echoed, turn=None,
                   lanes=dict(self.p.simul.lanes(u.id)), repeat_back=self.p._repeat_back,
                   last_instruction=self.p._last_instruction)
        self._ended[u.speaker] = e
        e.expiry = asyncio.get_running_loop().call_later(self.window_s + 0.05, self._expire, e)

    async def run(self, utt: str, turn: Coroutine[Any, Any, None]) -> None:
        """Run ``utt``'s turn (speech_end). While it may be continued it runs as its own task, so that stopping it
        (``take``) leaves the caller's task alone: speech_end then just returns."""
        e = self._find(utt)
        if e is None:
            await turn
            return
        task = e.turn = asyncio.ensure_future(turn)
        try:
            await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if task.cancelled() and self.silenced(utt) and not (me is not None and me.cancelling()):
                return  # stopped by the merge, not by the caller
            raise
        finally:
            e.turn = None

    def track(self, utt: str, task: asyncio.Task[T]) -> asyncio.Task[T]:
        """A voice or check task of ``utt``'s turn: cancelled if the speaker goes on."""
        e = self._find(utt)
        if e is not None:
            e.tasks.add(task)
            task.add_done_callback(e.tasks.discard)
        return task

    def played(self, utt: str | None, lang: str, seconds: float) -> None:
        """``seconds`` of voice in ``lang`` go to the worker now (one track per language plays them in order)."""
        if self.window_s <= 0:
            return
        end = self._clock[lang] = max(self._clock.get(lang, 0.0), time.monotonic()) + seconds
        if utt is None:
            return
        self._voice.setdefault(utt, {})[lang] = end
        self._voice.move_to_end(utt)
        while len(self._voice) > _KEEP:
            self._voice.popitem(last=False)

    def take(self, u: _Utt) -> str | None:
        """``u`` just started. If it continues its speaker's last turn (within the window, that turn's voice not
        played out yet): stop that turn, give ``u`` its audio and return its id. Else None."""
        e = self._ended.get(u.speaker)
        if e is None or self.window_s <= 0:
            return None
        gap = time.monotonic() - e.at
        if gap > self.window_s or not self._voice_open(e):
            return None
        del self._ended[u.speaker]
        if e.expiry is not None:
            e.expiry.cancel()
        u.prefix, u.echoed = e.audio, u.echoed or e.echoed
        langs = self._stop(e, u.id)
        log.info("%s continues %s (%.0f ms after its end of turn): merged, voice stopped in %s", u.id, e.utt,
                 gap * 1000.0, ", ".join(langs) or "no language")
        return e.utt

    def silenced(self, utt: str | None) -> bool:
        """Continued in a later utterance: nothing more of it goes out."""
        return utt is not None and utt in self._silenced

    def clear(self) -> None:
        """Burn, off the record, session end, close: the held audio goes."""
        for e in self._ended.values():
            if e.expiry is not None:
                e.expiry.cancel()
            e.audio = np.zeros(0, dtype=np.float32)
        self._ended.clear()
        self._voice.clear()
        self._clock.clear()

    # ------------------------------------------------------------ internals

    def _find(self, utt: str) -> _Ended | None:
        return next((e for e in self._ended.values() if e.utt == utt), None)

    def _expire(self, e: _Ended) -> None:
        if self._ended.get(e.speaker) is e:
            del self._ended[e.speaker]
        e.audio = np.zeros(0, dtype=np.float32)

    def _voice_open(self, e: _Ended) -> bool:
        """Has ``e``'s voice not finished playing? Also True while it is still translated or waits for its lane,
        and when it has no voice at all; False once something played and all of it has played out."""
        if e.turn is not None and not e.turn.done():
            return True
        if any(not t.done() for t in e.tasks):
            return True
        if any(lane.voice is not None and not lane.voice.done() for lane in e.lanes.values()):
            return True
        until = self._voice.get(e.utt)
        return not until or time.monotonic() < max(until.values())

    def _stop(self, e: _Ended, into: str) -> list[str]:
        """Stop everything of ``e`` at once; returns the languages whose voice was cut."""
        p = self.p
        self._silenced[e.utt] = into
        while len(self._silenced) > _KEEP:
            self._silenced.popitem(last=False)
        current = asyncio.current_task()
        for t in [e.turn, *e.tasks]:
            if t is not None and t is not current and not t.done():
                t.cancel()
        p.simul.drop(e.utt)
        p.simul.stop_lanes(e.lanes.values())
        p.spec.drop(e.utt)
        p.voicelock.start(e.speaker, e.utt, None, 0.0)  # F-10: no audio: drops and wipes its check still in flight
        e.audio = np.zeros(0, dtype=np.float32)  # the new utterance holds it now
        langs = list(self._voice.pop(e.utt, {}))
        for lang in langs:  # the writer is cancelled above; drop what its track still holds
            if p.tts_stop is None:
                continue
            try:
                p.tts_stop(lang)
            except Exception:
                log.exception("stopping the voice in %s failed", lang)
                continue
            self._clock.pop(lang, None)  # nothing queued in that language any more
            if p.echo.stop(lang) is not None:  # C-14: nothing plays any more, the mics open
                p._spawn(p.emit("voice.state", {"target_lang": lang, "playing": False, "paused": []}))
        # conversation memory (V-04): the cut half is no turn of its own; the merged sentence is remembered
        for rec in [r for r in p._turns if r.utt == e.utt]:
            p._turns.remove(rec)
        if p._last_instruction is not None and p._last_instruction.utt == e.utt:
            p._last_instruction = e.last_instruction
        if e.repeat_back:  # S-02: a repeat-back it took is checked on the merged sentence instead
            p._repeat_back = True
        p._drafts.pop(e.utt, None)
        return langs
