"""Boot-current trace features and pattern classification (ARCHITECTURE.md §5.3).

Thresholds come from the pack (placeholder values until the reference phone is measured).
"""
from __future__ import annotations

import statistics

from mcp_evidence.packs.schema import BootThresholds
from mcp_evidence.schema import BootFeatures, BootSample, BootTrace


def features(samples: list[BootSample], limit_a: float, t: BootThresholds, power_press_s: float | None = None) -> BootFeatures:
    if not samples:
        return BootFeatures(i_connect=0.0, i_peak=0.0, i_steady=0.0, t_drop=None, at_limit_s=0.0)
    connect_end = min(1.0, power_press_s) if power_press_s is not None else 1.0
    early = [s.current_a for s in samples if s.t_s <= connect_end] or [samples[0].current_a]
    peak_sample = max(samples, key=lambda s: s.current_a)
    tail = [s.current_a for s in samples[-5:]]
    near_zero = max(t.no_current_a, 0.1 * peak_sample.current_a)
    t_drop = next((s.t_s - peak_sample.t_s for s in samples if s.t_s > peak_sample.t_s and s.current_a <= near_zero), None)
    # longest continuous run at the limit
    longest, run_start = 0.0, None
    for s in samples:
        if s.current_a >= t.at_limit_fraction * limit_a:
            run_start = s.t_s if run_start is None else run_start
            longest = max(longest, s.t_s - run_start)
        else:
            run_start = None
    return BootFeatures(
        i_connect=max(early),
        i_peak=peak_sample.current_a,
        i_steady=statistics.median(tail),
        t_drop=t_drop,
        at_limit_s=longest,
    )


def classify(f: BootFeatures, t: BootThresholds) -> str:
    """First matching rule wins: AT_LIMIT, SHORT_IMMEDIATE, NO_CURRENT, RISE_THEN_DROP, NORMAL_BOOT."""
    if f.at_limit_s > t.at_limit_s:
        return "AT_LIMIT"
    if f.i_connect > t.short_connect_a:
        return "SHORT_IMMEDIATE"
    if f.i_peak < t.no_current_a:
        return "NO_CURRENT"
    if f.i_steady < t.drop_fraction * f.i_peak:
        return "RISE_THEN_DROP"
    return "NORMAL_BOOT"


def analyse(trace: BootTrace, t: BootThresholds, power_press_s: float | None = None) -> BootTrace:
    feats = features(trace.samples, trace.limit_a, t, power_press_s)
    return trace.model_copy(update={"features": feats, "pattern": classify(feats, t)})
