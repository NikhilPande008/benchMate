"""Scope checks on what the user says (ARCHITECTURE.md §6.5, PRODUCT.md §12).

Deterministic keyword rules, run on every user transcript and on start_repair's arguments:
- a swollen, punctured, hot or smoking battery (or smoke/fire anywhere) stops the session;
- "battery connected" / "battery removed" sets the battery state the power gate needs;
- a device that is not a phone board (charger, adapter, TV, mains) is refused.
"""
from __future__ import annotations

import re
from typing import Literal

DANGER_ANYWHERE = ["smoke", "smoking", "dhuan", "dhuaan", "dhua", "fire", "aag lag", "sparking", "धुआं", "धुआँ", "धुंआ", "आग"]
BATTERY_WORDS = ["battery", "batt", "बैटरी", "बैट्री", "बेटरी"]
BATTERY_DANGER = [
    "swollen", "swelling", "bulging", "puffed", "puncture", "punctured", "hot", "garam", "phool", "phoola", "phooli",
    "phul", "phula", "phuli", "fula", "fuli", "fooli", "leak", "leaking", "फूल", "फूली", "गरम", "गर्म",
]
NEGATIONS = ["nahi", "nahin", "not", "no", "without", "bina", "नहीं", "नही", "बिना"]
REMOVED = ["removed", "nikal", "nikaal", "nikali", "nikaali", "hata", "hatai", "hataya", "disconnected", "out", "bahar",
           "निकाल", "निकल", "हटा", "बाहर"]
CONNECTED = ["connected", "laga", "lagi", "lagaya", "lagai", "attached", "fitted", "inside", "in place", "लगी", "लगा"]
OUT_OF_SCOPE_DEVICES = [
    "charger", "adapter", "adaptor", "tv", "television", "laptop", "mains", "230v", "230 volt", "220v", "220 volt",
    "inverter", "ups", "power bank", "powerbank", "चार्जर", "टीवी",
]

BatteryReport = Literal["danger", "removed", "connected"]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+|[\u0900-\u097f]+", text.lower())


def _devanagari_match(word: str, term: str) -> bool:
    """Whole word, or a stem of 3+ characters at the start of the word (फूली, गरमी, निकाली).
    Never a substring: "आग" (fire) must not match "आगे" (ahead)."""
    return word == term or (len(term) >= 3 and word.startswith(term))


def _hits(words: list[str], terms: list[str]) -> list[int]:
    """Word indices where any term matches (multi-word terms match at their first word)."""
    out = []
    for i, w in enumerate(words):
        for t in terms:
            parts = t.split()
            if words[i : i + len(parts)] == parts or (not t.isascii() and len(parts) == 1 and _devanagari_match(w, t)):
                out.append(i)
                break
    return out


def _negated(words: list[str], i: int, window: int = 2) -> bool:
    return any(w in NEGATIONS for w in words[max(0, i - window) : i + window + 1])


def battery_report(text: str) -> BatteryReport | None:
    """What a user sentence says about the battery, if anything. Danger wins over everything.
    A negation within two words flips or cancels a keyword ("garam nahi", "nahi nikali")."""
    words = _words(text)
    if any(not _negated(words, i) for i in _hits(words, DANGER_ANYWHERE)):
        return "danger"
    if not _hits(words, BATTERY_WORDS):
        return None
    danger = _hits(words, BATTERY_DANGER)
    if any(not _negated(words, i) for i in danger):
        return "danger"
    if danger:  # "battery garam nahi hai": reassurance, says nothing about connection
        return None
    removed = _hits(words, REMOVED)
    if removed:
        return "connected" if _negated(words, removed[0]) else "removed"
    connected = _hits(words, CONNECTED)
    if connected:
        return "removed" if _negated(words, connected[0]) else "connected"
    if any(_negated(words, i, 3) for i in _hits(words, BATTERY_WORDS)):  # "no battery", "battery nahi hai"
        return "removed"
    return None


def device_out_of_scope(text: str | None) -> str | None:
    """The matched word if the device is not a phone board, else None."""
    if not text:
        return None
    words = _words(text)
    for term in OUT_OF_SCOPE_DEVICES:
        if _hits(words, [term]):
            return term
    return None


STOP_MESSAGE = {
    "en": "Stop. A swollen, hot or smoking battery is dangerous. Disconnect everything and move the phone away. I can't help with this one.",
    "hi": "Ruko. Phooli, garam ya dhuan deti battery khatarnak hai. Sab disconnect karo aur phone door rakho. Isme main madad nahi kar sakta.",
}
REFUSE_DEVICE = {
    "en": "I only help with phone boards on a bench supply. Chargers and anything on mains are out of scope.",
    "hi": "Main sirf bench supply pe phone board mein madad karta hoon. Charger aur mains wali cheezein scope ke bahar hain.",
}
