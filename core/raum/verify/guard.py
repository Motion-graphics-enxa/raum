"""Fact guard (V-01, plan §6.8). Pure Python, no model. Target < 5 ms per utterance.

CONTRACT (implemented by the security+guard workstream; signatures are fixed):

    extract(text, lang, *, kinds=None, glossary=None) -> list[Fact]
    check(src, src_lang, tgt, tgt_lang, *, kinds=None, confirm_on=(), glossary=None) -> GuardResult

``check`` compares the multisets of normalised facts per kind between source and
translation. Any missing, extra or different value is a mismatch (``red``), reported
with the exact pair. Facts whose kind is in ``confirm_on`` and that are present in the
source go to ``GuardResult.confirm`` (the listener gets "500 mg, twice a day. Correct?").

How it works
------------
Hand-rolled tables per language (de, en, tr, ru, ar; uk partly; any other language gets digits,
symbols and numeric dates). The text is normalised (lowercase; Arabic-Indic digits -> ASCII;
Arabic harakat/tatweel removed, alef/ya/ta-marbuta unified; ё -> е; Turkish ı/İ folded to i;
NBSP/narrow NBSP/thin space unified) with an index map back to the original, then regex passes
run in a fixed order, each consuming its span so a number is counted once:

    dates (numeric, day + month name, day-only ordinals, weekdays, month-only) -> times ->
    frequencies -> percent -> amounts -> doses -> durations -> bare numbers -> standalone
    currency / unit words; then glossary medications and names, allergy, negation, and the
    deadline promotion of dates.

Normalised values (``Fact.value``, the convention of ``eval/sets/README.md``): dose "500 mg" /
"0.5 g" / "10 µg" / "5 ml" / "100 IE" (the stated unit; the comparison converts to mg / ml, so 0,5 g
== 500 mg), frequency "2/day" | "1/week" | "q8h", date "--11-12" | "--03" (month) | "---12" (day) |
"wd:thu" (a stated year is kept for the comparison: 12.11.2026 != 12.11.2025, but == "12 November"),
time "09:00", duration "P7D" | "PT8H", amount "1200000 EUR" (or "1050000" without a currency) plus a
currency fact "EUR", percent "20%", number "3" | "2.5" (a count "10.000 Stück" / "10 000 штук" is the number
"10000": it matches a bare number, not an amount), medication/name/allergen = the glossary's German key,
allergy "present" when no allergen is named, negation "not".

Thousands: "1.200" / "1,200" by locale (``parse_number``); "3 200" / "950 000" in groups of three after a
1-3 digit head is one number with NBSP / narrow NBSP in any language and with a plain space in
space-thousands languages (ru, uk, fr, pl, ...), elsewhere only before a currency or count word.

Matching (``check``): exact multiset match per value, then lenient pairs that are the same fact
said differently — a date with a year vs the same day without one, a deadline vs a plain date, an
amount with a currency vs the same number without one. Weak facts only ever *satisfy* the other
side, never raise an alarm: number words for 1 that double as articles ("ein", "a", "bir", "один",
"واحد"; not reported by ``extract``), Arabic dual nouns (= 2), and the currency that comes with an
amount (a currency swap already shows on the amount row; a currency named on one side only is not
an error). Medication, allergy, name, negation, currency and unit are compared as sets (a drug
named twice in the source and once in the translation is fine). FactCheck ``src``/``tgt`` carry the
surface form as written (what the patient reads).

Known limits (deliberate, to keep false alarms down)
----------------------------------------------------
- Negation is compared by presence: does each side contain any negation cue? A sentence with
  two clauses, one negated, is not checked clause by clause. Comparative idioms that mean
  "at most / at the latest / at least" ("nicht mehr als", "no later than", "не позднее",
  "لا يتجاوز") are not counted as negation.
- Turkish verb negation is best effort from suffixes (-mıyor, -mayın, -mamalı, -madı, -mayacak,
  -maz, -mam, -mayız, -mamış) plus değil/yok/hiç/asla/hayır. Bare -ma/-me ("alma!") is not counted
  because it collides with verbal nouns ("açma"). A positive verb whose stem ends in m
  ("gömüyor") can read as negated.
- Proper names: only glossary ``kind: name`` terms are checked strictly. Other capitalised Latin
  tokens are reported only when they appear verbatim in both texts (German capitalises every
  noun, and transliteration into Cyrillic/Arabic is legitimate), so names cannot raise a false
  alarm — and a wrongly transliterated name is not caught.
- Number words cover 0–20 (+ tens) in all five languages; compounds ("fünfundzwanzig") and
  ordinal words ("zwölften") are not parsed. 1 is weak (see above).
- Slash dates are read day-first (12/11 = 12 November) in every language.
- Allergens are glossary medications/terms next to an allergy keyword in the same clause; other
  allergens ("Nüsse") give the value "present".
"""

from __future__ import annotations

import itertools
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from raum.events import FactCheck
from raum.glossary import Glossary

ALL_KINDS = frozenset({
    "dose", "frequency", "allergy", "date", "time", "duration", "number", "unit", "medication",
    "name", "negation", "deadline", "amount", "currency", "percent",
})
SET_KINDS = frozenset({"medication", "allergy", "name", "negation", "currency", "unit"})
_NUMERIC_KINDS = ("dose", "amount", "percent", "duration")  # fall back to "number" when not requested
_FAMILY = {"deadline": "date", "amount": "num", "number": "num"}


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


# ================================================================ normalisation

_DEL = frozenset([chr(c) for c in range(0x064B, 0x0660)] + ["\u0670", "\u0640"])  # harakat, alef khanjar, tatweel
_TABLE: dict[int, str] = {0x0660 + i: str(i) for i in range(10)}
_TABLE.update({0x06F0 + i: str(i) for i in range(10)})
_TABLE.update({ord(a): b for a, b in {
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ک": "ك", "ی": "ي",
    "\u2019": "'", "\u2018": "'", "\u02bc": "'", "`": "'", "\u00b4": "'",
    "\u202f": "\u00a0", "\u2009": "\u00a0", "\u2007": "\u00a0",
    "μ": "µ", "ё": "е", "Ё": "Е", "İ": "i", "\u2013": "-", "\u2014": "-", "\u2212": "-",
    "\u060c": ",",  # Arabic comma (a list separator; numbers use ٫ ٬)
}.items()})
_TABLE_TR = {**_TABLE, ord("I"): "i", ord("ı"): "i"}


def _norm_simple(s: str, lang: str) -> str:
    s = s.translate(_TABLE_TR if lang == "tr" else _TABLE).lower()
    return "".join(c for c in s if c not in _DEL) if any(c in _DEL for c in s) else s


def _norm_pat(s: str, lang: str) -> str:
    """Same character mapping as the text, for regex source (tables are lowercase; keeps ``(?P<..>``)."""
    s = s.translate(_TABLE_TR if lang == "tr" else _TABLE)
    return "".join(c for c in s if c not in _DEL) if any(c in _DEL for c in s) else s


class _Text:
    """Normalised text + map back to the original + consumed-span bookkeeping."""

    __slots__ = ("idx", "lang", "norm", "orig", "used")

    def __init__(self, text: str, lang: str) -> None:
        self.orig = text
        self.lang = lang
        t = text.translate(_TABLE_TR if lang == "tr" else _TABLE)
        low = t.lower()
        if len(low) != len(t):
            low = "".join(c.lower() if len(c.lower()) == 1 else c for c in t)
        if any(c in _DEL for c in low):
            keep = [i for i, c in enumerate(low) if c not in _DEL]
            self.norm = "".join(low[i] for i in keep)
            self.idx: list[int] | None = keep
        else:
            self.norm = low
            self.idx = None
        self.used = bytearray(len(self.norm))

    def ospan(self, a: int, b: int) -> tuple[int, int]:
        if self.idx is None or a >= b:
            return a, b
        return self.idx[a], self.idx[b - 1] + 1

    def surface(self, a: int, b: int) -> str:
        oa, ob = self.ospan(a, b)
        return self.orig[oa:ob].strip(" ,;:")

    def free(self, a: int, b: int) -> bool:
        return self.used.find(1, a, b) == -1

    def take(self, a: int, b: int) -> None:
        self.used[a:b] = b"\x01" * (b - a)


# ================================================================ numbers

_COMMA_DECIMAL = {"de", "tr", "ru", "uk", "fr", "es", "it", "pt", "pl", "nl", "ro", "hr", "sr", "bg", "sq",
                  "cs", "sk", "sl", "da", "sv", "no", "fi", "el", "hu", "az", "kk"}
_SPACE_THOUSANDS = {"ru", "uk", "fr", "pl", "bg", "cs", "sk", "kk"}


def _digits(sep: str) -> str:
    """Digit run regex. ``sep`` separates thousands groups: ``3 200``, ``950 000``, ``1 200 000,50`` read as one
    number. Groups only follow a 1-3 digit head that does not continue a time, date or fraction ("9:00 500 mg",
    "1/2 500", "am 12. 500 mg", a year "2026 500" stay apart). After normalisation NBSP, narrow NBSP, thin
    and figure space are all U+00A0."""
    lb = r"(?<![\w.,٫٬])"
    grouped = rf"(?<![:/])\d{{1,3}}(?:{sep}\d{{3}}(?!\d))+(?:[.,٫٬]\d+)?"
    return rf"{lb}(?:{grouped}|\d+(?:[.,٫٬]\d+)*)"


# U+00A0-family spaces group thousands in every language (typographic, never "2 x 500"); a plain space does in
# space-thousands languages, and elsewhere only right before a currency or a count word ("3 200 Euro" yes,
# "take 2 500 mg tablets" no).
_DIG_NBSP = _digits(" ")
_DIG_SPACE = _digits("[  ]")


def _dec(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


def _groups_ok(head: str, rest: list[str]) -> bool:
    return 1 <= len(head) <= 3 and all(len(p) == 3 for p in rest)


def parse_number(s: str, lang: str) -> Decimal | None:
    """Locale-aware: ``1.200,50`` (de/tr), ``1,200.50`` (en), ``1 200,50`` (ru), ``١٬٢٠٠٫٥٠``.
    A single separator followed by exactly three digits is a thousands separator unless it is the
    language's decimal mark (``1,200`` = 1200 in en, 1.2 in de); otherwise it is the decimal mark."""
    s = re.sub(r"[\s\u00a0\u202f\u2009\u2007]", "", s.translate(_TABLE))
    if "\u066b" in s or "\u066c" in s:
        return _dec(s.replace("\u066c", "").replace(",", "").replace("\u066b", "."))
    dots, commas = s.count("."), s.count(",")
    if dots and commas:
        dec = "." if s.rfind(".") > s.rfind(",") else ","
        th = "," if dec == "." else "."
        if s.count(dec) > 1:
            return None
        head, frac = s.split(dec)
        parts = head.split(th)
        if not _groups_ok(parts[0], parts[1:]):
            return None
        return _dec("".join(parts) + "." + frac)
    if dots or commas:
        sep = "." if dots else ","
        parts = s.split(sep)
        if len(parts) > 2:
            return _dec("".join(parts)) if _groups_ok(parts[0], parts[1:]) else None
        head, tail = parts
        if len(tail) == 3 and head != "0" and len(head) <= 3:
            dec_mark = "," if lang in _COMMA_DECIMAL else "."
            if sep != dec_mark:
                return _dec(head + tail)
        return _dec(head + "." + tail)
    return _dec(s)


def _fmt(d: Decimal) -> str:
    s = format(d.normalize(), "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


# ================================================================ language tables
# Strings are written naturally and normalised with the text's own normalisation at compile time.

_NUMWORDS: dict[str, dict[str, int]] = {
    "de": {"null": 0, "eins": 1, "zwei": 2, "zwo": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7,
           "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12, "dreizehn": 13, "vierzehn": 14,
           "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17, "achtzehn": 18, "neunzehn": 19, "zwanzig": 20,
           "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60, "siebzig": 70, "achtzig": 80,
           "neunzig": 90, "hundert": 100},
    "en": {"zero": 0, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
           "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
           "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100},
    "tr": {"sıfır": 0, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9,
           "on": 10, "yirmi": 20, "otuz": 30, "kırk": 40, "elli": 50, "altmış": 60, "yetmiş": 70, "seksen": 80,
           "doksan": 90},
    "ru": {"ноль": 0, "нуль": 0, "два": 2, "две": 2, "двух": 2, "двум": 2, "двумя": 2, "три": 3, "трех": 3,
           "трем": 3, "тремя": 3, "четыре": 4, "четырех": 4, "четырем": 4, "четырьмя": 4, "пять": 5, "пяти": 5,
           "пятью": 5, "шесть": 6, "шести": 6, "семь": 7, "семи": 7, "восемь": 8, "восьми": 8, "девять": 9,
           "девяти": 9, "десять": 10, "десяти": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13,
           "четырнадцать": 14, "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
           "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60,
           "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
           **{w + "и": v for w, v in (("одиннадцать", 11), ("двенадцать", 12), ("тринадцать", 13),
                                      ("четырнадцать", 14), ("пятнадцать", 15), ("шестнадцать", 16),
                                      ("семнадцать", 17), ("восемнадцать", 18), ("девятнадцать", 19),
                                      ("двадцать", 20), ("тридцать", 30))}},
    "uk": {"нуль": 0, "два": 2, "дві": 2, "двох": 2, "три": 3, "трьох": 3, "чотири": 4, "п'ять": 5, "шість": 6,
           "сім": 7, "вісім": 8, "дев'ять": 9, "десять": 10, "одинадцять": 11, "дванадцять": 12,
           "тринадцять": 13, "чотирнадцять": 14, "п'ятнадцять": 15, "шістнадцять": 16, "сімнадцять": 17,
           "вісімнадцять": 18, "дев'ятнадцять": 19, "двадцять": 20},
    "ar": {"صفر": 0, "اثنان": 2, "اثنين": 2, "اثنتان": 2, "اثنتين": 2, "ثلاث": 3, "ثلاثة": 3, "أربع": 4,
           "أربعة": 4, "خمس": 5, "خمسة": 5, "ست": 6, "ستة": 6, "سبع": 7, "سبعة": 7, "ثمان": 8, "ثماني": 8,
           "ثمانية": 8, "تسع": 9, "تسعة": 9, "عشر": 10, "عشرة": 10, "أحد عشر": 11, "إحدى عشرة": 11,
           "اثنا عشر": 12, "اثني عشر": 12, "اثنتا عشرة": 12, "اثنتي عشرة": 12, "عشرون": 20, "عشرين": 20,
           "ثلاثون": 30, "ثلاثين": 30, "أربعون": 40, "أربعين": 40, "خمسون": 50, "خمسين": 50, "مئة": 100,
           "مائة": 100},
}
_TR_UNITS = {"bir": 1, "iki": 2, "üç": 3, "dört": 4, "beş": 5, "altı": 6, "yedi": 7, "sekiz": 8, "dokuz": 9}
_NUMWORDS["tr"].update({f"on {w}": 10 + v for w, v in _TR_UNITS.items()})
_NUMWORDS["tr"].update({f"yirmi {w}": 20 + v for w, v in _TR_UNITS.items()})
_AR_UNITS = {"ثلاث": 3, "ثلاثة": 3, "أربع": 4, "أربعة": 4, "خمس": 5, "خمسة": 5, "ست": 6, "ستة": 6, "سبع": 7,
             "سبعة": 7, "ثمان": 8, "ثماني": 8, "ثمانية": 8, "تسع": 9, "تسعة": 9}
_NUMWORDS["ar"].update({f"{w} {t}": 10 + v for w, v in _AR_UNITS.items() for t in ("عشر", "عشرة")})

_WEAK1: dict[str, list[str]] = {
    "de": ["ein", "eine", "einen", "einem", "einer", "eines"],
    "en": ["one", "a", "an"],
    "tr": ["bir"],
    "ru": ["один", "одна", "одно", "одну", "одного", "одной", "одному", "одним", "одном"],
    "uk": ["один", "одна", "одне", "одну", "одного", "одній", "одним"],
    "ar": ["واحد", "واحدة"],
}
_AR_DUAL_NOUNS = ["قرصين", "قرصان", "حبتين", "حبتان", "كبسولتين", "ملعقتين", "ملعقتان", "نقطتين", "جرعتين",
                  "ورقتين", "نسختين", "كشفين", "قطعتين", "قطعتان"]

_SCALES: dict[str, list[tuple[str, int]]] = {
    "de": [(r"tausend", 10**3), (r"tsd\.?", 10**3), (r"mio\.?", 10**6), (r"millionen|million", 10**6),
           (r"mrd\.?", 10**9), (r"milliarden|milliarde", 10**9), (r"billionen|billion", 10**12)],
    "en": [(r"thousands?", 10**3), (r"millions?|mln", 10**6), (r"billions?|bn", 10**9), (r"trillions?", 10**12)],
    "tr": [(r"bin", 10**3), (r"milyon\w*", 10**6), (r"milyar\w*", 10**9)],
    "ru": [(r"тысяч\w*|тыс\.?", 10**3), (r"миллион\w*|млн\.?", 10**6), (r"миллиард\w*|млрд\.?", 10**9)],
    "uk": [(r"тисяч\w*|тис\.?", 10**3), (r"мільйон\w*|млн\.?", 10**6), (r"мільярд\w*|млрд\.?", 10**9)],
    "ar": [(r"ألف|آلاف|ألفا", 10**3), (r"مليون|ملايين|مليونا", 10**6), (r"مليار|مليارات|مليارا", 10**9)],
}
_SCALE1: dict[str, str] = {  # singular scale word = 1 x scale ("eine Million", "миллион евро")
    "de": r"million|milliarde", "en": r"million|billion", "tr": r"milyon|milyar", "ru": r"миллион|миллиард",
    "uk": r"мільйон|мільярд", "ar": r"مليون|مليار|ألف",
}
_AR_DUAL_SCALES = {"ألفين": 2000, "ألفان": 2000, "مليونين": 2 * 10**6, "مليونان": 2 * 10**6,
                   "مليارين": 2 * 10**9, "ملياران": 2 * 10**9}

_CUR_SYMBOLS = [(r"€", "EUR"), (r"\$", "USD"), (r"£", "GBP"), (r"₺", "TRY"), (r"₽", "RUB"), (r"₴", "UAH"),
                (r"eur", "EUR"), (r"usd", "USD"), (r"gbp", "GBP"), (r"chf", "CHF")]
_CURRENCIES: dict[str, list[tuple[str, str]]] = {
    "de": [(r"euro|euros", "EUR"), (r"us-dollar|dollar", "USD"), (r"franken", "CHF"), (r"lira", "TRY"),
           (r"rubel", "RUB")],
    "en": [(r"euros?", "EUR"), (r"dollars?|bucks", "USD"), (r"francs?", "CHF"), (r"liras?", "TRY"),
           (r"roubles?|rubles?", "RUB")],
    "tr": [(r"euro\w*|avro\w*", "EUR"), (r"dolar\w*", "USD"), (r"sterlin\w*", "GBP"), (r"lira\w*|tl", "TRY"),
           (r"ruble\w*", "RUB"), (r"frank\w*", "CHF")],
    "ru": [(r"евро|euros?", "EUR"), (r"доллар\w*", "USD"), (r"фунт\w* стерлинг\w*", "GBP"), (r"лир\w*", "TRY"),
           (r"рубл\w*|руб\.?", "RUB"), (r"франк\w*", "CHF")],
    "uk": [(r"євро|euros?", "EUR"), (r"долар\w*", "USD"), (r"гривн\w*|грн\.?", "UAH"), (r"рубл\w*", "RUB")],
    "ar": [(r"يورو|euros?", "EUR"), (r"دولار\w*", "USD"), (r"جنيه إسترليني", "GBP"), (r"ليرة|ليرات", "TRY"),
           (r"روبل", "RUB")],
}

_D = Decimal
_UNITS_COMMON: list[tuple[str, str, Decimal]] = [
    (r"mg", "mg", _D(1)), (r"µg|mcg|ug", "mg", _D("0.001")), (r"kg", "mg", _D(10**6)), (r"g|gr", "mg", _D(1000)),
    (r"ml", "ml", _D(1)), (r"l", "ml", _D(1000)), (r"i\.\s?e\.|i\.\s?u\.|ie|iu", "IE", _D(1)),
]
_UNITS: dict[str, list[tuple[str, str, Decimal]]] = {
    "de": [(r"milligramm", "mg", _D(1)), (r"mikrogramm", "mg", _D("0.001")), (r"kilogramm|kilo", "mg", _D(10**6)),
           (r"gramm", "mg", _D(1000)), (r"milliliter", "ml", _D(1)), (r"liter", "ml", _D(1000)),
           (r"einheiten|einheit", "IE", _D(1))],
    "en": [(r"milligrams?|milligrammes?", "mg", _D(1)), (r"micrograms?", "mg", _D("0.001")),
           (r"kilograms?|kilos?", "mg", _D(10**6)), (r"grams?|grammes?", "mg", _D(1000)),
           (r"millilit(?:er|re)s?", "ml", _D(1)), (r"lit(?:er|re)s?", "ml", _D(1000)),
           (r"(?:international )?units?", "IE", _D(1))],
    "tr": [(r"miligram\w*", "mg", _D(1)), (r"mikrogram\w*", "mg", _D("0.001")), (r"kilogram\w*|kilo", "mg", _D(10**6)),
           (r"gram\w*", "mg", _D(1000)), (r"mililitre\w*|cc", "ml", _D(1)), (r"litre\w*", "ml", _D(1000)),
           (r"ünite\w*", "IE", _D(1))],
    "ru": [(r"мг|миллиграмм\w*", "mg", _D(1)), (r"мкг|микрограмм\w*", "mg", _D("0.001")),
           (r"кг|килограмм\w*", "mg", _D(10**6)), (r"г|гр|грамм\w*", "mg", _D(1000)),
           (r"мл|миллилитр\w*", "ml", _D(1)), (r"л|литр\w*", "ml", _D(1000)), (r"ме|ед\.?|единиц\w*", "IE", _D(1))],
    "uk": [(r"мг|міліграм\w*", "mg", _D(1)), (r"мкг|мікрограм\w*", "mg", _D("0.001")), (r"кг", "mg", _D(10**6)),
           (r"г|грам\w*", "mg", _D(1000)), (r"мл|мілілітр\w*", "ml", _D(1)), (r"л|літр\w*", "ml", _D(1000)),
           (r"од\.?|одиниц\w*", "IE", _D(1))],
    "ar": [((r"ملغ|ملغم|مغ|مجم|ملجم|ملليغرام|مليغرام|ميليغرام|ملغرام|ملليجرام|مليجرام|ميليجرام|"
             r"ملي غرام|مللي غرام|ميلي غرام|ملي جرام|مللي جرام|ميلي جرام"), "mg", _D(1)),
           (r"ميكروغرام|ميكروجرام|مكغ", "mg", _D("0.001")), (r"كغ|كجم|كيلوغرام|كيلوجرام|كيلو", "mg", _D(10**6)),
           (r"غرام|غرامات|جرام|جرامات|غم|جم|غ", "mg", _D(1000)),
           (r"مل|ملل|مليلتر|ملليلتر|ميليلتر|مليلترات|ملي لتر|مللي لتر|ميلي لتر", "ml", _D(1)), (r"لتر|لترات", "ml", _D(1000)),
           (r"وحدة دولية|وحدات دولية|وحدة|وحدات", "IE", _D(1))],
}
_UNIT_SYMBOL = {("mg", _D(1)): "mg", ("mg", _D(1000)): "g", ("mg", _D("0.001")): "µg", ("mg", _D(10**6)): "kg",
                ("ml", _D(1)): "ml", ("ml", _D(1000)): "l", ("IE", _D(1)): "IE"}
_SOLO_UNIT_MIN_LEN = 4  # standalone unit words ("in Milligramm"), never bare symbols like "g"
# count words after a number ("10.000 Stück" = "10 000 штук" = "10.000 adet" = "10000 قطعة"): the value stays a
# number ("10000"), and the count is told apart from an amount ("10.000 Stück" -> "10.000 Euro" is red)
_COUNT = {"de": r"stück|stücke|stk\.?", "en": r"pieces?|pcs\.?", "tr": r"adet\w*", "ru": r"штук\w*|шт\.?",
          "uk": r"штук\w*|шт\.?", "ar": r"قطعة|قطع"}

_DUR: dict[str, list[tuple[str, str]]] = {
    "de": [(r"tag|tage|tagen|tages", "D"), (r"woche|wochen", "W"), (r"monat|monate|monaten|monats", "M"),
           (r"jahr|jahre|jahren|jahres", "Y"), (r"stunde|stunden", "H"), (r"minute|minuten", "MIN")],
    "en": [(r"days?", "D"), (r"weeks?", "W"), (r"months?", "M"), (r"years?", "Y"), (r"hours?|hrs?", "H"),
           (r"minutes?|mins?", "MIN")],
    "tr": [(r"gün\w*", "D"), (r"hafta\w*", "W"), (r"ay|aydır|ayda|ayı|aylık|aylar\w*", "M"),
           (r"yıl\w*|sene\w*", "Y"), (r"saat\w*", "H"), (r"dakika\w*", "MIN")],
    "ru": [(r"день|дня|дней|дням|днями|сутки|суток", "D"), (r"неделя|недели|недель|неделю|неделями", "W"),
           (r"месяц|месяца|месяцев|месяцы", "M"), (r"год|года|лет|годы|годам", "Y"), (r"час|часа|часов", "H"),
           (r"минута|минуты|минут|минуту", "MIN")],
    "uk": [(r"день|дні|днів", "D"), (r"тиждень|тижні|тижнів", "W"), (r"місяць|місяці|місяців", "M"),
           (r"рік|роки|років", "Y"), (r"година|години|годин", "H"), (r"хвилин\w*", "MIN")],
    "ar": [(r"يوم|أيام|يوما", "D"), (r"أسبوع|أسابيع|أسبوعا", "W"), (r"شهر|أشهر|شهور|شهرا", "M"),
           (r"سنة|سنوات|سنين|عام|أعوام|عاما", "Y"), (r"ساعة|ساعات", "H"), (r"دقيقة|دقائق", "MIN")],
}
_AR_DUAL_DUR = [(r"يومين|يومان", "D"), (r"أسبوعين|أسبوعان", "W"), (r"شهرين|شهران", "M"),
                (r"سنتين|سنتان|عامين|عامان", "Y"), (r"ساعتين|ساعتان", "H"), (r"دقيقتين|دقيقتان", "MIN")]

_PCT_WORDS = {"de": r"prozent", "en": r"percent|per cent|pct", "tr": r"", "ru": r"процент\w*",
              "uk": r"відсот\w*", "ar": r"بالمئة|بالمائة|في المئة|في المائة|بالمية|في المية"}

_MONTHS: dict[str, list[tuple[str, int]]] = {
    "de": [(r"januar|jänner|jan\.", 1), (r"februar|feber|feb\.", 2), (r"märz|maerz|mär\.|mrz\.", 3),
           (r"april|apr\.", 4), (r"mai", 5), (r"juni|jun\.", 6), (r"juli|jul\.", 7), (r"august|aug\.", 8),
           (r"september|sept\.|sep\.", 9), (r"oktober|okt\.", 10), (r"november|nov\.", 11),
           (r"dezember|dez\.", 12)],
    "en": [(r"january|jan\.?", 1), (r"february|feb\.?", 2), (r"march|mar\.", 3), (r"april|apr\.?", 4),
           (r"may", 5), (r"june|jun\.?", 6), (r"july|jul\.?", 7), (r"august|aug\.?", 8),
           (r"september|sept\.?|sep\.?", 9), (r"october|oct\.?", 10), (r"november|nov\.?", 11),
           (r"december|dec\.?", 12)],
    "tr": [(r"ocak", 1), (r"şubat", 2), (r"mart", 3), (r"nisan", 4), (r"mayıs", 5), (r"haziran", 6),
           (r"temmuz", 7), (r"ağustos", 8), (r"eylül", 9), (r"ekim", 10), (r"kasım", 11), (r"aralık", 12)],
    "ru": [(r"январ[ьяеюм]", 1), (r"феврал[ьяеюм]", 2), (r"март[аеу]?", 3), (r"апрел[ьяеюм]", 4),
           (r"ма[йяею]", 5), (r"июн[ьяеюм]", 6), (r"июл[ьяеюм]", 7), (r"август[аеу]?", 8),
           (r"сентябр[ьяеюм]", 9), (r"октябр[ьяеюм]", 10), (r"ноябр[ьяеюм]", 11), (r"декабр[ьяеюм]", 12)],
    "uk": [(r"січ(?:ень|ня|ні)", 1), (r"лют(?:ий|ого|ому)", 2), (r"берез(?:ень|ня|ні)", 3),
           (r"квіт(?:ень|ня|ні)", 4), (r"трав(?:ень|ня|ні)", 5), (r"черв(?:ень|ня|ні)", 6),
           (r"лип(?:ень|ня|ні)", 7), (r"серп(?:ень|ня|ні)", 8), (r"верес(?:ень|ня|ні)", 9),
           (r"жовт(?:ень|ня|ні)", 10), (r"листопад[аі]?", 11), (r"груд(?:ень|ня|ні)", 12)],
    "ar": [(r"يناير|كانون الثاني|كانون ثاني|جانفي", 1), (r"فبراير|شباط|فيفري", 2), (r"مارس|آذار", 3),
           (r"أبريل|ابريل|نيسان|أفريل", 4), (r"مايو|أيار|ماي", 5), (r"يونيو|يونيه|حزيران|جوان", 6),
           (r"يوليو|يوليه|تموز|جويلية", 7), (r"أغسطس|آب|أوت|غشت", 8), (r"سبتمبر|أيلول|شتنبر", 9),
           (r"أكتوبر|تشرين الأول|تشرين أول", 10), (r"نوفمبر|تشرين الثاني|تشرين ثاني|نونبر", 11),
           (r"ديسمبر|كانون الأول|كانون أول|دجنبر", 12)],
}
# month names that are also ordinary words: only taken alone (without a day) with extra evidence
_MONTH_SOLO_RISKY = {"en": {"may", "march"}, "ar": {"مارس", "آب", "ماي"}}
_AR_MONTH_CTX = r"(?:في|حتى|بحلول|شهر|منذ|قبل|بعد|من|إلى|الى|أوائل|أواخر|منتصف|نهاية|بداية|لغاية)"

_WEEKDAYS: dict[str, list[tuple[str, int]]] = {
    "de": [(r"montags?", 1), (r"dienstags?", 2), (r"mittwochs?", 3), (r"donnerstags?", 4), (r"freitags?", 5),
           (r"samstags?|sonnabends?", 6), (r"sonntags?", 7)],
    "en": [(r"mondays?", 1), (r"tuesdays?", 2), (r"wednesdays?", 3), (r"thursdays?", 4), (r"fridays?", 5),
           (r"saturdays?", 6), (r"sundays?", 7)],
    "tr": [(r"pazartesi(?:'\w+|\w*)", 1), (r"salı(?:'\w+|\w*)", 2), (r"çarşamba(?:'\w+|\w*)", 3),
           (r"perşembe(?:'\w+|\w*)", 4), (r"cumartesi(?:'\w+|\w*)", 6), (r"cuma(?:'\w+|\w*)", 5),
           (r"pazar(?=\s+gün)|pazar'\w+", 7)],
    "ru": [(r"понедельник\w*", 1), (r"вторник\w*", 2), (r"сред[ауы]", 3), (r"четверг\w*", 4),
           (r"пятниц[аеуы]", 5), (r"суббот[аеуы]", 6), (r"воскресень[еяю]", 7)],
    "uk": [(r"понеділ(?:ок|ка|ку)", 1), (r"вівтор(?:ок|ка)", 2), (r"серед[аиу]", 3), (r"четвер\w*", 4),
           (r"п'ятниц[яіюі]", 5), (r"субот[аиу]", 6), (r"неділ[яіюь]", 7)],
    "ar": [(r"يوم (?:ال)?(?:اثنين|إثنين)", 1), (r"(?:يوم )?(?:ال)?ثلاثاء", 2),
           (r"(?:يوم )?(?:ال)?أربعاء", 3), (r"(?:يوم )?(?:ال)?خميس", 4), (r"(?:يوم )?(?:ال)?جمعة", 5),
           (r"(?:يوم )?(?:ال)?سبت", 6), (r"يوم (?:ال)?أحد", 7)],
}
_WD_NAMES = {1: "mon", 2: "tue", 3: "wed", 4: "thu", 5: "fri", 6: "sat", 7: "sun"}

_ALLERGY = {"de": r"\w*allergi\w*|unverträglich\w*", "en": r"allerg\w*", "tr": r"alerj\w*", "ru": r"аллерги\w*",
            "uk": r"алергі\w*", "ar": r"حساسي\w*|تحسس\w*"}
_CONTRAST = {"de": r"aber|jedoch|sondern|deshalb|daher|darum|weil|denn", "en": r"but|however|so|therefore|because",
             "tr": r"ama|fakat|ancak|çünkü|için|bu yüzden|bu nedenle", "ru": r"но|однако|поэтому|потому|так что",
             "uk": r"але|однак|тому", "ar": r"لكن|ولكن|لكنني|ولكنني|لذلك|لذا|لأن"}

_NEG = {
    "de": r"nicht|kein\w*|nie|niemals|nichts|niemand\w*|nein|weder|nirgend\w*",
    "en": r"not|no|never|none|nothing|nobody|neither|nor|cannot|\w+n't|dont|doesnt|didnt|cant|wont|isnt|arent|"
          r"wasnt|werent|shouldnt|wouldnt|couldnt|mustnt|havent|hasnt|hadnt",
    "tr": r"değil\w*|yok\w*|hiç\w*|asla|hayır|\w+m[ıiuü]yor\w*|\w+m[ae]y[ıi]n(?:[ıi]z)?|\w+m[ae]m[ae]l[ıi]\w*|"
          r"\w+m[ae]d[ıiuü]\w*|\w+m[ae]y[ae]c[ae]k\w*|\w+m[ae]z(?:s[ae]\w*|l[ae]r\w*|s[ıi]n\w*)?|"
          r"\w+m[ae]m[ıiuü]ş\w*|\w+m[ae]m[ae]k\w*|\w+m[ae]y[ıi]z\w*|(?!(?:tamam|hamam|imam)(?!\w))\w+m[ae]m",
    "ru": r"не|нет|ни|нельзя|никогда|ничего|ничто|никто|никак\w*|нигде",
    "uk": r"не|ні|ніколи|нічого|немає|нема",
    "ar": r"[وف]?(?:لا|لم|لن|ليس|ليست|لست|لسنا|ليسوا|لستم|غير|أبدا|مش|مو|بلاش|أليس)",
}
_NEG_EXCL = {  # "at most / at the latest / at least": not a negation of the fact
    "de": r"nicht (?:mehr|später|länger|weniger|früher) als|nicht nur",
    "en": r"no[t]? (?:later|more|less|longer|fewer|earlier) than|not only",
    "tr": r"fazla olma\w*",
    "ru": r"не (?:более|позднее|позже|менее|больше|раньше|ранее|только)",
    "uk": r"не (?:пізніше|більше|менше|тільки)",
    "ar": r"لا (?:يزيد|تزيد|يتجاوز|تتجاوز|يقل|تقل)\w*|ليس فقط",
}

_DL_STRONG = {
    "de": r"\w*frist\w*|spätestens|ablauf\w*|abläuft|läuft|endet|enden|fällig\w*|stichtag|deadline",
    "en": r"deadline|due|expires?|expiry|ends?|no later than|not later than|cut-?off|time limit|limitation period",
    "tr": r"süre\w*|son gün\w*|son tarih\w*|vade\w*|sona|bitiyor|biter|dolmadan|doluyor|en geç",
    "ru": r"срок\w*|истека\w*|истечет|не позднее|не позже|заканчива\w*|оканчива\w*",
    "uk": r"строк\w*|термін\w*|спливає|не пізніше",
    "ar": r"مهلة|آخر موعد|موعد نهائي|الموعد النهائي|تنتهي|ينتهي|انتهاء|أقصاه|موعد أقصاه",
}
_DL_WEAK_BEFORE = {  # must be right in front of the date
    "de": r"bis(?: (?:zum|zur|spätestens|am|ende|anfang|mitte))?(?: (?:den|dem))?",
    "en": r"by|until|till|before(?: the)?|by the|until the",
    "tr": r"en geç",
    "ru": r"до|к",
    "uk": r"до",
    "ar": r"حتى|بحلول|لغاية|قبل",
}
_DL_AFTER = {"tr": r"(?:gün\w*\s+|ay\w*\s+)?kadar"}

_JOIN = re.compile(r"[\s\-]*(?:(?:,|\u060c|und|and|ve|ile|и|или|oder|or|veya|و)\s*)?[\s\-]*")
_DATE_GLUE = re.compile(r"[\s,]*(?:den|dem|der|the|le)?[\s,]*")
_TITLES = r"herr|herrn|frau|dr\.|prof\.|mr\.?|mrs\.?|ms\.?|miss|sayın|bay|bayan|господин|госпожа|السيد|السيدة"


# ================================================================ compiled per-language pack


def _alt(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _ralt(frags: Iterable[str]) -> str:
    return "|".join(sorted(frags, key=len, reverse=True))


class _Lex:
    """Alternation of regex fragments + lookup of the value of the fragment that matched."""

    def __init__(self, items: Iterable[tuple[str, Any]], lang: str) -> None:
        self.items = [(re.compile(_norm_pat(p, lang)), v) for p, v in items]
        self.alt = _ralt(_norm_pat(p, lang) for p, _ in items) if self.items else r"(?!x)x"

    def get(self, s: str) -> Any:
        for rx, v in self.items:
            if rx.fullmatch(s):
                return v
        return None


@dataclass
class _Pack:
    lang: str
    words: dict[str, int]
    weak: dict[str, int]
    scales: _Lex
    cur: _Lex
    units: _Lex
    dur: _Lex
    months: _Lex
    weekdays: _Lex
    rx: dict[str, re.Pattern[str]]
    lists: dict[str, list[tuple[re.Pattern[str], str]]]


_PACKS: dict[str, _Pack] = {}


def _pack(lang: str) -> _Pack:
    p = _PACKS.get(lang)
    if p is None:
        p = _PACKS[lang] = _build_pack(lang)
    return p


def _build_pack(lang: str) -> _Pack:
    L = lang if lang in _NUMWORDS else ""
    n = lambda s: _norm_pat(s, lang)

    words = {n(k): v for k, v in _NUMWORDS.get(L, {}).items()}
    if L == "ar":
        words.update({pre + k: v for k, v in list(words.items()) for pre in ("و", "ال", "وال")})
    weak = {n(k): 1 for k in _WEAK1.get(L, [])}
    if L == "ar":
        weak.update({pre + k: 1 for k in list(weak) for pre in ("و", "ال")})
        weak.update({n(k): 2 for k in _AR_DUAL_NOUNS})
    numw = _alt(words) or r"(?!x)x"
    numw_any = _alt({**words, **weak}) if (words or weak) else r"(?!x)x"
    dig = _DIG_SPACE if lang in _SPACE_THOUSANDS else _DIG_NBSP
    NUM = rf"(?:{dig}|(?<!\w)(?:{numw})(?!\w))"
    NUM_ANY = rf"(?:{dig}|(?<!\w)(?:{numw_any})(?!\w))"
    NUM_M = rf"(?:{_DIG_SPACE}|(?<!\w)(?:{numw})(?!\w))"  # before a currency or a count word

    scales = _Lex(_SCALES.get(L, []), lang)
    cur = _Lex([*_CURRENCIES.get(L, []), *_CUR_SYMBOLS], lang)
    units = _Lex([(u, (c, f)) for u, c, f in (*_UNITS.get(L, []), *_UNITS_COMMON)], lang)
    dur = _Lex(_DUR.get(L, _DUR["en"] if not L else []), lang)
    months = _Lex(_MONTHS.get(L, _MONTHS["en"] if not L else []), lang)
    weekdays = _Lex(_WEEKDAYS.get(L, _WEEKDAYS["en"] if not L else []), lang)
    B = r"(?!\w)"
    apo = r"(?:'\w+)?"
    rx: dict[str, str] = {}
    lists: dict[str, list[tuple[str, str]]] = {}

    # -------------------------------------------------- dates
    mon = months.alt
    d = r"(?P<d>[0-3]?\d)"
    y = r"(?P<y>\d{4})"
    dates: list[tuple[str, str]] = [
        (r"(?<![\d.,])(?P<y>\d{4})-(?P<mo>[01]?\d)-(?P<d>[0-3]?\d)(?!\d)", "dmy"),
        (rf"(?<![\d.,/]){d}[./-](?P<mo>[01]?\d)[./-](?P<y>\d{{4}}|\d{{2}})(?![\d.,]\d)(?!\d)", "dmy"),
    ]
    if L in ("de", "tr", "ru", "uk") or (not L and lang in _COMMA_DECIMAL):
        dates.append((rf"(?<![\d.,]){d}\.(?P<mo>[01]?\d)\.(?!\d)", "dmy"))
    dates.append((rf"(?<![\d.,/]){d}/(?P<mo>[01]?\d)(?![\d/])", "dm_slash"))
    if L == "de":
        dates.append((rf"(?<![\d.,]){d}\.?\s*(?P<mon>{mon}){B}(?:\.?\s*{y})?", "dmy"))
    elif L in ("en", ""):
        dates.append((rf"(?<!\w)(?P<mon>{mon})\s+(?:the\s+)?{d}(?:st|nd|rd|th)?{B}(?:,?\s+{y})?", "dmy"))
        dates.append((rf"(?<![\d.,])(?:the\s+)?{d}(?:st|nd|rd|th)?\s+(?:of\s+)?(?P<mon>{mon}){B}(?:,?\s+{y})?",
                      "dmy"))
    elif L == "tr":
        dates.append((rf"(?<![\d.,]){d}\.?\s+(?P<mon>{mon})(?:'\w+|\w*)(?:\s+{y})?", "dmy"))
    elif L in ("ru", "uk"):
        dates.append(((rf"(?<![\d.,]){d}(?:-?(?:го|е|ого))?\s+(?P<mon>{mon}){B}"
                      rf"(?:\s+{y}(?:\s*(?:г\.|года|год|р\.|року|рік)|\s*г{B})?)?"), "dmy"))
    elif L == "ar":
        dates.append((rf"(?<![\d.,]){d}\s+(?:من\s+)?(?:شهر\s+)?(?P<mon>{mon}){B}(?:\s+(?:عام\s+|سنه\s+)?{y})?",
                      "dmy"))
    # day without month ("am 12.", "on the 12th", "12-го", "ayın 12'si")
    day_only = {
        "de": rf"(?<!\w)(?:am|dem|den|zum|vom|ab dem|bis zum)\s+{d}\.(?![\d\w])",
        "en": rf"(?<!\w)the\s+{d}(?:st|nd|rd|th){B}(?=\s*(?:[,.;!?)]|$|at\s|in\s|of\s+(?:this|the|next)\s+month))",
        "tr": rf"(?<!\w)ayın\s+{d}(?:'\w+)?{B}",
        "ru": rf"(?<![\d.,]){d}(?:-?го\b|\s+числа{B})",
        "ar": rf"(?<!\w)(?:يوم|في)\s+{d}\s+من\s+الشهر",
    }.get(L)
    if day_only:
        dates.append((day_only, "day"))
    lists["dates"] = [(n(a), b) for a, b in dates]
    rx["weekday"] = rf"(?<!\w)(?:{weekdays.alt}){B}"
    rx["month_solo"] = rf"(?<!\w)(?P<mon>{mon}){B}" if L != "tr" else rf"(?<!\w)(?P<mon>{mon})(?:'\w+|\w*)(?P<ay>\s+ay\w*)?"

    # -------------------------------------------------- times
    H = r"(?P<h>[01]?\d|2[0-3])"
    M = r"(?::(?P<m>[0-5]\d))"
    MD = r"(?:[:.](?P<m>[0-5]\d)(?!\d))"  # not "10.000" (a number, not 10:00)
    times: list[tuple[str, str]] = []
    if L == "de":
        mer = r"(?P<mer>morgens|früh|vormittags|mittags|nachmittags|abends|nachts)"
        times += [
            ((rf"(?<!\w)(?:(?P<mer0>morgens|früh|vormittags|nachmittags|abends|nachts)\s+)?(?:um\s+)?"
             rf"(?P<h>[01]?\d|2[0-3]|{numw}){MD}?\s*uhr{B}(?:\s+{mer})?"), "t"),
            ((rf"(?<!\w)(?:(?P<mer0>morgens|früh|vormittags|nachmittags|abends)\s+)?um\s+{H}{MD}?"
             rf"(?=\s*(?:[,.](?!\d)|[;!?)]|$|morgens|abends|früh|vormittags|nachmittags|wieder|zurück|in\s|bei\s|am\s|im\s))"
             rf"(?:\s*{mer})?"), "t"),
        ]
    elif L in ("en", ""):
        times += [
            ((r"(?<![\d.])(?P<h>1[0-2]|0?[1-9])(?::(?P<m>[0-5]\d))?\s*(?P<mer>a\.\s?m\.|p\.\s?m\.|am|pm|a\.m|p\.m)"
             r"(?!\w)"), "t"),
            ((rf"(?<!\w)(?P<h>1[0-2]|0?[1-9]|{numw})\s+o'?clock{B}"
             r"(?:\s+(?P<mer>in the morning|in the afternoon|in the evening|at night))?"), "t"),
            ((rf"(?<!\w)at\s+{H}{MD}?(?=\s*(?:[,.](?!\d)|[;!?)]|$|in the|tomorrow|today|tonight|sharp|on\s))"
             r"(?:\s+(?P<mer>in the morning|in the afternoon|in the evening|at night))?"), "t"),
        ]
    elif L == "tr":
        times += [
            ((rf"(?<!\w)(?:(?P<mer0>sabah|akşam|öğleden sonra|öğlen|gece)\s+)?saat\s+(?P<h>[01]?\d|2[0-3]|{numw})"
             rf"{MD}?(?:'\w+|d[ae]|t[ae])?{B}"), "t"),
            (rf"(?<!\w)(?:(?P<mer0>sabah|akşam|öğleden sonra|öğlen|gece)\s+)?{H}{MD}?'(?:d[ae]|t[ae]){B}", "t"),
        ]
    elif L == "ru":
        times += [
            ((rf"(?<!\w)в\s+(?P<h>[01]?\d|2[0-3]|{numw}){MD}?(?:\s*(?P<hw>час(?:а|ов)?|ч\.?){B})?"
             rf"(?:\s+(?P<mer>утра|дня|вечера|ночи))?"), "t_ru"),
            (rf"(?<![\d.,]){H}{MD}?\s+(?P<mer>утра|дня|вечера|ночи){B}", "t"),
        ]
    elif L == "uk":
        times += [(rf"(?<!\w)о\s+{H}{MD}?\s*(?:годині|год\.?){B}", "t")]
    elif L == "ar":
        ords = {"الواحدة": 1, "الثانية": 2, "الثالثة": 3, "الرابعة": 4, "الخامسة": 5, "السادسة": 6,
                "السابعة": 7, "الثامنة": 8, "التاسعة": 9, "العاشرة": 10, "الحادية عشرة": 11, "الثانية عشرة": 12}
        words.update({n(k): v for k, v in ords.items()})
        ordalt = _alt(n(k) for k in ords)
        mer = r"(?P<mer>صباحا|صباحًا|مساء|مساءً|مساءا|ص|م|ظهرا|عصرا|ليلا|فجرا|بعد الظهر|في الصباح|في المساء)"
        times += [
            ((rf"(?<!\w)(?:في\s+)?الساعة\s+(?P<h>[01]?\d|2[0-3]|{ordalt}|{numw}){MD}?"
             rf"(?:\s+(?P<frac>والنصف|و\s*النصف|والربع|و\s*ربع|إلا\s+ربعا?|الا\s+ربعا?))?(?:\s+{mer})?{B}"), "t"),
            (rf"(?<![\d.,:]){H}{M}?\s+{mer}{B}", "t"),
        ]
    times.append((rf"(?<![\d:.,]){H}{M}(?![\d:])", "t"))
    lists["times"] = [(rx_, k) for rx_, k in ((n(a), b) for a, b in times)]

    # -------------------------------------------------- frequency
    freq: list[tuple[str, str]] = []
    if L == "de":
        per = r"(?P<per>täglich|am tag|pro tag|jeden tag|am tage|die woche|pro woche|in der woche|wöchentlich|" \
              r"jede woche|im monat|pro monat|monatlich)"
        freq += [
            (rf"(?<!\w)(?P<w>ein|zwei|drei|vier|fünf|sechs|sieben|acht|neun|zehn)mal\s+{per}{B}", "n"),
            (rf"(?<![\w.,])(?P<n>\d+)\s*(?:x|×|-?mal)\s+{per}{B}", "n"),
            (rf"(?<!\w)alle\s+(?P<n>{NUM})\s+(?P<q>stunden|tage){B}", "q"),
            (r"(?<!\w)(?P<q1>stündlich){B}", "q"),
            (r"(?<!\w)(?P<per1>täglich|jeden tag|pro tag|wöchentlich|jede woche|pro woche|monatlich|pro monat)(?!\w)",
             "one"),
        ]
    elif L in ("en", ""):
        per = r"(?P<per>a day|per day|each day|every day|daily|a week|per week|every week|weekly|a month|" \
              r"per month|monthly)"
        freq += [
            (rf"(?<!\w)(?P<w>once|twice|thrice)\s+{per}{B}", "n"),
            (rf"(?<!\w)(?P<n>{NUM})\s*(?:times|x|×)\s+{per}{B}", "n"),
            (rf"(?<!\w)every\s+(?P<n>{NUM})\s+(?P<q>hours?|days?){B}", "q"),
            (r"(?<!\w)(?P<q1>hourly|every hour){B}", "q"),
            (r"(?<!\w)(?P<q2>every other day){B}", "q"),
            (r"(?<!\w)(?P<per1>daily|every day|each day|per day|weekly|every week|per week|monthly|per month)(?!\w)",
             "one"),
            (r"(?<=mg|ml|µg|iu|ts|ls|es|ps)\s+(?P<per1>a day|a week)(?!\w)", "one"),
        ]
    elif L == "tr":
        times_w = r"(?:kez|kere|defa|sefer)"
        freq += [
            (rf"(?<!\w)(?P<per>günde|haftada|ayda)\s+(?P<n>{NUM_ANY})\s+{times_w}{B}", "n"),
            (rf"(?<!\w)(?:her\s+)?(?P<n>{NUM_ANY})\s+(?P<q>saat|gün)te\s+bir{B}", "q"),
            (rf"(?<!\w)(?:her\s+)?(?P<n>{NUM_ANY})\s+(?P<q>saat|gün)de\s+bir{B}", "q"),
            (r"(?<!\w)(?P<per1>her gün|günlük|günde|her hafta|haftalık|haftada)(?!\w)", "one"),
        ]
    elif L == "ru":
        per = r"(?P<per>в день|в сутки|в неделю|в месяц)"
        freq += [
            (rf"(?<!\w)(?P<n>{NUM_ANY})\s+раза?\s+{per}{B}", "n"),
            (rf"(?<!\w)(?P<w>дважды|трижды|однажды)\s+{per}{B}", "n"),
            (rf"(?<!\w)раз\s+{per}{B}", "n1"),
            (rf"(?<!\w)кажд(?:ые|ый|ою|ую)\s+(?P<n>{NUM})\s+(?P<q>час(?:а|ов)?|дн(?:я|ей)){B}", "q"),
            (r"(?<!\w)(?P<q1>каждый час|ежечасно){B}", "q"),
            (r"(?<!\w)(?P<per1>ежедневно|каждый день|еженедельно|каждую неделю|ежемесячно)(?!\w)", "one"),
            ((r"(?<!\w)(?P<per1>в день|в сутки|в неделю)(?=\s*(?:[,.;:!?)]|$|перед|после|во время|натощак|утром|"
             r"вечером|до еды))"), "one"),
        ]
    elif L == "uk":
        freq += [
            (rf"(?<!\w)(?P<n>{NUM_ANY})\s+раз(?:и|ів)?\s+(?P<per>на день|на добу|на тиждень|на місяць){B}", "n"),
            (r"(?<!\w)(?P<per1>щодня|щоденно|щотижня)(?!\w)", "one"),
        ]
    elif L == "ar":
        per = r"(?P<per>يوميا|في اليوم|باليوم|كل يوم|أسبوعيا|في الأسبوع|بالأسبوع|كل أسبوع|شهريا|في الشهر)"
        freq += [
            (rf"(?<!\w)و?مرة\s+(?:واحدة\s+)?{per}{B}", "n1"),
            (rf"(?<!\w)و?(?:مرتين|مرتان)\s+{per}{B}", "n2"),
            (rf"(?<!\w)(?P<n>{NUM})\s+مرات\s+{per}{B}", "n"),
            (rf"(?<!\w)كل\s+(?P<n>{NUM})\s+(?P<q>ساعات|ساعة|أيام|يوم){B}", "q"),
            (r"(?<!\w)كل\s+(?P<q1>ساعة){B}", "q"),
            (r"(?<!\w)(?P<per1>يوميا|كل يوم|أسبوعيا|كل أسبوع|شهريا)(?!\w)", "one"),
            (r"(?<!\w)(?P<per1>في اليوم|باليوم)(?!\s+(?:التالي|نفسه|الأول|الثاني|الذي|السابق))(?!\w)", "one"),
        ]
    lists["freq"] = [(n(a), b) for a, b in freq]

    # -------------------------------------------------- percent, amounts, doses, durations
    pw = _PCT_WORDS.get(L, "")
    pct = [(rf"(?P<n>{NUM})\s*(?:%|٪|(?:{n(pw)}){B})" if pw else rf"(?P<n>{NUM})\s*(?:%|٪)", "pct")]
    if L in ("tr", "ar"):
        pct.append((rf"(?<!\w)(?:%|٪)\s*(?P<n>{dig})", "pct"))
    if L == "tr":
        pct.append((rf"(?<!\w)yüzde\s+(?P<n>{NUM}){apo}{B}", "pct"))
    lists["pct"] = pct
    SC = scales.alt
    CUR = cur.alt
    CURP = _ralt(n(p) for p, _ in _CUR_SYMBOLS)
    amt = [
        (rf"(?<!\w)(?P<cur>{CURP})\s*(?P<n>{NUM_M})(?:\s*(?P<sc>{SC}){B})?", "amt"),
        (rf"(?P<n>{NUM_M})(?:\s*(?P<sc>{SC}))?\s*(?P<cur>{CUR}){apo}(?!\w)", "amt"),
        (rf"(?P<n>{NUM})\s*(?P<sc>{SC}){apo}{B}", "amt"),
    ]
    if L in _SCALE1:
        w1 = _alt(_WEAK1.get(L, []) and [n(w) for w in _WEAK1[L]])
        amt.append((rf"(?<!\w)(?:(?:{w1})\s+)?(?P<sc1>{n(_SCALE1[L])}){B}(?:\s*(?P<cur>{CUR}){apo}{B})?", "amt1"))
    if L == "ar":
        amt.append((rf"(?<!\w)(?P<dual>{_alt(n(k) for k in _AR_DUAL_SCALES)}){B}(?:\s*(?P<cur>{CUR}){B})?", "amt2"))
    lists["amt"] = amt
    rx["dose"] = rf"(?P<n>{NUM})\s*(?P<u>{units.alt}){apo}(?!\w)"
    rx["dur"] = rf"(?P<n>{NUM})\s+(?P<u>{dur.alt}){B}"
    rx["count"] = rf"(?P<n>{NUM_M})\s*(?:{n(_COUNT[L])}){apo}(?!\w)" if L in _COUNT else r"(?!x)x"
    if L == "ar":
        rx["dur2"] = rf"(?<!\w)(?P<u>{_ralt(n(p) for p, _ in _AR_DUAL_DUR)}){B}"
    rx["dig"] = dig
    rx["numw"] = rf"(?<!\w)(?:{numw}){B}"
    rx["weak"] = rf"(?<!\w)(?:{_alt(weak)}){B}" if weak else r"(?!x)x"
    rx["cur_solo"] = rf"(?<!\w)(?:{CUR}){B}"
    rx["unit_solo"] = rf"(?<!\w)(?:{units.alt}){B}"

    # -------------------------------------------------- allergy, negation, deadline, names
    rx["allergy"] = rf"(?<!\w)(?:{n(_ALLERGY[L])}){B}" if L in _ALLERGY else r"(?!x)x"
    rx["contrast"] = rf"(?<!\w)(?:{n(_CONTRAST[L])}){B}" if L in _CONTRAST else r"(?!x)x"
    rx["neg"] = rf"(?<!\w)(?:{n(_NEG[L])}){B}" if L in _NEG else r"(?!x)x"
    rx["neg_excl"] = rf"(?<!\w)(?:{n(_NEG_EXCL[L])}){B}" if L in _NEG_EXCL else r"(?!x)x"
    rx["dl_strong"] = rf"(?<!\w)(?:{n(_DL_STRONG[L])}){B}" if L in _DL_STRONG else r"(?!x)x"
    rx["dl_before"] = rf"(?<!\w)(?:{n(_DL_WEAK_BEFORE[L])})\s*$" if L in _DL_WEAK_BEFORE else r"(?!x)x"
    rx["dl_after"] = rf"^\s*(?:{n(_DL_AFTER[L])}){B}" if L in _DL_AFTER else r"(?!x)x"
    rx["title"] = rf"(?<!\w)(?:{n(_TITLES)})\s+(?P<name>\w[\w'-]+)"
    if L == "ar":
        rx["ar_month_ctx"] = rf"{n(_AR_MONTH_CTX)}\s*$"

    return _Pack(lang=lang, words=words, weak=weak, scales=scales, cur=cur, units=units, dur=dur, months=months,
                 weekdays=weekdays, rx={k: re.compile(v) for k, v in rx.items()},
                 lists={k: [(re.compile(a), b) for a, b in v] for k, v in lists.items()})


# ================================================================ internal facts


@dataclass(slots=True)
class _F:
    kind: str
    value: str
    a: int
    b: int
    key: tuple[str, str]
    alts: tuple[tuple[str, str], ...] = ()
    weak: bool = False
    num: Decimal | None = None
    surface: str = ""
    span: tuple[int, int] = (0, 0)
    heur: bool = False  # heuristic name (title + capitalised token): reported by extract, never compared
    hidden: bool = False  # weak fact that extract() does not report (an article read as "1")


_AR_DUAL_LEX: list[_Lex] = []


def _ar_dual_dur() -> _Lex:
    if not _AR_DUAL_LEX:
        _AR_DUAL_LEX.append(_Lex(_AR_DUAL_DUR, "ar"))
    return _AR_DUAL_LEX[0]


def _numval(s: str, p: _Pack) -> Decimal | None:
    s = s.strip()
    if not s:
        return None
    if s[0].isdigit():
        return parse_number(s, p.lang)
    w = re.sub(r"\s+", " ", s)
    v = p.words.get(w)
    if v is None:
        v = p.weak.get(w)
    if v is None and p.lang == "tr":  # suffixed word ("dokuzda")
        for k, kv in p.words.items():
            if w.startswith(k) and len(w) - len(k) <= 3:
                v = kv
                break
    return Decimal(v) if v is not None else None


def _mk(kind: str, value: str, a: int, b: int, key: str | None = None, alts: tuple[tuple[str, str], ...] = (),
        weak: bool = False, num: Decimal | None = None) -> _F:
    return _F(kind=kind, value=value, a=a, b=b, key=(key or kind, value), alts=alts, weak=weak, num=num)


def _date(kind: str, y: int | None, mo: int | None, d: int | None, a: int, b: int) -> _F | None:
    if mo is not None and not 1 <= mo <= 12:
        return None
    if d is not None and not 1 <= d <= 31:
        return None
    if y is not None and mo and d:
        f = _mk(kind, f"--{mo:02d}-{d:02d}", a, b, key="date", alts=(("date", f"--{mo:02d}-{d:02d}"),))
        f.key = ("date", f"{y:04d}-{mo:02d}-{d:02d}")
        return f
    if mo and d:
        v = f"--{mo:02d}-{d:02d}"
    elif mo:
        v = f"--{mo:02d}"
    elif d:
        v = f"---{d:02d}"
    else:
        return None
    return _mk(kind, v, a, b, key="date")


def _clock(h: int, m: int, mer: str | None) -> str | None:
    if mer:
        mer = mer.replace(".", "").replace(" ", "")
        if mer in ("pm", "nachmittags", "abends", "akşam", "öğledensonra", "вечера", "дня", "مساء", "مساءا",
                   "م", "عصرا", "بعدالظهر", "فيالمساء", "intheevening", "intheafternoon"):
            if h < 12:
                h += 12
        elif mer in ("am", "morgens", "früh", "vormittags", "sabah", "утра", "صباحا", "ص", "فجرا", "فيالصباح",
                     "inthemorning"):
            if h == 12:
                h = 0
        elif mer in ("nachts", "gece", "ночи", "ليلا", "atnight"):
            if 6 <= h < 12:
                h += 12
        elif mer in ("mittags", "öğlen", "ظهرا") and h < 6:
            h += 12
    if not (0 <= h <= 24 and 0 <= m < 60):
        return None
    return f"{h % 24:02d}:{m:02d}"


def _run(t: _Text, rx: re.Pattern[str], fn: Callable[[re.Match[str]], _F | None], out: list[_F]) -> None:
    for m in rx.finditer(t.norm):
        a, b = m.span()
        if a == b or not t.free(a, b):
            continue
        f = fn(m)
        if f is not None:
            t.take(f.a, f.b)
            out.append(f)


def _boundary_before(t: _Text, pos: int, limit: int) -> int:
    lo = max(0, pos - limit)
    for i in range(pos - 1, lo - 1, -1):
        c = t.norm[i]
        if c in "!?;؟\n" or (c == "." and not t.used[i]):
            return i + 1
    return lo


def _boundary_after(t: _Text, pos: int, limit: int, extra: str = "") -> int:
    hi = min(len(t.norm), pos + limit)
    for i in range(pos, hi):
        c = t.norm[i]
        if c in "!?;؟\n" or c in extra or (c == "." and not t.used[i]):
            return i
    return hi


# ================================================================ glossary


_GLOSS_CACHE: dict[tuple[Any, ...], list[tuple[Any, re.Pattern[str]]]] = {}


def _gloss_rx(glossary: Glossary, lang: str) -> list[tuple[Any, re.Pattern[str]]]:
    sig = (lang, tuple((t.de, t.kind, t.keep, tuple(t.forms_for(lang))) for t in glossary.terms))
    hit = _GLOSS_CACHE.get(sig)
    if hit is not None:
        return hit
    out = []
    for term in glossary.terms:
        forms = {_norm_simple(f, lang) for f in [*term.forms_for(lang), term.de] if f and f.strip()}
        if lang == "ar":
            forms |= {f[2:] for f in forms if f.startswith("ال") and len(f) > 4}
            forms = {f for f in forms if not f.startswith("ال")} or forms
            pat = rf"(?<!\w)(?:[وفبلك]?(?:ال|لل)?)(?:{_alt(forms)})\w*"
        else:
            pat = rf"(?<!\w)(?:{_alt(forms)})[\w-]*"
        out.append((term, re.compile(pat)))
    if len(_GLOSS_CACHE) > 64:
        _GLOSS_CACHE.clear()
    _GLOSS_CACHE[sig] = out
    return out


# ================================================================ extraction


def _scan(text: str, lang: str, glossary: Glossary | None) -> tuple[_Text, list[_F]]:
    lang = (lang or "").split("-")[0].lower()
    p = _pack(lang)
    t = _Text(text, lang)
    out: list[_F] = []
    norm = t.norm

    # -------------------------------------------------- dates
    def date_fn(kind: str) -> Callable[[re.Match[str]], _F | None]:
        def fn(m: re.Match[str]) -> _F | None:
            g = m.groupdict()
            if kind == "dm_slash" and ((len(g["d"]) < 2 and len(g["mo"]) < 2) or (g["d"], g["mo"]) == ("24", "7")):
                return None  # "1/2" is a half, "24/7" is around the clock
            yv = None
            if g.get("y"):
                yv = int(g["y"])
                if yv < 100:
                    yv += 2000
            if g.get("mon"):
                mo = p.months.get(m.group("mon"))
            else:
                mo = int(g["mo"]) if g.get("mo") else None
            dv = int(g["d"]) if g.get("d") else None
            return _date("date", yv, mo, dv, *m.span())
        return fn

    for rx, kind in p.lists["dates"]:
        _run(t, rx, date_fn(kind), out)

    def weekday_fn(m: re.Match[str]) -> _F | None:
        s = m.group(0)
        if lang == "tr" and (s.startswith("sali") or s.startswith("cuma") and not s.startswith("cumartesi")):
            # also ordinary words ("salı" tray, "cuma" prayer): need a capital or "günü"
            oa, _ = t.ospan(*m.span())
            if not t.orig[oa:oa + 1].isupper() and not re.match(r"\s+gün", norm[m.end():]):
                return None
        v = p.weekdays.get(re.sub(r"(?:'\w+)$", "", s)) or p.weekdays.get(s)
        if v is None:
            for rx_, val in p.weekdays.items:
                if rx_.match(s):
                    v = val
                    break
        return _mk("date", f"wd:{_WD_NAMES[v]}", *m.span(), key="date") if v else None

    _run(t, p.rx["weekday"], weekday_fn, out)

    def month_fn(m: re.Match[str]) -> _F | None:
        word = m.group("mon")
        oa, _ = t.ospan(*m.span())
        risky = _MONTH_SOLO_RISKY.get(lang, set())
        if lang == "tr" and not (t.orig[oa:oa + 1].isupper() or m.groupdict().get("ay")):
            return None
        if lang == "en" and word in risky and not t.orig[oa:oa + 1].isupper():
            return None
        if lang == "ar" and word in {_norm_simple(w, "ar") for w in risky} and \
                not p.rx["ar_month_ctx"].search(norm[max(0, m.start() - 12):m.start()]):
            return None
        mo = p.months.get(word)
        return _date("date", None, mo, None, *m.span()) if mo else None

    _run(t, p.rx["month_solo"], month_fn, out)

    # -------------------------------------------------- times
    def time_fn(kind: str) -> Callable[[re.Match[str]], _F | None]:
        def fn(m: re.Match[str]) -> _F | None:
            g = m.groupdict()
            if kind == "t_ru" and not (g.get("hw") or g.get("m") or g.get("mer")):
                return None
            hv = _numval(g["h"], p)
            if hv is None:
                return None
            mins = int(g["m"]) if g.get("m") else 0
            frac = (g.get("frac") or "").replace(" ", "")
            if frac:
                mins = 30 if "نصف" in frac else (15 if frac.startswith("و") else -15)
            h = int(hv)
            if mins < 0:
                h, mins = h - 1, 45
            v = _clock(h, mins, g.get("mer") or g.get("mer0"))
            return _mk("time", v, *m.span()) if v else None
        return fn

    for rx, kind in p.lists["times"]:
        _run(t, rx, time_fn(kind), out)

    # -------------------------------------------------- frequency
    def per_of(s: str) -> str:
        if any(k in s for k in ("woche", "week", "hafta", "недел", "тижд", "تиж", "اسبوع", "أسبوع")):
            return "week"
        if any(k in s for k in ("monat", "month", "ayda", "месяц", "місяц", "شهر")):
            return "month"
        return "day"

    words_freq = {"ein": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8,
                  "neun": 9, "zehn": 10, "once": 1, "twice": 2, "thrice": 3, "однажды": 1, "дважды": 2, "трижды": 3}

    def freq_fn(kind: str) -> Callable[[re.Match[str]], _F | None]:
        def fn(m: re.Match[str]) -> _F | None:
            g = m.groupdict()
            if kind == "q":
                if g.get("q1"):
                    return _mk("frequency", "q1h", *m.span())
                if g.get("q2"):
                    return _mk("frequency", "q2d", *m.span())
                nv = _numval(g["n"], p)
                if nv is None:
                    return None
                unit = "d" if g["q"][0] in "dtгيأ" or g["q"].startswith(("gün", "дн", "ايام", "يوم")) else "h"
                return _mk("frequency", f"q{_fmt(nv)}{unit}", *m.span())
            if kind == "one":
                return _mk("frequency", f"1/{per_of(g['per1'])}", *m.span())
            if kind in ("n1", "n2"):
                return _mk("frequency", f"{kind[1]}/{per_of(g['per'])}", *m.span())
            nv = Decimal(words_freq[g["w"]]) if g.get("w") else _numval(g["n"], p)
            if nv is None:
                return None
            return _mk("frequency", f"{_fmt(nv)}/{per_of(g['per'])}", *m.span())
        return fn

    for rx, kind in p.lists["freq"]:
        _run(t, rx, freq_fn(kind), out)

    # -------------------------------------------------- percent
    def pct_fn(m: re.Match[str]) -> _F | None:
        nv = _numval(m.group("n"), p)
        return _mk("percent", f"{_fmt(nv)}%", *m.span(), num=nv) if nv is not None else None

    for rx, _k in p.lists["pct"]:
        _run(t, rx, pct_fn, out)

    # -------------------------------------------------- amounts
    def amt_fn(kind: str) -> Callable[[re.Match[str]], _F | None]:
        def fn(m: re.Match[str]) -> _F | None:
            g = m.groupdict()
            if kind == "amt2":
                nv = Decimal(next(v for k, v in _AR_DUAL_SCALES.items() if _norm_simple(k, "ar") == g["dual"]))
            elif kind == "amt1":
                nv = Decimal(p.scales.get(g["sc1"]) or 0)
            else:
                nv = _numval(g["n"], p)
                if nv is None:
                    return None
                if g.get("sc"):
                    nv *= Decimal(p.scales.get(g["sc"]) or 1)
            if not nv:
                return None
            num = _fmt(nv)
            code = p.cur.get(g["cur"]) if g.get("cur") else None
            if code:
                # the currency is also a (weak) fact of its own: "Euro" -> "dollars" shows as its own row via
                # the amount; a currency named on one side only ("1,05 Millionen" -> "1.05 million euros")
                # never alarms
                ca, cb = m.span("cur")
                out.append(_F(kind="currency", value=code, a=ca, b=cb, key=("currency", code), weak=True))
                return _mk("amount", f"{num} {code}", *m.span(), key="amt", alts=(("num", num),), num=nv)
            return _mk("amount", num, *m.span(), key="num", num=nv)
        return fn

    for rx, kind in p.lists["amt"]:
        _run(t, rx, amt_fn(kind), out)

    # -------------------------------------------------- doses
    def dose_fn(m: re.Match[str]) -> _F | None:
        nv = _numval(m.group("n"), p)
        u = p.units.get(m.group("u"))
        if nv is None or u is None:
            return None
        base, factor = u
        if lang in ("ru", "uk") and m.group("u") in ("г", "л") and 1900 <= nv <= 2100:
            return None  # "2026 г." is a year
        f = _mk("dose", f"{_fmt(nv)} {_UNIT_SYMBOL[(base, factor)]}", *m.span(), num=nv)
        f.key = ("dose", f"{_fmt(nv * factor)} {base}")  # 0,5 g == 500 mg
        return f

    _run(t, p.rx["dose"], dose_fn, out)

    def count_fn(m: re.Match[str]) -> _F | None:
        nv = _numval(m.group("n"), p)
        if nv is None:
            return None
        v = _fmt(nv)
        return _mk("number", v, *m.span(), key="cnt", alts=(("num", v),), num=nv)  # == a bare "10000", != an amount

    _run(t, p.rx["count"], count_fn, out)

    # -------------------------------------------------- durations
    def dur_fn(m: re.Match[str]) -> _F | None:
        nv = _numval(m.group("n"), p)
        u = p.dur.get(m.group("u"))
        if nv is None or u is None or nv == 1 and m.group("n")[0].isalpha():
            return None
        v = f"PT{_fmt(nv)}{u[0]}" if u in ("H", "MIN") else f"P{_fmt(nv)}{u}"
        return _mk("duration", v, *m.span(), num=nv)

    _run(t, p.rx["dur"], dur_fn, out)
    if "dur2" in p.rx:
        def dur2_fn(m: re.Match[str]) -> _F | None:
            u = _ar_dual_dur().get(m.group("u"))
            if u is None:
                return None
            v = f"PT2{u[0]}" if u in ("H", "MIN") else f"P2{u}"
            return _mk("duration", v, *m.span(), num=Decimal(2))

        _run(t, p.rx["dur2"], dur2_fn, out)

    # -------------------------------------------------- bare numbers (strong digits / words, weak "one")
    def num_fn(m: re.Match[str]) -> _F | None:
        nv = _numval(m.group(0), p)
        return _mk("number", _fmt(nv), *m.span(), key="num", num=nv) if nv is not None else None

    _run(t, p.rx["dig"], num_fn, out)
    _run(t, p.rx["numw"], num_fn, out)

    def weak_fn(m: re.Match[str]) -> _F | None:
        v = p.weak.get(m.group(0))
        if not v:
            return None
        f = _mk("number", str(v), *m.span(), key="num", weak=True, num=Decimal(v))
        f.hidden = True
        return f

    _run(t, p.rx["weak"], weak_fn, out)

    # -------------------------------------------------- glossary: medications, names, allergen candidates
    gl_hits: list[tuple[Any, int, int]] = []
    if glossary is not None and glossary.terms:
        for term, rx in _gloss_rx(glossary, lang):
            for m in rx.finditer(norm):
                gl_hits.append((term, m.start(), m.end()))
                if term.kind in ("medication", "name"):
                    out.append(_mk(term.kind, term.de, m.start(), m.end()))

    # names after a title ("Herr Schmidt", "Dr. Weber", "السيد Müller"): Latin script, capitalised
    for m in p.rx["title"].finditer(norm):
        a, b = m.span("name")
        oa, ob = t.ospan(a, b)
        word = text[oa:ob]
        if word[:1].isupper() and word[:1].isascii() or word[:1] in "ÄÖÜÇĞİŞ":
            out.append(_F(kind="name", value=word, a=a, b=b, key=("name", word), heur=True))

    # standalone currency / unit words (not part of an amount / dose)
    def cur_fn(m: re.Match[str]) -> _F | None:
        code = p.cur.get(m.group(0))
        return _mk("currency", code, *m.span()) if code else None

    _run(t, p.rx["cur_solo"], cur_fn, out)

    def unit_fn(m: re.Match[str]) -> _F | None:
        if len(m.group(0)) < _SOLO_UNIT_MIN_LEN or lang == "ar":
            return None
        u = p.units.get(m.group(0))
        return _mk("unit", _UNIT_SYMBOL[u], *m.span()) if u else None

    _run(t, p.rx["unit_solo"], unit_fn, out)

    # -------------------------------------------------- allergy
    # The allergen slot of each keyword, in this order: (1) written right in front of it or inside the
    # same word ("Penicillin-Allergie", "penisiline alerjim", "X ve Y'ye alerjim"); (2) after it in the
    # same clause ("allergisch gegen X", "аллергия на X", "حساسية من X"); (3) earlier in the clause
    # ("Gegen Penicillin bin ich allergisch").
    seen_allergens: set[str] = set()
    present: tuple[int, int] | None = None
    cands = sorted((h for h in gl_hits if h[0].kind in ("medication", "term")), key=lambda h: h[1])
    for m in p.rx["allergy"].finditer(norm):
        ka, kb = m.span()
        picked = [h for h in cands if h[1] <= ka < h[2] or ka <= h[1] < kb]
        edge = min([ka, *(h[1] for h in picked)])
        for h in reversed([h for h in cands if h[2] <= edge]):
            if not _JOIN.fullmatch(norm[h[2]:edge]):
                break
            picked.append(h)
            edge = h[1]
        if not picked:
            stop = _boundary_after(t, kb, 60, ",\u060c")
            c = p.rx["contrast"].search(norm, kb, stop)
            picked = [h for h in cands if kb <= h[1] < (c.start() if c else stop)]
        if not picked:
            start = max(_boundary_before(t, ka, 30), *(i + 1 for i in range(max(0, ka - 30), ka) if norm[i] in ",\u060c"),
                        0)
            picked = [h for h in cands if start <= h[1] and h[2] <= ka]
        for term, a, b in sorted(picked, key=lambda h: h[1]):
            if term.de not in seen_allergens:
                seen_allergens.add(term.de)
                out.append(_mk("allergy", term.de, a, b))
        if not picked and present is None:
            present = (ka, kb)
    if present is not None and not seen_allergens:
        out.append(_mk("allergy", "present", *present))

    # -------------------------------------------------- negation (presence)
    excl = [m.span() for m in p.rx["neg_excl"].finditer(norm)]
    cue = None
    for m in p.rx["neg"].finditer(norm):
        if not any(a <= m.start() < b for a, b in excl):
            cue = m.span()
            break
    if cue is None and lang == "ar":
        mo = re.search(r"(?<!\w)[وف]?ألا(?!\w)", text)  # "that ... not" (not إلا "except"): read the original
        if mo:
            out.append(_F(kind="negation", value="not", a=0, b=0, key=("negation", "not"),
                          surface=mo.group(0), span=mo.span()))
    if cue is not None:
        out.append(_mk("negation", "not", *cue))

    # -------------------------------------------------- deadlines: dates inside a deadline phrase
    for f in out:
        if f.kind != "date":
            continue
        start = _boundary_before(t, f.a, 70)
        before = norm[start:f.a]
        end = _boundary_after(t, f.b, 40)
        after = norm[f.b:end]
        if (p.rx["dl_strong"].search(before) or p.rx["dl_before"].search(before)
                or p.rx["dl_after"].search(after) or p.rx["dl_strong"].search(after)):
            f.kind = "deadline"
    # "bis Freitag, den 6. November" / "до пятницы, 6 ноября": one deadline said twice
    dated = sorted((f for f in out if f.kind in ("date", "deadline")), key=lambda f: f.a)
    for x, y_ in itertools.pairwise(dated):
        if "deadline" in (x.kind, y_.kind) and _DATE_GLUE.fullmatch(norm[x.b:y_.a]):
            x.kind = y_.kind = "deadline"

    for f in out:
        if not f.span or f.span == (0, 0):
            f.span = t.ospan(f.a, f.b)
        if not f.surface:
            f.surface = t.surface(f.a, f.b)
    return t, out


def _select(facts: list[_F], kinds: frozenset[str]) -> list[_F]:
    out: list[_F] = []
    seen: set[tuple[str, str]] = set()
    for f in sorted(facts, key=lambda x: (x.span[0], x.span[1])):
        if f.kind not in kinds:
            if f.kind == "deadline" and "date" in kinds:
                f.kind = "date"
            elif f.kind in _NUMERIC_KINDS and "number" in kinds and f.num is not None:
                v = _fmt(f.num)
                f.kind, f.value, f.key, f.alts = "number", v, ("num", v), ()
            else:
                continue
        if f.kind in SET_KINDS:
            if (f.kind, f.value) in seen:
                continue
            seen.add((f.kind, f.value))
        out.append(f)
    return out


def _kinds(kinds: Iterable[str] | None) -> frozenset[str]:
    return ALL_KINDS if kinds is None else frozenset(kinds) & ALL_KINDS


def extract(text: str, lang: str, *, kinds: Iterable[str] | None = None,
            glossary: Glossary | None = None) -> list[Fact]:
    """All facts in ``text`` in text order, restricted to ``kinds`` (articles read as "1" are left out)."""
    if not text:
        return []
    _t, facts = _scan(text, lang, glossary)
    return [Fact(kind=f.kind, value=f.value, surface=f.surface, span=f.span)
            for f in _select(facts, _kinds(kinds)) if not f.hidden]


# ================================================================ comparison


def _fc(kind: str, s: _F | None, t: _F | None, match: bool) -> FactCheck:
    return FactCheck(kind=kind, src=s.surface if s else None, tgt=t.surface if t else None, match=match)


def _compare(sf: list[_F], tf: list[_F]) -> list[tuple[tuple[int, int], FactCheck]]:
    S = [f for f in sf if not f.weak]
    T = [f for f in tf if not f.weak]
    WS = [f for f in sf if f.weak]
    WT = [f for f in tf if f.weak]
    pair: dict[int, _F | None] = {}
    t_used: set[int] = set()

    def take(s: _F, pool: list[_F], used: set[int], ok: Callable[[_F, _F], bool]) -> _F | None:
        for i, t in enumerate(pool):
            if i not in used and ok(s, t):
                used.add(i)
                return t
        return None

    exact = lambda s, t: s.key == t.key
    lenient = lambda s, t: s.key in t.alts or t.key in s.alts
    for rule in (exact, lenient):
        for i, s in enumerate(S):
            if i not in pair:
                t = take(s, T, t_used, rule)
                if t is not None:
                    pair[i] = t
    # weak facts only satisfy a strong fact of the other side ("قطعتين" satisfies "2 Stück")
    same = lambda s, t: exact(s, t) or lenient(s, t)
    ws_used: set[int] = set()
    wt_used: set[int] = set()
    for i, s in enumerate(S):
        if i not in pair:
            t = take(s, WT, wt_used, same)
            if t is not None:
                pair[i] = t
    t_soft: dict[int, _F] = {}
    for j, t in enumerate(T):
        if j not in t_used:
            s = take(t, WS, ws_used, same)
            if s is not None:
                t_soft[j] = s
                t_used.add(j)

    rows: list[tuple[tuple[int, int], FactCheck]] = []
    fam = lambda k: _FAMILY.get(k, k)
    for i, s in enumerate(S):
        if i in pair:
            rows.append(((0, s.a), _fc(s.kind, s, pair[i], True)))
            continue
        t = take(s, T, t_used, lambda a, b: fam(a.kind) == fam(b.kind))  # show the exact wrong pair
        rows.append(((0, s.a), _fc(s.kind, s, t, False)))
    for j, t in enumerate(T):
        if j in t_soft:
            s = t_soft[j]
            rows.append(((0, s.a), _fc(t.kind, s, t, True)))
        elif j not in t_used:
            rows.append(((1, t.a), _fc(t.kind, None, t, False)))
    return rows


_NAME_TOKEN = re.compile(r"(?<![\w'-])[A-ZÀ-ÖØ-ÞĞİŞ][A-Za-zÀ-ÖØ-öø-ÿĞğİıŞşÇç'-]+")
_NAME_STOP = frozenset(_norm_simple(w, "de") for w in (
    "EUR", "USD", "GBP", "CHF", "TL", "IE", "IU", "OK", "Euro", "Euros", "Dollar", "Dollars", "Uhr", "Nein", "Ja",
    "Yes", "No", "Sie", "Ich", "Wir", "Mr", "Mrs", "Ms", "Dr", "Herr", "Frau", "Bitte", "Please", "Danke",
))


def _shared_names(src: str, tgt: str, sf: list[_F]) -> list[tuple[tuple[int, int], FactCheck]]:
    """Capitalised Latin tokens of the source that appear verbatim in the translation (always a match)."""
    taken = [f.span for f in sf if not f.heur]
    rows = []
    seen = set()
    for m in _NAME_TOKEN.finditer(src):
        tok = m.group(0).rstrip("'-")
        a, b = m.start(), m.start() + len(tok)
        if len(tok) < 2 or tok in seen or tok.casefold() in _NAME_STOP or any(x < b and a < y for x, y in taken):
            continue
        if re.search(rf"(?<![\w]){re.escape(tok)}(?!\w)", tgt):
            seen.add(tok)
            rows.append(((0, a), FactCheck(kind="name", src=tok, tgt=tok, match=True)))
    return rows


def check(src: str, src_lang: str, tgt: str, tgt_lang: str, *, kinds: Iterable[str] | None = None,
          confirm_on: Iterable[str] = (), glossary: Glossary | None = None) -> GuardResult:
    t0 = time.perf_counter()
    ks = _kinds(kinds)
    _ts, sf_all = _scan(src or "", src_lang, glossary)
    _tt, tf_all = _scan(tgt or "", tgt_lang, glossary)
    sf = [f for f in _select(sf_all, ks) if not f.heur]
    tf = [f for f in _select(tf_all, ks) if not f.heur]
    rows = _compare(sf, tf)
    if "name" in ks and src and tgt:
        rows += _shared_names(src, tgt, sf_all)
    rows.sort(key=lambda r: r[0])
    facts = [r for _, r in rows]
    conf = set(confirm_on) & ks
    confirm = [r for r in facts if r.kind in conf and r.src is not None]
    return GuardResult(facts=facts, red=any(not r.match for r in facts), confirm=confirm,
                       ms=(time.perf_counter() - t0) * 1000)
