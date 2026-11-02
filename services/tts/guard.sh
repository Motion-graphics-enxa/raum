#!/bin/bash
# Memory guard for raum-tts-svc: if host MemAvailable stays below the floor (default 8 GiB) for ~2 s,
# stop raum-tts-svc (and only that container) so the kernel OOM killer never has to pick another team's process.
# Started by start.sh (NAME=<container>: one guard per instance), stopped by stop.sh.
# Log: ~/Desktop/HackathonRaum/runtime/raum-tts/guard.log
FLOOR_MB=${FLOOR_MB:-8192}
NAME=${NAME:-raum-tts-svc}
LOG=$HOME/Desktop/HackathonRaum/runtime/raum-tts/guard.log
low=0
echo "$(date '+%F %T') guard up for $NAME, floor ${FLOOR_MB} MB" >> $LOG
while docker ps --format '{{.Names}}' | grep -qx $NAME; do
  a=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  if [ "$a" -lt "$FLOOR_MB" ]; then low=$((low+1)); else low=0; fi
  if [ "$low" -ge 4 ]; then
    echo "$(date '+%F %T') MemAvailable ${a} MB < ${FLOOR_MB} MB for 2 s: stopping $NAME" >> $LOG
    docker stop -t 2 $NAME >> $LOG 2>&1
    break
  fi
  sleep 0.5
done
echo "$(date '+%F %T') guard exit ($NAME)" >> $LOG
