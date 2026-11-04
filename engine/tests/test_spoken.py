from __future__ import annotations

import pytest

from mcp_evidence.spoken.parser import parse, readback

# (phrase, value in base unit or "OL", unit as said, mode when said)
PHRASES = [
    # English
    ("point zero zero two", 0.002, None, None),
    ("point zero zero two volts", 0.002, "V", None),
    ("zero point three five", 0.35, None, None),
    ("zero point three five volts", 0.35, "V", None),
    ("point three five", 0.35, None, None),
    ("point thirty five", 0.35, None, None),
    ("point oh oh two", 0.002, None, None),
    ("four point zero volts", 4.0, "V", None),
    ("four volts", 4.0, "V", None),
    ("one amp", 1.0, "A", "current"),
    ("point eight amps", 0.8, "A", "current"),
    ("three hundred milliamps", 0.3, "mA", "current"),
    ("two fifty milliamp", 0.25, "mA", "current"),
    ("one hundred and twenty milliamps", 0.12, "mA", "current"),
    ("seventy millivolts", 0.07, "mV", None),
    ("it reads zero point four five in diode mode", 0.45, None, "diode"),
    ("diode mode, point five two", 0.52, None, "diode"),
    ("zero point two ohms", 0.2, "ohm", "ohm"),
    ("one point five k ohm", 1500.0, "kohm", "ohm"),
    ("OL", "OL", None, None),
    ("it shows O L", "OL", None, None),
    ("open", "OL", None, None),
    ("overload on the meter", "OL", None, None),
    ("0.35", 0.35, None, None),
    ("0.35 V", 0.35, "V", None),
    ("0.35v", 0.35, "V", None),
    (".002", 0.002, None, None),
    ("4.0 volts", 4.0, "V", None),
    ("300 mA", 0.3, "mA", "current"),
    # romanised Hindi / Hinglish
    ("zero point teen paanch", 0.35, None, None),
    ("shunya point teen paanch volt", 0.35, "V", None),
    ("point shunya shunya do", 0.002, None, None),
    ("teen sau milliamp", 0.3, "mA", "current"),
    ("do sau milli amp", 0.2, "mA", "current"),
    ("chaar volt", 4.0, "V", None),
    ("ek amp", 1.0, "A", "current"),
    ("do point chaar volt", 2.4, "V", None),
    ("saadhe teen volt", 3.5, "V", None),
    ("derh amp", 1.5, "A", "current"),
    ("dhai volt", 2.5, "V", None),
    ("bees milliamp", 0.02, "mA", "current"),
    ("meter pe point zero zero do aa raha hai", 0.002, None, None),
    ("reading zero point chaar paanch hai diode mode mein", 0.45, None, "diode"),
    ("OL dikha raha hai", "OL", None, None),
    ("open aa raha hai", "OL", None, None),
    ("current point aath amp hai", 0.8, "A", "current"),
    ("ek hazaar ohm", 1000.0, "ohm", "ohm"),
    # Devanagari
    ("शून्य दशमलव तीन पाँच", 0.35, None, None),
    ("शून्य दशमलव तीन पाँच वोल्ट", 0.35, "V", None),
    ("दशमलव शून्य शून्य दो", 0.002, None, None),
    ("तीन सौ मिली एम्प", 0.3, "mA", "current"),
    ("चार वोल्ट", 4.0, "V", None),
    ("ओएल", "OL", None, None),
    ("डायोड मोड में शून्य दशमलव चार", 0.4, None, "diode"),
    ("०.३५ वोल्ट", 0.35, "V", None),
    ("ज़ीरो पॉइंट तीन पाँच", 0.35, None, None),
    ("पॉइंट आठ एम्प", 0.8, "A", "current"),
]


@pytest.mark.parametrize("phrase,value,unit,mode", PHRASES, ids=[p[0] for p in PHRASES])
def test_spoken_reading(phrase, value, unit, mode):
    r = parse(phrase)
    assert r.ok
    if value == "OL":
        assert r.open and r.value is None
    else:
        assert not r.open
        assert r.value == pytest.approx(value), r
        assert r.unit == unit
    assert r.mode == mode


def test_at_least_forty_phrases():
    assert len(PHRASES) >= 40


@pytest.mark.parametrize("phrase", ["haan", "theek hai", "probe laga diya", "kuch nahi dikh raha", ""])
def test_no_number_is_not_a_reading(phrase):
    r = parse(phrase)
    assert not r.ok and r.value is None and not r.open


def test_a_short_word_is_not_an_amp_unit():
    r = parse("there is a reading of point three")
    assert r.value == pytest.approx(0.3) and r.unit is None


def test_digits_are_kept_as_said():
    assert parse("four point zero volts").said == "4.0"
    assert parse("point zero zero two").said == "0.002"


@pytest.mark.parametrize(
    "phrase,words",
    [
        ("point zero zero two", "zero point zero zero two"),
        ("zero point teen paanch volt", "zero point three five volts"),
        ("teen sau milliamp", "three hundred milliamps"),
        ("four point zero volts", "four point zero volts"),
        ("OL", "OL, open"),
    ],
)
def test_readback(phrase, words):
    assert readback(parse(phrase)) == words
