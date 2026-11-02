"""Burn by key deletion (T-02, plan §6.10).

CONTRACT (implemented by the security workstream; signature is fixed).
"""

from __future__ import annotations

from pathlib import Path

from raum.events import BurnDone, now_ms
from raum.security.audit import AuditLog
from raum.security.crypto import SessionStore


def burn_session(store: SessionStore, audit: AuditLog) -> BurnDone:
    """Zero the key, delete the session file, append ``key_destroyed`` to the audit log and
    return the ``burn.done`` payload {key_destroyed_at, audit_head_hash}."""
    records = getattr(store, "record_count", None)
    path = Path(store.path)
    store.burn()
    destroyed_at = now_ms()
    entry = audit.append("key_destroyed", store.session_id,
                         {"records": records, "file_deleted": not path.exists()})
    head = entry.get("hash") if isinstance(entry, dict) else None
    return BurnDone(key_destroyed_at=destroyed_at, audit_head_hash=head or audit.head_hash())
