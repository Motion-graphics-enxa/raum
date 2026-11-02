#!/usr/bin/env bash
# Run raum-core and raum-agent in user space (no root, no systemd): for the shared GX10, where
# everything must stay inside ~/Desktop/HackathonRaum. A dedicated box uses deploy/systemd/ instead.
#
#   deploy/box.sh start | stop | restart | status | logs [core|agent|app]
#   RAUM_PROFILE=spark deploy/box.sh restart        # override raum.yaml's profile for this run
#   RAUM_APP=1 deploy/box.sh start                  # also serve the built app (vite preview, :5180,
#                                                   #  proxies /api + /rtc; RAUM_HTTPS=1 for phones)
# Env: RAUM_RUN (pid + log dir, default ~/Desktop/HackathonRaum/runtime/run),
#      RAUM_CORE_PORT (8000), LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (agent).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
RUN="${RAUM_RUN:-$HOME/Desktop/HackathonRaum/runtime/run}"
PORT="${RAUM_CORE_PORT:-8000}"
mkdir -p "$RUN"
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"
export RAUM_HOME="$ROOT"

pidof_svc() { cat "$RUN/$1.pid" 2>/dev/null || true; }
alive() { local p; p=$(pidof_svc "$1"); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }

start_svc() {
  local name=$1; shift
  if alive "$name"; then echo "$name: already running (pid $(pidof_svc "$name"))"; return; fi
  ( cd "$ROOT/$1" && shift && exec nohup "$@" ) >>"$RUN/$name.log" 2>&1 &
  echo $! >"$RUN/$name.pid"
  echo "$name: started (pid $!, log $RUN/$name.log)"
}

stop_svc() {
  local name=$1 p
  p=$(pidof_svc "$name")
  if [ -n "$p" ] && kill -0 "$p" 2>/dev/null; then
    kill "$p" 2>/dev/null || true
    for _ in $(seq 1 20); do kill -0 "$p" 2>/dev/null || break; sleep 0.25; done
    kill -9 "$p" 2>/dev/null || true
    echo "$name: stopped"
  fi
  rm -f "$RUN/$name.pid"
}

start_all() {
  (cd core && uv sync --frozen --quiet)
  start_svc core core uv run raum core --host 0.0.0.0 --port "$PORT"
  start_svc agent core uv run raum agent start
  if [ "${RAUM_APP:-0}" = 1 ]; then
    [ -f app/dist/index.html ] || (cd app && npm run build --silent)
    start_svc app app npm run preview -- --host 0.0.0.0 --port 5180 --strictPort
  fi
  for _ in $(seq 1 40); do
    curl -fsS -m 1 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1 && { echo "core: healthy on :$PORT"; return 0; }
    sleep 0.5
  done
  echo "core: not healthy yet; see $RUN/core.log" >&2
}

stop_all() { stop_svc app; stop_svc agent; stop_svc core; }

case "${1:-status}" in
  start) start_all ;;
  stop) stop_all ;;
  restart) stop_all; start_all ;;
  status)
    for s in core agent app; do
      if alive "$s"; then echo "$s: running (pid $(pidof_svc "$s"))"; else echo "$s: stopped"; fi
    done
    curl -fsS -m 2 "http://127.0.0.1:$PORT/api/health" 2>/dev/null && echo || true
    ;;
  logs) tail -n "${LINES:-60}" "$RUN/${2:-core}.log" ;;
  *) sed -n 2,10p "$0"; exit 2 ;;
esac
