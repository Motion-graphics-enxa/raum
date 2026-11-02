"""Canned conversations for the mock engines (one YAML per room mode).

See ``sprechzimmer.yaml`` for the format. Lines alternate role a and role b and carry their text in
de, en, tr, ru and ar. The mock ASR walks through the lines per role; the mock translator and
verifier look lines up by their text in any language.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import yaml

MOCK_DIR = Path(__file__).resolve().parent
DEFAULT_CONF = 0.93
FALLBACK_LANG = "en"


def normalize(text: str) -> str:
    """Case- and punctuation-insensitive key for looking lines up by text."""
    text = re.sub(r"^\[[a-z]{2,3}\]\s*", "", text.strip())
    return re.sub(r"[\W_]+", " ", text.casefold()).strip()


@dataclass
class MockLine:
    id: str
    mode: str
    role: str
    text: dict[str, str]
    conf: float = DEFAULT_CONF
    mistranslate: dict[str, str] = field(default_factory=dict)
    simple: dict[str, str] = field(default_factory=dict)

    def lang_for(self, lang: str | None) -> str:
        return lang if lang and lang in self.text else FALLBACK_LANG

    def source(self, lang: str | None) -> str:
        """The line as said by a speaker of ``lang`` (fallback en)."""
        return self.text[self.lang_for(lang)]

    def target(self, lang: str, *, faithful: bool = False) -> str:
        """The line as the (mock) translator renders it in ``lang``: mistranslation applied."""
        text = self.text.get(lang)
        if text is None:
            return f"[{lang}] {self.text[FALLBACK_LANG]}"
        return text if faithful else apply_mistranslation(text, self.mistranslate)

    def plain(self, lang: str) -> str | None:
        return self.simple.get(lang) or self.simple.get(FALLBACK_LANG)


def apply_mistranslation(text: str, mapping: dict[str, str]) -> str:
    for wrong_from, wrong_to in mapping.items():
        text = re.sub(rf"(?<!\d){re.escape(wrong_from)}(?!\d)", wrong_to, text, count=1)
    return text


@dataclass
class MockScript:
    mode: str
    lines: list[MockLine]

    def by_role(self, role: str) -> list[MockLine]:
        return [ln for ln in self.lines if ln.role == role]


@lru_cache(maxsize=8)
def load_script(mode: str) -> MockScript:
    path = MOCK_DIR / f"{mode}.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"no mock script for mode '{mode}': {path}")
    with path.open(encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    lines = []
    for item in raw.get("lines", []):
        lines.append(MockLine(
            id=str(item["id"]), mode=mode, role=str(item["role"]),
            text={k: str(v) for k, v in item["text"].items()},
            conf=float(item.get("conf", DEFAULT_CONF)),
            mistranslate={str(k): str(v) for k, v in (item.get("mistranslate") or {}).items()},
            simple={k: str(v) for k, v in (item.get("simple") or {}).items()},
        ))
    return MockScript(mode=mode, lines=lines)


def available_modes() -> list[str]:
    return sorted(p.stem for p in MOCK_DIR.glob("*.yaml"))


@dataclass(frozen=True)
class Match:
    line: MockLine
    lang: str  # language of the text that matched
    mistranslated: bool  # the text was the (wrong) translator output, not the canonical line


@lru_cache(maxsize=1)
def _index() -> dict[str, list[Match]]:
    idx: dict[str, list[Match]] = {}
    for mode in available_modes():
        for line in load_script(mode).lines:
            for lang, text in line.text.items():
                idx.setdefault(normalize(text), []).append(Match(line, lang, False))
                if line.mistranslate:
                    wrong = apply_mistranslation(text, line.mistranslate)
                    if wrong != text:
                        idx.setdefault(normalize(wrong), []).append(Match(line, lang, True))
    return idx


def find(text: str, mode: str | None = None) -> Match | None:
    """Find the line ``text`` belongs to (any language; prefers ``mode``)."""
    hits = _index().get(normalize(text), [])
    if not hits:
        return None
    if mode:
        for h in hits:
            if h.line.mode == mode:
                return h
    return hits[0]


# ---------------------------------------------------------------- segments (simultaneous mode)

_PUNCT_END = re.compile(r"[.,;:!?…،؛؟。！？][\"'»«”’)]*$")
_ABBREV_TOKEN = re.compile(r"^(?:Dr|Fr|Hr|Prof|Nr|ca|bzw|z\.B|u\.a|St|Mr|Mrs|Ms|No|vs)\.$|^\d+\.$", re.IGNORECASE)


@dataclass(frozen=True)
class Span:
    """A run of whole words of a line, as said in ``lang`` (token indices into ``text.split()``)."""

    line: MockLine
    lang: str
    mistranslated: bool  # found in the (wrong) translator output
    start: int
    end: int

    def tokens(self) -> list[str]:
        return _variant(self.line, self.lang, self.mistranslated).split()

    def render(self, lang: str, *, faithful: bool = False) -> str:
        """The same part of the line in ``lang``: the word span mapped proportionally, snapped to the
        target's clause ends so that consecutive segments map to consecutive, non-empty parts."""
        src = self.tokens()
        tgt = self.line.target(lang, faithful=faithful).split()
        a, b = map_span(self.start, self.end, src, tgt)
        return " ".join(tgt[a:b])


def _variant(line: MockLine, lang: str, mistranslated: bool) -> str:
    text = line.text[lang]
    return apply_mistranslation(text, line.mistranslate) if mistranslated else text


def _clause_ends(tokens: list[str]) -> list[int]:
    return [i + 1 for i, t in enumerate(tokens) if _PUNCT_END.search(t) and not _ABBREV_TOKEN.match(t)]


def map_span(start: int, end: int, src: list[str], tgt: list[str]) -> tuple[int, int]:
    """Token span [start, end) of ``src`` -> the matching span of ``tgt``: proportional; a cut at a source
    clause end snaps to the nearest target clause end."""
    n, n_src = len(tgt), len(src)
    if n_src <= 0 or n == 0:
        return 0, n
    ends = sorted(set(_clause_ends(tgt) + [n]))
    src_ends = set(_clause_ends(src))

    def pos(k: int) -> int:
        if k <= 0:
            return 0
        if k >= n_src:
            return n
        x = k * n / n_src
        if k not in src_ends:  # cut mid-clause (a turn ended at a pause): plain proportion
            return max(1, min(n - 1, round(x)))
        return min(ends, key=lambda e: (abs(e - x), e))

    a, b = pos(start), pos(end)
    if b <= a:  # too short to reach a clause end: plain proportion
        a, b = round(start * n / n_src), max(round(end * n / n_src), round(start * n / n_src) + 1)
    return a, min(b, n)


def _words(tokens: list[str]) -> list[tuple[str, int]]:
    """(normalised word, token index); a token may hold 0..n words ("mg'ı" -> "mg", "ı")."""
    return [(w, i) for i, t in enumerate(tokens) for w in normalize(t).split()]


def locate(text: str, mode: str | None = None, *, after: str = "") -> Span | None:
    """Find the line that contains ``text`` as a run of whole words (any language, also the mistranslated
    translator output). ``after``: what the speaker said before it in the same utterance; the run must then
    start right after it at the beginning of the line. Prefers ``mode``."""
    needle = normalize(text).split()
    head = normalize(after).split() if after.strip() else []
    if not needle:
        return None
    best: Span | None = None
    for m in sorted(available_modes(), key=lambda x: x != mode):
        for line in load_script(m).lines:
            for lang in line.text:
                for wrong in (False, True):
                    if wrong and not line.mistranslate:
                        continue
                    toks = _variant(line, lang, wrong).split()
                    if wrong and toks == line.text[lang].split():
                        continue
                    words = _words(toks)
                    flat = [w for w, _ in words]
                    if head:
                        if flat[:len(head)] != head or flat[len(head):len(head) + len(needle)] != needle:
                            continue
                        i = len(head)
                    else:
                        i = next((j for j in range(len(flat) - len(needle) + 1)
                                  if flat[j:j + len(needle)] == needle), -1)
                        if i < 0:
                            continue
                    span = Span(line, lang, wrong, words[i][1], words[i + len(needle) - 1][1] + 1)
                    if head or span.start == 0:
                        return span  # anchored at the utterance start: unambiguous
                    best = best or span
    return best
