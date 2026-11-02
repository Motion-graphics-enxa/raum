"""The speaker's own voice (C-13): voice sample + pace into ``synthesize``, the ``http`` engine against
``httpx.MockTransport``, and the privacy rule: the voice sample never leaves memory except to the
local voice service."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import struct
import wave

import httpx
import numpy as np
import pytest
from core_doubles import FakePublisher, install_security, make_config
from pipeline_helpers import DOC, PAT, Harness, not_implemented

from raum.config import EngineConfig
from raum.events import Control, SessionState
from raum.pipeline.engines import build_engines
from raum.pipeline.tts import MockTTS, make_tts
from raum.pipeline.tts_http import HttpTTS, PcmStream, stream_rate, tts_request
from raum.pipeline.voice import SpeakingRate, VoiceRef, speech_seconds
from raum.verify import guard


@pytest.fixture(autouse=True)
def no_guard(monkeypatch):
    monkeypatch.setattr(guard, "check", not_implemented)
    monkeypatch.setattr(guard, "extract", not_implemented)


def tone(seconds: float, freq: float = 220.0, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * 16000)) / 16000.0
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


async def speak(h: Harness, speaker: str, audio: np.ndarray, vad_ms: float = 300.0) -> str | None:
    utt = await h.p.speech_start(speaker)
    for i in range(0, len(audio), 320):
        await h.p.speech_audio(speaker, audio[i:i + 320])
    await h.p.speech_end(speaker, audio, vad_ms)
    return utt


def wav_pcm(b: bytes) -> tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(b), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2"), w.getframerate()


class Cloner:
    """A voice engine that clones: records what it was asked."""

    name = "cloner"
    sample_rate = 16000
    uses_voice_ref = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bytes | None, float | None]] = []

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        self.calls.append((text, lang, voice_ref, rate))
        yield np.zeros(160, dtype=np.int16)


def voiced(engine) -> Harness:
    h = Harness()
    h.engines.tts = engine
    h.p.echo.tail_s = 0.01

    async def sink(lang, pcm, rate):
        pass

    h.p.tts_sink = sink
    return h


# ---------------------------------------------------------------- units


def test_voice_ref_caps_clears_and_hides_itself():
    ref = VoiceRef(max_s=1.0)
    ref.add(tone(0.6))
    ref.add(tone(0.6))
    assert ref.full and ref.seconds == pytest.approx(1.0)
    pcm, rate = wav_pcm(ref.wav())
    assert rate == 16000 and len(pcm) == 16000
    assert repr(ref) == "<VoiceRef 1.0 s>"  # never its content
    ref.clear()
    assert ref.seconds == 0 and not ref.full


def test_pace_from_word_timestamps_or_audio():
    words = [{"word": "a", "start": 0.5, "end": 0.9}, {"word": "b", "start": 1.0, "end": 2.5}]
    assert speech_seconds(4.0, 500, words) == pytest.approx(2.0)
    assert speech_seconds(4.0, 500, [], prefix_pad_s=0.5) == pytest.approx(3.0)
    r = SpeakingRate()
    assert r.update("ja", 0.4) is None  # too short to say anything
    assert r.update("eins zwei drei vier fünf", 2.0) == 1.0  # 2.5 w/s
    assert r.update("eins zwei drei vier fünf sechs sieben acht neun zehn", 2.0) == 1.5  # 5 w/s, smoothed, capped


async def test_mock_and_piper_accept_and_ignore_voice_args():
    chunks = [c async for c in MockTTS({"first_audio_ms": 0}).synthesize("Hallo", "de", voice_ref=b"RIFF", rate=1.2)]
    assert chunks == []


# ---------------------------------------------------------------- pipeline


async def test_voice_sample_per_speaker_grows_to_its_cap_and_goes_with_their_words():
    eng = Cloner()
    h = voiced(eng)
    h.p.voice_ref_s, h.p.voice_ref_min_s = 4.0, 2.5
    await speak(h, DOC.id, tone(2.0))  # 1.7 s of speech (300 ms endpoint silence): not enough yet
    await h.p.drain()
    assert eng.calls and all(c[2] is None for c in eng.calls)
    assert all(c[3] is not None for c in eng.calls)  # the pace goes with every clause

    eng.calls.clear()
    await speak(h, DOC.id, tone(2.0))  # 3.4 s: from now on the doctor's words sound like the doctor
    await h.p.drain()
    refs = {c[2] for c in eng.calls}
    assert len(refs) == 1 and None not in refs
    pcm, rate = wav_pcm(refs.pop())
    assert rate == 16000 and len(pcm) == int(3.4 * 16000)
    expected = (np.clip(tone(2.0)[:27200], -1, 1) * 32767).astype(np.int16)
    assert np.array_equal(pcm[:27200], expected)  # the doctor's own speech, trailing silence cut

    for _ in range(3):
        await speak(h, DOC.id, tone(2.0))
    await h.p.drain()
    assert h.p._voice_refs[DOC.id].seconds == pytest.approx(4.0)  # capped
    assert h.p.voice_ref(PAT.id) is None  # nobody else's voice

    eng.calls.clear()
    await speak(h, PAT.id, tone(3.0, freq=300))  # the patient's words: the patient's sample
    await h.p.drain()
    ref = eng.calls[0][2]
    assert ref is not None and ref == h.p.voice_ref(PAT.id) and ref != h.p.voice_ref(DOC.id)


async def test_voice_sample_dropped_on_burn_and_close_and_skips_echoed_speech():
    eng = Cloner()
    h = voiced(eng)
    await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    held = h.p._voice_refs[DOC.id]
    assert h.p.voice_ref(DOC.id) is not None
    await h.p.handle_control(Control(action="burn"))
    assert h.p.voice_ref(DOC.id) is None and h.p._voice_refs == {} and held.seconds == 0
    assert h.p._rates == {}

    # an utterance a voice played over is not a clean sample
    utt = await h.p.speech_start(PAT.id)
    h.p.echo.play("ar", 5.0, h.p.participants.values())  # the Arabic voice now plays on the patient's phone
    audio = tone(4.0)
    for i in range(0, len(audio), 320):
        await h.p.speech_audio(PAT.id, audio[i:i + 320])
    await h.p.speech_end(PAT.id, audio, 300)
    assert h.one("transcript.final", utt)["text"] and h.p.voice_ref(PAT.id) is None
    h.p.echo.clear()

    def state(status: str, sid: str) -> SessionState:
        return SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer", status=status,
                            session_id=sid)

    h.p.set_state(state("live", "s-1"))
    await speak(h, DOC.id, tone(4.0))
    assert h.p.voice_ref(DOC.id) is not None
    h.p.set_state(state("live", "s-2"))  # a new session in the same room: new people, no old voices
    assert h.p._voice_refs == {}
    await speak(h, DOC.id, tone(4.0))
    h.p.set_state(state("ended", "s-2"))  # session end
    assert h.p._voice_refs == {}
    await speak(h, DOC.id, tone(4.0))
    assert h.p.voice_ref(DOC.id) is not None
    await h.p.aclose()
    assert h.p._voice_refs == {}


async def test_no_voice_sample_for_engines_that_do_not_clone():
    h = voiced(MockTTS({"first_audio_ms": 0}))
    await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    assert h.p._voice_refs == {} and h.p.voice_ref(DOC.id) is None


async def test_old_two_argument_engines_still_work():
    class Old:
        name, sample_rate = "old", 16000

        def __init__(self):
            self.n = 0

        async def synthesize(self, text, lang):
            self.n += 1
            yield np.zeros(160, dtype=np.int16)

    old = Old()
    h = voiced(old)
    await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    assert old.n > 0 and not h.p._voice_paused("ar")


# ---------------------------------------------------------------- the http engine


def pcm_bytes(n: int, value: int = 1000) -> bytes:
    return struct.pack(f"<{n}h", *([value] * n))


class Service:
    """A MockTransport stand-in for the voice service."""

    def __init__(self, status: int = 200, rate: int | None = 24000, wav: bool = False, fail_after: int | None = None,
                 down: bool = False) -> None:
        self.status, self.rate, self.wav, self.fail_after, self.down = status, rate, wav, fail_after, down
        self.requests: list[httpx.Request] = []

    def body(self, req: httpx.Request) -> dict:
        if req.headers.get("content-type", "").startswith("application/json"):
            return json.loads(req.content)
        return {"multipart": req.content}

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        await req.aread()
        self.requests.append(req)
        if self.down:
            raise httpx.ConnectError("refused", request=req)
        if self.status >= 400:
            return httpx.Response(self.status, text='{"echo": "would contain the request"}')
        n = 160 * len(self.body(req).get("text", "x y").split()) if "text" in self.body(req) else 320
        data = pcm_bytes(n)
        if self.wav:
            buf = io.BytesIO()
            with wave.open(buf, "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(self.rate or 24000)
                w.writeframes(data)
            data = buf.getvalue()
        fail_after = self.fail_after

        async def gen():
            for i in range(0, len(data), 333):  # odd-sized pieces
                if fail_after is not None and i >= fail_after:
                    raise httpx.ReadError("connection reset")
                yield data[i:i + 333]

        headers = {"x-sample-rate": str(self.rate)} if self.rate and not self.wav else {}
        return httpx.Response(200, headers=headers, content=gen())


def http_tts(service: Service, *, fallback=None, **options) -> HttpTTS:
    client = httpx.AsyncClient(transport=httpx.MockTransport(service))
    return HttpTTS("http://tts.test", model="cloner-1", options={"sample_rate": 24000, **options},
                   fallback=fallback, client=client)


async def collect(tts, text="eins zwei drei", lang="de", **kw) -> np.ndarray:
    chunks = [c async for c in tts.synthesize(text, lang, **kw)]
    assert all(c.dtype == np.int16 for c in chunks)
    return np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)


class Fallback:
    name, sample_rate = "fallback", 24000

    def __init__(self):
        self.calls = 0

    async def synthesize(self, text, lang, *, voice_ref=None, rate=None):
        self.calls += 1
        yield np.full(100, 7, dtype=np.int16)


def test_request_mapping():
    ref = b"RIFF....WAVEfmt voice"
    req = tts_request("Guten Tag.", "de", voice_ref=ref, rate=1.234, model="m", options={"sample_rate": 24000})
    body = req["json"]
    assert {k: body[k] for k in ("text", "lang", "rate", "model", "sample_rate")} == {
        "text": "Guten Tag.", "lang": "de", "rate": 1.23, "model": "m", "sample_rate": 24000}
    assert base64.b64decode(body["voice_ref"]) == ref and len(body["voice_id"]) == 16
    assert "voice_ref" not in tts_request("x", "de", voice_ref=None, rate=None, model=None, options={})["json"]
    mp = tts_request("x", "de", voice_ref=ref, rate=None, model=None, options={"voice_ref_as": "multipart"})
    assert mp["files"]["voice_ref"][1] == ref and mp["data"]["text"] == "x"
    assert stream_rate(httpx.Headers({"x-sample-rate": "22050"})) == 22050
    assert stream_rate(httpx.Headers({"content-type": "audio/L16; rate=16000; channels=1"})) == 16000
    assert stream_rate(httpx.Headers({})) is None


def test_pcm_stream_carries_odd_bytes_and_parses_wav():
    s = PcmStream(16000, 16000)
    data = pcm_bytes(1000)
    out = [c for i in range(0, len(data), 333) for c in s.feed(data[i:i + 333])] + s.flush()
    assert sum(len(c) for c in out) == 1000 and all(len(c) >= 320 for c in out[:-1])
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(data)
    s = PcmStream(None, 24000)
    raw = buf.getvalue()
    out = [c for i in range(0, len(raw), 7) for c in s.feed(raw[i:i + 7])] + s.flush()
    assert s.rate == 16000 and abs(sum(len(c) for c in out) - 1500) <= 2  # header skipped, resampled


async def test_http_streams_pcm_with_voice_ref_and_rate():
    svc = Service()
    tts = http_tts(svc)
    ref = b"RIFF" + b"\x01" * 100
    pcm = await collect(tts, voice_ref=ref, rate=1.3)
    assert len(pcm) == 480 and set(pcm.tolist()) == {1000}
    body = svc.body(svc.requests[0])
    assert body["text"] == "eins zwei drei" and body["lang"] == "de" and body["rate"] == 1.3
    assert base64.b64decode(body["voice_ref"]) == ref
    assert str(svc.requests[0].url) == "http://tts.test/tts"
    await tts.aclose()


async def test_http_resamples_a_wav_stream_and_multipart():
    svc = Service(rate=16000, wav=True)
    tts = http_tts(svc, voice_ref_as="multipart")
    pcm = await collect(tts, voice_ref=b"voice-bytes")
    assert abs(len(pcm) - 480) <= 2  # 320 samples at 16 kHz (multipart: the stub's default) -> 24 kHz
    assert b'name="voice_ref"' in svc.body(svc.requests[0])["multipart"]


async def test_http_failure_falls_back_then_retries_the_service(monkeypatch):
    svc = Service(status=503)
    fb = Fallback()
    tts = http_tts(svc, fallback=fb, retry_s=30)
    assert (await collect(tts)).tolist() == [7] * 100  # the fallback voice says the clause
    await collect(tts)
    assert len(svc.requests) == 1 and fb.calls == 2  # the service is skipped for retry_s
    tts._down_until = 0.0  # retry_s has passed
    svc.status = 200
    assert len(await collect(tts)) == 480 and len(svc.requests) == 2 and fb.calls == 2

    down = http_tts(Service(down=True), fallback=Fallback())
    assert (await collect(down)).tolist() == [7] * 100  # connection refused: same


async def test_http_mid_stream_failure_does_not_repeat_the_clause():
    fb = Fallback()
    tts = http_tts(Service(fail_after=999), fallback=fb)  # 499 samples arrive, then the connection drops
    pcm = await collect(tts, text=" ".join(["w"] * 10))
    assert len(pcm) == 499 and fb.calls == 0  # what was said stays said; no second voice repeats it

    fb = Fallback()
    tts = http_tts(Service(fail_after=333), fallback=fb)  # dropped before 20 ms of audio: nothing was said
    assert (await collect(tts)).tolist() == [7] * 100 and fb.calls == 1


async def test_http_gate_uses_the_fallback_until_warm():
    svc = Service()
    fb = Fallback()
    tts = http_tts(svc, fallback=fb)
    tts.gate = True
    await collect(tts)
    assert svc.requests == [] and fb.calls == 1  # cold service: not asked mid-load
    await tts.warmup()
    assert tts.ready and svc.requests[0].extensions["timeout"]["read"] == 300.0  # a long-timeout warm-up
    assert len(await collect(tts)) == 480 and fb.calls == 1


async def test_http_without_fallback_goes_text_only():
    h = voiced(http_tts(Service(status=500)))
    utt = await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    assert h.p._voice_fails.get("ar") or h.p._voice_paused("ar")  # clauses dropped, not the session
    assert h.one("translation.final", utt)["text"]  # the text continues


def test_make_tts_http_and_build_engines(monkeypatch):
    tts = make_tts("http", {"sample_rate": 22050}, "m", url="http://x:1", fallback={"engine": "mock"})
    assert isinstance(tts, HttpTTS) and tts.url == "http://x:1" and tts.name == "http:m"
    assert isinstance(tts.fallback, MockTTS) and tts.fallback.sample_rate == 22050  # same output rate
    assert make_tts("http", {}, fallback="none").fallback is None
    monkeypatch.setenv("PATH", "/nonexistent")
    assert make_tts("http", {"voices_dir": "/nonexistent"}).fallback is None  # no piper: text only on failure

    cfg = make_config_fast()
    cfg.models.tts = EngineConfig(engine="http", url="http://127.0.0.1:8090", model="chatterbox",
                                  options={"sample_rate": 24000}, fallback="none")
    engines = build_engines(cfg)
    assert isinstance(engines.tts, HttpTTS) and engines.tts.fallback is None and engines.tts.sample_rate == 24000
    assert engines.names["tts"] == "http:chatterbox"


def make_config_fast():
    from pipeline_helpers import fast_config

    return fast_config()


# ---------------------------------------------------------------- privacy


async def test_voice_ref_never_reaches_events_logs_store_or_audit(tmp_path, monkeypatch, caplog):
    from raum.sessions import SessionManager

    caplog.set_level(logging.DEBUG)
    install_security(monkeypatch, tmp_path)
    sm = SessionManager(make_config(tmp_path), publisher=FakePublisher())
    room = sm.room("sz-1000")
    sm.ensure_session(room)

    svc = Service(status=200)
    h = voiced(http_tts(svc, fallback=Fallback()))
    emit = h.p._emit

    async def emit_and_ingest(env):
        await emit(env)
        await sm.ingest("sz-1000", env)

    h.p._emit = emit_and_ingest
    await speak(h, DOC.id, tone(5.0, freq=233.0))
    await speak(h, PAT.id, tone(5.0, freq=311.0))
    svc.status = 500  # a failing call: its log line must not carry the voice either
    await speak(h, DOC.id, tone(3.0))
    await h.p.drain()

    sent = [svc.body(r) for r in svc.requests if "voice_ref" in svc.body(r)]
    assert sent, "the voice service got the speaker's voice"
    refs = {base64.b64decode(b["voice_ref"]) for b in sent}
    needles: set[str] = set()
    for ref in refs:
        b64 = base64.b64encode(ref).decode()
        pcm = ref[44:]
        needles |= {b64[60:100], base64.b64encode(pcm[1000:1060]).decode(), pcm[1000:1016].hex(),
                    repr(pcm[1000:1016])[2:-1]}
    needles |= {b["voice_id"] for b in sent}

    events = json.dumps([e.model_dump(mode="json") for e in h.events])
    assert h.events and "voice_ref" not in events
    store = json.dumps(room.session.store.read_all())
    assert "transcript.final" in store  # the store did get the conversation
    audit = sm.cfg.audit_path().read_text(encoding="utf-8") if sm.cfg.audit_path().exists() else ""
    logs = caplog.text + "\n".join(str(r.args) for r in caplog.records)
    assert "tts service" in logs  # the failure was logged ...
    for hay, where in ((events, "events"), (store, "store"), (audit, "audit"), (logs, "logs")):
        for n in needles:
            assert n not in hay, f"voice sample leaked into the {where}"
    await asyncio.sleep(0)


# ---------------------------------------------------------------- the service forgets the cloned voice


class ForgetfulService(Service):
    """The voice service, plus DELETE /tts/voices/{id}: records which ids it was told to drop."""

    def __init__(self, delete_status: int = 204, **kw) -> None:
        super().__init__(**kw)
        self.deleted: list[str] = []
        self.delete_status = delete_status

    async def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            assert req.url.path.startswith("/tts/voices/")
            self.deleted.append(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(self.delete_status)
        return await super().__call__(req)

    def sent_ids(self) -> set[str]:
        return {self.body(r)["voice_id"] for r in self.requests if r.method == "POST" and "voice_id" in self.body(r)}


def on_headphones(h: Harness) -> Harness:
    """Both devices on headphones: nobody's speech is echo, so both voices are sampled (C-14)."""
    h.p.set_participants([p.model_copy(update={"audio_out": "headphones"}) for p in (DOC, PAT)])
    return h


async def test_burn_deletes_every_cloned_voice_in_the_service():
    svc = ForgetfulService()
    h = on_headphones(voiced(http_tts(svc)))
    await speak(h, DOC.id, tone(3.0))
    await h.p.drain()
    await speak(h, DOC.id, tone(3.0, freq=260.0))  # the sample grew: a second id for the same speaker
    await speak(h, PAT.id, tone(4.0, freq=311.0))
    await h.p.drain()
    sent = svc.sent_ids()
    assert len(sent) >= 2, "the service got the speakers' voices"
    await h.p.handle_control(Control(action="burn"))
    await asyncio.sleep(0.05)
    assert set(svc.deleted) == sent  # every id the service ever got, nothing else
    assert all(not ref.sent_ids for ref in h.p._voice_refs.values())


async def test_session_end_and_close_delete_the_voices_too():
    svc = ForgetfulService()
    h = voiced(http_tts(svc))
    await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    h.p.set_state(SessionState(off_record=False, sealed=False, participants=[], mode="sprechzimmer",
                               status="ended", session_id="s-1"))
    await asyncio.sleep(0.05)
    assert set(svc.deleted) == svc.sent_ids() and svc.deleted
    svc.deleted.clear()
    await speak(h, PAT.id, tone(4.0))
    await h.p.drain()
    await h.p.aclose()  # the agent leaves: it waits for the DELETEs
    assert set(svc.deleted) == svc.sent_ids() - set() and svc.deleted


async def test_a_device_gone_for_good_has_only_its_voice_deleted():
    svc = ForgetfulService()
    h = on_headphones(voiced(http_tts(svc)))
    await speak(h, DOC.id, tone(4.0))
    await speak(h, PAT.id, tone(4.0, freq=311.0))
    await h.p.drain()
    pat_ids = set(h.p._voice_refs[PAT.id].sent_ids)
    assert pat_ids and pat_ids < svc.sent_ids()
    h.p.forget_speaker(PAT.id)
    await asyncio.sleep(0.05)
    assert set(svc.deleted) == pat_ids and PAT.id not in h.p._voice_refs and DOC.id in h.p._voice_refs


async def test_a_failing_delete_never_blocks_the_burn(caplog):
    svc = ForgetfulService(delete_status=500)
    h = voiced(http_tts(svc))
    await speak(h, DOC.id, tone(4.0))
    await h.p.drain()
    with caplog.at_level(logging.WARNING, logger="raum.pipeline.tts_http"):
        await h.p.handle_control(Control(action="burn"))
        await asyncio.sleep(0.05)
    assert h.p._voice_refs == {} and svc.deleted
    assert all("RIFF" not in r.message and "voice_ref" not in r.message for r in caplog.records)
