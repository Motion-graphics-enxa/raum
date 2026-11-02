"""Privacy meter (T-06, plan §6.11).

"Bytes to third parties: 0" comes from the nftables table ``inet raum`` (``deploy/raum.nft``, or
the cgroup-scoped table of the shared box): every named counter in that table counts traffic from
RAUM's own processes to destinations outside the private and tailnet ranges. Counters named
``*seal*``/``*drop*``/``*blocked*`` count packets the sealed mode dropped (they never left the box)
and are reported as ``blocked_bytes``. Counters named ``*tunnel*`` (e.g. ``tunnel_tx``/``tunnel_rx``)
count RAUM's own bytes on tailscale0; ``lan``/``local``/``private`` counters are ignored.

"Encrypted tunnel to your own devices: X KB" is the sum of the ``tunnel*`` counters when the
table has them (since the table was loaded), else ``/sys/class/net/tailscale0/statistics/{rx,tx}_bytes``
(whole interface, counted from raum-core start).

Off Linux (or without ``nft``) the meter reports zeros with ``source: "stub"``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from raum.events import PrivacyMeter

log = logging.getLogger("raum.privacy.meter")

_TUNNEL = ("tunnel", "tailnet")
_NOT_THIRD_PARTY = ("lan", "local", "private")
_BLOCKED = ("seal", "drop", "blocked")


def parse_nft_counters(raw: str | dict[str, Any]) -> tuple[int, int, int | None]:
    """``nft -j list counters table inet raum`` -> (third_party_bytes, blocked_bytes, tunnel_bytes).

    ``tunnel_bytes`` is None when the table has no ``tunnel*`` counter (then sysfs is used).
    """
    data = json.loads(raw) if isinstance(raw, str) else raw
    third = blocked = 0
    tunnel: int | None = None
    for item in data.get("nftables", []):
        c = item.get("counter") if isinstance(item, dict) else None
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).lower()
        b = int(c.get("bytes", 0) or 0)
        if any(k in name for k in _BLOCKED):
            blocked += b
        elif any(k in name for k in _TUNNEL):
            tunnel = (tunnel or 0) + b
        elif any(k in name for k in _NOT_THIRD_PARTY):
            continue
        else:
            third += b
    return third, blocked, tunnel


async def run_cmd(*argv: str, timeout: float = 1.0, stdin: bytes | None = None) -> tuple[int, str, str]:
    """Run a command without a shell. Returns (returncode, stdout, stderr); 127 if not found."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, PermissionError) as e:
        return 127, "", str(e)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", "timeout"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


_needs_sudo = False  # set after the first "not permitted": then skip the doomed direct call


async def nft(*args: str, timeout: float = 1.0, stdin: bytes | None = None) -> tuple[int, str, str]:
    """``nft`` directly, then ``sudo -n nft`` when the first lacks CAP_NET_ADMIN."""
    global _needs_sudo
    if not _needs_sudo:
        rc, out, err = await run_cmd("nft", *args, timeout=timeout, stdin=stdin)
        denied = rc not in (0, 127) and ("permission" in err.lower() or "not permitted" in err.lower())
        if not denied or not shutil.which("sudo"):
            return rc, out, err
        _needs_sudo = True
    return await run_cmd("sudo", "-n", "nft", *args, timeout=timeout, stdin=stdin)


def nft_available() -> bool:
    return sys.platform.startswith("linux") and shutil.which("nft") is not None


class PrivacyMeterReader:
    def __init__(self, *, table: str = "raum", family: str = "inet", tunnel_iface: str | None = "tailscale0",
                 sysfs: str | Path = "/sys/class/net") -> None:
        self.table = table
        self.family = family
        self.tunnel_iface = tunnel_iface
        self.sysfs = Path(sysfs)
        self._tunnel_base: int | None = None
        self._nft_retry_at = 0.0  # back off after a failure instead of spawning nft every second
        self.last: PrivacyMeter | None = None

    @classmethod
    def from_config(cls, tunnel: str | None) -> PrivacyMeterReader:
        iface = None
        if tunnel:
            iface = "tailscale0" if tunnel == "tailscale" else tunnel
        return cls(tunnel_iface=iface)

    def _tunnel_total(self) -> int | None:
        if not self.tunnel_iface:
            return None
        base = self.sysfs / self.tunnel_iface / "statistics"
        try:
            return int((base / "rx_bytes").read_text().strip()) + int((base / "tx_bytes").read_text().strip())
        except (OSError, ValueError):
            return None

    def tunnel_bytes(self) -> int:
        total = self._tunnel_total()
        if total is None:
            return 0
        if self._tunnel_base is None or total < self._tunnel_base:  # interface restarted
            self._tunnel_base = total
        return total - self._tunnel_base

    async def _counters(self) -> tuple[int, int, int | None] | None:
        if not nft_available() or time.monotonic() < self._nft_retry_at:
            return None
        rc, out, err = await nft("-j", "list", "counters", "table", self.family, self.table)
        if rc != 0:
            log.debug("nft counters unavailable (rc=%s): %s", rc, err.strip())
            self._nft_retry_at = time.monotonic() + 30
            return None
        try:
            return parse_nft_counters(out)
        except (ValueError, TypeError) as e:
            log.warning("could not parse nft counters: %s", e)
            return None

    async def read(self, sealed: bool) -> PrivacyMeter:
        counters = await self._counters()
        if counters is None:
            m = PrivacyMeter(third_party_bytes=0, tunnel_bytes=self.tunnel_bytes(), sealed=sealed,
                             source="stub")
        else:
            third, blocked, tunnel = counters
            m = PrivacyMeter(third_party_bytes=third, sealed=sealed, source="nft", blocked_bytes=blocked,
                             tunnel_bytes=self.tunnel_bytes() if tunnel is None else tunnel)
        self.last = m
        return m
