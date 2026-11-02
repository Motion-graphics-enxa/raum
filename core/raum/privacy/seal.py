"""Sealed mode (T-07, plan §6.11): the firewall drops all outbound traffic from RAUM services.

Interface with ``deploy/raum.nft``: table ``inet raum`` holds one rule with the comment
``raum-seal`` that matches outbound third-party traffic (users raum/ollama, destinations not
private and not tailnet). Core flips that rule's verdict in place (``nft -j`` replace by handle):

    sealed  -> the rule ends with ``drop``
    open    -> the rule has no verdict (it only counts, if it has a counter)

Off Linux, or without the rule, the state is recorded only (``applied: false``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from raum.privacy.meter import nft, nft_available

log = logging.getLogger("raum.privacy.seal")

SEAL_COMMENT = "raum-seal"
_VERDICTS = ("accept", "drop", "reject", "return", "continue")


def flip_expr(expr: list[dict[str, Any]], sealed: bool) -> list[dict[str, Any]]:
    """Return the rule expression with its verdict set for ``sealed``."""
    out = [e for e in expr if not (isinstance(e, dict) and len(e) == 1 and next(iter(e)) in _VERDICTS)]
    if sealed:
        out.append({"drop": None})
    return out


def find_seal_rule(ruleset: str | dict[str, Any], comment: str = SEAL_COMMENT) -> dict[str, Any] | None:
    data = json.loads(ruleset) if isinstance(ruleset, str) else ruleset
    for item in data.get("nftables", []):
        rule = item.get("rule") if isinstance(item, dict) else None
        if isinstance(rule, dict) and rule.get("comment") == comment:
            return rule
    return None


def replace_command(rule: dict[str, Any], sealed: bool) -> dict[str, Any]:
    new = {k: rule[k] for k in ("family", "table", "chain", "handle") if k in rule}
    new["expr"] = flip_expr(rule.get("expr", []), sealed)
    if rule.get("comment"):
        new["comment"] = rule["comment"]
    return {"nftables": [{"replace": {"rule": new}}]}


class SealController:
    def __init__(self, *, table: str = "raum", family: str = "inet", comment: str = SEAL_COMMENT) -> None:
        self.table = table
        self.family = family
        self.comment = comment
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
        rc, out, err = await nft("-j", "-a", "list", "table", self.family, self.table, timeout=2.0)
        if rc != 0:
            self.error = f"nft list failed: {err.strip() or rc}"
            log.warning("sealed mode not applied: %s", self.error)
            return self.status()
        try:
            rule = find_seal_rule(out, self.comment)
        except ValueError as e:
            rule, self.error = None, f"cannot parse ruleset: {e}"
        if rule is None:
            self.error = self.error or f"no rule with comment '{self.comment}' in table {self.family} {self.table}"
            log.warning("sealed mode not applied: %s", self.error)
            return self.status()
        payload = json.dumps(replace_command(rule, self.sealed)).encode()
        rc, _, err = await nft("-j", "-f", "-", timeout=2.0, stdin=payload)
        if rc != 0:
            self.error = f"nft replace failed: {err.strip() or rc}"
            log.warning("sealed mode not applied: %s", self.error)
            return self.status()
        self.applied = True
        log.info("sealed mode %s", "ON (outbound dropped)" if self.sealed else "off")
        return self.status()
