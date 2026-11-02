"""Trim the silence Chatterbox puts at both edges of every request, while streaming (v1.4).

T3 starts a clause with 20-130 ms of silence and ends it with 360-640 ms before its end-of-speech token (box,
2 Nov: 22 % of the received audio was silence). RAUM sends one request per clause and plays the clauses back to
back, so every join was a 0.5-0.6 s pause mid-sentence ("short pauses between words"). :class:`EdgeTrim` keeps
``lead_s`` before the first sound and ``tail_s`` after the last one, with short fades, and never cuts into sound
(a sample louder than ``THR``).

Streaming: the engine's chunks go through ``push`` in order, then ``end`` once the clause is complete.
- Lead: until the first sound, audio is held (only its last ``lead_s`` is kept). The chunk with the first sound
  goes out at once, so first audio is never later than before (earlier, by the silence cut).
- Tail: a chunk that ends in silence goes out up to ``tail_s`` after its last sound; the rest of that silence is
  held back. More sound follows: the held silence goes out first, unchanged (a pause inside the clause stays as it
  was). ``end``: the held silence is dropped and the kept tail ends with a ``FADE_OUT_S`` fade to zero.
  Only silence is ever held, so sound is never delayed.

Pure numpy, tested without the model: ``services/tts/tests/test_tts_edges.py``.
"""
from __future__ import annotations

import numpy as np

SR = 24000
THR = 10 ** (-46 / 20)  # 0.005, -46 dBFS: a quieter sample at an edge counts as silence (box: tails at -60..-75)
FADE_IN_S, FADE_OUT_S = 0.005, 0.010
_EMPTY = np.zeros(0, dtype=np.float32)


class EdgeTrim:
    """One clause's edges. ``lead_s`` / ``tail_s`` None: that edge stays as the engine made it."""

    def __init__(self, lead_s: float | None, tail_s: float | None, sr: int = SR, thr: float = THR) -> None:
        self.keep_lead = None if lead_s is None else max(0, round(lead_s * sr))
        self.keep_tail = None if tail_s is None else max(0, round(tail_s * sr))
        self.thr = thr
        self.fade_in = max(1, int(FADE_IN_S * sr))
        self.fade_out = max(1, int(FADE_OUT_S * sr))
        self.lead = self.keep_lead is not None  # still before the first sound
        self._pre = _EMPTY  # lead: the last keep_lead samples of the silence so far
        self._pre_n = 0  # lead: samples of silence seen so far
        self._held = _EMPTY  # tail: silence held back after the last sound
        self._quiet = 0  # tail: samples of silence sent since the last sound
        self.trimmed = 0  # samples dropped at both edges (for the log)

    def _loud(self, x: np.ndarray) -> np.ndarray:
        return np.flatnonzero(np.abs(x) > self.thr)

    def push(self, x: np.ndarray) -> np.ndarray:
        """The next engine chunk in; what may go out now (possibly empty)."""
        x = np.asarray(x, dtype=np.float32).reshape(-1)
        if self.lead:
            x = self._lead(x)
            if not len(x):
                return x
        return x if self.keep_tail is None else self._tail(x)

    def _lead(self, x: np.ndarray) -> np.ndarray:
        keep = self.keep_lead or 0
        loud = self._loud(x)
        if not loud.size:  # still silence: keep only its last ``keep`` samples
            self._pre_n += len(x)
            pre = np.concatenate([self._pre, x])
            self._pre = pre[max(0, len(pre) - keep):] if keep else _EMPTY
            return _EMPTY
        i = int(loud[0])
        head = np.concatenate([self._pre, x[:i]])
        head = head[max(0, len(head) - keep):] if keep else _EMPTY
        cut = self._pre_n + i - len(head)
        out = np.concatenate([head, x[i:]])
        if cut > 0:
            self.trimmed += cut
            n = min(self.fade_in, len(out))
            out[:n] *= np.arange(n, dtype=np.float32) / n  # from zero: no click at the cut
        self.lead, self._pre = False, _EMPTY
        return out

    def _tail(self, x: np.ndarray) -> np.ndarray:
        held = self._held
        buf = np.concatenate([held, x]) if len(held) else x
        self._held = _EMPTY
        loud = self._loud(x)  # the held part is silence by construction
        if loud.size:  # sound (again): the held silence was a pause inside the clause, it goes out whole
            start = len(held) + int(loud[-1]) + 1
            self._quiet = 0
        else:
            start = 0
        room = max(0, max(0, self.keep_tail - self.fade_out) - self._quiet)
        cut = min(len(buf), start + room)
        self._quiet += cut - start
        self._held = buf[cut:]
        return buf[:cut]

    def end(self) -> np.ndarray:
        """The clause is complete: the end of the kept tail, faded to zero (empty if nothing was cut)."""
        held, self._held = self._held, _EMPTY
        if self.lead:  # never any sound
            self.trimmed += self._pre_n
            self._pre, self._pre_n = _EMPTY, 0
        if not len(held):
            return _EMPTY
        n = min(self.fade_out, len(held))
        self.trimmed += len(held) - n
        return held[:n] * (1.0 - np.arange(1, n + 1, dtype=np.float32) / n)
