#!/bin/bash
# Show container state, health JSON, guard state and the last log lines (default 15). PORT=8091: the second instance.
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
PORT=${PORT:-8090}
if [ "$PORT" = 8090 ]; then NAME=raum-tts-svc; SUF=; else NAME=raum-tts-svc-$PORT; SUF=-$PORT; fi
docker ps -a --filter name=^$NAME$ --format '{{.Names}}  {{.Status}}'
curl -s -m 3 http://127.0.0.1:$PORT/health || echo "no answer on 127.0.0.1:$PORT"
echo
if [ -f $R/guard$SUF.pid ] && kill -0 $(cat $R/guard$SUF.pid) 2>/dev/null; then echo "guard running (pid $(cat $R/guard$SUF.pid))"; else echo "guard not running"; fi
tail -n 3 $R/guard.log 2>/dev/null
docker logs --tail ${1:-15} $NAME 2>&1 | grep -v "^$"
