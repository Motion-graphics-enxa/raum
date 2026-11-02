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
