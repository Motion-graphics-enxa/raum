"""Fact guard (V-01, plan §6.8 / §12.1): perturbations, locale formats, no false alarms, timing."""

from __future__ import annotations

import itertools
import json
import statistics
import time
from pathlib import Path

import pytest

from raum.config import repo_root
from raum.glossary import Glossary, Term
from raum.modes import get_mode
from raum.verify.guard import Fact, GuardResult, check, extract, parse_number

MED = Glossary.load("glossaries/medical-de.yaml")
SZ = get_mode("sprechzimmer")
KZ = get_mode("kanzlei")
VR = get_mode("verhandlungsraum")


def values(text: str, lang: str, kind: str | None = None, glossary: Glossary | None = MED) -> list[str]:
    return [f.value for f in extract(text, lang, glossary=glossary) if kind is None or f.kind == kind]


def kinds_of(text: str, lang: str) -> list[tuple[str, str]]:
    return [(f.kind, f.value) for f in extract(text, lang, glossary=MED)]


def room_check(mode, src: str, sl: str, tgt: str, tl: str) -> GuardResult:
    return check(src, sl, tgt, tl, kinds=mode.guard.kinds, confirm_on=mode.guard.confirm_on,
                 glossary=Glossary.load(mode.glossary))


def bad_rows(r: GuardResult) -> list[tuple[str, str | None, str | None]]:
    return [(f.kind, f.src, f.tgt) for f in r.facts if not f.match]


# ---------------------------------------------------------------- §20.7 demo sentences, correct translations

DEMO = {
    "sprechzimmer": [
        ("Ich nehme zweimal täglich 500 mg Metformin.", {
            "en": "I take 500 mg of metformin twice a day.",
            "tr": "Günde iki kez 500 mg Metformin alıyorum.",
            "ru": "Я принимаю 500 мг метформина два раза в день.",
            "ar": "أتناول 500 ملغ من الميتفورمين مرتين يوميًا."}),
        ("Ich bin allergisch gegen Penicillin.", {
            "en": "I am allergic to penicillin.",
            "tr": "Penisiline alerjim var.",
            "ru": "У меня аллергия на пенициллин.",
            "ar": "لدي حساسية من البنسلين."}),
        ("Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", {
            "en": "Please come back on Thursday, 12 November, at 9 a.m.",
            "tr": "12 Kasım Perşembe günü saat 9'da tekrar gelin.",
            "ru": "Приходите снова в четверг, 12 ноября, в 9 часов.",
            "ar": "يرجى العودة يوم الخميس 12 نوفمبر الساعة 9 صباحًا."}),
    ],
    "kanzlei": [
        ("Die Klagefrist endet am 24. November.", {
            "en": "The deadline for filing the claim ends on 24 November.",
            "tr": "Dava açma süresi 24 Kasım'da sona eriyor.",
            "ru": "Срок подачи иска истекает 24 ноября.",
            "ar": "تنتهي مهلة رفع الدعوى في 24 نوفمبر."}),
        ("Bringen Sie bitte Ihren Arbeitsvertrag und die letzten drei Lohnabrechnungen mit.", {
            "en": "Please bring your employment contract and your last three payslips.",
            "tr": "Lütfen iş sözleşmenizi ve son üç maaş bordronuzu getirin.",
            "ru": "Пожалуйста, принесите ваш трудовой договор и три последних расчётных листа.",
            "ar": "يرجى إحضار عقد العمل وآخر ثلاثة كشوف رواتب."}),
    ],
    "verhandlungsraum": [
        ("Unser Angebot liegt bei 1,2 Millionen Euro.", {
            "en": "Our offer is 1.2 million euros.",
            "tr": "Teklifimiz 1,2 milyon Euro.",
            "ru": "Наше предложение составляет 1,2 миллиона евро.",
            "ar": "عرضنا هو 1.2 مليون يورو."}),
        ("Wir können auf 1,05 Millionen gehen, wenn die Lieferung bis März erfolgt.", {
            "en": "We can go down to 1.05 million if delivery is made by March.",
            "tr": "Teslimat Mart ayına kadar yapılırsa 1,05 milyona inebiliriz.",
            "ru": "Мы можем опуститься до 1,05 миллиона, если поставка будет осуществлена до марта.",
            "ar": "يمكننا النزول إلى 1.05 مليون إذا تم التسليم بحلول مارس."}),
    ],
}
DEMO_CASES = [(room, de, lang, tr) for room, items in DEMO.items() for de, trs in items for lang, tr in trs.items()]

# more correct renderings a translator might produce (other word order, scripts, dialects)
VARIANTS = [
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "en",
     "Come back on Thursday, November 12th, at 9 o'clock."),
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "ar",
     "ارجع يوم الخميس 12 تشرين الثاني الساعة التاسعة صباحًا."),
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "ar",
     "عُد يوم الخميس ١٢ نوفمبر الساعة ٩."),
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "ru",
     "Приходите в четверг, 12 ноября, в 9:00."),
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12.11., um 9:00 Uhr wieder.", "en",
     "Come back on Thursday, 12 November, at 9:00."),
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin.", "en", "I take metformin 500 mg twice daily."),
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin.", "ar",
     "آخذ ٥٠٠ ملغ ميتفورمين مرتين في اليوم."),
    ("sprechzimmer", "Ich nehme zweimal täglich 500 Milligramm Metformin.", "ru",
     "Я принимаю метформин по 500 миллиграммов два раза в день."),
    ("sprechzimmer", "Ich bin allergisch gegen Penicillin.", "en", "I have a penicillin allergy."),
    ("sprechzimmer", "Ich bin allergisch gegen Penicillin.", "ar", "عندي حساسية للبنسلين."),
    ("sprechzimmer", "Ich bin nicht allergisch gegen Penicillin.", "en", "I'm not allergic to penicillin."),
    ("sprechzimmer", "Ich bin nicht allergisch gegen Penicillin.", "tr", "Penisiline alerjim yok."),
    ("sprechzimmer", "Ich bin nicht allergisch gegen Penicillin.", "ru", "У меня нет аллергии на пенициллин."),
    ("sprechzimmer", "Ich bin nicht allergisch gegen Penicillin.", "ar", "ليس لدي حساسية من البنسلين."),
    ("sprechzimmer", "Nehmen Sie keine Tabletten mehr.", "tr", "Artık hap almayın."),
    ("sprechzimmer", "Nehmen Sie die Tabletten nicht nüchtern ein.", "tr", "Tabletleri aç karnına almamalısınız."),
    ("sprechzimmer", "Nehmen Sie eine Tablette.", "en", "Take a tablet."),
    ("sprechzimmer", "Nehmen Sie 1 Tablette.", "en", "Take one tablet."),
    ("sprechzimmer", "Nehmen Sie 2 Tabletten.", "ar", "خذ قرصين."),
    ("sprechzimmer", "Ich nehme Metformin und Metformin hilft.", "en", "I take metformin and it helps."),
    ("kanzlei", "Die Klagefrist endet am 24.11.2026.", "en", "The deadline for the claim ends on 24 November 2026."),
    ("kanzlei", "Die Klagefrist endet am 24. November.", "en", "You must file the claim by 24 November."),
    ("kanzlei", "Ich arbeite seit fünf Jahren dort.", "tr", "Orada beş yıldır çalışıyorum."),
    ("kanzlei", "Ich arbeite seit fünf Jahren dort.", "ru", "Я работаю там пять лет."),
    ("kanzlei", "Ich arbeite seit fünf Jahren dort.", "ar", "أعمل هناك منذ خمس سنوات."),
    ("kanzlei", "Mein Arbeitgeber hat mir am 3. November gekündigt.", "tr", "İşverenim beni 3 Kasım'da işten çıkardı."),
    ("kanzlei", "Als nächstes reiche ich die Kündigungsschutzklage beim Arbeitsgericht ein.", "tr",
     "Bir sonraki adımda iş mahkemesine işe iade davası açacağım."),
    ("kanzlei", "Herr Schmidt hat am 3. November gekündigt.", "en", "Mr. Schmidt resigned on 3 November."),
    ("kanzlei", "Herr Schmidt hat am 3. November gekündigt.", "ru", "Господин Шмидт уволился 3 ноября."),
    ("verhandlungsraum", "Unser Angebot liegt bei 1,2 Millionen Euro.", "ar", "عرضنا هو ١\u066b٢ مليون يورو."),
    ("verhandlungsraum", "Unser Angebot liegt bei 1,2 Millionen Euro.", "en", "Our offer stands at €1.2 million."),
    ("verhandlungsraum", "Wir können auf 1,05 Millionen gehen, wenn die Lieferung bis März erfolgt.", "en",
     "We can go to 1.05 million euros if the delivery happens by March."),
    ("verhandlungsraum", "Wir geben 20 % Rabatt.", "tr", "%20 indirim veriyoruz."),
    ("verhandlungsraum", "Wir geben 20 % Rabatt.", "ru", "Мы даём скидку 20 процентов."),
    ("verhandlungsraum", "Wir geben 20 % Rabatt.", "ar", "نقدم خصمًا بنسبة ٢٠٪."),
    ("verhandlungsraum", "Liefern Sie nicht später als am 3. März.", "en", "Deliver no later than 3 March."),
    ("verhandlungsraum", "Liefern Sie spätestens am 3. März.", "en", "Deliver no later than 3 March."),
    ("verhandlungsraum", "Liefern Sie spätestens am 3. März.", "ru", "Поставьте не позднее 3 марта."),
]


@pytest.mark.parametrize(("room", "de", "lang", "tr"), DEMO_CASES + VARIANTS)
def test_no_false_alarms(room, de, lang, tr):
    mode = get_mode(room)
    for a, al, b, bl in ((de, "de", tr, lang), (tr, lang, de, "de")):
        r = room_check(mode, a, al, b, bl)
        assert not r.red, (al, bl, bad_rows(r))
        if (room, de, lang, tr) in DEMO_CASES:
            assert r.facts, "every demo sentence carries at least one fact"
        r_all = check(a, al, b, bl, glossary=Glossary.load(mode.glossary))  # every kind on
        assert not r_all.red, (al, bl, bad_rows(r_all))


def test_demo_facts_are_found_in_every_language():
    expect = {
        0: {("dose", "500 mg"), ("frequency", "2/day"), ("medication", "Metformin")},
        1: {("allergy", "Penicillin"), ("medication", "Penicillin")},
        2: {("date", "wd:thu"), ("date", "--11-12"), ("time", "09:00")},
    }
    for i, (de, trs) in enumerate(DEMO["sprechzimmer"]):
        for lang, text in [("de", de), *trs.items()]:
            assert set(kinds_of(text, lang)) == expect[i], (lang, text)
    kz = Glossary.load(KZ.glossary)
    for lang, text in [("de", DEMO["kanzlei"][0][0]), *DEMO["kanzlei"][0][1].items()]:
        assert [(f.kind, f.value) for f in extract(text, lang, glossary=kz)] == [("deadline", "--11-24")], lang
    for lang, text in [("de", DEMO["kanzlei"][1][0]), *DEMO["kanzlei"][1][1].items()]:
        assert [(f.kind, f.value) for f in extract(text, lang)] == [("number", "3")], lang
    for lang, text in [("de", DEMO["verhandlungsraum"][0][0]), *DEMO["verhandlungsraum"][0][1].items()]:
        assert values(text, lang) == ["1200000 EUR"], lang
    for lang, text in [("de", DEMO["verhandlungsraum"][1][0]), *DEMO["verhandlungsraum"][1][1].items()]:
        assert sorted((f.kind, f.value) for f in extract(text, lang)) == [("amount", "1050000"),
                                                                         ("deadline", "--03")], lang


# ---------------------------------------------------------------- §12.1 perturbations


PERTURBED = [
    # wrong number (3)
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin.", "en",
     "I take 50 mg of metformin twice a day.", ("dose", "500 mg", "50 mg")),
    ("verhandlungsraum", "Unser Angebot liegt bei 1,2 Millionen Euro.", "en",
     "Our offer is 1.5 million euros.", ("amount", "1,2 Millionen Euro", "1.5 million euros")),
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "ru",
     "Приходите снова в четверг, 13 ноября, в 9 часов.", ("date", "12. November", "13 ноября")),
    # wrong unit (2)
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin.", "ar",
     "أتناول 500 غرام من الميتفورمين مرتين يوميًا.", ("dose", "500 mg", "500 غرام")),
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin.", "tr",
     "Günde iki kez 500 ml Metformin alıyorum.", ("dose", "500 mg", "500 ml")),
    # negation flipped (2)
    ("sprechzimmer", "Ich bin nicht allergisch gegen Penicillin.", "en",
     "I am allergic to penicillin.", ("negation", "nicht", None)),
    ("sprechzimmer", "Nehmen Sie Metformin mit dem Essen ein.", "tr",
     "Metformini yemekle birlikte almayın.", ("negation", None, "almayın")),
    # allergy omitted (2)
    ("sprechzimmer", "Ich bin allergisch gegen Penicillin.", "en", "I am allergic.",
     ("allergy", "Penicillin", "allergic")),
    ("sprechzimmer", "Ich nehme zweimal täglich 500 mg Metformin und bin allergisch gegen Penicillin.", "ru",
     "Я принимаю 500 мг метформина два раза в день.", ("allergy", "Penicillin", None)),
    # date shifted (1)
    ("sprechzimmer", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.", "en",
     "Please come back on Friday, 13 November, at 9 a.m.", ("date", "Donnerstag", "Friday")),
]


@pytest.mark.parametrize(("room", "src", "lang", "bad", "row"), PERTURBED)
def test_perturbations_are_red_with_the_exact_pair(room, src, lang, bad, row):
    r = room_check(get_mode(room), src, "de", bad, lang)
    assert r.red
    assert row in bad_rows(r), bad_rows(r)
    back = room_check(get_mode(room), bad, lang, src, "de")  # also red in the other direction
    assert back.red


def test_more_perturbations():
    def red(src, sl, tgt, tl, **kw):
        return check(src, sl, tgt, tl, glossary=MED, **kw).red

    assert red("Kommen Sie am 12. November wieder.", "de", "Come back on 12 December.", "en")
    assert red("Um 9 Uhr.", "de", "At 9 p.m.", "en")
    assert red("Um 21 Uhr.", "de", "At 9 o'clock.", "en")
    assert red("Nehmen Sie dreimal täglich 2 Tabletten.", "de", "Take 2 tablets twice a day.", "en")
    assert red("Nehmen Sie 500 mg Ibuprofen.", "de", "Take 500 mg of paracetamol.", "en")  # drug swapped
    assert red("Ich bin allergisch gegen Penicillin.", "de", "Penisiline alerjim yok.", "tr")  # negation added
    assert red("Ich bin nicht allergisch gegen Penicillin.", "de", "У меня аллергия на пенициллин.", "ru")
    assert red("Ich bin nicht allergisch gegen Penicillin.", "de", "لدي حساسية من البنسلين.", "ar")
    assert red("Ich nehme Metformin.", "de", "Metformin almıyorum.", "tr")  # -mıyor
    assert red("Wir zahlen 500 Euro.", "de", "We pay 500 dollars.", "en")  # currency swapped
    assert red("Wir zahlen 1,2 Millionen Euro.", "de", "We pay 1.2 billion euros.", "en")
    assert red("Die Frist endet am 24. November.", "de", "Срок истекает 25 ноября.", "ru")
    assert red("Sie haben 7 Tage Zeit.", "de", "You have seven weeks.", "en")
    assert red("Wir geben 20 % Rabatt.", "de", "Мы даём скидку 25 процентов.", "ru")
    assert red("Nehmen Sie alle 8 Stunden 400 mg.", "de", "Take 400 mg every 6 hours.", "en")


# ---------------------------------------------------------------- numbers and locale formats


@pytest.mark.parametrize(("text", "lang", "value"), [
    ("1.200,50", "de", "1200.5"), ("1,200.50", "en", "1200.5"), ("1 200,50", "ru", "1200.5"),
    ("1\u00a0200,50", "ru", "1200.5"), ("1\u202f200,50", "ru", "1200.5"), ("1\u202f200,50", "de", "1200.5"),
    ("1.200,50", "tr", "1200.5"), ("1,2", "de", "1.2"), ("1,2", "tr", "1.2"), ("1,2", "ru", "1.2"),
    ("1.2", "en", "1.2"), ("1,200", "en", "1200"), ("1.200", "de", "1200"), ("1,200", "de", "1.2"),
    ("1.200", "en", "1.2"), ("1.05", "de", "1.05"), ("1.000.000", "de", "1000000"), ("1,000,000", "en", "1000000"),
    ("0,125", "de", "0.125"), ("0.125", "en", "0.125"), ("12", "ar", "12"),
])
def test_parse_number(text, lang, value):
    n = parse_number(text, lang)
    assert n is not None and format(n.normalize(), "f").rstrip("0").rstrip(".") == value.rstrip("0").rstrip(".")


def test_locale_amounts_are_equal():
    texts = [("1.200,50 €", "de"), ("1,200.50 EUR", "en"), ("1 200,50 евро", "ru"), ("1\u00a0200,50 евро", "ru"),
             ("1\u202f200,50 евро", "ru"), ("1.200,50 Euro", "tr"), ("١\u066c٢٠٠\u066b٥٠ يورو", "ar"), ("€1,200.50", "en")]
    for text, lang in texts:
        assert values(text, lang) == ["1200.5 EUR"], (text, lang)
    for (a, al), (b, bl) in itertools.pairwise(texts):
        assert not check(a, al, b, bl).red


def test_arabic_digits_and_units():
    assert values("٥٠٠ ملغ", "ar") == ["500 mg"] == values("500 mg", "en")
    assert not check("٥٠٠ ملغ", "ar", "500 mg", "de").red
    assert values("خذ ٢\u066b٥ مل", "ar") == ["2.5 ml"] == values("Nehmen Sie 2,5 ml", "de")
    assert values("١٢/١١/٢٠٢٦", "ar") == ["2026-11-12"]


def test_scales():
    for text, lang in [("1,2 Millionen Euro", "de"), ("1.2 million euros", "en"), ("1,2 milyon Euro", "tr"),
                       ("1,2 миллиона евро", "ru"), ("1.2 مليون يورو", "ar"), ("1,2 Mio. €", "de"),
                       ("EUR 1.2 million", "en")]:
        assert values(text, lang) == ["1200000 EUR"], (text, lang)
    assert values("eine Million Euro", "de") == ["1000000 EUR"] == values("a million euros", "en")
    assert values("drei Milliarden Dollar", "de") == ["3000000000 USD"] == values("три миллиарда долларов", "ru")
    assert values("5 bin TL", "tr") == ["5000 TRY"]
    assert values("مليونين يورو", "ar") == ["2000000 EUR"]
    assert values("2 Billionen", "de") == ["2000000000000"]  # German Billion = 10^12
    assert values("$500", "en") == ["500 USD"] == values("500 Dollar", "de") == values("500 долларов", "ru")
    assert values("500 دولار", "ar") == ["500 USD"]


NUMBER_WORDS = {
    "de": ["null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht", "neun", "zehn", "elf", "zwölf",
           "dreizehn", "vierzehn", "fünfzehn", "sechzehn", "siebzehn", "achtzehn", "neunzehn", "zwanzig"],
    "en": ["zero", None, "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve",
           "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen", "twenty"],
    "tr": ["sıfır", None, "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz", "on", "on bir", "on iki",
           "on üç", "on dört", "on beş", "on altı", "on yedi", "on sekiz", "on dokuz", "yirmi"],
    "ru": ["ноль", None, "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять", "десять",
           "одиннадцать", "двенадцать", "тринадцать", "четырнадцать", "пятнадцать", "шестнадцать", "семнадцать",
           "восемнадцать", "девятнадцать", "двадцать"],
    "ar": ["صفر", None, "اثنان", "ثلاثة", "أربعة", "خمسة", "ستة", "سبعة", "ثمانية", "تسعة", "عشرة", "أحد عشر",
           "اثنا عشر", "ثلاثة عشر", "أربعة عشر", "خمسة عشر", "ستة عشر", "سبعة عشر", "ثمانية عشر", "تسعة عشر",
           "عشرون"],
}


@pytest.mark.parametrize("lang", sorted(NUMBER_WORDS))
def test_number_words_0_to_20(lang):
    for n, word in enumerate(NUMBER_WORDS[lang]):
        if word is None:
            continue
        assert values(f"{word} .", lang, "number") == [str(n)], (lang, word)


def test_number_words_match_digits_across_languages():
    assert not check("die letzten drei Lohnabrechnungen", "de", "the last 3 payslips", "en").red
    assert not check("zwölf Tabletten", "de", "12 tablets", "en").red
    assert not check("üç tablet", "tr", "три таблетки", "ru").red
    assert not check("خمس سنوات", "ar", "5 Jahre", "de").red
    assert check("drei Tabletten", "de", "four tablets", "en").red
    # 1 is weak: "ein"/"a"/"bir" are articles, so they satisfy a 1 but never raise an alarm
    assert not check("Nehmen Sie eine Tablette.", "de", "Take a tablet.", "en").red
    assert not check("Nehmen Sie 1 Tablette.", "de", "Take a tablet.", "en").red
    assert not check("Bir tablet alın.", "tr", "Take one tablet.", "en").red
    assert check("Nehmen Sie 1 Tablette.", "de", "Take tablets.", "en").red


def test_no_double_counting():
    assert kinds_of("Ich nehme zweimal täglich 500 mg Metformin.", "de") == [
        ("frequency", "2/day"), ("dose", "500 mg"), ("medication", "Metformin")]
    assert [k for k, _ in kinds_of("Kommen Sie am 12.11.2026 um 9:30 Uhr.", "de")] == ["date", "time"]
    assert [k for k, _ in kinds_of("Wir geben 20 % auf 1.200 €.", "de")] == ["percent", "amount"]
    assert [k for k, _ in kinds_of("Sie haben 7 Tage Zeit.", "de")] == ["duration"]


# ---------------------------------------------------------------- units, frequency, dates, times, durations


def test_unit_synonyms():
    for text, lang in [("500 mg", "de"), ("500 Milligramm", "de"), ("500 milligrams", "en"), ("500 мг", "ru"),
                       ("500 миллиграммов", "ru"), ("500 ملغ", "ar"), ("500 مجم", "ar"), ("500 ملليغرام", "ar"),
                       ("500 miligram", "tr"), ("0,5 g", "de"), ("500.000 µg", "de"), ("500000 mcg", "en")]:
        assert values(text, lang) == ["500 mg"], (text, lang)
    assert values("100 IE", "de") == ["100 IU"] == values("100 IU", "en") == values("100 МЕ", "ru")
    assert values("10 Mikrogramm", "de") == ["0.01 mg"] == values("10 µg", "en") == values("10 μg", "en")
    assert values("5 ml", "en") == ["5 ml"] == values("5 мл", "ru") == values("5 مل", "ar")
    assert values("in Milligramm", "de", "unit") == ["mg"]


@pytest.mark.parametrize(("text", "lang", "value"), [
    ("zweimal täglich", "de", "2/day"), ("dreimal täglich", "de", "3/day"), ("2x täglich", "de", "2/day"),
    ("2 x täglich", "de", "2/day"), ("3-mal am Tag", "de", "3/day"), ("einmal pro Woche", "de", "1/week"),
    ("täglich", "de", "1/day"), ("alle 8 Stunden", "de", "q8h"),
    ("twice a day", "en", "2/day"), ("three times a day", "en", "3/day"), ("twice daily", "en", "2/day"),
    ("3 times per day", "en", "3/day"), ("once a week", "en", "1/week"), ("every 8 hours", "en", "q8h"),
    ("daily", "en", "1/day"),
    ("günde iki kez", "tr", "2/day"), ("günde iki defa", "tr", "2/day"), ("günde 3 kere", "tr", "3/day"),
    ("günde bir kez", "tr", "1/day"), ("haftada bir kez", "tr", "1/week"), ("8 saatte bir", "tr", "q8h"),
    ("два раза в день", "ru", "2/day"), ("три раза в день", "ru", "3/day"), ("дважды в день", "ru", "2/day"),
    ("один раз в день", "ru", "1/day"), ("раз в неделю", "ru", "1/week"), ("каждые 8 часов", "ru", "q8h"),
    ("ежедневно", "ru", "1/day"),
    ("مرتين يوميًا", "ar", "2/day"), ("مرتين في اليوم", "ar", "2/day"), ("ثلاث مرات يوميا", "ar", "3/day"),
    ("ثلاث مرات في اليوم", "ar", "3/day"), ("مرة واحدة يوميا", "ar", "1/day"), ("كل 8 ساعات", "ar", "q8h"),
    ("مرة في الأسبوع", "ar", "1/week"),
])
def test_frequency(text, lang, value):
    assert values(text, lang, "frequency") == [value]


@pytest.mark.parametrize(("text", "lang", "value"), [
    ("am 12. November", "de", "--11-12"), ("12.11.", "de", "--11-12"), ("12.11.2026", "de", "2026-11-12"),
    ("12/11", "en", "--11-12"), ("12 November", "en", "--11-12"), ("November 12th", "en", "--11-12"),
    ("the 12th of November", "en", "--11-12"), ("November 12, 2026", "en", "2026-11-12"),
    ("12 Kasım", "tr", "--11-12"), ("12 Kasım'da", "tr", "--11-12"), ("12.11.2026", "tr", "2026-11-12"),
    ("12 ноября", "ru", "--11-12"), ("12 ноября 2026 г.", "ru", "2026-11-12"), ("12-го ноября", "ru", "--11-12"),
    ("12 نوفمبر", "ar", "--11-12"), ("12 تشرين الثاني", "ar", "--11-12"), ("١٢ نوفمبر", "ar", "--11-12"),
    ("2026-11-12", "en", "2026-11-12"), ("am 12.", "de", "---12"), ("on the 12th.", "en", "---12"),
    ("Donnerstag", "de", "wd:thu"), ("Thursday", "en", "wd:thu"), ("Perşembe", "tr", "wd:thu"),
    ("в четверг", "ru", "wd:thu"), ("يوم الخميس", "ar", "wd:thu"), ("im März", "de", "--03"),
    ("in March", "en", "--03"), ("до марта", "ru", "--03"), ("في مارس", "ar", "--03"),
])
def test_dates(text, lang, value):
    assert values(text, lang, None)[-1:] == [value] or value in values(text, lang), values(text, lang)


def test_dates_do_not_fire_on_ordinary_words():
    assert values("You may come.", "en") == []
    assert values("They march on.", "en") == []
    assert values("Nehmen Sie 1/2 Tablette.", "de", "date") == []
    assert values("We are open 24/7.", "en", "date") == []
    assert values("Masanın altı", "tr", "date") == []


@pytest.mark.parametrize(("text", "lang", "value"), [
    ("um 9 Uhr", "de", "09:00"), ("9:00", "de", "09:00"), ("um 9.30 Uhr", "de", "09:30"), ("21 Uhr", "de", "21:00"),
    ("um 9 Uhr abends", "de", "21:00"), ("neun Uhr", "de", "09:00"), ("um 9.", "de", "09:00"),
    ("9 a.m.", "en", "09:00"), ("9am", "en", "09:00"), ("9 pm", "en", "21:00"), ("9 o'clock", "en", "09:00"),
    ("at 9.", "en", "09:00"), ("12 a.m.", "en", "00:00"),
    ("saat 9", "tr", "09:00"), ("saat 9'da", "tr", "09:00"), ("saat 09.00", "tr", "09:00"),
    ("akşam saat 9'da", "tr", "21:00"), ("saat dokuzda", "tr", "09:00"),
    ("в 9 часов", "ru", "09:00"), ("в 9:00", "ru", "09:00"), ("в 9 утра", "ru", "09:00"),
    ("в 9 вечера", "ru", "21:00"), ("в девять часов", "ru", "09:00"),
    ("الساعة 9", "ar", "09:00"), ("الساعة ٩", "ar", "09:00"), ("الساعة التاسعة", "ar", "09:00"),
    ("الساعة 9 مساءً", "ar", "21:00"), ("الساعة التاسعة والنصف", "ar", "09:30"),
])
def test_times(text, lang, value):
    assert values(text, lang, "time") == [value]


@pytest.mark.parametrize(("text", "lang"), [("7 Tage", "de"), ("sieben Tage", "de"), ("seven days", "en"),
                                            ("7 days", "en"), ("7 gün", "tr"), ("yedi gün", "tr"), ("7 дней", "ru"),
                                            ("семь дней", "ru"), ("7 أيام", "ar"), ("سبعة أيام", "ar")])
def test_durations(text, lang):
    assert values(text, lang, "duration") == ["P7D"]


def test_durations_other_units():
    assert values("seit fünf Jahren", "de") == ["P5Y"] == values("beş yıldır", "tr") == values("пять лет", "ru")
    assert values("خمس سنوات", "ar") == ["P5Y"] == values("for five years", "en")
    assert values("يومين", "ar") == ["P2D"] == values("2 Tage", "de")
    assert values("8 Stunden", "de") == ["PT8H"] == values("8 saat", "tr")


@pytest.mark.parametrize(("text", "lang"), [("20 %", "de"), ("20 Prozent", "de"), ("20 percent", "en"),
                                            ("20%", "en"), ("%20", "tr"), ("yüzde 20", "tr"),
                                            ("20 процентов", "ru"), ("20 بالمئة", "ar"), ("٢٠٪", "ar"),
                                            ("20 في المئة", "ar")])
def test_percent(text, lang):
    assert values(text, lang) == ["20%"]


# ---------------------------------------------------------------- medications, allergy, negation, names, deadlines


def test_medications_in_other_scripts():
    for text, lang in [("Metformin", "de"), ("метформина", "ru"), ("الميتفورمين", "ar"), ("Metformin'i", "tr"),
                       ("метформин", "ru")]:
        assert values(text, lang, "medication") == ["Metformin"], (text, lang)
    assert values("بالبنسلين", "ar", "medication") == ["Penicillin"]
    assert values("Ich nehme Metformin, Metformin!", "de", "medication") == ["Metformin"]  # a set
    assert values("Metformin", "de", "medication", glossary=None) == []


def test_allergy():
    assert values("Ich bin allergisch gegen Penicillin.", "de", "allergy") == ["Penicillin"]
    assert values("Ich habe eine Penicillinallergie.", "de", "allergy") == ["Penicillin"]
    assert values("I have a penicillin allergy.", "en", "allergy") == ["Penicillin"]
    assert values("Penisiline alerjim var.", "tr", "allergy") == ["Penicillin"]
    assert values("У меня аллергия на пенициллин.", "ru", "allergy") == ["Penicillin"]
    assert values("عندي حساسية من البنسلين", "ar", "allergy") == ["Penicillin"]
    assert values("Ich bin allergisch gegen Nüsse.", "de", "allergy") == ["present"]
    assert values("Ich bin allergisch gegen Penicillin und Amoxicillin.", "de", "allergy") == [
        "Penicillin", "Amoxicillin"]
    # the drug I take is not an allergen
    assert values("I take metformin twice a day, and I'm allergic to penicillin.", "en", "allergy") == ["Penicillin"]
    assert values("Ich bin allergisch gegen Penicillin, aber Metformin vertrage ich.", "de", "allergy") == [
        "Penicillin"]


@pytest.mark.parametrize(("text", "lang"), [
    ("Ich bin nicht allergisch.", "de"), ("Ich habe keine Allergien.", "de"), ("Nie wieder.", "de"),
    ("I'm not allergic.", "en"), ("I don't take it.", "en"), ("No allergies.", "en"), ("I never smoke.", "en"),
    ("Alerjim yok.", "tr"), ("Bu doğru değil.", "tr"), ("Metformin almıyorum.", "tr"), ("Almayın.", "tr"),
    ("Almamalısınız.", "tr"), ("Hiç sigara içmedim.", "tr"), ("Alkol içmeyeceksiniz.", "tr"),
    ("У меня нет аллергии.", "ru"), ("Я не курю.", "ru"), ("Вам нельзя пить.", "ru"),
    ("لا أدخن.", "ar"), ("لم آخذ الدواء.", "ar"), ("ليس لدي حساسية.", "ar"), ("لن آتي.", "ar"),
    ("يجب ألا تشرب الكحول.", "ar"),
])
def test_negation_cues(text, lang):
    assert values(text, lang, "negation") == ["negated"]


@pytest.mark.parametrize(("text", "lang"), [
    ("Ich nehme Metformin.", "de"), ("Nicht mehr als 2 Tabletten? Spätestens morgen.", "de"),
    ("No later than 3 March.", "en"), ("Metformin alıyorum.", "tr"), ("Orada beş yıldır çalışıyorum.", "tr"),
    ("Dava açma süresi 24 Kasım'da sona eriyor.", "tr"), ("Lütfen getirin.", "tr"), ("Не позднее 3 марта.", "ru"),
    ("لاحقا", "ar"), ("في موعد لا يتجاوز 3 مارس", "ar"), ("Kommen Sie wieder.", "de"),
])
def test_no_negation(text, lang):
    assert values(text, lang, "negation") == []


def test_glossary_names_are_strict_heuristic_names_never_alarm():
    g = Glossary(terms=[Term(de="Frau Yılmaz", kind="name", forms={"en": ["Mrs Yılmaz", "Ms Yılmaz"],
                                                                   "ru": ["госпожа Йылмаз"]})])
    assert not check("Frau Yılmaz kommt.", "de", "Mrs Yılmaz is coming.", "en", glossary=g).red
    assert not check("Frau Yılmaz kommt.", "de", "Госпожа Йылмаз придёт.", "ru", glossary=g).red
    r = check("Frau Yılmaz kommt.", "de", "Mrs Smith is coming.", "en", glossary=g)
    assert r.red and ("name", "Frau Yılmaz", None) in bad_rows(r)
    # not in the glossary: shared verbatim -> a green row; transliterated or missing -> no row, no alarm
    r = check("Herr Schmidt kommt am 3. Mai.", "de", "Mr Schmidt is coming on 3 May.", "en")
    assert not r.red and any(f.kind == "name" and f.src == "Schmidt" and f.match for f in r.facts)
    assert not check("Herr Schmidt kommt.", "de", "Господин Шмидт придёт.", "ru").red
    assert not check("Bringen Sie den Vertrag.", "de", "Bring the contract.", "en").red
    assert [f.value for f in extract("Herr Schmidt kommt.", "de") if f.kind == "name"] == ["Schmidt"]


def test_deadlines_and_confirm():
    kz = Glossary.load(KZ.glossary)
    for text, lang in [("Die Frist endet am 24. November.", "de"), ("bis zum 24. November", "de"),
                       ("spätestens am 24.11.", "de"), ("by 24 November", "en"), ("until November 24", "en"),
                       ("24 Kasım'a kadar", "tr"), ("son gün 24 Kasım", "tr"), ("до 24 ноября", "ru"),
                       ("срок истекает 24 ноября", "ru"), ("حتى 24 نوفمبر", "ar"), ("آخر موعد 24 نوفمبر", "ar")]:
        assert [(f.kind, f.value) for f in extract(text, lang, glossary=kz)] == [("deadline", "--11-24")], (text, lang)
    assert [f.kind for f in extract("Kommen Sie am 24. November.", "de")] == ["date"]
    r = room_check(KZ, "Die Klagefrist endet am 24. November.", "de",
                   "The deadline for filing the claim ends on 24 November.", "en")
    assert not r.red and [(c.kind, c.src, c.tgt) for c in r.confirm] == [("deadline", "24. November", "24 November")]
    # a deadline translated as a plain date is still the same date (no alarm), and a wrong date is red
    assert not room_check(KZ, "Die Klagefrist endet am 24. November.", "de", "Am 24. November.", "de").red
    assert room_check(KZ, "Die Klagefrist endet am 24. November.", "de", "The deadline is 25 November.", "en").red
    # a room without the deadline kind still compares the date
    assert [f.kind for f in extract("bis zum 24. November", "de", kinds=SZ.guard.kinds)] == ["date"]


def test_confirm_list_for_confirm_on():
    r = room_check(SZ, "Ich nehme zweimal täglich 500 mg Metformin und bin allergisch gegen Penicillin.", "de",
                   "أتناول 500 ملغ من الميتفورمين مرتين يوميًا ولدي حساسية من البنسلين.", "ar")
    assert not r.red
    assert [(c.kind, c.src, c.tgt, c.match) for c in r.confirm] == [
        ("dose", "500 mg", "500 ملغ", True), ("allergy", "Penicillin", "البنسلين", True)]
    assert {f.kind for f in r.facts} == {"frequency", "dose", "medication", "allergy"}
    r = room_check(VR, "Unser Angebot liegt bei 1,2 Millionen Euro.", "de", "Our offer is 1.5 million euros.", "en")
    assert r.red and [(c.kind, c.src, c.tgt, c.match) for c in r.confirm] == [
        ("amount", "1,2 Millionen Euro", "1.5 million euros", False)]
    assert check("Hallo", "de", "Hello", "en", confirm_on=["dose"]).confirm == []


def test_rows_one_per_source_value_and_unmatched_target():
    r = check("Nehmen Sie 500 mg.", "de", "Take 500 mg and 200 mg.", "en", kinds=["dose"])
    assert [(f.kind, f.src, f.tgt, f.match) for f in r.facts] == [
        ("dose", "500 mg", "500 mg", True), ("dose", None, "200 mg", False)]
    r = check("Nehmen Sie 500 mg und 200 mg.", "de", "Take 500 mg.", "en", kinds=["dose"])
    assert [(f.src, f.tgt, f.match) for f in r.facts] == [("500 mg", "500 mg", True), ("200 mg", None, False)]
    assert r.red and r.ms > 0


def test_kinds_restrict_and_fall_back_to_number():
    assert values("Nehmen Sie 500 mg.", "de") == ["500 mg"]
    assert [(f.kind, f.value) for f in extract("Nehmen Sie 500 mg.", "de", kinds=KZ.guard.kinds)] == [
        ("number", "500")]
    assert extract("Nehmen Sie 500 mg.", "de", kinds=["date"]) == []
    r = check("Nehmen Sie 500 mg.", "de", "Take 50 mg.", "en", kinds=["date", "time"])
    assert not r.red and r.facts == []


def test_extract_contract():
    fs = extract("Ich nehme 500 Milligramm Metformin.", "de", glossary=MED)
    assert all(isinstance(f, Fact) for f in fs)
    dose = next(f for f in fs if f.kind == "dose")
    assert dose.value == "500 mg" and dose.surface == "500 Milligramm"
    text = "أتناول ٥٠٠ ملغ من الميتفورمين مرتين يوميًا."
    for f in extract(text, "ar", glossary=MED):  # spans point into the original (diacritics and all)
        assert text[f.span[0]:f.span[1]].strip() == f.surface
    assert extract("", "de") == [] and extract("Hallo", "xx") == []
    assert values("Take 500 mg on 12.11.2026 at 9:00.", "pl") == ["500 mg", "2026-11-12", "09:00"]


def test_summary_tests_sentences_are_green():
    pairs = [
        ("en", "I take 500 mg metformin twice a day, and I'm allergic to penicillin.",
         "Ich nehme zweimal täglich 500 mg Metformin, und ich bin allergisch gegen Penicillin."),
        ("de", "Kommen Sie am Donnerstag, dem 12. November, um 9 Uhr wieder.",
         "Come back on Thursday, 12 November, at 9 o'clock."),
        ("en", "Can I still drink coffee?", "Darf ich noch Kaffee trinken?"),
        ("de", "Einverstanden, wir vereinbaren 1,05 Millionen bei Lieferung bis März.",
         "Agreed, we settle on 1.05 million with delivery by March."),
        ("en", "Can you deliver by March?", "Können Sie bis März liefern?"),
    ]
    for sl, a, b in pairs:
        tl = "de" if sl == "en" else "en"
        r = check(a, sl, b, tl, glossary=MED)
        assert not r.red, (a, bad_rows(r))


# ---------------------------------------------------------------- timing


def test_median_under_5_ms():
    src, tgt = DEMO["sprechzimmer"][2][0], DEMO["sprechzimmer"][2][1]["ar"]
    check(src, "de", tgt, "ar", glossary=MED)  # warm the per-language tables
    runs = []
    for _ in range(100):
        t0 = time.perf_counter()
        r = check(src, "de", tgt, "ar", kinds=SZ.guard.kinds, confirm_on=SZ.guard.confirm_on, glossary=MED)
        runs.append((time.perf_counter() - t0) * 1000)
    assert not r.red
    assert statistics.median(runs) < 5.0, statistics.median(runs)
    long = " ".join(de for _, de, _, _ in DEMO_CASES[::4])
    runs = []
    for _ in range(20):
        t0 = time.perf_counter()
        check(long, "de", long, "de", glossary=MED)
        runs.append((time.perf_counter() - t0) * 1000)
    assert statistics.median(runs) < 20.0


# ---------------------------------------------------------------- eval sets (plan §12.1), when present

SETS = sorted((repo_root() / "eval" / "sets").glob("*.jsonl"))


def _eval_items() -> list[dict]:
    out = []
    for p in SETS:
        for i, line in enumerate(Path(p).read_text(encoding="utf-8").splitlines(), 1):
            if line.strip():
                out.append({"_where": f"{p.name}:{i}", **json.loads(line)})
    return out


def _run_item(it: dict, tgt: str) -> GuardResult:
    mode = get_mode(it["room"])
    return check(it["src"], it["src_lang"], tgt, it["tgt_lang"], kinds=mode.guard.kinds,
                 confirm_on=mode.guard.confirm_on, glossary=Glossary.load(mode.glossary))


@pytest.mark.skipif(not SETS, reason="eval/sets/*.jsonl not in this tree yet")
def test_eval_sets():
    items = _eval_items()
    assert items
    for it in items:
        if it.get("ref"):
            r = _run_item(it, it["ref"])
            assert not r.red, (it["_where"], bad_rows(r))
        pert = it.get("perturbed") or {}
        if pert.get("tgt_text") and pert.get("expect", "red") == "red":
            assert _run_item(it, pert["tgt_text"]).red, it["_where"]
