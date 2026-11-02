#!/usr/bin/env bash
# RAUM on this machine: livekit-server --dev, raum-core, raum-agent and the app, together.
#   scripts/dev.sh            http on :5180 (two tabs on this Mac: mic works on localhost)
#   scripts/dev.sh --https    self-signed https on :5180 so phones on the LAN may use the mic
# Ctrl-C stops everything. Logs: data/logs/*.log
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
HTTPS=0
for a in "$@"; do
  case "$a" in
    --https) HTTPS=1 ;;
    -h|--help) sed -n 2,6p "$0"; exit 0 ;;
  esac
done

mkdir -p data/logs
pids=()
cleanup() {
  trap - INT TERM EXIT
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

lan_ip() {
  ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo 127.0.0.1
}
IP=$(lan_ip)
APP_PORT=${RAUM_APP_PORT:-5180}

need() { command -v "$1" >/dev/null || { echo "missing: $1 ($2)"; exit 1; }; }
need livekit-server "brew install livekit"
need uv "https://docs.astral.sh/uv/"
need npm "node.js"

# 1. LiveKit (reuse one that is already running)
if curl -fsS -m 1 http://127.0.0.1:7880 >/dev/null 2>&1; then
  echo "livekit  : already running on :7880"
else
  # LAN-only ICE: LiveKit answers STUN itself (UDP 3478); without stun_servers it hands browsers
  # Google's and Twilio's STUN. $IP is read at start, so a Wi-Fi change needs a dev.sh restart.
  LK_CONFIG="rtc:
  use_external_ip: false
  node_ip: $IP
  stun_servers: [\"$IP:3478\"]
turn:
  enabled: true
  udp_port: 3478
  tls_port: 0
  relay_range_start: 30000
  relay_range_end: 30999"
  livekit-server --dev --bind 0.0.0.0 --node-ip "$IP" --config-body "$LK_CONFIG" >data/logs/livekit.log 2>&1 &
  pids+=($!)
  echo "livekit  : :7880 (node-ip $IP, STUN/TURN $IP:3478)"
fi

# 2. Python env
(cd core && uv sync -q)

# 3. raum-core
(cd core && exec uv run raum core --host 0.0.0.0 --port 8000) >data/logs/core.log 2>&1 &
pids+=($!)
echo "core     : :8000   (logs data/logs/core.log)"

# 4. raum-agent (LiveKit Agents worker, mock or real engines per raum.yaml)
(cd core && exec uv run raum agent start) >data/logs/agent.log 2>&1 &
pids+=($!)
echo "agent    : worker  (logs data/logs/agent.log)"

# 5. app
[ -d app/node_modules ] || (cd app && npm ci --silent)
if [ "$HTTPS" = 1 ]; then
  (cd app && RAUM_HTTPS=1 exec npm run dev -- --host 0.0.0.0) >data/logs/app.log 2>&1 &
  SCHEME=https
else
  (cd app && exec npm run dev -- --host 0.0.0.0) >data/logs/app.log 2>&1 &
  SCHEME=http
fi
pids+=($!)

for _ in $(seq 1 60); do
  curl -fsS -m 1 http://127.0.0.1:8000/api/health >/dev/null 2>&1 && break
  sleep 0.5
done

echo
echo "RAUM is up (profile: $(grep -E '^profile:' raum.yaml | awk '{print $2}'))"
echo "  setup     $SCHEME://localhost:${APP_PORT}/            (open a room, QR codes)"
echo "  admin     $SCHEME://localhost:${APP_PORT}/admin"
echo "  phones    $SCHEME://$IP:${APP_PORT}/j/<room>?role=a|b $( [ "$HTTPS" = 1 ] || echo '(phones need --https for the mic)')"
echo "  table     $SCHEME://localhost:${APP_PORT}/t/<room>"
echo
# bash 3.2 (macOS) has no `wait -n`: poll until any service exits
while :; do
  for p in "${pids[@]}"; do
    if ! kill -0 "$p" 2>/dev/null; then
      echo "a service exited (pid $p); see data/logs/. stopping."
      exit 1
    fi
  done
  sleep 1
done
