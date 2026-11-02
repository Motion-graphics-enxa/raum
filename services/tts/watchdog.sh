#!/bin/bash
# Restart raum-tts-svc when it is stuck. RAUM falls back to Piper meanwhile. Checks every 15 s:
#  1. /health does not answer 3x in a row, or
#  2. /health says busy for > 30 s (and longer than that job's own wall-clock cap, busy_limit_s, + 5 s) AND a probe
#     POST /tts with a short German sentence delivers no audio within 10 s.
# Leaves the service alone when it was stopped on purpose (guard.sh / stop.sh).
# PORT=8091 ./watchdog.sh watches the second instance (raum-tts-svc-8091); run one watchdog per instance.
D=$HOME/Desktop/HackathonRaum/runtime/raum-tts; miss=0
PORT=${PORT:-8090}
if [ "$PORT" = 8090 ]; then NAME=raum-tts-svc; else NAME=raum-tts-svc-$PORT; fi
restart() {
  echo "$(date "+%F %T") $1: restarting $NAME" >> $D/watchdog.log
  (cd $D && PORT=$PORT ./stop.sh && PORT=$PORT ./start.sh) >> $D/watchdog.log 2>&1; miss=0
}
while true; do
  sleep 15
  docker ps --format "{{.Names}}" | grep -qx $NAME || { miss=0; continue; }
  h=$(curl -s -m 5 127.0.0.1:$PORT/health)
  if [ -z "$h" ]; then
    miss=$((miss+1)); [ $miss -ge 3 ] && restart "health failed 3x"; continue
  fi
  miss=0
  read busy_for limit < <(echo "$h" | python3 -c 'import json,sys
d=json.load(sys.stdin); print(int(d.get("busy_for_s", 0)) if d.get("busy") else 0, int(d.get("busy_limit_s", 0)))' 2>/dev/null || echo "0 0")
  if [ "${busy_for:-0}" -gt 30 ] && [ "${busy_for:-0}" -gt $(( ${limit:-0} + 5 )) ]; then
    n=$(curl -s -m 10 -o /dev/null -w "%{size_download}" -H "Content-Type: application/json" \
        -d '{"text":"Guten Tag.","lang":"de","sample_rate":24000}' 127.0.0.1:$PORT/tts)
    if [ "${n:-0}" -lt 4800 ]; then   # 0.1 s of audio; a 503 answer is ~100 bytes
      restart "busy ${busy_for}s (cap ${limit}s) and probe /tts gave no audio within 10 s"
    fi
  fi
done
