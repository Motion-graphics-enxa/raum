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
"""

from __future__ import annotations

from typing import Awaitable, Callable

import numpy as np

from raum.config import RaumConfig
from raum.events import Control, Envelope, ParticipantInfo, SessionState
from raum.modes.schema import ModeConfig
from raum.pipeline.base import Engines

Emit = Callable[[Envelope], Awaitable[None]]


class RoomPipeline:
    def __init__(self, room: str, mode: ModeConfig, engines: Engines, emit: Emit, config: RaumConfig) -> None:
        raise NotImplementedError

    def set_participants(self, participants: list[ParticipantInfo]) -> None:
        raise NotImplementedError

    def set_state(self, state: SessionState) -> None:
        raise NotImplementedError

    async def speech_start(self, speaker: str) -> str | None:
        raise NotImplementedError

    async def speech_audio(self, speaker: str, frame: np.ndarray) -> None:
        raise NotImplementedError

    async def speech_end(self, speaker: str, audio: np.ndarray | None, vad_ms: float) -> None:
        raise NotImplementedError

    async def handle_control(self, control: Control) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        raise NotImplementedError
