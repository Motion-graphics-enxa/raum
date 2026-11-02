"""N-02 deal glossary upload: YAML/CSV parsing, merge, ``POST /api/rooms/{room}/glossary``, the
``glossary`` control to the agent (and its re-send on relink), and the pipeline side."""

from __future__ import annotations

import pytest
from core_doubles import FakePublisher, install_security, make_config
from fastapi.testclient import TestClient
from pipeline_helpers import Harness

from raum.api.server import create_app
from raum.config import repo_root
from raum.events import Control, ParticipantInfo
from raum.glossary import Glossary, GlossaryError, parse_upload, term_to_dict
from raum.link import KEY_HEADER

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")

ROOM = "vr-3305"
KEY = {KEY_HEADER: "dev-internal"}

CSV = "de,en,tr,ar\nSkonto,cash discount|early payment discount,erken ödeme indirimi,خصم نقدي\n" \
      "Zahlungsziel,payment period,ödeme süresi,\n"
YAML = """
# our deal terms
terms:
  - de: Skonto
    en: [cash discount]
  - de: Petrov
    kind: name
    keep: true
    ru: Петров
"""


# ---------------------------------------------------------------- parsing


def test_parse_csv_variants():
    terms = parse_upload(CSV, filename="deal.csv")
    assert [t.de for t in terms] == ["Skonto", "Zahlungsziel"]
    assert terms[0].forms == {"en": ["cash discount", "early payment discount"], "tr": ["erken ödeme indirimi"],
                              "ar": ["خصم نقدي"]}
    assert terms[1].forms == {"en": ["payment period"], "tr": ["ödeme süresi"]}  # empty cell: no form
    excel = "﻿DE;EN;kind;keep\nMüller;Mueller;name;ja\n\n"  # German Excel: BOM, semicolons
    (t,) = parse_upload(excel)
    assert (t.de, t.kind, t.keep, t.forms) == ("Müller", "name", True, {"en": ["Mueller"]})
    tsv = parse_upload("de\ten\nFrist\tdeadline\n", content_type="text/tab-separated-values")
    assert tsv[0].forms == {"en": ["deadline"]}


def test_parse_yaml_and_the_shipped_glossaries():
    terms = parse_upload(YAML, filename="deal.yml")
    assert [term_to_dict(t) for t in terms] == [{"de": "Skonto", "kind": "term", "en": ["cash discount"]},
                                                {"de": "Petrov", "kind": "name", "keep": True, "ru": ["Петров"]}]
    assert parse_upload("- de: Frist\n  en: deadline\n")[0].forms == {"en": ["deadline"]}  # bare list
    for p in sorted((repo_root() / "glossaries").glob("*.yaml")):  # the shipped files are valid uploads
        assert parse_upload(p.read_text(encoding="utf-8"), filename=p.name)


@pytest.mark.parametrize("text,kw,msg", [
    ("", {}, "empty"),
    ("en,tr\nx,y\n", {}, "'de'"),
    ("de,english\nx,y\n", {}, "language code"),
    ("terms: [{en: x}]", {"filename": "a.yaml"}, "'de'"),
    ("terms: {de: x", {"filename": "a.yaml"}, "YAML"),
    ("de,en\n", {}, "no terms"),
    ("x", {"fmt": "xml"}, "unknown format"),
])
def test_parse_rejects(text, kw, msg):
    with pytest.raises(GlossaryError, match=msg):
        parse_upload(text, **kw)


def test_merge_prefers_uploaded_forms():
    g = Glossary.load("glossaries/deal-de.yaml")
    n = len(g.terms)
    added, updated = g.merge(parse_upload("de,en\nZahlungsziel,payment terms (net)\nSkonto,cash discount\n"))
    assert (added, updated) == (1, 1) and len(g.terms) == n + 1
    zt = next(t for t in g.terms if t.de == "Zahlungsziel")
    assert zt.forms["en"] == ["payment terms (net)", "payment terms"] and zt.forms["ar"] == ["مهلة الدفع"]
    assert "Skonto = cash discount" in g.prompt_lines("de", "en") and "en" in g.langs()


# ---------------------------------------------------------------- API + agent link


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    app = create_app(make_config(tmp_path))
    app.state.core.sessions.publisher = FakePublisher()
    return app


@pytest.fixture
def core(app):
    return app.state.core


@pytest.fixture
def client(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def test_upload_csv_before_anyone_joined_then_agent_links(client, core):
    r = client.post(f"/api/rooms/{ROOM}/glossary?by=a", content=CSV.encode(), headers={"content-type": "text/csv"})
    assert r.status_code == 200, r.text
    res = r.json()
    # Zahlungsziel is in glossaries/deal-de.yaml already: its uploaded forms become the preferred ones
    assert (res["terms"], res["added"], res["updated"], res["forwarded"]) == (2, 1, 1, False)
    assert res["total"] == 6 and res["langs"] == ["ar", "de", "en", "tr"]
    s = core.sessions.rooms[ROOM].session
    assert s is not None and s.status == "live"
    audit = [e for e in core.sessions.audit.entries(session=s.id) if e["event"] == "glossary_upload"]
    assert audit[0]["meta"] == {"by": "a", "terms": 2, "added": 1, "updated": 1}  # counts, never terms

    with client.websocket_connect(f"/api/internal/agent/{ROOM}", headers=KEY) as agent:
        assert agent.receive_json()["kind"] == "state"
        resend = agent.receive_json()["control"]  # the fresh agent gets the uploads it missed
        assert resend["action"] == "glossary" and [t["de"] for t in resend["value"]["terms"]] == ["Skonto",
                                                                                                    "Zahlungsziel"]
        body = b"--b\r\nContent-Disposition: form-data; name=\"by\"\r\n\r\na\r\n" \
               b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"deal.yaml\"\r\n" \
               b"Content-Type: application/octet-stream\r\n\r\n" + YAML.encode() + b"\r\n--b--\r\n"
        r = client.post(f"/api/rooms/{ROOM}/glossary", content=body,
                        headers={"content-type": "multipart/form-data; boundary=b"})
        assert r.status_code == 200, r.text
        assert (r.json()["added"], r.json()["updated"], r.json()["forwarded"]) == (1, 1, True)
        msg = agent.receive_json()
        assert msg["kind"] == "control" and msg["control"]["action"] == "glossary" and msg["control"]["by"] == "a"
        assert msg["control"]["value"]["terms"][1] == {"de": "Petrov", "kind": "name", "keep": True, "ru": ["Петров"]}

    g = client.get(f"/api/rooms/{ROOM}/glossary").json()
    assert g["source"] == "glossaries/deal-de.yaml" and g["uploaded"] == 4
    skonto = next(t for t in g["terms"] if t["de"] == "Skonto")
    assert skonto["en"][0] == "cash discount" and skonto["ar"] == ["خصم نقدي"]

    # a V-05 correction is mirrored too, and a burn forgets uploads and corrections
    client.post("/api/join", json={"room": ROOM, "role": "a", "lang": "de", "consent": True})
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "correct_term", "by": "a",
                                                    "value": {"src": "Skonto", "tgt": "إسقاط", "lang": "ar"}})
    g = client.get(f"/api/rooms/{ROOM}/glossary").json()
    assert next(t for t in g["terms"] if t["de"] == "Skonto")["ar"][0] == "إسقاط"
    client.post(f"/api/rooms/{ROOM}/burn")
    g = client.get(f"/api/rooms/{ROOM}/glossary").json()
    assert g["uploaded"] == 0 and not any(t["de"] == "Skonto" for t in g["terms"]) and len(g["terms"]) == 5


def test_upload_errors_and_control_route(client, core):
    r = client.post(f"/api/rooms/{ROOM}/glossary", content=b"en,tr\nx,y\n", headers={"content-type": "text/csv"})
    assert r.status_code == 400 and r.json()["detail"].startswith("glossary:")
    r = client.post("/api/rooms/zz-0000/glossary", content=CSV.encode(), headers={"content-type": "text/csv"})
    assert r.status_code == 404
    r = client.post(f"/api/rooms/{ROOM}/glossary", content=b"x" * (600 * 1024), headers={"content-type": "text/csv"})
    assert r.status_code == 413
    r = client.post(f"/api/rooms/{ROOM}/glossary?format=yaml", content=b"- de: Frist\n  en: deadline\n",
                    headers={"content-type": "text/plain"})
    assert r.status_code == 200 and r.json()["terms"] == 1
    # the same as a control with terms in the YAML shape
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "glossary", "by": "a",
                                                        "value": {"terms": [{"de": "Anzahlung", "en": "deposit"}]}})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert any(t["de"] == "Anzahlung" for t in client.get(f"/api/rooms/{ROOM}/glossary").json()["terms"])
    r = client.post(f"/api/rooms/{ROOM}/control", json={"action": "glossary", "value": {"terms": [{"en": "x"}]}})
    assert r.status_code == 400


# ---------------------------------------------------------------- pipeline


async def test_pipeline_merges_the_glossary_into_prompt_and_forgets_it_on_burn():
    ceo = ParticipantInfo(id="a-ceo1", role="a", lang="de")
    partner = ParticipantInfo(id="b-ptr1", role="b", lang="ar")
    h = Harness(mode="verhandlungsraum", room=ROOM, participants=[ceo, partner])
    seen: list[str] = []
    inner = h.engines.translate

    class Spy:
        name = "spy"

        def stream(self, req):
            seen.append(req.glossary)
            return inner.stream(req)

    h.engines.translate = Spy()  # type: ignore[assignment]
    terms = [term_to_dict(t) for t in parse_upload(CSV)]
    await h.p.handle_control(Control(action="glossary", by="a", value={"terms": terms}))
    assert "Skonto = خصم نقدي" in h.p.glossary.prompt_lines("de", "ar")
    await h.say(ceo.id)
    assert "Skonto = خصم نقدي" in seen[-1] and "Zahlungsziel = مهلة الدفع" in seen[-1]
    await h.p.handle_control(Control(action="burn"))
    assert "Skonto" not in h.p.glossary.prompt_lines("de", "ar")
    await h.p.aclose()
