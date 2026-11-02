"""Second-model check (plan §6.9): back-translation, similarity thresholds, fact-guard red wins."""

from __future__ import annotations

import asyncio
import math

import pytest

from raum.config import VerifyThresholds
from raum.events import FactCheck
from raum.verify.backtranslate import second_check
from raum.verify.guard import GuardResult
from raum.verify.similarity import badge_for, cosine

TH = VerifyThresholds(green=0.86, amber=0.78)
OK = GuardResult(facts=[FactCheck(kind="dose", src="500 mg", tgt="500 mg", match=True)], red=False)
RED = GuardResult(facts=[FactCheck(kind="dose", src="500 mg", tgt="50 mg", match=False)], red=True)


class Verifier:
    name = "stub"

    def __init__(self, back: str = "back", delay: float = 0.0, fail: Exception | None = None) -> None:
        self.back, self.delay, self.fail = back, delay, fail
        self.calls: list[tuple[str, str, str]] = []

    async def back_translate(self, text, text_lang, into_lang, turn=None):
        self.calls.append((text, text_lang, into_lang))
        await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        return self.back


class Embedder:
    """Returns two unit vectors with the requested cosine."""

    name = "stub"

    def __init__(self, s: float) -> None:
        self.s = s

    async def embed(self, texts):
        return [[1.0, 0.0], [self.s, math.sqrt(max(0.0, 1 - self.s * self.s))]]


def test_cosine_and_badges():
    assert cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert cosine([0, 0], [1, 0]) == 0.0
    with pytest.raises(ValueError):
        cosine([1], [1, 2])
    assert [badge_for(s, TH) for s in (0.95, 0.86, 0.85, 0.78, 0.77)] == ["green", "green", "amber", "amber", "red"]


@pytest.mark.parametrize("s,badge", [(0.93, "green"), (0.80, "amber"), (0.50, "red")])
async def test_similarity_badges(s, badge):
    v = Verifier(back="Nehmen Sie 500 mg.")
    r = await second_check("Nehmen Sie 500 mg.", "de", "خذ 500 ملغ.", "ar", verifier=v, embedder=Embedder(s),
                           thresholds=TH, guard=OK)
    assert r.badge == badge and r.similarity == pytest.approx(s, abs=1e-3)
    assert r.back_translation == "Nehmen Sie 500 mg."
    assert v.calls == [("خذ 500 ملغ.", "ar", "de")]  # target text back into the source language
    if badge == "amber":
        assert any("unsure" in x for x in r.reasons)
    if badge == "red":
        assert any("meaning differs" in x for x in r.reasons)


async def test_fact_guard_red_always_wins():
    r = await second_check("a", "de", "b", "ar", verifier=Verifier(), embedder=Embedder(1.0), thresholds=TH, guard=RED)
    assert r.badge == "red" and r.similarity == pytest.approx(1.0)
    assert "fact mismatch (dose): 500 mg -> 50 mg" in r.reasons


@pytest.mark.parametrize("guard,available,badge", [(OK, True, "green"), (RED, True, "red"), (None, False, "amber")])
async def test_verify_off_uses_the_guard_only(guard, available, badge):
    r = await second_check("a", "de", "b", "ar", verifier=None, embedder=None, thresholds=TH, guard=guard,
                           guard_available=available)
    assert r.badge == badge and r.similarity is None and r.back_translation is None
    assert any("second check off" in x for x in r.reasons)


async def test_checker_failure_and_timeout_give_a_reason():
    r = await second_check("a", "de", "b", "ar", verifier=Verifier(fail=ConnectionError("down")),
                           embedder=Embedder(1.0), thresholds=TH, guard=OK)
    assert r.badge == "green" and r.similarity is None and "second check failed: ConnectionError" in r.reasons
    r = await second_check("a", "de", "b", "ar", verifier=Verifier(delay=1.0), embedder=Embedder(1.0),
                           thresholds=TH, guard=None, guard_available=False, timeout_s=0.02)
    assert r.badge == "amber" and "second check failed: TimeoutError" in r.reasons
    assert "fact guard unavailable" in r.reasons


async def test_no_embedder():
    r = await second_check("a", "de", "b", "ar", verifier=Verifier(), embedder=None, thresholds=TH, guard=OK)
    assert r.similarity is None and r.back_translation == "back" and r.badge == "green"
