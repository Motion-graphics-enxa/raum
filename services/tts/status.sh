#!/bin/bash
# Show container state, health JSON, guard state and the last log lines (default 15).
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
docker ps -a --filter name=^raum-tts-svc$ --format '{{.Names}}  {{.Status}}'
curl -s -m 3 http://127.0.0.1:8090/health || echo "no answer on 127.0.0.1:8090"
echo
if [ -f $R/guard.pid ] && kill -0 $(cat $R/guard.pid) 2>/dev/null; then echo "guard running (pid $(cat $R/guard.pid))"; else echo "guard not running"; fi
tail -n 3 $R/guard.log 2>/dev/null
docker logs --tail ${1:-15} raum-tts-svc 2>&1 | grep -v "^$"
