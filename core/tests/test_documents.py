"""F-07 document camera: vision engines (mock, Ollama against MockTransport), the fact check of what
was read, the in-memory multipart parser and ``POST /api/rooms/{room}/document``."""

from __future__ import annotations

import base64
import json
import tempfile

import httpx
import pytest
from core_doubles import FakePublisher, install_security, make_config, recv_until
from fastapi import HTTPException
from fastapi.testclient import TestClient

from raum.api.forms import header_params, parse_multipart
from raum.api.server import create_app
from raum.config import load_config
from raum.documents import check_fact, is_heic, sniff_image, to_event
from raum.events import EVENT_TYPES, make_event, parse_event
from raum.pipeline.engines import build_vision
from raum.pipeline.vision import SCHEMA, MockVision, OllamaVision, parse_reading

pytestmark = pytest.mark.filterwarnings("ignore:The HMAC key")

ROOM = "sz-4821"
MARK = b"PHOTO-PIXELS-7f3a"  # stands in for the letter's pixels: must never reach the disk
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + MARK * 64 + b"\xff\xd9"


# ---------------------------------------------------------------- engines


async def test_mock_vision_reads_the_rooms_demo_document():
    v = MockVision({"latency_ms": 1}, rng=None)
    r = await v.read(JPEG, lang="ar", mode="sprechzimmer")
    assert r.source_lang == "de" and "د. فيبر" in r.title and "500" in r.text
    assert [f["kind"] for f in r.facts] == ["dose", "date"]
    assert r.facts[0]["value"].startswith("500 ملغ") and r.facts[0]["source"].startswith("Metformin 500 mg")
    r = await v.read(JPEG, lang="uk", mode="kanzlei")  # no Ukrainian text: English, marked
    assert r.title.startswith("[uk] ") and any(f["uncertain"] for f in r.facts)
    with pytest.raises(ValueError):
        await v.read(b"", lang="de")


def test_build_vision_per_profile(monkeypatch):
    monkeypatch.setenv("RAUM_PROFILE", "mock")
    assert isinstance(build_vision(load_config()), MockVision)
    monkeypatch.setenv("RAUM_PROFILE", "lite")
    assert build_vision(load_config()) is None
    monkeypatch.setenv("RAUM_PROFILE", "spark")
    cfg = load_config()
    v = build_vision(cfg)
    assert isinstance(v, OllamaVision) and v.model == "gemma4:26b"
    # the vision model is the translator's gemma4: same num_ctx, or Ollama reloads it mid-conversation
    assert cfg.models.vision.model == cfg.models.translate.model
    assert v.options["num_ctx"] == cfg.models.translate.options["num_ctx"] == 8192
    assert v.options["think"] is False


def client_for(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_ollama_vision_request_and_answer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        answer = {"title": "رسالة من الطبيب", "source_lang": "de", "text": "خذي 500 ملغ مرتين يوميًا.",
                  "facts": [{"kind": "dose", "value": "500 ملغ، مرتين يوميًا", "source": "500 mg, 2 x täglich",
                             "uncertain": False},
                            {"kind": "Frist", "value": "12 نوفمبر", "source": "12.11."},
                            {"kind": "amount", "value": ""}]}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": json.dumps(answer)},
                                         "done": True})

    v = OllamaVision("http://ollama:11434/", model="gemma4:26b",
                     options={"think": False, "num_ctx": 8192, "temperature": 0.1, "timeout_s": 5},
                     client=client_for(handler))
    r = await v.read(JPEG, lang="ar", mode="sprechzimmer")
    body = seen[0]
    assert body["model"] == "gemma4:26b" and body["stream"] is False and body["keep_alive"] == -1
    assert body["think"] is False
    assert body["options"]["num_ctx"] == 8192 and body["options"]["num_predict"] == 1024
    assert "timeout_s" not in body["options"]
    assert body["format"] == SCHEMA
    system, user = body["messages"]
    assert system["role"] == "system" and "for a person who reads Arabic" in system["content"]
    assert "every date, amount, dose and deadline" in system["content"] and "{lang}" not in system["content"]
    assert user["images"] == [base64.b64encode(JPEG).decode()]
    assert r.title == "رسالة من الطبيب" and r.source_lang == "de"
    assert [f["kind"] for f in r.facts] == ["dose", "deadline"]  # alias mapped, empty fact dropped
    assert v.name == "ollama:gemma4:26b"


async def test_ollama_vision_think_fallback_and_errors():
    bodies = []
    replies = iter([
        httpx.Response(400, json={"error": "\"gemma4\" does not support thinking"}),
        httpx.Response(200, json={"message": {"content": "```json\n{\"title\": \"T\", \"text\": \"x\", \"facts\": []}\n```"}}),
        httpx.Response(200, json={"message": {"content": "Just prose, no JSON."}}),
        httpx.Response(500, text="model runner crashed"),
        httpx.Response(200, json={"error": "image decode failed"}),
    ])

    def handler(request):
        bodies.append(json.loads(request.content))
        return next(replies)

    v = OllamaVision("http://o:1", model="gemma4:26b", options={"think": False}, client=client_for(handler))
    r = await v.read(JPEG, lang="de")
    assert "think" in bodies[0] and "think" not in bodies[1]
    assert r.title == "T" and r.text == "x"
    r = await v.read(JPEG, lang="de")
    assert r.text == "Just prose, no JSON." and r.facts == []
    with pytest.raises(RuntimeError, match="500"):
        await v.read(JPEG, lang="de")
    with pytest.raises(RuntimeError, match="image decode"):
        await v.read(JPEG, lang="de")


def test_parse_reading_is_tolerant():
    r = parse_reading('{"title": " A ", "text": "B", "facts": ["12.11.", {"kind": "dose", "value": "5 mg", '
                      '"uncertain": "true"}], "source_lang": "German"}')
    assert (r.title, r.text, r.source_lang) == ("A", "B", "de")
    assert r.facts == [{"kind": "number", "value": "12.11.", "source": None, "uncertain": False},
                       {"kind": "dose", "value": "5 mg", "source": None, "uncertain": True}]
    assert parse_reading("").text == "" and parse_reading({"facts": "nope"}).facts == []


# ---------------------------------------------------------------- what was read, checked by code


def test_fact_guard_marks_figures_that_changed():
    ok = {"kind": "dose", "value": "500 мг, два раза в день", "source": "Metformin 500 mg, 2 x täglich"}
    bad = {"kind": "dose", "value": "50 мг, два раза в день", "source": "Metformin 500 mg, 2 x täglich"}
    date_bad = {"kind": "date", "value": "13 ноября 2026", "source": "12.11.2026"}
    assert check_fact(ok, "de", "ru") and not check_fact(bad, "de", "ru") and not check_fact(date_bad, "de", "ru")
    assert check_fact({"kind": "amount", "value": "1,05 млн евро"}, "de", "ru")  # nothing printed to compare


async def test_every_canned_document_survives_the_guard():
    v = MockVision()
    for mode in ("sprechzimmer", "kanzlei", "verhandlungsraum"):
        for lang in ("de", "en", "tr", "ru", "ar"):
            doc = to_event(await v.read(JPEG, lang=lang, mode=mode), by="b", lang=lang, ms=1.0, model=v.name)
            flagged = [f for f in doc.facts if f.uncertain]
            # only the kanzlei salary is unsure (marked so in the demo data)
            assert [f.kind for f in flagged] == (["amount"] if mode == "kanzlei" else []), (mode, lang)


def test_document_event_contract():
    assert "document.ready" in EVENT_TYPES
    env = make_event("document.ready", ROOM, {"by": "b", "lang": "ar", "title": "t", "text": "x",
                                             "facts": [{"kind": "date", "value": "12 نوفمبر"}], "ms": 1200.5})
    data = parse_event(env.to_json()).payload()
    assert data.facts[0].uncertain is False and data.facts[0].source is None and data.source_lang is None


def test_sniff_image():
    assert sniff_image(JPEG) == "image/jpeg"
    assert sniff_image(b"\x89PNG\r\n\x1a\n" + b"\0" * 8) == "image/png"
    assert sniff_image(b"RIFF\0\0\0\0WEBPVP8 ") == "image/webp"
    assert sniff_image(b"%PDF-1.7") is None
    heic = b"\0\0\0\x18ftypheic\0\0\0\0"
    assert sniff_image(heic) is None and is_heic(heic)


# ---------------------------------------------------------------- multipart (in memory)


def multipart(fields: dict[str, str | tuple[str, bytes, str]], boundary: str = "XyZ") -> tuple[bytes, str]:
    out = b""
    for name, v in fields.items():
        out += f"--{boundary}\r\n".encode()
        if isinstance(v, tuple):
            fn, data, ct = v
            out += f'Content-Disposition: form-data; name="{name}"; filename="{fn}"\r\nContent-Type: {ct}\r\n\r\n'.encode()
            out += data + b"\r\n"
        else:
            out += f'Content-Disposition: form-data; name="{name}"\r\n\r\n{v}\r\n'.encode()
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def test_parse_multipart_binary_safe():
    tricky = JPEG + b"\r\n--XyZ-not-quite\r\n\r\n" + bytes(range(256))
    body, ctype = multipart({"by": "b", "lang": "ar", "image": ("IMG 1.jpg", tricky, "image/jpeg")})
    parts = parse_multipart(body, ctype)
    assert parts["image"].data == tricky and parts["image"].filename == "IMG 1.jpg"
    assert parts["image"].content_type == "image/jpeg"
    assert parts["by"].text() == "b" and parts["lang"].text() == "ar"
    assert header_params('form-data; name="a\\"b"; filename=x.csv') == ("form-data", {"name": 'a"b', "filename": "x.csv"})
    for bad_body, bad_type in ((body, "multipart/form-data"), (b"garbage", ctype), (body[:-12], ctype)):
        with pytest.raises(HTTPException) as e:
            parse_multipart(bad_body, bad_type)
        assert e.value.status_code == 400


# ---------------------------------------------------------------- API


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("RAUM_PROFILE", raising=False)
    install_security(monkeypatch, tmp_path)
    cfg = make_config(tmp_path)
    cfg.models.vision.options = {"latency_ms": 1}
    app = create_app(cfg)
    app.state.core.sessions.publisher = FakePublisher()
    return app


@pytest.fixture
def core(app):
    return app.state.core


@pytest.fixture
def client(app):
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def join(client, role="b", lang="ar", room=ROOM):
    r = client.post("/api/join", json={"room": room, "role": role, "lang": lang, "device": "phone", "consent": True})
    assert r.status_code == 200, r.text
    return r.json()


def post_doc(client, fields, room=ROOM):
    body, ctype = multipart(fields)
    return client.post(f"/api/rooms/{room}/document", content=body, headers={"content-type": ctype})


def test_document_endpoint_reads_broadcasts_and_stores(client, core, tmp_path, monkeypatch):
    b = join(client)

    def no_temp_files(*a, **k):
        raise AssertionError("the photo must not be spooled to a temporary file")

    for fn in ("SpooledTemporaryFile", "TemporaryFile", "NamedTemporaryFile", "mkstemp"):
        monkeypatch.setattr(tempfile, fn, no_temp_files)

    with client.websocket_connect(f"/api/events/{ROOM}") as sub:
        r = post_doc(client, {"image": ("letter.jpg", JPEG, "image/jpeg"), "by": b["identity"], "lang": "ar"})
        assert r.status_code == 200, r.text
        doc = r.json()
        got = recv_until(sub, "document.ready")
    assert got["data"] == doc
    assert doc["by"] == "b" and doc["lang"] == "ar" and doc["source_lang"] == "de"
    assert doc["model"] == "mock-gemma-vision" and doc["ms"] > 0
    assert [f["kind"] for f in doc["facts"]] == ["dose", "date"] and not any(f["uncertain"] for f in doc["facts"])
    pub = core.sessions.publisher
    assert pub.of("document.ready")[0].data == doc  # phones get it on the data channel

    s = core.sessions.rooms[ROOM].session
    stored = s.store.read_all()
    assert [x["type"] for x in stored] == ["document.ready"] and stored[0]["data"]["title"] == doc["title"]
    raw = json.dumps(stored)
    assert MARK.decode() not in raw and base64.b64encode(JPEG).decode()[:40] not in raw
    for p in tmp_path.rglob("*"):  # the photo never touched the disk, encrypted or not
        if p.is_file():
            assert MARK not in p.read_bytes(), p
    audit = [e for e in core.sessions.audit.entries(session=s.id) if e["event"] == "document"]
    assert audit and audit[0]["meta"]["facts"] == 2 and audit[0]["meta"]["lang"] == "ar"
    assert doc["text"] not in json.dumps(audit)


def test_document_endpoint_rejects(client, core):
    img = ("letter.jpg", JPEG, "image/jpeg")
    assert post_doc(client, {"image": img, "by": "b", "lang": "ar"}).status_code == 404  # no room yet
    join(client)
    assert post_doc(client, {"by": "b", "lang": "ar"}).status_code == 400
    assert post_doc(client, {"image": img, "lang": "ar"}).status_code == 400
    assert post_doc(client, {"image": img, "by": "b", "lang": "auto"}).status_code == 400
    r = post_doc(client, {"image": ("x.pdf", b"%PDF-1.7 ...", "application/pdf"), "by": "b", "lang": "ar"})
    assert r.status_code == 415
    r = post_doc(client, {"image": ("x.heic", b"\0\0\0\x18ftypheic\0\0\0\0", "image/heic"), "by": "b", "lang": "ar"})
    assert r.status_code == 415 and "HEIC" in r.json()["detail"]
    r = client.post(f"/api/rooms/{ROOM}/document", content=JPEG, headers={"content-type": "image/jpeg"})
    assert r.status_code == 415
    r = client.post(f"/api/rooms/{ROOM}/document", content=b"x",
                    headers={"content-type": "multipart/form-data; boundary=a", "content-length": str(64 * 1024 * 1024)})
    assert r.status_code == 413
    assert core.sessions.rooms[ROOM].session.store.read_all() == []
    client.post(f"/api/rooms/{ROOM}/burn")
    assert post_doc(client, {"image": img, "by": "b", "lang": "ar"}).status_code == 409


def test_document_endpoint_when_vision_is_off_or_fails(client, core):
    join(client)
    img = {"image": ("letter.jpg", JPEG, "image/jpeg"), "by": "b", "lang": "de"}
    core.sessions._vision, core.sessions._vision_ready = None, True
    r = post_doc(client, img)
    assert r.status_code == 503 and "off" in r.json()["detail"]

    class Broken:
        name = "ollama:gemma4:26b"

        async def read(self, image, *, lang, mode=None):
            raise RuntimeError("ollama 500: runner crashed")

    core.sessions._vision = Broken()
    r = post_doc(client, img)
    assert r.status_code == 502 and "could not be read" in r.json()["detail"]
    assert client.get("/api/health").json()["features"] == {"document_camera": True}


def test_document_off_the_record_is_shown_but_not_stored(client, core):
    join(client)
    client.post(f"/api/rooms/{ROOM}/control", json={"action": "off_record", "value": True})
    r = post_doc(client, {"image": ("l.jpg", JPEG, "image/jpeg"), "by": "a", "lang": "de"})
    assert r.status_code == 200 and r.json()["lang"] == "de"
    assert core.sessions.rooms[ROOM].session.store.read_all() == []
