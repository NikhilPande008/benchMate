"""Rough language tag for a transcript: "hi" (Devanagari), "hinglish" (Hindi in Latin script),
"en", or "unknown". Good enough to count whether a Hindi question got a Hindi reply; not a
language identifier."""
from __future__ import annotations

import re

# Common Hindi words in Latin script. English homographs (to, do, main, me, ab) are left out.
HINDI_LATIN = {
    "hai", "hain", "kya", "ka", "ki", "ke", "ko", "se", "mein", "nahi", "nahin", "haan", "theek", "thik",
    "kitna", "kitni", "dikha", "raha", "rahi", "rahe", "padho", "padh", "padhte", "batao", "bataiye",
    "samjhao", "matlab", "hota", "hoti", "aur", "ek", "ruko", "aage", "wapas", "abhi", "laga", "lagao",
    "diya", "gaya", "karo", "kijiye", "chalo", "wala", "wali", "yeh", "ye", "woh", "toh", "bhi", "dekho",
    "dekhiye", "ji", "aap", "tum", "hum", "kaise", "kahan", "kyun", "farak", "fark", "accha", "acha",
    "shukriya", "dhanyavaad", "lijiye", "hoga", "chahiye", "sakta", "sakte", "dikhata", "dikhati", "lagta",
    "pe", "par", "liye", "jab", "tak", "sirf",
}
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_LATIN_WORD = re.compile(r"[a-z']+")


def lang_tag(text: str) -> str:
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "unknown"
    if sum(1 for c in letters if _DEVANAGARI.match(c)) / len(letters) >= 0.3:
        return "hi"
    words = _LATIN_WORD.findall(text.lower())
    hindi = sum(1 for w in words if w in HINDI_LATIN)
    if hindi >= 1 and hindi / max(len(words), 1) >= 0.2:
        return "hinglish"
    return "en"
