"""Bilingual end-of-conversation summary (C-11, plan §20.4). Must be ready in < 10 s after End.

Two paths:

- **Mock / no second model** (verify engine mock or off, or no engines): deterministic, from the
  session's own records. Transcript finals + the session's own translations (one
  ``translation.final`` per language and utterance) give every item in both languages; facts come
  from the fact guard (``raum.verify.guard.extract``, regex fallback until it lands); keyword rules
  per section.
- **Real**: fill ``prompts/summary-<mode>.md``, ``engines.verify.complete_json``, then translate each
  item into ``lang_b`` with ``engines.translate``. Any failure or timeout falls back to the mock path.

Rule for all: only what was said; anything uncertain is marked; no advice.

Both paths: the checked facts (``raum.pipeline.readback``: flags with ``facts``) go into ``SummaryReady.facts``
with what became of them (noted / unconfirmed / confirmed / rejected), and into the file note.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from raum.config import repo_root
from raum.events import SummaryFact, SummaryItem, SummaryReady, SummarySection
from raum.glossary import Glossary
from raum.modes.schema import ModeConfig
from raum.pipeline.base import Engines, TranslateRequest
from raum.verify import guard
from raum.verify.guard import Fact

log = logging.getLogger("raum.summary")

MODEL_TIMEOUT_S = 8.0  # leaves headroom under the 10 s of C-11 for the fallback
ASR_MIN_CONF = 0.55
MAX_ITEMS = 8

LANG_NAMES_EN = {
    "de": "German", "en": "English", "tr": "Turkish", "ru": "Russian", "ar": "Arabic", "uk": "Ukrainian",
    "fa": "Persian", "pl": "Polish", "ro": "Romanian", "fr": "French", "es": "Spanish", "it": "Italian",
    "ku": "Kurdish", "sq": "Albanian", "sr": "Serbian", "hr": "Croatian", "bg": "Bulgarian", "vi": "Vietnamese",
    "zh": "Chinese", "ti": "Tigrinya", "so": "Somali", "ps": "Pashto", "hi": "Hindi", "pt": "Portuguese",
}
LANG_NAMES_DE = {
    "de": "Deutsch", "en": "Englisch", "tr": "Türkisch", "ru": "Russisch", "ar": "Arabisch", "uk": "Ukrainisch",
    "fa": "Persisch", "pl": "Polnisch", "ro": "Rumänisch", "fr": "Französisch", "es": "Spanisch",
    "it": "Italienisch", "ku": "Kurdisch", "sq": "Albanisch", "sr": "Serbisch", "hr": "Kroatisch",
    "bg": "Bulgarisch", "vi": "Vietnamesisch", "zh": "Chinesisch", "ti": "Tigrinya", "so": "Somali",
    "ps": "Paschtu", "hi": "Hindi", "pt": "Portugiesisch",
}

SECTION_TITLES: dict[str, dict[str, str]] = {
    "medications": {"de": "Medikamente", "en": "Medications", "tr": "İlaçlar", "ru": "Лекарства",
                    "ar": "الأدوية"},
    "allergies": {"de": "Allergien", "en": "Allergies", "tr": "Alerjiler", "ru": "Аллергии", "ar": "الحساسية"},
    "next_appointment": {"de": "Nächster Termin", "en": "Next appointment", "tr": "Sonraki randevu",
                         "ru": "Следующий приём", "ar": "الموعد القادم"},
    "warning_signs": {"de": "Warnzeichen", "en": "Warning signs", "tr": "Uyarı işaretleri",
                      "ru": "Тревожные признаки", "ar": "علامات التحذير"},
    "open_questions": {"de": "Offene Fragen", "en": "Open questions", "tr": "Açık sorular",
                       "ru": "Открытые вопросы", "ar": "أسئلة مفتوحة"},
    "parties": {"de": "Beteiligte", "en": "Parties", "tr": "Taraflar", "ru": "Стороны", "ar": "الأطراف"},
    "facts": {"de": "Sachverhalt", "en": "Facts", "tr": "Olaylar", "ru": "Обстоятельства", "ar": "الوقائع"},
    "dates": {"de": "Termine und Daten", "en": "Dates", "tr": "Tarihler", "ru": "Даты", "ar": "التواريخ"},
    "deadlines": {"de": "Fristen", "en": "Deadlines", "tr": "Süreler", "ru": "Сроки", "ar": "المهل"},
    "documents_requested": {"de": "Angeforderte Unterlagen", "en": "Documents requested",
                            "tr": "İstenen belgeler", "ru": "Запрошенные документы", "ar": "المستندات المطلوبة"},
    "next_steps": {"de": "Nächste Schritte", "en": "Next steps", "tr": "Sonraki adımlar", "ru": "Следующие шаги",
                   "ar": "الخطوات التالية"},
    "agreed": {"de": "Vereinbart", "en": "Agreed", "tr": "Anlaşılanlar", "ru": "Согласовано",
               "ar": "ما تم الاتفاق عليه"},
    "open": {"de": "Offene Punkte", "en": "Open points", "tr": "Açık noktalar", "ru": "Нерешённые вопросы",
             "ar": "نقاط مفتوحة"},
    "offers": {"de": "Angebote", "en": "Offers", "tr": "Teklifler", "ru": "Предложения", "ar": "العروض"},
}

NOTE_NAMES_DE = {"sprechzimmer": "Gesprächsnotiz", "kanzlei": "Aktennotiz", "verhandlungsraum": "Protokoll",
                 "gespraech": "Notiz"}


def section_title(key: str, langs: Iterable[str]) -> dict[str, str]:
    known = SECTION_TITLES.get(key)
    fallback = key.replace("_", " ").capitalize()
    out: dict[str, str] = {}
    for lang in langs:
        if lang in out:
            continue
        out[lang] = (known or {}).get(lang) or (known or {}).get("en") or fallback
    return out


# ---------------------------------------------------------------- records -> utterances


@dataclass
class Utterance:
    utt: str
    speaker: str = ""
    role: str = "?"
    lang: str = ""
    text: str = ""
    ts: int = 0
    conf: float = 1.0
    translations: dict[str, str] = field(default_factory=dict)
    red: bool = False
    rejected: bool = False
    confirmed: bool = False
    fact_status: str | None = None  # its checked facts (readback.py): noted | unconfirmed | confirmed | rejected

    def text_in(self, lang: str) -> str | None:
        if lang == self.lang:
            return self.text
        return self.translations.get(lang)

    def bilingual(self, lang_a: str, lang_b: str) -> dict[str, str]:
        out: dict[str, str] = {}
        for lang in (lang_a, lang_b):
            out[lang] = self.text_in(lang) or self.text
        return out

    @property
    def all_text(self) -> str:
        # padded so keywords written with a leading space match at word starts only
        return " " + " ".join([self.text, *self.translations.values()]).casefold() + " "

    @property
    def is_question(self) -> bool:
        return "?" in self.text or "؟" in self.text

    @property
    def uncertain(self) -> bool:  # a fact read back and never answered stays unsure
        return self.red or self.rejected or self.conf < ASR_MIN_CONF or self.fact_status == "unconfirmed"


def _role_of(speaker: str, data: dict[str, Any]) -> str:
    r = data.get("role")
    if r in ("a", "b", "observer"):
        return r
    head = speaker.split("-", 1)[0]
    return head if head in ("a", "b", "observer") else "?"


def collect(records: list[dict[str, Any]]) -> tuple[list[Utterance], list[dict[str, Any]]]:
    """Group the session's records by utterance. Returns (utterances in order, offer updates). An utterance
    continued in a later one (``utterance.continued``: a sentence cut at a pause, merged) is no statement of
    its own: the later one holds the whole sentence."""
    by_utt: dict[str, Utterance] = {}
    order: list[str] = []
    continued: set[str] = set()
    flag_utt: dict[str, str] = {}
    confirms: list[dict[str, Any]] = []
    offers: list[dict[str, Any]] = []
    segments: dict[tuple[str, str], dict[int, str]] = {}

    def get(utt: str) -> Utterance:
        if utt not in by_utt:
            by_utt[utt] = Utterance(utt=utt)
            order.append(utt)
        return by_utt[utt]

    for i, r in enumerate(records):
        if not isinstance(r, dict):
            continue
        t = r.get("type")
        d = r.get("data") or {}
        utt = r.get("utt") or f"_r{i}"
        if t == "transcript.final":
            u = get(utt)
            u.speaker = str(d.get("speaker", ""))
            u.role = _role_of(u.speaker, d)
            u.lang = str(d.get("lang", ""))
            u.text = str(d.get("text", "")).strip()
            u.ts = int(r.get("ts") or 0)
            try:
                u.conf = float(d.get("asr_conf", 1.0))
            except (TypeError, ValueError):
                u.conf = 1.0
        elif t == "translation.final":
            tl, tx = d.get("target_lang"), d.get("text")
            if tl and isinstance(d.get("seg"), int):  # simultaneous mode: the utterance = segments in order
                parts = segments.setdefault((utt, str(tl)), {})
                parts[d["seg"]] = str(tx or "").strip()
                get(utt).translations[str(tl)] = " ".join(parts[k] for k in sorted(parts) if parts[k])
            elif tl and tx:
                get(utt).translations[str(tl)] = str(tx).strip()
        elif t == "verify.result":
            if d.get("badge") == "red":
                get(utt).red = True
        elif t == "verify.facts":
            if d.get("red"):
                get(utt).red = True
        elif t == "flag":
            if d.get("id"):
                flag_utt[str(d["id"])] = utt
        elif t == "confirm":
            confirms.append({**d, "_utt": r.get("utt")})
        elif t == "offer.update":
            offers.append({**d, "utt": r.get("utt"), "_ts": r.get("ts")})
        elif t == "utterance.continued":
            continued.add(utt)

    for c in confirms:
        utt = flag_utt.get(str(c.get("flag_id"))) or c.get("_utt")
        if utt and utt in by_utt:
            if c.get("accepted") is False:
                by_utt[utt].rejected = True
            elif c.get("accepted"):
                by_utt[utt].confirmed = True
    for utt, status in fact_statuses(records).values():
        u = by_utt.get(str(utt))
        if u is not None and (u.fact_status is None or _RANK[status] > _RANK[u.fact_status]):
            u.fact_status = status

    utts = [by_utt[k] for k in order if by_utt[k].text and k not in continued]
    offers = [o for o in offers if o.get("utt") not in continued]
    utts = [u for u in utts if u.conf >= ASR_MIN_CONF or u.translations]
    utts.sort(key=lambda u: (u.ts, order.index(u.utt)))
    return utts, offers


# ---------------------------------------------------------------- checked facts (raum.pipeline.readback)

_RANK = {"noted": 0, "confirmed": 1, "unconfirmed": 2, "rejected": 3}  # the least sure wins a tie of one fact
STATUS_DE = {"noted": "notiert", "unconfirmed": "unbestätigt", "confirmed": "bestätigt", "rejected": "verneint"}


def fact_statuses(records: list[dict[str, Any]]) -> dict[str, tuple[str | None, str]]:
    """Flag id -> (utt, status) for every flag that carries checked facts: ``noted`` stays noted; an asked one
    (``unconfirmed``) becomes ``confirmed`` / ``rejected`` with the asked person's answer (a tap or aloud)."""
    flags: dict[str, tuple[str | None, str]] = {}
    answers: dict[str, bool] = {}
    for r in records:
        if not isinstance(r, dict):
            continue
        d = r.get("data") or {}
        if r.get("type") == "flag" and d.get("status") in ("noted", "unconfirmed") and d.get("id"):
            flags.setdefault(str(d["id"]), (r.get("utt"), str(d["status"])))
        elif r.get("type") == "confirm" and d.get("flag_id") and d.get("accepted") is not None:
            answers[str(d["flag_id"])] = bool(d["accepted"])
    out = {}
    for fid, (utt, status) in flags.items():
        if status == "unconfirmed" and fid in answers:
            status = "confirmed" if answers[fid] else "rejected"
        out[fid] = (utt, status)
    return out


def collect_facts(records: list[dict[str, Any]], lang_a: str, lang_b: str) -> list[SummaryFact]:
    """The conversation's checked facts in order, one row per fact (the same chip twice is one row; the least
    sure status wins: rejected > unconfirmed > confirmed > noted)."""
    statuses = fact_statuses(records)
    rows: dict[tuple[str, str], SummaryFact] = {}
    seen: set[str] = set()
    for r in records:
        d = (r.get("data") or {}) if isinstance(r, dict) else {}
        fid = str(d.get("id") or "")
        if r.get("type") != "flag" or fid not in statuses or fid in seen:
            continue
        seen.add(fid)
        utt, status = statuses[fid]
        for f in d.get("facts") or []:
            texts = {str(k): str(v) for k, v in (f.get("texts") or {}).items() if v}
            if not texts:
                continue
            text = {lang: texts.get(lang) or texts.get("en") or next(iter(texts.values()))
                    for lang in dict.fromkeys((lang_a, lang_b))}
            key = (str(f.get("kind") or ""), text[lang_a].casefold())
            old = rows.get(key)
            if old is None or _RANK[status] > _RANK[old.status]:
                rows[key] = SummaryFact(kind=key[0], text=text, status=status, utt=utt)  # type: ignore[arg-type]
    return list(rows.values())


# ---------------------------------------------------------------- facts (guard, regex fallback)

_NUM = r"\d+(?:[.,]\d+)*"
_DOSE_RE = re.compile(
    rf"{_NUM}\s*(?:mg|milligramm|milligrams?|mikrogramm|µg|mcg|gramm|g|ml|milliliter|i\.?e\.?|iu|einheiten|units|"
    r"tabletten?|tablets?|tropfen|drops|мг|ملغم?|ملليغرام)(?!\w)", re.IGNORECASE)
_FREQ_RE = re.compile(
    r"(?:\b(?:ein|zwei|drei|vier)mal\s+(?:täglich|am tag|pro tag|pro woche|wöchentlich)"
    r"|\b\d\s*(?:x|mal)\s*(?:täglich|am tag|pro tag)"
    r"|\b(?:once|twice|three times|four times|\d times)\s+(?:a|per)\s+(?:day|week)"
    r"|\b(?:once|twice) daily|\btäglich\b|\bdaily\b|morgens und abends"
    r"|günde\s+\w+\s+(?:kez|defa)|\w+\s+раза?\s+в\s+день|مرت(?:ين|ان))", re.IGNORECASE)
_MONTHS = ("januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember|"
           "january|february|march|june|july|october|december")
_WEEKDAYS = ("montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag|"
             "monday|tuesday|wednesday|thursday|friday|saturday|sunday")
_DATE_RE = re.compile(
    rf"(?:\b\d{{1,2}}\.\s*(?:{_MONTHS})\b|\b\d{{1,2}}\.\d{{1,2}}\.(?:\d{{2,4}})?"
    rf"|\b(?:{_MONTHS})\s+\d{{1,2}}(?:st|nd|rd|th)?\b|\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b"
    rf"|\b(?:{_WEEKDAYS})\b|\bbis (?:{_MONTHS})\b|\bnächste[nr]? woche\b|\bnext week\b)", re.IGNORECASE)
_TIME_RE = re.compile(r"\b\d{1,2}(?::\d{2})?\s*uhr\b|\b\d{1,2}:\d{2}\b|\b\d{1,2}\s*(?:am|pm)\b|\b\d{1,2} o'clock",
                      re.IGNORECASE)
_SCALE = r"(?:mio\.?|millionen|million|mrd\.?|milliarden|billion|tsd\.?|tausend|thousand|k)"
_CURRENCY = r"(?:euro|eur|€|usd|dollar|\$|lira|tl|рубл\w*|евро|يورو|دولار)"
_AMOUNT_RE = re.compile(
    rf"(?:€|\$|eur|usd)\s*\d[\d.,]*(?:\s*{_SCALE}\b)?|\d[\d.,]*\s*(?:{_SCALE}\s*)?{_CURRENCY}"
    rf"|\d[\d.,]*\s*(?:mio\.?|millionen|million|mrd\.?|milliarden|billion)(?!\w)", re.IGNORECASE)
_PERCENT_RE = re.compile(r"\d[\d.,]*\s*(?:%|prozent|percent)", re.IGNORECASE)
_DEADLINE_RE = re.compile(
    r"\bfrist\w*|\w*frist\b|spätestens|bis zum|innerhalb von \d+ \w+|\bdeadline\b|no later than|within \d+ \w+"
    r"|son gün|срок\w*|مهلة", re.IGNORECASE)

ALLERGY_KW = ("allerg", "unverträglich", "alerji", "аллерг", "алерг", "حساسية")
APPOINTMENT_KW = ("termin", "wiederkommen", "kommen sie", "kontrolle", "nachsorge", "appointment", "come back",
                  "follow-up", "follow up", "randevu", "приём", "прием", "придите", "приходите", "موعد")
WARNING_KW = ("sofort", "notaufnahme", "notruf", "notarzt", "112", "atemnot", "brustschmerz", "ohnmacht",
              "bewusstlos", "blutung", "fieber", "schwindel", "ausschlag", "schwellung", "immediately",
              "emergency", "shortness of breath", "chest pain", "faint", "bleeding", "fever", "dizz", " rash",
              "swelling", "acil", "hemen", "ateş", "немедленно", "срочно", "скорую", "температур", "فورا",
              "فوراً", "الطوارئ", "حمى")
DOCUMENT_KW = ("mitbringen", "bringen sie", "unterlagen", "dokument", "vertrag", "kopie", "nachweis",
               "bescheinigung", "lohnabrechnung", "kündigungsschreiben", "schreiben vom", " bring", "document",
               "contract", " copy", "payslip", "belge", "getir", "sözleşme", "документ", "принесите", "договор",
               "مستند", "أحضر", "وثيقة", "عقد")
NEXT_KW = ("nächste", "als nächstes", "wir werden", "ich werde", "einreichen", "reiche", "melden sie sich",
           "schicken", "senden", "next step", " next ", " we will", " i will", " we'll", " i'll", "sonraki",
           "следующ", "мы подадим", "отправ", "الخطوة التالية", "سنقوم", "سوف")
PARTY_KW = ("arbeitgeber", "firma", "gegenseite", "vermieter", "behörde", "employer", "company", "landlord",
            "işveren", "şirket", "работодател", "компани", "صاحب العمل", "الشركة")
AGREED_KW = ("einverstanden", "vereinbart", "abgemacht", " einig", "zugestimmt", "akzeptier", " deal",
             " agreed", " agree", " accept", "anlaştık", "kabul", "согласн", "договорились", "принимаем", "موافق",
             "اتفقنا")
OPEN_KW = ("offen", "klären", "prüfen", "später", "noch nicht", " open", " check", "clarify", " later", "not yet",
           "henüz", "açık", "уточн", "провер", "позже", "لاحقا", "مفتوح")


def _kw(text: str, words: Iterable[str]) -> bool:
    return any(w in text for w in words)


def fallback_extract(text: str, lang: str, glossary: Glossary | None = None) -> list[Fact]:
    """Small regex extractor used until (or when) the fact guard is unavailable."""
    facts: list[Fact] = []

    def add(kind: str, m: re.Match[str]) -> None:
        facts.append(Fact(kind=kind, value=m.group(0).casefold().strip(), surface=m.group(0).strip(),
                          span=(m.start(), m.end())))

    for kind, rx in (("dose", _DOSE_RE), ("frequency", _FREQ_RE), ("date", _DATE_RE), ("time", _TIME_RE),
                     ("amount", _AMOUNT_RE), ("percent", _PERCENT_RE), ("deadline", _DEADLINE_RE)):
        for m in rx.finditer(text):
            add(kind, m)
    if glossary is not None:
        for t in glossary.find(text, lang, kinds=["medication"]):
            facts.append(Fact(kind="medication", value=t.de.casefold(), surface=t.de))
    low = text.casefold()
    if _kw(low, ALLERGY_KW):
        facts.append(Fact(kind="allergy", value="allergy", surface="allergy"))
    return facts


_FACT_KINDS = ("dose", "frequency", "allergy", "date", "time", "medication", "amount", "currency", "percent",
               "deadline", "name", "number")


def extract_facts(text: str, lang: str, glossary: Glossary | None) -> list[Fact]:
    try:
        facts = list(guard.extract(text, lang, kinds=_FACT_KINDS, glossary=glossary))
    except NotImplementedError:
        return fallback_extract(text, lang, glossary)
    except Exception as e:  # noqa: BLE001 - never let the guard break the summary
        log.debug("guard.extract failed (%s); using regex fallback", e)
        return fallback_extract(text, lang, glossary)
    # the guard does not do keyword allergies or bare amounts in every language: merge the fallback's
    kinds = {f.kind for f in facts}
    for f in fallback_extract(text, lang, glossary):
        if f.kind not in kinds:
            facts.append(f)
    return facts


# ---------------------------------------------------------------- mock (deterministic) path


@dataclass
class _Ctx:
    mode: ModeConfig
    utts: list[Utterance]
    facts: dict[str, list[Fact]]
    offers: list[dict[str, Any]]
    lang_a: str
    lang_b: str
    used: set[str] = field(default_factory=set)

    def kinds(self, u: Utterance) -> set[str]:
        return {f.kind for f in self.facts.get(u.utt, [])}

    def first(self, u: Utterance, kind: str) -> str | None:
        for f in self.facts.get(u.utt, []):
            if f.kind == kind:
                return f.surface
        return None

    def all_of(self, u: Utterance, kind: str) -> str | None:
        vals = list(dict.fromkeys(f.surface for f in self.facts.get(u.utt, []) if f.kind == kind))
        return ", ".join(vals) if vals else None

    def item(self, u: Utterance, **extra: Any) -> SummaryItem:
        self.used.add(u.utt)
        if u.confirmed:
            extra["confirmed"] = True
        if u.fact_status:
            extra["status"] = u.fact_status
        return SummaryItem(text=u.bilingual(self.lang_a, self.lang_b), uncertain=u.uncertain, utt=u.utt, **extra)


def _medications(c: _Ctx) -> list[SummaryItem]:
    out = []
    for u in c.utts:
        k = c.kinds(u)
        allergy = "allergy" in k or _kw(u.all_text, ALLERGY_KW)
        if "dose" in k or ("frequency" in k and "medication" in k) or ("medication" in k and not allergy):
            facts = {key: v for key, v in (("name", c.first(u, "medication")), ("dose", c.first(u, "dose")),
                                           ("frequency", c.first(u, "frequency"))) if v}
            out.append(c.item(u, facts=facts))
    return out


def _allergies(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if "allergy" in c.kinds(u) or _kw(u.all_text, ALLERGY_KW)]


def _next_appointment(c: _Ctx) -> list[SummaryItem]:
    dated = [u for u in c.utts if c.kinds(u) & {"date", "time"}]
    best = [u for u in dated if _kw(u.all_text, APPOINTMENT_KW)] or [u for u in dated if u.role == "a"]
    if not best:
        return []
    u = best[-1]
    facts = {key: v for key, v in (("date", c.all_of(u, "date")), ("time", c.all_of(u, "time"))) if v}
    return [c.item(u, facts=facts)]


def _warning_signs(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if _kw(u.all_text, WARNING_KW)]


def _open_questions(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if u.role == "b" and u.is_question]


def _deadlines(c: _Ctx) -> list[SummaryItem]:
    out = []
    for u in c.utts:
        if "deadline" in c.kinds(u) or _DEADLINE_RE.search(u.all_text):
            facts = {key: v for key, v in (("date", c.all_of(u, "date")), ("deadline", c.first(u, "deadline")))
                     if v}
            out.append(c.item(u, facts=facts))
    return out


def _dates(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u, facts={"date": c.all_of(u, "date")}) for u in c.utts
            if "date" in c.kinds(u) and not ("deadline" in c.kinds(u) or _DEADLINE_RE.search(u.all_text))]


def _documents(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if _kw(u.all_text, DOCUMENT_KW)]


def _next_steps(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if _kw(u.all_text, NEXT_KW)]


def _parties(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if _kw(u.all_text, PARTY_KW)]


def _facts(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if u.role == "b" and not u.is_question and u.utt not in c.used]


def _agreed(c: _Ctx) -> list[SummaryItem]:
    return [c.item(u) for u in c.utts if _kw(u.all_text, AGREED_KW)]


def _open(c: _Ctx) -> list[SummaryItem]:
    agreed = {u.utt for u in c.utts if _kw(u.all_text, AGREED_KW)}
    return [c.item(u) for u in c.utts if u.utt not in agreed and (u.is_question or _kw(u.all_text, OPEN_KW))]


def format_amount(amount: float, lang: str) -> str:
    whole = abs(amount - round(amount)) < 1e-9
    s = f"{amount:,.0f}" if whole else f"{amount:,.2f}"
    if lang in ("de", "tr", "it", "es", "pt", "ro", "pl", "hr", "sr", "bg"):
        s = s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
    elif lang in ("ru", "uk", "fr"):
        s = s.replace(",", " ").replace(".", ",")
    return s


def _offers(c: _Ctx) -> list[SummaryItem]:
    if not c.offers:
        return [c.item(u, facts={"amount": c.first(u, "amount")}) for u in c.utts if "amount" in c.kinds(u)]
    out = []
    by_utt = {u.utt: u for u in c.utts}
    for o in c.offers:
        try:
            amount = float(o.get("amount", 0))
        except (TypeError, ValueError):
            continue
        cur = str(o.get("currency", "")).upper()
        by = str(o.get("by", ""))
        text: dict[str, str] = {}
        for lang in (c.lang_a, c.lang_b):
            who = c.mode.label(by, lang) if by in ("a", "b") else by
            text[lang] = f"{format_amount(amount, lang)} {cur} ({who})".strip()
        u = by_utt.get(str(o.get("utt")))
        extra: dict[str, Any] = {"amount": amount, "currency": cur, "by": by, "ts": o.get("ts") or o.get("_ts")}
        if u is not None:
            c.used.add(u.utt)
            extra["utt"] = u.utt
            extra["source"] = u.bilingual(c.lang_a, c.lang_b)
        out.append(SummaryItem(text=text, uncertain=bool(u and u.uncertain), **extra))
    return out


RULES: dict[str, Callable[[_Ctx], list[SummaryItem]]] = {
    "medications": _medications,
    "allergies": _allergies,
    "next_appointment": _next_appointment,
    "warning_signs": _warning_signs,
    "open_questions": _open_questions,
    "parties": _parties,
    "dates": _dates,
    "deadlines": _deadlines,
    "documents_requested": _documents,
    "next_steps": _next_steps,
    "agreed": _agreed,
    "open": _open,
    "offers": _offers,
    "facts": _facts,  # last: the client's statements not already filed elsewhere
}


def _fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000).strftime("%H:%M") if ts else "?"


def file_note_de(mode: ModeConfig, sections: list[SummarySection], utts: list[Utterance], lang_a: str,
                 lang_b: str, checked: list[SummaryFact] | None = None) -> dict[str, str]:
    """German note for the professional's file (S-04 / K-03); the checked facts with their status."""
    note_lang = "de" if "de" in (lang_a, lang_b) else lang_a
    kind = NOTE_NAMES_DE.get(mode.id, "Gesprächsnotiz")
    start = utts[0].ts if utts else 0
    end = utts[-1].ts if utts else 0
    day = datetime.fromtimestamp(start / 1000).strftime("%d.%m.%Y") if start else ""
    langs = " / ".join(dict.fromkeys(LANG_NAMES_DE.get(x, x) for x in (lang_a, lang_b)))
    lines = [f"{kind} {mode.name.get('de', mode.id)}" + (f", {day}, {_fmt_time(start)}–{_fmt_time(end)} Uhr"
                                                           if day else "") + f". Sprachen: {langs}."]
    any_items = False
    for s in sections:
        if not s.items:
            continue
        any_items = True
        title = s.title.get("de") or s.title.get(note_lang) or s.key
        parts = []
        for i in s.items:
            facts = (i.model_extra or {}).get("facts") or {}
            if s.key == "medications" and facts.get("name") and note_lang == lang_a:
                t = " ".join(x for x in (facts["name"], facts.get("dose")) if x)
                t += f", {facts['frequency']}" if facts.get("frequency") else ""
            else:
                t = (i.text.get(note_lang) or next(iter(i.text.values()), "")).rstrip(".")
            parts.append(t + (" (unsicher)" if i.uncertain else ""))
        body = "; ".join(parts)
        lines.append(f"{title}: {body}" + ("" if body.endswith(("?", "!", "؟")) else "."))
    if not any_items:
        lines.append("Keine Einträge.")
    if checked:
        rows = [f"{(f.text.get(note_lang) or next(iter(f.text.values()), '')).rstrip('.')} "
                f"({STATUS_DE.get(f.status, f.status)})" for f in checked]
        lines.append("Geprüfte Fakten: " + "; ".join(rows) + ".")
    lines.append("Gedolmetscht mit RAUM (lokale KI, kein beeidigter Dolmetscher). Nur Aussagen aus dem Gespräch.")
    return {note_lang: " ".join(lines)}


def mock_summary(mode: ModeConfig, records: list[dict[str, Any]], lang_a: str, lang_b: str,
                 glossary: Glossary | None = None) -> SummaryReady:
    utts, offers = collect(records)
    if glossary is None:
        glossary = _load_glossary(mode)
    facts: dict[str, list[Fact]] = {}
    for u in utts:
        src = u.text_in(lang_a) or u.text
        lang = lang_a if u.text_in(lang_a) else u.lang
        facts[u.utt] = extract_facts(src, lang, glossary)
    ctx = _Ctx(mode=mode, utts=utts, facts=facts, offers=offers, lang_a=lang_a, lang_b=lang_b)
    built: dict[str, list[SummaryItem]] = {}
    keys = list(mode.summary.sections)
    for key in sorted(keys, key=lambda k: k == "facts"):  # "facts" last
        rule = RULES.get(key)
        built[key] = (rule(ctx) if rule else [])[:MAX_ITEMS]
    sections = [SummarySection(key=k, title=section_title(k, (lang_a, lang_b)), items=built[k]) for k in keys]
    checked = collect_facts(records, lang_a, lang_b)
    note = file_note_de(mode, sections, utts, lang_a, lang_b, checked) if mode.summary.file_note_for else None
    return SummaryReady(template=mode.id, lang_a=lang_a, lang_b=lang_b, sections=sections, file_note=note,
                        facts=checked, source="records")


# ---------------------------------------------------------------- real (model) path


def _load_glossary(mode: ModeConfig) -> Glossary | None:
    try:
        return Glossary.load(mode.glossary) if mode.glossary else None
    except Exception as e:  # noqa: BLE001
        log.warning("glossary %s not loaded: %s", mode.glossary, e)
        return None


def fill_prompt(template: str, values: dict[str, str]) -> str:
    """Replace ``{name}`` slots without touching the JSON braces of the template."""
    out = template
    for k, v in values.items():
        out = out.replace("{" + k + "}", v)
    return out


def transcript_lines(utts: list[Utterance], lang_a: str) -> str:
    lines = []
    for u in utts:
        line = f"{u.role} ({u.lang}): {u.text}"
        if u.lang != lang_a and u.translations.get(lang_a):
            line += f"  [{lang_a}: {u.translations[lang_a]}]"
        lines.append(line)
    return "\n".join(lines) or "(empty)"


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def item_text(key: str, v: Any, mode: ModeConfig, lang: str) -> str:
    if isinstance(v, str):
        return v.strip()
    if not isinstance(v, dict):
        return _s(v)
    if v.get("text"):
        return _s(v["text"])
    if key == "medications":
        head = " ".join(x for x in (_s(v.get("name")), _s(v.get("dose"))) if x)
        return ", ".join(x for x in (head, _s(v.get("frequency")), _s(v.get("duration"))) if x)
    if key == "deadlines":
        s = ": ".join(x for x in (_s(v.get("what")), _s(v.get("date"))) if x)
        return f"{s} ({_s(v['consequence'])})" if _s(v.get("consequence")) else s
    if key == "offers":
        try:
            amount = format_amount(float(v.get("amount") or 0), lang)
        except (TypeError, ValueError):
            amount = _s(v.get("amount"))
        by = _s(v.get("by"))
        who = mode.label(by, lang) if by in ("a", "b") else by
        extra = ", ".join(x for x in (who, _s(v.get("time"))) if x)
        return f"{amount} {_s(v.get('currency')).upper()}".strip() + (f" ({extra})" if extra else "")
    if key == "next_steps" and v.get("what"):
        extra = ", ".join(x for x in (_s(v.get("who")), _s(v.get("when"))) if x)
        return _s(v["what"]) + (f" ({extra})" if extra else "")
    return ", ".join(_s(x) for k, x in v.items() if k != "uncertain" and _s(x))


def _as_list(v: Any) -> list[Any]:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


async def translate_text(engines: Engines, mode: ModeConfig, text: str, src: str, tgt: str,
                         glossary: Glossary | None) -> str:
    req = TranslateRequest(
        text=text, src_lang=src, tgt_lang=tgt, room_name=mode.display_name("en"),
        role_a=mode.label("a", "en"), role_b=mode.label("b", "en"), lang_a=src, lang_b=tgt,
        register=mode.register, glossary=glossary.prompt_lines(src, tgt) if glossary else "(none)")
    parts = [d async for d in engines.translate.stream(req)]
    return "".join(parts).strip() or text


def uses_model(engines: Engines | None) -> bool:
    if engines is None or engines.verify is None:
        return False
    name = (getattr(engines.verify, "name", "") or engines.names.get("verify", "") or "").lower()
    return not (name.startswith("mock") or name in ("off", "none"))


async def model_summary(mode: ModeConfig, records: list[dict[str, Any]], engines: Engines, lang_a: str,
                        lang_b: str) -> SummaryReady:
    assert engines.verify is not None
    utts, _ = collect(records)
    glossary = _load_glossary(mode)
    template = (repo_root() / mode.prompts.summary).read_text(encoding="utf-8")
    prompt = fill_prompt(template, {
        "lang_a": LANG_NAMES_EN.get(lang_a, lang_a), "lang_b": LANG_NAMES_EN.get(lang_b, lang_b),
        "role_a": mode.label("a", "en"), "role_b": mode.label("b", "en"),
        "transcript": transcript_lines(utts, lang_a),
    })
    data = await engines.verify.complete_json(prompt, purpose="summary")
    if not isinstance(data, dict):
        raise TypeError("summary model did not return a JSON object")

    rows: dict[str, list[tuple[str, bool]]] = {}
    for key in mode.summary.sections:
        items = []
        for v in _as_list(data.get(key)):
            t = item_text(key, v, mode, lang_a)
            if t:
                items.append((t, bool(isinstance(v, dict) and v.get("uncertain"))))
        rows[key] = items[:MAX_ITEMS]

    translated: dict[str, str] = {}
    if lang_b != lang_a:
        uniq = list(dict.fromkeys(t for items in rows.values() for t, _ in items))
        results = await asyncio.gather(*(translate_text(engines, mode, t, lang_a, lang_b, glossary) for t in uniq))
        translated = dict(zip(uniq, results))

    sections = []
    for key in mode.summary.sections:
        items = [SummaryItem(text={lang_a: t, lang_b: translated.get(t, t)}, uncertain=u) for t, u in rows[key]]
        sections.append(SummarySection(key=key, title=section_title(key, (lang_a, lang_b)), items=items))

    note = None
    checked = collect_facts(records, lang_a, lang_b)
    if mode.summary.file_note_for:
        raw = data.get("file_note_de") or data.get("case_note_de") or data.get("file_note")
        note = ({"de" if lang_a == "de" else lang_a: _s(raw)} if _s(raw)
                else file_note_de(mode, sections, utts, lang_a, lang_b, checked))
    return SummaryReady(template=mode.id, lang_a=lang_a, lang_b=lang_b, sections=sections, file_note=note,
                        facts=checked, source="model")


async def build_summary(mode: ModeConfig, records: list[dict[str, Any]], engines: Engines | None, lang_a: str,
                        lang_b: str) -> SummaryReady:
    t0 = time.perf_counter()
    summary: SummaryReady | None = None
    if uses_model(engines):
        assert engines is not None
        try:
            summary = await asyncio.wait_for(model_summary(mode, records, engines, lang_a, lang_b),
                                             MODEL_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - timeout, bad JSON, model down: the summary must still appear
            log.warning("model summary failed (%s: %s); using the deterministic summary", type(e).__name__, e)
    if summary is None:
        summary = mock_summary(mode, records, lang_a, lang_b)
    summary.generated_ms = round((time.perf_counter() - t0) * 1000, 1)
    return summary
