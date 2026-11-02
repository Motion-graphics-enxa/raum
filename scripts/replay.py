"""Replay a scripted conversation into a room through raum-core's agent link.

Exercises every screen (Talk, Table, Summary, burn) without LiveKit audio, models or the agent:
core persists the events, mirrors them on /api/events/{room} and the phones show them.
Speaks as the participants that already joined (role a / role b), or as stand-ins.

    cd core && uv run python ../scripts/replay.py --room sz-1234 [--core http://127.0.0.1:8000] [--speed 1]
    add --end to finish with End (summary), --burn to burn afterwards
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import urllib.request

import websockets

from raum.config import get_config
from raum.events import make_event
from raum.link import AGENT_LINK_PATH, KEY_HEADER

# (role, src_lang, src, {tgt_lang: text}, facts per tgt [(kind, src, tgt, match)], flags)
SCRIPT = {
    "sprechzimmer": [
        ("a", "de", "Guten Tag. Was führt Sie heute zu mir?",
         {"ar": "مرحبًا. ما الذي أتى بك إليّ اليوم؟", "en": "Good afternoon. What brings you to me today?",
          "tr": "İyi günler. Bugün sizi buraya ne getirdi?", "ru": "Добрый день. Что привело вас ко мне сегодня?"},
         [], []),
        ("b", "ar", "أتناول ٥٠٠ ملغ من الميتفورمين مرتين يوميًا، وعندي حساسية من البنسلين.",
         {"de": "Ich nehme zweimal täglich 500 mg Metformin, und ich bin allergisch gegen Penicillin."},
         [("dose", "500 mg", "500 mg", True), ("frequency", "2×/Tag", "2×/Tag", True),
          ("medication", "Metformin", "Metformin", True), ("allergy", "Penicillin", "Penicillin", True)],
         [("dose", "500 mg Metformin, zweimal täglich. Richtig?"), ("allergy", "Allergie gegen Penicillin. Richtig?")]),
        ("a", "de", "Gut. Dann bleiben wir bei 500 mg, zweimal täglich nach dem Essen.",
         {"ar": "حسنًا. سنبقى إذن على ٥٠٠ ملغ، مرتين يوميًا بعد الأكل.", "en": "Good. Then we stay at 500 mg, twice a day after meals.",
          "tr": "Peki. O zaman günde iki kez yemekten sonra 500 mg ile devam ediyoruz.", "ru": "Хорошо. Тогда остаёмся на 500 мг два раза в день после еды."},
         [("dose", "500 mg", "500 mg", True), ("frequency", "2×/Tag", "2×/Tag", True)], []),
        ("a", "de", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
         {"ar": "عودي يوم الخميس ١٣ نوفمبر الساعة ٩.", "en": "Come back on Thursday, 13 November, at 9 o'clock.",
          "tr": "13 Kasım Perşembe günü saat 9'da tekrar gelin.", "ru": "Приходите снова в четверг, 13 ноября, в 9 часов."},
         [("date", "12. November", "13. November", False), ("time", "9 Uhr", "9 Uhr", True)],
         [("mismatch", None)]),
    ],
}

FLAG_TEXT = {
    "de": "{src} wurde als {tgt} übersetzt. Bitte nachfragen.",
    "en": "{src} was translated as {tgt}. Please ask again.",
    "ar": "تُرجم {src} إلى {tgt}. يرجى السؤال مرة أخرى.",
    "tr": "{src}, {tgt} olarak çevrildi. Lütfen tekrar sorun.",
    "ru": "{src} переведено как {tgt}. Пожалуйста, уточните.",
}


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

        for i, (role, src_lang, src, trs, facts, flags) in enumerate(SCRIPT[mode], start=1):
            utt = f"u-r{int(time.time()) % 10000:04d}{i}"
            speaker = who[role]
            src_lang = langs[role] if role == "a" and langs[role] == "de" else src_lang
            listener = "b" if role == "a" else "a"
            tgt = langs[listener] or ("de" if listener == "a" else "ar")
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
            red = any(not f[3] for f in facts)
            if facts:
                await emit("verify.facts", {"target_lang": tgt, "red": red, "ms": 1.4,
                                            "facts": [{"kind": k, "src": s, "tgt": t, "match": m} for k, s, t, m in facts]}, utt)
            for j, (kind, text) in enumerate(flags):
                if kind == "mismatch":
                    bad = next(f for f in facts if not f[3])
                    texts = {lg: tpl.format(src=bad[1], tgt=bad[2]) for lg, tpl in FLAG_TEXT.items()}
                    await emit("flag", {"kind": "mismatch", "text": texts.get(tgt, texts["en"]), "texts": texts,
                                        "needs_confirm": True, "id": f"{utt}-f{j}", "confirm_by": listener,
                                        "target_lang": tgt}, utt)
                else:
                    await emit("flag", {"kind": kind, "text": text, "needs_confirm": True, "id": f"{utt}-f{j}",
                                        "confirm_by": listener, "target_lang": tgt}, utt)
            await emit("pipeline.timing", {"vad_ms": 340, "asr_ms": 210, "mt_first_ms": 430, "mt_full_ms": 980,
                                           "tts_first_ms": None, "check_ms": None, "target_lang": tgt,
                                           "models": {"asr": "mock-whisper", "translate": "mock-gemma",
                                                      "verify": "mock-gpt-oss"}}, utt)
            await asyncio.sleep(0.9 * sp)
            sim = 0.71 if red else 0.93
            await emit("verify.result", {"target_lang": tgt, "similarity": sim,
                                         "badge": "red" if red else "green",
                                         "reasons": ["fact mismatch: date"] if red else [],
                                         "back_translation": src}, utt)
            await emit("pipeline.timing", {"vad_ms": 340, "asr_ms": 210, "mt_first_ms": 430, "mt_full_ms": 980,
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
