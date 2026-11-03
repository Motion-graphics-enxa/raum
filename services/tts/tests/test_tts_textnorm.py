"""textnorm: numbers and units are spelled out before TTS. The voice read "500 mg" letter by letter (the echo check
heard "500-male gay"). Needs num2words (in the service venv):
``cd core && uv run --with num2words==0.5.14 pytest ../services/tts/tests/test_tts_textnorm.py``."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("num2words")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from textnorm import normalize_text


@pytest.mark.parametrize("lang,text,want", [
    ("en", "Please take 500 mg Metformin twice a day.", "Please take five hundred milligrams Metformin twice a day."),
    ("en", "1 mg at night", "one milligram at night"),
    ("en", "0.5 ml", "zero point five milliliters"),
    ("en", "5 mg/kg", "five milligrams per kilogram"),
    ("en", "38 °C", "thirty-eight degrees Celsius"),
    ("en", "500mg", "five hundred milligrams"),
    ("de", "500 mg Metformin", "fünfhundert Milligramm Metformin"),
    ("de", "1 mg", "ein Milligramm"),
    ("de", "10 IE Insulin", "zehn internationale Einheiten Insulin"),
    ("ru", "по 500 мг", "по пятьсот миллиграммов"),
    ("ru", "2 мг", "два миллиграмма"),
    ("ru", "21 мл", "двадцать один миллилитр"),
    ("ru", "1,5 мг", "одна целая пять десятых миллиграмма"),
    ("ru", "2 ЕД инсулина", "две единицы инсулина"),
    ("ru", "5,5 ммоль/л", "пять целых пять десятых миллимоля на литр"),
    ("ru", "давление 120 мм рт. ст.", "давление сто двадцать миллиметров ртутного столба."),
])
def test_units_spelled_out(lang, text, want):
    assert normalize_text(text, lang) == want


@pytest.mark.parametrize("lang,text", [
    ("en", "We have 5G here."),          # not grams: symbols are case sensitive
    ("en", "Room 12b"),                  # no unit
])
def test_no_false_units(lang, text):
    assert "gram" not in normalize_text(text, lang)


def test_russian_year_is_not_grams():
    assert "грамм" not in normalize_text("в 2024 г. было", "ru")


def test_numbers_unchanged_behaviour():
    assert normalize_text("Take 500 milligrams twice a day, 1.5 tablets.", "en") == \
        "Take five hundred milligrams twice a day, one point five tablets."
