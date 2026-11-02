"""Voice lock (F-10, plan §5.6, §6.13): each device's mic only produces utterances from its owner.

Echo control (C-14, ``raum.pipeline.echo``) pauses the mics that would hear the box's voice; the voice lock is
the content-based backstop for what still leaks into a mic: the box's own voice from a speaker, or the other
person talking near someone else's phone. Used by :class:`raum.pipeline.room.RoomPipeline` (a few marked lines).

Per room and device identity:

- **Enrollment.** The owner's first clean utterances (a transcript above ``asr_min_conf``, no voice played into
  this mic meanwhile, at least ``min_utt_s`` of speech) are embedded and averaged, weighted by their speech
  seconds, until ``enroll_s`` seconds are learned. Until then every utterance passes. An utterance that does
  not sound like what was learned so far is not learned (it still passes).
- **Check.** Every later utterance is embedded and compared (cosine) with the owner's voice. Below ``threshold``
  it is dropped: no ``transcript.final``, not translated, not spoken, not stored, not summarized. One
  ``voice.lock`` event per utterance (a score, never the voice) tells the speaker's phone and the glass box.
- Utterances with less than ``min_utt_s`` of speech are too short to verify: they pass (``short``).
- Devices listed in ``exempt_devices`` (default: a shared ``speakerphone``, A-03) hear many people: no lock.

**Latency.** The embedding starts as the utterance ends, on a worker thread (one per agent process; torch on one
thread), beside the final transcription; with semantic end of turn (``raum.pipeline.turn``) it starts at the
pause, beside the early final ASR, and the turn reuses it when it uses that transcript (``begin(reuse=True)``;
a check on audio that grew since is replaced and wiped). The verdict is taken after the transcription and
before anything of the utterance is published. ECAPA needs ~50 ms for 3 s of speech on one CPU core (M2), whisper ~250 ms, so on
the happy path the turn never waits (``voice.lock.waited_ms`` ~ 0). A late verdict is awaited at most
``max_wait_ms`` past the transcription; then the utterance passes as ``unchecked``, as on an engine error: the
lock is a backstop and never silences the owner because it is slow.

Engines (``models.voicelock``): ``off`` (default), ``mock`` (a deterministic spectral fingerprint without torch: a
pure tone and noise differ, the same tone at another level does not), ``ecapa`` (SpeechBrain
``speechbrain/spkrec-ecapa-voxceleb``, Apache-2.0, ~85 MB; ``uv sync --extra voicelock``). Options: ``threshold``,
``enroll_s``, ``min_utt_s``, ``max_s`` (speech embedded per utterance), ``max_wait_ms``, ``exempt_devices``, and for
ecapa ``model_dir`` (downloaded into it once, then loaded from it offline), ``device`` (cpu), ``threads`` (1).
``raum voicelock check`` loads the model (downloading it if needed) and prints the embedding times.

PRIVACY (plan §6.10): a speaker embedding is biometric data. :class:`Voiceprint` and the per-utterance vectors
live in memory only, per session: never on disk, in an event (only the score), a log line, the session store or
the audit log; their ``repr`` shows seconds only. They are zeroed (best effort) and dropped on burn, session end,
a new session, ``voice_lock_reset`` and when the room's pipeline closes; an utterance still in flight across a
burn is never learned.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from raum.pipeline.base import SpeakerEncoder
from raum.pipeline.voice import speech_seconds

log = logging.getLogger("raum.pipeline.voicelock")

SAMPLE_RATE = 16000
ECAPA_MODEL = "speechbrain/spkrec-ecapa-voxceleb"
ENGINES = ("off", "none", "mock", "ecapa")
DEFAULTS: dict[str, Any] = {
    "threshold": 0.35,  # ECAPA cosine; same mic, same session (see the F-10 notes in the report / STATUS)
    "enroll_s": 6.0,
    "min_utt_s": 1.0,
    "max_s": 8.0,
    "max_wait_ms": 1000.0,
    "exempt_devices": ["speakerphone"],
}


# ---------------------------------------------------------------- engines


class MockSpeakerEncoder:
    """Deterministic and torch-free: log energy in 22 log-spaced bands (60 Hz - 7.5 kHz), averaged over 32 ms
    frames, 30 dB of range, the level removed, L2-normalised. Silence gives the zero vector."""

    name = "mock"
    _N = 512
    _HOP = 256

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        self._edges = np.unique(np.round(np.geomspace(2, 240, 25)).astype(int))
        self._win = np.hanning(self._N).astype(np.float32)

    def embed(self, audio: np.ndarray) -> np.ndarray:
        x = np.asarray(audio, dtype=np.float32).reshape(-1)
        dim = len(self._edges) - 1
        if x.size == 0 or float(np.abs(x).max()) < 1e-4:
            return np.zeros(dim, dtype=np.float32)
        if x.size < self._N:
            x = np.pad(x, (0, self._N - x.size))
        k = 1 + (x.size - self._N) // self._HOP
        idx = np.arange(self._N)[None, :] + self._HOP * np.arange(k)[:, None]
        power = (np.abs(np.fft.rfft(x[idx] * self._win, axis=1)) ** 2).mean(axis=0)
        bands = np.add.reduceat(power, self._edges)[:-1]
        v = np.log10(np.maximum(bands, float(bands.max()) * 1e-3) + 1e-12)  # 30 dB of range: leakage is no voice
        v -= v.mean()
        n = float(np.linalg.norm(v))
        if n < 1e-6:
            return np.zeros(dim, dtype=np.float32)
        return (v / n).astype(np.float32)


def default_model_dir(source: str = ECAPA_MODEL) -> Path:
    """Outside the repo: ``~/.cache/raum/voicelock/<model>``."""
    return Path.home() / ".cache" / "raum" / "voicelock" / source.replace("/", "--")


class EcapaSpeakerEncoder:
    """SpeechBrain ECAPA-TDNN trained on VoxCeleb (192-d), on the CPU with one torch thread. Loaded once per agent
    process. First use downloads the model into ``model_dir``; later loads read that folder (no network, so it
    works in sealed mode, T-07)."""

    def __init__(self, model: str | None = None, options: dict[str, Any] | None = None) -> None:
        o = options or {}
        import torch  # type: ignore[import-not-found]

        torch.set_num_threads(int(o.get("threads", 1)))  # process-wide; denoise.py uses 1 as well
        try:
            from speechbrain.inference.speaker import EncoderClassifier  # type: ignore[import-not-found]
        except ImportError:  # speechbrain < 1.0
            from speechbrain.pretrained import EncoderClassifier  # type: ignore[import-not-found,no-redef]

        repo_id = model or ECAPA_MODEL
        savedir = Path(str(o.get("model_dir") or default_model_dir(repo_id))).expanduser()
        source = str(savedir) if (savedir / "hyperparams.yaml").is_file() else repo_id
        self.name = "ecapa:" + repo_id.rsplit("/", 1)[-1]
        self.device = str(o.get("device", "cpu"))
        self._torch = torch
        # savedir always given: SpeechBrain's default is ./pretrained_models in the working directory (the repo)
        self._enc = EncoderClassifier.from_hparams(source=source, savedir=str(savedir),
                                                   run_opts={"device": self.device})

    def embed(self, audio: np.ndarray) -> np.ndarray:
        torch = self._torch
        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
        with torch.inference_mode():
            e = self._enc.encode_batch(x)
        v = e.reshape(-1).float().cpu().numpy().astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n > 1e-9 else np.zeros_like(v)


# One model per agent process (rooms share it; loaded in prewarm). A failed load is remembered as None, so a
# missing extra logs once instead of once per room.
_encoders: dict[tuple[Any, ...], SpeakerEncoder | None] = {}


def make_speaker_encoder(engine: str, model: str | None = None,
                         options: dict[str, Any] | None = None) -> SpeakerEncoder | None:
    """``off`` -> None; ``mock`` / ``ecapa`` -> the process-wide encoder (None if ecapa cannot load: lock off)."""
    if engine not in ENGINES:
        raise ValueError(f"unknown voicelock engine: {engine!r}")
    if engine in ("off", "none"):
        return None
    o = options or {}
    key = (engine, model, str(o.get("model_dir") or ""), str(o.get("device", "cpu")), int(o.get("threads", 1)))
    if key in _encoders:
        return _encoders[key]
    enc: SpeakerEncoder | None
    if engine == "mock":
        enc = MockSpeakerEncoder(o)
    else:
        t = time.perf_counter()
        try:
            enc = EcapaSpeakerEncoder(model, o)
            log.info("voice lock model loaded: %s (%.1f s)", enc.name, time.perf_counter() - t)
        except Exception as e:  # noqa: BLE001  ImportError (extra not installed), download, torch
            log.warning("voice lock unavailable (%s: %s): every utterance passes", e.__class__.__name__, str(e)[:160])
            enc = None
    _encoders[key] = enc
    return enc


def warm_voicelock(cfg: Any) -> None:
    """Load the speaker model once per agent process and embed a second of quiet noise (first calls are slow)."""
    m = cfg.models.voicelock
    if not m.enabled:
        return
    enc = make_speaker_encoder(m.engine, m.model, m.options)
    if enc is None:
        return
    t = time.perf_counter()
    x = (np.random.default_rng(0).standard_normal(SAMPLE_RATE) * 0.01).astype(np.float32)
    try:
        for _ in range(2):
            enc.embed(x)
    except Exception as e:  # noqa: BLE001
        log.warning("voice lock warm-up failed (%s)", e.__class__.__name__)
        return
    log.info("voice lock ready (%s, %.0f ms warm-up)", enc.name, (time.perf_counter() - t) * 1000.0)


_executor: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """One worker thread per agent process: the model is CPU-bound and runs single-threaded."""
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="raum-voicelock")
    return _executor


def _timed_embed(encoder: SpeakerEncoder, clip: np.ndarray) -> tuple[np.ndarray, float]:
    t = time.perf_counter()
    v = np.asarray(encoder.embed(clip), dtype=np.float32).reshape(-1)
    return v, (time.perf_counter() - t) * 1000.0


def _retrieve(fut: asyncio.Future[Any]) -> None:
    if not fut.cancelled():
        fut.exception()  # an abandoned check (turn cancelled) must not log "exception never retrieved"


# ---------------------------------------------------------------- state (memory only)


class Voiceprint:
    """One device owner's voice: the seconds-weighted mean of their enrollment embeddings (memory only)."""

    __slots__ = ("_sum", "_vec", "seconds", "utterances")

    def __init__(self) -> None:
        self._sum: np.ndarray | None = None
        self._vec: np.ndarray | None = None
        self.seconds = 0.0
        self.utterances = 0

    def add(self, emb: np.ndarray, seconds: float) -> None:
        if self._sum is None:
            self._sum = np.zeros(emb.shape, dtype=np.float32)
            self._vec = np.zeros(emb.shape, dtype=np.float32)
        assert self._vec is not None
        self._sum += emb * np.float32(seconds)
        n = float(np.linalg.norm(self._sum))
        np.divide(self._sum, n if n > 1e-9 else 1.0, out=self._vec)
        self.seconds += float(seconds)
        self.utterances += 1

    def score(self, emb: np.ndarray) -> float | None:
        """Cosine similarity of an (L2-normalised) embedding with this voice; None before the first utterance."""
        if self._vec is None or self._vec.shape != emb.shape:
            return None
        return float(np.dot(self._vec, emb))

    def clear(self) -> None:
        for a in (self._sum, self._vec):
            if a is not None:
                a.fill(0.0)  # best effort: overwrite, not just drop the reference
        self._sum = self._vec = None
        self.seconds = 0.0
        self.utterances = 0

    def __repr__(self) -> str:
        return f"<Voiceprint {self.seconds:.1f} s>"


class LockCheck:
    """One utterance's check in flight: the embedding future (None: too short to verify)."""

    __slots__ = ("future", "gen", "seconds", "speaker", "utt")

    def __init__(self, speaker: str, utt: str, seconds: float, future: asyncio.Future[Any] | None, gen: int) -> None:
        self.speaker = speaker
        self.utt = utt
        self.seconds = seconds
        self.future = future
        self.gen = gen

    def wipe(self) -> None:
        """Zero the vector now, or as soon as the worker thread delivers it."""
        f = self.future
        if f is None:
            return
        if not f.done():
            f.add_done_callback(lambda _f: self.wipe())
        elif not f.cancelled() and f.exception() is None:
            f.result()[0].fill(0.0)

    def __repr__(self) -> str:  # never the future (its repr would show the vector)
        return f"<LockCheck {self.utt} {self.speaker} {self.seconds:.1f} s>"


Emit = Callable[[str, Any, str | None], Awaitable[Any]]
Spawn = Callable[[Awaitable[Any]], Any]


class VoiceLock:
    """The voice lock of one room: a :class:`Voiceprint` per device identity, the checks, the ``voice.lock`` event.

        lock = vl.start(speaker, utt, audio_16k, vad_ms, device)   # as the utterance ends: embedding starts
        ...                                                         # final transcription runs meanwhile
        if not await vl.admit(lock, clean=...):                     # before anything is published
            return                                                  # not the owner's voice: dropped
    """

    def __init__(self, encoder: SpeakerEncoder | None, options: dict[str, Any] | None = None, *,
                 emit: Emit | None = None, spawn: Spawn | None = None, pad_s: float = 0.5) -> None:
        o = {**DEFAULTS, **(options or {})}
        self.encoder = encoder
        self.threshold = float(o["threshold"])
        self.enroll_s = float(o["enroll_s"])
        self.min_utt_s = float(o["min_utt_s"])
        self.max_s = float(o["max_s"])
        self.max_wait_s = float(o["max_wait_ms"]) / 1000.0
        self.exempt = {str(d) for d in (o.get("exempt_devices") or [])}
        self.pad_s = float(pad_s)
        self._emit = emit
        self._spawn = spawn
        self._prints: dict[str, Voiceprint] = {}
        self._pending: dict[str, LockCheck] = {}  # utt -> its check in flight (one per utterance)
        self._gen = 0

    @classmethod
    def for_room(cls, engines: Any, cfg: Any, *, emit: Emit | None = None, spawn: Spawn | None = None) -> VoiceLock:
        pad = float((cfg.models.vad.options or {}).get("prefix_padding_duration", 0.5))
        return cls(getattr(engines, "voicelock", None), cfg.models.voicelock.options, emit=emit, spawn=spawn,
                   pad_s=pad)

    @property
    def enabled(self) -> bool:
        return self.encoder is not None

    def enrolled_s(self, speaker: str) -> float:
        vp = self._prints.get(speaker)
        return vp.seconds if vp is not None else 0.0

    def is_enrolled(self, speaker: str) -> bool:
        return self.enrolled_s(speaker) >= self.enroll_s

    # ---------------------------------------------------------------- per utterance

    def start(self, speaker: str, utt: str, audio: np.ndarray | None, vad_ms: float,
              device: str | None = None) -> LockCheck | None:
        """The utterance ended (or paused, with the early ASR): start its embedding on the worker thread; returns
        at once. A check already in flight for ``utt`` (on audio that has grown since) is replaced and wiped.
        None = no lock on this device."""
        old = self._pending.pop(utt, None)
        if old is not None:
            old.wipe()
        if self.encoder is None or (device is not None and device in self.exempt) or audio is None or not len(audio):
            return None
        seconds = speech_seconds(len(audio) / SAMPLE_RATE, vad_ms, None, self.pad_s)
        if seconds < self.min_utt_s:
            return LockCheck(speaker, utt, seconds, None, self._gen)
        tail = int(max(0.0, float(vad_ms)) / 1000.0 * SAMPLE_RATE)  # the endpoint silence
        speech = np.asarray(audio, dtype=np.float32).reshape(-1)[: max(0, len(audio) - tail)]
        clip = speech[: int(self.max_s * SAMPLE_RATE)]
        fut = asyncio.get_running_loop().run_in_executor(_pool(), _timed_embed, self.encoder, clip)
        fut.add_done_callback(_retrieve)
        chk = self._pending[utt] = LockCheck(speaker, utt, seconds, fut, self._gen)
        return chk

    def begin(self, speaker: str, utt: str, audio: np.ndarray | None, vad_ms: float, device: str | None = None,
              *, reuse: bool = False) -> LockCheck | None:
        """The turn's check: with ``reuse`` (the turn uses the early ASR's transcript, so the same audio) the one
        that started beside that ASR, if any; else a new one on ``audio``."""
        if reuse and utt in self._pending:
            return self._pending[utt]
        return self.start(speaker, utt, audio, vad_ms, device)

    async def admit(self, chk: LockCheck | None, *, clean: bool) -> bool:
        """The verdict, after the transcription; False = not the owner's voice: drop the utterance. ``clean``: it may
        teach the owner's voice (a confident transcript, no voice played into this mic meanwhile)."""
        if chk is None:
            return True
        if self._pending.get(chk.utt) is chk:
            del self._pending[chk.utt]
        t = time.monotonic()
        emb: np.ndarray | None = None
        ms: float | None = None
        status = "short"
        if chk.future is not None:
            try:
                emb, ms = await asyncio.wait_for(asyncio.shield(chk.future), self.max_wait_s)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                status = "unchecked"
                log.warning("voice lock: verdict for %s later than %.0f ms: passes unchecked", chk.utt,
                            self.max_wait_s * 1000.0)
            except Exception as e:  # noqa: BLE001
                status = "unchecked"
                log.warning("voice lock: check of %s failed (%s): passes unchecked", chk.utt, e.__class__.__name__)
        waited_ms = (time.monotonic() - t) * 1000.0

        accepted, score = True, None
        vp = self._prints.get(chk.speaker)
        try:
            if emb is not None and not np.any(emb):
                status = "unchecked"  # no voice in it
            elif emb is not None:
                score = vp.score(emb) if vp is not None else None
                if vp is not None and vp.seconds >= self.enroll_s:
                    accepted = score is not None and score >= self.threshold
                    status = "match" if accepted else "mismatch"
                else:
                    status = "enrolling"
                    # learn only clean speech that sounds like what was learned so far, never across a burn
                    if clean and chk.gen == self._gen and (score is None or score >= self.threshold):
                        if vp is None:
                            vp = self._prints[chk.speaker] = Voiceprint()
                        vp.add(emb, chk.seconds)
        finally:
            if emb is not None:
                emb.fill(0.0)  # the utterance's vector is not kept beyond its verdict

        data = {"speaker": chk.speaker, "accepted": accepted, "status": status,
                "score": round(score, 3) if score is not None else None, "threshold": self.threshold,
                "enrolled_s": round(vp.seconds, 1) if vp is not None else 0.0, "enroll_s": self.enroll_s,
                "ms": round(ms, 1) if ms is not None else None, "waited_ms": round(waited_ms, 1)}
        if not accepted:
            log.info("voice lock: %s on %s is not the owner's voice (score %.2f < %.2f): ignored", chk.utt,
                     chk.speaker, score if score is not None else float("nan"), self.threshold)
        if self._emit is not None:
            if accepted and self._spawn is not None:
                self._spawn(self._emit("voice.lock", data, chk.utt))  # never delays the turn
            else:
                await self._emit("voice.lock", data, chk.utt)
        return accepted

    # ---------------------------------------------------------------- forgetting

    def reset(self, by: str | None = None, participants: Iterable[Any] = ()) -> list[str]:
        """``voice_lock_reset``: forget and learn again. ``by``: an identity, a role (its devices), None = everyone."""
        if not by:
            ids = list(self._prints)
        elif by in ("a", "b", "observer"):
            ids = [p.id for p in participants if getattr(p, "role", None) == by]
        else:
            ids = [by]
        for i in ids:
            vp = self._prints.pop(i, None)
            if vp is not None:
                vp.clear()
        return ids

    def clear(self) -> None:
        """Burn, session end, a new session, close: every voice and in-flight vector, zeroed and dropped."""
        self._gen += 1
        for vp in self._prints.values():
            vp.clear()
        self._prints.clear()
        for chk in self._pending.values():
            chk.wipe()
        self._pending.clear()

    def __repr__(self) -> str:
        name = getattr(self.encoder, "name", "off")
        return f"<VoiceLock {name} {len(self._prints)} voices>"


# ---------------------------------------------------------------- ``raum voicelock check``


def main(argv: list[str] | None = None) -> int:
    """Load the configured speaker model (downloading it into ``model_dir`` if needed) and time the embedding."""
    import argparse

    from raum.config import get_config

    ap = argparse.ArgumentParser(prog="raum voicelock", description="F-10 voice lock: load and time the model")
    ap.add_argument("cmd", choices=["check"])
    ap.add_argument("--engine", help="override models.voicelock.engine (e.g. ecapa)")
    args = ap.parse_args(argv or [])
    m = get_config().models.voicelock
    engine = args.engine or m.engine
    if engine in ("off", "none"):
        print("models.voicelock.engine is off (try --engine ecapa)")
        return 1
    t = time.perf_counter()
    enc = make_speaker_encoder(engine, m.model, m.options)
    if enc is None:
        print("voice lock unavailable (see the log line above; ecapa needs `uv sync --extra voicelock`)")
        return 1
    print(f"loaded {enc.name} in {time.perf_counter() - t:.1f} s")
    rng = np.random.default_rng(0)
    for s in (1, 3, 5, 8):
        x = (rng.standard_normal(SAMPLE_RATE * s) * 0.05).astype(np.float32)
        enc.embed(x)
        t = time.perf_counter()
        enc.embed(x)
        print(f"{s} s of audio: {(time.perf_counter() - t) * 1000.0:.0f} ms")
    return 0
