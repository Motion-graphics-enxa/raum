"""Test doubles for raum-core tests.

``install_security`` probes ``raum.security`` (crypto, audit, verify_chain, burn). If any part is
still the contract stub, all four are replaced in ``raum.sessions`` by small file-based doubles that
follow the same contract; once the real implementation lands, the tests run against it unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from raum.config import RaumConfig, load_config
from raum.events import BurnDone, Envelope, now_ms
from raum.security.audit import GENESIS, VerifyReport
from raum.security.crypto import SessionBurned

FORBIDDEN_META = {"text", "transcript", "translation", "summary", "content"}


class FakeStore:
    """``<dir>/<id>.bin``; records XORed with a key stream so no plaintext touches the disk."""

    def __init__(self, session_id: str, directory: str | Path) -> None:
        self._sid = session_id
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{session_id}.bin"
        self._key = bytearray(os.urandom(32))
        self._burned = False
        self._n = 0

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def path(self) -> Path:
        return self._path

    @property
    def burned(self) -> bool:
        return self._burned

    def _xor(self, data: bytes, i: int) -> bytes:
        stream = b""
        c = 0
        while len(stream) < len(data):
            stream += hashlib.sha256(bytes(self._key) + i.to_bytes(8, "big") + c.to_bytes(4, "big")).digest()
            c += 1
        return bytes(a ^ b for a, b in zip(data, stream, strict=False))

    def append(self, record: dict[str, Any]) -> None:
        if self._burned:
            raise SessionBurned(self._sid)
        blob = self._xor(json.dumps(record).encode(), self._n)
        with self._path.open("ab") as f:
            f.write(base64.b64encode(blob) + b"\n")
        self._n += 1

    def read_all(self) -> list[dict[str, Any]]:
        if self._burned:
            raise SessionBurned(self._sid)
        if not self._path.exists():
            return []
        lines = self._path.read_bytes().splitlines()
        return [json.loads(self._xor(base64.b64decode(line), i)) for i, line in enumerate(lines)]

    def burn(self) -> None:
        for i in range(len(self._key)):
            self._key[i] = 0
        self._burned = True
        self._path.unlink(missing_ok=True)


def _hash(prev: str, line: dict[str, Any]) -> str:
    canon = json.dumps(line, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256((prev + canon).encode()).hexdigest()


class FakeAudit:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _lines(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(x) for x in self.path.read_text(encoding="utf-8").splitlines() if x.strip()]

    def head_hash(self) -> str:
        lines = self._lines()
        return lines[-1]["hash"] if lines else GENESIS

    def append(self, event: str, session: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        meta = dict(meta or {})
        bad = set(meta) & FORBIDDEN_META
        if bad:
            raise ValueError(f"content-like meta keys: {sorted(bad)}")
        line = {"ts": now_ms(), "session": session, "event": event, "meta": meta, "prev": self.head_hash()}
        entry = {**line, "hash": _hash(line["prev"], line)}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return entry

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        return [e for e in self._lines() if session is None or e.get("session") == session]


def fake_verify_chain(path: str | Path) -> VerifyReport:
    p = Path(path)
    lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []
    prev = GENESIS
    for i, raw in enumerate(lines, 1):
        e = json.loads(raw)
        line = {k: v for k, v in e.items() if k != "hash"}
        if line.get("prev") != prev or _hash(prev, line) != e.get("hash"):
            return VerifyReport(ok=False, lines=len(lines), first_bad=i, reason="hash mismatch")
        prev = e["hash"]
    return VerifyReport(ok=True, lines=len(lines))


def fake_burn(store: Any, audit: Any) -> BurnDone:
    store.burn()
    audit.append("key_destroyed", store.session_id, {})
    return BurnDone(key_destroyed_at=now_ms(), audit_head_hash=audit.head_hash())


def install_security(monkeypatch: Any, tmp_path: Path) -> bool:
    """Returns True when the real raum.security implementation is in use."""
    import raum.sessions as S

    probe = tmp_path / "security-probe"
    probe.mkdir(parents=True, exist_ok=True)
    try:
        store = S.SessionStore("probe", probe)
        store.append({"type": "probe"})
        audit = S.AuditLog(probe / "audit.jsonl")
        audit.append("probe", None, {})
        S.burn_session(store, audit)
        S.verify_chain(probe / "audit.jsonl")
        return True
    except NotImplementedError:
        monkeypatch.setattr(S, "SessionStore", FakeStore)
        monkeypatch.setattr(S, "AuditLog", FakeAudit)
        monkeypatch.setattr(S, "burn_session", fake_burn)
        monkeypatch.setattr(S, "verify_chain", fake_verify_chain)
        return False


class FakePublisher:
    """Stands in for LiveKitPublisher (RoomService.SendData)."""

    def __init__(self) -> None:
        self.events: list[Envelope] = []

    def publish(self, env: Envelope) -> None:
        self.events.append(env)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of(self, type: str) -> list[Envelope]:
        return [e for e in self.events if e.type == type]

    async def aclose(self) -> None:
        pass


def make_config(tmp_path: Path) -> RaumConfig:
    cfg = load_config()
    cfg.security.data_dir = str(tmp_path / "data")
    cfg.security.audit_log = str(tmp_path / "data" / "audit.jsonl")
    cfg.network.livekit_url = "auto"
    return cfg


def ev(type: str, room: str, data: dict[str, Any], utt: str | None = None) -> dict[str, Any]:
    """An agent -> core link message carrying one event."""
    return {"kind": "event", "event": {"v": 1, "type": type, "room": room, "utt": utt, "ts": now_ms(),
                                       "data": data}}


def recv_until(ws: Any, type: str, *, max_msgs: int = 400, pred: Any = None) -> dict[str, Any]:
    """Read envelopes from an /api/events socket until one of ``type`` (skips meter/gpu ticks)."""
    for _ in range(max_msgs):
        m = ws.receive_json()
        if m.get("type") == type and (pred is None or pred(m)):
            return m
    raise AssertionError(f"no {type} within {max_msgs} messages")
