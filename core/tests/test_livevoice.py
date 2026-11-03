"""Call mode: the listener's mic never turns the other person's own voice into an utterance (pipeline/livevoice.py).

A device with ``original_voice`` that plays out loud pauses its mic while another person is voicing (VAD start ->
pause / end) plus a short tail. Off for everyone else: the same room is unchanged."""

from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from pipeline_helpers import Harness, not_implemented

from raum.events import ParticipantInfo
from raum.pipeline.livevoice import LiveVoiceGuard
from raum.verify import guard

X = ParticipantInfo(id="a-doc1", role="a", lang="de", path="tunnel")
Y = ParticipantInfo(id="b-ru01", role="b", lang="ru", path="tunnel", original_voice=True)


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


def people(*ps: ParticipantInfo) -> dict[str, ParticipantInfo]:
    return {p.id: p for p in ps}


def test_guard_pauses_only_a_call_listener_while_another_person_voices():
    g = LiveVoiceGuard(tail_s=0.4)
    ps = people(X, Y)
    assert not g.is_paused(Y.id, ps, now=0.0)  # nobody voicing
    g.start(X.id)
    assert g.is_paused(Y.id, ps, now=1.0)
    assert not g.is_paused(X.id, ps, now=1.0)  # never by its own voice
    g.stop(X.id, now=2.0)
    assert g.is_paused(Y.id, ps, now=2.3)  # the tail: X's last words still on their way
    assert not g.is_paused(Y.id, ps, now=2.5)

    g.start(X.id)
    for other in (Y.model_copy(update={"original_voice": False}),  # the same room: unchanged
                  Y.model_copy(update={"audio_out": "headphones"})):  # nobody else hears it
        assert not g.is_paused(Y.id, people(X, other), now=3.0)
    obs = ParticipantInfo(id="t-table", role="observer", lang="de")
    g2 = LiveVoiceGuard()
    g2.start(obs.id)
    assert not g2.is_paused(Y.id, people(X, Y, obs), now=0.0)  # an observer is not a voice in the call
    assert not g.is_paused("b-unknown", ps, now=3.0)


async def test_pipeline_refuses_the_listeners_start_while_the_other_person_voices():
    h = Harness(participants=[X, Y])
    p = h.p
    assert p.mic_open(Y.id)
    utt = await p.speech_start(X.id)
    assert utt is not None
    assert not p.mic_open(Y.id)  # the worker feeds Y's VAD silence
    assert await p.speech_start(Y.id) is None  # the other person's voice from Y's speaker: not speech
    assert p.mic_open(X.id)  # Y's refused start does not pause X back
    for _ in range(25):
        await p.speech_audio(X.id, np.zeros(320, dtype=np.float32))
    await p.speech_end(X.id, None, 300.0)
    await p.drain()
    p.live_voice.tail_s = 0.0  # the tail elapsed
    assert p.mic_open(Y.id)
    assert await p.speech_start(Y.id) is not None  # Y's reply is heard


async def test_the_pause_lifts_at_the_vad_pause_not_after_the_end_of_turn_wait():
    h = Harness(participants=[X, Y])
    p = h.p
    await p.speech_start(X.id)
    p.live_voice.stop(X.id, now=0.0)  # VAD END_OF_SPEECH (speech_pause): the utterance may stay open for eot
    assert X.id in p._active  # still the open utterance
    assert p.mic_open(Y.id)  # ... but X is quiet: Y's first words are not dropped


async def test_an_utterance_the_listener_began_first_runs_to_its_end():
    h = Harness(participants=[X, Y])
    p = h.p
    assert await p.speech_start(Y.id) is not None
    await p.speech_start(X.id)  # both talk at once
    assert p.mic_open(Y.id)  # Y's own utterance is not cut


async def test_same_room_devices_are_never_paused():
    same = Y.model_copy(update={"original_voice": False, "path": "direct"})
    h = Harness(participants=[X, same])
    await h.p.speech_start(X.id)
    assert h.p.mic_open(same.id) and await h.p.speech_start(same.id) is not None


def test_metadata_carries_original_voice():
    from raum.agent.worker import participant_info

    def part(meta, attrs=None):
        return SimpleNamespace(identity="b-ru01", metadata=json.dumps(meta), attributes=attrs or {}, kind=0)

    assert participant_info(part({"role": "b", "lang": "ru", "original_voice": True})).original_voice is True
    assert participant_info(part({"role": "b", "lang": "ru", "original_voice": False},
                                 {"original_voice": "False"})).original_voice is False
    assert participant_info(part({"role": "b"}, {"original_voice": "True"}), prefer="attributes").original_voice
    assert participant_info(part({"role": "b"})).original_voice is False  # older tokens: off


def test_participant_update_sets_original_voice(tmp_path):
    from fastapi.testclient import TestClient

    from raum.api.server import create_app

    with TestClient(create_app()) as c:
        j = c.post("/api/join", json={"room": "sz-7002", "role": "b", "lang": "ru", "consent": True}).json()
        r = c.post("/api/rooms/sz-7002/participant", json={"identity": j["identity"], "original_voice": True})
        assert r.status_code == 200 and r.json()["original_voice"] is True and r.json()["lang"] == "ru"
        r = c.post("/api/rooms/sz-7002/participant", json={"identity": j["identity"], "lang": "uk"})
        assert r.json()["original_voice"] is True  # kept
        r = c.post("/api/rooms/sz-7002/participant", json={"identity": j["identity"], "original_voice": False})
        assert r.json()["original_voice"] is False
