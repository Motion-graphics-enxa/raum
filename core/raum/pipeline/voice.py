"""Inputs for a voice that sounds like the speaker (C-13): a short voice sample per speaker and their pace.

PRIVACY: a voice sample is biometric data. :class:`VoiceRef` lives in memory only: never on disk, in
an event, a log line, the session store or the audit log (its repr shows the length only). The
pipeline captures it only when the voice engine uses one (``tts.uses_voice_ref``), from accepted
utterances that no voice played over, and drops it on burn and when the room's pipeline closes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from raum.pipeline._util import float_to_wav

SAMPLE_RATE = 16000
BASE_WPS = 2.5  # rate 1.0 = 2.5 words per second
RATE_MIN, RATE_MAX = 0.7, 1.5  # beyond that, synthetic speech stops being pleasant to follow


class VoiceRef:
    """The first ``max_s`` seconds of a speaker's clean speech (16 kHz mono); ``wav()`` = WAV bytes."""

    __slots__ = ("_chunks", "_samples", "_wav", "max_samples", "sample_rate")

    def __init__(self, *, max_s: float = 8.0, sample_rate: int = SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self.max_samples = int(max_s * sample_rate)
        self._chunks: list[np.ndarray] = []
        self._samples = 0
        self._wav: bytes | None = None

    @property
    def seconds(self) -> float:
        return self._samples / self.sample_rate

    @property
    def full(self) -> bool:
        return self._samples >= self.max_samples

    def add(self, audio: np.ndarray) -> None:
        if self.full or audio is None or len(audio) == 0:
            return
        take = np.asarray(audio, dtype=np.float32).reshape(-1)[: self.max_samples - self._samples].copy()
        self._chunks.append(take)
        self._samples += len(take)
        self._wav = None

    def wav(self) -> bytes:
        if self._wav is None:
            self._wav = float_to_wav(np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32),
                                     self.sample_rate)
        return self._wav

    def clear(self) -> None:
        for c in self._chunks:
            c.fill(0.0)  # best effort: overwrite the samples, not just drop the reference
        self._chunks.clear()
        self._samples = 0
        self._wav = None

    def __repr__(self) -> str:
        return f"<VoiceRef {self.seconds:.1f} s>"


@dataclass(frozen=True)
class VoiceArgs:
    """What the pipeline hands the voice engine for one speaker's translated words."""

    ref: VoiceRef | None = None
    rate: float | None = None


def speech_seconds(audio_s: float, vad_ms: float, words: Sequence[dict[str, Any]] | None = None,
                   prefix_pad_s: float = 0.0) -> float:
    """How long the speaker actually spoke: the ASR word timestamps if there are any, else the utterance
    audio minus the endpoint silence and the VAD's prefix padding."""
    starts = [w.get("start") for w in words or [] if isinstance(w.get("start"), (int, float))]
    ends = [w.get("end") for w in words or [] if isinstance(w.get("end"), (int, float))]
    if starts and ends and max(ends) - min(starts) > 0.3:
        return float(max(ends) - min(starts))
    return max(audio_s - vad_ms / 1000.0 - prefix_pad_s, 0.5 * audio_s)


class SpeakingRate:
    """A speaker's pace (words per second, smoothed over their utterances) as a rate hint for the voice."""

    def __init__(self, base_wps: float = BASE_WPS, alpha: float = 0.5) -> None:
        self.base_wps = base_wps
        self.alpha = alpha
        self.wps: float | None = None

    def update(self, text: str, seconds: float) -> float | None:
        words = len(text.split())
        if words >= 3 and seconds >= 0.8:  # shorter: too noisy to say anything about the pace
            wps = words / seconds
            self.wps = wps if self.wps is None else self.alpha * wps + (1 - self.alpha) * self.wps
        return self.hint

    @property
    def hint(self) -> float | None:
        if self.wps is None:
            return None
        return round(min(RATE_MAX, max(RATE_MIN, self.wps / self.base_wps)), 2)
