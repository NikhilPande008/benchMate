"""System prompt for the realtime voice model.

The prompt is fixed for the whole session (CLAUDE.md rule 7). Changing state goes in as short
engine messages, never as an instructions update.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).parent / "prompts"

# name -> file. v0: Phase 1 baseline. v1: tight verbosity + exact short model preamble ("A").
# v1_local: tight verbosity, model silent before tool results; the browser plays a clip ("B").
# v2 / v2_local: as v1, plus latest-turn language rule and one-sentence replies (4 Nov results).
# v3 / v3_local: Phase 2. All 8 tools and the repair flow, Hinglish for mixed turns, values and
# power steps only from tool results, "[engine]" messages. v3 and v3_local differ only in Preambles.
# v4_local: v3_local with a session language from the engine instead of the latest-turn rule, and
# mostly Hinglish examples (Phase 2c: v3's Hindi reply rate was 28% in Hinglish sessions).
PROMPTS = {
    "v0": "realtime_v0.md",
    "v1": "realtime_v1.md",
    "v1_local": "realtime_v1_local.md",
    "v2": "realtime_v2.md",
    "v2_local": "realtime_v2_local.md",
    "v3": "realtime_v3.md",
    "v3_local": "realtime_v3_local.md",
    "v4_local": "realtime_v4_local.md",
}
LOCAL_PREAMBLE_PROMPTS = {"v1_local", "v2_local", "v3_local", "v4_local"}
CURRENT_PROMPT = PROMPTS["v3_local"]

# Trade terms passed to the transcription model as keywords (screen/log transcripts only).
TRANSCRIPTION_KEYWORDS = [
    "diode mode", "boot current", "VBAT", "current limit", "short", "supply", "multimeter",
    "boot cable", "OL", "capacitor", "ruko", "aage", "wapas",
]


@dataclass(frozen=True)
class Prompt:
    text: str
    version: str


@lru_cache(maxsize=16)
def load_prompt(name: str = CURRENT_PROMPT) -> Prompt:
    text = (PROMPT_DIR / name).read_text(encoding="utf-8").strip()
    digest = hashlib.sha256(text.encode()).hexdigest()[:8]
    return Prompt(text=text, version=f"{Path(name).stem}+{digest}")
