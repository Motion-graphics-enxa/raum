#!/bin/bash
# Bring RAUM back on the shared GX10 after a reboot or power-off. Usage: raum-up.sh [--no-app]
# Order: Ollama + models, whisper x2, LiveKit + firewall table, voice x2, tailscale, RAUM app.
# Lives at ~/Desktop/HackathonRaum/runtime/raum-up.sh on the box (copy of this file).
set -u
R=$HOME/Desktop/HackathonRaum/runtime; W=$HOME/raum-runtime/whisper.cpp
cd $HOME/raum-runtime
echo "== ollama"
if ! curl -fs -m 2 127.0.0.1:11434/api/version >/dev/null; then source ./ollama-env.sh; nohup ollama serve >> ollama-serve.log 2>&1 < /dev/null & fi
for i in $(seq 1 60); do curl -fs -m 2 127.0.0.1:11434/api/version >/dev/null && break; sleep 0.5; done
curl -s -m 180 127.0.0.1:11434/api/generate -d "{\"model\":\"gemma4:e4b\",\"prompt\":\"hi\",\"think\":false,\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_ctx\":8192,\"num_predict\":1}}" -o /dev/null
curl -s -m 300 127.0.0.1:11434/api/generate -d "{\"model\":\"gpt-oss:20b\",\"prompt\":\"hi\",\"stream\":false,\"keep_alive\":-1,\"options\":{\"num_ctx\":8192,\"num_predict\":1}}" -o /dev/null
curl -s -m 120 127.0.0.1:11434/api/embed -d "{\"model\":\"bge-m3\",\"input\":\"hi\",\"keep_alive\":-1}" -o /dev/null
curl -s 127.0.0.1:11434/api/ps | python3 -c "import sys,json; print([m[\"name\"] for m in json.load(sys.stdin)[\"models\"]])"
echo "== whisper :8081 (partials) and :8082 (finals), both -nlp"
for p in 8081 8082; do
  curl -fs -m 2 -o /dev/null 127.0.0.1:$p/ || (cd $W && setsid nohup ./build/bin/whisper-server -m models/ggml-large-v3-turbo.bin --host 127.0.0.1 --port $p -t 8 -nlp >> $HOME/raum-runtime/whisper-server-$p.log 2>&1 < /dev/null &)
done
sleep 6; for p in 8081 8082; do curl -s -o /dev/null -w "whisper $p %{http_code}\n" 127.0.0.1:$p/; done
echo "== livekit + nft table"
docker ps --format "{{.Names}}" | grep -qx raum-livekit || docker start raum-livekit >/dev/null || bash $R/livekit-run.sh
bash $R/raum-net.sh load 2>&1 | tail -1
echo "== voice :8090 + :8091"
cd $R/raum-tts && ./start.sh 2>&1 | grep -o "\"version\":\"[0-9.]*\"" ; PORT=8091 ./start.sh 2>&1 | grep -o "\"version\":\"[0-9.]*\""
docker update --cpu-shares 10240 raum-tts-svc raum-tts-svc-8091 raum-livekit >/dev/null 2>&1
echo "== tailscale (our own, userspace)"
T=$R/tailscale; pgrep -f "tailscaled --tun=userspace" >/dev/null || setsid bash $T/run.sh < /dev/null > /dev/null 2>&1 &
if [ "${1:-}" != "--no-app" ]; then
  echo "== RAUM app"; cd $HOME/Desktop/HackathonRaum/raum && bash deploy/box.sh start 2>&1 | tail -3
  echo 10000 | sudo -n tee /sys/fs/cgroup/user.slice/user-1000.slice/raum/cpu.weight >/dev/null 2>&1
  for p in $(pgrep -x whisper-server); do echo $p | sudo -n tee /sys/fs/cgroup/user.slice/user-1000.slice/raum/cgroup.procs >/dev/null 2>&1; done
fi
echo "== done"; free -g | sed -n 2p
