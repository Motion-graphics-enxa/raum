"""Transcription (plan §6.3): ``mock`` (canned lines) and ``whisper.cpp`` (HTTP server).

Input audio is mono float32 at 16 kHz.
"""

from __future__ import annotations

import logging
import math
import random
import re
import time
from typing import Any

import httpx
import numpy as np

from raum.pipeline import mockdata
from raum.pipeline._util import float_to_wav, lang_code, sample_ms, sleep_ms
from raum.pipeline.base import AsrResult, TurnContext

log = logging.getLogger("raum.pipeline.asr")

ASR_SAMPLE_RATE = 16000


_TRAILING_PUNCT = re.compile(r"[.,;:!?…،؛؟。！？]+$")
_MAX_ASSIGNED = 256


class MockASR:
    """Picks the next canned line for the speaker's role (per-room cursor per role).

    The line is fixed per utterance: partials reveal its words in proportion to ``speech_ms`` (``word_ms``
    per word), like a streaming recogniser: a word's punctuation shows once the next word is heard, so a
    clause end appears in the partials as the speaker moves past it. The final returns the whole line
    (``final_reveal: true``: only the words said so far, for pause and endpointing tests); partials and
    final carry the line's confidence. Audio content is ignored.
    """

    is_mock = True

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None,
                 rng: random.Random | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-whisper"
        self._rng = rng or random.Random()
        self._cursor: dict[tuple[str, str], int] = {}
        self._assigned: dict[tuple[str, str], mockdata.MockLine] = {}
        self.word_ms = float(self.options.get("word_ms", 380.0))  # speaking rate for partial reveal

    def _script(self, mode: str) -> mockdata.MockScript:
        try:
            return mockdata.load_script(mode)
        except FileNotFoundError:
            return mockdata.load_script("sprechzimmer")

    def line_for(self, turn: TurnContext) -> mockdata.MockLine:
        key = (turn.room, turn.utt)
        line = self._assigned.get(key)
        if line is None:
            script = self._script(turn.mode)
            role = turn.role if turn.role in ("a", "b") else "a"
            lines = script.by_role(role) or script.lines
            ck = (turn.room, role)
            i = self._cursor.get(ck, 0)
            line = lines[i % len(lines)]
            self._cursor[ck] = i + 1
            self._assigned[key] = line
            while len(self._assigned) > _MAX_ASSIGNED:  # oldest utterances first
                self._assigned.pop(next(iter(self._assigned)))
        return line

    def reset(self, room: str) -> None:
        for k in [k for k in self._cursor if k[0] == room]:
            del self._cursor[k]
        for k in [k for k in self._assigned if k[0] == room]:
            del self._assigned[k]

    async def transcribe(self, audio: np.ndarray, *, lang_hint: str | None, partial: bool,
                         turn: TurnContext) -> AsrResult:
        t0 = time.perf_counter()
        line = self.line_for(turn)
        lang = line.lang_for(lang_code(lang_hint))
        text = line.source(lang)
        if partial or self.options.get("final_reveal"):  # final_reveal: the final has only what was said
            speech_ms = turn.speech_ms or (len(audio) / ASR_SAMPLE_RATE * 1000.0 if audio is not None else 0.0)
            words = text.split()
            expected = max(1.0, len(words) * self.word_ms)
            n = max(1, math.ceil(min(1.0, speech_ms / expected) * len(words)))
            shown = words[:n]
            if n < len(words):  # the last word is still being heard: its punctuation is not known yet
                shown[-1] = _TRAILING_PUNCT.sub("", shown[-1]) or shown[-1]
            text = " ".join(shown)
        if partial:
            spec = self.options.get("partial_latency_ms")
            delay = sample_ms(spec, rng=self._rng) if spec is not None else \
                0.6 * sample_ms(self.options.get("latency_ms"), rng=self._rng)
        else:  # the line stays assigned: a merged utterance (same id) continues it
            delay = sample_ms(self.options.get("latency_ms"), rng=self._rng)
        await sleep_ms(delay)
        return AsrResult(text=text, lang=lang, conf=line.conf, ms=(time.perf_counter() - t0) * 1000.0,
                         has_conf=True)


class WhisperCppASR:
    """whisper.cpp ``server``: ``POST {url}/inference`` (multipart WAV, ``response_format=verbose_json``)."""

    is_mock = False

    def __init__(self, url: str, *, model: str | None = None, options: dict[str, Any] | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.name = f"whisper.cpp:{model}" if model else "whisper.cpp"
        self.options = options or {}
        timeout = httpx.Timeout(float(self.options.get("timeout_s", 15.0)), connect=2.0)
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def transcribe(self, audio: np.ndarray, *, lang_hint: str | None, partial: bool,
                         turn: TurnContext) -> AsrResult:
        t0 = time.perf_counter()
        hint = lang_code(lang_hint)
        if hint == "auto":
            hint = None
        if audio is None or len(audio) == 0:
            return AsrResult(text="", lang=hint or "", conf=0.0, ms=0.0)
        data = {
            "response_format": "verbose_json",
            "temperature": str(self.options.get("temperature", 0.0)),
            "language": hint or "auto",
        }
        prompt = whisper_prompt(self.options.get("prompt"), getattr(turn, "asr_prompt", None))
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("audio.wav", float_to_wav(audio, ASR_SAMPLE_RATE), "audio/wav")}
        r = await self._client.post(f"{self.url}/inference", data=data, files=files)
        r.raise_for_status()
        j = r.json()
        if isinstance(j, dict) and j.get("error"):
            raise RuntimeError(f"whisper.cpp: {j['error']}")
        text = str(j.get("text", "")).strip()
        lang = lang_code(j.get("language") or j.get("detected_language")) or hint or ""
        conf, words = _confidence(j)
        return AsrResult(text=text, lang=lang, conf=conf, ms=(time.perf_counter() - t0) * 1000.0, words=words,
                         has_conf=_has_scores(j))

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()


def whisper_prompt(configured: Any, glossary: str | None) -> str:
    """whisper's ``prompt``: the configured one (``models.asr.options.prompt``), then the room glossary's
    terms (``TurnContext.asr_prompt``). The glossary goes last because whisper.cpp keeps only the last
    n_text_ctx / 2 (224) prompt tokens, dropping from the front, and the text nearest the audio conditions the
    decoder most: the spelling hints survive a long configured prompt and weigh the most."""
    return " ".join(s for s in (str(configured or "").strip(), (glossary or "").strip()) if s)


def _confidence(j: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """Mean token probability from a verbose_json response (words / tokens / avg_logprob)."""
    probs: list[float] = []
    words: list[dict[str, Any]] = []
    logprobs: list[float] = []
    for seg in j.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        for w in seg.get("words") or []:
            if isinstance(w, dict):
                p = w.get("probability", w.get("p"))
                if isinstance(p, (int, float)):
                    probs.append(float(p))
                words.append(w)
        if not words:
            for t in seg.get("tokens") or []:
                if isinstance(t, dict):
                    p = t.get("p", t.get("probability"))
                    text = str(t.get("text", ""))
                    if isinstance(p, (int, float)) and not text.startswith(("[_", "<|")):
                        probs.append(float(p))
        lp = seg.get("avg_logprob")
        if isinstance(lp, (int, float)):
            logprobs.append(float(lp))
    if probs:
        return max(0.0, min(1.0, sum(probs) / len(probs))), words
    if logprobs:
        return max(0.0, min(1.0, math.exp(sum(logprobs) / len(logprobs)))), words
    return 0.9, words  # no scores in this server build: don't block the turn


def _has_scores(j: dict[str, Any]) -> bool:
    """Whether a verbose_json response carries any score ``_confidence`` can use (else conf is 0.9)."""
    for seg in j.get("segments") or []:
        if not isinstance(seg, dict):
            continue
        if isinstance(seg.get("avg_logprob"), (int, float)):
            return True
        for item in (seg.get("words") or []) + (seg.get("tokens") or []):
            if isinstance(item, dict) and isinstance(item.get("probability", item.get("p")), (int, float)):
                return True
    return False
