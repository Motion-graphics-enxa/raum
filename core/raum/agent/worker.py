"""raum-agent: the LiveKit Agents worker (plan §6.1). One agent participant per room.

    raum agent [start|dev] [--agent-name NAME] [--executor process|thread] [--idle N] [--log-level L]

Auto-dispatch to every room whose id encodes a mode (``sz-4821``, ``kz-1170``, ``vr-3305``); other
rooms are rejected. LiveKit URL / key / secret from ``LIVEKIT_URL`` / ``LIVEKIT_API_KEY`` /
``LIVEKIT_API_SECRET``, else ``raum.yaml`` ``network``.

Per room: subscribe to every remote participant's mic (agents skipped). Participant metadata is
JSON ``{"role", "lang", "device", "path"}``. Per track: ``rtc.AudioStream`` (16 kHz mono; or 48 kHz
-> denoise -> soxr to 16 kHz when a denoiser is active) -> Silero VAD stream; START_OF_SPEECH /
INFERENCE_DONE / END_OF_SPEECH drive ``RoomPipeline.speech_start / speech_audio / speech_end``
(the end-of-speech frames are the final audio; ``vad_ms`` is the endpoint delay). Every event is
published on the data channel (topic ``raum.events``, reliable) and forwarded to core over the link.
Voice output is published as one track per language, ``tts-<lang>``; while it plays (+300 ms) mic
audio in the room is ignored (echo control C-14).
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
from raum.pipeline._util import float_to_int16, int16_to_float
from raum.pipeline.engines import build_engines
from raum.pipeline.room import RoomPipeline

log = logging.getLogger("raum.agent")

AGENT_IDENTITY = "raum-agent"
AGENT_NAME = "RAUM"
ECHO_TAIL_S = 0.3  # C-14: ignore mics for the playback duration plus 300 ms
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

    proc.userdata["vad"] = load_vad(get_config())


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


def participant_info(p: Any) -> ParticipantInfo:
    """Metadata JSON ``{"role","lang","device","path"}``; attributes as fallback; identity ``b-k3x9``."""
    meta: dict[str, Any] = {}
    if p.metadata:
        try:
            v = json.loads(p.metadata)
            meta = v if isinstance(v, dict) else {}
        except ValueError:
            meta = {}
    attrs = dict(p.attributes or {})
    head = p.identity.split("-", 1)[0]
    role = meta.get("role") or attrs.get("role") or (head if head in ("a", "b") else "b")
    if role not in ("a", "b", "observer"):
        role = "b"
    path = meta.get("path") or attrs.get("path") or "direct"
    if path not in ("direct", "tunnel", "relayed"):
        path = "direct"
    return ParticipantInfo(id=p.identity, role=role, lang=str(meta.get("lang") or attrs.get("lang") or "auto"),
                           device=str(meta.get("device") or attrs.get("device") or "phone"), path=path)


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
                               on_control=self.pipeline.handle_control)
        self.infos: dict[str, ParticipantInfo] = {}
        self.track_tasks: dict[str, asyncio.Task[None]] = {}
        self.turn_tasks: set[asyncio.Task[None]] = set()
        self.tts: dict[str, Any] = {}
        self.echo_until = 0.0
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
        room.on("participant_attributes_changed", lambda _changed, p: self._on_participant_changed(p))
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
        for e in (self.engines.verify, self.engines.embed):
            if e is not None and hasattr(e, "gate"):
                e.gate = True  # live checks answer "warming up" until the model is resident
        for e in (self.engines.translate, self.engines.verify, self.engines.embed):
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

    def _sync_participants(self) -> None:
        self.pipeline.set_participants(list(self.infos.values()))

    def _on_participant_connected(self, p: Any) -> None:
        if is_agent(p):
            return
        info = participant_info(p)
        self.infos[p.identity] = info
        self._sync_participants()
        asyncio.create_task(self._emit_new("participant.joined", info))

    def _on_participant_changed(self, p: Any) -> None:
        if is_agent(p) or p.identity not in self.infos:
            return
        info = participant_info(p)
        if info != self.infos.get(p.identity):
            self.infos[p.identity] = info
            self._sync_participants()
            asyncio.create_task(self._emit_new("participant.joined", info))

    def _on_participant_disconnected(self, p: Any) -> None:
        if self.infos.pop(p.identity, None) is None:
            return
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

    def _gated(self) -> bool:
        return self.pipeline.off_record or time.monotonic() < self.echo_until

    async def _track_loop(self, identity: str, track: Any) -> None:
        from livekit import rtc
        from livekit.agents.vad import VADEventType

        denoise = self.engines.denoise
        clean = bool(getattr(denoise, "active", False))
        rate = 48000 if clean else 16000
        stream = rtc.AudioStream(track, sample_rate=rate, num_channels=1)
        vad_stream = self.vad.stream()
        resampler = None
        if clean:
            import soxr

            resampler = soxr.ResampleStream(48000, 16000, 1, dtype="float32")

        async def pump() -> None:
            avg = None
            async for ev in stream:
                frame = ev.frame
                if clean:
                    x = int16_to_float(frame.data)
                    t = time.perf_counter()
                    y = denoise.process(x, 48000)
                    ms = (time.perf_counter() - t) * 1000.0 * (4800.0 / len(x))  # per 100 ms of audio
                    avg = ms if avg is None else 0.9 * avg + 0.1 * ms
                    self.pipeline.denoise_ms[identity] = avg
                    z = resampler.resample_chunk(y)  # type: ignore[union-attr]
                    if z.size == 0:
                        continue
                    pcm = float_to_int16(z)
                    frame = rtc.AudioFrame(data=pcm.tobytes(), sample_rate=16000, num_channels=1,
                                           samples_per_channel=len(pcm))
                if self._gated():
                    frame = rtc.AudioFrame(data=bytes(frame.samples_per_channel * 2), sample_rate=frame.sample_rate,
                                           num_channels=1, samples_per_channel=frame.samples_per_channel)
                vad_stream.push_frame(frame)
            vad_stream.end_input()

        async def events() -> None:
            speaking = False
            async for ev in vad_stream:
                if ev.type == VADEventType.START_OF_SPEECH:
                    if self._gated():
                        continue
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
                    vad_ms = float(ev.silence_duration) * 1000.0
                    self._spawn_turn(self.pipeline.speech_end(identity, audio, vad_ms))
                    await asyncio.sleep(0)  # let speech_end detach the utterance before the next start
            if speaking:
                self._spawn_turn(self.pipeline.speech_end(identity, None, 0.0))

        try:
            await asyncio.gather(pump(), events())
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("audio loop for %s failed", identity)
        finally:
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
            self.echo_until = max(self.echo_until, time.monotonic() + src.queued_duration + ECHO_TAIL_S)


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
    ap.add_argument("--agent-name", default=os.environ.get("RAUM_AGENT_NAME", ""),
                    help="explicit dispatch only (default: auto-dispatch to every RAUM room)")
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
