"""Small helpers shared by the engines and the room pipeline."""

from __future__ import annotations

import asyncio
import io
import random
import re
import wave
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from raum.config import repo_root

# BCP-47 primary subtag -> English name, used in model prompts (LLMs do better with names than codes).
LANG_NAMES: dict[str, str] = {
    "de": "German", "en": "English", "tr": "Turkish", "ru": "Russian", "ar": "Arabic", "uk": "Ukrainian",
    "fa": "Persian", "pl": "Polish", "ro": "Romanian", "it": "Italian", "fr": "French", "es": "Spanish",
    "pt": "Portuguese", "ku": "Kurdish", "sq": "Albanian", "sr": "Serbian", "hr": "Croatian", "bs": "Bosnian",
    "bg": "Bulgarian", "el": "Greek", "vi": "Vietnamese", "zh": "Chinese", "ja": "Japanese", "ko": "Korean",
    "hi": "Hindi", "ur": "Urdu", "ps": "Pashto", "ti": "Tigrinya", "so": "Somali", "nl": "Dutch",
    "hu": "Hungarian", "cs": "Czech", "sk": "Slovak", "ka": "Georgian", "hy": "Armenian", "az": "Azerbaijani",
}

# whisper.cpp reports full lower-case names ("german"); map back to codes.
_NAME_TO_CODE = {v.lower(): k for k, v in LANG_NAMES.items()}


def lang_name(code: str | None) -> str:
    if not code:
        return "the source language"
    return LANG_NAMES.get(code, code)


def lang_code(value: str | None) -> str | None:
    """Normalise "German" / "german" / "de" / "de-DE" to "de"."""
    if not value:
        return None
    v = value.strip()
    low = v.lower()
    if low in _NAME_TO_CODE:
        return _NAME_TO_CODE[low]
    return low.split("-")[0].split("_")[0] or None


def sample_ms(spec: Any, default: float = 0.0, rng: random.Random | None = None) -> float:
    """A latency spec from a profile: ``250`` (fixed) or ``[150, 300]`` (uniform)."""
    if spec is None:
        return default
    if isinstance(spec, (int, float)):
        return float(spec)
    if isinstance(spec, (list, tuple)) and len(spec) == 2:
        lo, hi = float(spec[0]), float(spec[1])
        return (rng or random).uniform(min(lo, hi), max(lo, hi))
    return default


async def sleep_ms(ms: float) -> None:
    if ms > 0:
        await asyncio.sleep(ms / 1000.0)
    else:
        await asyncio.sleep(0)


def float_to_wav(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Mono float32 [-1, 1] -> 16-bit PCM WAV bytes."""
    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    pcm = (np.clip(a, -1.0, 1.0) * 32767.0).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def int16_to_float(pcm: np.ndarray | bytes | memoryview) -> np.ndarray:
    a = np.frombuffer(pcm, dtype=np.int16) if not isinstance(pcm, np.ndarray) else pcm.astype(np.int16)
    return (a.astype(np.float32) / 32768.0).reshape(-1)


def float_to_int16(audio: np.ndarray) -> np.ndarray:
    return (np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0) * 32767.0).astype(np.int16)


# ---------------------------------------------------------------- prompts

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@lru_cache(maxsize=32)
def _read_prompt(path: str) -> str:
    p = Path(path)
    if not p.is_absolute():
        p = repo_root() / p
    return p.read_text(encoding="utf-8").strip()


def load_prompt(path: str | Path) -> str:
    return _read_prompt(str(path))


def render_prompt(template: str, **values: Any) -> str:
    """Fill ``{name}`` slots that are given; leave everything else (e.g. literal JSON) untouched."""

    def sub(m: re.Match[str]) -> str:
        key = m.group(1)
        return str(values[key]) if key in values else m.group(0)

    return _PLACEHOLDER.sub(sub, template)


def engine_options(options: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    return {k: v for k, v in (options or {}).items() if k in allowed}
