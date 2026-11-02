"""Sealed mode (T-07, plan §6.11): the firewall drops all outbound third-party traffic from RAUM.

Interface with the nftables table ``inet raum`` (``deploy/raum.nft``; on the shared GX10 a
cgroup-scoped table from ``runtime/raum-net.sh``): every packet from a RAUM process to an address
that is not loopback, LAN or tailnet passes ``jump seal`` right before the ``third_party`` counter.
The chain ``seal`` is empty while open. Sealing puts exactly one rule into it; unsealing flushes it,
which removes only that rule. Each is one atomic nft transaction, passed as ONE argv string (not
``nft -f -``: under uvloop the child's stdin is a socket and nft 1.0.9 refuses it, "Not a regular
file: /dev/stdin"):

    seal on:   nft 'flush chain inet raum seal; add rule inet raum seal counter name "sealed_drops"
                    drop comment "raum-seal"'
    seal off:  nft 'flush chain inet raum seal'
    state:     nft -j list chain inet raum seal   (sealed = a rule with a drop verdict)

LAN, loopback and the tailnet return before ``jump seal``, so phones keep working. Reloading the
table unseals it. Off Linux, or without the table, the state is recorded only (``applied: false``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from raum.privacy.meter import nft, nft_available

log = logging.getLogger("raum.privacy.seal")

SEAL_CHAIN = "seal"
SEAL_COMMENT = "raum-seal"
DROP_COUNTER = "sealed_drops"


def seal_command(sealed: bool, *, family: str = "inet", table: str = "raum", chain: str = SEAL_CHAIN) -> str:
    """One nft batch (a single argv string) that seals (one drop rule) or unseals (empty chain)."""
    cmds = [f"flush chain {family} {table} {chain}"]
    if sealed:
        cmds.append(f'add rule {family} {table} {chain} '
                    f'counter name "{DROP_COUNTER}" drop comment "{SEAL_COMMENT}"')
    return "; ".join(cmds)


def chain_sealed(listing: str | dict[str, Any]) -> bool:
    """``nft -j list chain inet raum seal`` -> True when a rule in it drops."""
    data = json.loads(listing) if isinstance(listing, str) else listing
    for item in data.get("nftables", []):
        rule = item.get("rule") if isinstance(item, dict) else None
        if isinstance(rule, dict) and any(isinstance(e, dict) and "drop" in e for e in rule.get("expr", [])):
            return True
    return False


class SealController:
    def __init__(self, *, table: str = "raum", family: str = "inet", chain: str = SEAL_CHAIN) -> None:
        self.table = table
        self.family = family
        self.chain = chain
        self.sealed = False
        self.applied = False
        self.method = "nft" if nft_available() else "state-only"
        self.error: str | None = None

    def status(self) -> dict[str, Any]:
        return {"sealed": self.sealed, "applied": self.applied, "method": self.method, "error": self.error}

    async def set(self, on: bool) -> dict[str, Any]:
        self.sealed = bool(on)
        self.applied = False
        self.error = None
        if not nft_available():
            self.method = "state-only"
            return self.status()
        self.method = "nft"
        cmd = seal_command(self.sealed, family=self.family, table=self.table, chain=self.chain)
        rc, _, err = await nft(cmd, timeout=2.0)
        if rc != 0:
            self.error = f"nft failed: {err.strip() or rc}"
            log.warning("sealed mode not applied: %s", self.error)
            return self.status()
        # read back: the kernel ruleset is the truth
        rc, out, err = await nft("-j", "list", "chain", self.family, self.table, self.chain, timeout=2.0)
        try:
            actual = chain_sealed(out) if rc == 0 else None
        except ValueError:
            actual = None
        if actual is not None and actual != self.sealed:
            self.error = f"chain {self.family} {self.table} {self.chain} is {'sealed' if actual else 'open'}"
            log.warning("sealed mode not applied: %s", self.error)
            return self.status()
        self.applied = True
        log.info("sealed mode %s", "ON (outbound to third parties dropped)" if self.sealed else "off")
        return self.status()
