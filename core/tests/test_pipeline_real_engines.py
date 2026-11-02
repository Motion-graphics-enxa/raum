"""Real engines (whisper.cpp, Ollama) against ``httpx.MockTransport``: no server, no GPU, no network."""

from __future__ import annotations

import json
import math

import httpx
import numpy as np
import pytest

from raum.pipeline.asr import WhisperCppASR
from raum.pipeline.base import TranslateRequest, TurnContext
from raum.pipeline.embed import OllamaEmbedder
from raum.pipeline.translate import OllamaTranslator
from raum.pipeline.verifier import OllamaVerifier, parse_json_object

TURN = TurnContext(room="sz-1", mode="sprechzimmer", speaker="a-1", role="a", utt="u-0001")


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def form_fields(request: httpx.Request) -> dict[str, str]:
    body = request.content
    out = {}
    for part in body.split(b"--")[1:]:
        if b'name="' not in part:
            continue
        name = part.split(b'name="', 1)[1].split(b'"', 1)[0].decode()
        value = part.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
        out[name] = value[:4].decode("latin-1") if name == "file" else value.decode()
    return out


async def test_whisper_cpp_posts_wav_and_reads_verbose_json():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["fields"] = form_fields(request)
        return httpx.Response(200, json={
            "task": "transcribe", "language": "german", "duration": 1.0, "text": " Nehmen Sie 500 mg. ",
            "segments": [{"id": 0, "text": " Nehmen Sie 500 mg.", "avg_logprob": -0.3,
                          "words": [{"word": "Nehmen", "probability": 0.9}, {"word": "Sie", "probability": 0.7}]}],
        })

    asr = WhisperCppASR("http://whisper:8081/", model="large-v3-turbo", client=client(handler))
    res = await asr.transcribe(np.zeros(16000, dtype=np.float32), lang_hint="de", partial=False, turn=TURN)
    assert seen["path"] == "/inference"
    assert seen["fields"]["response_format"] == "verbose_json"
    assert seen["fields"]["language"] == "de"
    assert seen["fields"]["file"] == "RIFF"
    assert res.text == "Nehmen Sie 500 mg." and res.lang == "de"
    assert res.conf == pytest.approx(0.8) and len(res.words) == 2
    assert asr.name == "whisper.cpp:large-v3-turbo"


async def test_whisper_cpp_confidence_fallbacks_and_errors():
    payloads = iter([
        {"text": "hi", "language": "en", "segments": [{"avg_logprob": -0.1}]},
        {"text": "hi", "segments": [{"tokens": [{"text": "[_BEG_]", "p": 0.1}, {"text": "hi", "p": 0.6}]}]},
        {"text": "hi"},
        {"error": "failed to read audio"},
    ])

    def handler(request):
        if b'name="language"\r\n\r\nauto' not in request.content:
            raise AssertionError("no hint -> language=auto")
        return httpx.Response(200, json=next(payloads))

    asr = WhisperCppASR("http://w:1", client=client(handler))
    audio = np.zeros(1600, dtype=np.float32)
    r = await asr.transcribe(audio, lang_hint="auto", partial=True, turn=TURN)
    assert r.conf == pytest.approx(math.exp(-0.1)) and r.lang == "en"
    r = await asr.transcribe(audio, lang_hint=None, partial=False, turn=TURN)
    assert r.conf == pytest.approx(0.6)  # special tokens skipped
    r = await asr.transcribe(audio, lang_hint=None, partial=False, turn=TURN)
    assert r.conf == pytest.approx(0.9)
    with pytest.raises(RuntimeError, match="failed to read audio"):
        await asr.transcribe(audio, lang_hint=None, partial=False, turn=TURN)
    empty = await asr.transcribe(np.zeros(0, dtype=np.float32), lang_hint="de", partial=False, turn=TURN)
    assert empty.text == "" and empty.conf == 0.0


def _req(text="Nehmen Sie 500 mg.", src="de", tgt="ar", context="(none)"):
    return TranslateRequest(text=text, src_lang=src, tgt_lang=tgt, room_name="Consultation room", role_a="Doctor",
                            role_b="Patient", lang_a="de", lang_b="ar", register="formal",
                            glossary="Metformin = ميتفورمين", context=context, turn=TURN)


async def test_ollama_translator_streams_chat():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        bodies.append(json.loads(request.content))
        lines = [{"message": {"role": "assistant", "content": "\n خذ"}, "done": False},
                 {"message": {"role": "assistant", "content": " 500 ملغ"}, "done": False},
                 {"message": {"role": "assistant", "content": "."}, "done": True}]
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode() + b"\n")

    tr = OllamaTranslator("http://ollama:11434", model="gemma4:26b",
                          options={"temperature": 0.1, "num_predict": 256, "first_token_ms": 99},
                          client=client(handler))
    deltas = [d async for d in tr.stream(_req())]
    assert deltas == ["خذ", " 500 ملغ", "."]
    body = bodies[0]
    assert body["model"] == "gemma4:26b" and body["stream"] is True and body["keep_alive"] == -1
    assert body["options"] == {"temperature": 0.1, "num_predict": 256}  # mock-only keys filtered out
    system, user = body["messages"]
    assert system["role"] == "system" and user == {"role": "user", "content": "Nehmen Sie 500 mg."}
    assert "Consultation room" in system["content"] and "from German into Arabic" in system["content"]
    assert "Register: formal." in system["content"] and "Metformin = ميتفورمين" in system["content"]
    assert "{" not in system["content"]

    [d async for d in tr.stream(_req(text="Zweiter Satz."))]
    assert bodies[1]["messages"][0] == system  # fixed system prefix per room/direction: cache reuse


async def test_ollama_translator_errors():
    def handler(request):
        return httpx.Response(404, json={"error": "model 'gemma4:26b' not found"})

    tr = OllamaTranslator("http://o:1", model="gemma4:26b", client=client(handler))
    with pytest.raises(RuntimeError, match="404"):
        [d async for d in tr.stream(_req())]

    def handler2(request):
        return httpx.Response(200, content=b'{"error": "out of memory"}\n')

    tr = OllamaTranslator("http://o:1", model="gemma4:26b", client=client(handler2))
    with pytest.raises(RuntimeError, match="out of memory"):
        [d async for d in tr.stream(_req())]


async def test_ollama_verifier_back_translation_and_json():
    bodies = []
    replies = iter(["  Nehmen Sie 500 mg.  ", '{"understood": "partly", "missing": ["12 Nov"], "wrong": []}',
                    'Sure: {"understood": "yes"} done', "Einfache Worte."])

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"role": "assistant", "content": next(replies)}, "done": True})

    v = OllamaVerifier("http://o:11434", model="gpt-oss:120b", options={"reasoning": "low", "num_predict": 128},
                       client=client(handler))
    assert await v.back_translate("خذ 500 ملغ.", "ar", "de", TURN) == "Nehmen Sie 500 mg."
    b = bodies[0]
    assert b["stream"] is False and b["think"] == "low" and b["keep_alive"] == -1
    assert b["options"]["num_predict"] == 128 and b["options"]["temperature"] == 0.0
    assert "Translate the following Arabic text into German" in b["messages"][0]["content"]
    assert b["messages"][1]["content"] == "خذ 500 ملغ."

    j = await v.complete_json("prompt", purpose="repeat_back")
    assert j == {"understood": "partly", "missing": ["12 Nov"], "wrong": []} and bodies[1]["format"] == "json"
    assert (await v.complete_json("prompt", purpose="repeat_back")) == {"understood": "yes"}
    assert await v.complete_text("prompt", purpose="explain_simply") == "Einfache Worte."
    assert "format" not in bodies[3]


def test_parse_json_object():
    assert parse_json_object('{"a": 1}') == {"a": 1}
    with pytest.raises(ValueError):
        parse_json_object("no json here")
    with pytest.raises(TypeError):
        parse_json_object("[1, 2]")


async def test_ollama_embedder():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/embed" and body["model"] == "bge-m3" and body["input"] == ["a", "b"]
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

    e = OllamaEmbedder("http://o:11434", model="bge-m3", client=client(handler))
    assert await e.embed(["a", "b"]) == [[0.1, 0.2], [0.3, 0.4]]

    def bad(request):
        return httpx.Response(200, json={"embeddings": [[0.1]]})

    with pytest.raises(RuntimeError):
        await OllamaEmbedder("http://o:1", model="bge-m3", client=client(bad)).embed(["a", "b"])


async def test_ollama_translator_think_false_and_fallback():
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        b = json.loads(request.content)
        bodies.append(b)
        if "think" in b:
            return httpx.Response(400, json={"error": '"gemma4:26b" does not support thinking'})
        line = {"message": {"role": "assistant", "content": "خذ 500 ملغ."}, "done": True}
        return httpx.Response(200, content=json.dumps(line).encode() + b"\n")

    tr = OllamaTranslator("http://o:1", model="gemma4:26b",
                          options={"temperature": 0.1, "think": False}, client=client(handler))
    assert [d async for d in tr.stream(_req())] == ["خذ 500 ملغ."]
    assert bodies[0]["think"] is False and "think" not in bodies[0]["options"]
    assert "think" not in bodies[1]  # retried without the field
    [d async for d in tr.stream(_req())]
    assert "think" not in bodies[2]  # remembered
