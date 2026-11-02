"""Noise cleaning (plan §6.1, §6.3). Works at 48 kHz (DeepFilterNet native).

``passthrough`` returns the audio unchanged. ``deepfilternet`` tries the optional ``df`` package
(DeepFilterNet3); if it is missing or fails to load, it logs one line and passes audio through.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

log = logging.getLogger("raum.pipeline.denoise")


class PassthroughDenoiser:
    name = "passthrough"
    active = False  # the worker skips the 48 kHz path when nothing would be cleaned

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        return audio


class DeepFilterNetDenoiser:
    """DeepFilterNet3 via the optional ``deepfilternet`` package (``from df.enhance import ...``)."""

    name = "deepfilternet"
    active = True

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        from df.enhance import enhance, init_df  # type: ignore[import-not-found]

        self._enhance = enhance
        self._model, self._state, _ = init_df()
        self._sr = int(self._state.sr())
        self.options = options or {}

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != self._sr or audio.size == 0:
            return audio
        import torch  # type: ignore[import-not-found]

        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
        y = self._enhance(self._model, self._state, x)
        return y.squeeze(0).cpu().numpy().astype(np.float32)


def make_denoiser(engine: str, options: dict[str, Any] | None = None) -> PassthroughDenoiser | DeepFilterNetDenoiser:
    if engine in ("passthrough", "off", "none", "mock"):
        return PassthroughDenoiser()
    if engine == "deepfilternet":
        try:
            return DeepFilterNetDenoiser(options)
        except Exception as e:  # noqa: BLE001  ImportError, model download, torch missing
            log.warning("deepfilternet unavailable (%s): passing audio through", e.__class__.__name__)
            return PassthroughDenoiser()
    raise ValueError(f"unknown denoise engine: {engine!r}")
