"""Second-model check (V-02, plan §6.9).

1. The verifier (gpt-oss, a different family from the translator) translates the translation back
   into the source language.
2. The embedder (bge-m3) embeds source and back-translation; cosine similarity ``s``.
3. ``s >= green`` green, ``amber <= s < green`` amber, else red. **Fact-guard red always wins.**
4. Verify engine off (edge/lite) or failing: the result comes from the fact guard only, with a reason.

Never raises: a failing checker yields a result with a reason, so the live path is never affected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from raum.config import VerifyThresholds
from raum.events import Badge
from raum.pipeline._ollama import WarmingUp
from raum.pipeline.base import Embedder, TurnContext, Verifier
from raum.verify.guard import GuardResult
from raum.verify.similarity import badge_for, cosine

log = logging.getLogger("raum.verify.backtranslate")


@dataclass
class CheckResult:
    similarity: float | None
    badge: Badge
    reasons: list[str] = field(default_factory=list)
    back_translation: str | None = None
    ms: float = 0.0


def guard_reasons(guard: GuardResult | None) -> list[str]:
    if guard is None:
        return []
    out = []
    for f in guard.facts:
        if not f.match:
            out.append(f"fact mismatch ({f.kind}): {f.src or '-'} -> {f.tgt or '-'}")
    return out


async def second_check(src: str, src_lang: str, tgt: str, tgt_lang: str, *, verifier: Verifier | None,
                       embedder: Embedder | None, thresholds: VerifyThresholds,
                       guard: GuardResult | None = None, guard_available: bool = True,
                       turn: TurnContext | None = None, timeout_s: float = 20.0) -> CheckResult:
    t0 = time.perf_counter()
    reasons = guard_reasons(guard)
    guard_red = bool(guard and guard.red)
    if guard is None:
        reasons.append("fact guard unavailable" if not guard_available else "fact guard not run")

    def guard_only_badge() -> Badge:
        if guard_red:
            return "red"
        return "green" if guard is not None else "amber"

    similarity: float | None = None
    back: str | None = None
    warming = False
    if verifier is None:
        reasons.append("second check off in this profile: fact guard only")
    else:
        try:
            back = await asyncio.wait_for(verifier.back_translate(tgt, tgt_lang, src_lang, turn), timeout_s)
            if embedder is None:
                reasons.append("embedder off: no similarity, fact guard only")
            else:
                vecs = await asyncio.wait_for(embedder.embed([src, back]), timeout_s)
                similarity = round(cosine(vecs[0], vecs[1]), 4)
        except asyncio.CancelledError:
            raise
        except WarmingUp:
            warming = True
            reasons.append("checker warming up")
        except Exception as e:  # noqa: BLE001  checker slow or down (§6.15): never blocks, never breaks the turn
            log.warning("second check failed: %s: %s", e.__class__.__name__, e)
            reasons.append(f"second check failed: {e.__class__.__name__}")

    if similarity is None:
        badge = guard_only_badge()
        if warming and badge == "green":
            badge = "amber"  # the second opinion isn't there yet: unsure, never a silent pass
    else:
        badge = "red" if guard_red else badge_for(similarity, thresholds)
        if similarity < thresholds.amber:
            reasons.append(f"meaning differs (similarity {similarity:.2f})")
        elif similarity < thresholds.green:
            reasons.append(f"unsure, please confirm (similarity {similarity:.2f})")
    return CheckResult(similarity=similarity, badge=badge, reasons=reasons, back_translation=back,
                       ms=(time.perf_counter() - t0) * 1000.0)
