from __future__ import annotations

import json

import pytest

from engine.telemetry import Span, percentile, summarize, turn_metrics


def span(span_name, t, turn, session="se_a", **attrs):
    return Span(
        session_id=session, turn=turn, name=span_name, t_ms=t, source="browser", attrs=json.dumps(attrs) if attrs else None
    )


def test_percentile_matches_linear_interpolation():
    xs = [100, 200, 300, 400, 500]
    assert percentile(xs, 50) == 300
    assert percentile(xs, 95) == pytest.approx(480)
    assert percentile([7], 95) == 7
    assert percentile([], 50) is None


def test_plain_and_tool_turns():
    spans = [
        # turn 1: plain, server starts audio 700 ms after speech_end, audible at 760 ms
        span("speech_end", 1000, 1),
        span("first_audio", 1700, 1),
        span("audio_onset", 1760, 1),
        # turn 2: tool turn with preamble at 600 ms, value spoken after the tool result
        span("speech_end", 5000, 2),
        span("first_audio", 5600, 2),
        span("tool_call_emitted", 5650, 2, name="get_reading"),
        Span(session_id="se_a", turn=2, name="tool_started", t_ms=5660, source="engine"),
        Span(session_id="se_a", turn=2, name="tool_finished", t_ms=7160, source="engine"),
        span("tool_result_received", 7170, 2),
        span("first_audio", 7800, 2),
        # turn 0 (before any speech_end) is ignored
        span("first_audio", 100, 0),
    ]
    plain, tool = turn_metrics(spans)

    assert (plain.kind, plain.first_audio, plain.audio_onset, plain.value_audio) == ("plain", 700, 760, None)
    assert tool.kind == "tool" and tool.tools == ["get_reading"]
    assert tool.first_audio == 600
    assert tool.value_audio == 2800
    assert tool.tool_ms == 1500


def test_onset_before_first_audio_is_ignored():
    # stale audio from an interrupted reply drains after speech_end, before this reply starts
    spans = [span("speech_end", 1000, 1), span("audio_onset", 1100, 1), span("first_audio", 1600, 1), span("audio_onset", 1750, 1)]
    (t,) = turn_metrics(spans)
    assert (t.first_audio, t.audio_onset) == (600, 750)


def test_onset_far_after_first_audio_counts_as_undetected():
    (t,) = turn_metrics([span("speech_end", 1000, 1), span("first_audio", 1600, 1), span("audio_onset", 4000, 1)])
    assert t.audio_onset is None


def test_turn_without_audio_has_no_latency():
    (t,) = turn_metrics([span("speech_end", 1000, 1)])
    assert t.first_audio is None


def test_summary_groups_by_turn_kind():
    spans = []
    for i, lat in enumerate([500, 600, 700, 800], start=1):
        spans += [span("speech_end", i * 10_000, i), span("first_audio", i * 10_000 + lat, i)]
    summary = summarize(turn_metrics(spans))
    assert summary["plain"]["turns"]["n"] == 4
    assert summary["plain"]["first_audio"]["p50"] == 650
    assert summary["tool"]["turns"]["n"] == 0
