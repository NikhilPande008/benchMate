"""Boot-current watch from supply-display frames (ARCHITECTURE.md §5.3).

Every 0.5 s the latest frame's live-current field is read by Reader B (the cheap path: local and
~10 ms). A sample is kept only if it is legible, confident and not above the limit by more than a
count; a missed sample is skipped and counted (`gaps`), never invented. The trace gets the pack's
features and pattern (mcp_evidence/boot), and every sample goes to `on_sample`, where the engine's
at-limit watchdog decides on "disconnect now".
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

import numpy as np

from mcp_evidence.boot import analyse
from mcp_evidence.packs.schema import BootThresholds
from mcp_evidence.schema import BootSample, BootTrace

from .reader_b import read_field

MIN_CONF = 0.6


@dataclass
class WatchResult:
    trace: BootTrace
    samples: int
    gaps: int
    confidence: float


async def watch_current(
    frame: Callable[[float], Awaitable[np.ndarray | None]],  # t_s → the frame at that moment
    crop: Callable[[np.ndarray], np.ndarray],  # frame → live-current field crop
    trigger: str,
    limit_a: float,
    thresholds: BootThresholds,
    on_sample: Callable[[BootSample], Awaitable[bool]] | None = None,
    duration_s: float | None = None,
    interval_s: float = 0.5,
    digits: int = 4,
) -> WatchResult:
    n = int((duration_s or thresholds.watch_s) / interval_s) + 1
    samples: list[BootSample] = []
    gaps = 0
    for i in range(n):
        t = round(i * interval_s, 3)
        img = await frame(t)
        if img is None:
            gaps += 1
            continue
        r = await asyncio.to_thread(read_field, crop(img), digits)
        if not r.legible or r.value is None or r.confidence < MIN_CONF or r.value > limit_a * 1.02 + 0.002:
            gaps += 1
            continue
        s = BootSample(t_s=t, current_a=float(r.value))
        samples.append(s)
        if on_sample and await on_sample(s):
            break
    trace = analyse(BootTrace(trigger=trigger, limit_a=limit_a, samples=samples), thresholds)  # type: ignore[arg-type]
    conf = 0.0 if not samples else float(np.clip(0.95 * len(samples) / (len(samples) + gaps), 0.0, 0.95))
    return WatchResult(trace, len(samples), gaps, conf)
