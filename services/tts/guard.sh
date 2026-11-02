#!/bin/bash
# Memory guard for raum-tts-svc: if host MemAvailable stays below the floor (default 8 GiB) for ~2 s,
# stop raum-tts-svc (and only that container) so the kernel OOM killer never has to pick another team's process.
# Started by start.sh, stopped by stop.sh. Log: ~/Desktop/HackathonRaum/runtime/raum-tts/guard.log
FLOOR_MB=${FLOOR_MB:-8192}
LOG=$HOME/Desktop/HackathonRaum/runtime/raum-tts/guard.log
low=0
echo "$(date '+%F %T') guard up, floor ${FLOOR_MB} MB" >> $LOG
while docker ps --format '{{.Names}}' | grep -qx raum-tts-svc; do
  a=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
  if [ "$a" -lt "$FLOOR_MB" ]; then low=$((low+1)); else low=0; fi
  if [ "$low" -ge 4 ]; then
    echo "$(date '+%F %T') MemAvailable ${a} MB < ${FLOOR_MB} MB for 2 s: stopping raum-tts-svc" >> $LOG
    docker stop -t 2 raum-tts-svc >> $LOG 2>&1
    break
  fi
  sleep 0.5
done
echo "$(date '+%F %T') guard exit" >> $LOG
