"""Seven-segment glyphs: which segments (a top, b top-right, c bottom-right, d bottom, e bottom-left,
f top-left, g middle) each character lights. General display knowledge, shared by Reader B and
the synthetic generator; real displays draw 6, 7 and 9 in two ways, so both are listed."""
from __future__ import annotations

SEGMENTS = "abcdefg"

GLYPHS: dict[str, list[str]] = {
    "0": ["abcdef"],
    "1": ["bc"],
    "2": ["abdeg"],
    "3": ["abcdg"],
    "4": ["bcfg"],
    "5": ["acdfg"],
    "6": ["acdefg", "cdefg"],
    "7": ["abc", "abcf"],
    "8": ["abcdefg"],
    "9": ["abcdfg", "abcfg"],
    "-": ["g"],
    "O": ["abcdef"],
    "L": ["def"],
    "E": ["adefg"],
    "r": ["eg"],
    " ": [""],
}

# Letters that error and status screens use ("Err", "OPEn", "bAt", "HI"). A reading that decodes
# to one of these is not a number; listing them keeps a damaged digit from being forced into a
# digit it doesn't look like.
LETTERS: dict[str, str] = {
    "A": "abcefg", "C": "adef", "c": "deg", "d": "bcdeg", "F": "aefg", "H": "bcefg", "h": "cefg",
    "J": "bcd", "n": "ceg", "o": "cdeg", "P": "abefg", "t": "defg", "U": "bcdef", "u": "cde", "y": "bcdfg",
}

# segment pattern → character ("O" reads as "0"; the reader decides OL from context)
PATTERNS: dict[frozenset[str], str] = {}
for _ch, _styles in GLYPHS.items():
    for _s in _styles:
        PATTERNS.setdefault(frozenset(_s), "0" if _ch == "O" else _ch)
for _ch, _s in LETTERS.items():
    PATTERNS.setdefault(frozenset(_s), _ch)
