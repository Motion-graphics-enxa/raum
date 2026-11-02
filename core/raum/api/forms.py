"""Uploads without temp files: a bounded body reader and a small in-memory ``multipart/form-data`` parser.

Starlette's ``UploadFile`` (python-multipart) spools anything over 1 MB to a temporary file on disk.
A photographed document must never touch the disk unencrypted, so uploads are read into memory with a
size cap and split here. Enough of RFC 7578 for browsers, curl and httpx: one part per field,
``Content-Disposition: form-data; name=...; filename=...``, optional ``Content-Type``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fastapi import HTTPException, Request

_PARAM = re.compile(r';\s*([a-zA-Z0-9_*-]+)\s*=\s*(?:"((?:[^"\\]|\\.)*)"|([^;\s]*))')


@dataclass
class Part:
    name: str
    data: bytes
    filename: str | None = None
    content_type: str | None = None

    def text(self) -> str:
        return self.data.decode("utf-8", errors="replace").strip()


async def read_body(request: Request, limit: int) -> bytes:
    """The request body, refusing (413) anything over ``limit`` bytes before reading it all."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, f"upload too large (max {limit // (1024 * 1024)} MB)")
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if len(buf) > limit:
            raise HTTPException(413, f"upload too large (max {limit // (1024 * 1024)} MB)")
    return bytes(buf)


def header_params(value: str) -> tuple[str, dict[str, str]]:
    """``form-data; name="image"; filename="a.jpg"`` -> ``("form-data", {"name": "image", ...})``."""
    head, _, _ = value.partition(";")
    params = {}
    for m in _PARAM.finditer(value[len(head):]):
        v = m.group(2) if m.group(2) is not None else m.group(3) or ""
        params[m.group(1).lower()] = v.replace('\\"', '"')
    return head.strip().lower(), params


def is_multipart(content_type: str | None) -> bool:
    return (content_type or "").lower().startswith("multipart/form-data")


def _next_delimiter(body: bytes, delim: bytes, start: int) -> int:
    """Index of the next ``CRLF--boundary`` that really is a delimiter: followed by ``--`` (the last
    one) or by optional transport padding and CRLF."""
    needle = b"\r\n" + delim
    pos = body.find(needle, start)
    while pos >= 0:
        after = pos + len(needle)
        rest = body[after:after + 2]
        if rest == b"--" or body[after:after + 64].lstrip(b" \t").startswith(b"\r\n"):
            return pos
        pos = body.find(needle, pos + 1)
    return -1


def parse_multipart(body: bytes, content_type: str) -> dict[str, Part]:
    """Fields by name (the first part wins when a name repeats). Raises 400 on a malformed body."""
    _, params = header_params(content_type)
    boundary = params.get("boundary")
    if not boundary:
        raise HTTPException(400, "multipart body without a boundary")
    delim = b"--" + boundary.encode("latin-1")
    parts: dict[str, Part] = {}
    pos = body.find(delim)
    if pos < 0:
        raise HTTPException(400, "malformed multipart body")
    while True:
        start = pos + len(delim)
        if body[start:start + 2] == b"--":  # closing delimiter
            break
        nxt = _next_delimiter(body, delim, start)
        if nxt < 0:
            raise HTTPException(400, "malformed multipart body (no closing boundary)")
        chunk = body[start:nxt]
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            raise HTTPException(400, "malformed multipart part")
        headers: dict[str, str] = {}
        for line in head.decode("utf-8", errors="replace").split("\r\n"):
            k, _, v = line.partition(":")
            if k:
                headers[k.strip().lower()] = v.strip()
        _, disp = header_params(headers.get("content-disposition", ""))
        name = disp.get("name")
        if name and name not in parts:
            parts[name] = Part(name=name, data=data, filename=disp.get("filename"),
                               content_type=headers.get("content-type"))
        pos = nxt + 2
    return parts
