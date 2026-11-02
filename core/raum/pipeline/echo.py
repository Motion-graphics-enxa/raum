"""Echo control per device (C-14, plan §6.13). Used by :class:`raum.pipeline.room.RoomPipeline`.

While the box's voice for language X plays (its playback duration plus ``tail_s``, 300 ms), the mics
that would hear it are paused:

- ``audio_out == "speaker"`` devices whose language is X: the voice comes out next to their own mic;
- every device except ``headphones`` ones when some device in the room has ``audio_out == "room"``
  (a shared speaker / A-03 speakerphone the whole room hears);
- ``headphones`` devices never: nobody else hears their voice.

Everyone else keeps talking: the next speaker's first words are not dropped. Audio from a paused
device is echo and is dropped, except an utterance that began before the pause, which runs to its
end (the pipeline lets active speakers through).
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass, field

from raum.events import ParticipantInfo
from raum.pipeline._util import lang_code

ECHO_TAIL_S = 0.3


def paused_for(lang: str, participants: Iterable[ParticipantInfo]) -> list[str]:
    """Identities whose mic pauses while the voice for ``lang`` plays (in participant order)."""
    people = list(participants)
    if any(p.audio_out == "room" for p in people):
        return [p.id for p in people if p.audio_out != "headphones"]
    return [p.id for p in people if p.audio_out == "speaker" and lang_code(p.lang) == lang]


@dataclass
class Playback:
    lang: str
    audio_end: float  # time.monotonic() at which the queued voice has played out
    paused: list[str] = field(default_factory=list)


class EchoControl:
    """Playback bookkeeping per language. The voice sink plays each language's audio in order, in real
    time (one LiveKit track per language), so the end is ``max(end, now) + duration`` per chunk."""

    def __init__(self, tail_s: float = ECHO_TAIL_S) -> None:
        self.tail_s = tail_s
        self.playing: dict[str, Playback] = {}

    def play(self, lang: str, seconds: float, participants: Iterable[ParticipantInfo],
             now: float | None = None) -> Playback | None:
        """``seconds`` of voice in ``lang`` go to the speakers now. Returns the playback if one starts."""
        now = time.monotonic() if now is None else now
        pb = self.playing.get(lang)
        if pb is not None:
            pb.audio_end = max(pb.audio_end, now) + seconds
            return None
        pb = Playback(lang=lang, audio_end=now + seconds, paused=paused_for(lang, participants))
        self.playing[lang] = pb
        return pb

    def remaining(self, lang: str, now: float | None = None) -> float | None:
        """Seconds until the pause for ``lang`` lifts (<= 0: due), None if nothing plays."""
        pb = self.playing.get(lang)
        if pb is None:
            return None
        return pb.audio_end + self.tail_s - (time.monotonic() if now is None else now)

    def stop(self, lang: str) -> Playback | None:
        return self.playing.pop(lang, None)

    def is_paused(self, identity: str, now: float | None = None) -> bool:
        if not self.playing:
            return False
        now = time.monotonic() if now is None else now
        return any(identity in pb.paused and now < pb.audio_end + self.tail_s for pb in self.playing.values())

    def refresh(self, participants: Iterable[ParticipantInfo]) -> list[Playback]:
        """A device changed language or output mid-playback: recompute; returns the playbacks that changed."""
        people = list(participants)
        changed: list[Playback] = []
        for pb in self.playing.values():
            new = paused_for(pb.lang, people)
            if new != pb.paused:
                pb.paused = new
                changed.append(pb)
        return changed

    def clear(self) -> None:
        self.playing.clear()
