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


def normalize_text(text: str, lang: str) -> str:
    if num2words is None or lang not in _N2W:
        return text

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
                 ("ru", "Принимайте по 500 миллиграммов."), ("tr", "Günde iki kez 500 miligram, %20 değil 20%."),
                 ("ar", "تناول 500 ملليغرام.")]:
        print(l, normalize_text(s, l))
