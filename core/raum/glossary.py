"""Room glossaries (``glossaries/*.yaml``): fixed translations and names the fact guard checks.

Format::

    terms:
      - de: Metformin
        kind: medication        # medication | name | term
        keep: true              # keep as is in every language
        ru: [Метформин]         # accepted / fixed forms per language (str or list)

Session corrections (V-05) are layered on top with :meth:`Glossary.add_correction`, an uploaded
deal glossary (N-02) with :meth:`Glossary.merge`. Uploads are YAML in the format above or CSV with a
header row ``de,<lang>,...`` (optional ``kind`` / ``keep`` / ``note`` columns; several accepted forms
in one cell separated by ``|``; comma, semicolon or tab separated)::

    de,en,tr
    Liefertermin,delivery date,teslim tarihi
    Skonto,cash discount|early payment discount,erken ödeme indirimi
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from raum.config import repo_root

_META_KEYS = {"kind", "keep", "note"}
_KINDS = {"term", "name", "medication"}
_LANG_COL = re.compile(r"^[a-z]{2,3}$")
MAX_TERMS = 2000
MAX_FIELD = 200
# The recogniser prompt (``Glossary.asr_prompt``): whisper keeps at most 224 prompt tokens, and a long prompt
# makes it "hear" prompt text in short or silent audio. 300 characters is ~60-100 tokens.
ASR_PROMPT_MAX_TERMS = 40
ASR_PROMPT_MAX_CHARS = 300
_ASR_PROMPT_MAX_WORDS = 3  # a longer phrase is everyday words: no spelling to hint, and it eats the budget
_ASR_PROMPT_RANK = {"medication": 0, "name": 1}  # then every other term
_UNKNOWN_LANGS = ("", "und", "auto")


class GlossaryError(ValueError):
    """An uploaded glossary that cannot be read."""


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
    _asr_prompts: dict[str, tuple[Any, str]] = field(default_factory=dict, init=False, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | Path | None) -> Glossary:
        if not path:
            return cls()
        p = Path(path)
        if not p.is_absolute():
            p = repo_root() / p
        with p.open(encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        try:
            terms = parse_terms(raw.get("terms", []) or [])
        except GlossaryError as e:
            raise ValueError(f"{p}: {e}") from None
        return cls(terms=terms, source=str(p))

    def merge(self, terms: Iterable[Term]) -> tuple[int, int]:
        """N-02: layer uploaded terms over this glossary for the session. A term whose German form is
        already known gets the uploaded forms as preferred ones (and the upload's kind / keep); a new
        term is appended. Returns ``(added, updated)``."""
        added = updated = 0
        index = {t.de.casefold(): t for t in self.terms}
        for new in terms:
            old = index.get(new.de.casefold())
            if old is None:
                t = Term(de=new.de, kind=new.kind, keep=new.keep, forms={k: list(v) for k, v in new.forms.items()})
                self.terms.append(t)
                index[t.de.casefold()] = t
                added += 1
                continue
            for lang, forms in new.forms.items():
                rest = [f for f in old.forms.get(lang, []) if f.casefold() not in {x.casefold() for x in forms}]
                old.forms[lang] = [*forms, *rest]
            old.kind = new.kind if new.kind != "term" else old.kind
            old.keep = old.keep or new.keep
            updated += 1
        return added, updated

    def langs(self) -> list[str]:
        out = {"de"} if self.terms else set()
        for t in self.terms:
            out.update(t.forms)
        return sorted(out)

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

    def asr_prompt(self, lang: str | None, *, max_terms: int = ASR_PROMPT_MAX_TERMS,
                   max_chars: int = ASR_PROMPT_MAX_CHARS) -> str:
        """Spelling hints for the speech recogniser (whisper's ``prompt``): the terms as a speaker of ``lang``
        says them, medications first, then names, then other terms (glossary order within a kind),
        comma-separated, ending with a period: ``"Metformin, Penicillin, Insulin, Rezept."``. "" when there is
        nothing to hint.

        A term without a form in ``lang`` is left out, except a medication or name (its German key: drug and
        proper names sound the same in every language). ``lang`` unknown (None / "und" / "auto"): the German
        forms of medications, names and kept terms only; any other German word would push a speaker of
        another language toward German. Capped at ``max_terms`` / ``max_chars`` (see ASR_PROMPT_MAX_CHARS).
        Cached per language until the glossary changes (a merged upload or a correction rebuilds it)."""
        code = (lang or "").split("-")[0].lower()
        if code in _UNKNOWN_LANGS:
            code = ""
        sig = (max_terms, max_chars,
               tuple((t.de, t.kind, t.keep, tuple(t.forms.get(code, ()))) for t in self.terms))
        hit = self._asr_prompts.get(code)
        if hit is not None and hit[0] == sig:
            return hit[1]
        picked: list[str] = []
        seen: set[str] = set()
        size = 0
        for t in sorted(self.terms, key=lambda t: _ASR_PROMPT_RANK.get(t.kind, 2)):  # stable: file order per kind
            if code:
                forms = t.forms_for(code)
                form = forms[0] if forms else (t.de if t.kind != "term" else None)
            else:
                form = t.de if (t.kind != "term" or t.keep) else None
            form = " ".join((form or "").split())
            if not form or "," in form or len(form.split()) > _ASR_PROMPT_MAX_WORDS or form.casefold() in seen:
                continue
            size += len(form) + (2 if picked else 0)  # ", " between terms
            if len(picked) >= max_terms or size + 1 > max_chars:  # +1: the closing period
                break  # in priority order: a medication never loses its place to a shorter term after it
            picked.append(form)
            seen.add(form.casefold())
        prompt = ", ".join(picked) + "." if picked else ""
        self._asr_prompts[code] = (sig, prompt)
        return prompt

    def add_correction(self, src: str, tgt: str, tgt_lang: str, src_lang: str = "de") -> None:
        """V-05: a professional's live correction applies for the rest of the session."""
        for t in self.terms:
            if src.casefold() in {f.casefold() for f in t.forms_for(src_lang)}:
                t.forms[tgt_lang] = [tgt, *[f for f in t.forms.get(tgt_lang, []) if f != tgt]]
                return
        term = Term(de=src, kind="term")  # a non-German source term is its own key until a German one is known
        if src_lang != "de":
            term.forms[src_lang] = [src]
        term.forms[tgt_lang] = [tgt]
        self.terms.append(term)


# ---------------------------------------------------------------- parsing (files and uploads)


def _forms(v: Any) -> list[str]:
    items = v if isinstance(v, (list, tuple)) else str(v).split("|")
    out: list[str] = []
    for x in items:
        s = str(x).strip()[:MAX_FIELD]
        if s and s not in out:
            out.append(s)
    return out


def _truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("true", "yes", "ja", "1", "x")


def parse_terms(raw: Any) -> list[Term]:
    """Terms in the ``glossaries/*.yaml`` shape (a list of mappings with a ``de`` key)."""
    if not isinstance(raw, list):
        raise GlossaryError("expected a list of terms")
    if len(raw) > MAX_TERMS:
        raise GlossaryError(f"too many terms ({len(raw)} > {MAX_TERMS})")
    terms: list[Term] = []
    for t in raw:
        if not isinstance(t, dict) or not str(t.get("de") or "").strip():
            raise GlossaryError(f"every term needs a 'de' key: {t!r}"[:200])
        forms: dict[str, list[str]] = {}
        for k, v in t.items():
            if k == "de" or k in _META_KEYS or v is None or v == "":
                continue
            lang = str(k).strip().lower()
            if not _LANG_COL.match(lang):
                raise GlossaryError(f"not a language code: {k!r}")
            f = _forms(v)
            if f:
                forms[lang] = f
        kind = str(t.get("kind") or "term").strip().lower()
        terms.append(Term(de=str(t["de"]).strip()[:MAX_FIELD], kind=kind if kind in _KINDS else "term",
                          keep=_truthy(t.get("keep")), forms=forms))
    return terms


def term_to_dict(t: Term) -> dict[str, Any]:
    """A term in the ``glossaries/*.yaml`` shape (the wire format of the ``glossary`` control)."""
    d: dict[str, Any] = {"de": t.de, "kind": t.kind}
    if t.keep:
        d["keep"] = True
    d.update({lang: list(forms) for lang, forms in t.forms.items()})
    return d


def parse_csv(text: str) -> list[Term]:
    body = text.lstrip("﻿").strip()
    if not body:
        raise GlossaryError("empty file")
    try:
        dialect: Any = csv.Sniffer().sniff(body.split("\n", 1)[0], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(body), dialect) if any(c.strip() for c in r)]
    header = [c.strip().lower() for c in rows[0]]
    if "de" not in header:
        raise GlossaryError("the first row must name the columns, starting with 'de' (e.g. de,en,tr)")
    for h in header:
        if h and h not in _META_KEYS and not _LANG_COL.match(h):
            raise GlossaryError(f"not a language code in the header: {h!r}")
    raw = [{h: c.strip() for h, c in zip(header, r, strict=False) if h} for r in rows[1:]]
    return parse_terms([t for t in raw if t.get("de")])


def parse_upload(text: str, *, filename: str | None = None, content_type: str | None = None,
                 fmt: str | None = None) -> list[Term]:
    """An uploaded glossary: YAML (``terms:``) or CSV (``de,<lang>,...``), told apart by ``fmt``, the
    file name, the content type, or the text itself."""
    text = text.lstrip("﻿")
    kind = (fmt or "").lower()
    name = (filename or "").lower()
    ctype = (content_type or "").lower()
    if not kind:
        if name.endswith((".yaml", ".yml")) or "yaml" in ctype:
            kind = "yaml"
        elif name.endswith((".csv", ".tsv")) or "csv" in ctype or "tab-separated" in ctype:
            kind = "csv"
        else:
            first = next((ln.strip() for ln in text.splitlines()
                          if ln.strip() and not ln.lstrip().startswith("#")), "")
            kind = "yaml" if first.startswith(("terms:", "- ", "{", "[")) else "csv"
    if kind == "csv":
        terms = parse_csv(text)
    elif kind == "yaml":
        try:
            raw = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise GlossaryError(f"not valid YAML: {str(e).splitlines()[0]}") from None
        if isinstance(raw, dict):
            raw = raw.get("terms")
        terms = parse_terms(raw or [])
    else:
        raise GlossaryError(f"unknown format {fmt!r} (yaml or csv)")
    if not terms:
        raise GlossaryError("no terms found")
    return terms
