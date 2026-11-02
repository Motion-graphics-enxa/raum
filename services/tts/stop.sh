#!/bin/bash
# Stop the RAUM TTS service and its memory guard. Voices live in memory only, so they are gone after this.
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
if [ -f $R/guard.pid ]; then kill $(cat $R/guard.pid) 2>/dev/null; rm -f $R/guard.pid; fi
docker stop -t 10 raum-tts-svc >/dev/null 2>&1 && docker rm raum-tts-svc >/dev/null 2>&1 && echo "raum-tts-svc stopped" || { docker rm raum-tts-svc >/dev/null 2>&1; echo "raum-tts-svc not running"; }
