"""Session encryption (T-01, plan §6.10). AES-256-GCM, key in memory only.

CONTRACT (implemented by the security workstream; signatures are fixed).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class SessionBurned(RuntimeError):
    pass


class SessionStore:
    """Append-only encrypted record file ``<dir>/<session_id>.bin``.

    - 256-bit key from ``os.urandom``, held in a ``bytearray`` (never written to disk).
    - Each record: JSON -> AES-256-GCM with a fresh 96-bit nonce; AAD binds session id + record index.
    - ``burn()`` overwrites the key bytes with zeros, drops references, deletes the file.
    """

    def __init__(self, session_id: str, directory: str | Path) -> None:
        raise NotImplementedError

    @property
    def session_id(self) -> str: ...

    @property
    def path(self) -> Path: ...

    @property
    def burned(self) -> bool: ...

    def append(self, record: dict[str, Any]) -> None:
        """Encrypt and append one record. Raises SessionBurned after burn."""
        raise NotImplementedError

    def read_all(self) -> list[dict[str, Any]]:
        """Decrypt every record in order. Raises SessionBurned after burn."""
        raise NotImplementedError

    def burn(self) -> None:
        raise NotImplementedError
