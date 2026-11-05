"""Session language: the reply language for the whole session (CLAUDE.md "Settings that are already
decided"), sent to the voice model as an engine message, and the language of the preamble clip.

Two values: "hi" (Hindi or Hinglish; replies in Hinglish) and "en". The rule:
- Only clear turns count: Devanagari, or at least 3 words with a language. "ok", "haan" and
  "yes" alone say nothing about the language.
- The first clear turn sets the language provisionally; the second decides it. If the two differ,
  the session is Hinglish, which covers mixed speech.
- After that it changes only by the screen toggle, or after two consecutive clear turns in the other
  language (one English sentence in a Hindi session doesn't flip it).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from engine.telemetry.lang import lang_tag

MIN_CLEAR_WORDS = 3
_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
_WORDS = re.compile(r"[a-z]+|[ऀ-ॣ०-ॿ]+")


def group(tag: str | None) -> str | None:
    if tag in ("hi", "hinglish"):
        return "hi"
    if tag == "en":
        return "en"
    return None


def clear_language(text: str, tag: str | None = None) -> str | None:
    """'hi' or 'en' if this turn clearly shows a language, else None."""
    g = group(tag if tag is not None else lang_tag(text))
    if g is None:
        return None
    if g == "hi" and _DEVANAGARI.search(text):
        return "hi"
    return g if len(_WORDS.findall(text.lower())) >= MIN_CLEAR_WORDS else None


@dataclass
class SessionLanguage:
    main: str | None = None
    switch_after: int = 2
    _streak: int = 0
    seen: int = 0  # clear turns before the language was decided (decided at 2)
    source: str | None = None  # "turns" | "toggle"

    @property
    def decided(self) -> bool:
        return self.seen >= 2

    def observe(self, g: str | None) -> bool:
        """Feed one turn's clear language ('hi', 'en' or None). True if the session language changed."""
        if g is None:
            return False
        before = self.main
        if self.main is None:
            self.main, self.seen, self.source = g, 1, "turns"
        elif not self.decided:
            self.seen = 2
            if g != self.main:
                self.main = "hi"  # mixed first turns: Hinglish
        elif g == self.main:
            self._streak = 0
        else:
            self._streak += 1
            if self._streak >= self.switch_after:
                self.main, self._streak, self.source = g, 0, "turns"
        return self.main != before

    def toggle(self, lang: str) -> bool:
        """The screen toggle: sets and decides the language. True if it changed."""
        if lang not in ("hi", "en"):
            raise ValueError(lang)
        before = self.main
        self.main, self.seen, self._streak, self.source = lang, 2, 0, "toggle"
        return self.main != before

    @property
    def lang(self) -> str:
        return self.main or "en"


NOTE = {
    "hi": "Session language: Hinglish. Reply in Hinglish (Hindi in Latin script) from now on, whatever language a single turn is in; numbers and trade terms stay in English.",
    "en": "Session language: English. Reply in English from now on, whatever language a single turn is in; numbers and trade terms stay in English.",
}
