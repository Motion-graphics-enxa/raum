"""Engine interfaces (plan §6.1, §6.3). Each stage has a ``mock`` and a ``real`` implementation,
selected by ``raum.yaml`` (``models.<stage>.engine``). See ``raum.pipeline.engines.build_engines``.

Audio conventions: mono float32 numpy arrays in [-1, 1]. ASR input is 16 kHz.
Denoise works at 48 kHz (DeepFilterNet native). TTS yields int16 PCM chunks.

CONTRACT: these signatures are shared by the pipeline, the agent worker, core (summary) and eval.
"""

from __future__ import annotations

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import numpy as np


@dataclass
class TurnContext:
    """What an engine may know about the turn. Mocks use it to pick canned lines."""

    room: str
    mode: str  # mode id, e.g. "sprechzimmer"
    speaker: str  # participant identity
    role: str  # "a" | "b"
    utt: str  # utterance id, e.g. "u-0007"
    speech_ms: float = 0.0  # speech duration so far
    turn_index: int = 0  # utterances so far in this room
    # spelling hints for the recogniser (whisper ``prompt``): the room glossary's terms in the speaker's
    # language, for rooms with ``asr.glossary_prompt`` (room.py ``_asr_prompt``); None = no hints
    asr_prompt: str | None = None


@dataclass
class AsrResult:
    text: str
    lang: str
    conf: float  # 0..1 (mean token probability or equivalent)
    ms: float = 0.0
    words: list[dict[str, Any]] = field(default_factory=list)
    has_conf: bool = True  # False: the engine reported no scores and ``conf`` is a placeholder
    from_partial: bool = False  # the stable last partial, reused as the final (no final ASR pass)


@dataclass
class TranslateRequest:
    text: str
    src_lang: str
    tgt_lang: str
    room_name: str
    role_a: str
    role_b: str
    lang_a: str
    lang_b: str
    register: str = "formal"
    glossary: str = "(none)"  # rendered glossary lines
    context: str = "(none)"  # earlier turns, names/pronouns only
    turn: TurnContext | None = None
    # simultaneous mode: ``text`` is one segment of an utterance; ``earlier`` = the utterance's earlier
    # segments as (source, translation) pairs, in order
    segment: bool = False
    earlier: list[tuple[str, str]] = field(default_factory=list)


@runtime_checkable
class Denoiser(Protocol):
    name: str

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray: ...


@runtime_checkable
class ASR(Protocol):
    name: str

    async def transcribe(self, audio: np.ndarray, *, lang_hint: str | None, partial: bool,
                         turn: TurnContext) -> AsrResult: ...


@runtime_checkable
class Translator(Protocol):
    name: str

    def stream(self, req: TranslateRequest) -> AsyncIterator[str]:
        """Yield text deltas as they generate. Concatenated deltas = final translation."""
        ...


@runtime_checkable
class Verifier(Protocol):
    """Second model (gpt-oss). Different family from the translator."""

    name: str

    async def back_translate(self, text: str, text_lang: str, into_lang: str,
                             turn: TurnContext | None = None) -> str: ...

    async def complete_json(self, prompt: str, *, purpose: str) -> dict[str, Any]:
        """Run a JSON-returning prompt (repeat-back, summary, explain-simply wrapper)."""
        ...

    async def complete_text(self, prompt: str, *, purpose: str) -> str: ...


@runtime_checkable
class Embedder(Protocol):
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@runtime_checkable
class TTS(Protocol):
    name: str
    sample_rate: int

    def synthesize(self, text: str, lang: str, *, voice_ref: bytes | None = None,
                   rate: float | None = None, background: bool = False) -> AsyncIterator[np.ndarray]:
        """Yield int16 PCM chunks at ``sample_rate``. May yield nothing (mock / off).

        ``voice_ref``: WAV bytes of the speaker's voice (memory only, never logged) for engines that
        clone voices; ``rate``: the speaker's pace relative to 2.5 words/s. Engines that cannot use them
        accept and ignore them (the pipeline also calls older two-argument engines without them).
        ``background``: a speculative clause, made before the end of turn, that must never delay a live
        one. The http engine sends it at low priority and raises ``tts_http.VoiceBusy`` when the service
        will not make it now; other engines ignore it. Pass it only where ``takes_kwarg(tts.synthesize,
        "background")``."""
        ...


def takes_kwarg(fn: Any, name: str) -> bool:
    """Can ``fn`` be called with keyword ``name`` (a named parameter, or ``**kwargs``)? For optional engine
    arguments such as ``synthesize(..., background=True)``: engines without it get the plain call."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    p = params.get(name)
    if p is not None:
        return p.kind in (p.KEYWORD_ONLY, p.POSITIONAL_OR_KEYWORD)
    return any(q.kind is q.VAR_KEYWORD for q in params.values())


@runtime_checkable
class SpeakerEncoder(Protocol):
    """Voice lock (F-10, ``raum.pipeline.voicelock``): a speaker embedding of 16 kHz mono speech.

    ``embed`` is synchronous and CPU-bound; the pipeline calls it on a worker thread, never on the event
    loop. Returns a 1-D float32 vector, L2-normalised (all zeros = no voice in it).
    PRIVACY: the vector is biometric data; it stays in memory (see voicelock.py)."""

    name: str

    def embed(self, audio: np.ndarray) -> np.ndarray: ...


@dataclass
class Engines:
    denoise: Denoiser
    asr: ASR
    translate: Translator
    verify: Verifier | None  # None = second-model check off (edge/lite)
    embed: Embedder | None
    tts: TTS | None
    names: dict[str, str] = field(default_factory=dict)  # stage -> model name, for the glass box
    voicelock: SpeakerEncoder | None = None  # F-10; None = voice lock off (the default)

    async def aclose(self) -> None:
        for e in (self.asr, self.translate, self.verify, self.embed, self.tts):
            close = getattr(e, "aclose", None)
            if close:
                await close()
