"""RAUM local streaming TTS service (Chatterbox Multilingual V3, MIT).

Listens on 127.0.0.1:8090 only. Speaker voices live in process memory only (never written to disk).
See API.md next to this file.

Concurrency model (v1.2 after the 2 Nov hangs; v1.3 adds request priorities, admission.py):
- One GPU job at a time. A job runs in its own worker thread and pushes PCM into an in-memory buffer; it
  releases the GPU as soon as synthesis ends, whatever pace the client reads at. (Before, the GPU lock was
  held by a generator suspended at ``yield`` while RAUM played or paused the audio, so a paused or vanished
  listener blocked every other request for minutes.)
- Admission: a request that would wait more than ~1 s for the GPU gets 503 at once, before any header or
  audio, so RAUM's client speaks that clause with Piper.
- Priorities: ``"priority": "background"`` (a speculative clause) is admitted only when the GPU is free right now
  and no live request waits, else 409 at once; it never counts as waiting. A live request preempts a running
  background job at its next token step; if audio already went out, that stream is cut (no final chunk), so the
  client sees an incomplete body instead of a clause that ends early.
- Headers go out only with the first audio chunk. A failure before audio is an HTTP error (503), never an
  empty 200, so the client always falls back.
- Caps per request: audio <= min(2.5 s + 0.12 s/char x script factor, 20 s); wall clock <= 2 s + 1.5 x the
  audio produced so far (and <= 2 s + 1.5 x the audio cap). On a cap the stream ends cleanly ("cap hit").
- A client that goes away cancels its job within one token step (~20 ms).
"""
import asyncio, base64, binascii, collections, hashlib, io, logging, os, re, sys, threading, time, uuid
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
import anyio
import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, Request, HTTPException, Response
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Literal

from admission import BACKGROUND, LIVE, Gate, QuietCuts, StreamCut, priority_of
from cb_engine import CBEngine, SR
from textnorm import normalize_text

log = logging.getLogger("raum-tts")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logging.getLogger("uvicorn.error").addFilter(QuietCuts())  # a cut background stream is not an app error

MAX_VOICES = int(os.environ.get("RAUM_TTS_MAX_VOICES", "64"))
MIN_REF_S, MAX_REF_S = 2.0, 30.0
LANGS_PRIMARY = ["de", "en", "ru", "tr", "ar"]
TOKENS_PER_S = 25

# --- limits (env-tunable) ---
ADMIT_WAIT_S = float(os.environ.get("RAUM_TTS_ADMIT_WAIT_S", "1.0"))      # longer predicted wait -> 503
GRACE_S = 0.25                                                           # always wait this long first
FIRST_AUDIO_S = float(os.environ.get("RAUM_TTS_FIRST_AUDIO_S", "3.0"))   # no audio by then -> 503
WALL_BASE_S, WALL_FACTOR = 2.0, 1.5                                      # wall <= 2 s + 1.5 x audio
AUDIO_BASE_S, AUDIO_PER_CHAR_S = 2.5, 0.12
AUDIO_CAP_TTS_S = float(os.environ.get("RAUM_TTS_MAX_AUDIO_S", "20"))     # /tts (RAUM clauses)
AUDIO_CAP_SPEAK_S = 60.0                                                 # /v1/speak (multi-sentence)
# speech per character differs by script: Arabic omits short vowels, ru/tr words are long
SCRIPT = {"ar": 1.4, "ru": 1.15, "tr": 1.15, "he": 1.3, "zh": 3.0, "ja": 2.5, "ko": 2.0}
RATE_MIN, RATE_MAX = 0.8, 1.3   # RAUM sends the speaker's pace (0.7-1.5); beyond this the stretch sounds bad
# background text longer than this -> 409: bounds the one vocode a preempted job may still finish
BG_MAX_CHARS = int(os.environ.get("RAUM_TTS_BG_MAX_CHARS", "400"))

t_start = time.time()
engine = CBEngine()
gate = Gate(admit_wait_s=ADMIT_WAIT_S, grace_s=GRACE_S, bg_max_chars=BG_MAX_CHARS)   # the GPU, by priority
voices: "collections.OrderedDict[str, dict]" = collections.OrderedDict()
voices_lock = threading.Lock()
stats = collections.Counter()
current = {"job": None}      # the job holding the GPU

# Default voice when a request brings no speaker reference: the built-in Chatterbox voice (MIT, ships with the
# weights). An operator can drop a consented 8-10 s recording at $RAUM_TTS_DEFAULT_VOICES/<lang>.wav to give a
# language its own voice. (The macOS `say` clips are not used: Apple licenses system voices for personal,
# non-commercial use only.)
DEFAULT_DIR = os.environ.get("RAUM_TTS_DEFAULT_VOICES", "/work/voices")
BUILTIN_LANG = "en"
default_voices: dict = {}
# cfg_weight for a reference in another (or unknown) language. Resemble suggests 0 against accent leakage, but on
# short clauses 0 and 0.3 made the model babble or repeat (17/24 and 19/24 clean vs 22/24 at 0.5).
XLING_CFG = float(os.environ.get("RAUM_TTS_XLING_CFG", "0.5"))
SAME_CFG = 0.5


def _prep_ref(wav, sr):
    wav = np.asarray(wav, dtype=np.float32).reshape(-1)[: int(MAX_REF_S * sr)]
    peak = float(np.abs(wav).max()) if wav.size else 0.0
    return wav / peak * 0.9 if peak > 0 else wav


def _load_default_voices():
    if not os.path.isdir(DEFAULT_DIR):
        return
    for fn in sorted(os.listdir(DEFAULT_DIR)):
        lang, ext = os.path.splitext(fn)
        if ext.lower() not in (".wav", ".flac") or lang not in engine.langs:
            continue
        try:
            wav, sr = sf.read(os.path.join(DEFAULT_DIR, fn), dtype="float32", always_2d=True)
            default_voices[lang] = engine.make_voice(_prep_ref(wav.mean(axis=1), sr), sr)
            log.info("default voice for %s from %s", lang, fn)
        except Exception as e:  # noqa: BLE001
            log.warning("default voice %s unusable: %s", fn, e.__class__.__name__)


def _default_voice(lang):
    """(voice, cfg_weight) for requests without a speaker voice."""
    if lang in default_voices:
        return default_voices[lang], SAME_CFG
    return None, (SAME_CFG if lang == BUILTIN_LANG else XLING_CFG)


def _cfg_for(voice_lang, lang):
    return SAME_CFG if (voice_lang and voice_lang == lang) else XLING_CFG


def max_audio_s(text, lang, cap):
    return min(AUDIO_BASE_S + AUDIO_PER_CHAR_S * len(text) * SCRIPT.get(lang, 1.0), cap)


def expected_job_s(text, lang):
    """Rough synthesis time, for admission: ~0.065 s of speech per char at RTF ~0.6, plus overhead."""
    return 0.3 + 0.6 * (0.4 + 0.065 * len(text) * SCRIPT.get(lang, 1.0))


def _warm():
    """Warm every code path (synthesis, voice embedding, resampling) so the first real request is fast."""
    t = time.perf_counter()
    audio = []
    for lang, txt in [("de", "Guten Tag, wie geht es Ihnen heute? Ich hoffe, gut."),
                      ("en", "Good morning, how are you today?")]:
        audio = [c for c in engine.stream(txt, lang, None)]
    ref = np.concatenate(audio)
    import librosa
    for sr in (48000, 16000):  # the common LiveKit / ASR rates
        v = engine.make_voice(librosa.resample(ref, orig_sr=SR, target_sr=sr), sr)
    for _ in engine.stream("Danke.", "de", v):
        pass
    del v
    log.info("warm in %.1fs", time.perf_counter() - t)


_warm()
_load_default_voices()
app = FastAPI(title="raum-tts", version="1.3")


# ---------------- helpers ----------------
def _decode_audio(body: bytes, sample_rate: int | None):
    if body[:4] in (b"RIFF", b"fLaC", b"OggS"):
        wav, sr = sf.read(io.BytesIO(body), dtype="float32", always_2d=True)
        return wav.mean(axis=1), sr
    if not sample_rate:
        raise HTTPException(400, "raw PCM needs ?sample_rate=<hz> (s16le mono); or send a WAV/FLAC file")
    if len(body) % 2:
        body = body[:-1]
    return np.frombuffer(body, dtype="<i2").astype(np.float32) / 32768.0, int(sample_rate)


_SPLIT = re.compile(r"(?<=[.!?;:。？！؟])\s+")


def _sentences(text: str, max_chars=160):
    out = []
    for s in _SPLIT.split(text.strip()):
        s = s.strip()
        while len(s) > max_chars:  # very long sentence: cut at the last comma/space before the limit
            cut = max(s.rfind(",", 0, max_chars), s.rfind("،", 0, max_chars))
            cut = cut if cut > 40 else s.rfind(" ", 0, max_chars)
            cut = cut if cut > 0 else max_chars
            out.append(s[:cut + 1].strip())
            s = s[cut + 1:].strip()
        if s:
            out.append(s)
    return out


def _lang_code(v):
    return str(v or "").strip().lower().replace("_", "-").split("-")[0]


def _lookup(key):
    if not key:
        return None
    with voices_lock:
        e = voices.get(key)
        if e is not None:
            voices.move_to_end(key)
        return e


def _store_voice(key, v, dur, lang):
    with voices_lock:
        voices[key] = {"voice": v, "created": time.time(), "ref_s": dur, "lang": lang}
        while len(voices) > MAX_VOICES:
            voices.popitem(last=False)


BG_REFUSED = ("busy", "preempted", "too long")   # why a background request is refused for live speech -> 409


def _unavailable(reason, retry_s=1, background=False):
    key = ("bg_" if background else "") + "rejected_" + reason.split(":")[0].replace(" ", "_")
    stats[key] += 1
    if background and reason.startswith(BG_REFUSED):
        return JSONResponse({"error": "tts busy", "reason": reason, "priority": BACKGROUND}, status_code=409,
                            headers={"Cache-Control": "no-store"})
    return JSONResponse({"error": "tts busy" if reason.startswith("busy") else "tts unavailable", "reason": reason},
                        status_code=503, headers={"Retry-After": str(retry_s), "Cache-Control": "no-store"})


# ---------------- GPU admission ----------------
async def _admit(request: Request, expected_s: float, priority: str = LIVE, chars: int = 0):
    """(ticket, None) with the GPU held for this request, or (None, why). Live: waits <= ADMIT_WAIT_S (-> 503) and
    preempts a background job; background: only if the GPU is free right now (-> 409). See admission.py."""
    if priority == BACKGROUND:
        ticket, why = gate.try_background(expected_s, chars)
    else:
        ticket, why = await gate.admit_live(expected_s)
    if why:
        return None, why
    if await request.is_disconnected():
        gate.release(ticket)
        return None, "client gone"
    return ticket, None


class Job:
    """One synthesis on the GPU. The worker thread owns the (already acquired) GPU ticket and always releases it."""

    def __init__(self, ticket, parts, lang, resolve, cfg, *, speed, exaggeration, temperature, audio_cap_s, tag,
                 wall_cap_s=None):
        self.ticket = ticket
        self.parts, self.lang, self.resolve, self.cfg = parts, lang, resolve, cfg
        self.speed, self.exaggeration, self.temperature = speed, exaggeration, temperature
        self.audio_cap_s = audio_cap_s
        self.wall_cap_s = min(WALL_BASE_S + WALL_FACTOR * audio_cap_s, wall_cap_s or 1e9)
        self.tag = tag
        self.loop = asyncio.get_running_loop()
        self.q: asyncio.Queue = asyncio.Queue()
        self.cancel = threading.Event()
        self.t0 = time.perf_counter()
        ticket.expected_end = self.t0 + expected_job_s(" ".join(parts), lang)
        self.audio_s = 0.0
        self.reason = None
        self.first_at = None

    def _put(self, item):
        self.loop.call_soon_threadsafe(self.q.put_nowait, item)

    @property
    def preempted(self):
        return bool(self.reason and self.reason.startswith("preempted"))

    def should_stop(self):
        if self.cancel.is_set():
            self.reason = self.reason or "client gone"
            return True
        if self.ticket.preempted.is_set():   # background only: a live request wants the GPU
            self.reason = self.reason or "preempted: live request"
            return True
        el = time.perf_counter() - self.t0
        if el > WALL_BASE_S + WALL_FACTOR * self.audio_s or el > self.wall_cap_s:
            self.reason = self.reason or f"cap hit: wall clock {el:.1f}s for {self.audio_s:.1f}s audio"
            return True
        return False

    def run(self):
        current["job"] = self
        try:
            if self.should_stop():   # preempted (or the client left) before it began
                return
            voice = self.resolve()
            budget = int(TOKENS_PER_S * self.audio_cap_s * self.speed)   # tokens: output audio <= cap
            for i, s in enumerate(self.parts):
                if budget <= 8:
                    self.reason = self.reason or f"cap hit: audio {self.audio_cap_s:.1f}s"
                    break
                st = {}
                for chunk in engine.stream(s, self.lang, voice, cfg_weight=self.cfg, exaggeration=self.exaggeration,
                                           temperature=self.temperature, speed=self.speed,
                                           max_new_tokens=budget, stats=st, should_stop=self.should_stop):
                    if self.first_at is None:
                        self.first_at = time.perf_counter()
                    self.audio_s += len(chunk) / SR
                    self._put((np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes())
                budget -= st.get("tokens", 0)
                if st.get("stopped"):
                    break
                if st.get("capped") and budget <= 8:
                    self.reason = self.reason or f"cap hit: audio {self.audio_cap_s:.1f}s (no end of speech)"
                    break
                if i < len(self.parts) - 1:
                    self._put(b"\x00\x00" * int(0.12 * SR))   # short pause between sentences
                    self.audio_s += 0.12
        except Exception as e:  # noqa: BLE001
            self.reason = self.reason or f"error {e.__class__.__name__}"
            log.exception("%s failed", self.tag)
        finally:
            current["job"] = None
            gate.release(self.ticket)
            self._put(None)
            el = time.perf_counter() - self.t0
            if self.reason and self.reason.startswith("cap hit"):
                stats["cap_hit"] += 1
                log.warning("%s %s; ended after %.1fs, %.1fs audio", self.tag, self.reason, el, self.audio_s)
            elif self.reason == "client gone":
                stats["client_gone"] += 1
                log.info("%s client gone; stopped after %.1fs, %.1fs audio", self.tag, el, self.audio_s)
            elif self.preempted:
                stats["preempted"] += 1
                log.info("%s preempted by a live request after %.2fs, %.1fs audio", self.tag, el, self.audio_s)
            elif self.reason:
                log.warning("%s %s", self.tag, self.reason)
            else:
                stats["done"] += 1
                log.info("%s gpu released after %.0fms, %.2fs audio", self.tag, el * 1000, self.audio_s)


async def _serve(request: Request, job: Job, headers: dict):
    """Start the worker; answer only when the first audio exists (else 503, or 409 for a preempted background
    job); stream the rest from memory. A background job preempted after audio went out cuts its stream."""
    bg = job.ticket.background
    try:
        threading.Thread(target=job.run, name="tts-job", daemon=True).start()
    except Exception:
        gate.release(job.ticket)
        raise
    try:
        first = await asyncio.wait_for(job.q.get(), timeout=FIRST_AUDIO_S)
    except asyncio.TimeoutError:
        job.cancel.set()
        job.reason = job.reason or f"cap hit: no audio within {FIRST_AUDIO_S:.1f}s"
        return _unavailable(f"slow: no audio within {FIRST_AUDIO_S:.1f}s", background=bg)
    if first is None:  # ended before any audio: cap, error, preempted, or client gone
        return _unavailable(job.reason or "no audio", background=bg)
    log.info("%s lang=%s chars=%d cfg=%.1f speed=%.2f first audio after %.0fms", job.tag, job.lang,
             sum(map(len, job.parts)), job.cfg, job.speed, (time.perf_counter() - job.t_request) * 1000)

    async def body():
        try:
            yield first
            while True:
                try:
                    item = await asyncio.wait_for(job.q.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    if await request.is_disconnected():
                        break
                    continue
                if item is None:
                    break
                yield item
            if job.preempted:   # the clause is incomplete: no final chunk, so the client cannot take it as whole
                raise StreamCut(job.tag)
        finally:
            if not job.cancel.is_set():
                job.cancel.set()   # no-op if the job already finished; stops it within one token otherwise
                job.ticket.expected_end = time.perf_counter()

    return StreamingResponse(body(), media_type=f"audio/L16;rate={SR};channels=1", headers=headers)


def _audio_headers(cfg, n_parts, cap_s, priority=LIVE):
    return {"X-Sample-Rate": str(SR), "X-Audio-Format": "s16le", "X-Channels": "1",
            "X-Cfg-Weight": str(cfg), "X-Sentences": str(n_parts), "X-Max-Audio-S": f"{cap_s:.1f}",
            "X-Priority": priority, "Cache-Control": "no-store", "X-Accel-Buffering": "no"}


# ---------------- API ----------------
class SpeakReq(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    lang: str = Field(..., min_length=2, max_length=5)
    voice_id: str | None = None
    speed: float = Field(1.0, ge=0.5, le=2.0)
    exaggeration: float = Field(0.5, ge=0.0, le=2.0)
    cfg_weight: float | None = Field(None, ge=0.0, le=1.0)
    temperature: float = Field(0.8, ge=0.05, le=2.0)
    normalize: bool = True
    priority: Literal["live", "background"] = LIVE


@app.get("/health")
async def health():
    free = None
    try:
        free = int(open("/proc/meminfo").read().split("MemAvailable:")[1].split()[0]) // 1024
    except Exception:
        pass
    job, holder = current["job"], gate.holder
    return {"status": "ok", "model": "chatterbox-multilingual-v3 (t3 bf16 + cuda graph, meanflow s3gen)",
            "version": app.version, "sample_rate": SR, "format": "s16le mono", "languages": engine.langs,
            "primary_languages": LANGS_PRIMARY, "voices": len(voices), "max_voices": MAX_VOICES,
            "busy": gate.lock.locked(), "busy_for_s": round(time.perf_counter() - job.t0, 1) if job else 0.0,
            "busy_limit_s": round(job.wall_cap_s, 1) if job else 0.0,
            "busy_priority": holder.priority if holder else None, "bg_max_chars": BG_MAX_CHARS,
            "waiting": gate.live_waiting, "counters": dict(stats), "uptime_s": round(time.time() - t_start),
            "gpu_allocated_mb": torch.cuda.memory_allocated() >> 20,
            "gpu_reserved_mb": torch.cuda.memory_reserved() >> 20, "host_mem_available_mb": free}


@app.post("/v1/voices")
async def create_voice(request: Request, sample_rate: int | None = None, lang: str | None = None,
                       exaggeration: float = 0.5):
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        f = form.get("file") or form.get("audio")
        if f is None or not hasattr(f, "read"):
            raise HTTPException(400, "multipart upload needs a 'file' field")
        body = await f.read()
        sample_rate = sample_rate or (int(form["sample_rate"]) if "sample_rate" in form else None)
        lang = lang or form.get("lang")
    else:
        body = await request.body()
    if not body:
        raise HTTPException(400, "empty body; POST the reference audio (WAV, or raw s16le with ?sample_rate=)")
    try:
        wav, sr = _decode_audio(body, sample_rate)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"could not decode audio: {e.__class__.__name__}")
    dur = len(wav) / sr
    if dur < MIN_REF_S:
        raise HTTPException(422, f"reference too short ({dur:.1f}s); need >= {MIN_REF_S}s, 5-10s is best")
    wav = _prep_ref(wav, sr)
    ticket, why = await _admit(request, 0.3)
    if why:
        return _unavailable(why)
    t = time.perf_counter()
    try:
        v = await anyio.to_thread.run_sync(lambda: engine.make_voice(wav, sr, exaggeration=exaggeration))
    finally:
        gate.release(ticket)
    vid = "v_" + uuid.uuid4().hex[:16]
    _store_voice(vid, v, round(dur, 2), _lang_code(lang) or None)
    ms = round((time.perf_counter() - t) * 1000)
    log.info("voice %s.. created from %.1fs ref in %dms", vid[:6], dur, ms)
    return {"voice_id": vid, "ref_seconds": round(dur, 2), "embed_ms": ms, "lang": voices[vid]["lang"]}


@app.delete("/v1/voices/{voice_id}")
async def delete_voice(voice_id: str):
    with voices_lock:
        if voices.pop(voice_id, None) is None:
            raise HTTPException(404, "unknown voice_id")
    return Response(status_code=204)


@app.post("/v1/speak")
async def speak(req: SpeakReq, request: Request):
    t_req = time.perf_counter()
    lang = _lang_code(req.lang)
    if lang not in engine.langs:
        raise HTTPException(422, f"unsupported lang '{req.lang}'; supported: {engine.langs}")
    voice, voice_lang = None, None
    if req.voice_id:
        ent = _lookup(req.voice_id)
        if ent is None:
            raise HTTPException(404, "unknown voice_id")
        voice, voice_lang = ent["voice"], ent.get("lang")
    text = normalize_text(req.text, lang) if req.normalize else req.text
    parts = _sentences(text)
    if not parts:
        raise HTTPException(422, "empty text")
    if voice is None:
        voice, cfg = _default_voice(lang)
    else:
        cfg = _cfg_for(voice_lang, lang)
    if req.cfg_weight is not None:
        cfg = req.cfg_weight
    cap = max_audio_s(text, lang, AUDIO_CAP_SPEAK_S)
    bg = req.priority == BACKGROUND
    ticket, why = await _admit(request, expected_job_s(text, lang), req.priority, len(req.text))
    if why:
        return _unavailable(why, background=bg)
    try:
        job = Job(ticket, parts, lang, lambda: voice, cfg, speed=req.speed, exaggeration=req.exaggeration,
                  temperature=req.temperature, audio_cap_s=cap,
                  tag=("speak bg" if bg else "speak") + " voice=" + (req.voice_id or "default")[:8])
        job.t_request = t_req
    except Exception:
        gate.release(ticket)
        raise
    return await _serve(request, job, _audio_headers(cfg, len(parts), cap, req.priority))


# ---------------- RAUM compatibility API (core/raum/pipeline/tts_http.py) ----------------
@app.post("/tts")
async def tts_compat(request: Request):
    """RAUM's http engine: JSON {text, lang, rate?, model?, sample_rate, voice_ref? (base64 WAV), voice_id?}
    or the same as multipart with a ``voice_ref`` file part. Streams s16le mono at 24 kHz (X-Sample-Rate).
    Optional ``voice_lang``: the reference speaker's language (unknown means cross-lingual). Optional
    ``priority``: "live" (default) or "background" (a speculative clause; 409 unless the GPU is free now)."""
    t_req = time.perf_counter()
    ref = None
    if request.headers.get("content-type", "").startswith("multipart/"):
        form = await request.form()
        fields = {k: v for k, v in form.items() if isinstance(v, str)}
        f = form.get("voice_ref")
        if f is not None and hasattr(f, "read"):
            ref = await f.read()
    else:
        try:
            fields = await request.json()
        except Exception:
            raise HTTPException(400, "expected a JSON body")
        if not isinstance(fields, dict):
            raise HTTPException(400, "expected a JSON object")
        if fields.get("voice_ref"):
            try:
                ref = base64.b64decode(fields["voice_ref"])
            except (binascii.Error, ValueError, TypeError):
                ref = None
                log.warning("tts: voice_ref is not base64; default voice")
    text = str(fields.get("text") or "").strip()
    lang = _lang_code(fields.get("lang"))
    if not text:
        raise HTTPException(422, "empty text")
    if len(text) > 4000:
        raise HTTPException(413, "text longer than 4000 characters")
    if lang not in engine.langs:
        raise HTTPException(422, f"unsupported lang; supported: {engine.langs}")
    priority = priority_of(fields.get("priority"))
    if priority is None:
        raise HTTPException(422, "priority must be 'live' or 'background'")
    bg, chars = priority == BACKGROUND, len(text)
    try:
        rate = float(fields.get("rate") or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    speed = min(RATE_MAX, max(RATE_MIN, rate))
    vid = str(fields.get("voice_id") or "")[:64] or None
    req_vlang = _lang_code(fields.get("voice_lang") or fields.get("ref_lang")) or None

    ent, pending, state = _lookup(vid), None, "cached"
    if ent is None and ref:
        key = vid or hashlib.sha256(ref).hexdigest()[:16]
        ent = _lookup(key)
        if ent is None:
            try:
                wav, sr = _decode_audio(ref, None)
                if len(wav) / sr >= MIN_REF_S:
                    pending = (key, _prep_ref(wav, sr), sr, round(len(wav) / sr, 2))
                else:
                    log.warning("tts: voice_ref shorter than %.0f s; default voice", MIN_REF_S)
            except Exception as e:  # noqa: BLE001 -- unreadable ref: speak with the default voice, never fail
                log.warning("tts: voice_ref unreadable (%s); default voice", e.__class__.__name__)
    if ent is not None:
        if req_vlang and ent.get("lang") != req_vlang:
            ent["lang"] = req_vlang
        voice0, cfg = ent["voice"], _cfg_for(ent.get("lang"), lang)
    elif pending is not None:
        voice0, cfg, state = None, _cfg_for(req_vlang, lang), "new"
    else:
        (voice0, cfg), state = _default_voice(lang), "default"

    def resolve():
        if pending is None:
            return voice0
        key, wav, sr, dur = pending
        e = _lookup(key)  # registered by a concurrent request meanwhile
        if e is not None:
            return e["voice"]
        t = time.perf_counter()
        try:
            v = engine.make_voice(wav, sr)
        except Exception as ex:  # noqa: BLE001
            log.warning("tts: voice embedding failed (%s); default voice", ex.__class__.__name__)
            return _default_voice(lang)[0]
        _store_voice(key, v, dur, req_vlang)
        log.info("tts: voice %s.. registered from %.1fs ref in %.0fms", key[:6], dur, (time.perf_counter() - t) * 1000)
        return v

    if fields.get("cfg_weight") is not None:  # tuning override
        try:
            cfg = min(1.0, max(0.0, float(fields["cfg_weight"])))
        except (TypeError, ValueError):
            pass
    text = normalize_text(text, lang)
    parts = _sentences(text)
    cap = max_audio_s(text, lang, AUDIO_CAP_TTS_S)
    wall = None
    try:  # test hooks: can only tighten the caps, never loosen them
        if fields.get("_cap_audio_s") is not None:
            cap = min(cap, max(0.5, float(fields["_cap_audio_s"])))
        if fields.get("_cap_wall_s") is not None:
            wall = max(0.05, float(fields["_cap_wall_s"]))
    except (TypeError, ValueError):
        pass
    ticket, why = await _admit(request, expected_job_s(text, lang), priority, chars)
    if why:
        log.info("tts %s refused (%s) lang=%s chars=%d", priority, why, lang, chars)
        return _unavailable(why, background=bg)
    try:
        job = Job(ticket, parts, lang, resolve, cfg, speed=speed, exaggeration=0.5, temperature=0.8,
                  audio_cap_s=cap, tag=f"tts{' bg' if bg else ''} voice={state}", wall_cap_s=wall)
        job.t_request = t_req
    except Exception:
        gate.release(ticket)
        raise
    return await _serve(request, job, _audio_headers(cfg, len(parts), cap, priority))


@app.delete("/tts/voices/{voice_id}")
async def delete_voice_compat(voice_id: str):
    return await delete_voice(voice_id)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("RAUM_TTS_HOST", "127.0.0.1"),
                port=int(os.environ.get("RAUM_TTS_PORT", "8090")), log_level="info", workers=1)
