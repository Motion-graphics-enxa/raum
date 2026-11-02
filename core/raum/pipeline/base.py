"""Engine interfaces (plan §6.1, §6.3). Each stage has a ``mock`` and a ``real`` implementation,
selected by ``raum.yaml`` (``models.<stage>.engine``). See ``raum.pipeline.engines.build_engines``.

Audio conventions: mono float32 numpy arrays in [-1, 1]. ASR input is 16 kHz.
Denoise works at 48 kHz (DeepFilterNet native). TTS yields int16 PCM chunks.

CONTRACT: these signatures are shared by the pipeline, the agent worker, core (summary) and eval.
"""

from __future__ import annotations

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


@dataclass
class AsrResult:
    text: str
    lang: str
    conf: float  # 0..1 (mean token probability or equivalent)
    ms: float = 0.0
    words: list[dict[str, Any]] = field(default_factory=list)


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

    def synthesize(self, text: str, lang: str) -> AsyncIterator[np.ndarray]:
        """Yield int16 PCM chunks at ``sample_rate``. May yield nothing (mock / off)."""
        ...


@dataclass
class Engines:
    denoise: Denoiser
    asr: ASR
    translate: Translator
    verify: Verifier | None  # None = second-model check off (edge/lite)
    embed: Embedder | None
    tts: TTS | None
    names: dict[str, str] = field(default_factory=dict)  # stage -> model name, for the glass box

    async def aclose(self) -> None:
        for e in (self.asr, self.translate, self.verify, self.embed, self.tts):
            close = getattr(e, "aclose", None)
            if close:
                await close()
