"""Uploads into a room: the document camera (F-07) and the deal glossary (N-02).

Both bodies are read into memory with a size cap (``raum.api.forms``); nothing is spooled to disk.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from raum.api.deps import CoreDep
from raum.api.forms import is_multipart, parse_multipart, read_body
from raum.documents import MAX_IMAGE_BYTES, is_heic, norm_lang, sniff_image
from raum.events import DocumentReady
from raum.glossary import GlossaryError, parse_upload
from raum.modes import mode_for_room

router = APIRouter(prefix="/api", tags=["rooms"])

MAX_GLOSSARY_BYTES = 512 * 1024

_DOCUMENT_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {"multipart/form-data": {"schema": {
            "type": "object",
            "required": ["image", "by", "lang"],
            "properties": {
                "image": {"type": "string", "format": "binary", "description": "JPEG, PNG or WebP photo"},
                "by": {"type": "string", "description": "role (a | b) or identity of the device"},
                "lang": {"type": "string", "description": "the reader's language, e.g. ar"},
            },
        }}},
    },
}

_GLOSSARY_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {"schema": {"type": "object", "required": ["file"], "properties": {
                "file": {"type": "string", "format": "binary", "description": "glossary .yaml or .csv"},
                "by": {"type": "string"}}}},
            "text/csv": {"schema": {"type": "string"}, "example": "de,en,tr\nLiefertermin,delivery date,teslim tarihi\n"},
            "application/yaml": {"schema": {"type": "string"},
                                 "example": "terms:\n  - de: Liefertermin\n    en: delivery date\n"},
        },
    },
}


@router.post("/rooms/{room}/document", response_model=DocumentReady, openapi_extra=_DOCUMENT_BODY,
             responses={400: {"description": "missing field"}, 413: {"description": "photo too large"},
                        415: {"description": "not a JPEG/PNG/WebP photo"},
                        502: {"description": "the vision model failed"},
                        503: {"description": "document camera off in this profile"}})
async def document(room: str, request: Request, core: CoreDep) -> DocumentReady:
    """F-07: photograph a letter, contract or term sheet; the vision model explains it in ``lang`` and
    lists every date, amount, dose and deadline. Returns and broadcasts ``document.ready``. The photo is
    read in memory and dropped after the reading; only the reading is stored (encrypted)."""
    ctype = request.headers.get("content-type", "")
    if not is_multipart(ctype):
        raise HTTPException(415, "send multipart/form-data with fields image, by and lang")
    body = await read_body(request, MAX_IMAGE_BYTES + 64 * 1024)
    parts = parse_multipart(body, ctype)
    del body
    image = parts.pop("image", None) or parts.pop("file", None)
    by = parts["by"].text() if "by" in parts else ""
    lang = norm_lang(parts["lang"].text()) if "lang" in parts else None
    try:
        if image is None or not image.data:
            raise HTTPException(400, "field 'image' (the photo) is required")
        if not by:
            raise HTTPException(400, "field 'by' (role) is required")
        if lang is None:
            raise HTTPException(400, "field 'lang' (the reader's language, e.g. 'ar') is required")
        if len(image.data) > MAX_IMAGE_BYTES:
            raise HTTPException(413, f"photo too large (max {MAX_IMAGE_BYTES // (1024 * 1024)} MB)")
        if sniff_image(image.data) is None:
            hint = " (HEIC: send it as JPEG)" if is_heic(image.data) else ""
            raise HTTPException(415, f"not a JPEG, PNG or WebP photo{hint}")
        return await core.sessions.document(room, image.data, by=by, lang=lang)
    finally:
        del image  # the photo is not kept after the reading


async def _glossary_text(request: Request) -> tuple[str, str | None, str | None, dict[str, str]]:
    ctype = request.headers.get("content-type", "")
    body = await read_body(request, MAX_GLOSSARY_BYTES)
    if is_multipart(ctype):
        parts = parse_multipart(body, ctype)
        f = parts.get("file") or next((p for p in parts.values() if p.filename), None)
        if f is None:
            raise HTTPException(400, "field 'file' (the glossary) is required")
        fields = {k: p.text() for k, p in parts.items() if p is not f and not p.filename}
        return f.data.decode("utf-8-sig", errors="replace"), f.filename, f.content_type, fields
    return body.decode("utf-8-sig", errors="replace"), None, ctype, {}


@router.post("/rooms/{room}/glossary", openapi_extra=_GLOSSARY_BODY,
             responses={400: {"description": "unreadable glossary"}, 413: {"description": "file too large"}})
async def upload_glossary(room: str, request: Request, core: CoreDep, by: str | None = None,
                          format: str | None = None) -> dict[str, Any]:
    """N-02: a deal glossary for this room's session, as YAML (``glossaries/*.yaml`` format) or CSV
    with a header ``de,<lang>,...``. Merged into the session glossary (translation prompt and fact
    guard) and forwarded to the agent; gone with the session on burn. Multipart (``file``) or a raw
    body (``text/csv``, ``application/yaml``); ``?format=csv|yaml`` overrides the detection."""
    mode_for_room(room)
    text, filename, ctype, fields = await _glossary_text(request)
    try:
        terms = parse_upload(text, filename=filename, content_type=ctype, fmt=format or fields.get("format"))
    except GlossaryError as e:
        raise HTTPException(400, f"glossary: {e}") from None
    return await core.sessions.upload_glossary(room, terms, by=by or fields.get("by"))


@router.get("/rooms/{room}/glossary")
async def get_glossary(room: str, core: CoreDep) -> dict[str, Any]:
    """The session glossary as the interpreter uses it: the room's file, uploads, live corrections."""
    mode_for_room(room)
    return core.sessions.glossary_info(room)
