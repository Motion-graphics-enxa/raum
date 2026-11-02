# Running RAUM on a shared box (the hackathon GX10)

The GX10 was shared with other teams, so RAUM ran fully in user space and never touched the
global firewall, the system Tailscale or other containers.

- `livekit-run.sh` + `livekit.yaml`: the `raum-livekit` container with LAN-only STUN/TURN on the box's
  own :3478 (no Google/Twilio STUN, so no third-party bytes). Dev keys only on a private LAN.
- `raum-net.sh {load|seal|unseal|status}`: our own nft table `inet raum` scoped to the RAUM cgroup
  (`user.slice/user-1000.slice/raum`): egress counters for the privacy meter and the Seal chain.
  On a dedicated box use `deploy/raum.nft` instead.
