"""The other person's own voice on a call (``ParticipantInfo.original_voice``): echo guard for the listener's mic.

When the two people are in different places, each phone may play the other person's real mic track quietly under
the translated voice (the app's "Original voice", ``app/src/lib/originalVoice.ts``; LiveKit carries it, nothing
leaves the box). A phone that plays it out loud (``audio_out`` not ``headphones``) has that voice next to its own
mic. Browser echo cancellation removes most of it; this guard makes sure what is left never becomes an utterance:

    while another person (not an observer) is voicing (VAD speech start -> its pause or end) and for ``tail_s``
    after (the voice is still on its way through LiveKit and the speaker), the listener's mic is paused:
    ``speech_start`` refuses it and the worker feeds its VAD silence (``RoomPipeline.mic_open``).

Voicing, not the utterance: an utterance stays open through the end-of-turn wait (up to ~1.6 s of silence), and
the reply's first words must not be dropped there. An utterance the listener began before stays open to its end,
as with the box's own voice (C-14, ``raum.pipeline.echo``). A refused start is not voicing, so two devices never
hold each other paused. Off for every device without ``original_voice`` (the same room: unchanged).
"""

from __future__ import annotations

import time
from collections.abc import Mapping

from raum.events import ParticipantInfo

LIVE_VOICE_TAIL_S = 0.4  # VAD end already trails the last word by ~0.25 s; this covers the tunnel's latency


class LiveVoiceGuard:
    def __init__(self, tail_s: float = LIVE_VOICE_TAIL_S) -> None:
        self.tail_s = tail_s
        self._voicing: dict[str, float | None] = {}  # speaker -> None while voicing, else when it stopped

    def start(self, speaker: str) -> None:
        self._voicing[speaker] = None

    def stop(self, speaker: str, now: float | None = None) -> None:
        if speaker in self._voicing:
            self._voicing[speaker] = time.monotonic() if now is None else now

    def clear(self) -> None:
        self._voicing.clear()

    def is_paused(self, identity: str, participants: Mapping[str, ParticipantInfo],
                  now: float | None = None) -> bool:
        """Does ``identity``'s mic hear another person's live voice right now?"""
        p = participants.get(identity)
        if p is None or not p.original_voice or p.audio_out == "headphones" or not self._voicing:
            return False
        now = time.monotonic() if now is None else now
        for speaker, stopped in list(self._voicing.items()):
            if speaker == identity:
                continue
            other = participants.get(speaker)
            if other is not None and other.role == "observer":
                continue
            if stopped is None or now < stopped + self.tail_s:
                return True
            del self._voicing[speaker]  # long over
        return False
