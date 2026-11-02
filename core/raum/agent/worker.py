"""raum-agent: the LiveKit Agents worker (plan §6.1). One agent participant per room.

    raum agent [start|dev] [--agent-name NAME] [--executor process|thread] [--idle N] [--log-level L]

Auto-dispatch to every room whose id encodes a mode (``sz-4821``, ``kz-1170``, ``vr-3305``); other
rooms are rejected. LiveKit URL / key / secret from ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
``LIVEKIT_API_SECRET``, else ``raum.yaml`` ``network``.

Per room: subscribe to every remote participant's mic (agents skipped). Participant metadata is
JSON ``{"role", "lang", "device", "path", "audio_out"}`` (attributes carry the same keys). Core changes a
device's language / voice output live (``POST /api/rooms/{room}/participant`` updates both): the
change events re-read it, the pipeline uses it from the next utterance, and ``participant.joined`` is
sent again with the new info. Per track: ``rtc.AudioStream`` (16 kHz mono; or 48 kHz -> soxr to
16 kHz when DeepFilterNet is on) -> Silero VAD stream; START_OF_SPEECH / INFERENCE_DONE /
END_OF_SPEECH drive ``RoomPipeline.speech_start / speech_audio / speech_end`` (the end-of-speech frames
are the final audio; ``vad_ms`` is the endpoint delay). With DeepFilterNet the 48 kHz audio is also
cleaned beside the VAD path (``raum.pipeline.clean``: worker thread, 200 ms blocks, realtime gate) and
the final audio's samples are swapped for their clean versions; VAD and partials never wait for it. Every event is
published on the data channel (topic ``raum.events``, reliable) and forwarded to core over the link.
Voice output is published as one track per language, ``tts-<lang>``. Echo control is per device
(C-14, ``raum.pipeline.echo``): while a voice plays (+300 ms) only the mics that hear it are fed silence
(``RoomPipeline.mic_open``); an utterance already under way runs to its end. Resident Piper voices
start at room join for the room's languages (``PiperTTS.prepare``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from typing import Any

import numpy as np

from raum.config import RaumConfig, get_config
from raum.events import EVENTS_TOPIC, Envelope, ParticipantInfo, make_event
from raum.modes import ModeError, mode_for_room
from raum.pipeline._util import float_to_int16, int16_to_float, lang_code
from raum.pipeline.clean import cleaner_for
from raum.pipeline.denoise import make_denoiser
from raum.pipeline.engines import build_engines
from raum.pipeline.room import RoomPipeline

log = logging.getLogger("raum.agent")

AGENT_IDENTITY = "raum-agent"
AGENT_NAME = "RAUM"
AUDIO_OUTS = ("headphones", "speaker", "room")
# C-12: a phone that left (Wi-Fi gone, page reloaded) stays a translation target this long, so the
# turns spoken while it was away are translated into its language and reach it through the backlog.
REJOIN_GRACE_S = float(os.environ.get("RAUM_REJOIN_GRACE_S", "120"))
VAD_OPTIONS = {"min_speech_duration", "min_silence_duration", "prefix_padding_duration", "max_buffered_speech",
               "activation_threshold", "deactivation_threshold", "force_cpu"}

_vad_cache: Any = None


def load_vad(cfg: RaumConfig) -> Any:
    """Silero VAD, loaded once per process (``options`` from ``models.vad``)."""
    global _vad_cache
    if _vad_cache is None:
        from livekit.plugins import silero

        opts = {k: v for k, v in (cfg.models.vad.options or {}).items() if k in VAD_OPTIONS}
        _vad_cache = silero.VAD.load(sample_rate=16000, **opts)
        log.info("silero vad loaded (%s)", opts)
    return _vad_cache


def prewarm(proc: Any) -> None:
    import websockets.asyncio.client  # noqa: F401  (imported here, not on the job's event loop)

    import raum.agent.link_client  # noqa: F401

    cfg = get_config()
    proc.userdata["vad"] = load_vad(cfg)
    warm_denoiser(cfg)


def warm_denoiser(cfg: RaumConfig) -> None:
    """Load DeepFilterNet once per process and run two silent blocks, before any room joins."""
    m = cfg.models.denoise
    if not m.enabled:
        return
    cleaner = cleaner_for(make_denoiser(m.engine, m.options), m.options)
    if cleaner is None:
        return
    t = time.perf_counter()
    try:
        cleaner.warm()
    except Exception as e:  # noqa: BLE001
        cleaner.gate.trip(f"warm-up failed ({e.__class__.__name__})")
        return
    log.info("noise cleaning ready (%.0f ms warm-up)", (time.perf_counter() - t) * 1000.0)


def livekit_credentials(cfg: RaumConfig) -> tuple[str, str, str]:
    url = os.environ.get("LIVEKIT_URL") or cfg.network.livekit_internal_url
    if url == "auto" or not url:
        url = "ws://127.0.0.1:7880"
    key = os.environ.get("LIVEKIT_API_KEY") or cfg.network.livekit_api_key
    secret = os.environ.get("LIVEKIT_API_SECRET") or cfg.network.livekit_api_secret
    return url, key, secret


def is_agent(p: Any) -> bool:
    from livekit import rtc

    return p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT or p.identity.startswith(("agent-", AGENT_IDENTITY))


def participant_info(p: Any, *, prefer: str = "metadata") -> ParticipantInfo:
    """Metadata JSON ``{"role","lang","device","path","audio_out"}`` and attributes (same keys, strings);
    identity ``b-k3x9`` as the role fallback. Core sets both to the same JSON; ``prefer`` names the one
    that just changed, so whichever change event arrives first already reads the new values."""
    meta: dict[str, Any] = {}
    if p.metadata:
        try:
            v = json.loads(p.metadata)
            meta = v if isinstance(v, dict) else {}
        except ValueError:
            meta = {}
    attrs = dict(p.attributes or {})
    first, second = (attrs, meta) if prefer == "attributes" else (meta, attrs)

    def get(key: str, default: str) -> str:
        return str(first.get(key) or second.get(key) or default)

    head = p.identity.split("-", 1)[0]
    role = get("role", head if head in ("a", "b") else "b")
    if role not in ("a", "b", "observer"):
        role = "b"
    path = get("path", "direct")
    if path not in ("direct", "tunnel", "relayed"):
        path = "direct"
    audio_out = get("audio_out", "speaker")
    if audio_out not in AUDIO_OUTS:
        audio_out = "speaker"
    return ParticipantInfo(id=p.identity, role=role, lang=get("lang", "auto"), device=get("device", "phone"),
                           path=path, audio_out=audio_out)


class RoomAgent:
    def __init__(self, ctx: Any, cfg: RaumConfig, mode: Any, vad: Any) -> None:
        from raum.agent.link_client import LinkClient

        self.ctx = ctx
        self.cfg = cfg
        self.mode = mode
        self.vad = vad
        self.room_name: str = ctx.job.room.name
        self.engines = build_engines(cfg)
        self.pipeline = RoomPipeline(self.room_name, mode, self.engines, self.emit, cfg)
        self.pipeline.tts_sink = self._tts_sink
        self.link = LinkClient(self.room_name, core_url=cfg.network.core_url, key=cfg.network.internal_key,
                               agent_id=f"{ctx.worker_id}/{ctx.job.id}", on_state=self.pipeline.set_state,
                               on_control=self.pipeline.handle_control, on_utt_seq=self._continue_utts)
        self.infos: dict[str, ParticipantInfo] = {}
        self.departed: dict[str, tuple[ParticipantInfo, float]] = {}  # identity -> (info, monotonic left)
        self.track_tasks: dict[str, asyncio.Task[None]] = {}
        self.turn_tasks: set[asyncio.Task[None]] = set()
        self.tts: dict[str, Any] = {}
        self._publish_errors = 0
        self._closed = False

    # ---------------------------------------------------------------- events out

    async def emit(self, env: Envelope) -> None:
        room = self.ctx.room
        try:
            if room.isconnected():
                await room.local_participant.publish_data(env.to_json(), reliable=True, topic=EVENTS_TOPIC)
        except Exception as e:  # noqa: BLE001  a dropped data packet must not stop the turn
            self._publish_errors += 1
            if self._publish_errors in (1, 10, 100):
                log.warning("publish_data failed (%d so far): %s", self._publish_errors, e)
        self.link.send_event(env)

    async def _emit_new(self, type: str, data: Any) -> None:
        await self.emit(make_event(type, self.room_name, data))

    # ---------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        from livekit.agents import AutoSubscribe

        room = self.ctx.room
        room.on("participant_connected", self._on_participant_connected)
        room.on("participant_disconnected", self._on_participant_disconnected)
        room.on("participant_metadata_changed", lambda p, *_: self._on_participant_changed(p))
        room.on("participant_attributes_changed",
                lambda _changed, p: self._on_participant_changed(p, prefer="attributes"))
        room.on("track_subscribed", self._on_track_subscribed)
        room.on("track_unsubscribed", self._on_track_unsubscribed)
        self.ctx.add_shutdown_callback(self.aclose)
        await self.ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        self.link.start()
        log.info("joined %s (mode %s, engines %s)", self.room_name, self.mode.id, self.engines.names)
        for p in list(room.remote_participants.values()):
            self._on_participant_connected(p)
            for pub in p.track_publications.values():
                if pub.track is not None and pub.subscribed:
                    self._on_track_subscribed(pub.track, pub, p)
        self._prepare_voices()
        for e in (self.engines.verify, self.engines.embed, self.engines.tts):
            if e is not None and hasattr(e, "gate"):
                e.gate = True  # live checks answer "warming up" (voice: the fallback) until the model is resident
        for e in (self.engines.translate, self.engines.verify, self.engines.embed, self.engines.tts):
            warm = getattr(e, "warmup", None)
            if callable(warm):
                asyncio.create_task(self._warm(warm, getattr(e, "name", "engine")))

    async def _warm(self, fn: Any, name: str) -> None:
        delay = 5.0
        for attempt in range(1, 8):
            if self._closed:
                return
            try:
                t0 = time.monotonic()
                await fn()
                log.info("%s warm (%.1f s)", name, time.monotonic() - t0)
                return
            except Exception as e:  # noqa: BLE001
                log.warning("warm-up of %s failed (attempt %d): %s", name, attempt, e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def aclose(self, *_: Any) -> None:
        if self._closed:
            return
        self._closed = True
        for t in list(self.track_tasks.values()):
            t.cancel()
        await asyncio.gather(*self.track_tasks.values(), return_exceptions=True)
        await self.pipeline.aclose()
        await self.link.aclose()
        await self.engines.aclose()
        log.info("left %s", self.room_name)

    # ---------------------------------------------------------------- membership

    def _continue_utts(self, seq: int) -> None:
        """C-12: core already holds ``u-0001 .. u-<seq>`` for this session (this agent replaces one that
        left: the room emptied past its departure timeout, or a crash). Continue after them."""
        if hasattr(self.pipeline, "seed_utterances"):
            self.pipeline.seed_utterances(seq)
        elif getattr(self.pipeline, "_utt_n", seq) < seq:
            self.pipeline._utt_n = seq
            log.info("continuing utterance ids after u-%04d", seq)

    def _sync_participants(self) -> None:
        now = time.monotonic()
        for ident, (_, left) in list(self.departed.items()):
            if ident in self.infos or now - left >= REJOIN_GRACE_S:
                del self.departed[ident]
        # C-12: a device that left stays a translation target during the grace; as "headphones", so
        # its voice (which nobody in the room hears now) pauses no microphone (C-14)
        away = [info.model_copy(update={"audio_out": "headphones"}) for info, _ in self.departed.values()]
        self.pipeline.set_participants(list(self.infos.values()) + away)
        self._prepare_voices()

    def _room_langs(self) -> list[str]:
        """The mode's fixed role languages plus every device's language (no "auto")."""
        langs = [rc.default_lang for rc in self.mode.roles.values()] + [i.lang for i in self.infos.values()]
        codes = (lang_code(x) for x in langs)
        return list(dict.fromkeys(c for c in codes if c and c not in ("auto", "und")))

    def _prepare_voices(self) -> None:
        """Resident voice processes (Piper binary) for the room's languages; idempotent, never blocks."""
        prepare = getattr(self.engines.tts, "prepare", None)
        if not callable(prepare):
            return
        try:
            prepare(self._room_langs())
        except Exception:  # voice is optional (§6.15): one-shot synthesis still works
            log.exception("voice prepare failed")

    def _on_participant_connected(self, p: Any) -> None:
        if is_agent(p):
            return
        info = participant_info(p)
        self.departed.pop(p.identity, None)
        self.infos[p.identity] = info
        self._sync_participants()
        asyncio.create_task(self._emit_new("participant.joined", info))

    def _on_participant_changed(self, p: Any, prefer: str = "metadata") -> None:
        """Metadata or attributes changed (core: language / voice output). Both carry the same JSON, so
        the second event of a pair finds nothing new and announces nothing."""
        if is_agent(p) or p.identity not in self.infos:
            return
        info = participant_info(p, prefer=prefer)
        old = self.infos.get(p.identity)
        if info != old:
            self.infos[p.identity] = info
            self._sync_participants()
            log.info("participant %s changed: lang %s -> %s, audio_out %s -> %s", p.identity,
                     old.lang if old else None, info.lang, old.audio_out if old else None, info.audio_out)
            asyncio.create_task(self._emit_new("participant.joined", info))

    def _on_participant_disconnected(self, p: Any) -> None:
        info = self.infos.pop(p.identity, None)
        if info is None:
            return
        if REJOIN_GRACE_S > 0 and info.role != "observer":
            self.departed[p.identity] = (info, time.monotonic())
            asyncio.get_running_loop().call_later(REJOIN_GRACE_S + 0.1, self._sync_participants)
        self._sync_participants()
        for sid, t in list(self.track_tasks.items()):
            if sid.startswith(p.identity + "/"):
                t.cancel()
        asyncio.create_task(self._emit_new("participant.left", {"id": p.identity}))

    def _on_track_subscribed(self, track: Any, pub: Any, p: Any) -> None:
        from livekit import rtc

        if track.kind != rtc.TrackKind.KIND_AUDIO or is_agent(p):
            return
        if pub.source not in (rtc.TrackSource.SOURCE_MICROPHONE, rtc.TrackSource.SOURCE_UNKNOWN):
            return
        key = f"{p.identity}/{track.sid}"
        if key in self.track_tasks:
            return
        if p.identity not in self.infos:
            self._on_participant_connected(p)
        task = asyncio.create_task(self._track_loop(p.identity, track), name=f"mic-{key}")
        self.track_tasks[key] = task
        task.add_done_callback(lambda _t, k=key: self.track_tasks.pop(k, None))
        log.info("listening to %s (%s)", p.identity, self.infos.get(p.identity))

    def _on_track_unsubscribed(self, track: Any, pub: Any, p: Any) -> None:
        t = self.track_tasks.get(f"{p.identity}/{track.sid}")
        if t:
            t.cancel()

    # ---------------------------------------------------------------- audio in

    def _mic_frame(self, identity: str, frame: Any) -> Any:
        """Off the record, or paused for echo (C-14): the VAD gets silence, so it cannot start an utterance
        from the box's own voice and is in a clean state when the pause lifts."""
        if self.pipeline.mic_open(identity):
            return frame
        from livekit import rtc

        return rtc.AudioFrame(data=bytes(frame.samples_per_channel * 2), sample_rate=frame.sample_rate,
                              num_channels=1, samples_per_channel=frame.samples_per_channel)

    async def _track_loop(self, identity: str, track: Any) -> None:
        from livekit import rtc
        from livekit.agents.vad import VADEventType

        # Noise cleaning (raum.pipeline.clean) runs beside the raw path, never in front of the VAD:
        # VAD and partials hear the raw audio; the final ASR pass gets the cleaned samples.
        cleaner = cleaner_for(self.engines.denoise, self.cfg.models.denoise.options)
        cleaner = cleaner if cleaner is not None and cleaner.usable else None
        rate = 48000 if cleaner else 16000
        stream = rtc.AudioStream(track, sample_rate=rate, num_channels=1)
        vad_stream = self.vad.stream()
        cs = None
        resampler = None
        if cleaner is not None:
            import soxr

            def metric(ms: float) -> None:
                avg = self.pipeline.denoise_ms.get(identity)
                self.pipeline.denoise_ms[identity] = ms if avg is None else 0.9 * avg + 0.1 * ms

            cs = cleaner.stream(on_metric=metric)
            resampler = soxr.ResampleStream(48000, 16000, 1, dtype="float32")  # same as the clean path's

        async def pump() -> None:
            async for ev in stream:
                frame = ev.frame
                if cs is not None:
                    x = int16_to_float(frame.data)
                    if not self.pipeline.mic_open(identity):  # echo pause (C-14) on both paths
                        x = np.zeros_like(x)
                    cs.push(x)
                    z = resampler.resample_chunk(x)  # type: ignore[union-attr]
                    if z.size == 0:
                        continue
                    pcm = float_to_int16(z)
                    vad_stream.push_frame(rtc.AudioFrame(data=pcm.tobytes(), sample_rate=16000, num_channels=1,
                                                         samples_per_channel=len(pcm)))
                else:
                    vad_stream.push_frame(self._mic_frame(identity, frame))
            vad_stream.end_input()

        speaking = False

        async def events() -> None:
            nonlocal speaking
            async for ev in vad_stream:
                if ev.type == VADEventType.START_OF_SPEECH:
                    # None while off the record or paused for echo (C-14)
                    speaking = await self.pipeline.speech_start(identity) is not None
                    if speaking:
                        for f in ev.frames:
                            await self.pipeline.speech_audio(identity, int16_to_float(f.data))
                elif ev.type == VADEventType.INFERENCE_DONE:
                    if speaking and ev.speaking:
                        for f in ev.frames:
                            await self.pipeline.speech_audio(identity, int16_to_float(f.data))
                elif ev.type == VADEventType.END_OF_SPEECH and speaking:
                    speaking = False
                    audio = np.concatenate([int16_to_float(f.data) for f in ev.frames]) if ev.frames else None
                    if audio is not None and cs is not None:
                        audio = cs.cleaned(audio, int(ev.samples_index))
                    vad_ms = float(ev.silence_duration) * 1000.0
                    self._spawn_turn(self.pipeline.speech_end(identity, audio, vad_ms))
                    await asyncio.sleep(0)  # let speech_end detach the utterance before the next start
            if speaking:
                speaking = False
                self._spawn_turn(self.pipeline.speech_end(identity, None, 0.0))

        try:
            await asyncio.gather(pump(), events())
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("audio loop for %s failed", identity)
        finally:
            if speaking and not self._closed:
                # C-12: the speaker dropped mid-sentence; finish the turn with what was heard, so no
                # utterance stays open on the other devices until (and across) the rejoin
                self._spawn_turn(self.pipeline.speech_end(identity, None, 0.0))
            if cs is not None:
                cs.close()
            await stream.aclose()
            await vad_stream.aclose()

    def _spawn_turn(self, coro: Any) -> None:
        t = asyncio.create_task(coro)
        self.turn_tasks.add(t)
        t.add_done_callback(self.turn_tasks.discard)

    # ---------------------------------------------------------------- voice out (C-13, C-14)

    async def _tts_source(self, lang: str, rate: int) -> Any:
        from livekit import rtc

        src = self.tts.get(lang)
        if src is None:
            src = rtc.AudioSource(rate, 1, queue_size_ms=2000)
            track = rtc.LocalAudioTrack.create_audio_track(f"tts-{lang}", src)
            opts = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            await self.ctx.room.local_participant.publish_track(track, opts)
            self.tts[lang] = src
            log.info("publishing voice track tts-%s", lang)
        return src

    async def _tts_sink(self, lang: str, pcm: np.ndarray, rate: int) -> None:
        from livekit import rtc

        src = await self._tts_source(lang, rate)
        step = rate // 50  # 20 ms frames
        pcm = np.asarray(pcm, dtype=np.int16)
        for i in range(0, len(pcm), step):
            chunk = pcm[i:i + step]
            await src.capture_frame(rtc.AudioFrame(data=chunk.tobytes(), sample_rate=rate, num_channels=1,
                                                   samples_per_channel=len(chunk)))


# ---------------------------------------------------------------- job entry points (module level: picklable)


async def request_fnc(req: Any) -> None:
    name = req.room.name
    try:
        mode_for_room(name)
    except ModeError:
        log.info("not a RAUM room, skipping: %s", name)
        await req.reject()
        return
    await req.accept(name=AGENT_NAME, identity=AGENT_IDENTITY, attributes={"raum.agent": "1"})


async def entrypoint(ctx: Any) -> None:
    cfg = get_config()
    mode = mode_for_room(ctx.job.room.name)
    vad = ctx.proc.userdata.get("vad") or load_vad(cfg)
    agent = RoomAgent(ctx, cfg, mode, vad)
    await agent.start()


def run(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="raum agent", description="RAUM LiveKit agent worker")
    ap.add_argument("mode", nargs="?", choices=["start", "dev"], default="start")
    ap.add_argument("--agent-name", default=None,
                    help="explicit dispatch only (default: RAUM_AGENT_NAME / network.agent_name; empty = "
                         "auto-dispatch to every RAUM room)")
    ap.add_argument("--executor", choices=["process", "thread"],
                    default=os.environ.get("RAUM_AGENT_EXECUTOR", "process"))
    ap.add_argument("--idle", type=int, default=int(os.environ.get("RAUM_AGENT_IDLE", "1")),
                    help="warm job processes kept ready")
    ap.add_argument("--log-level", default=os.environ.get("LIVEKIT_LOG_LEVEL", "INFO"))
    args = ap.parse_args(argv or [])

    from livekit.agents import AgentServer, JobExecutorType, WorkerPermissions
    from livekit.agents.cli import cli as lk_cli
    from livekit.agents.cli import proto

    cfg = get_config()
    if args.agent_name is None:
        args.agent_name = cfg.network.dispatch_name()  # core puts the same name into its tokens
    url, key, secret = livekit_credentials(cfg)
    if args.executor == "thread":
        # plugins must register on the main thread; all room threads then share one Silero session
        load_vad(cfg)
    max_rooms = max(1, int(os.environ.get("RAUM_AGENT_MAX_ROOMS", "12")))

    def room_load(srv: Any) -> float:
        # Capacity is rooms (the GPU queue in raum.scheduler paces the models), not host CPU:
        # browsers and builds on a laptop must not make the box refuse a room.
        try:
            return min(1.0, len(srv.active_jobs) / max_rooms)
        except (AttributeError, RuntimeError):
            return 0.0

    server = AgentServer(
        ws_url=url, api_key=key, api_secret=secret, setup_fnc=prewarm,
        load_fnc=room_load, load_threshold=0.999,
        job_executor_type=JobExecutorType.THREAD if args.executor == "thread" else JobExecutorType.PROCESS,
        num_idle_processes=args.idle, initialize_process_timeout=30.0,
        port=int(os.environ.get("RAUM_AGENT_HTTP_PORT", "0")),  # 8081 is whisper on the box
        permissions=WorkerPermissions(can_publish=True, can_subscribe=True, can_publish_data=True),
    )
    server.rtc_session(entrypoint, agent_name=args.agent_name, on_request=request_fnc)
    log.info("raum-agent: %s profile=%s dispatch=%s", url, cfg.profile, args.agent_name or "auto")
    lk_cli._run_worker(server, proto.CliArgs(log_level=args.log_level.upper(), url=url, api_key=key,
                                             api_secret=secret, dev=args.mode == "dev"))
    return 0
