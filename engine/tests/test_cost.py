"""Cost from recorded usage: prices per modality, cached input, old spans, transcription, run totals."""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from engine.config import REALTIME_PRICES, TRANSCRIPTION_PRICES_PER_MIN
from engine.telemetry import RealtimeSessionRow, Span, make_engine
from engine.telemetry.cost import over_budget, response_cost, run_cost, transcription_cost, usd_per_line_from_history

MINI = "gpt-realtime-2.1-mini"


def test_response_cost_prices_each_modality_and_cached_input():
    usage = {
        "in_audio_tokens": 1000, "in_cached_audio_tokens": 400,  # 600 uncached audio
        "in_text_tokens": 3000, "in_cached_text_tokens": 2000,  # 1000 uncached text
        "out_audio_tokens": 200, "out_text_tokens": 50,
    }
    p = REALTIME_PRICES[MINI]
    expected = (600 * p["audio_in"] + 400 * p["audio_cached"] + 1000 * p["text_in"] + 2000 * p["text_cached"]
                + 200 * p["audio_out"] + 50 * p["text_out"]) / 1e6
    assert response_cost(MINI, usage) == pytest.approx(expected)
    assert response_cost(MINI, usage) == pytest.approx(0.01096)  # 6000 + 120 + 600 + 120 + 4000 + 120 per 1M


def test_flagship_costs_more_than_mini_for_the_same_usage():
    usage = {"in_text_tokens": 2000, "out_audio_tokens": 100}
    assert response_cost("gpt-realtime-2.1", usage) > 3 * response_cost(MINI, usage)


def test_old_spans_without_the_input_split_are_priced_as_an_upper_bound():
    old = {"in_tokens": 1000, "in_cached_tokens": 800, "out_tokens": 100}
    p = REALTIME_PRICES[MINI]
    assert response_cost(MINI, old) == pytest.approx((200 * p["audio_in"] + 800 * p["text_cached"] + 100 * p["audio_out"]) / 1e6)


def test_unknown_model_is_priced_as_flagship():
    usage = {"out_audio_tokens": 1000}
    assert response_cost("gpt-realtime-9", usage) == response_cost("gpt-realtime-2.1", usage)


def test_transcription_is_per_minute():
    assert transcription_cost("gpt-transcribe", 120) == pytest.approx(2 * TRANSCRIPTION_PRICES_PER_MIN["gpt-transcribe"])


def _store(db, run_id, session_id, model, prompt, spans):
    with Session(db) as s:
        s.add(RealtimeSessionRow(id=session_id, run_id=run_id, model=model, prompt_version="x", created_ms=0, prompt=prompt))
        for i, (name, attrs) in enumerate(spans):
            s.add(Span(run_id=run_id, session_id=session_id, name=name, t_ms=float(i), source="browser", attrs=json.dumps(attrs)))
        s.commit()


def test_run_cost_adds_responses_and_transcription_per_line(tmp_path):
    db = make_engine(tmp_path / "c.sqlite")
    r1 = {"in_text_tokens": 1000, "out_audio_tokens": 100}
    r2 = {"in_text_tokens": 2000, "out_audio_tokens": 50}
    _store(db, "x-1_a", "se_1", MINI, "v3_local", [
        ("response_done", r1),  # before any line: the greeting/connect
        ("line_start", {"id": "t01", "clip_ms": 3000}),
        ("response_done", r2),
        ("line_start", {"id": "t02", "clip_ms": 2000}),
    ])
    c = run_cost(db, "x-1_a")
    assert (c.responses, c.lines) == (2, 2)
    assert c.realtime_usd == pytest.approx(response_cost(MINI, r1) + response_cost(MINI, r2))
    assert c.transcription_usd == pytest.approx(transcription_cost("gpt-transcribe", 5.0))
    assert c.per_line["connect"] == pytest.approx(response_cost(MINI, r1))
    assert c.per_line["t01"] == pytest.approx(response_cost(MINI, r2) + transcription_cost("gpt-transcribe", 3.0))
    assert c.usd == pytest.approx(c.realtime_usd + c.transcription_usd)


def test_run_prefix_is_literal_and_filters_by_model_and_prompt(tmp_path):
    db = make_engine(tmp_path / "c.sqlite")
    usage = {"out_audio_tokens": 1000}
    _store(db, "x-9_b-mini", "se_a", MINI, "v3_local", [("line_start", {"id": "a"}), ("response_done", usage)])
    _store(db, "x-9Xb-flag", "se_b", "gpt-realtime-2.1", "v2_local", [("line_start", {"id": "b"}), ("response_done", usage)])
    assert run_cost(db, "x-9_b").responses == 1  # "_" is not a wildcard
    assert run_cost(db, "x-9", model=MINI).responses == 1
    assert run_cost(db, "x-9", prompt="v2_local").responses == 1
    per_line, seen = usd_per_line_from_history(db, MINI, ["x-9"], "v3_local")
    assert seen == 1 and per_line == pytest.approx(response_cost(MINI, usage))


def test_history_falls_back_to_a_default_when_there_is_none(tmp_path):
    per_line, seen = usd_per_line_from_history(make_engine(tmp_path / "c.sqlite"), MINI, ["x-"])
    assert seen == 0 and per_line > 0


@pytest.mark.parametrize("spent,budget,over", [(1.99, 2.0, False), (2.0, 2.0, True), (5.0, None, False)])
def test_over_budget(spent, budget, over):
    assert over_budget(spent, budget) is over
