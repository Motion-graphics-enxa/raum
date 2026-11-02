# Running RAUM on a shared box (the hackathon GX10)

The GX10 was shared with other teams, so RAUM ran fully in user space and never touched the
global firewall, the system Tailscale or other containers.

- `livekit-run.sh` + `livekit.yaml`: the `raum-livekit` container with LAN-only STUN/TURN on the box's
  own :3478 (no Google/Twilio STUN, so no third-party bytes). Dev keys only on a private LAN.
- `raum-net.sh {load|seal|unseal|status}`: our own nft table `inet raum` scoped to the RAUM cgroup
  (`user.slice/user-1000.slice/raum`): egress counters for the privacy meter and the Seal chain.
  On a dedicated box use `deploy/raum.nft` instead.

## After a reboot
`raum-up.sh` brings everything back in order: Ollama with gemma4:e4b, gpt-oss:20b and bge-m3 (num_ctx 8192, keep_alive -1); whisper on :8081 and :8082 (-nlp); LiveKit and `raum-net.sh load`; both voice instances; our tailscale; the app; the cgroup weight. The box copy is runtime/raum-up.sh. Pass `--no-app` to leave the app stopped.
