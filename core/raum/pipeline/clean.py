"""Streaming noise cleaning for the final ASR pass (plan §6.1; redesign after DeepFilterNet froze the
agent on the GX10).

VAD and partial captions run on the raw audio, so cleaning never delays end of speech. Each track's
48 kHz audio is also cleaned, off the event loop, on one worker thread per agent process, in blocks
(default 200 ms). Each block carries the previous 100 ms of raw audio as context, and that part of
the output is dropped. The result is resampled to 16 kHz and kept by absolute 16 kHz sample index,
the same index Silero reports as ``VADEvent.samples_index``. The final ASR pass takes the clean
samples that cover the utterance; anything not cleaned yet (the trailing silence) comes from the raw
audio, so nothing waits for the cleaner.

A realtime-factor gate turns cleaning off for the whole process (one log line) when a block takes
longer than ``max_rtf`` x its duration on average, when the worker falls ``max_lag_s`` behind, or when
the model fails. Audio then flows exactly as with ``passthrough``.

Options (``models.denoise.options``): ``block_ms`` (200), ``context_ms`` (100), ``max_rtf`` (0.3),
``max_lag_s`` (1.0), ``keep_s`` (60, clean audio kept per track).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

log = logging.getLogger("raum.pipeline.clean")

IN_RATE = 48000
OUT_RATE = 16000
_RATIO = IN_RATE // OUT_RATE


class RealtimeGate:
    """Process-wide on/off switch for cleaning, driven by the measured realtime factor."""

    def __init__(self, max_rtf: float = 0.3, *, warmup_blocks: int = 3, alpha: float = 0.2) -> None:
        self.max_rtf = float(max_rtf)
        self.warmup_blocks = int(warmup_blocks)
        self.alpha = float(alpha)
        self.rtf: float | None = None
        self.blocks = 0
        self.tripped = False
        self.reason = ""

    def record(self, proc_s: float, audio_s: float) -> None:
        r = proc_s / max(audio_s, 1e-6)
        self.rtf = r if self.rtf is None else (1.0 - self.alpha) * self.rtf + self.alpha * r
        self.blocks += 1
        if self.blocks >= self.warmup_blocks and self.rtf > self.max_rtf:
            self.trip(f"realtime factor {self.rtf:.2f} > {self.max_rtf:.2f}")

    def trip(self, reason: str) -> None:
        if not self.tripped:
            self.tripped = True
            self.reason = reason
            log.warning("noise cleaning off for this agent process: %s (ASR uses the raw audio)", reason)


def _timed(fn: Callable[[np.ndarray, int], np.ndarray], x: np.ndarray) -> tuple[np.ndarray, float]:
    t = time.perf_counter()
    y = fn(x, IN_RATE)
    return y, time.perf_counter() - t


class Cleaner:
    """One per agent process and denoiser: the worker thread, the gate and the stream settings."""

    def __init__(self, denoiser: Any, options: dict[str, Any] | None = None, *,
                 executor: ThreadPoolExecutor | None = None) -> None:
        o = options or {}
        self.denoiser = denoiser
        self.block = int(IN_RATE * float(o.get("block_ms", 200)) / 1000.0)
        self.block -= self.block % _RATIO
        self.context = int(IN_RATE * float(o.get("context_ms", 100)) / 1000.0)
        self.max_lag = int(IN_RATE * float(o.get("max_lag_s", 1.0)))
        self.keep = int(OUT_RATE * float(o.get("keep_s", 60.0)))
        self.gate = RealtimeGate(float(o.get("max_rtf", 0.3)))
        # one thread: torch runs single-threaded (denoise.py) and the model state is not thread-safe
        self.executor = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="raum-denoise")

    @property
    def usable(self) -> bool:
        return not self.gate.tripped

    def warm(self, blocks: int = 2) -> None:
        """Run a couple of silent blocks synchronously (prewarm): first calls are slow and must not
        count against the gate."""
        x = np.zeros(self.block + self.context, dtype=np.float32)
        for _ in range(blocks):
            self.denoiser.process(x, IN_RATE)

    def stream(self, on_metric: Callable[[float], None] | None = None) -> CleanStream:
        return CleanStream(self, on_metric=on_metric)


class CleanStream:
    """One per audio track. ``push`` raw 48 kHz float32 audio from the event loop; ``cleaned`` swaps
    the clean samples into an utterance's raw 16 kHz audio."""

    def __init__(self, cleaner: Cleaner, *, on_metric: Callable[[float], None] | None = None) -> None:
        import soxr

        self.c = cleaner
        self.on_metric = on_metric
        self._pending: list[np.ndarray] = []
        self._pending_n = 0
        self._ctx = np.zeros(0, dtype=np.float32)
        # same parameters as the raw path's resampler in the worker, so the two 16 kHz indexes agree
        self._rs = soxr.ResampleStream(IN_RATE, OUT_RATE, 1, dtype="float32")
        self._ring = np.zeros(cleaner.keep, dtype=np.float32)
        self.end = 0  # absolute 16 kHz index of the next clean sample
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def active(self) -> bool:
        return not self._closed and self.c.usable

    def push(self, x48: np.ndarray) -> None:
        if not self.active or x48.size == 0:
            return
        self._pending.append(np.asarray(x48, dtype=np.float32))
        self._pending_n += x48.size
        if self._pending_n > self.c.max_lag + self.c.block:
            self.c.gate.trip(f"worker {self._pending_n / IN_RATE:.1f} s behind")
            self._pending.clear()
            self._pending_n = 0
            return
        if self._pending_n >= self.c.block and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._drain())

    def _take_block(self) -> np.ndarray:
        buf = np.concatenate(self._pending) if len(self._pending) > 1 else self._pending[0]
        block, rest = buf[: self.c.block], buf[self.c.block :]
        self._pending = [rest] if rest.size else []
        self._pending_n = int(rest.size)
        return block

    async def _drain(self) -> None:
        loop = asyncio.get_running_loop()
        while self._pending_n >= self.c.block and self.active:
            block = self._take_block()
            x = np.concatenate([self._ctx, block]) if self._ctx.size else block
            try:
                y, dt = await loop.run_in_executor(self.c.executor, _timed, self.c.denoiser.process, x)
            except Exception as e:  # noqa: BLE001
                self.c.gate.trip(f"model failed ({e.__class__.__name__})")
                return
            if self._closed:
                return
            y = np.asarray(y, dtype=np.float32).reshape(-1)
            if y.size != x.size:
                self.c.gate.trip(f"model returned {y.size} samples for {x.size}")
                return
            self._ctx = block[-self.c.context :] if self.c.context else self._ctx
            self.c.gate.record(dt, block.size / IN_RATE)
            if self.on_metric is not None:
                self.on_metric(dt * 1000.0 * (IN_RATE / 10.0) / block.size)  # ms per 100 ms of audio
            self._append(self._rs.resample_chunk(y[-block.size :]))

    def _append(self, z: np.ndarray) -> None:
        n = int(z.size)
        if n == 0:
            return
        k = self.c.keep
        if n >= k:
            z, self.end = z[-k:], self.end + n - k
            n = k
        i = self.end % k
        first = min(n, k - i)
        self._ring[i : i + first] = z[:first]
        if first < n:
            self._ring[: n - first] = z[first:]
        self.end += n

    def slice(self, start: int, stop: int) -> np.ndarray:
        """Clean 16 kHz samples ``[start, min(stop, end))``; empty if ``start`` is not (or no longer) kept."""
        stop = min(stop, self.end)
        if start < 0 or start < self.end - self.c.keep or stop <= start:
            return np.zeros(0, dtype=np.float32)
        idx = np.arange(start, stop) % self.c.keep
        return self._ring[idx]

    def cleaned(self, raw16: np.ndarray, end_index: int) -> np.ndarray:
        """``raw16`` is an utterance ending at absolute index ``end_index`` (Silero's samples_index).
        Returns it with every sample the cleaner has finished replaced by its clean version."""
        clean = self.slice(end_index - raw16.size, end_index)
        if clean.size == 0:
            return raw16
        out = np.array(raw16, dtype=np.float32, copy=True)
        out[: clean.size] = clean
        return out

    def close(self) -> None:
        self._closed = True
        self._pending.clear()
        self._pending_n = 0
        if self._task is not None and not self._task.done():
            self._task.cancel()


_cleaners: dict[int, Cleaner] = {}


def cleaner_for(denoiser: Any, options: dict[str, Any] | None = None) -> Cleaner | None:
    """The process-wide cleaner for an active denoiser (None for passthrough)."""
    if denoiser is None or not getattr(denoiser, "active", False):
        return None
    c = _cleaners.get(id(denoiser))
    if c is None or c.denoiser is not denoiser:
        c = _cleaners[id(denoiser)] = Cleaner(denoiser, options)
    return c
