"""``engine: http``: a generic streaming TTS service, e.g. the local GPU voice-cloning service on :8090.

    POST {url}/tts   JSON {text, lang, rate?, model?, sample_rate, voice_ref? (base64 WAV), voice_id?}
    -> 200, raw PCM int16 mono streamed back; rate from ``X-Sample-Rate`` (or ``audio/L16; rate=N``),
       else ``options.sample_rate``. A WAV stream (RIFF header) is accepted too.

``options.voice_ref_as: multipart`` sends the same fields as form data with the reference as a file
part ``voice_ref``. The whole request/response mapping is :func:`tts_request` and :func:`stream_rate`,
so the service's final API is a local change here.

One long-lived ``httpx.AsyncClient``; ``warmup()`` at room join with a long timeout (a cold model must
not be aborted mid-load, as with Ollama); while the agent's gate is up and the service is not warm,
clauses go to the fallback. On failure before any audio, the clause is spoken by the fallback engine
(Piper) and the service is skipped for ``retry_s``; without a fallback the error reaches the pipeline,
which continues text only (§6.15).

PRIVACY: the voice reference goes to this local service only. Errors carry status codes, never
request or response bodies.
"""

from __future__ import annotations

import base64
import hashlib
import inspect
import logging
import struct
import time
from collections.abc import AsyncIterator, Iterable
from typing import Any

import httpx
import numpy as np

log = logging.getLogger("raum.pipeline.tts.http")

DEFAULT_URL = "http://127.0.0.1:8090"
WARMUP_TIMEOUT_S = 300.0
FORGET_TIMEOUT_S = 2.0  # per DELETE; burn never waits longer than this per voice
WARMUP_TEXT = "Hello."


class TtsServiceError(RuntimeError):
    pass


def voice_id(voice_ref: bytes) -> str:
    """The id the service keeps a speaker's sample under (``DELETE {url}/tts/voices/{id}`` forgets it)."""
    return hashlib.sha256(voice_ref).hexdigest()[:16]


def tts_request(text: str, lang: str, *, voice_ref: bytes | None, rate: float | None, model: str | None,
                options: dict[str, Any]) -> dict[str, Any]:
    """httpx request arguments for one clause: THE mapping to the service API."""
    fields: dict[str, Any] = {"text": text, "lang": lang, "sample_rate": int(options.get("sample_rate", 24000))}
    if rate is not None:
        fields["rate"] = round(float(rate), 2)
    if model:
        fields["model"] = model
    if voice_ref is not None:
        fields["voice_id"] = voice_id(voice_ref)  # lets the service cache the speaker
        if options.get("voice_ref_as") == "multipart":
            return {"data": {k: str(v) for k, v in fields.items()},
                    "files": {"voice_ref": ("voice_ref.wav", voice_ref, "audio/wav")}}
        fields["voice_ref"] = base64.b64encode(voice_ref).decode("ascii")
    return {"json": fields}


def stream_rate(headers: httpx.Headers) -> int | None:
    """Sample rate of the streamed audio, from the response headers."""
    v = headers.get("x-sample-rate")
    if v and v.strip().isdigit():
        return int(v)
    for part in (headers.get("content-type") or "").split(";")[1:]:
        k, _, val = part.strip().partition("=")
        if k.lower() == "rate" and val.strip().isdigit():
            return int(val)
    return None


class PcmStream:
    """Bytes as they arrive -> int16 chunks at ``out_rate`` (odd bytes carried over, a WAV header parsed
    and skipped, resampled if the service speaks at another rate, chunks of at least 20 ms)."""

    def __init__(self, rate: int | None, out_rate: int) -> None:
        self.rate = rate
        self.out_rate = out_rate
        self._buf = b""
        self._head: bytes | None = b""  # None once past a possible WAV header
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._rs: Any = None
        self.min_samples = max(1, out_rate // 50)

    def _wav_header(self, b: bytes) -> bytes | None:
        """Bytes after the header (None: need more). Not a WAV: everything."""
        self._head = (self._head or b"") + b
        h = self._head
        if len(h) < 12:
            return None if h[:4] == b"RIFF"[: len(h)] else self._past(h)
        if h[:4] != b"RIFF" or h[8:12] != b"WAVE":
            return self._past(h)
        i = 12
        while i + 8 <= len(h):
            cid, size = h[i:i + 4], struct.unpack("<I", h[i + 4:i + 8])[0]
            if cid == b"data":
                return self._past(h[i + 8:])
            if cid == b"fmt " and i + 16 <= len(h):
                self.rate = struct.unpack("<I", h[i + 12:i + 16])[0]
            i += 8 + size + (size & 1)
        return None

    def _past(self, rest: bytes) -> bytes:
        self._head = None
        return rest

    def _emit(self, pcm: np.ndarray, final: bool) -> list[np.ndarray]:
        rate = self.rate or self.out_rate
        if rate != self.out_rate and (pcm.size or final):
            if self._rs is None:
                import soxr

                self._rs = soxr.ResampleStream(rate, self.out_rate, 1, dtype="int16")
            pcm = self._rs.resample_chunk(pcm, last=final)
        if pcm.size:
            self._pending.append(pcm)
            self._pending_n += len(pcm)
        if self._pending_n >= self.min_samples or (final and self._pending_n):
            out = np.concatenate(self._pending)
            self._pending, self._pending_n = [], 0
            return [out]
        return []

    def feed(self, b: bytes) -> list[np.ndarray]:
        if self._head is not None:
            rest = self._wav_header(b)
            if rest is None:
                return []
            b = rest
        b = self._buf + b
        cut = len(b) - len(b) % 2
        self._buf = b[cut:]
        return self._emit(np.frombuffer(b[:cut], dtype="<i2").astype(np.int16), final=False) if cut else []

    def flush(self) -> list[np.ndarray]:
        return self._emit(np.zeros(0, dtype=np.int16), final=True)


def _takes_voice_args(fn: Any) -> bool:
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return any(p.kind is p.VAR_KEYWORD for p in params.values()) or {"voice_ref", "rate"} <= set(params)


class HttpTTS:
    is_mock = False
    simulated = False
    uses_voice_ref = True  # the pipeline captures a voice sample per speaker for this engine

    def __init__(self, url: str = DEFAULT_URL, *, model: str | None = None, options: dict[str, Any] | None = None,
                 fallback: Any = None, client: httpx.AsyncClient | None = None) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.options = dict(options or {})
        self.name = f"http:{model}" if model else "http"
        self.sample_rate = int(self.options.get("sample_rate", 24000))
        self.options["sample_rate"] = self.sample_rate
        self.path = str(self.options.get("path", "/tts"))
        self.retry_s = float(self.options.get("retry_s", 15.0))
        self.warmup_lang = str(self.options.get("warmup_lang", "en"))
        self.fallback = fallback
        if fallback is not None and getattr(fallback, "sample_rate", self.sample_rate) != self.sample_rate:
            fallback.sample_rate = self.sample_rate  # Piper resamples to its sample_rate
        timeout = httpx.Timeout(float(self.options.get("timeout_s", 8.0)),
                                connect=float(self.options.get("connect_s", 1.0)))
        self._own = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)
        self.ready = False  # warm (warm-up or a clause went through)
        self.gate = False  # set by the agent: until warm, clauses go to the fallback
        self._down_until = 0.0

    # ------------------------------------------------------------ lifecycle

    def prepare(self, langs: Iterable[str]) -> list[str]:
        """Room join: the fallback's resident voices (the service itself is warmed by ``warmup``)."""
        prepare = getattr(self.fallback, "prepare", None)
        return list(prepare(langs)) if callable(prepare) else []

    async def warmup(self) -> None:
        req = tts_request(WARMUP_TEXT, self.warmup_lang, voice_ref=None, rate=None, model=self.model,
                          options=self.options)
        async with self._client.stream("POST", self.url + self.path, timeout=httpx.Timeout(WARMUP_TIMEOUT_S, connect=5.0),
                                       **req) as r:
            if r.status_code >= 400:
                raise TtsServiceError(f"tts warm-up: HTTP {r.status_code}")
            async for _ in r.aiter_bytes():
                pass
        self.ready = True

    async def forget(self, voice_ids: Iterable[str]) -> int:
        """Make the service drop these speakers' samples (session end, burn, a device gone for good).
        204 and 404 both mean gone. Fail-soft: logs the id prefix and the error class, never a body."""
        gone = 0
        for vid in dict.fromkeys(voice_ids):
            try:
                r = await self._client.delete(f"{self.url}{self.path}/voices/{vid}",
                                              timeout=httpx.Timeout(FORGET_TIMEOUT_S, connect=1.0))
            except Exception as e:  # noqa: BLE001
                log.warning("tts: could not delete voice %s… (%s)", vid[:4], _reason(e))
                continue
            if r.status_code in (200, 204, 404):
                gone += 1
            else:
                log.warning("tts: deleting voice %s… returned HTTP %d", vid[:4], r.status_code)
        return gone

    async def aclose(self) -> None:
        if self._own:
            await self._client.aclose()
        close = getattr(self.fallback, "aclose", None)
        if callable(close):
            await close()

    # ------------------------------------------------------------ speech

    def _skip_service(self) -> bool:
        if self.fallback is None:
            return False  # nothing else to try: always ask the service
        return time.monotonic() < self._down_until or (self.gate and not self.ready)

    async def synthesize(self, text: str, lang: str, *, voice_ref: bytes | None = None,
                         rate: float | None = None) -> AsyncIterator[np.ndarray]:
        if not text.strip():
            return
        if not self._skip_service():
            spoke = False
            try:
                async for chunk in self._stream(text, lang, voice_ref, rate):
                    spoke = True
                    yield chunk
                self.ready = True
                return
            except (httpx.HTTPError, TtsServiceError, OSError, ValueError) as e:
                self._down_until = time.monotonic() + self.retry_s
                log.warning("tts service %s failed (%s)%s", self.url, _reason(e),
                            "" if self.fallback is None else f"; fallback voice for {self.retry_s:.0f} s")
                if self.fallback is None:
                    raise  # the pipeline continues text only (§6.15)
                if spoke:
                    return  # half the clause was said: do not repeat it in another voice
        async for chunk in self._fallback(text, lang, voice_ref, rate):
            yield chunk

    async def _stream(self, text: str, lang: str, voice_ref: bytes | None, rate: float | None) -> AsyncIterator[np.ndarray]:
        req = tts_request(text, lang, voice_ref=voice_ref, rate=rate, model=self.model, options=self.options)
        async with self._client.stream("POST", self.url + self.path, **req) as r:
            if r.status_code >= 400:
                raise TtsServiceError(f"HTTP {r.status_code}")  # no body: it could echo the request
            pcm = PcmStream(stream_rate(r.headers), self.sample_rate)
            async for b in r.aiter_bytes():
                for chunk in pcm.feed(b):
                    yield chunk
            for chunk in pcm.flush():
                yield chunk

    async def _fallback(self, text: str, lang: str, voice_ref: bytes | None, rate: float | None) -> AsyncIterator[np.ndarray]:
        fb = self.fallback
        if fb is None:
            return
        kwargs = {"voice_ref": voice_ref, "rate": rate} if _takes_voice_args(fb.synthesize) else {}
        async for chunk in fb.synthesize(text, lang, **kwargs):
            yield chunk


def _reason(e: BaseException) -> str:
    """Error class and message only: never a request or response body."""
    msg = str(e) if isinstance(e, TtsServiceError) else ""
    return f"{e.__class__.__name__}{': ' + msg if msg else ''}"
