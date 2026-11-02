"""raum speakerphone (A-03): a USB or Bluetooth speakerphone on the box joins a room as a participant.

    raum speakerphone --room sz-4821 [--role a] [--lang de] [--device "Jabra"]
    raum speakerphone --list-devices
    raum speakerphone --room sz-4821 --wav x.wav --out-wav voice.wav      # no audio hardware (tests)

It joins with participant metadata ``{"role", "lang", "device": "speakerphone", "path": "direct",
"audio_out": "room"}`` (token minted with the box's LiveKit keys like ``scripts/bot.py``, with the same
room settings and agent dispatch as core's tokens), publishes the system microphone as its mic track
and plays the agent's ``tts-<lang>`` voice track on the device. ``audio_out: room`` tells the agent that
everyone in the room hears this speaker, so every mic pauses while it plays (C-14).

Audio goes through PortAudio (``sounddevice``, extra ``speakerphone``): on Linux that is ALSA, and
with PipeWire the ``pipewire`` / ``default`` devices route to PipeWire's default source and sink
(pick the speakerphone with ``wpctl set-default``), or name the device directly (``--device Jabra``,
a substring of its name, or its index from ``--list-devices``). 48 kHz mono is used when the device
takes it, otherwise its own rate, resampled.

Exit codes: 0 stopped (SIGINT / SIGTERM / ``--duration``), 2 bad arguments or room id, 3 no audio:
``sounddevice`` or PortAudio missing, no such device, device busy, or the device went away while
running. Exit 3 is final; ``deploy/box.sh`` does not restart it, so a missing device never becomes a
crash loop. LiveKit or core being down is not an error: it waits and rejoins with backoff.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import threading
import time
import wave
from collections import deque
from collections.abc import AsyncIterator
from typing import Any

import numpy as np

log = logging.getLogger("raum.speakerphone")

RATE = 48000  # LiveKit side: 48 kHz mono int16, 10 ms frames
FRAME = RATE // 100
EXIT_USAGE = 2
EXIT_NO_AUDIO = 3
WATCHDOG_S = 3.0  # no microphone block for this long while running = the device is gone


class AudioUnavailable(RuntimeError):
    """No usable audio: exit code 3, never retried."""


# ---------------------------------------------------------------- PortAudio devices


def load_sounddevice() -> Any:
    try:
        import sounddevice as sd
    except ImportError as e:
        raise AudioUnavailable("python package 'sounddevice' is missing: cd core && uv sync --extra speakerphone") from e
    except OSError as e:  # the package is there, libportaudio is not
        raise AudioUnavailable(
            f"PortAudio not found ({e}). Install it (Ubuntu: sudo apt install libportaudio2) or put "
            "libportaudio.so.2 on LD_LIBRARY_PATH") from e
    return sd


def list_devices(sd: Any) -> str:
    try:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        d_in, d_out = sd.default.device
    except Exception as e:
        raise AudioUnavailable(f"cannot list audio devices: {e}") from e
    if not len(devices):
        raise AudioUnavailable("no audio devices (is the speakerphone plugged in? ALSA/PipeWire visible?)")
    rows = ["  idx  in out   rate  api          name"]
    for i, d in enumerate(devices):
        mark = ("<" if i == d_in else " ") + (">" if i == d_out else " ")
        api = hostapis[d["hostapi"]]["name"] if 0 <= d["hostapi"] < len(hostapis) else "?"
        rows.append(f"{mark}{i:3d} {d['max_input_channels']:3d} {d['max_output_channels']:3d} "
                    f"{int(d['default_samplerate']):6d}  {api[:12]:12} {d['name']}")
    rows.append("  < default input, > default output. Use --device <index or part of the name>.")
    return "\n".join(rows)


def pick_device(sd: Any, spec: str | None, kind: str) -> int:
    """Index of the input or output device named by ``spec`` (index, or a case-insensitive part of
    the name), or the system default."""
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    try:
        devices = sd.query_devices()
        default = sd.default.device[0 if kind == "input" else 1]
    except Exception as e:
        raise AudioUnavailable(f"cannot list audio devices: {e}") from e
    if spec is None or spec == "":
        idx = default
        if idx is None or idx < 0 or idx >= len(devices):
            raise AudioUnavailable(f"no default {kind} device; pick one with --device (see --list-devices)")
    elif spec.strip().isdigit():
        idx = int(spec)
        if idx >= len(devices):
            raise AudioUnavailable(f"no audio device {idx} (see --list-devices)")
    else:
        hits = [i for i, d in enumerate(devices) if spec.casefold() in d["name"].casefold() and d[key] > 0]
        if not hits:
            raise AudioUnavailable(f"no {kind} device matching {spec!r} (see --list-devices)")
        idx = hits[0]
        if len(hits) > 1:
            log.info("%d %s devices match %r; using %d (%s)", len(hits), kind, spec, idx, devices[idx]["name"])
    if devices[idx][key] < 1:
        raise AudioUnavailable(f"device {idx} ({devices[idx]['name']}) has no {kind} channels")
    return int(idx)


def pick_format(sd: Any, idx: int, kind: str) -> tuple[int, int]:
    """(sample rate, channels) the device accepts as int16: 48 kHz mono preferred."""
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    d = sd.query_devices(idx)
    rates = [RATE] + [r for r in (int(d["default_samplerate"]), 44100, 16000) if r != RATE]
    errors = []
    for ch in (1, 2):
        for rate in rates:
            try:
                check(device=idx, channels=ch, dtype="int16", samplerate=rate)
                return rate, ch
            except Exception as e:  # noqa: BLE001
                errors.append(str(e))
    raise AudioUnavailable(f"{kind} device {idx} ({d['name']}) takes no mono/stereo int16 format: {errors[-1]}")


# ---------------------------------------------------------------- sources: microphone or WAV


class MicSource:
    """System microphone -> 10 ms frames, 48 kHz mono int16. PortAudio's callback thread hands blocks
    to the event loop; a slow consumer loses the oldest audio, never the box."""

    def __init__(self, sd: Any, device: str | None) -> None:
        self.sd = sd
        self.idx = pick_device(sd, device, "input")
        self.rate, self.channels = pick_format(sd, self.idx, "input")
        self.name = sd.query_devices(self.idx)["name"]
        self._q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=100)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stream: Any = None
        self.overflows = 0

    def _cb(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status and status.input_overflow:
            self.overflows += 1
        block = np.array(indata, dtype=np.int16, copy=True)
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._push, block)

    def _push(self, block: np.ndarray) -> None:
        if self._q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._q.get_nowait()
        self._q.put_nowait(block)

    def open(self) -> None:
        self._loop = asyncio.get_running_loop()
        try:
            self._stream = self.sd.InputStream(device=self.idx, samplerate=self.rate, channels=self.channels,
                                               dtype="int16", blocksize=self.rate // 100, callback=self._cb)
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"cannot open microphone {self.idx} ({self.name}): {e}") from e
        log.info("microphone: [%d] %s, %d Hz, %d ch", self.idx, self.name, self.rate, self.channels)

    async def frames(self) -> AsyncIterator[np.ndarray]:
        resampler = None
        if self.rate != RATE:
            import soxr

            resampler = soxr.ResampleStream(self.rate, RATE, 1, dtype="int16")
        pending = np.zeros(0, dtype=np.int16)
        while True:
            try:
                block = await asyncio.wait_for(self._q.get(), timeout=WATCHDOG_S)
            except TimeoutError:
                raise AudioUnavailable(f"microphone {self.idx} ({self.name}) stopped delivering audio "
                                       "(unplugged?)") from None
            mono = block.reshape(-1, self.channels).mean(axis=1).astype(np.int16) if self.channels > 1 \
                else block.reshape(-1)
            if resampler is not None:
                mono = resampler.resample_chunk(mono)
            pending = np.concatenate([pending, mono])
            while len(pending) >= FRAME:
                yield pending[:FRAME]
                pending = pending[FRAME:]

    def close(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(Exception):
                self._stream.stop()
                self._stream.close()
            self._stream = None


class WavSource:
    """A WAV file as the microphone, in real time: the file, ``gap`` seconds of silence, repeated
    (``repeat`` 0 = forever), then silence."""

    def __init__(self, path: str, repeat: int = 1, gap: float = 6.0) -> None:
        self.path = path
        self.repeat = repeat
        self.gap = gap
        self.pcm = read_wav(path)

    def open(self) -> None:
        log.info("microphone: %s (%.1f s, repeat %s)", self.path, len(self.pcm) / RATE, self.repeat or "forever")

    async def frames(self) -> AsyncIterator[np.ndarray]:
        silence = np.zeros(FRAME, dtype=np.int16)
        t_next = time.monotonic()
        n = 0

        async def tick() -> None:
            nonlocal t_next
            t_next += 0.01
            delay = t_next - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)

        while True:
            if self.repeat == 0 or n < self.repeat:
                n += 1
                for i in range(0, len(self.pcm), FRAME):
                    chunk = self.pcm[i:i + FRAME]
                    yield chunk if len(chunk) == FRAME else np.concatenate([chunk, silence[: FRAME - len(chunk)]])
                    await tick()
            end = time.monotonic() + self.gap
            while time.monotonic() < end:
                yield silence
                await tick()

    def close(self) -> None:
        pass


def read_wav(path: str) -> np.ndarray:
    """Mono int16 at 48 kHz."""
    try:
        with wave.open(path, "rb") as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (OSError, wave.Error) as e:
        raise SystemExit(f"{path}: {e}") from None
    if width != 2:
        raise SystemExit(f"{path}: need 16-bit PCM (got {8 * width}-bit)")
    a = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch).mean(axis=1)
    if rate != RATE:
        import soxr

        a = soxr.resample(a.astype(np.float32), rate, RATE)
    return np.clip(a, -32768, 32767).astype(np.int16)


# ---------------------------------------------------------------- sinks: speaker, WAV, nothing


class SpeakerSink:
    """Voice -> the device. PortAudio's callback pulls from a small jitter buffer: it starts playing
    once ``prebuffer_ms`` are queued and pads underruns with silence."""

    def __init__(self, sd: Any, device: str | None, prebuffer_ms: int = 60, max_ms: int = 3000) -> None:
        self.sd = sd
        self.idx = pick_device(sd, device, "output")
        self.rate, self.channels = pick_format(sd, self.idx, "output")
        self.name = sd.query_devices(self.idx)["name"]
        self.prebuffer = self.rate * prebuffer_ms // 1000
        self.max = self.rate * max_ms // 1000
        self._buf: deque[np.ndarray] = deque()
        self._n = 0
        self._lock = threading.Lock()
        self._primed = False
        self._stream: Any = None
        self._resampler: Any = None
        self.lost = False
        self.underruns = 0
        if self.rate != RATE:
            import soxr

            self._resampler = soxr.ResampleStream(RATE, self.rate, 1, dtype="int16")

    def open(self) -> None:
        try:
            self._stream = self.sd.OutputStream(device=self.idx, samplerate=self.rate, channels=self.channels,
                                                dtype="int16", blocksize=self.rate // 100, callback=self._cb,
                                                finished_callback=self._finished)
            self._stream.start()
        except Exception as e:
            raise AudioUnavailable(f"cannot open speaker {self.idx} ({self.name}): {e}") from e
        log.info("speaker: [%d] %s, %d Hz, %d ch", self.idx, self.name, self.rate, self.channels)

    def _finished(self) -> None:
        if self._stream is not None:  # not closed by us
            self.lost = True

    def write(self, pcm: np.ndarray) -> None:
        if self._resampler is not None:
            pcm = self._resampler.resample_chunk(pcm)
        if not len(pcm):
            return
        with self._lock:
            self._buf.append(pcm)
            self._n += len(pcm)
            while self._n > self.max and self._buf:  # fell behind: drop the oldest voice
                self._n -= len(self._buf.popleft())

    def _cb(self, outdata: Any, frames: int, time_info: Any, status: Any) -> None:
        out = np.zeros(frames, dtype=np.int16)
        with self._lock:
            if not self._primed and self._n >= self.prebuffer:
                self._primed = True
            if self._primed:
                got = 0
                while got < frames and self._buf:
                    head = self._buf[0]
                    take = min(frames - got, len(head))
                    out[got:got + take] = head[:take]
                    got += take
                    if take == len(head):
                        self._buf.popleft()
                    else:
                        self._buf[0] = head[take:]
                self._n -= got
                if got < frames:
                    self._primed = False
                    if got:
                        self.underruns += 1
        outdata[:] = np.repeat(out[:, None], self.channels, axis=1) if self.channels > 1 else out[:, None]

    def close(self) -> None:
        s, self._stream = self._stream, None
        if s is not None:
            with contextlib.suppress(Exception):
                s.stop()
                s.close()


class WavSink:
    """Voice -> a WAV file (48 kHz mono), for tests without audio hardware."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._w: Any = None
        self.lost = False
        self.samples = 0

    def open(self) -> None:
        self._w = wave.open(self.path, "wb")  # noqa: SIM115  (closed in close())
        self._w.setnchannels(1)
        self._w.setsampwidth(2)
        self._w.setframerate(RATE)
        log.info("speaker: %s", self.path)

    def write(self, pcm: np.ndarray) -> None:
        if self._w is not None:
            self._w.writeframes(np.asarray(pcm, dtype=np.int16).tobytes())
            self.samples += len(pcm)

    def close(self) -> None:
        if self._w is not None:
            self._w.close()
            self._w = None
            log.info("voice written: %s (%.1f s)", self.path, self.samples / RATE)


class NullSink:
    lost = False

    def open(self) -> None:
        log.info("speaker: off (--no-playback)")

    def write(self, pcm: np.ndarray) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- the participant


def is_agent(p: Any) -> bool:
    from livekit import rtc

    return p.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT or p.identity.startswith(("agent-", "raum-agent"))


def mint_token(cfg: Any, a: argparse.Namespace, name: str) -> str:
    import warnings

    from livekit import api

    from raum.api.join import room_configuration

    meta = {"role": a.role, "lang": a.lang, "device": "speakerphone", "path": "direct", "audio_out": "room"}
    tok = (api.AccessToken(a.api_key, a.api_secret)
           .with_identity(a.identity)
           .with_name(name)
           .with_metadata(json.dumps(meta))
           .with_attributes({k: str(v) for k, v in meta.items()})
           .with_grants(api.VideoGrants(room_join=True, room=a.room, can_publish=True, can_subscribe=True,
                                        can_publish_data=True))
           .with_room_config(room_configuration(cfg)))
    with warnings.catch_warnings():  # livekit-server --dev uses a short secret
        warnings.filterwarnings("ignore", message=".*HMAC key.*")
        return tok.to_jwt()


class Speakerphone:
    def __init__(self, a: argparse.Namespace, cfg: Any, source: Any, sink: Any, name: str) -> None:
        self.a = a
        self.cfg = cfg
        self.source = source
        self.sink = sink
        self.name = name
        self.voice = f"tts-{a.lang}"
        self.stop = asyncio.Event()
        self.fatal: AudioUnavailable | None = None
        self.mic: Any = None  # rtc.AudioSource of the current connection
        self.connected = False
        self.played = 0.0
        self.sent = 0

    # ---- audio in: one capture loop for the whole run, fed into whichever connection is current

    async def _capture(self) -> None:
        try:
            async for pcm in self.source.frames():
                if self.stop.is_set():
                    return
                if self.sink.lost:
                    raise AudioUnavailable("the speaker went away (unplugged?)")
                mic = self.mic
                if mic is None or not self.connected:
                    continue
                from livekit import rtc

                frame = rtc.AudioFrame(data=pcm.tobytes(), sample_rate=RATE, num_channels=1,
                                       samples_per_channel=len(pcm))
                try:
                    await asyncio.wait_for(mic.capture_frame(frame), timeout=1.0)
                    self.sent += 1
                except Exception as e:  # noqa: BLE001  reconnecting: drop the frame
                    log.debug("mic frame dropped: %s", e)
        except AudioUnavailable as e:
            self.fatal = e
            self.stop.set()

    # ---- audio out: the agent's voice in our language

    async def _play(self, track: Any) -> None:
        from livekit import rtc

        stream = rtc.AudioStream(track, sample_rate=RATE, num_channels=1)
        log.info("playing %s", self.voice)
        try:
            async for ev in stream:
                pcm = np.frombuffer(ev.frame.data, dtype=np.int16)
                self.sink.write(pcm)
                self.played += len(pcm) / RATE
        finally:
            await stream.aclose()

    # ---- presence: RTT badge in the glass box and Admin (best effort; core may be down)

    async def _presence(self) -> None:
        import httpx

        core = self.cfg.network.core_url.rstrip("/")
        async with httpx.AsyncClient(timeout=3.0) as http:
            while not self.stop.is_set():
                if self.connected:
                    with contextlib.suppress(Exception):
                        t0 = time.perf_counter()
                        await http.get(f"{core}/api/health")
                        rtt = round((time.perf_counter() - t0) * 1000)
                        await http.post(f"{core}/api/rooms/{self.a.room}/presence",
                                        json={"identity": self.a.identity, "rtt_ms": rtt})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.stop.wait(), timeout=5.0)

    # ---- one LiveKit connection, until it ends

    async def _session(self, room: Any) -> None:
        from livekit import rtc

        players: dict[str, asyncio.Task[None]] = {}
        ended = asyncio.Event()

        def want(pub: Any, p: Any) -> bool:
            return pub.kind == rtc.TrackKind.KIND_AUDIO and pub.name == self.voice and is_agent(p)

        def on_published(pub: Any, p: Any) -> None:
            if want(pub, p):
                pub.set_subscribed(True)

        def on_subscribed(track: Any, pub: Any, p: Any) -> None:
            if want(pub, p) and pub.sid not in players:
                players[pub.sid] = asyncio.create_task(self._play(track))

        def on_unsubscribed(track: Any, pub: Any, p: Any) -> None:
            t = players.pop(pub.sid, None)
            if t:
                t.cancel()

        def on_disconnected(reason: Any = None) -> None:
            (log.info if self.stop.is_set() else log.warning)("disconnected from %s (reason %s)", self.a.room, reason)
            self.connected = False
            ended.set()

        def on_reconnecting() -> None:
            log.info("connection lost, LiveKit is reconnecting")
            self.connected = False

        def on_reconnected() -> None:
            log.info("reconnected")
            self.connected = True

        room.on("track_published", on_published)
        room.on("track_subscribed", on_subscribed)
        room.on("track_unsubscribed", on_unsubscribed)
        room.on("disconnected", on_disconnected)
        room.on("reconnecting", on_reconnecting)
        room.on("reconnected", on_reconnected)

        self.mic = rtc.AudioSource(RATE, 1, queue_size_ms=200)
        track = rtc.LocalAudioTrack.create_audio_track("speakerphone", self.mic)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
        self.connected = True
        for p in list(room.remote_participants.values()):
            for pub in list(p.track_publications.values()):
                on_published(pub, p)
                if pub.track is not None and pub.subscribed:
                    on_subscribed(pub.track, pub, p)
        log.info("in %s as %s (role %s, lang %s, voice %s)", self.a.room, self.a.identity, self.a.role, self.a.lang,
                 self.voice)
        stop_wait = asyncio.ensure_future(self.stop.wait())
        end_wait = asyncio.ensure_future(ended.wait())
        try:
            await asyncio.wait({stop_wait, end_wait}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stop_wait.cancel()
            end_wait.cancel()
            self.connected = False
            self.mic = None
            for t in players.values():
                t.cancel()
            await asyncio.gather(*players.values(), return_exceptions=True)
            if room.isconnected():
                await room.disconnect()

    async def run(self) -> int:
        from livekit import rtc

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop.set)
        self.source.open()
        self.sink.open()
        tasks = [asyncio.create_task(self._capture(), name="capture")]
        if not self.a.no_presence:
            tasks.append(asyncio.create_task(self._presence(), name="presence"))
        if self.a.duration:
            loop.call_later(self.a.duration, self.stop.set)
        backoff, failures = 1.0, 0
        try:
            while not self.stop.is_set():
                room = rtc.Room()
                try:
                    await room.connect(self.a.url, mint_token(self.cfg, self.a, self.name),
                                       rtc.RoomOptions(auto_subscribe=False))
                except Exception as e:  # noqa: BLE001  LiveKit down or restarting: wait, never crash
                    failures += 1
                    if failures in (1, 5) or failures % 20 == 0:
                        log.warning("cannot join %s at %s (attempt %d): %s; retrying", self.a.room, self.a.url,
                                    failures, e)
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), timeout=backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff, failures = 1.0, 0
                await self._session(room)
                if not self.stop.is_set():
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(self.stop.wait(), timeout=1.0)
        finally:
            self.stop.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self.source.close()
            self.sink.close()
        if self.fatal is not None:
            raise self.fatal
        log.info("stopped (mic frames sent %d, voice played %.1f s)", self.sent, self.played)
        return 0


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="raum speakerphone", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--room", default=os.environ.get("RAUM_SPEAKERPHONE_ROOM"), help="room id, e.g. sz-4821")
    ap.add_argument("--role", choices=["a", "b"], default=os.environ.get("RAUM_SPEAKERPHONE_ROLE", "a"))
    ap.add_argument("--lang", default=os.environ.get("RAUM_SPEAKERPHONE_LANG"),
                    help="speaker's language (default: the role's language in the room's mode, else de)")
    ap.add_argument("--device", default=os.environ.get("RAUM_SPEAKERPHONE_DEVICE") or None,
                    help="input and output device: index or part of the name (default: system default)")
    ap.add_argument("--in-device", help="microphone only (overrides --device)")
    ap.add_argument("--out-device", help="speaker only (overrides --device)")
    ap.add_argument("--list-devices", action="store_true", help="print the audio devices and exit")
    ap.add_argument("--wav", help="publish this WAV instead of the microphone (tests)")
    ap.add_argument("--repeat", type=int, default=1, help="with --wav: times to play it (0 = forever)")
    ap.add_argument("--gap", type=float, default=6.0, help="with --wav: seconds of silence after each play")
    ap.add_argument("--out-wav", help="write the voice to this WAV instead of playing it (tests)")
    ap.add_argument("--no-playback", action="store_true", help="do not play the voice")
    ap.add_argument("--identity", help="LiveKit identity (default: <role>-spk; a restart replaces the old one)")
    ap.add_argument("--duration", type=float, help="leave after N seconds")
    ap.add_argument("--no-presence", action="store_true", help="no RTT pings to core")
    ap.add_argument("--url", help="LiveKit URL (default: LIVEKIT_URL or network.livekit_internal_url)")
    ap.add_argument("--api-key")
    ap.add_argument("--api-secret")
    ap.add_argument("--log-level", default=os.environ.get("RAUM_LOG_LEVEL", "INFO"))
    return ap


def run(argv: list[str] | None = None) -> int:
    ap = build_parser()
    a = ap.parse_args(argv or [])
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # presence pings every 5 s
    try:
        if a.list_devices:
            print(list_devices(load_sounddevice()))
            return 0
        if not a.room:
            ap.print_usage()
            print("raum speakerphone: --room is required (or RAUM_SPEAKERPHONE_ROOM)")
            return EXIT_USAGE
        from raum.config import get_config
        from raum.modes import ModeError, mode_for_room

        try:
            mode = mode_for_room(a.room)
        except ModeError as e:
            print(f"raum speakerphone: {e}")
            return EXIT_USAGE
        cfg = get_config()
        if not a.lang:
            d = mode.roles[a.role].default_lang  # type: ignore[index]
            a.lang = d if d and d != "auto" else "de"
        a.lang = a.lang.split("-")[0].lower()
        a.identity = a.identity or f"{a.role}-spk"
        a.url = a.url or os.environ.get("LIVEKIT_URL") or cfg.network.livekit_internal_url
        a.api_key = a.api_key or os.environ.get("LIVEKIT_API_KEY") or cfg.network.livekit_api_key
        a.api_secret = a.api_secret or os.environ.get("LIVEKIT_API_SECRET") or cfg.network.livekit_api_secret
        sd = None
        if not a.wav or not (a.out_wav or a.no_playback):
            sd = load_sounddevice()
        source = WavSource(a.wav, a.repeat, a.gap) if a.wav else MicSource(sd, a.in_device or a.device)
        if a.out_wav:
            sink: Any = WavSink(a.out_wav)
        elif a.no_playback:
            sink = NullSink()
        else:
            sink = SpeakerSink(sd, a.out_device or a.device)
        name = f"{mode.label(a.role, a.lang)} (speakerphone)"
        return asyncio.run(Speakerphone(a, cfg, source, sink, name).run())
    except AudioUnavailable as e:
        print(f"raum speakerphone: no audio: {e}", flush=True)
        log.error("no audio: %s (exit %d: not restarted)", e, EXIT_NO_AUDIO)
        return EXIT_NO_AUDIO
