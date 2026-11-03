"""Spell out numbers in the target language before TTS (models read digits unreliably).
Uses num2words (LGPL-2.1, used unmodified as a library)."""
import re
try:
    from num2words import num2words
except Exception:  # pragma: no cover
    num2words = None

_N2W = {"de": "de", "en": "en", "ru": "ru", "tr": "tr", "ar": "ar", "fr": "fr", "es": "es", "it": "it",
        "pt": "pt", "nl": "nl", "pl": "pl", "da": "dk", "no": "no", "sv": "sv", "fi": "fi", "he": "he",
        "ko": "ko", "ja": "ja", "hi": "hi", "el": "el"}
_PCT = {"de": "Prozent", "en": "percent", "ru": "процентов", "tr": "yüzde", "ar": "بالمئة"}
_NUM = re.compile(r"(?<![\w.,])(\d{1,3}(?:[ . ]\d{3})+|\d+)(?:([.,])(\d+))?(\s?%)?(?![\w])")


def _words(n, lang):
    try:
        return num2words(n, lang=_N2W[lang])
    except Exception:
        return None


# Units after a number are spelled out too: the voice read "500 mg" letter by letter, and the echo check
# heard "500-male gay" on every run (en and ru). Forms: en (one, many); de/tr one form; ru (1, 2-4, 5+),
# where a fraction takes the 2-4 form ("1,5 миллиграмма"). Symbols are case sensitive ("5G" is not grams).
_UNITS = [  # (symbols, en, de, ru, tr); longest symbols first inside one alternation
    (("mmHg", "мм рт. ст.", "мм рт.ст.", "мм рт ст"), ("millimeter of mercury", "millimeters of mercury"),
     "Millimeter Quecksilbersäule", ("миллиметр ртутного столба", "миллиметра ртутного столба",
                                     "миллиметров ртутного столба"), "milimetre cıva"),
    (("mmol", "ммоль"), ("millimole", "millimoles"), "Millimol", ("миллимоль", "миллимоля", "миллимолей"), "milimol"),
    (("mcg", "µg", "μg", "мкг"), ("microgram", "micrograms"), "Mikrogramm", ("микрограмм", "микрограмма", "микрограммов"),
     "mikrogram"),
    (("mg", "мг"), ("milligram", "milligrams"), "Milligramm", ("миллиграмм", "миллиграмма", "миллиграммов"), "miligram"),
    (("kg", "кг"), ("kilogram", "kilograms"), "Kilogramm", ("килограмм", "килограмма", "килограммов"), "kilogram"),
    (("ml", "mL", "мл"), ("milliliter", "milliliters"), "Milliliter", ("миллилитр", "миллилитра", "миллилитров"),
     "mililitre"),
    (("dl", "dL", "дл"), ("deciliter", "deciliters"), "Deziliter", ("децилитр", "децилитра", "децилитров"), "desilitre"),
    (("mm", "мм"), ("millimeter", "millimeters"), "Millimeter", ("миллиметр", "миллиметра", "миллиметров"), "milimetre"),
    (("cm", "см"), ("centimeter", "centimeters"), "Zentimeter", ("сантиметр", "сантиметра", "сантиметров"), "santimetre"),
    (("°C", "° C"), ("degree Celsius", "degrees Celsius"), "Grad Celsius", ("градус Цельсия", "градуса Цельсия",
                                                                         "градусов Цельсия"), "santigrat derece"),
    (("IU", "IE", "ЕД", "ед."), ("international unit", "international units"),
     ("internationale Einheit", "internationale Einheiten"),
     ("единица", "единицы", "единиц"), "ünite"),
    (("g", "г"), ("gram", "grams"), "Gramm", ("грамм", "грамма", "граммов"), "gram"),
    (("l", "L", "л"), ("liter", "liters"), "Liter", ("литр", "литра", "литров"), "litre"),
]
_PER = {  # "5 mg/kg", "5,5 ммоль/л"
    "kg": ("per kilogram", "pro Kilogramm", "на килограмм", "kilogram başına"),
    "кг": ("per kilogram", "pro Kilogramm", "на килограмм", "kilogram başına"),
    "l": ("per liter", "pro Liter", "на литр", "litre başına"), "L": ("per liter", "pro Liter", "на литр", "litre başına"),
    "л": ("per liter", "pro Liter", "на литр", "litre başına"),
    "dl": ("per deciliter", "pro Deziliter", "на децилитр", "desilitre başına"),
    "dL": ("per deciliter", "pro Deziliter", "на децилитр", "desilitre başına"),
    "дл": ("per deciliter", "pro Deziliter", "на децилитр", "desilitre başına"),
    "h": ("per hour", "pro Stunde", "в час", "saatte"), "ч": ("per hour", "pro Stunde", "в час", "saatte"),
    "d": ("per day", "pro Tag", "в сутки", "günde"), "Tag": ("per day", "pro Tag", "в сутки", "günde"),
    "day": ("per day", "pro Tag", "в сутки", "günde"), "сут": ("per day", "pro Tag", "в сутки", "günde"),
}
_UNIT_OF = {s: u for u in _UNITS for s in u[0]}
_UNIT_RE = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[ . ]\d{3})+|\d+)(?:([.,])(\d+))?\s?("
    + "|".join(re.escape(s) for s in sorted(_UNIT_OF, key=len, reverse=True))
    + r")(?:/(" + "|".join(re.escape(s) for s in sorted(_PER, key=len, reverse=True)) + r"))?(?![\w])")
_LANG_COL = {"en": 1, "de": 2, "ru": 3, "tr": 4}
_RU_FEM = {"IU", "IE", "ЕД", "ед."}  # единица is feminine


def _words_f(n: int):
    try:
        return num2words(n, lang="ru", gender="f")
    except Exception:  # noqa: BLE001 - any num2words failure keeps the digits
        return None


def _ru_form(n: int) -> int:
    if n % 10 == 1 and n % 100 != 11:
        return 0
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return 1
    return 2


def _units(text: str, lang: str) -> str:
    col = _LANG_COL.get(lang)
    if col is None:
        return text

    def rep(m):
        ip, sep, frac, sym, per = m.groups()
        n = int(re.sub(r"[ . ]", "", ip))
        if sym in ("г", "g") and lang == "ru" and sep is None and 1800 <= n <= 2199 and not per:
            return m.group(0)  # "2024 г." is a year in Russian, not grams
        num = ip if sep is None else f"{ip}{sep}{frac}"
        fraction = frac is not None and len(frac) <= 2
        forms = _UNIT_OF[sym][col]
        if lang == "en":
            word = forms[0] if (n == 1 and not fraction) else forms[1]
        elif lang == "ru":
            word = forms[1] if fraction else forms[_ru_form(n)]
            if sym in _RU_FEM and not fraction and sep is None:
                num = _words_f(n) or num  # "две единицы", not "два единицы"
        elif isinstance(forms, tuple):
            word = forms[0] if (n == 1 and not fraction) else forms[1]
        else:
            word = forms
        if lang == "de" and n == 1 and sep is None:
            num = "eine" if sym in _RU_FEM else "ein"  # "ein Milligramm", "eine Einheit" (not "eins")
        if per:
            word += " " + _PER[per][col - 1]
        rest = m.string[m.end():]
        if sym.endswith(".") and not per and (not rest.strip() or rest.lstrip()[:1].isupper()):
            word += "."  # the abbreviation's dot also ended the sentence: keep the sentence end
        return f"{num} {word}"

    return _UNIT_RE.sub(rep, text)


def normalize_text(text: str, lang: str) -> str:
    if num2words is None or lang not in _N2W:
        return text
    text = _units(text, lang)

    def rep(m):
        ip, sep, frac, pct = m.group(1), m.group(2), m.group(3), m.group(4)
        ip = re.sub(r"[ . ]", "", ip)
        # "1.5"/"1,5": decimal in both conventions when the fraction has 1-2 digits
        if frac is not None and len(frac) <= 2:
            w = _words(float(f"{ip}.{frac}"), lang)
        elif frac is not None:
            w = _words(int(ip + frac), lang)
        else:
            w = _words(int(ip), lang)
        if w is None:
            return m.group(0)
        if pct:
            p = _PCT.get(lang, "%")
            w = f"{p} {w}" if lang == "tr" else f"{w} {p}"
        return w

    if lang == "tr":  # Turkish writes the percent sign first: %20 -> yüzde yirmi
        text = re.sub(r"%\s?(\d+(?:[.,]\d+)?)", lambda m: "yüzde " + m.group(1), text)
    return _NUM.sub(rep, text)


if __name__ == "__main__":
    for l, s in [("de", "Nehmen Sie zweimal täglich 500 Milligramm, 1,5 Tabletten, 20 %."),
                 ("en", "Take 500 milligrams twice a day, 1.5 tablets."),
                 ("en", "Take 500 mg Metformin, 1 mg at night, 0.5 ml, 5 mg/kg, 120 mmHg, 38 °C."),
                 ("de", "500 mg Metformin, 1,5 l Wasser, 10 IE Insulin, 1 IE."),
                 ("ru", "Принимайте по 500 мг, 2 ЕД инсулина, 1,5 мг, 21 мл, 5,5 ммоль/л, в 2024 г., 120 мм рт. ст."),
                 ("ru", "Принимайте по 500 миллиграммов."), ("tr", "Günde iki kez 500 miligram, %20 değil 20%."),
                 ("ar", "تناول 500 ملليغرام.")]:
        print(l, normalize_text(s, l))
