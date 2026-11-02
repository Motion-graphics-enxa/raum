"""Second-model check scope: off in the Gespraech room (fact guard only, never a prompt); one check
per utterance in rooms with three or more languages (pipeline.check_targets: one)."""

from __future__ import annotations

from pipeline_helpers import DOC, Harness, fast_config

from raum.events import ParticipantInfo

RU = ParticipantInfo(id="b-ru01", role="b", lang="ru", audio_out="headphones")
EN = ParticipantInfo(id="b-en01", role="b", lang="en", audio_out="headphones")
DE = DOC.model_copy(update={"audio_out": "headphones"})


class CountingVerifier:
    """Wraps the mock verifier and counts back-translations per target language."""

    def __init__(self, inner):
        self.inner = inner
        self.calls: list[str] = []
        self.name = inner.name
        self.is_mock = True

    async def back_translate(self, text, text_lang, into_lang, turn=None):
        self.calls.append(text_lang)
        return await self.inner.back_translate(text, text_lang, into_lang, turn)

    def __getattr__(self, k):
        return getattr(self.inner, k)


def harness(mode: str, room: str, participants, check_targets: str = "one") -> tuple[Harness, CountingVerifier]:
    cfg = fast_config()
    cfg.pipeline.check_targets = check_targets
    cfg.pipeline.simultaneous = "off"  # consecutive here; simul.py shares checker_for (tested in simul tests)
    h = Harness(mode=mode, room=room, participants=participants, cfg=cfg)
    v = CountingVerifier(h.engines.verify)
    h.engines.verify = v  # type: ignore[assignment]
    return h, v


async def test_gespraech_runs_no_second_check_and_never_asks():
    gs_a = ParticipantInfo(id="a-host1", role="a", lang="de", audio_out="headphones")
    gs_b = ParticipantInfo(id="b-gst01", role="b", lang="en", audio_out="headphones")
    h, v = harness("gespraech", "gs-1000", [gs_a, gs_b])
    for who in (gs_a.id, gs_b.id, gs_a.id, gs_b.id):
        await h.say(who, seconds=0.6)
        await h.p.drain()
    assert v.calls == []  # no back-translation at all
    results = h.of("verify.result")
    assert results and all(r.data["similarity"] is None for r in results)
    assert all("second check off in this room: fact guard only" in r.data["reasons"] for r in results)
    assert not [f for f in h.of("flag") if f.data["kind"] in ("confirm", "mismatch")]  # no "Stimmt das?"
    assert not h.of("confirm")


async def test_three_languages_check_one_target_per_utterance():
    h, v = harness("sprechzimmer", "sz-1000", [DE, RU, EN])
    await h.say(DE.id, seconds=0.6)  # the doctor speaks: the first patient-side listener's language (ru)
    await h.p.drain()
    await h.say(EN.id, seconds=0.6)  # a listener speaks: the doctor's language (de)
    await h.p.drain()
    assert v.calls == ["ru", "de"]
    by = {(r.utt, r.data["target_lang"]): r.data for r in h.of("verify.result")}
    assert len(by) == 4
    guard_only = [d for d in by.values() if d["similarity"] is None]
    assert len(guard_only) == 2 and all(any("only (one per utterance)" in x for x in d["reasons"]) for d in guard_only)


async def test_check_targets_all_checks_every_target():
    h, v = harness("sprechzimmer", "sz-1000", [DE, RU, EN], check_targets="all")
    await h.say(DE.id, seconds=0.6)
    await h.p.drain()
    assert sorted(v.calls) == ["en", "ru"]


async def test_two_person_rooms_are_unchanged():
    h, v = harness("sprechzimmer", "sz-1000", [DE, RU])
    await h.say(DE.id, seconds=0.6)
    await h.p.drain()
    await h.say(RU.id, seconds=0.6)
    await h.p.drain()
    assert v.calls == ["ru", "de"]
