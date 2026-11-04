"""Session language for the local preamble clip.

Set by the first user turn that has a language; changed only after two consecutive turns in the
other language, so one English word in a Hindi session doesn't flip the clip.
Hindi and Hinglish count as one language here ("hi").
"""
from __future__ import annotations

from dataclasses import dataclass


def group(tag: str | None) -> str | None:
    if tag in ("hi", "hinglish"):
        return "hi"
    if tag == "en":
        return "en"
    return None


@dataclass
class SessionLanguage:
    main: str | None = None
    switch_after: int = 2
    _streak: int = 0

    def observe(self, tag: str | None) -> bool:
        """Feed one user turn's tag. True if the main language changed."""
        g = group(tag)
        if g is None:
            return False
        if self.main is None:
            self.main = g
            return True
        if g == self.main:
            self._streak = 0
            return False
        self._streak += 1
        if self._streak >= self.switch_after:
            self.main, self._streak = g, 0
            return True
        return False

    @property
    def lang(self) -> str:
        return self.main or "en"
