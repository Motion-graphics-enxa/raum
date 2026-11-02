"""Meaning similarity and badge thresholds (plan §6.9, V-02)."""

from __future__ import annotations

import math
from collections.abc import Sequence

from raum.config import VerifyThresholds
from raum.events import Badge


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise ValueError(f"vector sizes differ: {len(a)} != {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (na * nb)))


def badge_for(similarity: float, thresholds: VerifyThresholds) -> Badge:
    """``s >= green`` green, ``amber <= s < green`` amber, else red (calibrated in Sprint 3)."""
    if similarity >= thresholds.green:
        return "green"
    if similarity >= thresholds.amber:
        return "amber"
    return "red"
