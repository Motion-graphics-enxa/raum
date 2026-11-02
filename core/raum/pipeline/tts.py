"""Voice output (plan §6.3, C-13): ``mock``, ``piper`` (the ``piper`` binary), ``http`` (a streaming TTS
service, ``raum.pipeline.tts_http``, with Piper as its fallback), ``off``.

``synthesize(text, lang, *, voice_ref=None, rate=None)`` yields int16 PCM chunks at ``sample_rate``.
``voice_ref`` (WAV bytes of the speaker, memory only) and ``rate`` (the speaker's pace, 1.0 = 2.5 words/s)
are for engines that clone voices; mock and Piper accept and ignore them. The mock yields nothing after
the simulated first-audio delay, so the glass box still shows a ``tts_first_ms``.

Piper is the MIT-licensed binary (``options.binary``, else ``piper`` on PATH). The ``piper-tts`` PyPI
package (1.3.x is GPL) is not a dependency; it is used only if it happens to be importable and no
``binary`` is configured.

Binary mode keeps one resident process per voice per agent process: ``piper --model <voice.onnx>
--output_dir <dir>`` reads one text line on stdin and writes the wav path on stdout. ``prepare(langs)``
starts them at room join; requests per voice are served one at a time, in order; each wav is read,
deleted and streamed in chunks. If a resident process dies or hangs, that clause is spoken by a
one-shot ``piper --output-raw`` and the resident restarts (backing off if it keeps dying); until it is
ready again, clauses go one-shot. Options: ``resident`` (default true), ``output_dir`` (default
``/dev/shm`` if present: wavs never touch the disk), ``timeout_s`` (per clause, 10), ``load_timeout_s``
(model load + warm-up, 60), ``warmup`` (true), ``args`` (extra CLI arguments, e.g. ``--espeak_data``).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import json
import logging
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

import numpy as np

from raum.pipeline._util import sample_ms, sleep_ms

log = logging.getLogger("raum.pipeline.tts")

CHUNK_S = 0.1  # resident mode streams the wav in 100 ms chunks
WARMUP_TEXT = "ok."


class MockTTS:
    is_mock = True
    simulated = True  # no audio: the pipeline reports the simulated first-audio delay

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-piper"
        self.sample_rate = int(self.options.get("sample_rate", 22050))

    async def synthesize(self, text: str, lang: str, *, voice_ref: bytes | None = None,
                         rate: float | None = None, background: bool = False) -> AsyncIterator[np.ndarray]:
        await sleep_ms(sample_ms(self.options.get("first_audio_ms")))
        return
        yield  # pragma: no cover  (makes this an async generator)


# ---------------------------------------------------------------- resident piper (binary)


class PiperError(RuntimeError):
    pass


def read_wav(path: Path, *, delete: bool = True) -> tuple[np.ndarray, int]:
    """16-bit PCM wav -> (int16 mono, sample rate). Deletes the file (also when it is unreadable)."""
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    finally:
        if delete:
            with contextlib.suppress(OSError):
                path.unlink()
    if width != 2:
        raise PiperError(f"unsupported wav ({width * 8}-bit)")
    pcm = np.frombuffer(raw[: len(raw) - len(raw) % 2], dtype="<i2").astype(np.int16)
    if channels > 1:
        pcm = pcm[: len(pcm) - len(pcm) % channels].reshape(-1, channels)[:, 0].copy()
    return pcm, rate


def _discard_wav(fut: concurrent.futures.Future[Path]) -> None:
    """A caller gave up on a request that still produced a wav: delete it."""
    if fut.cancelled() or fut.exception() is not None:
        return
    with contextlib.suppress(OSError):
        fut.result().unlink()


def _default_output_dir() -> str | None:
    shm = "/dev/shm"
    return shm if os.path.isdir(shm) and os.access(shm, os.W_OK) else None


_START = object()


class ResidentPiper:
    """One resident ``piper --model <voice> --output_dir <dir>`` process. A single thread owns the process
    and serves requests in submission order, so it works from any event loop (thread executor: rooms on
    different loops share it). ``ready`` = up and warm; otherwise callers go one-shot and call ``start``."""

    def __init__(self, binary: str, model: Path, *, output_dir: str | None = None, timeout_s: float = 10.0,
                 load_timeout_s: float = 60.0, warmup: bool = True, args: list[str] | None = None) -> None:
        self.binary = binary
        self.model = Path(model)
        self.timeout_s = timeout_s
        self.load_timeout_s = load_timeout_s
        self.warmup = warmup
        self.args = list(args or [])
        self.dir = Path(tempfile.mkdtemp(prefix="raum-piper-", dir=output_dir))
        self.ready = False
        self.closed = False
        self.starts = 0  # processes started
        self.failures = 0  # consecutive failures (back-off)
        self.retry_at = 0.0
        self._fresh = False  # started without warm-up: the first request also loads the model
        self._starting = False
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._q: queue.SimpleQueue[Any] = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._run, name=f"piper-{self.model.stem}", daemon=True)
        self._thread.start()

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def start(self) -> None:
        """Start (or restart) the process in the background, unless it is up, starting, or backing off."""
        with self._lock:
            if self.closed or self.ready or self._starting or time.monotonic() < self.retry_at:
                return
            self._starting = True
        self._q.put(_START)

    def submit(self, text: str) -> concurrent.futures.Future[Path]:
        fut: concurrent.futures.Future[Path] = concurrent.futures.Future()
        if self.closed:
            fut.set_exception(PiperError("resident piper closed"))
        else:
            self._q.put((text, fut))
        return fut

    def close(self, timeout: float | None = None) -> None:
        """Stop the process (EOF on stdin) and the thread; ``timeout`` waits for it."""
        if not self.closed:
            self.closed = True
            self._q.put(None)
        if timeout is not None:
            self._thread.join(timeout)

    # ------------------------------------------------------------ the owning thread

    def _run(self) -> None:
        try:
            while True:
                item = self._q.get()
                if item is None:
                    break
                if item is _START:
                    self._boot()
                    continue
                text, fut = item
                if not fut.set_running_or_notify_cancel():
                    continue
                try:
                    path = self._ask(text, self.load_timeout_s if self._fresh else self.timeout_s)
                except Exception as e:  # noqa: BLE001
                    fut.set_exception(e)
                    if self.ready:
                        self._failed(e)
                        self.start()  # first failure: restart at once (later ones back off)
                    continue
                self._fresh = False
                self.failures = 0
                fut.set_result(path)
        finally:
            self.ready = False
            self._kill()
            while True:  # nobody serves these any more
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, tuple) and item[1].set_running_or_notify_cancel():
                    item[1].set_exception(PiperError("resident piper closed"))
            shutil.rmtree(self.dir, ignore_errors=True)

    def _boot(self) -> None:
        try:
            self._kill()
            self._sweep()
            cmd = [self.binary, "--model", str(self.model), "--output_dir", str(self.dir), *self.args]
            self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                          stderr=subprocess.DEVNULL)
            self.starts += 1
            t0 = time.monotonic()
            if self.warmup:
                wav = self._ask(WARMUP_TEXT, self.load_timeout_s)
                with contextlib.suppress(OSError):
                    wav.unlink()
            self._fresh = not self.warmup
            self.ready = True
            log.info("piper %s resident (pid %s, %.0f ms)", self.model.name, self.pid,
                     (time.monotonic() - t0) * 1000.0)
        except Exception as e:  # noqa: BLE001
            self._failed(e)
        finally:
            with self._lock:
                self._starting = False

    def _ask(self, text: str, timeout: float) -> Path:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.stdout is None or proc.poll() is not None:
            raise PiperError("resident piper not running")
        line = " ".join(text.split()).encode("utf-8") + b"\n"
        hung = threading.Event()

        def kill() -> None:
            hung.set()
            with contextlib.suppress(OSError):
                proc.kill()

        timer = threading.Timer(timeout, kill)
        timer.daemon = True
        timer.start()
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
            for _ in range(16):  # the path line; skip anything else a build might print
                raw = proc.stdout.readline()
                if not raw:
                    raise PiperError(f"no answer within {timeout:.0f} s" if hung.is_set() else "process exited")
                out = raw.decode("utf-8", "replace").strip()
                if out.endswith(".wav"):
                    break
            else:
                raise PiperError("no wav path on stdout")
        except (OSError, ValueError) as e:
            raise PiperError(f"pipe: {e}") from e
        finally:
            timer.cancel()
        path = Path(out)
        return path if path.is_absolute() else self.dir / path

    def _failed(self, e: BaseException) -> None:
        self.ready = False
        self._kill()
        self.failures += 1
        delay = 0.0 if self.failures == 1 else min(30.0, 2.0 ** (self.failures - 2))
        self.retry_at = time.monotonic() + delay
        log.warning("piper %s: resident process failed (%s); one-shot meanwhile, restart in %.0f s",
                    self.model.name, e, delay)

    def _kill(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(OSError, ValueError):
            if proc.stdin:
                proc.stdin.close()  # EOF: piper exits on its own
        try:
            proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=2.0)
        with contextlib.suppress(OSError, ValueError):
            if proc.stdout:
                proc.stdout.close()

    def _sweep(self) -> None:
        """Wavs a dead process left behind (nobody will read them)."""
        cutoff = time.time() - 5.0
        for f in self.dir.glob("*.wav"):
            with contextlib.suppress(OSError):
                if f.stat().st_mtime < cutoff:
                    f.unlink()


_RESIDENTS: dict[tuple[str, str], ResidentPiper] = {}
_RESIDENTS_LOCK = threading.Lock()


def resident_piper(binary: str, model: Path, **kwargs: Any) -> ResidentPiper:
    """The process-wide resident for this binary and voice (created on first use, not started)."""
    key = (str(binary), str(model))
    with _RESIDENTS_LOCK:
        r = _RESIDENTS.get(key)
        if r is None or r.closed:
            r = _RESIDENTS[key] = ResidentPiper(binary, model, **kwargs)
        return r


def close_residents(timeout: float = 3.0) -> None:
    with _RESIDENTS_LOCK:
        rs = list(_RESIDENTS.values())
        _RESIDENTS.clear()
    for r in rs:
        r.close()  # all at once, then wait
    for r in rs:
        r.close(timeout)


atexit.register(close_residents)


# ---------------------------------------------------------------- piper engine


class PiperTTS:
    """Piper voices from ``options.voices_dir`` (``<lang>_*.onnx`` + ``.onnx.json``) or ``options.voices``
    (``{lang: path-or-name}``). Output is resampled to ``sample_rate`` if a voice differs."""

    is_mock = False
    simulated = False

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None) -> None:
        self.options = options or {}
        self.name = f"piper:{model}" if model else "piper"
        self.sample_rate = int(self.options.get("sample_rate", 22050))
        self.voices_dir = Path(self.options.get("voices_dir", "/opt/raum/voices")).expanduser()
        self._voices: dict[str, Any] = {}
        self._lib: Any = None
        if not self.options.get("binary"):
            try:
                import piper  # type: ignore[import-not-found]

                self._lib = piper
            except Exception:  # noqa: BLE001
                self._lib = None
        self._binary = self.options.get("binary") or shutil.which("piper")
        if self._lib is None and not self._binary:
            raise RuntimeError("piper: the binary is not installed (options.binary or piper on PATH)")
        if not self._voice_files():
            raise RuntimeError(f"piper: no voices in {self.voices_dir}")
        self.resident = self._lib is None and bool(self.options.get("resident", True))
        self.args = [str(a) for a in self.options.get("args") or []]
        self._resident_opts: dict[str, Any] = {
            "output_dir": self.options.get("output_dir") or _default_output_dir(),
            "timeout_s": float(self.options.get("timeout_s", 10.0)),
            "load_timeout_s": float(self.options.get("load_timeout_s", 60.0)),
            "warmup": bool(self.options.get("warmup", True)),
            "args": self.args,
        }

    def _voice_files(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for lang, v in (self.options.get("voices") or {}).items():
            p = Path(v).expanduser()
            if not p.suffix:
                p = self.voices_dir / f"{v}.onnx"
            if p.is_file():
                out[lang] = p
        if self.voices_dir.is_dir():
            for p in sorted(self.voices_dir.glob("*.onnx")):
                lang = p.name.split("_")[0].split("-")[0]
                out.setdefault(lang, p)
        return out

    def voice_for(self, lang: str) -> Path | None:
        return self._voice_files().get(lang)

    def _resident(self, path: Path) -> ResidentPiper:
        return resident_piper(str(self._binary), path, **self._resident_opts)

    def resident_for(self, lang: str) -> ResidentPiper | None:
        path = self.voice_for(lang) if self.resident else None
        return self._resident(path) if path else None

    def prepare(self, langs: Iterable[str]) -> list[str]:
        """Start the resident processes for these languages' voices (room join, a new language).
        Idempotent and non-blocking; returns the languages that have a voice."""
        out: list[str] = []
        for lang in dict.fromkeys(langs):
            r = self.resident_for(lang)
            if r is not None:
                r.start()
                out.append(lang)
        return out

    @staticmethod
    def _voice_rate(path: Path) -> int:
        try:
            cfg = json.loads(Path(f"{path}.json").read_text(encoding="utf-8"))
            return int(cfg["audio"]["sample_rate"])
        except Exception:  # noqa: BLE001
            return 22050

    def _resample(self, pcm: np.ndarray, rate: int) -> np.ndarray:
        if rate == self.sample_rate or pcm.size == 0:
            return pcm
        import soxr

        y = soxr.resample(pcm.astype(np.float32) / 32768.0, rate, self.sample_rate)
        return (np.clip(y, -1, 1) * 32767).astype(np.int16)

    async def synthesize(self, text: str, lang: str, *, voice_ref: bytes | None = None,
                         rate: float | None = None, background: bool = False) -> AsyncIterator[np.ndarray]:
        # background (a speculative clause, see tts_http.py) is accepted and ignored: Piper has no priorities
        path = self.voice_for(lang)
        if path is None or not text.strip():
            return
        if self._lib is not None:
            rate = self._voice_rate(path)
            async for chunk in self._synth_lib(path, text):
                yield self._resample(chunk, rate)
            return
        if self.resident:
            pcm = await self._synth_resident(path, text)
            if pcm is not None:
                step = max(1, int(self.sample_rate * CHUNK_S))
                for i in range(0, len(pcm), step):
                    yield pcm[i:i + step]
                return
        rate = self._voice_rate(path)
        async for chunk in self._synth_bin(path, text):
            yield self._resample(chunk, rate)

    async def _synth_resident(self, path: Path, text: str) -> np.ndarray | None:
        """The clause from the resident process; None = speak it one-shot (not ready, died, hung)."""
        r = self._resident(path)
        if not r.ready:
            r.start()  # first use, or restart after a failure (respects the back-off)
            return None
        fut = r.submit(text)
        try:
            wav = await asyncio.wrap_future(fut)
        except asyncio.CancelledError:
            fut.add_done_callback(_discard_wav)
            raise
        except Exception as e:  # noqa: BLE001  the resident logs and restarts itself
            log.info("piper %s: one-shot for this clause (%s)", path.name, e)
            return None

        def load() -> np.ndarray:
            pcm, rate = read_wav(wav)
            return self._resample(pcm, rate)

        try:
            return await asyncio.to_thread(load)
        except Exception as e:  # noqa: BLE001
            log.warning("piper %s: unreadable wav (%s): one-shot for this clause", path.name, e)
            return None

    async def _synth_lib(self, path: Path, text: str) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[np.ndarray | BaseException | None] = asyncio.Queue()
        voice = self._voices.get(str(path))
        if voice is None:
            voice = await asyncio.to_thread(self._lib.PiperVoice.load, str(path))
            self._voices[str(path)] = voice

        def produce() -> None:
            try:
                if hasattr(voice, "synthesize_stream_raw"):  # piper-tts < 1.3
                    for b in voice.synthesize_stream_raw(text):
                        loop.call_soon_threadsafe(q.put_nowait, np.frombuffer(b, dtype=np.int16))
                else:  # piper-tts >= 1.3: AudioChunk objects
                    for c in voice.synthesize(text):
                        loop.call_soon_threadsafe(q.put_nowait, np.frombuffer(c.audio_int16_bytes, dtype=np.int16))
                loop.call_soon_threadsafe(q.put_nowait, None)
            except BaseException as e:  # noqa: BLE001
                loop.call_soon_threadsafe(q.put_nowait, e)

        threading.Thread(target=produce, name="piper", daemon=True).start()
        while True:
            item = await q.get()
            if item is None:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def _synth_bin(self, path: Path, text: str) -> AsyncIterator[np.ndarray]:
        """One-shot: a fresh ``piper --output-raw`` per clause (the fallback, and ``resident: false``)."""
        proc = await asyncio.create_subprocess_exec(
            str(self._binary), "--model", str(path), "--output-raw", *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert proc.stdin and proc.stdout
        proc.stdin.write(" ".join(text.split()).encode("utf-8") + b"\n")
        await proc.stdin.drain()
        proc.stdin.close()
        rest = b""
        try:
            while True:
                b = await proc.stdout.read(8192)
                if not b:
                    break
                b = rest + b
                cut = len(b) - (len(b) % 2)
                rest = b[cut:]
                if cut:
                    yield np.frombuffer(b[:cut], dtype=np.int16)
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()


def clones_voice(engine: Any, options: dict[str, Any] | None = None) -> bool:
    """Does this voice engine speak in an AI copy of the speaker's voice? Only ``http`` does
    (``HttpTTS.uses_voice_ref``), unless ``options.voice_ref`` is false (the pipeline then takes no
    sample). ``GET /api/health`` reports it as ``features.voice_clone``: the app offers the voice choice."""
    return engine == "http" and bool((options or {}).get("voice_ref", True))


def make_tts(engine: Any, options: dict[str, Any] | None = None, model: str | None = None, *,
             url: str | None = None, fallback: Any = "piper") -> Any:
    """``fallback`` (``http`` only): ``piper`` (default; same options), ``none``, or
    ``{engine, model, options}``. It always speaks at the service's ``sample_rate``."""
    if engine in ("off", "none", False, None):
        return None
    if engine == "mock":
        return MockTTS(options, model=model)
    if engine == "piper":
        try:
            return PiperTTS(options, model=model)
        except Exception as e:  # noqa: BLE001
            log.warning("tts disabled: %s", e)
            return None
    if engine == "http":
        from raum.pipeline.tts_http import DEFAULT_URL, HttpTTS

        opts = dict(options or {})
        rate = int(opts.get("sample_rate", 24000))
        opts["sample_rate"] = rate
        fb: Any
        if isinstance(fallback, dict):
            fb_opts = {**(fallback.get("options") or {}), "sample_rate": rate}
            fb = make_tts(fallback.get("engine", "piper"), fb_opts, fallback.get("model"), fallback=None)
        elif fallback in ("off", "none", False, None, "http"):
            fb = None
        else:
            fb = make_tts(str(fallback), opts, None, fallback=None)
        if fb is None:
            log.info("tts http: no fallback voice (text only if the service fails)")
        return HttpTTS(url or DEFAULT_URL, model=model, options=opts, fallback=fb)
    raise ValueError(f"unknown tts engine: {engine!r}")
