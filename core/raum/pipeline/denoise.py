"""Noise cleaning (plan §6.1, §6.3). Works at 48 kHz (DeepFilterNet native).

``passthrough`` returns the audio unchanged. ``deepfilternet`` tries the optional ``df`` package
(DeepFilterNet3); if it is missing or fails to load, it logs one line and passes audio through.
The model loads once per agent process; ``raum.pipeline.clean`` runs it off the event loop on the
final ASR audio only.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import numpy as np

log = logging.getLogger("raum.pipeline.denoise")


class PassthroughDenoiser:
    name = "passthrough"
    active = False  # the worker skips the 48 kHz path when nothing would be cleaned

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        return audio


def _shim_torchaudio_backend() -> None:
    """deepfilternet 0.5.6 imports ``torchaudio.backend.common.AudioMetaData`` (type hints only); the module
    is gone in torchaudio >= 2.9. Provide a stub so the package imports on current torch."""
    import sys
    import types

    try:
        import torchaudio.backend.common  # type: ignore[import-not-found]  # noqa: F401
    except ModuleNotFoundError:
        backend = types.ModuleType("torchaudio.backend")
        common = types.ModuleType("torchaudio.backend.common")

        class AudioMetaData:
            pass

        common.AudioMetaData = AudioMetaData  # type: ignore[attr-defined]
        backend.common = common  # type: ignore[attr-defined]
        sys.modules.setdefault("torchaudio.backend", backend)
        sys.modules.setdefault("torchaudio.backend.common", common)


class DeepFilterNetDenoiser:
    """DeepFilterNet3 via the optional ``deepfilternet`` package (``from df.enhance import ...``)."""

    name = "deepfilternet"
    active = True

    def __init__(self, options: dict[str, Any] | None = None) -> None:
        _shim_torchaudio_backend()
        import torch  # type: ignore[import-not-found]

        torch.set_num_threads(int((options or {}).get("threads", 1)))  # many threads stall the agent loop
        from df.enhance import enhance, init_df  # type: ignore[import-not-found]

        self.options = options or {}
        self._enhance = enhance
        # log_file=None: no enhance.log in the working directory; model_dir: an unpacked DeepFilterNet3
        # folder (else the weights download once into the user cache dir).
        self._model, self._state, _ = init_df(model_base_dir=self.options.get("model_dir"), log_file=None)
        self._sr = int(self._state.sr())

    def process(self, audio: np.ndarray, sample_rate: int) -> np.ndarray:
        if sample_rate != self._sr or audio.size == 0:
            return audio
        import torch  # type: ignore[import-not-found]

        x = torch.from_numpy(np.ascontiguousarray(audio, dtype=np.float32)).unsqueeze(0)
        y = self._enhance(self._model, self._state, x)
        return y.squeeze(0).cpu().numpy().astype(np.float32)


# One model per agent process (rooms share it; loaded in prewarm). A failed load is remembered too,
# so a missing package logs once instead of once per room.
_loaded: dict[str, PassthroughDenoiser | DeepFilterNetDenoiser] = {}


def make_denoiser(engine: str, options: dict[str, Any] | None = None) -> PassthroughDenoiser | DeepFilterNetDenoiser:
    if engine in ("passthrough", "off", "none", "mock"):
        return PassthroughDenoiser()
    if engine == "deepfilternet":
        key = json.dumps(options or {}, sort_keys=True, default=str)
        if key in _loaded:
            return _loaded[key]
        try:
            d: PassthroughDenoiser | DeepFilterNetDenoiser = DeepFilterNetDenoiser(options)
        except Exception as e:  # noqa: BLE001  ImportError, model download, torch missing
            log.warning("deepfilternet unavailable (%s): passing audio through", e.__class__.__name__)
            d = PassthroughDenoiser()
        _loaded[key] = d
        return d
    raise ValueError(f"unknown denoise engine: {engine!r}")
