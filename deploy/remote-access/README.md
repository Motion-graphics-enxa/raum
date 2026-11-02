# Remote access (A-04) without touching the box's system Tailscale

`tailscaled-run.sh` starts our own `tailscaled` in userspace-networking mode (separate state dir,
socket and port), logged into the team's own tailnet, as a subnet router for the box's LAN address:

    T=~/Desktop/HackathonRaum/runtime/tailscale
    setsid nohup $T/run.sh >/dev/null 2>&1 </dev/null &
    $T/tailscale --socket=$T/tailscaled.sock up --hostname=raum-box \
      --advertise-routes=10.24.249.204/32 --accept-dns=false --accept-routes=false
    # approve the route in the admin console (Machines -> raum-box -> Edit route settings)

A remote phone with the Tailscale app (same tailnet) opens the same https://10.24.249.204:5180.
Set `RAUM_TUNNEL_SELF_IPS=10.24.249.204` so such joins show the "Tunnel" path (e18401e).
WireGuard end to end; the coordination server sees metadata only.
