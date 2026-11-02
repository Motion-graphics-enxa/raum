#!/usr/bin/env bash
# One-command deploy from the laptop to the box (plan §10.1 tactic 3). Target: under 30 s.
#
#   deploy/push.sh                 push to ${RAUM_HOST:-gx10} (an ~/.ssh/config alias, §8 S1)
#   deploy/push.sh --dry-run       print every step, touch nothing, connect to nothing
#   RAUM_HOST=192.168.8.10 deploy/push.sh
#
# Steps
#   1. rsync the repo to $HOST:$RAUM_DEST (default ~/Desktop/HackathonRaum/raum on the shared GX10;
#      light: no .git, node_modules, .venv, internal/,
#      data/, eval/out, *.wav, dist). Excluded paths on the box are never deleted, so its .venv,
#      node_modules, dist, data -> /var/lib/raum and deploy/.env survive every push.
#   2. on the box: cd core && uv sync --frozen
#   3. on the box: cd app && npm ci (only when package-lock.json changed) && npm run build
#      (only when the app sources changed; --force-build to rebuild anyway)
#   4. reload Caddy if deploy/Caddyfile changed and the container runs
#   5. sudo systemctl restart raum-core raum-agent
#   6. health check: curl -fsS http://127.0.0.1:8000/api/health (retries for ~20 s)
#
# Options: --dry-run  --no-build  --force-build  --no-restart  --no-health  -h|--help
# Env:     RAUM_HOST (ssh target, default spark), RAUM_SSH (overrides RAUM_HOST for ssh only),
#          RAUM_DEST (default Desktop/HackathonRaum/raum, relative = under the remote $HOME),
#          RAUM_RESTART (systemd | box | none; default: systemd units if installed, else deploy/box.sh),
#          RSYNC (rsync binary)
# The box's profile comes from RAUM_PROFILE in ~/Desktop/HackathonRaum/runtime/raum.env (shared box,
# written by install.sh --user-space) or /etc/raum/raum.env (dedicated box), so pushing a raum.yaml
# that says `profile: mock` does not switch the box back to the mock pipeline.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST="${RAUM_SSH:-${RAUM_HOST:-gx10}}"
DEST="${RAUM_DEST:-Desktop/HackathonRaum/raum}"
RESTART_MODE="${RAUM_RESTART:-auto}"
RSYNC="${RSYNC:-rsync}"
DRY=0 BUILD=1 FORCE_BUILD=0 RESTART=1 HEALTH=1

usage() { sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY=1 ;;
    --no-build) BUILD=0 ;;
    --force-build) FORCE_BUILD=1 ;;
    --no-restart) RESTART=0 ;;
    --no-health) HEALTH=0 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# one multiplexed SSH connection for rsync and the remote steps (saves ~1 s per connection)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15
          -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-raum-%r@%h:%p" -o ControlPersist=120)

EXCLUDES=(
  --exclude=/.git --exclude=.claude/ --exclude=internal/ --exclude=node_modules/ --exclude=.venv/
  --exclude=/data --exclude=eval/out/ --exclude=eval/fixtures/gen/ --exclude='*.wav' --exclude=dist/
  --exclude=__pycache__/ --exclude='*.pyc' --exclude=.pytest_cache/ --exclude=.ruff_cache/
  --exclude=.vite/ --exclude=.DS_Store --exclude=.env --exclude='.env.*' --exclude=models/
  --exclude='*.gguf' --exclude=recordings/ --exclude=desktop/src-tauri/target/
)
RSYNC_CMD=("$RSYNC" -az --delete "${EXCLUDES[@]}" -e "ssh ${SSH_OPTS[*]}" "$ROOT/" "$HOST:$DEST/")

# Runs on the box. Parameters arrive as positional args: DEST BUILD FORCE_BUILD RESTART HEALTH
read -r -d '' REMOTE <<'REMOTE_EOF' || true
set -euo pipefail
DEST="$1" BUILD="$2" FORCE_BUILD="$3" RESTART="$4" HEALTH="$5"
export PATH="/usr/local/bin:$HOME/.local/bin:$PATH"
RESTART_MODE="${6:-auto}"
case "$DEST" in /*) ;; *) DEST="$HOME/$DEST" ;; esac
# dedicated box: python for user raum lives in /opt/raum/python (home dirs are 0750)
[ -d /opt/raum/python ] && export UV_PYTHON_INSTALL_DIR=/opt/raum/python
t0=$(date +%s%N)
lap() { printf '    %s (%d ms)\n' "$1" $(( ($(date +%s%N) - t0) / 1000000 )); }
cd "$DEST"

ENV_FILE="$HOME/Desktop/HackathonRaum/runtime/raum.env"
if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
echo "==> core: uv sync --frozen ${RAUM_UV_EXTRAS:-}"
# shellcheck disable=SC2086
(cd core && uv sync --frozen --quiet ${RAUM_UV_EXTRAS:-})
lap "python environment in sync"

if [ "$BUILD" = 1 ]; then
  cd "$DEST/app"
  lock="$(sha256sum package-lock.json | cut -d' ' -f1)"
  if [ ! -d node_modules ] || [ "$(cat node_modules/.raum-lock 2>/dev/null)" != "$lock" ]; then
    echo "==> app: npm ci (lockfile changed)"
    npm ci --no-audit --no-fund --loglevel=error
    echo "$lock" > node_modules/.raum-lock
  else
    echo "==> app: lockfile unchanged, skip npm ci"
  fi
  src="$(find src public index.html package.json tsconfig*.json vite.config.* -type f 2>/dev/null \
         | LC_ALL=C sort | xargs sha256sum | sha256sum | cut -d' ' -f1)"
  if [ "$FORCE_BUILD" = 1 ] || [ ! -f dist/index.html ] || [ "$(cat node_modules/.raum-src 2>/dev/null)" != "$src" ]; then
    echo "==> app: npm run build"
    npm run build --silent
    echo "$src" > node_modules/.raum-src
  else
    echo "==> app: sources unchanged, skip build"
  fi
  lap "app ready"
  cd "$DEST"
fi

caddy_hash="$(sha256sum deploy/Caddyfile | cut -d' ' -f1)"
if [ "$(cat "$HOME/.cache/raum-caddy-hash" 2>/dev/null)" != "$caddy_hash" ]; then
  if sudo -n docker compose -f deploy/docker-compose.yml ps --status running caddy 2>/dev/null | grep -q caddy; then
    echo "==> caddy: Caddyfile changed, reload"
    sudo -n docker compose -f deploy/docker-compose.yml exec -T caddy \
      caddy reload --config /etc/raum-deploy/Caddyfile --adapter caddyfile
    mkdir -p "$HOME/.cache" && echo "$caddy_hash" > "$HOME/.cache/raum-caddy-hash"
  fi
fi

if [ -z "${RAUM_PROFILE:-}" ] && ! sudo -n grep -q '^RAUM_PROFILE=' /etc/raum/raum.env 2>/dev/null; then
  echo "    note: no RAUM_PROFILE in $ENV_FILE (install.sh --user-space writes it) or /etc/raum/raum.env;"
  echo "          services use raum.yaml's '$(grep -m1 '^profile:' raum.yaml)'"
fi

if [ "$RESTART" = 1 ]; then
  mode="$RESTART_MODE"
  if [ "$mode" = auto ]; then
    if systemctl list-unit-files raum-core.service 2>/dev/null | grep -q raum-core; then mode=systemd; else mode=box; fi
  fi
  case "$mode" in
    systemd)
      echo "==> restart raum-core raum-agent (systemd)"
      sudo -n systemctl restart raum-core raum-agent || { echo "systemctl restart failed" >&2; exit 1; }
      ;;
    box)
      echo "==> restart core + agent (deploy/box.sh, user space)"
      bash deploy/box.sh restart
      ;;
    none) echo "==> restart skipped" ;;
  esac
  lap "services restarted"
fi

if [ "$HEALTH" = 1 ]; then
  echo "==> health: http://127.0.0.1:8000/api/health"
  for _ in $(seq 1 40); do
    if out="$(curl -fsS --max-time 2 http://127.0.0.1:8000/api/health 2>/dev/null)"; then
      echo "    $out"
      lap "healthy"
      exit 0
    fi
    sleep 0.5
  done
  echo "raum-core not healthy after 20 s; last log lines:" >&2
  sudo -n journalctl -u raum-core -n 30 --no-pager >&2 2>/dev/null || bash deploy/box.sh logs core >&2 || true
  exit 1
fi
REMOTE_EOF

quote() { printf '%q ' "$@"; }

if [ "$DRY" = 1 ]; then
  echo "dry run: nothing is copied, nothing connects to $HOST"
  echo
  echo "1. rsync $ROOT/ -> $HOST:$DEST/"
  echo "   $(quote "${RSYNC_CMD[@]}")"
  echo
  echo "2-6. on $HOST (one ssh session):"
  echo "   ssh ${SSH_OPTS[*]} $HOST bash -s -- $DEST $BUILD $FORCE_BUILD $RESTART $HEALTH  <<script"
  printf '%s\n' "$REMOTE" | sed 's/^/   | /'
  exit 0
fi

start=$(date +%s)
echo "==> rsync -> $HOST:$DEST"
"${RSYNC_CMD[@]}"
printf '%s\n' "$REMOTE" | ssh "${SSH_OPTS[@]}" "$HOST" bash -s -- "$DEST" "$BUILD" "$FORCE_BUILD" "$RESTART" "$HEALTH" "$RESTART_MODE"
took=$(( $(date +%s) - start ))
if [ "$took" -gt 30 ]; then
  echo "pushed in ${took}s (target < 30 s; first push and npm ci are slower)"
else
  echo "pushed in ${took}s"
fi
