#!/bin/bash
# eval-gemma GPU window (tag "gemma", shared window "dia asr gemma"): smaller translators (A), cheaper checkers (B).
# Phase 1 (accuracy, parallel lanes, no lock): A translators / B checkers on Ollama, Q MetricX QE container.
#   Ollama slots (MAX_LOADED_MODELS=3): bge-m3 + at most 2 of ours; gemma4:26b is unloaded after its own run.
# Phase 2 (latency, one model at a time, each run under runtime/gpu-latency.lock, <= 3 min).
# End: gpt-oss:120b (num_ctx 8192, keep_alive -1) + gemma4:26b + bge-m3 restored and verified (EXIT trap).
# Watchdog (1 s): gpt-oss:120b loaded by someone else / bge-m3 gone / gemma4:26b gone while we did not unload it /
# "gemma" not in GPU_WINDOW / MemAvailable < 9.5 GB for 2 s -> ABORT: kill our benches + containers, unload ours.
E=$HOME/Desktop/HackathonRaum/runtime/eval-gemma
R=$HOME/Desktop/HackathonRaum/raum
WIN=$HOME/Desktop/HackathonRaum/runtime/GPU_WINDOW
LOCK=$HOME/Desktop/HackathonRaum/runtime/gpu-latency.lock
TAG=gemma
FLOOR_MB=9728
mkdir -p $E/out/pids $E/out/cands
exec >> $E/out/window.log 2>&1 < /dev/null
export PATH=$HOME/.local/bin:$PATH
source $HOME/raum-runtime/ollama-env.sh
T0=$(date +%s); DEADLINE=$((T0 + ${BUDGET:-2100}))
el() { echo $(( $(date +%s) - T0 )); }
avail() { awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo; }
models() { curl -s -m 10 127.0.0.1:11434/api/ps | python3 -c 'import json,sys; print(" ".join(sorted(m["name"] for m in json.load(sys.stdin).get("models", []))))' 2>/dev/null; }
psizes() { python3 $E/src/psizes.py 2>&1; }
say() { echo "$(date +%T) [+$(el)s] $*"; }
inwin() { grep -qw $TAG $WIN 2>/dev/null; }
aborted() { [ -f $E/out/ABORT ]; }
pulled() { ollama list 2>/dev/null | awk '{print $1}' | grep -qx "$1"; }
unload() {
  curl -s -m 30 127.0.0.1:11434/api/generate -d "{\"model\":\"$1\",\"keep_alive\":0}" > /dev/null
  for i in $(seq 1 40); do models | tr ' ' '\n' | grep -qx "$1" || return 0; sleep 0.5; done
  say "WARN: $1 still loaded after unload"
}
warm() { curl -s -m 300 127.0.0.1:11434/api/generate -d "{\"model\":\"$1\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_predict\":1,\"num_ctx\":8192}}" > /dev/null; }
gemma_up() {
  say "reloading gemma4:26b"
  curl -s -m 300 127.0.0.1:11434/api/generate -d '{"model":"gemma4:26b","prompt":"hi","stream":false,"think":false,"keep_alive":-1,"options":{"num_predict":1,"num_ctx":8192}}' > /dev/null
}
bge_up() { curl -s -m 120 127.0.0.1:11434/api/embed -d '{"model":"bge-m3","input":["warm"],"keep_alive":-1}' > /dev/null; }
cand_on() { touch "$E/out/cands/$1"; }
cand_off() { unload "$1"; rm -f "$E/out/cands/$1"; say "unloaded $1; ps: $(models)"; }
kill_ours() {
  docker kill raum-gemma-qe raum-gemma-qe-mx raum-gemma-qelat > /dev/null 2>&1
  for f in $E/out/pids/*; do [ -f "$f" ] && kill $(cat "$f") 2>/dev/null; done
  for f in $E/out/cands/*; do [ -f "$f" ] && unload "$(basename "$f")"; rm -f "$f"; done
}
restore() {
  [ -n "$WD" ] && kill $WD 2>/dev/null
  kill_ours
  for i in 1 2 3; do
    m=$(models)
    echo "$m" | grep -q gemma4:26b || gemma_up
    echo "$m" | grep -q bge-m3 || bge_up
    say "reloading gpt-oss:120b (try $i), avail $(avail) MB"
    curl -s -m 300 127.0.0.1:11434/api/generate -d '{"model":"gpt-oss:120b","prompt":"hi","stream":false,"keep_alive":-1,"options":{"num_predict":1,"num_ctx":8192}}' > /dev/null
    m=$(models); echo "$m" | grep -q gemma4:26b || { gemma_up; m=$(models); }
    echo "$m" | grep -q bge-m3 || { bge_up; m=$(models); }
    say "/api/ps: $(psizes); avail $(avail) MB"
    [ "$m" = "bge-m3:latest gemma4:26b gpt-oss:120b" ] && { say "RESTORE OK"; break; }
    sleep 5
  done
  [ "$(models)" = "bge-m3:latest gemma4:26b gpt-oss:120b" ] || say "RESTORE FAILED: $(models)"
}
finish() {
  restore
  # same-conditions latency of the current checker (resident), under the shared lock
  if ! aborted && inwin && [ "$(models)" = "bge-m3:latest gemma4:26b gpt-oss:120b" ]; then
    lat "B gpt-oss:120b back-translation (current checker)" $E/src/bench_b.py --model gpt-oss:120b --tag oss120-bt-lat --mode bt --think low --seq 30 --limit 30
    say "/api/ps after: $(psizes)"
  fi
  say "=== window end; avail $(avail) MB"
}
trap finish EXIT

# py <label> <timeout> <cmd...>: run a bench in the core venv (no lock); its PID is registered for the watchdog
py() {
  local label=$1 to=$2; shift 2
  aborted && { say "skip $label (abort)"; return 1; }
  [ $(date +%s) -gt $DEADLINE ] && { say "skip $label (deadline)"; return 1; }
  say ">> $label"
  (cd $R && exec timeout $to uv run --no-sync --project core python "$@" --deadline $DEADLINE) &
  local p=$!; echo $p > $E/out/pids/$p; wait $p; local rc=$?; rm -f $E/out/pids/$p
  say "<< $label rc=$rc; avail $(avail) MB; ps: $(psizes)"
}
# lat <label> <cmd...>: a timing run under the shared latency lock (<= 175 s inside the lock)
lat() {
  local label=$1; shift
  aborted && { say "skip $label (abort)"; return 1; }
  say ">> [lock wait] $label"
  (cd $R && exec flock -w 600 $LOCK timeout 175 uv run --no-sync --project core python "$@") &
  local p=$!; echo $p > $E/out/pids/$p; wait $p; local rc=$?; rm -f $E/out/pids/$p
  say "<< $label rc=$rc"
}
qe() {  # qe <name> <lock 0|1> <env...>
  local name=$1 locked=$2; shift 2
  aborted && return 1
  say ">> metrics container $name (lock $locked)"
  local pre=""; [ "$locked" = 1 ] && pre="flock -w 600 $LOCK timeout 175"
  docker rm -f $name > /dev/null 2>&1
  $pre docker run --rm --name $name --gpus all --network none --memory 24g --memory-swap 24g --shm-size 1g \
    -v $E:/work -w /tmp -e HF_HOME=/work/hf -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e TQDM_DISABLE=1 \
    -e PYTHONDONTWRITEBYTECODE=1 -e OMP_NUM_THREADS=4 "$@" \
    nvcr.io/nvidia/pytorch:26.07-py3 /work/venvs/qe/bin/python -u /work/src/qe.py 2>&1 \
    | grep --line-buffered -E "^[0-9]{2}:[0-9]{2}:[0-9]{2} |Error|error|Traceback|Killed" | grep --line-buffered -v -E "W1102|UserWarning"
  docker ps --format "{{.Names}}" | grep -qx $name && { say "container $name still running after timeout: killing"; docker kill $name > /dev/null 2>&1; }
  say "<< metrics container $name; avail $(avail) MB"
}
A=$E/src/bench_a.py; B=$E/src/bench_b.py

say "=== window start; GPU_WINDOW='$(cat $WIN 2>/dev/null)'; ps: $(psizes); avail $(avail) MB"
rm -f $E/out/ABORT $E/out/GEMMA_OFF $E/out/A_DONE $E/out/Q_DONE $E/out/Q3MODEL $E/out/pids/* $E/out/cands/*
inwin || { say "GPU_WINDOW has no $TAG; aborting"; trap - EXIT; exit 1; }
models | tr ' ' '\n' | grep -qx gpt-oss:120b && unload gpt-oss:120b
m=$(models); say "start ps: $m"
echo "$m" | grep -q bge-m3 || bge_up
echo "$m" | grep -q gemma4:26b || gemma_up
(cd $R && md5sum core/raum/verify/guard.py core/raum/pipeline/verifier.py core/raum/pipeline/translate.py prompts/translate.md prompts/backtranslate.md profiles/spark.yaml | cut -c1-8 | tr '\n' ' '; echo " code md5 at start")

( low=0; n=0
  while true; do
    m=$(models); a=$(avail); n=$((n+1)); why=""
    if [ -n "$m" ]; then
      echo "$m" | tr ' ' '\n' | grep -qx gpt-oss:120b && why="gpt-oss:120b loaded by someone else"
      echo "$m" | grep -q bge-m3 || why="bge-m3 gone"
      [ -f $E/out/GEMMA_OFF ] || echo "$m" | grep -q gemma4:26b || why="gemma4:26b gone"
    fi
    [ "$a" -lt $FLOOR_MB ] && low=$((low+1)) || low=0
    [ $low -ge 2 ] && why="MemAvailable ${a} MB < ${FLOOR_MB} MB"
    [ $((n % 10)) -eq 0 ] && ! inwin && why="'$TAG' no longer in GPU_WINDOW ('$(cat $WIN 2>/dev/null)')"
    if [ -n "$why" ]; then
      say "WATCHDOG: $why (ps: $m; avail $a MB): stopping our GPU work"
      touch $E/out/ABORT; kill_ours
      break
    fi
    sleep 1
  done ) &
WD=$!

# ================= phase 1: accuracy (parallel) =================
laneA() {
  py "A gemma4:26b accuracy" 300 $A --model gemma4:26b --tag g26b --conc 4
  touch $E/out/GEMMA_OFF; unload gemma4:26b; say "gemma4:26b unloaded to free an Ollama slot (reloaded for its latency run and at the end)"
  for spec in "gemma4:e4b e4b" "gemma4:e2b e2b" "translategemma:4b tg4b"; do
    set -- $spec
    cand_on $1; py "A $1 accuracy" 300 $A --model $1 --tag $2 --conc 4
    [ $2 = tg4b ] && py "A $1 native prompt accuracy" 300 $A --model $1 --tag tg4b-native --prompt native --conc 4
    cand_off $1
  done
  touch $E/out/A_DONE
  if pulled translategemma:12b && [ $(( DEADLINE - $(date +%s) )) -gt 900 ]; then
    cand_on translategemma:12b; py "A translategemma:12b accuracy" 300 $A --model translategemma:12b --tag tg12b --conc 4; cand_off translategemma:12b
    for i in $(seq 1 300); do [ -f $E/out/Q_DONE ] && break; sleep 2; done
    qe raum-gemma-qe 0 -e STEPS=comet
  fi
  say "lane A done"
}
laneB() {
  for i in $(seq 1 600); do { [ -f $E/out/GEMMA_OFF ] && pulled gpt-oss:20b; } && break; aborted && return; sleep 2; done
  cand_on gpt-oss:20b
  py "B gpt-oss:20b back-translation accuracy" 600 $B --model gpt-oss:20b --tag oss20-bt --mode bt --think low --seq 0 --conc 4 &
  py "B gpt-oss:20b judge accuracy" 600 $B --model gpt-oss:20b --tag oss20-judge --mode judge --think low --seq 0 --conc 4
  wait; cand_off gpt-oss:20b
  Q=qwen3:30b-a3b-instruct-2507-q4_K_M; QT=""
  for i in $(seq 1 300); do pulled $Q && break; aborted && return; [ $(( DEADLINE - $(date +%s) )) -lt 1200 ] && break; sleep 2; done
  if ! pulled $Q; then say "qwen3 instruct-2507 not pulled in time: using qwen3:30b-a3b (thinking-2507) with think false"; Q=qwen3:30b-a3b; QT="--think false"; fi
  cand_on $Q
  py "B $Q back-translation accuracy" 600 $B --model $Q --tag q3i-bt --mode bt $QT --seq 0 --conc 4 &
  py "B $Q judge accuracy" 600 $B --model $Q --tag q3i-judge --mode judge $QT --seq 0 --conc 4
  wait
  echo $Q > $E/out/Q3MODEL
  cand_off $Q
  if [ $(( DEADLINE - $(date +%s) )) -gt 1000 ]; then
    cand_on qwen3:30b-a3b
    py "B qwen3 thinking-2507 think on (subset)" 400 $B --model qwen3:30b-a3b --tag q3t-think-bt --mode btraw --think true --num-predict 2048 --only ref,semantic,negation,allergy_omitted --limit 80 --seq 0 --conc 4
    cand_off qwen3:30b-a3b
  fi
  say "lane B done"
}
laneQ() {  # one metrics container at a time (memory): MetricX QE scores (+ CPU latency), then COMET once lane A is done
  qe raum-gemma-qe-mx 0 -e STEPS=metricx-large,metricx-xl,cpu
  for i in $(seq 1 600); do [ -f $E/out/A_DONE ] && break; aborted && return; sleep 2; done
  qe raum-gemma-qe 0 -e STEPS=comet
  touch $E/out/Q_DONE
  say "lane Q done"
}
laneA & PA=$!
laneB & PB=$!
laneQ & PQ=$!
wait $PA $PB $PQ
say "phase 1 done; files: $(ls $E/out | tr '\n' ' ')"

# ================= phase 2: latency, one model at a time under the shared lock =================
aborted && exit 1
gemma_up; rm -f $E/out/GEMMA_OFF
lat "A gemma4:26b latency" $A --model gemma4:26b --tag g26b-lat --pick 10
for spec in "gemma4:e4b e4b" "gemma4:e2b e2b" "translategemma:4b tg4b" "translategemma:12b tg12b"; do
  set -- $spec
  [ -f $E/out/a-$2.json ] || continue
  cand_on $1; warm $1; lat "A $1 latency" $A --model $1 --tag $2-lat --pick 10; cand_off $1
done
if [ -f $E/out/b-oss20-bt.json ]; then
  cand_on gpt-oss:20b; warm gpt-oss:20b
  lat "B gpt-oss:20b back-translation latency" $B --model gpt-oss:20b --tag oss20-bt-lat --mode bt --think low --seq 40 --limit 40
  lat "B gpt-oss:20b judge latency" $B --model gpt-oss:20b --tag oss20-judge-lat --mode judge --think low --seq 40 --limit 40
  cand_off gpt-oss:20b
fi
if [ -f $E/out/Q3MODEL ]; then
  Q=$(cat $E/out/Q3MODEL); QT=""; [ $Q = qwen3:30b-a3b ] && QT="--think false"
  cand_on $Q; warm $Q
  lat "B $Q back-translation latency" $B --model $Q --tag q3i-bt-lat --mode bt $QT --seq 40 --limit 40
  lat "B $Q judge latency" $B --model $Q --tag q3i-judge-lat --mode judge $QT --seq 40 --limit 40
  cand_off $Q
fi
qe raum-gemma-qelat 1 -e STEPS=metricx-large,metricx-xl,lat -e SCORE=0
say "phase 2 done"
