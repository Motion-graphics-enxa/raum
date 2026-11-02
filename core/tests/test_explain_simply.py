"""K-02 explain simply, end to end: device control -> core -> agent pipeline -> ``explain.draft`` (only
the professional sees it) -> approve -> ``explain.sent`` (stored, sent to the client). Mock engines
throughout, plus the real path (gpt-oss with ``prompts/explain-simply.md``) against MockTransport."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from core_doubles import FakePublisher, install_security, make_config
from pipeline_helpers import Harness

from raum.events import Control, ParticipantInfo
from raum.link import ControlMsg, StateMsg, parse_link_message
from raum.pipeline import mockdata
from raum.pipeline.verifier import OllamaVerifier
from raum.sessions import AgentConn, SessionManager
from raum.verify import guard

ROOM = "kz-1170"
KZ03 = next(ln for ln in mockdata.load_script("kanzlei").lines if ln.id == "kz-03")  # Die Klagefrist endet ...


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    def not_implemented(*_a, **_k):
        raise NotImplementedError

    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


async def lawyer_room() -> tuple[Harness, ParticipantInfo, ParticipantInfo, str]:
    lawyer = ParticipantInfo(id="a-law1", role="a", lang="de")
    client = ParticipantInfo(id="b-cli1", role="b", lang="ar")
    h = Harness(mode="kanzlei", room=ROOM, participants=[lawyer, client])
    await h.say(lawyer.id)
    utt = await h.say(lawyer.id)  # kz-03
    await h.p.drain()
    assert h.one("transcript.final", utt)["text"] == KZ03.text["de"]
    return h, lawyer, client, utt


async def test_end_to_end_through_core(tmp_path, monkeypatch):
    install_security(monkeypatch, tmp_path)
    sm = SessionManager(make_config(tmp_path), publisher=FakePublisher())
    _, s, a = await sm.join(ROOM, role="a", lang="de", device="phone", path="direct")
    _, _, b = await sm.join(ROOM, role="b", lang="ar", device="phone", path="direct")
    h = Harness(mode="kanzlei", room=ROOM, participants=[a, b])

    collect = h.p._emit

    async def to_core(env):  # the agent worker: data channel (collected) + link to core
        await collect(env)
        await sm.ingest(ROOM, env)

    h.p._emit = to_core

    async def agent_side(raw: str) -> None:
        msg = parse_link_message(raw)
        if isinstance(msg, ControlMsg):
            await h.p.handle_control(msg.control)
        elif isinstance(msg, StateMsg):
            h.p.set_state(msg.state)

    conn = AgentConn(agent_side, agent_id="w-1")
    await sm.attach_agent(ROOM, conn)
    await h.say(a.id)
    utt = await h.say(a.id)
    await h.p.drain()

    r = await sm.control(ROOM, Control(action="explain_simply", utt=utt, by=a.id))
    assert r["forwarded"] is True
    await h.p.drain()
    draft = h.one("explain.draft", utt)
    assert draft == {"source_utt": utt, "target_lang": "ar", "text": KZ03.simple["ar"]}
    backlog = [e.type for e in sm.backlog(sm.rooms[ROOM])]
    assert "explain.draft" in backlog  # the table / professional sees the draft ...
    assert "explain.draft" not in [x["type"] for x in s.store.read_all()]  # ... but a draft is never stored

    edited = KZ03.simple["ar"] + " (24.11.)"
    r = await sm.control(ROOM, Control(action="explain_approve", utt=utt, by=a.id, text=edited))
    assert r["forwarded"] is True
    sent = h.one("explain.sent", utt)
    assert sent == {"source_utt": utt, "target_lang": "ar", "text": edited}
    stored = [x for x in s.store.read_all() if x["type"] == "explain.sent"]
    assert [x["data"]["text"] for x in stored] == [edited]
    assert "explain_approved" in [e["event"] for e in sm.audit.entries(session=s.id)]

    # the agent restarts mid-approval: core sends the approved text itself
    sm.detach_agent(ROOM, conn)
    r = await sm.control(ROOM, Control(action="explain_approve", utt=utt, by="a", text="Einfach."))
    assert r["forwarded"] is False
    published = sm.publisher.of("explain.sent")
    assert published and published[-1].data == {"source_utt": utt, "target_lang": "ar", "text": "Einfach."}
    assert [x["data"]["text"] for x in s.store.read_all() if x["type"] == "explain.sent"] == [edited, "Einfach."]
    await h.p.aclose()


def ollama_verifier(handler) -> OllamaVerifier:
    return OllamaVerifier("http://ollama:11434", model="gpt-oss:120b", options={"reasoning": "low"},
                          client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_real_path_uses_the_explain_prompt():
    h, _, _, utt = await lawyer_room()
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "Einfach gesagt:\n„Sie müssen bis 24. November klagen.“"}})

    h.engines.verify = ollama_verifier(handler)
    await h.p.handle_control(Control(action="explain_simply", utt=utt, by="a", value="tr"))
    await h.p.drain()
    prompt = seen[0]["messages"][0]["content"]
    assert prompt.startswith("Reasoning: low") and "in Turkish, at plain-language level" in prompt
    assert f"Statement (German): {KZ03.text['de']}" in prompt and "Keep every date, deadline and amount" in prompt
    assert seen[0]["think"] == "low"
    assert h.one("explain.draft", utt) == {"source_utt": utt, "target_lang": "tr",
                                            "text": "Sie müssen bis 24. November klagen."}


@pytest.mark.parametrize("failure", ["http_500", "warming_up", "empty", "verify_off"])
async def test_falls_back_to_the_translation(failure):
    h, _, _, utt = await lawyer_room()
    calls = []

    def handler(request):
        calls.append(request)
        if failure == "http_500":
            return httpx.Response(500, text="runner crashed")
        return httpx.Response(200, json={"message": {"content": "  "}})

    v = ollama_verifier(handler)
    if failure == "warming_up":
        v.gate, v.ready = True, False  # the agent gates a model that is still loading
    h.engines.verify = None if failure == "verify_off" else v
    await h.p.handle_control(Control(action="explain_simply", utt=utt, by="a"))
    await h.p.drain()
    assert h.one("explain.draft", utt)["text"] == KZ03.target("ar")  # what the client already read
    assert (calls == []) == (failure in ("warming_up", "verify_off"))


async def test_untranslated_language_is_translated_for_the_draft():
    h, _, _, utt = await lawyer_room()
    h.engines.verify = None
    await h.p.handle_control(Control(action="explain_simply", utt=utt, by="a", value={"lang": "ru"}))
    await h.p.drain()
    assert h.one("explain.draft", utt) == {"source_utt": utt, "target_lang": "ru", "text": KZ03.target("ru")}


async def test_explain_never_blocks_the_link():
    h, _, _, utt = await lawyer_room()
    release = asyncio.Event()

    class Slow:
        name = "slow"
        is_mock = False

        async def complete_text(self, prompt, *, purpose):
            await release.wait()
            return "Einfach."

    h.engines.verify = Slow()  # type: ignore[assignment]
    await asyncio.wait_for(h.p.handle_control(Control(action="explain_simply", utt=utt, by="a")), 0.5)
    await h.p.handle_control(Control(action="off_record", value=True))  # the next control is not held up
    assert h.p.off_record is True and h.of("explain.draft") == []
    release.set()
    await h.p.drain()
    assert h.one("explain.draft", utt)["text"] == "Einfach."
