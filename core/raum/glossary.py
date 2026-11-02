"""Room glossaries (``glossaries/*.yaml``): fixed translations and names the fact guard checks.

Format::

    terms:
      - de: Metformin
        kind: medication        # medication | name | term
        keep: true              # keep as is in every language
        ru: [Метформин]         # accepted / fixed forms per language (str or list)

Session corrections (V-05) are layered on top with :meth:`Glossary.add_correction`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import yaml

from raum.config import repo_root

_META_KEYS = {"kind", "keep", "note"}


@dataclass
class Term:
    de: str
    kind: str = "term"
    keep: bool = False
    forms: dict[str, list[str]] = field(default_factory=dict)  # lang -> accepted forms (not incl. de)

    def forms_for(self, lang: str) -> list[str]:
        """All accepted surface forms of this term in ``lang`` (first = preferred)."""
        out: list[str] = []
        if lang == "de":
            out.append(self.de)
        out.extend(self.forms.get(lang, []))
        if self.keep and self.de not in out:
            out.append(self.de)
        return out

    def preferred(self, lang: str) -> str | None:
        f = self.forms_for(lang)
        return f[0] if f else None


@dataclass
class Glossary:
    terms: list[Term] = field(default_factory=list)
    source: str | None = None

    @classmethod
    def load(cls, path: str | Path | None) -> "Glossary":
        if not path:
            return cls()
        p = Path(path)
        if not p.is_absolute():
            p = repo_root() / p
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        terms: list[Term] = []
        for t in raw.get("terms", []) or []:
            if not isinstance(t, dict) or "de" not in t:
                raise ValueError(f"{p}: every term needs a 'de' key: {t!r}")
            forms: dict[str, list[str]] = {}
            for k, v in t.items():
                if k == "de" or k in _META_KEYS or v is None:
                    continue
                forms[k] = [v] if isinstance(v, str) else [str(x) for x in v]
            terms.append(Term(de=str(t["de"]), kind=str(t.get("kind", "term")), keep=bool(t.get("keep")),
                              forms=forms))
        return cls(terms=terms, source=str(p))

    def by_kind(self, *kinds: str) -> list[Term]:
        return [t for t in self.terms if t.kind in kinds]

    def medications(self) -> list[Term]:
        return self.by_kind("medication")

    def names(self) -> list[Term]:
        return self.by_kind("name")

    def find(self, text: str, lang: str, kinds: Iterable[str] | None = None) -> list[Term]:
        """Terms whose form in ``lang`` (or German / kept form) occurs in ``text`` (case-insensitive)."""
        low = text.casefold()
        kinds = set(kinds) if kinds else None
        hits = []
        for t in self.terms:
            if kinds and t.kind not in kinds:
                continue
            candidates = set(t.forms_for(lang)) | ({t.de} if t.keep else set())
            if any(c.casefold() in low for c in candidates):
                hits.append(t)
        return hits

    def prompt_lines(self, src_lang: str, tgt_lang: str) -> str:
        """Fixed translations for the translate prompt's ``{glossary}`` slot."""
        pairs = []
        for t in self.terms:
            s = t.preferred(src_lang) or t.de
            g = t.preferred(tgt_lang)
            if g:
                pairs.append(f"{s} = {g}")
        return "; ".join(pairs) if pairs else "(none)"

    def add_correction(self, src: str, tgt: str, tgt_lang: str, src_lang: str = "de") -> None:
        """V-05: a professional's live correction applies for the rest of the session."""
        for t in self.terms:
            if src.casefold() in {f.casefold() for f in t.forms_for(src_lang)}:
                t.forms[tgt_lang] = [tgt, *[f for f in t.forms.get(tgt_lang, []) if f != tgt]]
                return
        de = src if src_lang == "de" else src
        term = Term(de=de, kind="term")
        if src_lang != "de":
            term.forms[src_lang] = [src]
        term.forms[tgt_lang] = [tgt]
        self.terms.append(term)
