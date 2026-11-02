"""Burn by key deletion (T-02, plan §6.10).

CONTRACT (implemented by the security workstream; signature is fixed).
"""

from __future__ import annotations

from raum.events import BurnDone
from raum.security.audit import AuditLog
from raum.security.crypto import SessionStore


def burn_session(store: SessionStore, audit: AuditLog) -> BurnDone:
    """Zero the key, delete the session file, append ``key_destroyed`` to the audit log and
    return the ``burn.done`` payload {key_destroyed_at, audit_head_hash}."""
    raise NotImplementedError
