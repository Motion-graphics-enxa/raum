#!/usr/bin/env bash
# RAUM installer (F-03): hardware detection -> profile -> prerequisites -> services.
#
#   ./install.sh --user-space --dry-run   shared GX10 (the documented path): no root, no systemd.
#                                    Repo in ~/Desktop/HackathonRaum/raum (deploy/push.sh puts it
#                                    there), settings in ~/Desktop/HackathonRaum/runtime/raum.env,
#                                    services run by deploy/box.sh. Then without --dry-run.
#   ./install.sh --dry-run          dedicated box: root, systemd units, /opt/raum, nft table
#   ./install.sh                    show the plan, ask before changing anything
#   ./install.sh --yes              apply without asking (non-interactive)
#
# Profile (profiles/*.yaml, plan §6.7), first match wins:
#   nvidia-smi shows a GB10 (DGX Spark / ASUS Ascent GX10)      -> spark
#   Jetson (/etc/nv_tegra_release or device-tree model)          -> edge
#   NVIDIA GPU with >= 24 GB (reported >= 23000 MiB; a 4090 says 24564)  -> gpu-24
#   Apple Silicon with >= 32 GB unified memory                   -> mac
#   anything else                                                -> lite
#
# Options
#   --user-space       no root: check (not install) system packages and the shared services
#                      (Ollama, LiveKit, whisper), uv into ~/.local/bin, build the app, whisper.cpp
#                      into ~/Desktop/HackathonRaum/whisper.cpp, write runtime/raum.env, box.sh restart
#   --profile P        skip detection (spark | gpu-24 | mac | edge | lite | mock)
#   --pull-models      also download the profile's models (Ollama + whisper; ~90 GB on spark)
#   --bakeoff-models   with --pull-models: also the bake-off candidates (gemma4:e4b, qwen3:30b-a3b)
#   --no-services      prerequisites only: no systemd units, nft table, containers or box.sh restart
#   --no-whisper       do not build whisper.cpp
#   --host IP          LAN IP for Caddy/LiveKit (default: the source address of the default route)
#   --dry-run | --yes | -h
# Env: RAUM_BOX_DIR (user space; default ~/Desktop/HackathonRaum), RAUM_UV_EXTRAS (e.g.
#      "--extra speakerphone"), RAUM_FAKE_* (see detection; tests every branch on any machine).
#
# Safe by default: no model downloads unless --pull-models, existing config files
# (/etc/raum/raum.env, runtime/raum.env, deploy/.env) are never overwritten, nothing is removed, and
# without a terminal or --yes it only prints the plan. On macOS it sets up a development run.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP="${RAUM_APP_DIR:-/opt/raum/app}"
DRY=0 YES=0 PROFILE="" PULL=0 BAKEOFF=0 SERVICES=1 WHISPER=1 HOST_IP="" USERSPACE=0

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run|-n) DRY=1 ;;
    --yes|-y) YES=1 ;;
    --user-space) USERSPACE=1 ;;
    --profile) PROFILE="${2:?--profile needs a value}"; shift ;;
    --profile=*) PROFILE="${1#*=}" ;;
    --pull-models) PULL=1 ;;
    --bakeoff-models) BAKEOFF=1 ;;
    --no-services) SERVICES=0 ;;
    --no-whisper) WHISPER=0 ;;
    --host) HOST_IP="${2:?--host needs a value}"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---------------------------------------------------------------- detection
# RAUM_FAKE_* override the probes, so every branch can be tested with --dry-run on any machine:
#   RAUM_FAKE_GPU="NVIDIA GB10, [N/A]"   (nvidia-smi --query-gpu=name,memory.total csv line)
#   RAUM_FAKE_OS=Linux RAUM_FAKE_ARCH=aarch64 RAUM_FAKE_MEM_GB=64 RAUM_FAKE_JETSON=1

OS="${RAUM_FAKE_OS:-$(uname -s)}"
ARCH="${RAUM_FAKE_ARCH:-$(uname -m)}"

gpu_lines() {
  if [ -n "${RAUM_FAKE_GPU+x}" ]; then
    printf '%s\n' "$RAUM_FAKE_GPU"
  elif command -v nvidia-smi >/dev/null 2>&1; then
    nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>/dev/null || true
  fi
}

is_jetson() {
  [ "${RAUM_FAKE_JETSON:-0}" = 1 ] && return 0
  [ -n "${RAUM_FAKE_OS:-}" ] && return 1
  [ -f /etc/nv_tegra_release ] && return 0
  grep -qi jetson /proc/device-tree/model 2>/dev/null
}

mem_gb() {
  if [ -n "${RAUM_FAKE_MEM_GB:-}" ]; then echo "$RAUM_FAKE_MEM_GB"; return; fi
  if [ "$OS" = Darwin ]; then
    echo $(( $(sysctl -n hw.memsize) / 1073741824 ))
  else
    awk '/MemTotal/ { printf "%d\n", $2 / 1048576 }' /proc/meminfo 2>/dev/null || echo 0
  fi
}

detect_profile() {
  local gpus max_mib
  gpus="$(gpu_lines)"
  if printf '%s' "$gpus" | grep -q 'GB10'; then echo spark; return; fi
  if is_jetson; then echo edge; return; fi
  if [ -n "$gpus" ]; then
    max_mib="$(printf '%s\n' "$gpus" | awk -F',' '{ gsub(/[^0-9]/, "", $2); if ($2 + 0 > m) m = $2 + 0 } END { print m + 0 }')"
    if [ "$max_mib" -ge 23000 ]; then echo gpu-24; return; fi
  fi
  if [ "$OS" = Darwin ] && [ "$ARCH" = arm64 ] && [ "$(mem_gb)" -ge 32 ]; then echo mac; return; fi
  echo lite
}

DETECTED="$(detect_profile)"
PROFILE="${PROFILE:-$DETECTED}"
[ -f "$ROOT/profiles/$PROFILE.yaml" ] || { echo "unknown profile '$PROFILE' (profiles/*.yaml)" >&2; exit 2; }

# ---------------------------------------------------------------- helpers

SUDO=""
if [ "$(id -u)" -ne 0 ]; then SUDO="sudo"; fi
ME="${SUDO_USER:-$(id -un)}"
STEPS=0

say() { printf '\n==> %s\n' "$*"; }
run() {  # run a command (argv)
  STEPS=$((STEPS + 1))
  if [ "$DRY" = 1 ]; then printf '  + %s\n' "$*"; else printf '+ %s\n' "$*"; "$@"; fi
}
runsh() {  # run a shell snippet (pipes, redirections)
  STEPS=$((STEPS + 1))
  if [ "$DRY" = 1 ]; then printf '  + %s\n' "$1"; else printf '+ %s\n' "$1"; bash -c "$1"; fi
}
as_user() {  # run a shell snippet as the login user, also under `sudo ./install.sh`, so .venv,
  # node_modules, dist and the whisper build stay writable for deploy/push.sh
  if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != root ]; then
    run sudo -u "$SUDO_USER" -H env UV_PYTHON_INSTALL_DIR=/opt/raum/python PATH="/usr/local/bin:$PATH" bash -c "$1"
  else
    run env UV_PYTHON_INSTALL_DIR=/opt/raum/python bash -c "$1"
  fi
}
have() { command -v "$1" >/dev/null 2>&1; }
write_new() {  # write_new <path> <mode> <owner:group|-> <content>: only if the file does not exist
  local path="$1" mode="$2" owner="$3" content="$4" sudo="$SUDO"
  [ "$owner" = - ] && sudo=""  # user space: our own file, no sudo
  if [ -e "$path" ]; then echo "  keep existing $path"; return; fi
  STEPS=$((STEPS + 1))
  if [ "$DRY" = 1 ]; then
    printf '  + write %s (%s%s):\n' "$path" "$mode" "$([ "$owner" = - ] || echo " $owner")"
    printf '%s\n' "$content" | sed 's/^/      /'
    return
  fi
  printf '%s\n' "$content" | $sudo tee "$path" >/dev/null
  $sudo chmod "$mode" "$path"
  [ "$owner" = - ] || $sudo chown "$owner" "$path"
  echo "+ wrote $path"
}
ollama_models() {  # models the profile runs on Ollama
  grep -E 'engine:[[:space:]]*ollama' "$ROOT/profiles/$PROFILE.yaml" \
    | sed -n 's/.*model:[[:space:]]*\([^,}[:space:]]*\).*/\1/p' | sort -u
}
asr_model() {
  grep -E '^[[:space:]]*asr:' "$ROOT/profiles/$PROFILE.yaml" | sed -n 's/.*model:[[:space:]]*\([^,}[:space:]]*\).*/\1/p'
}
lan_ip() {
  if [ -n "$HOST_IP" ]; then echo "$HOST_IP"; return; fi
  # no packet is sent: this only asks the routing table which source address it would use
  # (|| true: under pipefail a missing `ip` or no route must not abort the installer)
  { ip -4 route get 1.1.1.1 2>/dev/null || true; } \
    | awk '{ for (i = 1; i <= NF; i++) if ($i == "src") { print $(i + 1); exit } }'
}
node_ok() {  # Vite 8 needs Node >= 20.19 (or >= 22.12)
  local v major minor
  v="$(node -p 'process.versions.node' 2>/dev/null || echo 0.0.0)"
  major="${v%%.*}"; minor="$(echo "$v" | cut -d. -f2)"
  [ "$major" -gt 22 ] || { [ "$major" -eq 22 ] && [ "$minor" -ge 12 ]; } || { [ "$major" -eq 20 ] && [ "$minor" -ge 19 ]; }
}
up() {  # up <url>: something answers (read-only probe)
  curl -s -m 2 -o /dev/null "$1" 2>/dev/null  # any HTTP answer
}
whisper_cuda_flags() {
  if [ -n "$(gpu_lines)" ] || is_jetson; then
    echo "-DGGML_CUDA=1 -DCMAKE_CUDA_ARCHITECTURES=native"   # GB10 needs 'native' (§8 S5)
  fi
}

# ---------------------------------------------------------------- plan

GPU_DESC="$(gpu_lines | head -1)"
echo "RAUM installer"
echo "  machine : $OS $ARCH, $(mem_gb) GB RAM${GPU_DESC:+, GPU: $GPU_DESC}"
echo "  profile : $PROFILE$([ "$PROFILE" = "$DETECTED" ] || echo " (detected: $DETECTED)")"
echo "  repo    : $ROOT"
[ "$USERSPACE" = 1 ] && echo "  layout  : user space (no root, no systemd; deploy/box.sh)"
[ "$DRY" = 1 ] && echo "  mode    : dry run (nothing is changed)"

if [ "$DRY" = 0 ] && [ "$YES" = 0 ]; then
  if [ -t 0 ]; then
    printf '\nApply the steps for profile %s? [y/N] ' "$PROFILE"
    read -r answer
    case "$answer" in y|Y|yes) ;; *) echo "nothing changed"; exit 0 ;; esac
  else
    echo "no terminal and no --yes: printing the plan only"
    DRY=1
  fi
fi

# ---------------------------------------------------------------- 1. profile

if [ "$USERSPACE" = 1 ]; then
  : # the profile goes into runtime/raum.env (RAUM_PROFILE), so a pushed raum.yaml never flips the box
else
  say "raum.yaml: profile"
  CURRENT="$(sed -n 's/^profile:[[:space:]]*\([^[:space:]#]*\).*/\1/p' "$ROOT/raum.yaml" | head -1)"
  if [ "$CURRENT" = "$PROFILE" ]; then
    echo "  already 'profile: $PROFILE'"
  elif [ "$DRY" = 1 ]; then
    echo "  + raum.yaml: profile: ${CURRENT:-<none>} -> $PROFILE"
  else
    awk -v p="$PROFILE" '!done && /^profile:/ { sub(/^profile:[[:space:]]*[^[:space:]#]*/, "profile: " p); done = 1 } { print }' \
      "$ROOT/raum.yaml" > "$ROOT/raum.yaml.tmp" && mv "$ROOT/raum.yaml.tmp" "$ROOT/raum.yaml"
    echo "+ raum.yaml: profile: $CURRENT -> $PROFILE"
  fi
fi

# ---------------------------------------------------------------- macOS: development setup only

if [ "$OS" = Darwin ]; then
  [ "$USERSPACE" = 1 ] && echo "  (macOS is always user space)"
  say "macOS: prerequisites (Homebrew)"
  if have brew; then
    for pkg in uv node livekit; do have "${pkg/livekit/livekit-server}" || run brew install "$pkg"; done
    if [ "$PROFILE" != mock ]; then
      have ollama || run brew install ollama
      have whisper-server || run brew install whisper-cpp
    fi
  else
    echo "  Homebrew not found: install uv, node, livekit-server (and ollama, whisper-cpp) by hand"
  fi
  say "macOS: project dependencies"
  run bash -c "cd '$ROOT/core' && uv sync${RAUM_UV_EXTRAS:+ $RAUM_UV_EXTRAS}"
  run bash -c "cd '$ROOT/app' && npm ci"
  if [ "$PULL" = 1 ] && [ "$PROFILE" != mock ]; then
    say "models"
    for m in $(ollama_models); do run ollama pull "$m"; done
    run bash -c "mkdir -p '$ROOT/models' && curl -fL -o '$ROOT/models/ggml-$(asr_model).bin' https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$(asr_model).bin"
  fi
  say "run (no services on macOS)"
  cat <<EOF
  scripts/dev.sh                               # livekit-server --dev, core :8000, agent, app :5180
                                               #   (scripts/dev.sh --https so phones may use the mic)
EOF
  if [ "$PROFILE" != mock ]; then
    cat <<EOF
  ollama serve                                 # translation$( [ "$PROFILE" = lite ] && echo " (gemma4:e2b)" )
  whisper-server -m models/ggml-$(asr_model).bin --port 8081 -l auto   # transcription (--pull-models fetches the model)
EOF
  fi
  echo
  echo "done ($STEPS step(s)$([ "$DRY" = 1 ] && echo ', dry run'))"
  exit 0
fi

[ "$OS" = Linux ] || { echo "unsupported OS: $OS" >&2; exit 1; }

# ---------------------------------------------------------------- Linux, user space (shared GX10)

if [ "$USERSPACE" = 1 ]; then
  if [ "$(id -u)" -eq 0 ]; then echo "--user-space runs as the login user, not root" >&2; exit 2; fi
  BOX="${RAUM_BOX_DIR:-$HOME/Desktop/HackathonRaum}"
  RUNTIME="$BOX/runtime"
  ENV_FILE="$RUNTIME/raum.env"
  export PATH="$HOME/.local/bin:$PATH"

  say "folders in $BOX (everything RAUM writes stays here)"
  case "$ROOT" in "$BOX"/*) ;; *) echo "  note: this repo is $ROOT; deploy/push.sh keeps it at $BOX/raum" ;; esac
  run mkdir -p "$RUNTIME/run" "$RUNTIME/voices" "$BOX/data"

  say "system packages (checked only: installing them needs root)"
  MISSING=""
  for c in curl git rsync cmake cc ffmpeg; do have "$c" || MISSING="$MISSING $c"; done
  if [ -n "$MISSING" ]; then
    echo "  missing:$MISSING  -> ask the box admin: sudo apt-get install -y curl git rsync cmake build-essential ffmpeg"
  else
    echo "  curl git rsync cmake cc ffmpeg: present"
  fi
  if ldconfig -p 2>/dev/null | grep -q 'libportaudio\.so'; then
    echo "  libportaudio2: present (speakerphone, A-03)"
  else
    echo "  libportaudio2: missing; only for \`raum speakerphone\` (A-03): sudo apt-get install -y libportaudio2"
  fi

  say "uv (~/.local/bin)"
  if have uv; then echo "  uv present: $(uv --version 2>/dev/null)"; else
    runsh "curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh"
  fi

  say "Node.js >= 20.19 (Vite 8)"
  if node_ok; then echo "  node $(node --version) present"; else
    echo "  node $(node --version 2>/dev/null || echo missing): too old or missing. Without root: nvm"
    echo "  (https://github.com/nvm-sh/nvm) or a nodejs.org tarball unpacked into ~/.local"
  fi

  say "shared services (checked, not installed)"
  if up http://127.0.0.1:11434/api/version; then echo "  Ollama :11434 running"
  elif [ "$PROFILE" = mock ]; then echo "  Ollama :11434 not running (not needed for mock)"
  else echo "  Ollama :11434 not running: ask the box admin, or \`ollama serve\` in user space"; fi
  if up http://127.0.0.1:7880; then echo "  LiveKit :7880 running"
  else echo "  LiveKit :7880 not running: docker run -d --name livekit --restart unless-stopped --network host livekit/livekit-server --dev --bind 0.0.0.0 --node-ip <box-ip>  (or a livekit-server binary in ~/.local/bin)"; fi
  WHISPER_UP=0
  if up http://127.0.0.1:8081; then WHISPER_UP=1; echo "  whisper :8081 running"
  else echo "  whisper :8081 not running"; fi

  say "Python environment and app build"
  run bash -c "cd '$ROOT/core' && uv sync --frozen${RAUM_UV_EXTRAS:+ $RAUM_UV_EXTRAS}"
  run bash -c "cd '$ROOT/app' && npm ci --no-audit --no-fund && npm run build"

  W="$BOX/whisper.cpp"
  WHISPER_LINE="# WHISPER_DIR=$W     # box.sh starts whisper-server on :8081 when nothing answers there"
  ASR_MODEL="$(asr_model)"
  if [ "$WHISPER" = 1 ] && [ "$PROFILE" != mock ] && [ "$WHISPER_UP" = 0 ]; then
    say "whisper.cpp ($ASR_MODEL) in $W"
    if [ -x "$W/build/bin/whisper-server" ]; then echo "  whisper-server already built"; else
      [ -d "$W/.git" ] || run git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$W"
      [ -d /usr/local/cuda/bin ] && export PATH="/usr/local/cuda/bin:$PATH"
      # shellcheck disable=SC2046
      run cmake -S "$W" -B "$W/build" $(whisper_cuda_flags) -DCMAKE_BUILD_TYPE=Release
      run cmake --build "$W/build" -j --config Release --target whisper-server
    fi
    if [ "$PULL" = 1 ]; then
      [ -f "$W/models/ggml-$ASR_MODEL.bin" ] || run bash "$W/models/download-ggml-model.sh" "$ASR_MODEL"
    else
      echo "  model ggml-$ASR_MODEL.bin: download with --pull-models (or $W/models/download-ggml-model.sh $ASR_MODEL)"
    fi
    WHISPER_LINE="WHISPER_DIR=$W     # box.sh starts whisper-server on :8081 when nothing answers there
WHISPER_MODEL=$ASR_MODEL"
  fi

  if [ "$PROFILE" != mock ]; then
    say "Ollama models for $PROFILE"
    MODELS="$(ollama_models)"
    [ "$BAKEOFF" = 1 ] && MODELS="$MODELS gemma4:e4b qwen3:30b-a3b"
    if [ "$PULL" = 1 ]; then
      for m in $MODELS; do run ollama pull "$m"; done
    else
      echo "  skipped (add --pull-models): ${MODELS//$'\n'/ }"
    fi
  fi

  say "box settings: $ENV_FILE (sourced by deploy/box.sh and deploy/push.sh)"
  write_new "$ENV_FILE" 0600 - "$(cat <<EOF
# RAUM on this box, user space (deploy/box.sh). Written once by install.sh --user-space; edit freely,
# it is never overwritten and lives outside the repo, so deploy/push.sh never touches it.
RAUM_PROFILE=$PROFILE
RAUM_DATA_DIR=$BOX/data          # encrypted sessions + audit log
RAUM_APP=1                       # serve the app on :5180 for phones (proxies /api and /rtc)
RAUM_HTTPS=1                     # self-signed https: phones only give the mic to https pages
$WHISPER_LINE
# RAUM_UV_EXTRAS="--extra speakerphone"
# RAUM_SPEAKERPHONE_ROOM=sz-4821   # A-03: the USB speakerphone joins this room
# RAUM_SPEAKERPHONE_ROLE=a         # and speaks for this role; RAUM_SPEAKERPHONE_DEVICE=<index|name>
EOF
)"
  echo "  no nft table without root: the privacy meter reads 'stub', sealed mode is state only"

  if [ "$SERVICES" = 1 ]; then
    say "services (deploy/box.sh)"
    run bash "$ROOT/deploy/box.sh" restart
  fi

  say "done ($STEPS step(s)$([ "$DRY" = 1 ] && echo ', dry run'))"
  IP="$(lan_ip)"
  cat <<EOF
  checks:
    bash $ROOT/deploy/box.sh status
    curl -fsS http://127.0.0.1:8000/api/health
    open https://${IP:-<box-ip>}:5180/admin
  from the laptop afterwards: deploy/push.sh
EOF
  exit 0
fi

# ---------------------------------------------------------------- Linux, dedicated box: prerequisites

say "Linux: system packages"
if have apt-get; then
  run $SUDO apt-get update -q
  run $SUDO apt-get install -y -q curl ca-certificates git rsync jq ffmpeg build-essential cmake nftables espeak-ng \
    libportaudio2
  if ! have docker; then
    echo "  Docker missing (DGX OS ships it). Installing the Ubuntu packages:"
    run $SUDO apt-get install -y -q docker.io docker-compose-v2
  fi
else
  echo "  no apt-get: install curl git rsync jq ffmpeg cmake nftables libportaudio2 and docker by hand"
fi

say "uv (system-wide, /usr/local/bin)"
if [ -x /usr/local/bin/uv ]; then
  echo "  uv present: $(/usr/local/bin/uv --version 2>/dev/null)"
else
  runsh "curl -LsSf https://astral.sh/uv/install.sh | $SUDO env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh"
fi

say "Node.js >= 20.19 (Vite 8)"
if node_ok; then
  echo "  node $(node --version) present"
else
  runsh "curl -fsSL https://deb.nodesource.com/setup_22.x | ${SUDO:+$SUDO -E }bash -"
  run $SUDO apt-get install -y -q nodejs
fi

if [ "$PROFILE" != mock ]; then
  say "Ollama"
  if have ollama; then echo "  ollama present"; else runsh "curl -fsSL https://ollama.com/install.sh | sh"; fi
  run $SUDO mkdir -p /etc/systemd/system/ollama.service.d
  run $SUDO install -m 0644 "$ROOT/deploy/systemd/ollama-raum.conf" /etc/systemd/system/ollama.service.d/raum.conf
  run $SUDO systemctl daemon-reload
  run $SUDO systemctl restart ollama
fi

# ---------------------------------------------------------------- user, directories, config

say "user raum and directories"
if id raum >/dev/null 2>&1; then echo "  user raum exists"; else
  run $SUDO useradd --system --create-home --home-dir /opt/raum --shell /usr/sbin/nologin raum
fi
for g in video render audio; do getent group "$g" >/dev/null 2>&1 && run $SUDO usermod -aG "$g" raum; done
run $SUDO chmod 0755 /opt/raum                       # the login user rsyncs into /opt/raum/app
run $SUDO install -d -o "$ME" -m 0755 /opt/raum/python  # uv's Python, readable by raum
run $SUDO install -d -o raum -g raum -m 0700 /var/lib/raum
run $SUDO install -d -o root -g raum -m 0750 /etc/raum
if [ "$ROOT" != "$APP" ]; then
  echo "  note: the services expect the repo at $APP (this one is $ROOT)."
  echo "        Push it there from the laptop with deploy/push.sh, or clone it there and rerun."
  run $SUDO install -d -o "$ME" -m 0755 "$APP"
fi
# RAUM_DATA_DIR: profiles may name a home-relative data_dir (the shared-box layout); here it is
# /var/lib/raum, the services' StateDirectory
write_new /etc/raum/raum.env 0640 root:raum "$(cat <<EOF
# RAUM service environment (see deploy/raum.env.example). Written by install.sh.
RAUM_PROFILE=$PROFILE
RAUM_HOME=$APP
RAUM_DATA_DIR=/var/lib/raum
EOF
)"

# ---------------------------------------------------------------- dependencies and app build

say "Python environment and app build"
as_user "cd '$ROOT/core' && /usr/local/bin/uv sync --frozen${RAUM_UV_EXTRAS:+ $RAUM_UV_EXTRAS}"
as_user "cd '$ROOT/app' && npm ci --no-audit --no-fund && npm run build"

# ---------------------------------------------------------------- whisper.cpp

ASR_MODEL="$(asr_model)"
if [ "$WHISPER" = 1 ] && [ "$PROFILE" != mock ]; then
  say "whisper.cpp ($ASR_MODEL) in /opt/raum/whisper.cpp"
  W=/opt/raum/whisper.cpp
  if [ -x "$W/build/bin/whisper-server" ]; then
    echo "  whisper-server already built"
  else
    [ -d "$W/.git" ] || { run $SUDO install -d -o "$ME" -m 0755 "$W"; as_user "git clone --depth 1 https://github.com/ggml-org/whisper.cpp '$W'"; }
    [ -d /usr/local/cuda/bin ] && export PATH="/usr/local/cuda/bin:$PATH"
    as_user "cmake -S '$W' -B '$W/build' $(whisper_cuda_flags) -DCMAKE_BUILD_TYPE=Release && cmake --build '$W/build' -j --config Release --target whisper-server"
  fi
  if [ "$PULL" = 1 ]; then
    [ -f "$W/models/ggml-$ASR_MODEL.bin" ] || as_user "bash '$W/models/download-ggml-model.sh' '$ASR_MODEL'"
  else
    echo "  model ggml-$ASR_MODEL.bin: download with --pull-models (or $W/models/download-ggml-model.sh $ASR_MODEL)"
  fi
fi

# ---------------------------------------------------------------- models

if [ "$PROFILE" != mock ]; then
  say "Ollama models for $PROFILE"
  MODELS="$(ollama_models)"
  [ "$BAKEOFF" = 1 ] && MODELS="$MODELS gemma4:e4b qwen3:30b-a3b"
  if [ "$PULL" = 1 ]; then
    for m in $MODELS; do run ollama pull "$m"; done
  else
    echo "  skipped (add --pull-models): ${MODELS//$'\n'/ }"
  fi
fi

# ---------------------------------------------------------------- services

if [ "$SERVICES" = 1 ]; then
  say "systemd units"
  UNITS="raum-nft raum-core raum-agent"
  [ "$WHISPER" = 1 ] && [ "$PROFILE" != mock ] && UNITS="raum-nft whisper raum-core raum-agent"
  for u in $UNITS; do run $SUDO install -m 0644 "$ROOT/deploy/systemd/$u.service" "/etc/systemd/system/$u.service"; done
  run $SUDO systemctl daemon-reload
  if ! id ollama >/dev/null 2>&1; then
    echo "  user ollama missing: creating it so deploy/raum.nft can load"
    run $SUDO useradd --system --no-create-home --shell /usr/sbin/nologin ollama
  fi
  # shellcheck disable=SC2086
  run $SUDO systemctl enable --now $UNITS

  say "LiveKit + Caddy (docker compose)"
  IP="$(lan_ip)"
  [ -n "$IP" ] || IP="127.0.0.1"
  write_new "$ROOT/deploy/.env" 0644 "$ME:$(id -gn "$ME" 2>/dev/null || echo "$ME")" "$(cat <<EOF
# docker compose settings (see deploy/env.example). Written by install.sh.
RAUM_HOST=$IP
RAUM_NODE_IP=$IP
RAUM_UID=$(id -u raum 2>/dev/null || echo 999)
RAUM_GID=$(id -g raum 2>/dev/null || echo 999)
EOF
)"
  run $SUDO docker compose -f "$ROOT/deploy/docker-compose.yml" up -d
fi

# ---------------------------------------------------------------- summary

say "done ($STEPS step(s)$([ "$DRY" = 1 ] && echo ', dry run'))"
IP="${IP:-$(lan_ip)}"
cat <<EOF
  checks:
    systemctl status raum-core raum-agent whisper --no-pager
    curl -fsS http://127.0.0.1:8000/api/health
    sudo nft list table inet raum
    open https://${IP:-<box-ip>}/admin   (install the local CA on team phones first, §9.3 B:
      sudo docker compose -f $ROOT/deploy/docker-compose.yml cp caddy:/data/caddy/pki/authorities/local/root.crt .)
  from the laptop afterwards: deploy/push.sh
EOF
