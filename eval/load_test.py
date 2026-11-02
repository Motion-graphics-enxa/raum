#!/usr/bin/env python3
"""Load test (plan §12.3): N rooms x 2 bot participants talking, p50/p95 latency per N.

    eval/make_fixtures.sh de ru                      # spoken utterances (once)
    # full run against the box (from the Mac over the LAN, or on the box itself)
    uv run --project core python eval/load_test.py --url ws://<spark-ip>:7880 --lang ru
    # smoke: one room, 30 s, local livekit-server --dev
    uv run --project core python eval/load_test.py --ns 1 --duration 30

Each room gets two bots (identity like ``b-k3x9``, token minted here with livekit-api, metadata
``{"role","lang","device","path"}``): role a speaks German, role b the demo language. Each bot plays
the next pre-recorded utterance every ``--interval`` seconds (b offset by half an interval), with
silence in between so the agent's VAD sees real endpoints. Rooms start at random offsets.

Collected from the data channel (topic ``raum.events``):
  * ``pipeline.timing``  vad/asr/mt_first/mt_full/tts_first/check ms, as measured by the agent
  * client-observed latency: bot's end of speech -> first ``translation.delta`` (e2e_first) and
    -> ``translation.final`` (e2e_full) for that utterance; includes network and VAD endpointing

Writes eval/out/load.csv (one row per N; rewritten after every N so a crash keeps earlier rows),
eval/out/load.json, and eval/out/load.png if matplotlib happens to be installed.
No agent in the room = bots still join and publish; timings are simply 0.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import string
import sys
import time
import warnings
import wave
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import EVAL_DIR, OUT_DIR, now_iso, percentile, write_json

TIMING_FIELDS = ["vad_ms", "asr_ms", "mt_first_ms", "mt_full_ms", "tts_first_ms", "check_ms"]
E2E_FIELDS = ["e2e_first_ms", "e2e_full_ms"]
PREFIXES = ["sz", "kz", "vr"]
SR = 16000
warnings.filterwarnings("ignore", message="The HMAC key is")  # livekit --dev key "secret" is short
FRAME = SR // 100  # 10 ms


# ------------------------------------------------------------------ audio


def load_clips(folder: Path, limit: int = 0) -> list[bytes]:
    """16-bit mono PCM at 16 kHz from every wav in folder (other rates/channels converted)."""
    clips = []
    for p in sorted(folder.glob("*.wav")):
        with wave.open(str(p), "rb") as w:
            if w.getsampwidth() != 2:
                print(f"skip {p.name}: not 16-bit", file=sys.stderr)
                continue
            raw, ch, rate = w.readframes(w.getnframes()), w.getnchannels(), w.getframerate()
        if ch != 1 or rate != SR:
            import numpy as np
            import soxr

            a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768
            a = a.reshape(-1, ch).mean(axis=1)
            if rate != SR:
                a = soxr.resample(a, rate, SR)
            raw = (np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes()
        clips.append(raw)
        if limit and len(clips) >= limit:
            break
    return clips


# ------------------------------------------------------------------ collection


@dataclass
class Collector:
    timings: dict[tuple, dict] = field(default_factory=dict)  # (room, utt, target_lang) -> data
    first_delta: dict[tuple, float] = field(default_factory=dict)
    final: dict[tuple, float] = field(default_factory=dict)
    speaker: dict[tuple, str] = field(default_factory=dict)  # (room, utt) -> identity
    counts: dict[str, int] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)  # from pipeline.timing.models: mock vs real engines

    def add(self, room: str, env: dict, t: float) -> None:
        typ, utt, data = env.get("type"), env.get("utt"), env.get("data") or {}
        if not typ:
            return
        self.counts[typ] = self.counts.get(typ, 0) + 1
        room = env.get("room") or room
        tl = data.get("target_lang")
        if typ == "pipeline.timing":
            self.timings.setdefault((room, utt, tl), data)
            if data.get("models") and not self.models:
                self.models = dict(data["models"])
        elif typ in ("utterance.start", "transcript.final") and data.get("speaker"):
            self.speaker.setdefault((room, utt), data["speaker"])
        elif typ == "translation.delta":
            self.first_delta.setdefault((room, utt, tl), t)
        elif typ == "translation.final":
            self.final.setdefault((room, utt, tl), t)


class Bot:
    def __init__(self, url: str, token: str, identity: str, room: str, role: str, lang: str,
                 clips: list[bytes], offset: float, interval: float, duration: float, col: Collector) -> None:
        self.url, self.token, self.identity, self.room_name = url, token, identity, room
        self.role, self.lang, self.clips = role, lang, clips
        self.offset, self.interval, self.duration, self.col = offset, interval, duration, col
        self.utt_ends: list[float] = []  # monotonic time when each utterance's last sample played
        self.sent = 0
        self.agent_seen = False
        self.error: str | None = None
        self.room: Any = None

    def _on_data(self, pkt: Any) -> None:
        if pkt.topic not in (None, "", "raum.events"):
            return
        try:
            env = json.loads(pkt.data)
        except (ValueError, UnicodeDecodeError):
            return
        if isinstance(env, dict):
            self.col.add(self.room_name, env, time.monotonic())

    def _check_agent(self, p: Any) -> None:
        from livekit import rtc

        if getattr(p, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT or \
                str(getattr(p, "identity", "")).startswith("agent"):
            self.agent_seen = True

    async def run(self) -> None:
        from livekit import rtc

        self.room = rtc.Room()
        self.room.on("data_received", self._on_data)
        self.room.on("participant_connected", self._check_agent)
        try:
            await self.room.connect(self.url, self.token, rtc.RoomOptions(auto_subscribe=False))
            for p in self.room.remote_participants.values():
                self._check_agent(p)
            source = rtc.AudioSource(SR, 1, queue_size_ms=200)
            track = rtc.LocalAudioTrack.create_audio_track("mic", source)
            await self.room.local_participant.publish_track(
                track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE))
            await self._play(source)
            await source.aclose()
        except Exception as e:  # noqa: BLE001
            self.error = repr(e)

    async def _play(self, source: Any) -> None:
        from livekit import rtc

        silence = bytes(FRAME * 2)
        frames_total = int(self.duration * 100)
        next_frame = int(self.offset * 100)
        t0 = time.monotonic()
        buf, pos, k = None, 0, 0
        for n in range(frames_total):
            if buf is None and n >= next_frame and self.clips:
                buf, pos = self.clips[k % len(self.clips)], 0
                k += 1
                next_frame += int(self.interval * 100)
            if buf is not None:
                chunk = buf[pos:pos + FRAME * 2]
                pos += FRAME * 2
                if len(chunk) < FRAME * 2:
                    chunk = chunk + bytes(FRAME * 2 - len(chunk))
                if pos >= len(buf):
                    buf = None
                    self.sent += 1
                    # the media clock: frame n plays at t0 + n*10 ms once the source runs in real time
                    self.utt_ends.append(t0 + (n + 1) / 100)
            else:
                chunk = silence
            await source.capture_frame(rtc.AudioFrame(chunk, SR, 1, FRAME))

    def end_before(self, t: float) -> float | None:
        ends = [e for e in self.utt_ends if e <= t]
        return ends[-1] if ends else None

    async def close(self) -> None:
        if self.room is not None:
            try:
                await self.room.disconnect()
            except Exception:  # noqa: BLE001
                pass


def mint(key: str, secret: str, identity: str, room: str, role: str, lang: str) -> str:
    from livekit import api

    meta = json.dumps({"role": role, "lang": lang, "device": "laptop", "path": "direct"})
    return (api.AccessToken(key, secret).with_identity(identity).with_name(f"Load bot {role} ({lang})")
            .with_metadata(meta).with_ttl(timedelta(hours=2))
            .with_grants(api.VideoGrants(room_join=True, room=room, can_publish=True, can_subscribe=True,
                                         can_publish_data=True)).to_jwt())


async def server_view(args: argparse.Namespace, rooms: list[str], delay: float, out: dict[str, Any]) -> None:
    """Ask the LiveKit server (RoomService) who is in the rooms: proves the bots joined and publish."""
    from livekit import api

    await asyncio.sleep(delay)
    http = args.url.replace("wss://", "https://").replace("ws://", "http://")
    try:
        async with api.LiveKitAPI(http, args.api_key, args.api_secret) as lk:
            bots = tracks = 0
            for room in rooms:
                res = await lk.room.list_participants(api.ListParticipantsRequest(room=room))
                for p in res.participants:
                    if p.identity.startswith("b-"):
                        bots += 1
                        tracks += sum(1 for t in p.tracks if t.type == 0)  # TrackType.AUDIO
            out.update(bots=bots, audio_tracks=tracks)
    except Exception as e:  # noqa: BLE001
        out["error"] = repr(e)
        print(f"server check failed: {e!r}", file=sys.stderr)


def ident(rng: random.Random) -> str:
    return "b-" + "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(4))


# ------------------------------------------------------------------ one N


async def run_n(n: int, args: argparse.Namespace, clips_a: list[bytes], clips_b: list[bytes],
                rng: random.Random) -> dict[str, Any]:
    col = Collector()
    tag = "".join(rng.choice(string.ascii_lowercase) for _ in range(3))
    bots: list[Bot] = []
    rooms = []
    for i in range(n):
        room = f"{PREFIXES[i % 3]}-lt{tag}{n:02d}{i:02d}"
        rooms.append(room)
        start = rng.uniform(0, args.interval) if n > 1 else 0.5
        for role, lang, clips, off in (("a", "de", clips_a, start), ("b", args.lang, clips_b,
                                                                       start + args.interval / 2)):
            idn = ident(rng)
            bots.append(Bot(args.url, mint(args.api_key, args.api_secret, idn, room, role, lang), idn, room,
                            role, lang, clips, off, args.interval, args.duration, col))
    print(f"N={n}: {len(bots)} bots in {len(rooms)} rooms for {args.duration:.0f}s", file=sys.stderr)
    t0 = time.monotonic()
    seen: dict[str, Any] = {}
    check = asyncio.create_task(server_view(args, rooms, min(6.0, args.duration / 2), seen))
    await asyncio.gather(*(b.run() for b in bots))
    await check
    await asyncio.sleep(args.drain)
    for b in bots:
        await b.close()
    by_id = {b.identity: b for b in bots}

    e2e_first, e2e_full = [], []
    for key, t in col.first_delta.items():
        bot = by_id.get(col.speaker.get((key[0], key[1]), ""))
        end = bot.end_before(t) if bot else None
        if end is not None and t - end < args.interval:
            e2e_first.append((t - end) * 1000)
    for key, t in col.final.items():
        bot = by_id.get(col.speaker.get((key[0], key[1]), ""))
        end = bot.end_before(t) if bot else None
        if end is not None and t - end < args.interval:
            e2e_full.append((t - end) * 1000)

    row: dict[str, Any] = {
        "n_rooms": n, "bots": len(bots), "duration_s": args.duration, "interval_s": args.interval,
        "wall_s": round(time.monotonic() - t0, 1),
        "bots_failed": sum(1 for b in bots if b.error),
        "rooms_with_agent": len({b.room_name for b in bots if b.agent_seen}),
        "server_bots": seen.get("bots"),  # what LiveKit itself lists mid-run (None = API unreachable)
        "server_audio_tracks": seen.get("audio_tracks"),
        "utterances_sent": sum(b.sent for b in bots),
        "timing_events": len(col.timings),
        "events_total": sum(col.counts.values()),
        "models": ";".join(f"{k}={v}" for k, v in sorted(col.models.items())),
    }
    series = {f: [d.get(f) for d in col.timings.values()] for f in TIMING_FIELDS}
    series |= {"e2e_first_ms": e2e_first, "e2e_full_ms": e2e_full}
    for f, vals in series.items():
        p50, p95 = percentile(vals, 50), percentile(vals, 95)
        row[f"{f}_p50"] = None if p50 is None else round(p50, 1)
        row[f"{f}_p95"] = None if p95 is None else round(p95, 1)
    errors = [b.error for b in bots if b.error]
    print(f"N={n}: server saw {row['server_bots']} bots / {row['server_audio_tracks']} audio tracks; sent "
          f"{row['utterances_sent']} utterances, {row['timing_events']} timing events, "
          f"agent in {row['rooms_with_agent']}/{n} rooms, {row['bots_failed']} bot(s) failed"
          + (f" (first error: {errors[0]})" if errors else ""), file=sys.stderr)
    return {"row": row, "event_counts": col.counts, "errors": errors[:5]}


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def maybe_plot(path: Path, rows: list[dict]) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    ns = [r["n_rooms"] for r in rows]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    for f, label in (("e2e_first_ms", "first words"), ("e2e_full_ms", "full sentence")):
        for q, style in (("p50", "-o"), ("p95", "--o")):
            ys = [r.get(f"{f}_{q}") for r in rows]
            if any(y is not None for y in ys):
                ax.plot(ns, [y / 1000 if y is not None else float("nan") for y in ys], style,
                        label=f"{label} {q}")
    ax.axhline(3.0, color="grey", lw=0.8)
    ax.set_xlabel("conversations at once")
    ax.set_ylabel("seconds after speaker stops")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    return True


async def main_async(args: argparse.Namespace) -> int:
    fx = Path(args.fixtures)
    clips_a, clips_b = load_clips(fx / "de", args.clips), load_clips(fx / args.lang, args.clips)
    if not clips_a or not clips_b:
        print(f"no fixtures in {fx}/de or {fx}/{args.lang}: run eval/make_fixtures.sh de {args.lang}",
              file=sys.stderr)
        return 2
    rng = random.Random(args.seed)
    out = Path(args.out)
    rows, details = [], []
    for n in [int(x) for x in args.ns.split(",") if x.strip()]:
        res = await run_n(n, args, clips_a, clips_b, rng)
        rows.append(res["row"])
        details.append(res)
        write_csv(out / "load.csv", rows)
        write_json(out / "load.json", {"generated_at": now_iso(), "url": args.url, "lang": args.lang,
                                       "runs": details})
        if n != int(args.ns.split(",")[-1]):
            await asyncio.sleep(args.pause)
    plotted = maybe_plot(out / "load.png", rows)
    print(f"wrote {out / 'load.csv'}, {out / 'load.json'}" + (f", {out / 'load.png'}" if plotted else ""))
    for r in rows:
        print(f"N={r['n_rooms']:>2}  timings={r['timing_events']:>4}  "
              f"mt_first p50/p95={r['mt_first_ms_p50']}/{r['mt_first_ms_p95']}  "
              f"e2e_full p50/p95={r['e2e_full_ms_p50']}/{r['e2e_full_ms_p95']}")
    return 1 if all(r["bots_failed"] == r["bots"] for r in rows) else 0


def main(argv: list[str] | None = None) -> int:
    try:
        from raum.config import load_config

        net = load_config().network
        d_url, d_key, d_secret = net.livekit_internal_url, net.livekit_api_key, net.livekit_api_secret
    except Exception:  # noqa: BLE001
        d_url, d_key, d_secret = "ws://127.0.0.1:7880", "devkey", "secret"
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=d_url, help=f"LiveKit URL (default {d_url})")
    ap.add_argument("--api-key", default=d_key)
    ap.add_argument("--api-secret", default=d_secret)
    ap.add_argument("--ns", default="1,2,4,8,12", help="room counts to run, in order")
    ap.add_argument("--duration", type=float, default=300.0, help="seconds per N")
    ap.add_argument("--interval", type=float, default=12.0, help="seconds between one bot's utterances")
    ap.add_argument("--lang", default="ru", help="language of role b (fixtures in <fixtures>/<lang>)")
    ap.add_argument("--fixtures", default=str(EVAL_DIR / "fixtures" / "gen"))
    ap.add_argument("--clips", type=int, default=0, help="max clips per language (0 = all)")
    ap.add_argument("--drain", type=float, default=8.0, help="seconds to wait for late events after speaking")
    ap.add_argument("--pause", type=float, default=10.0, help="seconds between N runs (rooms empty out)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default=str(OUT_DIR))
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
