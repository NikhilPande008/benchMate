"""Checks the latency-run analysis on synthetic spans (no browser, no API)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from sqlmodel import Session

from engine.telemetry import Span, make_engine

_spec = importlib.util.spec_from_file_location("run_latency", Path(__file__).parent / "run_latency.py")
run_latency = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_latency)

T0 = 1_000_000.0
MANIFEST = {
    "duration_s": 40,
    "turns": [
        {"id": "p01", "kind": "plain", "lang": "en", "start_ms": 8000, "end_ms": 10000},
        {"id": "t01", "kind": "tool", "lang": "en", "start_ms": 20000, "end_ms": 21000},
        {"id": "p02", "kind": "plain", "lang": "hi", "start_ms": 32000, "end_ms": 33000},
    ],
}


def _spans():
    def s(name, t, turn, source="browser", attrs=None):
        return Span(run_id="r", session_id="se", turn=turn, name=name, t_ms=T0 + t, source=source, attrs=attrs)

    return [
        s("mic_open", 0, 0),
        s("session_connected", 1800, 0),
        # p01: VAD reports the end 400 ms after the true end, audio 700 ms later, audible 50 ms after that
        s("speech_started", 8300, 0, attrs='{"audio_start_ms":2000}'),
        s("speech_end", 10400, 1, attrs='{"audio_end_ms":4900}'), s("first_audio", 11100, 1), s("audio_onset", 11150, 1),
        # t01: preamble, tool, value
        s("speech_end", 21500, 2), s("first_audio", 22000, 2), s("audio_onset", 22040, 2),
        s("tool_call_emitted", 22100, 2), s("tool_started", 22110, 2, "engine"),
        s("tool_finished", 23610, 2, "engine"), s("tool_result_received", 23620, 2),
        s("first_audio", 24300, 2),
        # p02: model called a tool it should not have
        s("speech_end", 33300, 3), s("tool_call_emitted", 33900, 3), s("first_audio", 34000, 3),
    ]


def test_analyse_aligns_scripted_lines_with_turns(tmp_path):
    db = tmp_path / "t.sqlite"
    with Session(make_engine(db)) as sess:
        sess.add_all(_spans())
        sess.commit()

    res = run_latency.analyse("r", MANIFEST, db)
    p01, t01, p02 = res["rows"]

    assert res["connect_ms"] == 1800
    assert (p01["vad_lag"], p01["first_audio"], p01["user_felt"]) == (400, 700, 1150)
    assert p01["vad_tail"] == 900  # server heard 2900 ms of speech for a 2000 ms line
    assert "problem" not in p01
    assert (t01["observed"], t01["first_audio"], t01["value_audio"], t01["tool_ms"]) == ("tool", 500, 2800, 1500)
    assert p02["problem"] == "expected plain, model did tool"
    assert res["summary"]["plain"]["first_audio"]["n"] == 1
