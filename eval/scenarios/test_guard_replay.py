"""The guardrail replay (guard_replay.py) on a small synthetic timeline shaped like the 4 Nov runs."""
from __future__ import annotations

import json
import shutil
from types import SimpleNamespace as NS

import pytest

from eval.scenarios.guard_replay import engine_endings, replay_session, timeline

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

APPROVED = json.dumps({"approved": True, "step": "connect_boot_cable", "say": {"en": "Connect the boot cable now.", "hi": "Ab boot cable lagao."}})


def span(t, span_name, source="browser", **attrs):
    return NS(t_ms=t, name=span_name, source=source, attrs=json.dumps(attrs), session_id="se")


def said(t, text, role="assistant"):
    return NS(t_ms=t, role=role, text=text, session_id="se")


def session(gap_ms=6000, barge=False, confirm=None, paraphrase=False):
    spans = [span(0, "ptt_down"), span(1000, "tool_finished", "engine", name="get_reading", output=json.dumps({"kind": "supply", "volts": 4.0, "limit_a": 1.0})),
             span(2000, "tool_finished", "engine", name="request_power_step", output=APPROVED)]
    trans = [said(2500, "Ab boot cable lagao.")]
    t = 2500 + gap_ms
    if barge:
        spans.append(span(t - 10, "barge_in", via="ptt"))
    spans.append(span(t, "ptt_down"))
    if confirm:
        trans.append(said(t + 500, confirm, role="user"))
    trans.append(said(t + 1500, "Next, connect the boot cable to proceed." if paraphrase else "Haan, ab boot cable lagao."))
    return spans, trans


@needs_node
def test_the_gate_sentence_repeated_a_turn_later_passes_only_under_the_lifetime_rule():
    spans, trans = session()
    assert replay_session(spans, trans, "lifetime") == []
    assert [x["sentence"] for x in replay_session(spans, trans, "turn")] == ["Haan, ab boot cable lagao."]


@needs_node
@pytest.mark.parametrize("kw", [{"gap_ms": 20_000}, {"barge": True}, {"confirm": "lag gaya"}, {"paraphrase": True}])
def test_after_an_ending_or_as_a_paraphrase_it_is_caught(kw):
    spans, trans = session(**kw)
    trips = replay_session(spans, trans, "lifetime")
    assert len(trips) == 1 and trips[0]["kind"] == "connect_boot_cable"


def test_the_engine_endings_are_added_to_the_timeline():
    spans, trans = session(confirm="cable laga diya")
    ev = engine_endings(timeline(spans, trans))
    assert [e["reason"] for e in ev if e["type"] == "ended"] == ["confirmed"]
