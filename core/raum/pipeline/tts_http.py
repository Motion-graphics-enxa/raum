"""``engine: http``: a generic streaming TTS service, e.g. the local GPU voice-cloning service on :8090.

    POST {url}/tts   JSON {text, lang, rate?, model?, sample_rate, voice_ref? (base64 WAV), voice_id?,
                           priority? ("background" for a speculative clause; absent = live)}
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

``synthesize(..., background=True)``: a speculative clause, made before the speaker's end of turn. The service
refuses it at once while live speech runs or waits (409), and cuts it when live speech arrives mid-clause, so
it never delays a live clause (services/tts/API.md, "Priorities"). That raises :class:`VoiceBusy`. Whatever
fails, a background call never puts the service aside (``retry_s``) and never uses the fallback: the live path
synthesises that clause. A service that answers without ``X-Priority`` (older than v1.3) would serve a
background request as live, so once a response shows that, background calls raise VoiceBusy unsent.

Several instances (``options.urls: [http://127.0.0.1:8090, http://127.0.0.1:8091]``; unset: ``url`` alone, exactly
as before). The service makes one clause at a time, for every room; with several rooms live, one room's clause was
refused behind another's and spoken by Piper, so voices mixed mid-utterance. Each request picks an instance: one
that ``/health`` says is idle (a quick probe, ``options.probe_s``, only when there is a choice; rooms run in their
own processes, so this client cannot count the other rooms' requests), then the one with the fewest requests of
this client open, then the order of ``urls``. A busy refusal (503/429) or any other failure before audio tries
the next instance first; only when every instance failed does the clause go to the fallback and the service as a
whole is put aside for ``retry_s``. "Down" is per instance: one instance down never puts the others aside. A
background clause tries the instances the same way and raises VoiceBusy only when all refuse. ``forget`` and
``warmup`` go to every instance (a voice may have been registered on any of them).

PRIVACY: the voice reference goes to this local service only. Errors carry status codes, never
request or response bodies.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import inspect
import logging
import struct
import time
from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from typing import Any

import httpx
import numpy as np

from raum.pipeline import stats, voicegate

log = logging.getLogger("raum.pipeline.tts.http")

DEFAULT_URL = "http://127.0.0.1:8090"
WARMUP_TIMEOUT_S = 300.0
FORGET_TIMEOUT_S = 2.0  # per DELETE; burn never waits longer than this per voice
WARMUP_TEXT = "Hello."
PROBE_S = 0.1  # GET /health per instance before a live clause, when there are several to choose from
BUSY_S = 1.0  # an instance that refused as busy comes after the others for this long (it is not put aside)


class TtsServiceError(RuntimeError):
    def __init__(self, message: str = "", status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # the HTTP status, if the service answered with one


class VoiceBusy(TtsServiceError):
    """A background clause (``synthesize(..., background=True)``) was not made: the service is busy with live
    speech (HTTP 409; 429/503 too), a live request cut the stream mid-clause, or the service is put aside after
    a failure / not warm yet. Expected, not a fault: the live path synthesises the clause. It may come after
    some chunks were yielded (a cut stream): discard them."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"voice busy: {reason}")
        self.reason = reason


BUSY_STATUSES = frozenset({409, 429, 503})  # a background request refused for live speech (503: an older service)
LIVE_BUSY_STATUSES = frozenset({429, 503})  # a live request refused at admission (queue too long, predicted wait)


@dataclass
class _Instance:
    """One instance of the voice service (``options.urls``) as this client sees it."""

    url: str
    index: int
    inflight: int = 0  # this client's requests open on it (live and background)
    down_until: float = 0.0  # put aside after a failure, for retry_s: per instance
    busy_until: float = 0.0  # refused as busy: the others first until then
    priorities: bool | None = None  # honours request priorities (sends X-Priority, v1.3+)? None until it answers
    load: int = 0  # the last /health probe: 0 idle, 1 a background job (live preempts it), 2 unknown, 3 busy
    probe_after: float = 0.0  # a probe failed: not probed again (load unknown) until then


def _urls(value: Any) -> list[str]:
    """``options.urls``: a list, or a comma/space separated string; [] when unset."""
    if not value:
        return []
    items = value.replace(",", " ").split() if isinstance(value, str) else list(value)
    return [str(u).strip().rstrip("/") for u in items if str(u).strip()]


def _health_load(h: Any) -> int:
    """How busy ``GET /health`` says an instance is (lower is better; see :class:`_Instance`)."""
    if not isinstance(h, dict) or "busy" not in h:
        return 2
    if h.get("waiting"):
        return 3
    if not h.get("busy"):
        return 0
    return 1 if h.get("busy_priority") == "background" else 3


def voice_id(voice_ref: bytes) -> str:
    """The id the service keeps a speaker's sample under (``DELETE {url}/tts/voices/{id}`` forgets it)."""
    return hashlib.sha256(voice_ref).hexdigest()[:16]


def tts_request(text: str, lang: str, *, voice_ref: bytes | None, rate: float | None, model: str | None,
                options: dict[str, Any], background: bool = False) -> dict[str, Any]:
    """httpx request arguments for one clause: THE mapping to the service API."""
    fields: dict[str, Any] = {"text": text, "lang": lang, "sample_rate": int(options.get("sample_rate", 24000))}
    if rate is not None:
        fields["rate"] = round(float(rate), 2)
    if model:
        fields["model"] = model
    if background:
        fields["priority"] = "background"  # live is the service's default: live requests stay as they were
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
        self.model = model
        self.options = dict(options or {})
        # options.urls: several instances of the service (module docstring); unset: url alone, as before
        urls = list(dict.fromkeys(_urls(self.options.get("urls")) or [url.rstrip("/")]))
        self._inst = [_Instance(u, i) for i, u in enumerate(urls)]
        self.url = urls[0]
        self.probe_s = float(self.options.get("probe_s", PROBE_S))
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
        self.ready = False  # warm (warm-up or a clause went through, on any instance)
        self.gate = False  # set by the agent: until warm, clauses go to the fallback

    @property
    def urls(self) -> list[str]:
        return [i.url for i in self._inst]

    @property
    def _down_until(self) -> float:
        """Until when the service as a whole is put aside: the earliest instance back (0.0: one never failed)."""
        return min(i.down_until for i in self._inst)

    @_down_until.setter
    def _down_until(self, value: float) -> None:
        for i in self._inst:
            i.down_until = value

    @property
    def priorities(self) -> bool | None:
        """Does the service honour request priorities (it sends X-Priority, v1.3+)? None until a response tells.
        An older one would treat a background request as live and make live clauses wait behind it. Per instance;
        this is True if any instance does."""
        seen = {i.priorities for i in self._inst}
        return True if True in seen else False if False in seen else None

    @priorities.setter
    def priorities(self, value: bool | None) -> None:
        for i in self._inst:
            i.priorities = value

    # ------------------------------------------------------------ lifecycle

    def prepare(self, langs: Iterable[str]) -> list[str]:
        """Room join: the fallback's resident voices (the service itself is warmed by ``warmup``)."""
        prepare = getattr(self.fallback, "prepare", None)
        return list(prepare(langs)) if callable(prepare) else []

    async def warmup(self) -> None:
        """Warm every instance. Ready as soon as one is; raises only if every instance failed (one that failed
        while another is warm is put aside for ``retry_s``)."""
        if len(self._inst) == 1:
            await self._warm(self._inst[0])
            return
        res = await asyncio.gather(*(self._warm(i) for i in self._inst), return_exceptions=True)
        failed = [(i, r) for i, r in zip(self._inst, res) if isinstance(r, BaseException)]
        if len(failed) == len(res):
            raise failed[0][1]
        for i, e in failed:
            i.down_until = time.monotonic() + self.retry_s
            log.warning("tts service %s: warm-up failed (%s); the other instances speak", i.url, _reason(e))

    async def _warm(self, inst: _Instance) -> None:
        req = tts_request(WARMUP_TEXT, self.warmup_lang, voice_ref=None, rate=None, model=self.model,
                          options=self.options)
        async with self._client.stream("POST", inst.url + self.path, timeout=httpx.Timeout(WARMUP_TIMEOUT_S, connect=5.0),
                                       **req) as r:
            if r.status_code >= 400:
                raise TtsServiceError(f"tts warm-up: HTTP {r.status_code}", r.status_code)
            inst.priorities = "x-priority" in r.headers
            async for _ in r.aiter_bytes():
                pass
        self.ready = True

    async def forget(self, voice_ids: Iterable[str]) -> int:
        """Make the service drop these speakers' samples (session end, burn, a device gone for good), on every
        instance: a sample may have been registered on any of them. 204 and 404 both mean gone; a voice counts as
        gone when every instance said so. Fail-soft: logs the id prefix and the error class, never a body."""
        gone = 0
        for vid in dict.fromkeys(voice_ids):
            done = await asyncio.gather(*(self._forget(i, vid) for i in self._inst))
            gone += all(done)
        return gone

    async def _forget(self, inst: _Instance, vid: str) -> bool:
        where = f" on {inst.url}" if len(self._inst) > 1 else ""
        try:
            r = await self._client.delete(f"{inst.url}{self.path}/voices/{vid}",
                                          timeout=httpx.Timeout(FORGET_TIMEOUT_S, connect=1.0))
        except Exception as e:  # noqa: BLE001
            log.warning("tts: could not delete voice %s…%s (%s)", vid[:4], where, _reason(e))
            return False
        if r.status_code in (200, 204, 404):
            return True
        log.warning("tts: deleting voice %s…%s returned HTTP %d", vid[:4], where, r.status_code)
        return False

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
                         rate: float | None = None, background: bool = False) -> AsyncIterator[np.ndarray]:
        """``background``: a speculative clause at the service's low priority. Raises :class:`VoiceBusy` when it is
        not made (module docstring); never falls back, never puts the service aside."""
        if not text.strip():
            return
        if background:
            async for chunk in self._background(text, lang, voice_ref, rate):
                yield chunk
            return
        if not self._skip_service():
            order = await self._order(background=False)
            err: BaseException | None = None
            for k, inst in enumerate(order):
                spoke = False
                more = k + 1 < len(order)
                try:
                    # aclosing: a listener that stops (barge-in) closes the request now, not when GC finds it
                    async with contextlib.aclosing(self._stream(text, lang, voice_ref, rate, inst=inst,
                                                                more=more)) as stream:
                        async for chunk in stream:
                            spoke = True
                            yield chunk
                    self.ready = True
                    return
                except (httpx.HTTPError, TtsServiceError, OSError, ValueError) as e:
                    err = e
                    now = time.monotonic()
                    if not spoke and isinstance(e, TtsServiceError) and e.status in LIVE_BUSY_STATUSES:
                        inst.busy_until = now + BUSY_S  # busy, not broken: the others first for a moment
                    else:
                        inst.down_until = now + self.retry_s  # this instance only
                    if spoke:
                        log.warning("tts service %s failed mid-clause (%s)", inst.url, _reason(e))
                        if self.fallback is None:
                            raise  # the pipeline continues text only (§6.15)
                        return  # half the clause was said: do not repeat it in another voice
                    if more:
                        log.info("tts service %s failed (%s); trying %s", inst.url, _reason(e), order[k + 1].url)
            if order:  # every instance failed: the service as a whole is put aside
                until = time.monotonic() + self.retry_s
                for inst in order:
                    inst.down_until = max(inst.down_until, until)
                log.warning("tts service %s failed (%s)%s", order[-1].url if len(order) > 1 else self.url,
                            _reason(err) if err else "?",
                            "" if self.fallback is None else f"; fallback voice for {self.retry_s:.0f} s")
                if self.fallback is None and err is not None:
                    raise err  # the pipeline continues text only (§6.15)
        async for chunk in self._fallback(text, lang, voice_ref, rate):
            yield chunk

    async def _order(self, *, background: bool) -> list[_Instance]:
        """The instances to try for one request, best first (module docstring). Put-aside instances are left out
        unless there is no fallback (then they come last: nothing else to try)."""
        now = time.monotonic()
        up = [i for i in self._inst if now >= i.down_until]
        if self.fallback is None:
            up += sorted((i for i in self._inst if now < i.down_until), key=lambda i: i.down_until)
        if background:  # an instance without priorities would serve it as live, ahead of the next live clause
            up = [i for i in up if i.priorities is not False]
        probe = not background and len(up) > 1 and self.probe_s > 0
        if probe:
            await self._probe(up)
        lead = len([i for i in up if now >= i.down_until])  # the put-aside tail keeps its place
        head = sorted(up[:lead], key=lambda i: (i.load if probe else 0, now < i.busy_until, i.inflight, i.index))
        return head + up[lead:]

    async def _probe(self, insts: list[_Instance]) -> None:
        """``GET /health`` on each (concurrently, ``probe_s`` at most): busy with another room's live clause?"""
        now = time.monotonic()

        async def one(inst: _Instance) -> None:
            if now < inst.probe_after:
                inst.load = 2
                return
            try:
                r = await self._client.get(inst.url + "/health",
                                           timeout=httpx.Timeout(self.probe_s, connect=self.probe_s))
                inst.load = _health_load(r.json() if r.status_code == 200 else None)
            except Exception:  # noqa: BLE001 -- unknown load: the request itself finds out
                inst.load, inst.probe_after = 2, time.monotonic() + BUSY_S

        await asyncio.gather(*(one(i) for i in insts))

    async def _background(self, text: str, lang: str, voice_ref: bytes | None,
                          rate: float | None) -> AsyncIterator[np.ndarray]:
        """A speculative clause: the service only, no fallback, ``_down_until`` untouched. Tries each instance
        before any audio; raises :class:`VoiceBusy` if one refused as busy, else the last error as it is
        (connection refused, HTTP 500, ...)."""
        if self._skip_service():  # live clauses are on the fallback voice now: do not mix voices ahead of them
            raise VoiceBusy("service put aside" if time.monotonic() < self._down_until else "service not warm")
        order = await self._order(background=True)
        if not order:
            raise VoiceBusy("service without priorities")
        busy: VoiceBusy | None = None
        err: BaseException | None = None
        for k, inst in enumerate(order):
            sent = False
            try:
                async with contextlib.aclosing(self._stream(text, lang, voice_ref, rate, background=True,
                                                            inst=inst)) as stream:
                    async for chunk in stream:
                        sent = True
                        yield chunk
                self.ready = True
                return
            except VoiceBusy as e:
                if sent:
                    raise  # a cut stream: its chunks are out (the caller discards them), no other try
                busy = e
                inst.busy_until = time.monotonic() + BUSY_S
            except (httpx.HTTPError, TtsServiceError, OSError, ValueError) as e:
                if sent:
                    raise
                err = e
        raise busy if busy is not None else err  # type: ignore[misc]

    async def _stream(self, text: str, lang: str, voice_ref: bytes | None, rate: float | None,
                      background: bool = False, *, inst: _Instance | None = None,
                      more: bool = False) -> AsyncIterator[np.ndarray]:
        """Stage ``tts`` / ``tts.bg`` (pipeline/stats.py): ``ttfb_ms`` (request to the first audio chunk),
        ``wall_ms``, ``audio_ms``, ``queue_ms`` (the service's GPU wait, ``X-Queue-Ms``); a refusal (409 etc.;
        a live 503/429 when another instance is tried next, ``more``) notes ``refused=1`` only; a cut background
        stream ``cut=1``, a consumer that stopped ``cancelled=1``, another failure ``error=1``."""
        inst = inst or self._inst[0]
        req = tts_request(text, lang, voice_ref=voice_ref, rate=rate, model=self.model, options=self.options,
                          background=background)
        ctx = contextvars.copy_context()  # a generator closed late (by GC) still notes into this utterance
        t0 = time.monotonic()
        m: dict[str, float | None] = {}
        n = 0
        done = False
        inst.inflight += 1
        try:
            # voice in flight (voicegate.py): deferrable GPU work in every room waits while a live clause synthesises
            async with (contextlib.nullcontext() if background else voicegate.speaking()) as mark, \
                    self._client.stream("POST", inst.url + self.path, **req) as r:
                if r.status_code >= 400:
                    if background and r.status_code in BUSY_STATUSES:
                        m["refused"] = 1
                        raise VoiceBusy(f"HTTP {r.status_code}")
                    if more and r.status_code in LIVE_BUSY_STATUSES:
                        m["refused"] = 1
                    raise TtsServiceError(f"HTTP {r.status_code}", r.status_code)  # no body: it could echo it
                inst.priorities = "x-priority" in r.headers
                q = r.headers.get("x-queue-ms", "")
                m["queue_ms"] = float(q) if q.replace(".", "", 1).isdigit() else None
                pcm = PcmStream(stream_rate(r.headers), self.sample_rate)
                try:
                    async for b in r.aiter_bytes():
                        for chunk in pcm.feed(b):
                            if not n:
                                m["ttfb_ms"] = (time.monotonic() - t0) * 1000.0
                            n += len(chunk)
                            if mark is not None:
                                mark.touch()
                            yield chunk
                except httpx.HTTPError as e:
                    if background:  # the service cuts a background stream when live speech needs the voice
                        m["cut"] = 1
                        raise VoiceBusy("stream cut") from e
                    raise
                for chunk in pcm.flush():
                    if not n:
                        m["ttfb_ms"] = (time.monotonic() - t0) * 1000.0
                    n += len(chunk)
                    yield chunk
            done = True
        except Exception:
            if not m.get("refused") and not m.get("cut"):
                m["error"] = 1
            raise
        finally:
            inst.inflight -= 1
            if m.get("refused"):
                m = {"refused": 1}  # a refusal is no work: its round trip stays out of the sums
            else:
                if not done and "error" not in m and "cut" not in m:
                    m["cancelled"] = 1
                m.update(wall_ms=(time.monotonic() - t0) * 1000.0, audio_ms=n / self.sample_rate * 1000.0)
            ctx.run(stats.note, "tts.bg" if background else "tts", **m)

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
