"""Build the engine set from ``raum.yaml`` (``models.<stage>.engine``), plan §6.7.

    engines = build_engines(get_config())

Swapping the mock pipeline for the real models is one line in raum.yaml (``profile: spark``).
"""

from __future__ import annotations

import logging
import random

from raum.config import EngineConfig, RaumConfig
from raum.pipeline.asr import MockASR, WhisperCppASR
from raum.pipeline.base import Engines
from raum.pipeline.denoise import make_denoiser
from raum.pipeline.embed import MockEmbedder, OllamaEmbedder
from raum.pipeline.translate import MockTranslator, OllamaTranslator
from raum.pipeline.tts import make_tts
from raum.pipeline.verifier import MockVerifier, OllamaVerifier
from raum.pipeline.vision import MockVision, OllamaVision

log = logging.getLogger("raum.pipeline.engines")

OLLAMA_URL = "http://127.0.0.1:11434"
WHISPER_URL = "http://127.0.0.1:8081"
WHISPER_ENGINES = ("whisper.cpp", "whispercpp", "whisper")


def _model(ec: EngineConfig, default: str) -> str:
    return ec.model or default


def build_engines(cfg: RaumConfig, *, seed: int | None = None) -> Engines:
    """One engine per stage. Unknown engine names raise ``ValueError``; optional engines whose
    dependency is missing (deepfilternet, piper) fall back with a log line."""
    rng = random.Random(seed)
    m = cfg.models

    denoise = make_denoiser(m.denoise.engine, m.denoise.options)

    if m.asr.engine == "mock":
        asr = MockASR(m.asr.options, model=m.asr.model, rng=rng)
    elif m.asr.engine in WHISPER_ENGINES:
        asr = WhisperCppASR(m.asr.url or WHISPER_URL, model=m.asr.model, options=m.asr.options)
    else:
        raise ValueError(f"unknown asr engine: {m.asr.engine!r}")

    if m.translate.engine == "mock":
        translate = MockTranslator(m.translate.options, model=m.translate.model, rng=rng)
    elif m.translate.engine == "ollama":
        translate = OllamaTranslator(m.translate.url or OLLAMA_URL, model=_model(m.translate, "gemma4:26b"),
                                     options=m.translate.options)
    else:
        raise ValueError(f"unknown translate engine: {m.translate.engine!r}")

    verify: MockVerifier | OllamaVerifier | None
    if not m.verify.enabled:
        verify = None
    elif m.verify.engine == "mock":
        verify = MockVerifier(m.verify.options, model=m.verify.model, rng=rng)
    elif m.verify.engine == "ollama":
        verify = OllamaVerifier(m.verify.url or OLLAMA_URL, model=_model(m.verify, "gpt-oss:120b"),
                                options=m.verify.options)
    else:
        raise ValueError(f"unknown verify engine: {m.verify.engine!r}")

    embed: MockEmbedder | OllamaEmbedder | None
    if not m.embed.enabled:
        embed = None
    elif m.embed.engine == "mock":
        embed = MockEmbedder(m.embed.options, model=m.embed.model)
    elif m.embed.engine == "ollama":
        embed = OllamaEmbedder(m.embed.url or OLLAMA_URL, model=_model(m.embed, "bge-m3"), options=m.embed.options)
    else:
        raise ValueError(f"unknown embed engine: {m.embed.engine!r}")

    tts = make_tts(m.tts.engine, m.tts.options, m.tts.model, url=m.tts.url,
                   fallback=(m.tts.model_extra or {}).get("fallback", "piper")) if m.tts.enabled else None

    names = cfg.engine_names()
    names["denoise"] = denoise.name
    if tts is None:
        names["tts"] = "off"
    if verify is None:
        names["verify"] = "off"
    if embed is None:
        names["embed"] = "off"
    return Engines(denoise=denoise, asr=asr, translate=translate, verify=verify, embed=embed, tts=tts,
                   names=names)


def build_vision(cfg: RaumConfig, *, seed: int | None = None) -> MockVision | OllamaVision | None:
    """The document camera engine (F-07, ``models.vision``); ``None`` when the profile turns it off.
    Built by raum-core (not the agent): a photo is uploaded to core, read once and dropped."""
    ec = cfg.models.vision
    if not ec.enabled:
        return None
    if ec.engine == "mock":
        return MockVision(ec.options, model=ec.model, rng=random.Random(seed))
    if ec.engine == "ollama":
        return OllamaVision(ec.url or OLLAMA_URL, model=_model(ec, "gemma4:26b"), options=ec.options)
    raise ValueError(f"unknown vision engine: {ec.engine!r}")
