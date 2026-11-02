#!/bin/bash
# Start the RAUM TTS service (Chatterbox Multilingual V3) on 127.0.0.1:8090 in container raum-tts-svc.
# Footprint: ~6 GB of unified memory (about 2.6 GB GPU allocations + 2.5 GB host-side CUDA/kernel/runtime memory).
# Refuses to start unless 14 GB is available, so >= 8 GB stays free; a guard (guard.sh) stops it if free memory sinks below 8 GB.
set -e
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
NAME=raum-tts-svc
IMG=nvcr.io/nvidia/pytorch:26.07-py3
if docker ps --format '{{.Names}}' | grep -qx $NAME; then echo "$NAME already running"; exit 0; fi
docker rm -f $NAME >/dev/null 2>&1 || true
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt "${MIN_AVAIL_MB:-14000}" ]; then
  echo "only ${avail} MB available; need ${MIN_AVAIL_MB:-14000} MB to start and still leave 8 GB free (override: MIN_AVAIL_MB=...)"; exit 1
fi
docker run -d --name $NAME --gpus all --network host --memory 8g --memory-swap 8g --shm-size 1g \
  -v $R:/work:ro -e HF_HOME=/work/hf-cache -e HF_HUB_OFFLINE=1 -e NUMBA_CACHE_DIR=/tmp/numba \
  -e PYTHONDONTWRITEBYTECODE=1 -e MALLOC_ARENA_MAX=2 -e MALLOC_MMAP_THRESHOLD_=131072 -e MALLOC_TRIM_THRESHOLD_=131072 \
  -e RAUM_TTS_HOST=127.0.0.1 -e RAUM_TTS_PORT=8090 \
  $IMG /work/venvs/cb/bin/python -u /work/src/server.py > /dev/null
echo -n "starting $NAME (model load + warm-up, ~45 s)"
for i in $(seq 1 120); do
  if curl -sf -m 2 http://127.0.0.1:8090/health > /dev/null; then
    echo; curl -s http://127.0.0.1:8090/health; echo
    if [ "${NO_GUARD:-0}" != "1" ]; then
      nohup $R/guard.sh > /dev/null 2>&1 < /dev/null &
      echo $! > $R/guard.pid; echo "memory guard pid $(cat $R/guard.pid) (floor ${FLOOR_MB:-8192} MB, log guard.log)"
    fi
    exit 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx $NAME; then echo; echo "container exited:"; docker logs --tail 30 $NAME; exit 1; fi
  echo -n .; sleep 2
done
echo; echo "not healthy after 240 s; see: docker logs $NAME"; exit 1
