#!/bin/bash
# Start the RAUM TTS service (Chatterbox Multilingual V3) on 127.0.0.1:8090 in container raum-tts-svc.
# A second instance (v1.4, RAUM's tts options.urls): PORT=8091 ./start.sh -> container raum-tts-svc-8091, guard-8091.pid.
# Footprint: ~6 GB of unified memory (about 2.6 GB GPU allocations + 2.5 GB host-side CUDA/kernel/runtime memory).
# Refuses to start unless 14 GB is available, so >= 8 GB stays free; a guard (guard.sh) stops it if free memory sinks below 8 GB.
set -e
R=$HOME/Desktop/HackathonRaum/runtime/raum-tts
PORT=${PORT:-8090}
if [ "$PORT" = 8090 ]; then NAME=raum-tts-svc; SUF=; else NAME=raum-tts-svc-$PORT; SUF=-$PORT; fi
IMG=nvcr.io/nvidia/pytorch:26.07-py3
if docker ps --format '{{.Names}}' | grep -qx $NAME; then echo "$NAME already running"; exit 0; fi
docker rm -f $NAME >/dev/null 2>&1 || true
# The zh segmenter (spacy-pkuseg) downloads its 34 MB model from github.com into ~/.pkuseg at every start of a fresh
# container (third-party bytes). Mounted from $R/pkuseg it is fetched at most once. Fill it once from a running
# container: docker cp raum-tts-svc:/root/.pkuseg/. $R/pkuseg/
mkdir -p $R/pkuseg
if [ ! -d $R/pkuseg/spacy_ontonotes ]; then
  echo "warning: $R/pkuseg is empty; the zh segmenter will download it once (fill: docker cp raum-tts-svc:/root/.pkuseg/. $R/pkuseg/)"
fi
avail=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
if [ "$avail" -lt "${MIN_AVAIL_MB:-14000}" ]; then
  echo "only ${avail} MB available; need ${MIN_AVAIL_MB:-14000} MB to start and still leave 8 GB free (override: MIN_AVAIL_MB=...)"; exit 1
fi
docker run -d --name $NAME --gpus all --network host --memory 8g --memory-swap 8g --shm-size 1g \
  -v $R:/work:ro -v $R/pkuseg:/root/.pkuseg -e HF_HOME=/work/hf-cache -e HF_HUB_OFFLINE=1 -e NUMBA_CACHE_DIR=/tmp/numba \
  -e PYTHONDONTWRITEBYTECODE=1 -e MALLOC_ARENA_MAX=2 -e MALLOC_MMAP_THRESHOLD_=131072 -e MALLOC_TRIM_THRESHOLD_=131072 \
  -e RAUM_TTS_HOST=127.0.0.1 -e RAUM_TTS_PORT=$PORT \
  $IMG /work/venvs/cb/bin/python -u /work/src/server.py > /dev/null
echo -n "starting $NAME on :$PORT (model load + warm-up, ~45 s)"
for i in $(seq 1 120); do
  if curl -sf -m 2 http://127.0.0.1:$PORT/health > /dev/null; then
    echo; curl -s http://127.0.0.1:$PORT/health; echo
    if [ "${NO_GUARD:-0}" != "1" ]; then
      NAME=$NAME nohup $R/guard.sh > /dev/null 2>&1 < /dev/null &
      echo $! > $R/guard$SUF.pid; echo "memory guard pid $(cat $R/guard$SUF.pid) (floor ${FLOOR_MB:-8192} MB, log guard.log)"
    fi
    exit 0
  fi
  if ! docker ps --format '{{.Names}}' | grep -qx $NAME; then echo; echo "container exited:"; docker logs --tail 30 $NAME; exit 1; fi
  echo -n .; sleep 2
done
echo; echo "not healthy after 240 s; see: docker logs $NAME"; exit 1
