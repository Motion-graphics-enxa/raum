#!/bin/bash
# Stop the RAUM TTS service and its memory guard. Voices live in memory only, so they are gone after this.
# PORT=8091 ./stop.sh stops the second instance (raum-tts-svc-8091).
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
PORT=${PORT:-8090}
if [ "$PORT" = 8090 ]; then NAME=raum-tts-svc; SUF=; else NAME=raum-tts-svc-$PORT; SUF=-$PORT; fi
if [ -f $R/guard$SUF.pid ]; then kill $(cat $R/guard$SUF.pid) 2>/dev/null; rm -f $R/guard$SUF.pid; fi
docker stop -t 10 $NAME >/dev/null 2>&1 && docker rm $NAME >/dev/null 2>&1 && echo "$NAME stopped" || { docker rm $NAME >/dev/null 2>&1; echo "$NAME not running"; }
