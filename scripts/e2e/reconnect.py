"""C-12 reconnect/resume check: a phone drops out of a live conversation and comes back.

    cd core && uv run python ../scripts/e2e/reconnect.py --wav ../recordings/de.wav --scenario stop --gap 10

A speaking bot (role a, de; ``scripts/bot.py``) talks every ``--every`` seconds. A listening phone
(role b, ar; this script's ``listen`` mode, same logic as ``app/src/lib/live.ts``) records every
envelope it gets on the data channel and, after a reconnect or a fresh rejoin, fetches the core
backlog (``GET /api/rooms/:room/events?since=lastTs-1``) and merges it, deduplicated.

Scenarios (what happens to the listener after ``--warmup`` seconds, for ``--gap`` seconds):

  stop    SIGSTOP / SIGCONT: the phone is frozen, no packet in or out (Wi-Fi gone, no goodbye)
  kill    SIGKILL, then a new process with the same token (app killed / page reloaded)
  leave   clean disconnect, then a rejoin in the same process, store kept (live.ts rejoin path)
  empty   both phones leave, the room is empty for the gap, both come back (agent restarts if the
          gap is longer than the room's departure_timeout)

Both phones join through core (``POST /api/join``), so the tokens carry core's room configuration
(departure_timeout, explicit dispatch with ``RAUM_AGENT_NAME``). Report: turns the listener missed
live and got from the backlog, turns missing in the end (must be 0), when LiveKit / the agent saw
the drop, whether the session and the agent survived, and whether utterance ids kept increasing.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import signal
import subprocess
import sys
import time
import warnings
from pathlib import Path

import httpx

warnings.filterwarnings("ignore", message="The HMAC key is")
HERE = Path(__file__).resolve().parent
BOT = HERE.parent / "bot.py"
EVENTS_TOPIC = "raum.events"


def now_ms() -> int:
    return int(time.time() * 1000)


def dedupe_key(e: dict) -> str | None:
    """Same key as ``app/src/lib/store.ts`` ``dedupeKey``."""
    t = e.get("type")
    if t in ("privacy.meter", "gpu.stats", "session.state"):
        return None
    d = e.get("data") or {}
    if t == "translation.delta":
        extra = f"{d.get('target_lang')}:{d.get('seq') if d.get('seq') is not None else ''}:{d.get('delta')}"
    elif t == "transcript.partial":
        extra = str(d.get("seq"))
    elif "target_lang" in d:
        extra = str(d["target_lang"])
    elif "id" in d:
        extra = str(d["id"])
    else:
        extra = ""
    return f"{t}|{e.get('utt') or ''}|{e.get('ts')}|{extra}"


# ---------------------------------------------------------------- listen mode (the phone)


class Listener:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.out = open(a.out, "a", buffering=1, encoding="utf-8")  # noqa: SIM115  (closed at exit)
        self.seen: set[str] = set()
        self.last_ts = 0
        self.room = None
        self.stop = asyncio.Event()
        self.rejoin = asyncio.Event()
        self.leave = asyncio.Event()
        self.http = httpx.AsyncClient(timeout=5.0)

    def mark(self, what: str, **kw: object) -> None:
        self.out.write(json.dumps({"mark": what, "t": now_ms(), **kw}) + "\n")

    def take(self, env: dict, via: str) -> bool:
        k = dedupe_key(env)
        if k is not None:
            if k in self.seen:
                return False
            self.seen.add(k)
        self.last_ts = max(self.last_ts, int(env.get("ts") or 0))
        self.out.write(json.dumps({"via": via, "t": now_ms(), "env": env}, ensure_ascii=False) + "\n")
        return True

    async def backlog(self, why: str) -> None:
        since = self.last_ts - 1 if self.last_ts else 0
        try:
            r = await self.http.get(f"{self.a.core}/api/rooms/{self.a.room}/events", params={"since": since})
            r.raise_for_status()
            n = sum(self.take(e, "backlog") for e in r.json())
            self.mark("backlog", why=why, since=since, new=n)
        except Exception as e:  # noqa: BLE001
            self.mark("backlog_failed", why=why, error=str(e))

    async def connect(self, why: str) -> None:
        from livekit import rtc

        room = rtc.Room()

        def on_data(p: rtc.DataPacket) -> None:
            if p.topic != EVENTS_TOPIC:
                return
            try:
                self.take(json.loads(p.data), "live")
            except ValueError:
                pass

        room.on("data_received", on_data)
        room.on("reconnecting", lambda: self.mark("reconnecting"))
        room.on("reconnected", lambda: (self.mark("reconnected"), asyncio.ensure_future(self.backlog("reconnected"))))
        room.on("disconnected", lambda reason: self.mark("disconnected", reason=str(reason)))
        await room.connect(self.a.url, self.a.token, rtc.RoomOptions(auto_subscribe=False))
        self.room = room
        self.mark("connected", why=why, identity=room.local_participant.identity)
        await self.backlog(why)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, self.stop.set)
        loop.add_signal_handler(signal.SIGTERM, self.stop.set)
        loop.add_signal_handler(signal.SIGUSR1, self.leave.set)
        loop.add_signal_handler(signal.SIGUSR2, self.rejoin.set)
        await self.connect("start")
        while not self.stop.is_set():
            waiters = {asyncio.ensure_future(e.wait()) for e in (self.stop, self.leave, self.rejoin)}
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
            for w in waiters:
                w.cancel()
            if self.leave.is_set():
                self.leave.clear()
                if self.room is not None:
                    await self.room.disconnect()
                    self.room = None
                    self.mark("left")
            if self.rejoin.is_set():
                self.rejoin.clear()
                if self.room is None:
                    await self.connect("rejoin")
        if self.room is not None:
            await self.room.disconnect()
        self.mark("stopped")
        await self.http.aclose()


# ---------------------------------------------------------------- run mode (the orchestrator)


def join(core: str, room: str, role: str, lang: str) -> dict:
    r = httpx.post(f"{core}/api/join", json={"room": room, "role": role, "lang": lang, "device": "bot",
                                             "consent": True}, timeout=5)
    r.raise_for_status()
    return r.json()


def spawn_listener(a: argparse.Namespace, room: str, token: str, out: Path) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, __file__, "listen", "--core", a.core, "--url", a.url, "--room", room,
                             "--token", token, "--out", str(out)])


def spawn_speaker(a: argparse.Namespace, room: str, token: str, log: Path, repeat: int) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, str(BOT), "--room", room, "--role", "a", "--lang", "de", "--token", token,
                             "--url", a.url, "--wav", a.wav, "--repeat", str(repeat), "--every", str(a.every),
                             "--delay", "1", "--tail", "4"], stdout=open(log, "a"), stderr=subprocess.STDOUT)


def read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def utt_no(u: str | None) -> int:
    try:
        return int(str(u).split("-")[1])
    except (IndexError, ValueError):
        return -1


def run(a: argparse.Namespace) -> int:
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    core = a.core
    room = a.room or httpx.post(f"{core}/api/rooms", json={"mode": "sprechzimmer"}, timeout=5).json()["room"]
    ja, jb = join(core, room, "a", "de"), join(core, room, "b", "ar")
    stamp = time.strftime("%H%M%S")
    lst_out = out_dir / f"{room}-{a.scenario}-{stamp}-b.jsonl"
    spk_log = out_dir / f"{room}-{a.scenario}-{stamp}-a.log"
    print(f"room {room}  a={ja['identity']}  b={jb['identity']}  scenario={a.scenario} gap={a.gap}s")
    total = a.warmup + a.gap + a.after
    repeat = max(2, int(total // a.every) + 1)
    b = spawn_listener(a, room, jb["token"], lst_out)
    time.sleep(1.5)
    spk = spawn_speaker(a, room, ja["token"], spk_log, repeat)
    session0 = httpx.get(f"{core}/api/rooms/{room}", timeout=5).json()["state"]["session_id"]

    time.sleep(a.warmup)
    t_drop = now_ms()
    print(f"+{a.warmup:.0f}s drop ({a.scenario})")
    if a.scenario == "stop":
        os.kill(b.pid, signal.SIGSTOP)
        time.sleep(a.gap)
        os.kill(b.pid, signal.SIGCONT)
    elif a.scenario == "kill":
        b.kill()
        b.wait()
        time.sleep(a.gap)
        b = spawn_listener(a, room, jb["token"], lst_out)
    elif a.scenario == "leave":
        os.kill(b.pid, signal.SIGUSR1)
        time.sleep(a.gap)
        os.kill(b.pid, signal.SIGUSR2)
    elif a.scenario == "empty":
        spk.send_signal(signal.SIGINT)
        spk.wait(timeout=10)
        os.kill(b.pid, signal.SIGUSR1)
        time.sleep(a.gap)
        os.kill(b.pid, signal.SIGUSR2)
        spk = spawn_speaker(a, room, ja["token"], spk_log, max(2, int(a.after // a.every)))
    t_back = now_ms()
    print(f"+{a.warmup + a.gap:.0f}s back")
    time.sleep(a.after)
    spk.send_signal(signal.SIGINT)
    try:
        spk.wait(timeout=10)
    except subprocess.TimeoutExpired:
        spk.kill()
    time.sleep(a.drain)
    b.send_signal(signal.SIGINT)
    try:
        b.wait(timeout=10)
    except subprocess.TimeoutExpired:
        b.kill()

    # ---------------------------------------------------------------- evaluate
    truth = httpx.get(f"{core}/api/rooms/{room}/events", timeout=5).json()
    info = httpx.get(f"{core}/api/rooms/{room}", timeout=5).json()
    finals = {e["utt"]: e["ts"] for e in truth if e["type"] == "translation.final" and e["data"]["target_lang"] == "ar"}
    spoken = sorted({e["utt"] for e in truth if e["type"] == "transcript.final" and e["data"]["speaker"] == ja["identity"]},
                    key=utt_no)
    untranslated = [u for u in spoken if u not in finals]  # b's language was not a target then
    rows = read_jsonl(lst_out)
    got: dict[str, str] = {}
    for r in rows:
        env = r.get("env")
        if env and env["type"] == "translation.final" and env["data"].get("target_lang") == "ar":
            got.setdefault(env["utt"], r["via"])
    marks = [r for r in rows if "mark" in r]
    missing = sorted(set(finals) - set(got), key=utt_no)
    in_gap = sorted((u for u, ts in finals.items() if t_drop <= ts <= t_back + 3000), key=utt_no)
    starts = [e["utt"] for e in truth if e["type"] == "utterance.start"]
    monotonic = all(utt_no(x) < utt_no(y) for x, y in itertools.pairwise(starts))
    left = [e for e in truth if e["type"] == "participant.left" and e["data"]["id"] == jb["identity"]]
    joined = [e for e in truth if e["type"] == "participant.joined" and e["data"]["id"] == jb["identity"]]

    def rel(ts: int) -> str:
        return f"{(ts - t_drop) / 1000:+.1f}s"

    print(f"\nscenario {a.scenario}, gap {a.gap:.0f} s, room {room}")
    print(f"  turns spoken by a                : {len(spoken)}  {spoken}")
    print(f"  never translated for b (ar)      : {len(untranslated)}  {untranslated}")
    print(f"  turns (ar finals) in the session : {len(finals)}  {sorted(finals, key=utt_no)}")
    print(f"  turns finished during the gap    : {len(in_gap)}  {in_gap}")
    print("  listener got them via            : "
          + ", ".join(f"{u}={got.get(u, 'MISSING')}" for u in sorted(finals, key=utt_no)))
    print(f"  missing at the end               : {len(missing)}  {missing}")
    print("  listener link marks              : "
          + ", ".join(f"{m['mark']}{'(' + m.get('why', '') + ')' if m.get('why') else ''}@{rel(m['t'])}"
                      + (f" new={m['new']}" if 'new' in m else "") for m in marks))
    print("  agent saw b leave / join         : "
          + (", ".join(f"left@{rel(e['ts'])}" for e in left) or "never left") + "; "
          + ", ".join(f"joined@{rel(e['ts'])}" for e in joined))
    session1 = (info.get("state") or {}).get("session_id")
    print(f"  session kept                     : {session0 == session1} ({session0})")
    print(f"  agent linked at the end          : {info.get('agent_connected')}")
    print(f"  utterance ids increasing         : {monotonic}  {starts}")
    print(f"  files                            : {lst_out}  {spk_log}")
    return 0 if not missing and not untranslated and monotonic and session0 == session1 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="?", choices=["run", "listen"], default="run")
    ap.add_argument("--core", default=os.environ.get("RAUM_CORE", "http://127.0.0.1:8000"))
    ap.add_argument("--url", default=os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"), help="LiveKit")
    ap.add_argument("--room", help="room id (default: a new sprechzimmer room)")
    ap.add_argument("--scenario", choices=["stop", "kill", "leave", "empty"], default="stop")
    ap.add_argument("--gap", type=float, default=10.0, help="seconds the phone is gone")
    ap.add_argument("--warmup", type=float, default=14.0)
    ap.add_argument("--after", type=float, default=20.0, help="seconds of conversation after the gap")
    ap.add_argument("--drain", type=float, default=5.0)
    ap.add_argument("--every", type=float, default=6.0, help="seconds between the speaker's utterances")
    ap.add_argument("--wav", help="16-bit PCM speech for the speaker (run mode)")
    ap.add_argument("--out-dir", default=str(HERE.parents[1] / "eval" / "out" / "c12"))
    ap.add_argument("--token")
    ap.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "listen":
        asyncio.run(Listener(a).run())
        return 0
    if not a.wav:
        ap.error("--wav is required")
    return run(a)


if __name__ == "__main__":
    sys.exit(main())
