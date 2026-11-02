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

from raum.pipeline import mockdata, stats
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
    """whisper.cpp ``server``: ``POST {url}/inference`` (multipart WAV, ``response_format=verbose_json``).

    ``options.final_url``: a second server (same model) for every non-partial call: the pause's early final, the
    turn's final, the voice echo check. whisper.cpp runs one request at a time, so on one server the final queued
    behind a running partial (box: 515 ms wall for a 0.16 s clip, the end of turn and the pre-render waiting on
    it). Partials stay on ``url``. If the final server does not answer, that call goes to ``url``."""

    is_mock = False

    def __init__(self, url: str, *, model: str | None = None, options: dict[str, Any] | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.name = f"whisper.cpp:{model}" if model else "whisper.cpp"
        self.options = options or {}
        self.final_url = str(self.options.get("final_url") or "").rstrip("/") or None
        self._final_down_until = 0.0  # the final server refused a connection: finals on url for a while
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
            # whisper.cpp fills verbose_json's language_probabilities with a second encoder pass; RAUM never
            # reads them (box: de1 0.16 s without). Also the server flag -nlp (deploy/box.sh); older servers ignore it
            "no_language_probabilities": "true",
        }
        seed = code_switch_seed(hint, self.options.get("code_switch_seed"))
        prompt = whisper_prompt(self.options.get("prompt"), getattr(turn, "asr_prompt", None), seed=seed)
        if prompt:
            data["prompt"] = prompt
        files = {"file": ("audio.wav", float_to_wav(audio, ASR_SAMPLE_RATE), "audio/wav")}
        # stage "asr" / "asr.partial" (pipeline/stats.py): wall_ms and audio_ms only. whisper.cpp's verbose_json
        # carries no processing timings (its server holds one mutex per request; a wait for it is inside wall_ms).
        t_post = time.monotonic()
        failed = 1
        url = self.url
        if not partial and self.final_url and t_post >= self._final_down_until:
            url = self.final_url
        try:
            try:
                r = await self._client.post(f"{url}/inference", data=data, files=files)
            except httpx.ConnectError:
                if url == self.url:
                    raise
                log.warning("whisper final server %s refused the connection: finals on %s for 30 s",
                            self.final_url, self.url)
                self._final_down_until = time.monotonic() + 30.0
                r = await self._client.post(f"{self.url}/inference", data=data, files=files)
            r.raise_for_status()
            j = r.json()
            failed = 0
        finally:
            stats.note("asr.partial" if partial else "asr", wall_ms=(time.monotonic() - t_post) * 1000.0,
                       audio_ms=len(audio) / ASR_SAMPLE_RATE * 1000.0, error=failed or None)
        if isinstance(j, dict) and j.get("error"):
            raise RuntimeError(f"whisper.cpp: {j['error']}")
        text = strip_seed(_whisper_text(j), seed) if prompt and seed and seed in prompt else _whisper_text(j)
        lang = lang_code(j.get("language") or j.get("detected_language")) or hint or ""
        conf, words = _confidence(j)
        return AsrResult(text=text, lang=lang, conf=conf, ms=(time.perf_counter() - t0) * 1000.0, words=words,
                         has_conf=_has_scores(j))

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()


# A speaker's sentence with an English phrase in it, before the glossary terms. An all-Cyrillic (or all-German)
# term list made whisper drop an English phrase said inside Russian speech, or spell it in Cyrillic
# ("Батайсинк и Тейснотворкинг"); with this sentence first it keeps it in Latin (box, mix-ru / mix2-ru, ru + auto).
# Only with glossary terms (no prompt: whisper keeps the English anyway). English speakers: none.
CODE_SWITCH_SEEDS: dict[str, str] = {
    "ru": "Доктор, I think it is not working.",
    "de": "Herr Doktor, I think it is not working.",
    "uk": "Лікарю, I think it is not working.",
    "tr": "Doktor bey, I think it is not working.",
    "ar": "دكتور، I think it is not working.",
}


def code_switch_seed(lang: str | None, configured: Any = None) -> str:
    """The seed sentence for a speaker of ``lang``: ``asr.options.code_switch_seed`` ({lang: text}, "" = none;
    ``false`` = off) over the built-in ones."""
    if configured is False:
        return ""
    code = (lang or "").split("-")[0].lower()
    if isinstance(configured, dict) and code in configured:
        return str(configured[code] or "").strip()
    return CODE_SWITCH_SEEDS.get(code, "")


def whisper_prompt(configured: Any, glossary: str | None, *, seed: str = "") -> str:
    """whisper's ``prompt``: the configured one (``models.asr.options.prompt``), the code-switch seed (only with
    glossary terms), then the room glossary's terms (``TurnContext.asr_prompt``). The glossary goes last because
    whisper.cpp keeps only the last n_text_ctx / 2 (224) prompt tokens, dropping from the front, and the text
    nearest the audio conditions the decoder most: the spelling hints survive a long configured prompt and weigh
    the most."""
    terms = (glossary or "").strip()
    return " ".join(s for s in (str(configured or "").strip(), seed.strip() if terms else "", terms) if s)


def strip_seed(text: str, seed: str) -> str:
    """``text`` without an echo of the seed sentence (whisper can repeat its prompt on near-silent audio)."""
    if not seed or not text:
        return text
    pat = re.compile(r"\s*".join(re.escape(w) for w in seed.split()), re.IGNORECASE)
    out = pat.sub(" ", text)
    return text if out == text else _SPACES.sub(" ", out).strip()


_SPACES = re.compile(r"\s+")


def _whisper_text(j: dict[str, Any]) -> str:
    """The transcript of a verbose_json response. whisper.cpp's top-level ``text`` joins the segments with
    "\n", and a segment boundary can fall inside a word ("Frühst\nück."): concatenate the segment texts as
    they are (each carries its own leading space), then collapse whitespace."""
    segs = [s for s in (j.get("segments") or []) if isinstance(s, dict) and isinstance(s.get("text"), str)]
    raw = "".join(s["text"] for s in segs) if segs else str(j.get("text", "")).replace("\n", "")
    return _SPACES.sub(" ", raw).strip()


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
