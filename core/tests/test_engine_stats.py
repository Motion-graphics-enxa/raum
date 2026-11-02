"""Engine clients note where a call's time went (pipeline/stats.py): wall vs Ollama's own timings, ASR, voice.

Real engine clients against ``httpx.MockTransport``: no server, no GPU. With no Stages set every note is a no-op."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import numpy as np
import pytest

from raum.pipeline import stats
from raum.pipeline.asr import WhisperCppASR
from raum.pipeline.base import TranslateRequest, TurnContext
from raum.pipeline.embed import OllamaEmbedder
from raum.pipeline.translate import OllamaTranslator
from raum.pipeline.tts_http import HttpTTS, VoiceBusy
from raum.pipeline.verifier import OllamaVerifier

TURN = TurnContext(room="sz-1", mode="sprechzimmer", speaker="a-1", role="a", utt="u-0001")
MS = 1_000_000  # ns per ms
TIMINGS = {"total_duration": 9000 * MS, "load_duration": 1200 * MS, "prompt_eval_count": 312,
           "prompt_eval_duration": 94 * MS, "eval_count": 60, "eval_duration": 2000 * MS}


def client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def req(text="Nehmen Sie 500 mg."):
    return TranslateRequest(text=text, src_lang="de", tgt_lang="ar", room_name="Consultation room", role_a="Doctor",
                            role_b="Patient", lang_a="de", lang_b="ar", register="formal", glossary="",
                            context="(none)", turn=TURN)


def recording() -> stats.Stages:
    s = stats.Stages()
    stats.use(s)  # inside the test's own task: does not leak into other tests
    return s


def chat_lines(final: dict | None = TIMINGS) -> bytes:
    lines = [{"message": {"content": "\n خذ"}, "done": False}, {"message": {"content": " 500 ملغ"}, "done": False}]
    if final is not None:
        lines.append({"message": {"content": "."}, "done": True, "done_reason": "stop", **final})
    return "\n".join(json.dumps(x) for x in lines).encode() + b"\n"


async def test_translator_notes_server_timings_ns_to_ms(caplog):
    s = recording()
    tr = OllamaTranslator("http://ollama", model="gemma4:26b",
                          client=client(lambda r: httpx.Response(200, content=chat_lines())))
    with caplog.at_level(logging.INFO, logger="raum.pipeline.ollama"):
        assert [d async for d in tr.stream(req())] == ["خذ", " 500 ملغ", "."]
    mt = s.snapshot()["mt"]
    assert mt["n"] == 1
    assert mt["load_ms"] == 1200.0 and mt["prompt_ms"] == 94.0 and mt["eval_ms"] == 2000.0
    assert mt["server_ms"] == 9000.0 and mt["prompt_n"] == 312 and mt["eval_n"] == 60
    assert 0 <= mt["ttft_ms"] <= mt["wall_ms"]
    assert mt["queue_ms"] == 0.0  # wall − server, floored: the fake answers faster than its own server_ms
    assert "cancelled" not in mt and "error" not in mt
    assert "ollama gemma4:26b reloaded: load 1200 ms" in caplog.text


async def test_translator_queue_is_wall_minus_server_and_no_reload_log(caplog):
    s = recording()
    fast = {**TIMINGS, "total_duration": 1000, "load_duration": 400 * MS}  # 0.001 ms inside Ollama
    tr = OllamaTranslator("http://ollama", model="gemma4:26b",
                          client=client(lambda r: httpx.Response(200, content=chat_lines(fast))))
    with caplog.at_level(logging.INFO, logger="raum.pipeline.ollama"):
        [d async for d in tr.stream(req())]
    mt = s.snapshot()["mt"]
    assert mt["queue_ms"] == pytest.approx(mt["wall_ms"], abs=0.11)
    assert mt["load_ms"] == 400.0 and "reloaded" not in caplog.text  # under 500 ms: no line


async def test_translator_closed_early_still_notes_wall():
    s = recording()
    tr = OllamaTranslator("http://ollama", model="gemma4:26b",
                          client=client(lambda r: httpx.Response(200, content=chat_lines())))
    gen = tr.stream(req())
    assert await gen.__anext__() == "خذ"
    await gen.aclose()  # the consumer stopped: no final chunk read
    mt = s.snapshot()["mt"]
    assert mt["n"] == 1 and mt["cancelled"] == 1 and mt["wall_ms"] >= 0 and "ttft_ms" in mt
    assert "server_ms" not in mt and "queue_ms" not in mt


async def test_translator_task_cancelled_mid_stream_still_notes_wall():
    s = recording()

    async def body():
        yield chat_lines(None)  # two deltas, then Ollama goes quiet
        await asyncio.sleep(30)

    tr = OllamaTranslator("http://ollama", model="gemma4:26b",
                          client=client(lambda r: httpx.Response(200, content=body())))
    got = asyncio.Event()

    async def consume():
        async for _ in tr.stream(req()):
            got.set()

    task = asyncio.create_task(consume())
    await asyncio.wait_for(got.wait(), 1.0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    mt = s.snapshot()["mt"]
    assert mt["cancelled"] == 1 and mt["wall_ms"] > 0 and mt["ttft_ms"] <= mt["wall_ms"]


async def test_translator_error_and_think_retry_note_once_per_call():
    s = recording()
    calls = []

    def handler(r):
        calls.append(json.loads(r.content))
        if "think" in calls[-1]:
            return httpx.Response(400, json={"error": "think value not supported"})
        return httpx.Response(200, content=chat_lines())

    tr = OllamaTranslator("http://ollama", model="m", options={"think": False}, client=client(handler))
    [d async for d in tr.stream(req())]
    assert len(calls) == 2 and s.snapshot()["mt"]["n"] == 1  # the retry notes, the refused first try does not

    bad = OllamaTranslator("http://ollama", model="m", client=client(lambda r: httpx.Response(500, text="boom")))
    with pytest.raises(RuntimeError):
        [d async for d in bad.stream(req())]
    mt = s.snapshot()["mt"]
    assert mt["n"] == 2 and mt["error"] == 1 and "cancelled" not in mt


async def test_verifier_notes_check_stages():
    s = recording()

    def handler(r):
        body = json.loads(r.content)
        content = '{"understood": "yes"}' if body.get("format") == "json" else "Take 500 mg."
        return httpx.Response(200, json={"message": {"content": content}, "done": True, "done_reason": "stop",
                                         **TIMINGS})

    v = OllamaVerifier("http://ollama", model="gpt-oss:120b", client=client(handler))
    assert await v.back_translate("خذ 500 ملغ.", "ar", "de") == "Take 500 mg."
    assert (await v.complete_json("x", purpose="repeat_back"))["understood"] == "yes"
    snap = s.snapshot()
    for stage in ("check", "check.repeat_back"):
        c = snap[stage]
        assert c["n"] == 1 and c["load_ms"] == 1200.0 and c["prompt_ms"] == 94.0 and c["eval_ms"] == 2000.0
        assert c["server_ms"] == 9000.0 and c["prompt_n"] == 312 and c["eval_n"] == 60
        assert c["wall_ms"] >= 0 and c["queue_ms"] == 0.0

    broken = OllamaVerifier("http://ollama", model="gpt-oss:120b",
                            client=client(lambda r: httpx.Response(500, text="no")))
    with pytest.raises(RuntimeError):
        await broken.complete_text("x", purpose="explain_simply")
    e = s.snapshot()["check.explain_simply"]
    assert e["error"] == 1 and "wall_ms" in e and "server_ms" not in e


async def test_embed_notes_wall_and_server_when_present():
    s = recording()
    with_t = {"embeddings": [[0.1, 0.2]], "total_duration": 30 * MS, "load_duration": 2 * MS}
    e = OllamaEmbedder("http://ollama", model="bge-m3", client=client(lambda r: httpx.Response(200, json=with_t)))
    await e.embed(["a"])
    snap = s.snapshot()["embed"]
    assert snap["n"] == 1 and snap["load_ms"] == 2.0 and snap["server_ms"] == 30.0 and snap["wall_ms"] >= 0

    s2 = recording()
    bare = OllamaEmbedder("http://ollama", model="bge-m3",
                          client=client(lambda r: httpx.Response(200, json={"embeddings": [[0.1]]})))
    await bare.embed(["a"])
    assert set(s2.snapshot()["embed"]) == {"n", "wall_ms"}


async def test_whisper_notes_asr_and_partial_with_audio_length():
    s = recording()
    j = {"language": "de", "text": " Ja.", "segments": [{"text": " Ja.", "avg_logprob": -0.1}]}
    asr = WhisperCppASR("http://whisper", client=client(lambda r: httpx.Response(200, json=j)))
    one_s = np.zeros(16000, dtype=np.float32)
    await asr.transcribe(one_s, lang_hint="de", partial=True, turn=TURN)
    await asr.transcribe(one_s[:8000], lang_hint="de", partial=False, turn=TURN)
    snap = s.snapshot()
    assert snap["asr.partial"]["n"] == 1 and snap["asr.partial"]["audio_ms"] == 1000.0
    assert snap["asr"]["n"] == 1 and snap["asr"]["audio_ms"] == 500.0 and snap["asr"]["wall_ms"] >= 0
    assert "error" not in snap["asr"]


AUDIO = b"\x10\x00" * 4800  # 0.2 s of int16 at 24 kHz


def tts_service(status=200, queue_ms: str | None = "37"):
    async def handler(r: httpx.Request) -> httpx.Response:
        await r.aread()
        if status >= 400:
            return httpx.Response(status, json={"error": "tts busy"})

        async def gen():
            for i in range(0, len(AUDIO), 960):
                yield AUDIO[i:i + 960]

        headers = {"x-sample-rate": "24000", "x-priority": json.loads(r.content).get("priority", "live")}
        if queue_ms is not None:
            headers["x-queue-ms"] = queue_ms
        return httpx.Response(200, headers=headers, content=gen())

    return HttpTTS("http://tts", options={"sample_rate": 24000}, client=client(handler))


async def test_http_tts_notes_ttfb_wall_audio_and_service_queue():
    s = recording()
    out = [c async for c in tts_service().synthesize("eins zwei", "de")]
    assert sum(map(len, out)) == 4800
    t = s.snapshot()["tts"]
    assert t["n"] == 1 and t["audio_ms"] == 200.0 and t["queue_ms"] == 37.0
    assert 0 <= t["ttfb_ms"] <= t["wall_ms"]
    assert not {"refused", "cancelled", "error", "cut"} & set(t)

    [c async for c in tts_service(queue_ms=None).synthesize("eins", "de", background=True)]
    bg = s.snapshot()["tts.bg"]
    assert bg["n"] == 1 and bg["audio_ms"] == 200.0 and "queue_ms" not in bg  # an older service: no header


async def test_http_tts_refusal_notes_refused_only_and_stop_notes_cancelled():
    s = recording()
    with pytest.raises(VoiceBusy):
        [c async for c in tts_service(status=409).synthesize("eins", "de", background=True)]
    assert s.snapshot()["tts.bg"] == {"n": 1, "refused": 1}

    gen = tts_service().synthesize("eins zwei", "de")
    await gen.__anext__()
    await gen.aclose()  # the listener barged in
    t = s.snapshot()["tts"]
    assert t["cancelled"] == 1 and 0 < t["audio_ms"] < 200.0 and "ttfb_ms" in t


async def test_eot_text_judge_notes():
    from raum.pipeline.turn import LlmJudge

    s = recording()
    judge = LlmJudge("http://ollama", "gemma4:26b", {})
    await judge.aclose()
    judge._client = client(lambda r: httpx.Response(200, json={"message": {"content": "yes"}, "done": True,
                                                                **TIMINGS}))
    assert await judge.finished("Nehmen Sie 500 mg.", "de") is True
    e = s.snapshot()["eot.text"]
    assert e["n"] == 1 and e["load_ms"] == 1200.0 and e["eval_n"] == 60 and "error" not in e


async def test_no_stages_set_nothing_breaks():
    stats.use(None)
    tr = OllamaTranslator("http://ollama", model="m",
                          client=client(lambda r: httpx.Response(200, content=chat_lines())))
    assert [d async for d in tr.stream(req())] == ["خذ", " 500 ملغ", "."]
    gen = tr.stream(req())
    await gen.__anext__()
    await gen.aclose()
    assert [c async for c in tts_service().synthesize("eins", "de")]
    with pytest.raises(VoiceBusy):
        [c async for c in tts_service(status=409).synthesize("eins", "de", background=True)]
    asr = WhisperCppASR("http://whisper", client=client(lambda r: httpx.Response(200, json={"text": "Ja."})))
    assert (await asr.transcribe(np.zeros(1600, dtype=np.float32), lang_hint="de", partial=False,
                                 turn=TURN)).text == "Ja."
    assert stats.current() is None
