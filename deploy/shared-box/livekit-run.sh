#!/usr/bin/env bash
# (Re)create the raum-livekit container with runtime/livekit.yaml (no external STUN; embedded TURN/STUN
# on the LAN). Same name, host networking, dev keys devkey/secret, same ports (7880 signalling,
# 7881 TCP, 7882 UDP media) plus 3478/udp STUN+TURN and 30000-30999/udp TURN relays.
# The docker scope (cgroup) changes on every recreate, so the nft table is reloaded at the end.
#   runtime/livekit-run.sh            recreate raum-livekit  (interrupts live phone sessions!)
#   NAME=x PORTS_OFFSET=10000 ...     throwaway trial on other ports (no nft reload)
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd -P)
NAME=${NAME:-raum-livekit}
IMAGE=${LIVEKIT_IMAGE:-livekit/livekit-server@sha256:25cd902de7cacbd5f1f779f6a6ac1eae20ba8f56b0935934ce6b0ab80d1e3d20}  # v1.13.11
CONF=$HERE/livekit.yaml
OFF=${PORTS_OFFSET:-0}
if [ "$OFF" != 0 ]; then  # trial: shift every port so it can run next to the real one
  CONF=$HERE/livekit-trial.yaml
  sed -E "s/^port: 7880/port: $((7880+OFF))/; s/tcp_port: 7881/tcp_port: $((7881+OFF))/;
          s/udp_port: 7882/udp_port: $((7882+OFF))/; s/udp_port: 3478/udp_port: $((3478+OFF))/;
          s/10.24.249.204:3478/10.24.249.204:$((3478+OFF))/; s/relay_range_start: 30000/relay_range_start: $((30000+OFF))/;
          s/relay_range_end: 30999/relay_range_end: $((30999+OFF))/" "$HERE/livekit.yaml" >"$CONF"
fi
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --network host --restart unless-stopped \
  -v "$CONF:/etc/livekit.yaml:ro" \
  "$IMAGE" --dev --config /etc/livekit.yaml --bind 0.0.0.0 --node-ip 10.24.249.204
if [ "$OFF" = 0 ]; then
  for _ in $(seq 1 20); do curl -fsS -m 1 http://127.0.0.1:7880/ >/dev/null 2>&1 && break; sleep 0.5; done
  "$HERE/raum-net.sh" load   # new docker scope -> new cgroup id in table inet raum
fi
docker ps --filter "name=^$NAME\$" --format '{{.Names}} {{.Status}}'
