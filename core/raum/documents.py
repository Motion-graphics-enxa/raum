"""Document camera (F-07) in raum-core: photo in, ``document.ready`` out.

The vision model (``raum.pipeline.vision``) reads the photo and lists every date, amount, dose and
deadline twice: as printed (``source``) and in the reader's language (``value``). The fact guard
(V-01, code, no model) then compares the two per fact, the same way it compares a sentence with its
translation; a figure that does not survive into the reader's language is marked ``uncertain``, as
is anything the model itself was unsure of.

Privacy: the photo is handled in memory only. It is never written to disk, never stored in the
session and never logged; the caller drops it once ``read_document`` returns. Only the reading
(title, text, facts) becomes a content event and is stored encrypted like every other.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from raum.events import DocumentFact, DocumentReady
from raum.glossary import Glossary
from raum.pipeline.vision import DocumentReading
from raum.verify import guard

log = logging.getLogger("raum.documents")

MAX_IMAGE_BYTES = 15 * 1024 * 1024  # a phone photo; the app downscales to ~2048 px JPEG first
_LANG_RE = re.compile(r"^[a-z]{2,3}$")

# guard kinds compared per document fact kind
GUARD_KINDS: dict[str, tuple[str, ...]] = {
    "dose": ("dose", "frequency"),
    "date": ("date", "time"),
    "deadline": ("date", "deadline", "time", "duration"),
    "amount": ("amount", "percent"),
    "time": ("time",),
    "duration": ("duration",),
    "percent": ("percent",),
    "number": ("number",),
}


def sniff_image(data: bytes | bytearray | memoryview) -> str | None:
    """MIME type from the magic bytes, for the formats Ollama decodes; ``None`` for anything else."""
    head = bytes(data[:16])
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def is_heic(data: bytes | bytearray | memoryview) -> bool:
    head = bytes(data[:16])
    return head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1")


def norm_lang(value: str | None) -> str | None:
    v = (value or "").strip().replace("_", "-").split("-")[0].lower()
    return v if _LANG_RE.match(v) and v != "auto" else None


def check_fact(fact: dict[str, Any], src_lang: str, lang: str, glossary: Glossary | None = None) -> bool:
    """True when the fact guard finds the same figures in ``source`` (as printed) and ``value``
    (as the reader sees it). Facts without a printed form, or the guard missing, count as no mismatch."""
    source = fact.get("source")
    if not source:
        return True
    kinds = GUARD_KINDS.get(str(fact.get("kind")), ("number",))
    try:
        res = guard.check(str(source), src_lang, str(fact.get("value") or ""), lang, kinds=kinds, glossary=glossary)
    except NotImplementedError:
        return True
    except Exception:
        log.exception("fact guard failed on a document fact")
        return True
    return not res.red


def to_event(reading: DocumentReading, *, by: str, lang: str, ms: float | None, model: str | None,
             glossary: Glossary | None = None) -> DocumentReady:
    src_lang = norm_lang(reading.source_lang) or "de"
    facts = []
    for f in reading.facts:
        ok = check_fact(f, src_lang, lang, glossary)
        facts.append(DocumentFact(kind=str(f.get("kind") or "number"), value=str(f.get("value") or ""),
                                  source=f.get("source"), uncertain=bool(f.get("uncertain")) or not ok))
    return DocumentReady(by=by, lang=lang, title=reading.title, text=reading.text, facts=facts,
                         ms=round(ms, 1) if ms is not None else None, source_lang=norm_lang(reading.source_lang),
                         model=model)
