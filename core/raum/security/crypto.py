"""Session encryption (T-01, plan §6.10). AES-256-GCM, key in memory only.

CONTRACT (implemented by the security workstream; signatures are fixed).

On-disk format of ``<dir>/<session_id>.bin`` (append-only, one frame per record)::

    [4-byte big-endian length L][12-byte nonce][L bytes: AES-256-GCM ciphertext + 16-byte tag]

- the plaintext is the record as compact UTF-8 JSON;
- every record gets a fresh random 96-bit nonce (``os.urandom``);
- the associated data binds the record to its session and position:
  ``session_id (UTF-8) || 0x00 || record index (8-byte big-endian)``, so a record that is edited,
  truncated, reordered or copied in from another session fails authentication on read.

The 256-bit key lives only in a ``bytearray`` (``self._key``) in this process. ``burn()`` overwrites
it with zeros in place, drops the cipher object and deletes the file. Honest limit: CPython (and
OpenSSL inside ``cryptography``) may have made transient copies of the key that we cannot wipe;
what matters is that the key never touches the disk, so the file is useless once the process
forgets the key.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import threading
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

log = logging.getLogger("raum.security.crypto")

KEY_BYTES = 32  # AES-256
NONCE_BYTES = 12  # 96-bit GCM nonce
TAG_BYTES = 16
_LEN = struct.Struct(">I")
_HEADER = _LEN.size + NONCE_BYTES


class SessionBurned(RuntimeError):
    pass


class SessionTampered(ValueError):
    """A record failed authentication: edited, truncated, reordered or not from this session."""


class SessionStore:
    """Append-only encrypted record file ``<dir>/<session_id>.bin``.

    - 256-bit key from ``os.urandom``, held in a ``bytearray`` (never written to disk).
    - Each record: JSON -> AES-256-GCM with a fresh 96-bit nonce; AAD binds session id + record index.
    - ``burn()`` overwrites the key bytes with zeros, drops references, deletes the file.
    """

    def __init__(self, session_id: str, directory: str | Path) -> None:
        sid = str(session_id)
        if not sid or sid in (".", "..") or any(c in sid for c in "/\\\x00"):
            raise ValueError(f"invalid session id: {session_id!r}")
        self._sid = sid
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / f"{sid}.bin"
        self._lock = threading.Lock()
        self._key = bytearray(os.urandom(KEY_BYTES))
        self._aead: AESGCM | None = AESGCM(self._key)
        self._count = 0
        self._burned = False
        # A file left behind by an earlier process was encrypted with a key that died with it:
        # it can never be read again, so start clean instead of appending to garbage.
        self._path.unlink(missing_ok=True)

    @property
    def session_id(self) -> str:
        return self._sid

    @property
    def path(self) -> Path:
        return self._path

    @property
    def burned(self) -> bool:
        return self._burned

    @property
    def record_count(self) -> int:
        """Records appended so far (extension, used by ``burn_session`` for the audit line)."""
        return self._count

    def _aad(self, index: int) -> bytes:
        return self._sid.encode("utf-8") + b"\x00" + index.to_bytes(8, "big")

    def append(self, record: dict[str, Any]) -> None:
        """Encrypt and append one record. Raises SessionBurned after burn."""
        plain = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._lock:
            if self._burned or self._aead is None:
                raise SessionBurned(self._sid)
            nonce = os.urandom(NONCE_BYTES)
            ct = self._aead.encrypt(nonce, plain, self._aad(self._count))
            frame = _LEN.pack(len(ct)) + nonce + ct
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                view = memoryview(frame)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            self._count += 1

    def read_all(self) -> list[dict[str, Any]]:
        """Decrypt every record in order. Raises SessionBurned after burn, SessionTampered on a bad record."""
        with self._lock:
            if self._burned or self._aead is None:
                raise SessionBurned(self._sid)
            try:
                raw = self._path.read_bytes()
            except FileNotFoundError:
                return []
            out: list[dict[str, Any]] = []
            pos = index = 0
            while pos < len(raw):
                if pos + _HEADER > len(raw):
                    raise SessionTampered(f"{self._sid}: truncated header in record {index}")
                (n,) = _LEN.unpack_from(raw, pos)
                start = pos + _HEADER
                if n < TAG_BYTES or start + n > len(raw):
                    raise SessionTampered(f"{self._sid}: bad length in record {index}")
                nonce = raw[pos + _LEN.size:start]
                try:
                    plain = self._aead.decrypt(nonce, raw[start:start + n], self._aad(index))
                except InvalidTag:
                    raise SessionTampered(f"{self._sid}: record {index} failed authentication") from None
                out.append(json.loads(plain))
                pos = start + n
                index += 1
            return out

    def burn(self) -> None:
        """Zero the key in place, drop the cipher, delete the file. Idempotent."""
        with self._lock:
            for i in range(len(self._key)):
                self._key[i] = 0
            self._aead = None
            self._burned = True
            try:
                self._path.unlink(missing_ok=True)
            except OSError as e:  # the key is gone either way; the file is unreadable without it
                log.error("could not delete %s after burn: %s", self._path, e)

    def __repr__(self) -> str:  # never show key material
        return f"SessionStore({self._sid!r}, records={self._count}, burned={self._burned})"
