"""Spoken meter readings in English, Hindi (Devanagari and romanised) and mixed speech.

    parse("zero point teen paanch volt")    -> 0.35 V
    parse("शून्य दशमलव तीन पाँच")              -> 0.35
    parse("teen sau milliamp")              -> 0.3 A (300 mA)
    parse("point zero zero two")            -> 0.002
    parse("OL dikha raha hai")              -> open

Deterministic word rules; the first number in the sentence is the reading. Spoken numbers are
never trusted on their own: the engine reads them back and waits for a confirmation before they
reach the ledger (ARCHITECTURE.md §5.5).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DEV_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

ZERO = {"zero", "shunya", "shoonya", "sunya", "shunye", "शून्य", "ज़ीरो", "जीरो"}
UNITS = {
    # English
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
    # romanised Hindi
    "ek": 1, "do": 2, "teen": 3, "tin": 3, "char": 4, "chaar": 4, "paanch": 5, "panch": 5, "paach": 5,
    "chhe": 6, "che": 6, "chhah": 6, "chah": 6, "chheh": 6, "saat": 7, "sat": 7, "aath": 8, "ath": 8, "nau": 9,
    "das": 10, "dus": 10, "gyarah": 11, "gyara": 11, "barah": 12, "bara": 12, "terah": 13, "tera": 13,
    "chaudah": 14, "chauda": 14, "pandrah": 15, "pandra": 15, "solah": 16, "sola": 16, "satrah": 17,
    "satra": 17, "atharah": 18, "athara": 18, "unnis": 19, "unnees": 19,
    # Devanagari
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पाँच": 5, "पांच": 5, "छह": 6, "छः": 6, "छे": 6, "सात": 7, "आठ": 8,
    "नौ": 9, "दस": 10, "ग्यारह": 11, "बारह": 12, "तेरह": 13, "चौदह": 14, "पंद्रह": 15, "सोलह": 16, "सत्रह": 17,
    "अठारह": 18, "उन्नीस": 19,
}
TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
    "bees": 20, "bis": 20, "tees": 30, "chalis": 40, "chaalis": 40, "pachas": 50, "pachaas": 50, "saath": 60,
    "sattar": 70, "assi": 80, "nabbe": 90,
    "बीस": 20, "तीस": 30, "चालीस": 40, "पचास": 50, "साठ": 60, "सत्तर": 70, "अस्सी": 80, "नब्बे": 90,
}
OTHER_NUMBERS = {"pachchees": 25, "pachees": 25, "पच्चीस": 25, "paintees": 35, "पैंतीस": 35, "pachattar": 75, "पचहत्तर": 75}
HUNDRED = {"hundred", "sau", "सौ"}
THOUSAND = {"thousand", "hazaar", "hazar", "हज़ार", "हजार"}
POINT = {"point", "dot", "decimal", "dashamlav", "dashmalav", "दशमलव", "पॉइंट", "प्वाइंट", "पाइंट", "पॉईंट"}
FRACTIONS = {"derh": 1.5, "dedh": 1.5, "डेढ़": 1.5, "dhai": 2.5, "dhaai": 2.5, "ढाई": 2.5}
SAADHE = {"saadhe", "sadhe", "साढ़े", "साढे"}  # saadhe teen = 3.5
OH = {"oh", "o"}  # zero, only after "point"
OPEN = {"ol", "open", "overload", "infinity", "ओएल", "ओपन"}

# unit word -> (unit as said, multiplier to the base unit, base unit)
UNIT_WORDS = {
    "v": ("V", 1, "V"), "volt": ("V", 1, "V"), "volts": ("V", 1, "V"), "वोल्ट": ("V", 1, "V"),
    "mv": ("mV", 1e-3, "V"), "millivolt": ("mV", 1e-3, "V"), "millivolts": ("mV", 1e-3, "V"), "मिलीवोल्ट": ("mV", 1e-3, "V"),
    "a": ("A", 1, "A"), "amp": ("A", 1, "A"), "amps": ("A", 1, "A"), "ampere": ("A", 1, "A"), "amperes": ("A", 1, "A"),
    "एम्प": ("A", 1, "A"), "एम्पियर": ("A", 1, "A"), "ऐम्प": ("A", 1, "A"),
    "ma": ("mA", 1e-3, "A"), "milliamp": ("mA", 1e-3, "A"), "milliamps": ("mA", 1e-3, "A"), "milliampere": ("mA", 1e-3, "A"),
    "मिलीएम्प": ("mA", 1e-3, "A"), "मिलीएम्पियर": ("mA", 1e-3, "A"),
    "ohm": ("ohm", 1, "ohm"), "ohms": ("ohm", 1, "ohm"), "ओम": ("ohm", 1, "ohm"),
    "kohm": ("kohm", 1e3, "ohm"), "kiloohm": ("kohm", 1e3, "ohm"),
}
MODE_WORDS = {
    "diode": "diode", "डायोड": "diode",
    "continuity": "ohm", "resistance": "ohm", "ohm": "ohm", "ohms": "ohm", "ओम": "ohm",
    "dcv": "dcv", "voltage": "dcv",
    "current": "current", "करंट": "current", "amp": "current", "amps": "current", "milliamp": "current", "ma": "current",
}
# phrases rewritten before tokenising (order matters)
PHRASES = [
    (r"\bo\s*\.?\s*l\b", " ol "), (r"\boh el\b", " ol "), (r"\bover\s*load\b", " ol "), (r"ओ\s*एल", " ol "),
    (r"\bmilli\s+volts?\b", " millivolt "), (r"\bmilli\s+amps?\b", " milliamp "), (r"\bmilli\s+amperes?\b", " milliamp "),
    (r"मिली\s+वोल्ट", " मिलीवोल्ट "), (r"मिली\s+एम्प", " मिलीएम्प "),
    (r"\bkilo\s+ohms?\b", " kohm "), (r"\bk\s+ohms?\b", " kohm "), (r"(\d)\s*k\b", r"\1 kohm"), (r"किलो\s+ओम", " kohm "),
    (r"\bd\s*c\s+volts?\b", " dcv "), (r"\bdc\s+voltage\b", " dcv "),
    (r"(\d)([a-zऀ-ॿ])", r"\1 \2"),  # "0.35v" -> "0.35 v"
]


@dataclass(frozen=True)
class SpokenReading:
    value: float | None  # in the base unit (V, A, ohm)
    said: str | None  # the number as said, digits kept ("4.0", "0.002", "300")
    unit: str | None  # unit as said: V, mV, A, mA, ohm, kohm
    base_unit: str | None  # V, A, ohm
    mode: str | None  # diode, dcv, ohm, current (when said)
    open: bool

    @property
    def ok(self) -> bool:
        return self.open or self.value is not None


ONES_EN = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
UNIT_NAMES = {"V": "volts", "mV": "millivolts", "A": "amps", "mA": "milliamps", "ohm": "ohms", "kohm": "kilo-ohms"}


def _int_words(n: int) -> str:
    teens = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
    tens = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
    if n < 10:
        return ONES_EN[n]
    if n < 20:
        return teens[n - 10]
    if n < 100:
        return tens[n // 10] + ("" if n % 10 == 0 else " " + ONES_EN[n % 10])
    if n < 1000:
        rest = n % 100
        return ONES_EN[n // 100] + " hundred" + ("" if rest == 0 else " " + _int_words(rest))
    rest = n % 1000
    return _int_words(n // 1000) + " thousand" + ("" if rest == 0 else " " + _int_words(rest))


def readback(r: SpokenReading) -> str:
    """The reading in words, digit by digit after the point, as the agent reads it back.
    Technicians use English numbers and units in Hindi sentences too, so one form serves both."""
    if r.open:
        return "OL, open"
    assert r.said is not None
    whole, _, frac = r.said.partition(".")
    words = _int_words(int(whole or "0"))
    if frac:
        words += " point " + " ".join(ONES_EN[int(d)] for d in frac)
    if r.unit:
        words += " " + UNIT_NAMES[r.unit]
    return words


def _normalise(text: str) -> list[str]:
    t = text.lower().translate(DEV_DIGITS)
    t = re.sub(r"[,;:!?।\"'()\[\]]", " ", t)
    t = re.sub(r"\.(?!\d)", " ", t)  # sentence full stops, but keep "0.35"
    t = re.sub(r"(?<!\d)\.(?=\d)", " point ", t)  # ".35" -> "point 35"
    for pat, rep in PHRASES:
        t = re.sub(pat, rep, t)
    return t.split()


def _number_value(tok: str) -> int | None:
    if tok in ZERO:
        return 0
    for table in (UNITS, TENS, OTHER_NUMBERS):
        if tok in table:
            return table[tok]
    return None


def _parse_number(tokens: list[str]) -> tuple[str | None, int]:
    """(number as a digit string, index after it). The first number in the token list wins."""
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        starts = (
            re.fullmatch(r"\d+(\.\d+)?", tok)
            or tok in POINT
            or tok in FRACTIONS
            or tok in SAADHE
            or _number_value(tok) is not None
        )
        if starts:
            break
        i += 1
    if i == n:
        return None, n

    # Digits typed as numerals: "0.35", "300"
    if re.fullmatch(r"\d+(\.\d+)?", tokens[i]):
        return tokens[i], i + 1

    # Hindi fractions
    if tokens[i] in FRACTIONS:
        return f"{FRACTIONS[tokens[i]]:g}", i + 1
    if tokens[i] in SAADHE and i + 1 < n and _number_value(tokens[i + 1]) is not None:
        return f"{_number_value(tokens[i + 1]) + 0.5:g}", i + 2

    total, current, seen_int = 0, 0, False
    while i < n:
        tok = tokens[i]
        v = _number_value(tok)
        if tok in HUNDRED:
            current = (current or 1) * 100
            seen_int = True
        elif tok in THOUSAND:
            total += (current or 1) * 1000
            current = 0
            seen_int = True
        elif v is not None:
            if v >= 10 and 1 <= current <= 9 and not total and i > 0 and tokens[i - 1] not in HUNDRED:
                current = current * 100 + v  # "two fifty" -> 250
            elif v < 10 and current % 10 == 0 and current % 100 >= 20:
                current += v  # "thirty five"
            elif v < 100 and current % 100 == 0 and current >= 100:
                current += v  # "three hundred twenty"
            elif seen_int and current and current < 10 and v < 10:
                break  # "four five" is not one number
            else:
                current = v if not seen_int else current + v
            seen_int = True
        elif tok == "and" and seen_int:
            pass
        else:
            break
        i += 1
    whole = total + current
    digits = str(whole) if seen_int else "0"

    if i < n and tokens[i] in POINT:
        i += 1
        frac, pending_tens = "", None
        while i < n:
            tok = tokens[i]
            if re.fullmatch(r"\d+", tok):
                frac += tok
            elif tok in OH:
                frac += "0"
            else:
                v = _number_value(tok)
                if v is None:
                    break
                if v in (10, 20, 30, 40, 50, 60, 70, 80, 90) and tok in TENS:
                    if pending_tens is not None:
                        frac += str(pending_tens)
                    pending_tens = v
                    i += 1
                    continue
                if pending_tens is not None and v < 10:
                    frac += str(pending_tens + v)
                    pending_tens = None
                else:
                    if pending_tens is not None:
                        frac += str(pending_tens)
                        pending_tens = None
                    frac += str(v)
            i += 1
        if pending_tens is not None:
            frac += str(pending_tens)
        if frac:
            return f"{digits}.{frac}", i
    if not seen_int:
        return None, i
    return digits, i


def parse(text: str) -> SpokenReading:
    tokens = _normalise(text)
    is_open = any(t in OPEN for t in tokens)
    mode = next((MODE_WORDS[t] for t in tokens if t in MODE_WORDS and t not in UNIT_WORDS), None)
    said, end = (None, 0) if is_open else _parse_number(tokens)
    unit = scale = base = None
    if said is not None:
        # The unit follows the number: within three words, and a one-letter unit ("v", "a") only
        # directly after it, so "a reading of point three" is not "point three amps".
        for k, tok in enumerate(tokens[end : end + 3]):
            if tok in UNIT_WORDS and (len(tok) > 1 or k == 0):
                unit, scale, base = UNIT_WORDS[tok]
                break
    if mode is None and base == "A":
        mode = "current"
    if mode is None and base == "ohm":
        mode = "ohm"
    value = None
    if said is not None:
        value = round(float(said) * (scale or 1), 9)
    return SpokenReading(value=value, said=said, unit=unit, base_unit=base, mode=mode, open=is_open)
