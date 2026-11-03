"""Turn-level latency metrics and p50/p95 reports from stored spans.

A turn starts at the browser's `speech_end` span (the client received
`input_audio_buffer.speech_stopped`); every span the browser records after it carries the same
turn index until the next `speech_end`.

Per turn:
- first_audio:  speech_end -> `output_audio_buffer.started` (server began streaming audio)
- audio_onset:  speech_end -> audible output on the remote track, at or after first_audio
- value_audio:  tool turns only; speech_end -> first audio after the tool result reached the browser
- tool_ms:      engine tool_started -> tool_finished
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from .store import Span

TURN_START = "speech_end"
ONSET_WINDOW_MS = 1000


@dataclass
class TurnMetrics:
    session_id: str
    turn: int
    kind: str  # "plain" | "tool"
    speech_end_t: float  # epoch ms
    first_audio: float | None = None
    audio_onset: float | None = None
    value_audio: float | None = None
    tool_ms: float | None = None
    tools: list[str] = field(default_factory=list)


def _first_after(times: list[float], t0: float) -> float | None:
    after = [t for t in times if t >= t0]
    return min(after) if after else None


def turn_metrics(spans: list[Span]) -> list[TurnMetrics]:
    by_turn: dict[tuple[str, int], list[Span]] = defaultdict(list)
    for s in spans:
        by_turn[(s.session_id, s.turn)].append(s)

    out = []
    for (session_id, turn), group in sorted(by_turn.items(), key=lambda kv: min(s.t_ms for s in kv[1])):
        names: dict[str, list[float]] = defaultdict(list)
        for s in group:
            names[s.name].append(s.t_ms)
        if TURN_START not in names:
            continue
        se = min(names[TURN_START])
        calls = [s for s in group if s.name == "tool_call_emitted"]
        m = TurnMetrics(
            session_id=session_id,
            turn=turn,
            kind="tool" if calls else "plain",
            speech_end_t=se,
            tools=[json.loads(s.attrs or "{}").get("name", "?") for s in calls],
        )
        fa = _first_after(names["first_audio"], se)
        # Audible onset of *this* reply: within ONSET_WINDOW_MS after the server says its audio
        # started. After a barge-in the previous reply's tail can still be sounding, so the
        # detector (which needs 300 ms of silence first) misses the start; that turn gets None.
        onset = _first_after(names["audio_onset"], fa) if fa is not None else None
        if onset is not None and onset - fa > ONSET_WINDOW_MS:
            onset = None
        m.first_audio = fa - se if fa is not None else None
        m.audio_onset = onset - se if onset is not None else None
        if calls:
            tr = _first_after(names["tool_result_received"], se)
            va = _first_after(names["first_audio"], tr) if tr is not None else None
            m.value_audio = va - se if va is not None else None
            started, finished = names["tool_started"], names["tool_finished"]
            if started and finished:
                m.tool_ms = min(finished) - min(started)
        out.append(m)
    return out


def percentile(values: list[float], q: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default)."""
    if not values:
        return None
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


METRICS = ("first_audio", "audio_onset", "value_audio", "tool_ms")


def summarize(turns: list[TurnMetrics]) -> dict[str, dict[str, dict[str, float | int | None]]]:
    """{kind: {metric: {n, p50, p95, max}}}"""
    out: dict[str, dict[str, dict[str, float | int | None]]] = {}
    for kind in ("plain", "tool"):
        ts = [t for t in turns if t.kind == kind]
        out[kind] = {"turns": {"n": len(ts)}}
        for metric in METRICS:
            vals = [v for t in ts if (v := getattr(t, metric)) is not None]
            if not vals:
                continue
            out[kind][metric] = {
                "n": len(vals),
                "p50": percentile(vals, 50),
                "p95": percentile(vals, 95),
                "max": max(vals),
            }
    return out


def format_summary(title: str, summary: dict) -> str:
    lines = [title, f"  {'turn':<6} {'metric':<12} {'n':>3} {'p50 ms':>8} {'p95 ms':>8} {'max ms':>8}"]
    for kind, metrics in summary.items():
        n_turns = metrics["turns"]["n"]
        if not n_turns:
            continue
        for metric in METRICS:
            if metric not in metrics:
                continue
            row = metrics[metric]
            lines.append(
                f"  {kind:<6} {metric:<12} {row['n']:>3} {row['p50']:>8.0f} {row['p95']:>8.0f} {row['max']:>8.0f}"
            )
    return "\n".join(lines)
