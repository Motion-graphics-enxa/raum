#!/bin/bash
# run.sh ROOM LANG WAV : listener (records tts-*) + speaker bot, then gap analysis + tts log
ROOM=$1; L=$2; WAV=$3; DUR=${4:-34}
cd ~/Desktop/HackathonRaum/raum/core && export PATH=$HOME/.local/bin:$PATH
T0=$(date -u +%Y-%m-%dT%H:%M:%S)
(timeout $((DUR+8)) uv run python /tmp/raum-gaps/rec.py --room $ROOM --role b --lang $L --duration $DUR --json --out /tmp/raum-gaps/$ROOM > /tmp/raum-gaps/$ROOM.out 2>&1 &)
sleep 4
timeout $((DUR-6)) uv run python ../scripts/bot.py --room $ROOM --role a --lang de --wav $WAV --tail 10 > /tmp/raum-gaps/a$ROOM.out 2>&1
sleep $((DUR-14))
while pgrep -f "rec.py --room $ROOM" >/dev/null; do sleep 1; done
grep -h '"type": "translation.final"\|"type": "transcript.final"' /tmp/raum-gaps/$ROOM.out | python3 -c "
import sys,json
for l in sys.stdin:
    e=json.loads(l); print(e['type'], e.get('utt'), e['data'].get('text'))"
python3 -c "
import json; d=json.load(open('/tmp/raum-gaps/$ROOM.json'))
for e in d['events']:
    if e['type']=='pipeline.timing':
        x=e['data']; st=x.get('stages',{})
        print('timing', e['utt'], {k:x.get(k) for k in ('mt_first_ms','tts_first_ms','tts_publish_first_ms','voice_clauses')})
        for k in ('tts','tts.bg','voice','voice.live','voice.ahead','voice.spec','spec.tts.bg','spec.voice.spec','echo.asr','asr.partial'):
            if k in st: print('   ', k, st[k])
    if e['type'] in ('voice.state',): print(round(e['t'],3), e['type'], e['data'])
print('tts track arrivals: first %.3f last %.3f' % (d['arrivals'][0][0], d['arrivals'][-1][0]) if d['arrivals'] else 'no audio')
"
uv run python /tmp/raum-gaps/gaps.py /tmp/raum-gaps/$ROOM.wav --json /tmp/raum-gaps/$ROOM.json
docker logs --since $T0 raum-tts-svc 2>&1 | grep -v "GET /health\|Mel Inference\|POST /tts"
