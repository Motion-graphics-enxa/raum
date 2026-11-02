#!/usr/bin/env bash
# Run raum-core and raum-agent in user space (no root, no systemd): for the shared GX10, where
# everything must stay inside ~/Desktop/HackathonRaum. A dedicated box uses deploy/systemd/ instead.
#
#   deploy/box.sh start | stop | restart | status | logs [core|agent|app|speakerphone|whisper|whisper-final]
#   RAUM_PROFILE=spark deploy/box.sh restart        # override raum.yaml's profile for this run
#   RAUM_APP=1 deploy/box.sh start                  # also serve the built app (vite preview, :5180,
#                                                   #  proxies /api + /rtc; RAUM_HTTPS=1 for phones)
#   RAUM_SPEAKERPHONE_ROOM=sz-4821 deploy/box.sh start   # also put the USB/Bluetooth speakerphone into
#        that room (A-03; RAUM_SPEAKERPHONE_ROLE a|b, _LANG, _DEVICE <index|name>; see
#        `uv run raum speakerphone --list-devices`). Restarted if it crashes, never when it exits 3
#        (no PortAudio / no such device / unplugged): fix the device, then `deploy/box.sh restart`.
#   WHISPER_DIR=~/Desktop/HackathonRaum/whisper.cpp   # start whisper-server :8081 if nothing answers
#        there (WHISPER_MODEL large-v3-turbo, WHISPER_THREADS 8); restart keeps it, stop ends it.
# Install / first setup on the shared box: ./install.sh --user-space (writes runtime/raum.env).
# Env: sourced first from $RAUM_ENV_FILE (default ~/Desktop/HackathonRaum/runtime/raum.env) if present.
#      RAUM_RUN (pid + log dir, default ~/Desktop/HackathonRaum/runtime/run),
#      RAUM_CORE_PORT (8000), LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (agent).
#      Shared box, privacy meter + sealed mode scoped to RAUM's own processes (T-06/T-07):
#      RAUM_NET_UP   command run before the services start, e.g. "runtime/raum-net.sh up"
#                    (creates the cgroup, reloads table inet raum: counters 0, seal off)
#      RAUM_CGROUP   cgroup v2 dir every service joins (sudo -n), e.g.
#                    /sys/fs/cgroup/user.slice/user-1000.slice/raum; nft matches its sockets
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
# Box-local settings outside the repo (survive rsync): RAUM_PROFILE=spark, RAUM_APP=1, RAUM_HTTPS=1 ...
ENV_FILE="${RAUM_ENV_FILE:-$HOME/Desktop/HackathonRaum/runtime/raum.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
RUN="${RAUM_RUN:-$HOME/Desktop/HackathonRaum/runtime/run}"
PORT="${RAUM_CORE_PORT:-8000}"
mkdir -p "$RUN"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export RAUM_HOME="$ROOT"

pidof_svc() { cat "$RUN/$1.pid" 2>/dev/null || true; }
alive() { local p; p=$(pidof_svc "$1"); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

# Each service runs in its own process group (setsid), so stop takes down every child too:
# uv run -> python, and the vite preview server. Without setsid (macOS) we fall back to pkill -P.
SETSID=""
command -v setsid >/dev/null 2>&1 && SETSID=setsid

join_cgroup() {  # move this (sub)shell into $RAUM_CGROUP; the service it execs and its children inherit it
  local pid=${BASHPID:-}
  [ -n "${RAUM_CGROUP:-}" ] && [ -n "$pid" ] || return 0
  echo "$pid" | sudo -n tee "$RAUM_CGROUP/cgroup.procs" >/dev/null 2>&1 ||
    echo "box.sh: could not join $RAUM_CGROUP (privacy meter/seal won't see this service)" >&2
}

start_svc() {
  local name=$1; shift
  if alive "$name"; then echo "$name: already running (pid $(pidof_svc "$name"))"; return; fi
  ( cd "$ROOT/$1" && shift && join_cgroup && exec $SETSID nohup "$@" ) >>"$RUN/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "$name: started (pid $!, log $RUN/$name.log)"
}

kill_tree() {  # signal a service's whole tree
  local sig=$1 p=$2
  if [ -n "$SETSID" ]; then
    kill "-$sig" -- "-$p" 2>/dev/null || kill "-$sig" "$p" 2>/dev/null || true
  else
    pkill "-$sig" -P "$p" 2>/dev/null || true
    kill "-$sig" "$p" 2>/dev/null || true
  fi
}

stop_svc() {
  local name=$1 p
  p=$(pidof_svc "$name")
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill_tree TERM "$p"
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
    kill_tree KILL "$p"
    echo "$name: stopped"
  fi
  rm -f "$RUN/$name.pid"
}

start_all() {
  if [ -n "${RAUM_NET_UP:-}" ] && ! alive core; then  # never reset counters/seal under a running core
    # shellcheck disable=SC2086
    $RAUM_NET_UP || echo "box.sh: RAUM_NET_UP failed; privacy meter falls back to the stub" >&2
  fi
  # shellcheck disable=SC2086
  (cd core && uv sync --frozen --quiet ${RAUM_UV_EXTRAS:-})
  start_svc core core uv run raum core --host 0.0.0.0 --port "$PORT"
  start_svc agent core uv run raum agent start
  if [ -n "${WHISPER_DIR:-}" ] && [ -x "$WHISPER_DIR/build/bin/whisper-server" ] \
      && ! alive whisper && ! curl -s -m 1 -o /dev/null http://127.0.0.1:8081; then
    # user-space whisper (install.sh --user-space); kept running across restarts, `stop` ends it
    start_svc whisper . "$WHISPER_DIR/build/bin/whisper-server" \
      -m "$WHISPER_DIR/models/ggml-${WHISPER_MODEL:-large-v3-turbo}.bin" --host 127.0.0.1 --port 8081 \
      -t "${WHISPER_THREADS:-8}" -l auto -nlp   # -nlp: no second encoder pass for language_probabilities
  fi
  if [ -n "${WHISPER_DIR:-}" ] && [ -x "$WHISPER_DIR/build/bin/whisper-server" ] \
      && ! alive whisper-final && ! curl -s -m 1 -o /dev/null http://127.0.0.1:8082; then
    # the finals' own server (asr.options.final_url): one request at a time each, so no final waits for a partial
    start_svc whisper-final . "$WHISPER_DIR/build/bin/whisper-server" \
      -m "$WHISPER_DIR/models/ggml-${WHISPER_MODEL:-large-v3-turbo}.bin" --host 127.0.0.1 --port 8082 \
      -t "${WHISPER_THREADS:-8}" -l auto -nlp
  fi
  if [ -n "${RAUM_SPEAKERPHONE_ROOM:-}" ]; then
    # supervisor: rejoins on its own while LiveKit restarts; a crash restarts it after 5 s, but exit
    # 0 (stopped), 2 (bad room id) and 3 (no audio device) are final, so no crash loop
    start_svc speakerphone core bash -c 'while :; do uv run raum speakerphone; rc=$?;
      case $rc in 0|2|3) echo "speakerphone exited $rc (not restarted)"; exit $rc ;; esac
      echo "speakerphone exited $rc, restarting in 5 s"; sleep 5; done'
  fi
  if [ "${RAUM_APP:-0}" = 1 ]; then
    [ -f app/dist/index.html ] || (cd app && npm run build --silent)
    start_svc app app node_modules/.bin/vite preview --host 0.0.0.0 --port 5180 --strictPort
  fi
  for _ in $(seq 1 40); do
    curl -fsS -m 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && { echo "core: healthy on :$PORT"; return 0; }
    sleep 0.5
  done
  echo "core: not healthy yet; see $RUN/core.log" >&2
}

stop_all() {
  stop_svc speakerphone; stop_svc app; stop_svc agent; stop_svc core
  # orphans left by an older box.sh (before process groups): free our ports
  if command -v fuser >/dev/null 2>&1; then
    fuser -k -TERM 5180/tcp "$PORT"/tcp >/dev/null 2>&1 || true
  fi
}

case "${1:-status}" in
  start) start_all ;;
  stop) stop_all; stop_svc whisper; stop_svc whisper-final ;;
  restart) stop_all; start_all ;;
  status)
    for s in core agent app speakerphone whisper whisper-final; do
      if alive "$s"; then echo "$s: running (pid $(pidof_svc "$s"))"; else echo "$s: stopped"; fi
    done
    curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null && echo || true
    ;;
  logs) tail -n "${LINES:-60}" "$RUN/${2:-core}.log" ;;
  *) sed -n 2,18p "$0"; exit 2 ;;
esac
