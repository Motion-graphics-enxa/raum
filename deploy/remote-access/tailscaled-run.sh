#!/bin/bash
# RAUM remote access (A-04): our own userspace tailscaled, separate from the system one (not ours).
T=$HOME/Desktop/HackathonRaum/runtime/tailscale
exec $T/tailscaled --tun=userspace-networking --statedir=$T/state --socket=$T/tailscaled.sock --port=0 >> $T/tailscaled.log 2>&1
