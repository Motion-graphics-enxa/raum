"""Tamper-evident audit log (T-04, plan §6.10). SHA-256 hash chain. NO CONTENT EVER.

Each line: ``{ts, session, event, meta, prev, hash}`` with
``hash = sha256(prev + canonical_json(line_without_hash))``; the first line's prev is 64 zeros.

Events: session_start, consent, models, off_record_on, off_record_off, summary_approved,
key_destroyed (and: session_end, sealed_on, sealed_off, human_interpreter, burn_all).

CONTRACT (implemented by the security workstream; signatures are fixed).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS = "0" * 64


@dataclass
class VerifyReport:
    ok: bool
    lines: int
    first_bad: int | None = None  # 1-based line number
    reason: str | None = None


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        raise NotImplementedError

    def append(self, event: str, session: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one entry and return it (with hash). Must reject content-like meta keys
        (text, transcript, translation, summary...)."""
        raise NotImplementedError

    def head_hash(self) -> str:
        raise NotImplementedError

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        raise NotImplementedError


def verify_chain(path: str | Path) -> VerifyReport:
    raise NotImplementedError
