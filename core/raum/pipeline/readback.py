"""Facts read back the way a human interpreter would (pure helpers; ``RoomPipeline`` raises the flags).

The rooms' ``guard.confirm_on`` facts (Sprechzimmer: dose, allergy; Kanzlei: deadline; Verhandlungsraum: amount)
and every fact the fact guard finds wrong:

- **green** (fact guard and checker green, ASR sure): nobody is asked. The professional's screen gets a quiet
  "noted" chip per fact ("✓ Allergie: Penicillin · ✓ Metformin 500 mg, zweimal täglich"), the summary files
  the fact as noted.
- **uncertain** (a guard mismatch, the checker amber or red, a low ASR confidence on the utterance): one short
  read-back in the listener's language, spoken through the room's voice and shown with small yes/no buttons
  ("Nur zur Sicherheit: 400 mg?"). The listener's next utterance answers it when it is a short yes or no
  (``yes_no``); it is translated like any other. Unanswered, the fact stays "unconfirmed". Never asked twice.

``groups`` puts a dose together with its medication and frequency (one chip, one question); ``chip`` and
``question`` word a group in one language: the listener's words where the translation had them right, the
speaker's value otherwise (a mismatch reads back what was SAID), drug names in the glossary's form.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field

from raum.events import FactCheck
from raum.glossary import Glossary
from raum.pipeline import texts as T

# ---------------------------------------------------------------- wording

JUST_TO_BE_SURE = T.JUST_TO_BE_SURE  # "Nur zur Sicherheit: {facts}?"

_ASK_PHRASE = {  # a kind inside the question (chips use texts.fact_text with its label)
    "allergy": {"de": "Allergie gegen {v}", "en": "allergy to {v}", "tr": "{v} alerjisi", "ru": "аллергия на {v}",
                "ar": "حساسية من {v}"},
    "deadline": {"de": "Frist bis {v}", "en": "deadline {v}", "tr": "son tarih {v}", "ru": "срок до {v}",
                 "ar": "المهلة حتى {v}"},
}

_AND = {"de": " und ", "en": " and ", "tr": " ve ", "ru": " и ", "ar": " و"}
_COMMA = {"ar": "، "}

_MONTHS = {
    "de": ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
           "November", "Dezember"),
    "en": ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"),
    "tr": ("Ocak", "Şubat", "Mart", "Nisan", "Mayıs", "Haziran", "Temmuz", "Ağustos", "Eylül", "Ekim", "Kasım",
           "Aralık"),
    "ru": ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
           "ноября", "декабря"),
    "ar": ("يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر",
           "ديسمبر"),
}
_WEEKDAYS = {
    "de": {"mon": "Montag", "tue": "Dienstag", "wed": "Mittwoch", "thu": "Donnerstag", "fri": "Freitag",
           "sat": "Samstag", "sun": "Sonntag"},
    "en": {"mon": "Monday", "tue": "Tuesday", "wed": "Wednesday", "thu": "Thursday", "fri": "Friday",
           "sat": "Saturday", "sun": "Sunday"},
    "tr": {"mon": "Pazartesi", "tue": "Salı", "wed": "Çarşamba", "thu": "Perşembe", "fri": "Cuma",
           "sat": "Cumartesi", "sun": "Pazar"},
    "ru": {"mon": "понедельник", "tue": "вторник", "wed": "среда", "thu": "четверг", "fri": "пятница",
           "sat": "суббота", "sun": "воскресенье"},
    "ar": {"mon": "الاثنين", "tue": "الثلاثاء", "wed": "الأربعاء", "thu": "الخميس", "fri": "الجمعة",
           "sat": "السبت", "sun": "الأحد"},
}

# spoken only: the voice says "Milligramm", the screen keeps "mg"
_UNIT_WORDS = {
    "mg": {"de": "Milligramm", "en": "milligrams", "tr": "miligram", "ru": "миллиграмм", "ar": "ملليغرام"},
    "ml": {"de": "Milliliter", "en": "millilitres", "tr": "mililitre", "ru": "миллилитров", "ar": "ملليلتر"},
    "µg": {"de": "Mikrogramm", "en": "micrograms", "tr": "mikrogram", "ru": "микрограммов", "ar": "ميكروغرام"},
    "g": {"de": "Gramm", "en": "grams", "tr": "gram", "ru": "граммов", "ar": "غرام"},
    "ie": {"de": "internationale Einheiten", "en": "units", "tr": "ünite", "ru": "единиц", "ar": "وحدة"},
}
_UNIT_ALIASES = {"mg": "mg", "мг": "mg", "ملغ": "mg", "ملغم": "mg", "ml": "ml", "мл": "ml", "مل": "ml",
                 "µg": "µg", "μg": "µg", "mcg": "µg", "мкг": "µg", "g": "g", "ie": "ie", "iu": "ie", "ед": "ie"}
_UNIT_RX = re.compile(r"(?<=\d)\s*(" + "|".join(sorted(map(re.escape, _UNIT_ALIASES), key=len, reverse=True))
                      + r")(?![\w])", re.IGNORECASE)
_TR_SUFFIX = re.compile(r"(?<=\S)['’](?:[a-zçğıöşü]{1,4})$", re.IGNORECASE)  # "500 mg'ı" -> "500 mg"


def _sep(lang: str) -> str:
    return _COMMA.get(lang, ", ")


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def _k(s: str | None) -> str:
    return (s or "").strip().casefold()


# ---------------------------------------------------------------- facts -> groups -> words


def worth_noting(f: FactCheck) -> bool:
    """A fact with a concrete value said by the speaker (allergy "present" names nothing)."""
    return bool((f.src or "").strip()) and _k(f.src) != "present"


def groups(chosen: Iterable[FactCheck], context: Iterable[FactCheck] = ()) -> list[list[FactCheck]]:
    """The facts as chips / question parts: each dose with the utterance's medication(s) and frequency (one
    group), each other fact on its own. An allergen named by an allergy fact is not repeated as a drug."""
    chosen = [f for f in chosen if worth_noting(f)]
    context = list(context)
    every = [*chosen, *context]
    allergens = {_k(f.src) for f in every if f.kind == "allergy"}
    out: list[list[FactCheck]] = []
    doses = [f for f in chosen if f.kind == "dose"]
    if doses:
        meds = _uniq(f for f in every if f.kind == "medication" and _k(f.src) not in allergens)
        freqs = _uniq(f for f in every if f.kind == "frequency")
        out.append([*meds, *doses, *freqs])
    taken = {id(f) for g in out for f in g}
    for f in chosen:
        if id(f) in taken or (doses and f.kind in ("medication", "frequency")):
            continue
        if f.kind == "medication" and _k(f.src) in allergens:
            continue
        out.append([f])
    return out


def _uniq(facts: Iterable[FactCheck]) -> list[FactCheck]:
    seen: set[tuple[str, str]] = set()
    out = []
    for f in facts:
        key = (f.kind, _k(f.src or f.tgt))
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


@dataclass
class Wording:
    """How to say the facts of one utterance in any language: the source and target languages, the
    glossary (drug names), and the normalised values of the source's facts (``guard.extract``)."""

    src_lang: str
    tgt_lang: str
    glossary: Glossary | None = None
    values: dict[tuple[str, str], str] = field(default_factory=dict)  # (kind, surface casefold) -> value

    def value(self, f: FactCheck, lang: str) -> str:
        if lang == self.tgt_lang and f.match and f.tgt:
            v = f.tgt  # what the listener read
        elif lang == self.src_lang or not f.src:
            v = f.src or f.tgt or ""
        else:  # a third language, or a mismatch read back to the listener: the SAID value, in their words
            v = self._formatted(f, lang) or f.src
        if f.kind in ("medication", "allergy"):
            v = self._term(v, lang) or v
        return _TR_SUFFIX.sub("", v.strip()) if lang == "tr" else v.strip()

    def _formatted(self, f: FactCheck, lang: str) -> str | None:
        norm = self.values.get((f.kind, _k(f.src)))
        if not norm:
            return None
        if f.kind in ("date", "deadline"):
            return format_date(norm, lang)
        if f.kind == "time":
            return format_time(norm, lang)
        if f.kind in ("frequency", "amount"):
            return T.fact_text("_", norm, lang)
        if f.kind == "dose":
            return norm
        return None

    def _term(self, surface: str, lang: str) -> str | None:
        if self.glossary is None:
            return None
        key = _k(surface)
        best: tuple[int, str] | None = None
        for t in self.glossary.terms:
            if t.kind not in ("medication", "allergy", "term"):
                continue
            for x in {t.de, *(x for fs in t.forms.values() for x in fs)}:
                fx = _k(x)
                if fx == key:
                    return t.preferred(lang) or t.de
                # an inflected form: Turkish "Penisiline", Russian "пенициллину" -> the term
                if len(fx) >= 5 and key.startswith(fx) and len(key) - len(fx) <= 3 and (best is None or len(fx) > best[0]):
                    best = (len(fx), t.preferred(lang) or t.de)
        return best[1] if best else None

    def group_text(self, group: list[FactCheck], lang: str, *, ask: bool) -> str:
        """One chip ("Metformin 500 mg, zweimal täglich", "Allergie: Penicillin") or one part of a question
        ("Allergie gegen Penicillin", "12. November": no label inside a question)."""
        if len(group) == 1 and group[0].kind != "dose":
            f = group[0]
            v = self.value(f, lang)
            if not ask:
                return T.fact_text(f.kind, v, lang)
            phrase = _ASK_PHRASE.get(f.kind)
            return T.pick(phrase, lang).format(v=v) if phrase else v
        head = " ".join(self.value(f, lang) for f in group if f.kind in ("medication", "dose"))
        tail = [self.value(f, lang) for f in group if f.kind == "frequency"]
        return _sep(lang).join([x for x in (head, *tail) if x])

    def chips(self, gs: list[list[FactCheck]], langs: Iterable[str]) -> list[dict[str, object]]:
        """``Flag.facts``: one entry per group, its chip text per language."""
        out: list[dict[str, object]] = []
        for g in gs:
            main = next((f for f in g if f.kind == "dose"), g[0])
            out.append({"kind": main.kind, "src": main.src, "tgt": main.tgt, "match": all(f.match for f in g),
                        "texts": {lang: _cap(self.group_text(g, lang, ask=False)) for lang in langs}})
        return out

    def question(self, gs: list[list[FactCheck]], lang: str) -> str:
        parts = [self.group_text(g, lang, ask=True) for g in gs]
        joined = T.pick(_AND, lang).join(parts) if len(parts) <= 2 else _sep(lang).join(parts)
        return T.pick(JUST_TO_BE_SURE, lang).format(facts=joined)


def format_date(value: str, lang: str) -> str | None:
    """A normalised date ("--11-12", "2026-11-12", "--03", "---12", "wd:thu") in ``lang``."""
    if value.startswith("wd:"):
        return _WEEKDAYS.get(lang, _WEEKDAYS["en"]).get(value[3:])
    m = re.fullmatch(r"(\d{4})?--?(\d{2})?-?(\d{2})?", value)
    if not m:
        return None
    y, mo, d = m.group(1), m.group(2), m.group(3)
    if value.startswith("---"):  # day only
        mo, d = None, value[3:]
    months = _MONTHS.get(lang, _MONTHS["en"])
    name = months[int(mo) - 1] if mo and 1 <= int(mo) <= 12 else None
    day = str(int(d)) if d else None
    if day and name:
        s = f"{day}. {name}" if lang == "de" else f"{day} {name}"
    else:
        s = name or (f"{day}." if lang == "de" and day else day) or ""
    return f"{s} {y}".strip() if y and s else (s or None)


def format_time(value: str, lang: str) -> str | None:
    m = re.fullmatch(r"(\d{2}):(\d{2})", value)
    if not m:
        return None
    h, mi = int(m.group(1)), m.group(2)
    return f"{h}:{mi} Uhr" if lang == "de" else f"{h}:{mi}"


def spoken(text: str, lang: str) -> str:
    """The read-back as the voice should say it: units in words ("500 mg" -> "500 Milligramm")."""

    def unit(m: re.Match[str]) -> str:
        words = _UNIT_WORDS[_UNIT_ALIASES[m.group(1).casefold()]]
        return " " + T.pick(words, lang)

    return _UNIT_RX.sub(unit, text)


# ---------------------------------------------------------------- the spoken answer

_YES: dict[str, frozenset[str]] = {
    "de": frozenset({"ja", "jaja", "jawohl", "jap", "jep", "jo", "genau", "richtig", "stimmt", "korrekt", "exakt"}),
    "en": frozenset({"yes", "yeah", "yep", "yup", "yea", "correct", "right", "exactly", "sure", "aye", "indeed"}),
    "tr": frozenset({"evet", "doğru", "aynen", "tabii", "tabi", "elbette", "kesinlikle"}),
    "ru": frozenset({"да", "ага", "угу", "верно", "правильно", "точно", "конечно", "именно"}),
    "ar": frozenset({"نعم", "اجل", "ايوه", "ايوا", "ايه", "اي", "صحيح", "صح", "بالضبط", "طبعا", "تمام"}),
}
_NO: dict[str, frozenset[str]] = {
    "de": frozenset({"nein", "nee", "ne", "nö", "falsch", "nicht", "keineswegs"}),
    "en": frozenset({"no", "nope", "nah", "wrong", "incorrect", "not"}),
    "tr": frozenset({"hayır", "hayir", "yok", "yanlış", "yanlis", "değil"}),
    "ru": frozenset({"нет", "не", "неверно", "неправильно", "нету"}),
    "ar": frozenset({"لا", "كلا", "خطا", "غلط", "مش", "ليس"}),
}
_NEGATORS = frozenset({"nicht", "not", "değil", "degil", "не", "مش", "ليس", "غير", "isn't", "n't"})
_FILLERS = frozenset({"äh", "ähm", "öhm", "hm", "hmm", "mhm", "also", "uh", "um", "uhm", "er", "erm", "oh", "eh",
                      "şey", "ee", "ну", "э", "эм", "اه", "امم", "يعني"})
_UNSURE = (  # "don't know" starts with a no-word but answers nothing
    "nicht sicher", "weiß nicht", "weiss nicht", "keine ahnung", "not sure", "don't know", "dont know",
    "no idea", "emin değilim", "bilmiyorum", "не знаю", "не уверен", "не уверена", "لا اعرف", "لا ادري",
    "مش عارف", "مش عارفه", "لست متاكد", "لست متاكده",
)
_YES_PHRASES = ("das stimmt", "stimmt so", "that's right", "that is right", "that's correct", "that is correct",
                "все верно", "всё верно", "это так", "так точно", "aynen öyle", "öyle", "هذا صحيح")
_NO_PHRASES = ("das stimmt nicht", "stimmt nicht", "nicht richtig", "that's wrong", "that's not right",
               "that is not right", "not right", "not correct", "doğru değil", "не так", "не верно",
               "не правильно", "غير صحيح", "مش صحيح", "ليس صحيحا", "هذا خطا")
MAX_ANSWER_WORDS = 4

_AR_FOLD = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ة": "ه", "ؤ": "و", "ئ": "ي"})
_AR_MARKS = re.compile("[ً-ٰٟـ]")
_PUNCT = re.compile(r"[^\w\s'’]+")


def _normalise(text: str) -> list[str]:
    t = unicodedata.normalize("NFC", text or "").casefold().replace("’", "'")
    t = _AR_MARKS.sub("", t).translate(_AR_FOLD)
    t = _PUNCT.sub(" ", t.replace("-", " "))
    return t.split()


def _starts(tokens: list[str], phrases: Iterable[str]) -> bool:
    joined = " ".join(tokens)
    return any(joined == p or joined.startswith(p + " ") for p in (" ".join(_normalise(x)) for x in phrases))


def yes_no(text: str, lang: str | None) -> bool | None:
    """A short spoken answer to a read-back: True (yes), False (no), None (not an answer: longer than
    ``MAX_ANSWER_WORDS`` words, not starting with a yes/no word, "don't know", or both yes and no).
    ``lang`` picks the word list (plus English, which many people use); unknown languages: English only."""
    tokens = _normalise(text)
    if not tokens or len(tokens) > MAX_ANSWER_WORDS:
        return None
    while len(tokens) > 1 and tokens[0] in _FILLERS:
        tokens = tokens[1:]
    code = (lang or "").split("-")[0].lower()
    yes = _YES.get(code, frozenset()) | _YES["en"]
    no = _NO.get(code, frozenset()) | _NO["en"]
    if _starts(tokens, _UNSURE):
        return None
    if _starts(tokens, _NO_PHRASES):
        return False
    if _starts(tokens, _YES_PHRASES):
        return None if any(t in no for t in tokens[2:]) else True
    first, rest = tokens[0], tokens[1:]
    if first in yes:
        if rest and (rest[0] in _NEGATORS or rest[0].endswith("n't")):
            return False  # "stimmt nicht", "right not"
        return None if any(t in no for t in rest) else True
    if first in no:
        return None if any(t in yes for t in rest) else False
    return None


def utt_number(utt: str | None) -> int:
    """``u-0007`` -> 7 (0 for anything else): which utterance came after which."""
    m = re.search(r"(\d+)$", utt or "")
    return int(m.group(1)) if m else 0


__all__ = ["JUST_TO_BE_SURE", "MAX_ANSWER_WORDS", "Wording", "format_date", "format_time", "groups", "spoken",
           "utt_number", "worth_noting", "yes_no"]
