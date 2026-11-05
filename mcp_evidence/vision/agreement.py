"""The two-reader agreement rule and plausibility checks (CLAUDE.md rule 3; BUILD_GUIDE points 3–5).

A field is accepted only when:
- Reader B reads the same digits and decimal point on two frames ~150 ms apart (the display is
  stable: no flicker, no digit mid-update), with enough confidence on both, and one of the two is
  the latest frame (the value is what the display shows now, not 300 ms ago); and
- Reader A (the vision model) reads the same digits and decimal point on a frame of that span.

Three frames 150 ms apart: B on all three, A on frame 1. Frame 2 arrives while A is still
answering, so it costs no time. If no stable pair is confirmed, A again on frame 2. Still no
agreement → low confidence, and the agent asks the user to read the value aloud. Never one frame,
never one reader.

Tightened 5 Nov after the held-out run: two agreeing frames used to suffice even if the third
frame showed a new value (an LED current changing 0.000 → 0.003 was accepted as 0.000).

Then the plausibility rules: supply current ≤ its own limit (else re-read; persisting → a reader
error), the meter value inside its mode's range, the meter mode matching the requested one, units
normalised (mV → V, kΩ → Ω) so auto-ranging never looks like a change, OL kept as "open".

Degraded ("reader_b_only", the vision API down or slow): B must be stable on all three frames,
and the value goes back with confidence ≤ 0.85 and needs_confirmation, so the user's read-back
confirmation is the second reader.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import numpy as np

from .reader_a import ReadA, ReaderA, fields_of
from .reader_b import ReadB, read_field

B_MIN = 0.6  # Reader B confidence on each frame used
A_MIN = 0.5  # Reader A's own confidence
B_ONLY_MAX_CONF = 0.85  # below the safety gate's 0.9: no power step on one reader
AGREED_MIN, AGREED_MAX = 0.9, 0.99

UNIT_SCALE = {"V": 1.0, "mV": 1e-3, "A": 1.0, "mA": 1e-3, "ohm": 1.0, "kohm": 1e3, "Mohm": 1e6, "diode_V": 1.0}
BASE_UNIT = {"V": "V", "mV": "V", "A": "A", "mA": "A", "ohm": "ohm", "kohm": "ohm", "Mohm": "ohm", "diode_V": "diode_V"}
MODE_OF_UNIT = {"diode_V": "diode", "V": "dcv", "mV": "dcv", "ohm": "ohm", "kohm": "ohm", "Mohm": "ohm", "A": "current", "mA": "current"}
PLAUSIBLE = {"diode": (0.0, 3.0), "dcv": (-1000.0, 1000.0), "ohm": (0.0, 6e7), "current": (-10.0, 10.0)}
SUPPLY_UNITS = {"set_voltage": "V", "live_current": "A", "current_limit": "A"}
MODE_WORDS = {"diode": "diode mode", "dcv": "volts", "ohm": "ohms", "current": "amps"}


@dataclass
class FieldResult:
    field: str
    status: str  # agreed | b_only | unclear
    key: str | None = None
    value: float | None = None  # as displayed (display unit)
    decimals: int = 0
    ol: bool = False
    unit: str | None = None  # display unit (mV, kohm, ...)
    si_value: float | None = None  # in the base unit (V, A, ohm)
    base_unit: str | None = None
    confidence: float = 0.0
    needs_confirmation: bool = False
    reason: str | None = None
    b_keys: list[str | None] = field(default_factory=list)
    a_keys: list[str | None] = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status in ("agreed", "b_only")


@dataclass
class DisplayResult:
    display: str
    fields: dict[str, FieldResult]
    status: str  # ok | unclear | implausible | wrong_mode | camera_unavailable
    level: str  # full | reader_b_only
    mode: str | None = None  # meter mode as Reader A saw it
    reason: str | None = None
    say: dict[str, str] | None = None
    frames: list[int] = field(default_factory=list)
    a_calls: int = 0
    a_errors: int = 0
    hedged: bool = False
    ms: float = 0.0
    b_ms: float = 0.0
    a_ms: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def _stable(b: list[ReadB | None], i: int, j: int) -> str | None:
    bi, bj = b[i], b[j]
    if bi is None or bj is None or not bi.legible or not bj.legible:
        return None
    if bi.confidence < B_MIN or bj.confidence < B_MIN:
        return None
    return bi.key if bi.key == bj.key else None


def _a_key(a: ReadA | None) -> str | None:
    return a.key if a is not None and a.confidence >= A_MIN else None


def decide(b: list[ReadB | None], a: list[ReadA | None]) -> tuple[str | None, int | None]:
    """The agreed key and the index of the Reader A read that confirmed it, or (None, None).
    Pairs ending at the latest frame: (L−1, L), then (L−2, L); the value must be what a Reader A
    read on a frame of the pair's span says."""
    last = len(b) - 1
    if last < 1:
        return None, None
    for i in (last - 1, last - 2):
        if i < 0:
            continue
        k = _stable(b, i, last)
        if k is None:
            continue
        for x in range(last, i - 1, -1):
            if x < len(a) and _a_key(a[x]) == k:
                return k, x
    return None, None


def decide_b_only(b: list[ReadB | None]) -> str | None:
    if len(b) < 3:
        return None
    k = _stable(b, 0, 1)
    return k if k is not None and _stable(b, 1, 2) == k else None


def _field_result(f: str, key: str | None, b: list[ReadB | None], a: list[ReadA | None], ai: int | None, display: str,
                  status: str) -> FieldResult:
    bk = [r.key if r else None for r in b]
    ak = [r.key if r else None for r in a]
    if key is None:
        return FieldResult(f, "unclear", b_keys=bk, a_keys=ak, confidence=0.3,
                           reason=next((r.reason for r in b if r and r.reason), None) or "readers_disagree")
    bref = next(r for r in reversed(b) if r is not None and r.key == key)
    aref = a[ai] if ai is not None else None
    unit = SUPPLY_UNITS.get(f) if display == "supply" else (aref.unit if aref and aref.unit not in ("OL", "unknown") else None)
    ol = key == "OL"
    value = None if ol else bref.value
    si = None if (ol or value is None or unit is None) else value * UNIT_SCALE.get(unit, 1.0)
    if status == "agreed":
        conf = float(np.clip(1 - (1 - (aref.confidence if aref else 0.0)) * (1 - bref.confidence), AGREED_MIN, AGREED_MAX))
        needs = False
    else:
        conf = float(min(B_ONLY_MAX_CONF, bref.confidence))
        needs = True
    return FieldResult(f, status, key, value, bref.decimals, ol, unit, si, BASE_UNIT.get(unit or "", unit), conf, needs,
                       b_keys=bk, a_keys=ak)


Crops = Callable[[np.ndarray], dict[str, np.ndarray]]  # frame → {"display": crop, field: crop, ...}


@dataclass
class DisplaySpec:
    display: str  # "supply" | "meter"
    crops: Crops  # display crop for Reader A plus one crop per field for Reader B
    digits: dict[str, int] = field(default_factory=lambda: {})
    polarity: str | None = None


async def read_display(spec: DisplaySpec, next_frame: Callable[[int], Awaitable[np.ndarray | None]], reader_a: ReaderA | None,
                       level: str = "full", expected_mode: str | None = None) -> DisplayResult:
    """Run the agreement rule on frames from `next_frame(i)` (i = 0, 1, 2; the camera's frames
    ~150 ms apart). Reader B runs on a worker thread; Reader A on the display crop."""
    t0 = time.perf_counter()
    names = fields_of(spec.display)
    frames: list[dict[str, np.ndarray]] = []
    b: dict[str, list[ReadB | None]] = {f: [] for f in names}
    a: list[ReadA | None] | dict = {f: [] for f in names}
    res = DisplayResult(spec.display, {}, "unclear", level)
    b_ms = 0.0

    async def frame(i: int) -> bool:
        img = await next_frame(i)
        if img is None:
            return False
        crops = spec.crops(img)
        frames.append(crops)
        return True

    async def read_b(i: int) -> None:
        nonlocal b_ms
        crops = frames[i]
        reads = await asyncio.to_thread(lambda: {f: read_field(crops[f], spec.digits.get(f, 4), spec.polarity) for f in names})
        for f in names:
            b[f].append(reads[f])
            b_ms += reads[f].ms

    async def read_a(i: int) -> None:
        if reader_a is None or level != "full":
            for f in names:
                a[f].append(None)
            return
        res.a_calls += 1
        out = await reader_a.read(spec.display, frames[i]["display"], 0)
        for f in names:
            r = out.get(f)
            a[f].append(r)
            if r is not None and r.error:
                res.a_errors += 1
            if r is not None and r.hedged:
                res.hedged = True
            res.a_ms = max(res.a_ms, r.ms if r else 0.0)

    if not (await frame(0)) or not (await frame(1)):
        res.status, res.reason = "camera_unavailable", "no_frames"
        return res
    for f in names:
        a[f].append(None)  # no Reader A read of frame 0

    async def third() -> bool:
        if not await frame(2):
            return False
        return True

    # Reader A starts at once; frame 2 arrives (150 ms) while it answers (1–2 s)
    _, _, _, got3 = await asyncio.gather(read_b(0), read_b(1), read_a(1), third())
    if not got3:
        res.status, res.reason = "camera_unavailable", "no_frames"
        return res
    await read_b(2)

    def settled() -> dict[str, tuple[str | None, int | None]]:
        return {f: decide(b[f], a[f]) for f in names}

    got = settled()
    # A again on frame 2 only if a field has a stable B pair ending now that no A read confirmed
    need_a = level == "full" and any(k is None and (_stable(b[f], 1, 2) or _stable(b[f], 0, 2)) for f, (k, _) in got.items())
    if need_a:
        await read_a(2)
        got = settled()
    else:
        for f in names:
            a[f].append(None)
    for f in names:
        if level == "full":
            k, ai = got[f]
            res.fields[f] = _field_result(f, k, b[f], a[f], ai, spec.display, "agreed")
        else:
            res.fields[f] = _field_result(f, decide_b_only(b[f]), b[f], a[f], None, spec.display, "b_only")
    res.frames = list(range(len(frames)))
    res.b_ms, res.ms = b_ms, (time.perf_counter() - t0) * 1000
    if spec.display == "meter":
        am = [r for r in a["main"] if r is not None and r.legible]
        res.mode = next((r.mode for r in reversed(am) if r.mode), None) or (MODE_OF_UNIT.get(am[-1].unit) if am else None)
    return check(res, expected_mode)


def check(res: DisplayResult, expected_mode: str | None = None) -> DisplayResult:
    """Status and plausibility of a display read."""
    fs = res.fields
    if not all(r.accepted for r in fs.values()):
        res.status = "unclear"
        res.reason = next((r.reason for r in fs.values() if not r.accepted), "readers_disagree")
        return res
    if res.display == "supply":
        live, lim = fs["live_current"], fs["current_limit"]
        if live.value is not None and lim.value is not None:
            count = 10 ** -max(live.decimals, 0)
            if live.value > lim.value + count:
                res.status, res.reason = "implausible", "current_above_limit"
                return res
    else:
        m = fs["main"]
        mode = res.mode
        if expected_mode and mode and mode != expected_mode:
            res.status, res.reason = "wrong_mode", f"meter_on_{mode}"
            res.say = {"en": f"Your meter is on {MODE_WORDS.get(mode, mode)}; switch it to {MODE_WORDS.get(expected_mode, expected_mode)}.",
                       "hi": f"Meter abhi {MODE_WORDS.get(mode, mode)} pe hai; {MODE_WORDS.get(expected_mode, expected_mode)} pe karo."}
            return res
        lo, hi = PLAUSIBLE.get(mode or expected_mode or "", (-np.inf, np.inf))
        if not m.ol and m.si_value is not None and not (lo <= m.si_value <= hi):
            res.status, res.reason = "implausible", "out_of_range"
            return res
    res.status = "ok"
    return res
