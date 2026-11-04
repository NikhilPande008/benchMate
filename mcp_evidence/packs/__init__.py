"""Pack loading, model lookup and reading classification."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .schema import BOOT_PATTERNS, POWER_STEPS, SCHEMA_VERSION, Band, Measurement, Pack, Point

PACK_DIR = Path(__file__).resolve().parents[2] / "packs"
GENERAL_PACK = "general_dead_phone"

__all__ = [
    "BOOT_PATTERNS",
    "GENERAL_PACK",
    "PACK_DIR",
    "POWER_STEPS",
    "SCHEMA_VERSION",
    "Pack",
    "all_packs",
    "classify_band",
    "load_pack",
    "model_lookup",
]


def _pack_files() -> dict[str, Path]:
    files = {GENERAL_PACK: PACK_DIR / f"{GENERAL_PACK}.json"}
    for p in sorted(PACK_DIR.glob("*/pack.json")):
        files[p.parent.name] = p
    return files


@lru_cache(maxsize=16)
def load_pack(pack_id: str) -> Pack:
    path = _pack_files().get(pack_id)
    if path is None:
        raise KeyError(f"no pack {pack_id!r}")
    return Pack.model_validate(json.loads(path.read_text(encoding="utf-8")))


def all_packs() -> list[Pack]:
    return [load_pack(pid) for pid in _pack_files()]


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9ऀ-ॿ]+", " ", text.lower()).strip()


def model_lookup(query: str | None) -> tuple[Pack, bool]:
    """(pack, matched). An unknown model gets the general dead-phone flow with matched=False."""
    q = f" {_norm(query or '')} "
    for pack in all_packs():
        if pack.general:
            continue
        names = [pack.pack_id.replace("_", " "), pack.name.en, *pack.aliases]
        if any(f" {_norm(n)} " in q for n in names if _norm(n)):
            return pack, True
    return load_pack(GENERAL_PACK), False


def classify_band(bands: list[Band], value: float | None, is_open: bool) -> str | None:
    """Outcome class of a reading. OL matches the `ol` band; otherwise value in [lo, hi)."""
    if is_open:
        return next((b.cls for b in bands if b.ol), None)
    if value is None:
        return None
    for b in bands:
        if (b.lo is None or value >= b.lo) and (b.hi is None or value < b.hi):
            return b.cls
    return None


def classify_point(point: Point, value: float | None, is_open: bool) -> str | None:
    return classify_band(point.reference.bands, value, is_open)


KEYWORD_GAP = 2  # words allowed between a phrase's words: "nothing is getting hot" ~ "nothing hot"


def _phrase_in(phrase: list[str], words: list[str], gap: int = KEYWORD_GAP) -> bool:
    """The phrase's words in order, each within `gap` words of the one before."""
    for start in (i for i, w in enumerate(words) if w == phrase[0]):
        pos, ok = start, True
        for w in phrase[1:]:
            nxt = next((j for j in range(pos + 1, min(pos + 2 + gap, len(words))) if words[j] == w), None)
            if nxt is None:
                ok = False
                break
            pos = nxt
        if ok:
            return True
    return False


def classify_keywords(measurement: Measurement, text: str) -> str | None:
    """Observation classes from spoken words. Classes are tried in pack order, so list negations
    ("kuch garam nahi") before the plain words they contain. A phrase may have up to two words
    between its words, as people say it ("the capacitor is getting hot")."""
    words = _norm(text).split()
    for cls in measurement.classes:
        for kw in (measurement.keywords or {}).get(cls, []):
            if _phrase_in(_norm(kw).split(), words):
                return cls
    return None
