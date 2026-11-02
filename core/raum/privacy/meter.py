"""Privacy meter (T-06, plan §6.11).

"Bytes to third parties: 0" comes from the nftables table ``inet raum`` (``deploy/raum.nft``):
every named counter in that table counts traffic from the RAUM service users to destinations
outside the private and tailnet ranges. Counters whose name says ``tunnel``/``lan``/``local``
are not third-party traffic; counters named ``*seal*``/``*drop*``/``*blocked*`` count packets the
sealed mode dropped (they never left the box) and are reported as ``blocked_bytes``.

"Encrypted tunnel to your own devices: X KB" is read from
``/sys/class/net/tailscale0/statistics/{rx,tx}_bytes`` (counted from raum-core start).

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

_NOT_THIRD_PARTY = ("tunnel", "tailnet", "lan", "local", "private")
_BLOCKED = ("seal", "drop", "blocked")


def parse_nft_counters(raw: str | dict[str, Any]) -> tuple[int, int]:
    """``nft -j list counters table inet raum`` -> (third_party_bytes, blocked_bytes)."""
    data = json.loads(raw) if isinstance(raw, str) else raw
    third = blocked = 0
    for item in data.get("nftables", []):
        c = item.get("counter") if isinstance(item, dict) else None
        if not isinstance(c, dict):
            continue
        name = str(c.get("name", "")).lower()
        b = int(c.get("bytes", 0) or 0)
        if any(k in name for k in _BLOCKED):
            blocked += b
        elif any(k in name for k in _NOT_THIRD_PARTY):
            continue
        else:
            third += b
    return third, blocked


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


async def nft(*args: str, timeout: float = 1.0, stdin: bytes | None = None) -> tuple[int, str, str]:
    """``nft`` directly, then ``sudo -n nft`` when the first lacks CAP_NET_ADMIN."""
    rc, out, err = await run_cmd("nft", *args, timeout=timeout, stdin=stdin)
    denied = rc not in (0, 127) and ("permission" in err.lower() or "not permitted" in err.lower())
    if denied and shutil.which("sudo"):
        rc, out, err = await run_cmd("sudo", "-n", "nft", *args, timeout=timeout, stdin=stdin)
    return rc, out, err


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

    async def _third_party(self) -> tuple[int, int] | None:
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
        counters = await self._third_party()
        tunnel = self.tunnel_bytes()
        if counters is None:
            m = PrivacyMeter(third_party_bytes=0, tunnel_bytes=tunnel, sealed=sealed, source="stub")
        else:
            third, blocked = counters
            m = PrivacyMeter(third_party_bytes=third, tunnel_bytes=tunnel, sealed=sealed, source="nft",
                             blocked_bytes=blocked)
        self.last = m
        return m
