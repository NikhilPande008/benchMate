"""The experiment analysis on hand-built spans (no browser, no API)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from analysis import analyse_session, connect_stages, summarize  # noqa: E402
from engine.telemetry import Span, Transcript  # noqa: E402

T0 = 1_000_000.0


def s(span_name, t, **attrs):
    return Span(session_id="se", turn=0, name=span_name, t_ms=T0 + t, source="browser", attrs=json.dumps(attrs) if attrs else None)


def line(t, id, kind, lang, end, pause=0):
    return s("line_start", t, id=id, set="synthetic", kind=kind, lang=lang, true_start=T0 + t + 100, true_end=T0 + end, pause_ms=pause)


def test_false_cutoff_then_reply():
    spans = [
        line(0, "p05", "plain", "en", 3500, pause=900),
        s("speech_started", 300),
        s("speech_end", 1900, item_id="i1"),  # VAD fires inside the pause...
        s("speech_started", 2600),  # ...and the speaker carries on
        s("speech_end", 4400, item_id="i2"),
        s("first_audio", 4800, response_id="r2"),
        s("audio_onset", 4950),
        s("response_done", 5000, response_id="r2", outputs="final_answer"),
        s("audio_stopped", 7000, response_id="r2"),
        s("line_done", 8200, timed_out=False),
    ]
    tr = [
        Transcript(session_id="se", role="user", item_id="i2", text="what boot current means", lang_tag="en", t_ms=T0 + 4600),
        Transcript(session_id="se", role="assistant", response_id="r2", phase="final_answer", text="Boot current is the current drawn at power on.", lang_tag="en", t_ms=T0 + 5000),
    ]
    (row,) = analyse_session(spans, tr)
    assert row.false_cutoff
    assert (row.end_to_server, row.server_to_audio, row.end_to_audible) == (900, 400, 1450)
    assert row.reply_ms == [2200]
    assert row.reply_lang == "en" and row.user_text == "what boot current means"


def test_local_preamble_tool_line_with_held_output():
    spans = [
        line(0, "t03", "tool", "hi", 1200),
        s("speech_end", 2100, item_id="i1", mode="semantic_medium"),
        s("tool_call_started", 2400, call_id="c1"),
        s("local_preamble_start", 2405, call_id="c1", lang="hi"),
        s("local_preamble_onset", 2460),
        s("tool_call_emitted", 2420, name="get_reading", call_id="c1"),
        Span(session_id="se", turn=0, name="tool_started", t_ms=T0 + 2425, source="engine"),
        Span(session_id="se", turn=0, name="tool_finished", t_ms=T0 + 3925, source="engine"),
        s("tool_result_received", 3930),
        s("tool_output_held", 3950, call_id="c1", waited_ms=20),
        s("response_created", 4250, response_id="r2"),
        s("first_audio", 4600, response_id="r2"),
        s("audio_onset", 4760),
        s("response_done", 4700, response_id="r2", outputs="final_answer"),
        s("audio_stopped", 6000, response_id="r2"),
    ]
    (row,) = analyse_session(spans, [])
    assert row.observed == "tool" and row.model_spoke_before_result is False
    assert (row.end_to_preamble, row.end_to_value, row.tool_ms, row.result_to_created) == (1260, 3560, 1500, 300)
    assert row.local_clip_lang == "hi"
    assert row.turn_to_call == 320


def test_push_to_talk_uses_the_commit_as_the_server_signal():
    spans = [
        line(0, "p01", "plain", "hinglish", 2000),
        s("speech_end", 2200, mode="ptt"),
        s("committed", 2350, item_id="i1"),
        s("first_audio", 2900, response_id="r1"),
        s("audio_onset", 3050),
    ]
    (row,) = analyse_session(spans, [])
    assert (row.end_to_server, row.server_to_audio, row.end_to_audible) == (350, 550, 1050)
    assert not row.false_cutoff


def test_summary_counts_hindi_replies_and_connect_stages():
    spans = [line(0, "p02", "plain", "hi", 1000), s("speech_end", 1900, item_id="i1"), s("first_audio", 2300, response_id="r1")]
    tr = [Transcript(session_id="se", role="assistant", response_id="r1", phase="final_answer", text="बूट करंट वह करंट है।", lang_tag="hi", t_ms=T0 + 2400)]
    rows = analyse_session(spans, tr)
    connect = connect_stages([s("page_load", 0), s("secret_fetched", 40, source="pool"), s("ready", 1500), s("rtt", 1600, ms=210)])
    summary = summarize(rows, [connect])
    assert summary["language"]["hi_lines_hindi_reply"] == {"lines": 1, "matching": 1}
    assert summary["connect"]["ready"]["p50"] == 1500 and summary["connect"]["rtt"]["p50"] == 210
    assert summary["secret_sources"] == ["pool"]
