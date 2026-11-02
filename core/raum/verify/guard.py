"""Fact guard (V-01, plan §6.8). Pure Python, no model. Target < 5 ms per utterance.

CONTRACT (implemented by the security+guard workstream; signatures are fixed):

    extract(text, lang, *, kinds=None, glossary=None) -> list[Fact]
    check(src, src_lang, tgt, tgt_lang, *, kinds=None, confirm_on=(), glossary=None) -> GuardResult

``check`` compares the multisets of normalised facts per kind between source and
translation. Any missing, extra or different value is a mismatch (``red``), reported
with the exact pair. Facts whose kind is in ``confirm_on`` and that are present in the
source go to ``GuardResult.confirm`` (the listener gets "500 mg, twice a day. Correct?").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from raum.events import FactCheck
from raum.glossary import Glossary


@dataclass(frozen=True)
class Fact:
    kind: str  # see raum.modes.schema.GuardKind
    value: str  # normalised, language-independent, e.g. "500 mg", "2/day", "--11-12", "1200000 EUR"
    surface: str  # as written in the text, e.g. "500 Milligramm"
    span: tuple[int, int] = (0, 0)


@dataclass
class GuardResult:
    facts: list[FactCheck] = field(default_factory=list)  # one entry per compared value
    red: bool = False
    confirm: list[FactCheck] = field(default_factory=list)  # facts that need a confirm prompt
    ms: float = 0.0


def extract(text: str, lang: str, *, kinds: Iterable[str] | None = None,
            glossary: Glossary | None = None) -> list[Fact]:
    raise NotImplementedError


def check(src: str, src_lang: str, tgt: str, tgt_lang: str, *, kinds: Iterable[str] | None = None,
          confirm_on: Iterable[str] = (), glossary: Glossary | None = None) -> GuardResult:
    raise NotImplementedError
