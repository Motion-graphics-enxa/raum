"""Voice output (plan §6.3, C-13): ``mock``, ``piper`` (optional ``piper`` package or binary), ``off``.

``synthesize`` yields int16 PCM chunks at ``sample_rate``. The mock yields nothing after the
simulated first-audio delay, so the glass box still shows a ``tts_first_ms``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import numpy as np

from raum.pipeline._util import sample_ms, sleep_ms

log = logging.getLogger("raum.pipeline.tts")


class MockTTS:
    is_mock = True
    simulated = True  # no audio: the pipeline reports the simulated first-audio delay

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None) -> None:
        self.options = options or {}
        self.name = model or "mock-piper"
        self.sample_rate = int(self.options.get("sample_rate", 22050))

    async def synthesize(self, text: str, lang: str) -> AsyncIterator[np.ndarray]:
        await sleep_ms(sample_ms(self.options.get("first_audio_ms")))
        return
        yield  # pragma: no cover  (makes this an async generator)


class PiperTTS:
    """Piper voices from ``options.voices_dir`` (``<lang>_*.onnx`` + ``.onnx.json``) or ``options.voices``
    (``{lang: path-or-name}``). Uses the ``piper`` Python package if importable, else the ``piper``
    binary (``--output-raw``). Output is resampled to ``sample_rate`` if a voice differs."""

    is_mock = False
    simulated = False

    def __init__(self, options: dict[str, Any] | None = None, *, model: str | None = None) -> None:
        self.options = options or {}
        self.name = f"piper:{model}" if model else "piper"
        self.sample_rate = int(self.options.get("sample_rate", 22050))
        self.voices_dir = Path(self.options.get("voices_dir", "/opt/raum/voices"))
        self._voices: dict[str, Any] = {}
        try:
            import piper  # type: ignore[import-not-found]

            self._lib = piper
        except Exception:  # noqa: BLE001
            self._lib = None
        self._binary = self.options.get("binary") or shutil.which("piper")
        if self._lib is None and not self._binary:
            raise RuntimeError("piper: neither the python package nor the binary is installed")
        if not self._voice_files():
            raise RuntimeError(f"piper: no voices in {self.voices_dir}")

    def _voice_files(self) -> dict[str, Path]:
        out: dict[str, Path] = {}
        for lang, v in (self.options.get("voices") or {}).items():
            p = Path(v)
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

    async def synthesize(self, text: str, lang: str) -> AsyncIterator[np.ndarray]:
        path = self.voice_for(lang)
        if path is None or not text.strip():
            return
        rate = self._voice_rate(path)
        if self._lib is not None:
            async for chunk in self._synth_lib(path, text):
                yield self._resample(chunk, rate)
        else:
            async for chunk in self._synth_bin(path, text):
                yield self._resample(chunk, rate)

    async def _synth_lib(self, path: Path, text: str) -> AsyncIterator[np.ndarray]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[np.ndarray | BaseException | None] = asyncio.Queue()
        voice = self._voices.get(str(path))
        if voice is None:
            voice = await asyncio.to_thread(self._lib.PiperVoice.load, str(path))  # type: ignore[union-attr]
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
        proc = await asyncio.create_subprocess_exec(
            str(self._binary), "--model", str(path), "--output-raw",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        assert proc.stdin and proc.stdout
        proc.stdin.write(text.encode("utf-8") + b"\n")
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
                proc.kill()
            await proc.wait()


def make_tts(engine: str, options: dict[str, Any] | None = None, model: str | None = None) -> MockTTS | PiperTTS | None:
    if engine in ("off", "none"):
        return None
    if engine == "mock":
        return MockTTS(options, model=model)
    if engine == "piper":
        try:
            return PiperTTS(options, model=model)
        except Exception as e:  # noqa: BLE001
            log.warning("tts disabled: %s", e)
            return None
    raise ValueError(f"unknown tts engine: {engine!r}")
