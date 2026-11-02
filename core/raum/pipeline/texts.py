"""Localised texts the pipeline puts into ``flag`` events and read-backs (de, en, tr, ru, ar; fallback en)."""

from __future__ import annotations

import re

LANGS = ("de", "en", "tr", "ru", "ar")

SAY_AGAIN = {
    "de": "Bitte noch einmal sagen.",
    "en": "Please say that again.",
    "tr": "Lütfen tekrar söyleyin.",
    "ru": "Пожалуйста, повторите.",
    "ar": "من فضلك، قلها مرة أخرى.",
}

# A read-back, like a human interpreter: a natural clarification, not an exam question ("Stimmt das?" is gone).
# Spoken in the listener's language and shown with small yes/no buttons (raum.pipeline.readback).
JUST_TO_BE_SURE = {
    "de": "Nur zur Sicherheit: {facts}?",
    "en": "Just to be sure: {facts}?",
    "tr": "Emin olmak için: {facts}?",
    "ru": "Просто уточню: {facts}?",
    "ar": "للتأكد فقط: {facts}؟",
}

UNSURE = {
    "de": "Diese Übersetzung ist vielleicht falsch. Bitte beim Gegenüber nachfragen.",
    "en": "This translation may be wrong. Please check with the other person.",
    "tr": "Bu çeviri yanlış olabilir. Lütfen karşınızdaki kişiye sorun.",
    "ru": "Этот перевод может быть неверным. Пожалуйста, уточните у собеседника.",
    "ar": "قد تكون هذه الترجمة خاطئة. يرجى التأكد من الطرف الآخر.",
}

# simultaneous mode: the second check turned red after this part was already shown and spoken
CORRECTION = {
    "de": "Korrektur: Dieser Teil wurde vielleicht falsch übersetzt. Bitte beim Gegenüber nachfragen.",
    "en": "Correction: this part may have been translated wrongly. Please check with the other person.",
    "tr": "Düzeltme: Bu kısım yanlış çevrilmiş olabilir. Lütfen karşınızdaki kişiye sorun.",
    "ru": "Поправка: эта часть, возможно, переведена неверно. Пожалуйста, уточните у собеседника.",
    "ar": "تصحيح: ربما تُرجم هذا الجزء بشكل خاطئ. يرجى التأكد من الطرف الآخر.",
}

LABELS = {
    "allergy": {"de": "Allergie", "en": "Allergy", "tr": "Alerji", "ru": "Аллергия", "ar": "حساسية"},
    "deadline": {"de": "Frist", "en": "Deadline", "tr": "Son tarih", "ru": "Срок", "ar": "المهلة"},
    "date": {"de": "Datum", "en": "Date", "tr": "Tarih", "ru": "Дата", "ar": "التاريخ"},
    "amount": {"de": "Betrag", "en": "Amount", "tr": "Tutar", "ru": "Сумма", "ar": "المبلغ"},
}

_THOUSANDS = {"de": ".", "tr": ".", "ru": " ", "en": ",", "ar": ","}
_DECIMAL = {"de": ",", "tr": ",", "ru": ",", "en": ".", "ar": "."}


def pick(table: dict[str, str], lang: str | None) -> str:
    return table.get(lang or "en") or table["en"]


def all_langs(table: dict[str, str], extra: str | None = None) -> dict[str, str]:
    out = {k: table[k] for k in LANGS if k in table}
    if extra and extra not in out:
        out[extra] = table["en"]
    return out


def per_day(n: int, lang: str) -> str:
    if lang == "de":
        return {1: "einmal täglich", 2: "zweimal täglich", 3: "dreimal täglich"}.get(n, f"{n}-mal täglich")
    if lang == "tr":
        return f"günde {n} kez"
    if lang == "ru":
        word = "раз" if n == 1 or n >= 5 else "раза"
        return f"{n} {word} в день"
    if lang == "ar":
        return {1: "مرة يوميًا", 2: "مرتين يوميًا"}.get(n, f"{n} مرات يوميًا")
    return {1: "once a day", 2: "twice a day"}.get(n, f"{n} times a day")


def format_number(x: float, lang: str) -> str:
    if float(x).is_integer():
        s = f"{int(x):,}".replace(",", "\0")
        return s.replace("\0", _THOUSANDS.get(lang, ","))
    whole, frac = f"{x:,.2f}".rstrip("0").split(".")
    whole = whole.replace(",", _THOUSANDS.get(lang, ","))
    return f"{whole}{_DECIMAL.get(lang, '.')}{frac}"


_PER_DAY = re.compile(r"^(\d+)\s*/\s*(day|d|tag)$", re.IGNORECASE)
_AMOUNT = re.compile(r"^(\d+(?:\.\d+)?)\s*([A-Z]{3})$")


def fact_text(kind: str, value: str | None, lang: str) -> str:
    """A normalised fact value ("2/day", "1200000 EUR", "500 mg") as a short phrase in ``lang``."""
    if not value:
        return "-"
    m = _PER_DAY.match(value.strip())
    if m:
        text = per_day(int(m.group(1)), lang)
    else:
        a = _AMOUNT.match(value.strip())
        text = f"{format_number(float(a.group(1)), lang)} {a.group(2)}" if a else value
    label = LABELS.get(kind)
    return f"{pick(label, lang)}: {text}" if label else text
