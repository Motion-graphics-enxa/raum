"""Replay a scripted conversation into a room through raum-core's agent link.

Exercises every screen (Talk, Table, Summary, burn) without LiveKit audio, models or the agent:
core persists the events, mirrors them on /api/events/{room} and the phones show them.
Speaks as the participants that already joined (role a / role b), or as stand-ins.

The facts go the pipeline's own way (the real fact guard, then ``RoomPipeline._fact_flags`` /
``_settle_facts`` with the scripted checker badge, raum.pipeline.readback): green facts become a "noted" chip
on the doctor's screen, an amber check or a guard mismatch a read-back to the patient, and the patient's
next short yes/no answers it (a ``confirm`` via voice), as on the box.

    cd core && uv run python ../scripts/replay.py --room sz-1234 [--core http://127.0.0.1:8000] [--speed 1]
    add --end to finish with End (summary), --burn to burn afterwards
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request
from types import SimpleNamespace

import websockets

from raum.config import get_config
from raum.events import ParticipantInfo, make_event
from raum.link import AGENT_LINK_PATH, KEY_HEADER
from raum.modes import get_mode
from raum.pipeline import readback
from raum.pipeline.engines import build_engines
from raum.pipeline.room import RoomPipeline
from raum.verify import guard

# (role, src_lang, src, {tgt_lang: text}, checker badge)
SCRIPT = {
    "sprechzimmer": [
        ("a", "de", "Guten Tag. Was führt Sie heute zu mir?",
         {"ar": "مرحبًا. ما الذي أتى بك إليّ اليوم؟", "en": "Good afternoon. What brings you to me today?",
          "tr": "İyi günler. Bugün sizi buraya ne getirdi?", "ru": "Добрый день. Что привело вас ко мне сегодня?"},
         "green"),
        # green: noted on the doctor's screen, nobody asked
        ("b", "ru", "Я принимаю 500 мг метформина два раза в день, и у меня аллергия на пенициллин.",
         {"de": "Ich nehme zweimal täglich 500 mg Metformin, und ich bin allergisch gegen Penicillin."}, "green"),
        # the checker is unsure (amber): read back to the patient
        ("a", "de", "Gut. Dann bleiben wir bei 500 mg Metformin, zweimal täglich nach dem Essen.",
         {"ar": "حسنًا. سنبقى إذن على 500 ملغ من ميتفورمين، مرتين يوميًا بعد الأكل.",
          "en": "Good. Then we stay at 500 mg of Metformin, twice a day after meals.",
          "tr": "Peki. O zaman günde iki kez yemekten sonra 500 mg Metformin ile devam ediyoruz.",
          "ru": "Хорошо. Тогда остаёмся на Метформине по 500 мг два раза в день после еды."}, "amber"),
        # not an answer: the dose stays unconfirmed, nobody asks again
        ("b", "ru", "Спасибо. Можно мне пить кофе?", {"de": "Danke. Darf ich Kaffee trinken?"}, "green"),
        # the translation says the 13th: a guard mismatch, read back at once
        ("a", "de", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
         {"ar": "عودي يوم الخميس، 13 نوفمبر، الساعة 9.", "en": "Come back on Thursday, 13 November, at 9 o'clock.",
          "tr": "13 Kasım Perşembe günü saat 9'da tekrar gelin.", "ru": "Приходите снова в четверг, 13 ноября, в 9 часов."},
         "red"),
        # the patient's spoken yes confirms the date
        ("b", "ru", "Да.", {"de": "Ja."}, "green"),
    ],
}
SIMILARITY = {"green": 0.93, "amber": 0.84, "red": 0.71}


def pipeline(room: str, mode_id: str, emit, who: dict[str, str], langs: dict[str, str | None]) -> RoomPipeline:
    """The room pipeline's flag logic on mock engines (nothing is transcribed; the mock voice has no sink, so a
    read-back is "spoken" into nothing and the phones show it as spoken)."""
    cfg = get_config().model_copy(deep=True)
    for stage in ("asr", "translate", "verify", "embed", "tts"):
        getattr(cfg.models, stage).engine = "mock"
    p = RoomPipeline(room, get_mode(mode_id), build_engines(cfg, seed=0), emit, cfg)  # mock voice: no sink
    p.set_participants([ParticipantInfo(id=who[r], role=r, lang=langs[r] or ("de" if r == "a" else "ru"))
                        for r in ("a", "b")])
    return p


def participants(core: str, room: str) -> dict[str, dict]:
    try:
        with urllib.request.urlopen(f"{core}/api/rooms/{room}", timeout=3) as r:
            info = json.load(r)
        ps = (info.get("state") or {}).get("participants") or []
        return {p["role"]: p for p in ps if p.get("role") in ("a", "b")}
    except Exception:
        return {}


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--room", required=True)
    ap.add_argument("--core", default="http://127.0.0.1:8000")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("--end", action="store_true")
    ap.add_argument("--burn", action="store_true")
    args = ap.parse_args()
    cfg = get_config()
    room = args.room
    mode = {"sz": "sprechzimmer"}.get(room.split("-")[0], "sprechzimmer")
    sp = 1 / max(0.1, args.speed)
    ps = participants(args.core, room)
    who = {r: ps.get(r, {}).get("id", f"{r}-replay") for r in ("a", "b")}
    langs = {r: ps.get(r, {}).get("lang") for r in ("a", "b")}
    print("speaking as", who, "langs", langs)

    url = args.core.replace("http", "ws", 1) + AGENT_LINK_PATH.format(room=room)
    async with websockets.connect(url, additional_headers={KEY_HEADER: cfg.network.internal_key}) as ws:
        await ws.send(json.dumps({"kind": "hello", "room": room, "agent": "replay"}))

        async def emit(etype: str, data: dict, utt: str | None = None) -> None:
            env = make_event(etype, room, data, utt=utt)
            await ws.send(json.dumps({"kind": "event", "event": env.model_dump(mode="json")}))

        sent: list[dict] = []

        async def send(env) -> None:  # the pipeline's own flags
            if env.type == "flag":
                sent.append(env.data)
            await ws.send(json.dumps({"kind": "event", "event": env.model_dump(mode="json")}))

        p = pipeline(room, mode, send, who, langs)
        asked: dict | None = None  # the read-back the patient's next utterance may answer
        base = int(time.time()) % 10000
        for i, (role, src_lang, src, trs, badge) in enumerate(SCRIPT[mode], start=1):
            utt = f"u-{base:04d}{i}"
            speaker = who[role]
            src_lang = langs[role] if role == "a" and langs[role] == "de" else src_lang
            listener = "b" if role == "a" else "a"
            tgt = langs[listener] or ("de" if listener == "a" else "ru")
            tr = trs.get(tgt) or trs.get("en") or next(iter(trs.values()))
            await emit("utterance.start", {"speaker": speaker, "lang": src_lang}, utt)
            words = src.split()
            for k in range(1, len(words) + 1, 2):
                await asyncio.sleep(0.32 * sp)
                await emit("transcript.partial", {"speaker": speaker, "lang": src_lang,
                                                  "text": " ".join(words[:k]), "seq": k}, utt)
            await asyncio.sleep(0.34 * sp)
            await emit("utterance.end", {"speaker": speaker, "lang": src_lang}, utt)
            await asyncio.sleep(0.22 * sp)
            await emit("transcript.final", {"speaker": speaker, "lang": src_lang, "text": src, "asr_conf": 0.93}, utt)
            await asyncio.sleep(0.2 * sp)
            for n, w in enumerate(tr.split(" ")):
                await emit("translation.delta", {"target_lang": tgt, "delta": (" " if n else "") + w, "seq": n}, utt)
                await asyncio.sleep(0.045 * sp)
            await emit("translation.final", {"target_lang": tgt, "text": tr}, utt)
            rec = SimpleNamespace(utt=utt, role=role, lang=src_lang, text=src, conf=0.93)
            if asked is not None and role == asked["role"]:  # the asked person's next utterance
                yes = readback.yes_no(src, src_lang)
                if yes is not None:
                    await emit("confirm", {"by": role, "accepted": yes, "flag_id": asked["id"], "via": "voice",
                                           "answer_utt": utt}, asked["utt"])
                asked = None
            res = guard.check(src, src_lang, tr, tgt, kinds=p.mode.guard.kinds, confirm_on=p.mode.guard.confirm_on,
                              glossary=p.glossary)
            await emit("verify.facts", {"target_lang": tgt, "red": res.red, "ms": round(res.ms, 2),
                                        "facts": [f.model_dump() for f in res.facts]}, utt)
            before = len(sent)
            flagged = await p._fact_flags(rec, tgt, res)  # a guard mismatch: read back at once
            await emit("pipeline.timing", {"vad_ms": 340, "asr_ms": 210, "mt_first_ms": 780, "mt_full_ms": 1190,
                                           "tts_first_ms": None, "check_ms": None, "target_lang": tgt,
                                           "models": {"asr": "mock-whisper", "translate": "mock-gemma",
                                                      "verify": "mock-gpt-oss"}}, utt)
            await asyncio.sleep(0.9 * sp)
            badge = "red" if res.red else badge
            await emit("verify.result", {"target_lang": tgt, "similarity": SIMILARITY[badge], "badge": badge,
                                         "reasons": [f"fact mismatch ({f.kind}): {f.src} -> {f.tgt}"
                                                     for f in res.facts if not f.match],
                                         "back_translation": src}, utt)
            if not flagged:  # the checker decides: noted (green) or read back (amber / red)
                await p._settle_facts(rec, tgt, res, badge)
            for f in sent[before:]:
                if f.get("needs_confirm") and f.get("status") == "unconfirmed":
                    asked = {"id": f["id"], "utt": utt, "role": f.get("confirm_by") or listener}
            await emit("pipeline.timing", {"vad_ms": 340, "asr_ms": 210, "mt_first_ms": 780, "mt_full_ms": 1190,
                                           "tts_first_ms": None, "check_ms": 1120, "target_lang": tgt,
                                           "models": {"asr": "mock-whisper", "translate": "mock-gemma",
                                                      "verify": "mock-gpt-oss"}}, utt)
            await asyncio.sleep(2.2 * sp)

    if args.end or args.burn:
        req = urllib.request.Request(f"{args.core}/api/rooms/{room}/end", method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            print("end:", r.status)
    if args.burn:
        await asyncio.sleep(3 * sp)
        req = urllib.request.Request(f"{args.core}/api/rooms/{room}/burn", method="POST")
        with urllib.request.urlopen(req, timeout=15) as r:
            print("burn:", json.load(r))


if __name__ == "__main__":
    asyncio.run(main())
