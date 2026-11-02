"""Tamper-evident audit log (T-04, plan §6.10). SHA-256 hash chain. NO CONTENT EVER.

Each line: ``{ts, session, event, meta, prev, hash}`` with
``hash = sha256(prev + canonical_json(line_without_hash))``; the first line's prev is 64 zeros.

Events: session_start, consent, models, off_record_on, off_record_off, summary_approved,
key_destroyed (and: session_end, sealed_on, sealed_off, human_interpreter, burn_all).

CONTRACT (implemented by the security workstream; signatures are fixed).

Notes:

- ``canonical_json`` = ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)``;
  the hash input is ``(prev + canonical_json(line)).encode("utf-8")``.
- Appends are serialised by a thread lock and an exclusive ``flock`` on the file, and every append
  reads the chain head from the file itself, so several ``AuditLog`` objects (or a restarted core)
  on the same file keep one chain. Each line is fsynced.
- Meta keys that could carry conversation content (:data:`FORBIDDEN_META_KEYS`, compared
  case-insensitively, top level) are rejected with ``ValueError``; so are long string values.
- The chain detects edited, deleted, inserted and reordered lines. Cutting lines off the *end* is
  only visible against a head hash kept elsewhere (``burn.done`` carries ``audit_head_hash``;
  ``raum audit verify --expect-head`` checks it).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:  # POSIX (the box and dev Macs); without it we still have the thread lock
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

from raum.events import now_ms

GENESIS = "0" * 64

FORBIDDEN_META_KEYS = frozenset({
    "text", "transcript", "translation", "summary", "content", "src", "tgt", "utterance",
    "transcripts", "translations", "utterances", "back_translation", "delta", "partial", "speech", "audio",
})
MAX_META_STRING = 512  # meta values are identifiers and numbers, never sentences
FIELDS = ("ts", "session", "event", "meta", "prev", "hash")
_TAIL_CHUNK = 64 * 1024


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def entry_hash(prev: str, line: dict[str, Any]) -> str:
    """``sha256(prev + canonical_json(line_without_hash))`` as hex."""
    body = {k: v for k, v in line.items() if k != "hash"}
    return hashlib.sha256((prev + canonical_json(body)).encode("utf-8")).hexdigest()


@dataclass
class VerifyReport:
    ok: bool
    lines: int
    first_bad: int | None = None  # 1-based line number
    reason: str | None = None


def _check_meta(meta: dict[str, Any] | None) -> dict[str, Any]:
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise TypeError("audit meta must be a dict")
    bad = sorted(k for k in meta if str(k).casefold() in FORBIDDEN_META_KEYS)
    if bad:
        raise ValueError(f"audit meta must never carry content; rejected keys: {bad}")

    def walk(v: Any) -> None:
        if isinstance(v, str) and len(v) > MAX_META_STRING:
            raise ValueError("audit meta values must be short identifiers, never content")
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, (list, tuple)):
            for x in v:
                walk(x)

    walk(meta)
    out = dict(meta)
    try:
        canonical_json(out)
    except (TypeError, ValueError) as e:
        raise ValueError(f"audit meta must be JSON-serialisable: {e}") from None
    return out


def _last_hash(f: Any) -> tuple[str, bool]:
    """(hash of the last well-formed line, file needs a newline before the next line)."""
    f.seek(0, os.SEEK_END)
    size = f.tell()
    if size == 0:
        return GENESIS, False
    f.seek(size - 1)
    needs_nl = f.read(1) != b"\n"
    start = max(0, size - _TAIL_CHUNK)
    while True:
        f.seek(start)
        chunk = f.read(size - start)
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # first piece may be a partial line
        for raw in reversed(lines):
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            h = e.get("hash") if isinstance(e, dict) else None
            if isinstance(h, str) and len(h) == 64:
                return h, needs_nl
        if start == 0:
            return GENESIS, needs_nl
        start = max(0, start - _TAIL_CHUNK * 4)


def read_entries(path: str | Path, session: str | None = None) -> list[dict[str, Any]]:
    """Parse the log without creating anything. Unparseable lines are skipped (``verify_chain`` reports them)."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return []
    out = []
    for line in raw.split(b"\n"):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if isinstance(e, dict) and (session is None or e.get("session") == session):
            out.append(e)
    return out


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return self._path

    def append(self, event: str, session: str | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
        """Append one entry and return it (with hash). Must reject content-like meta keys
        (text, transcript, translation, summary...)."""
        if not isinstance(event, str) or not event:
            raise ValueError("audit event must be a non-empty string")
        if session is not None and not isinstance(session, str):
            raise ValueError("audit session must be a string or None")
        clean = _check_meta(meta)
        with self._lock, self._path.open("a+b") as f:
            if fcntl is not None:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                prev, needs_nl = _last_hash(f)
                line = {"ts": now_ms(), "session": session, "event": event, "meta": clean, "prev": prev}
                entry = {**line, "hash": entry_hash(prev, line)}
                data = canonical_json(entry).encode("utf-8") + b"\n"
                f.write((b"\n" if needs_nl else b"") + data)
                f.flush()
                os.fsync(f.fileno())
            finally:
                if fcntl is not None:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return dict(entry)

    def head_hash(self) -> str:
        with self._lock:
            try:
                with self._path.open("rb") as f:
                    return _last_hash(f)[0]
            except FileNotFoundError:
                return GENESIS

    def entries(self, session: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            return read_entries(self._path, session)


def verify_chain(path: str | Path) -> VerifyReport:
    """Recompute the chain. Detects edited, deleted, inserted, reordered and malformed lines."""
    p = Path(path)
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return VerifyReport(ok=True, lines=0)
    lines = raw.split(b"\n")
    if lines and lines[-1] == b"":
        lines.pop()
    total = len(lines)

    def bad(no: int, reason: str) -> VerifyReport:
        return VerifyReport(ok=False, lines=total, first_bad=no, reason=reason)

    prev = GENESIS
    for no, data in enumerate(lines, 1):
        if not data.strip():
            return bad(no, "empty line")
        try:
            e = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return bad(no, "invalid JSON")
        if not isinstance(e, dict):
            return bad(no, "not a JSON object")
        missing = [k for k in FIELDS if k not in e]
        if missing:
            return bad(no, f"missing fields: {', '.join(missing)}")
        extra = sorted(set(e) - set(FIELDS))
        if extra:
            return bad(no, f"unexpected fields: {', '.join(extra)}")
        if e["prev"] != prev:
            return bad(no, "prev does not match the previous line's hash (line deleted, inserted or reordered)")
        if entry_hash(prev, e) != e["hash"]:
            return bad(no, "hash mismatch (line edited)")
        prev = e["hash"]
    return VerifyReport(ok=True, lines=total)
