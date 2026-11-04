"""All seven fake-bench scenarios, driven through the 8 tools by a scripted technician.

Fault scenarios must reach the scripted root cause and close as fixed after a healthy check.
Robustness scenarios must produce a re-check request or a degraded-mode event, and never a
confident wrong answer.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from engine_run import RunResult, Technician  # noqa: E402
from engine.tools import MAX_OUTPUT_CHARS  # noqa: E402
from mcp_evidence.fake_bench import SCENARIOS  # noqa: E402

FAULT_SCENARIOS = ["short_battery_line", "no_current", "not_charging"]
ROBUSTNESS = ["glare_low_confidence", "conflicting_reading", "camera_unavailable", "vision_timeout"]


def drive(scenario: str, **kw) -> tuple[RunResult, Technician]:
    tech = Technician(scenario, **kw)
    return asyncio.run(tech.run()), tech


def test_the_seven_scenarios_are_covered():
    assert sorted(FAULT_SCENARIOS + ROBUSTNESS) == sorted(SCENARIOS)


@pytest.mark.parametrize("spoken", [False, True], ids=["camera", "spoken"])
@pytest.mark.parametrize("lang", ["en", "hi"])
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_every_scenario_reaches_the_scripted_root_cause_and_a_confirmed_fix(scenario, lang, spoken):
    res, tech = drive(scenario, lang=lang, spoken=spoken)
    assert res.found == res.true_fault, [c.output for c in res.calls]
    assert res.outcome == "fixed"
    assert res.wrong_fix == 0 and not res.confident_wrong
    assert 1 <= res.steps <= 6
    assert res.max_output <= MAX_OUTPUT_CHARS
    assert tech.repair.state == "closed"


def test_glare_asks_for_a_read_aloud_and_never_ranks_an_unclear_reading():
    res, tech = drive("glare_low_confidence")
    assert res.read_aloud >= 1
    unclear = [e for e in tech.repair.evidence if e.source == "camera" and e.kind == "meter"]
    assert unclear and all(e.confidence < 0.9 and e.measurement_id is None for e in unclear)
    used = {o.evidence_id for o in tech.repair.ledger.active}
    assert not used & {e.id for e in unclear}
    assert all(e.source == "spoken" for e in tech.repair.evidence if e.id in used and e.kind in ("meter", "spoken"))


def test_conflicting_reading_asks_for_a_remeasure_before_any_fix():
    res, tech = drive("conflicting_reading")
    assert res.rechecks == 1
    outputs = [json.loads(c.output) for c in res.calls]
    first_conflict = next(i for i, o in enumerate(outputs) if o.get("conflict"))
    remeasure = next(i for i, o in enumerate(outputs) if o.get("step") == "remeasure")
    fix = next(i for i, o in enumerate(outputs) if o.get("step") == "fix")
    assert first_conflict < remeasure < fix
    assert outputs[remeasure]["measurement"] == "vbat_conn"  # the slipped probe reading
    # the slipped reading was superseded, not averaged in
    vbat = [e for e in tech.repair.evidence if e.measurement_id == "vbat_conn" and e.outcome]
    assert vbat[0].outcome == "short" and vbat[0].superseded_by == vbat[1].id


def test_camera_unavailable_is_reported_as_degraded_and_readings_go_aloud():
    res, tech = drive("camera_unavailable")
    assert "camera_unavailable" in res.degraded
    assert any(e["event"] == "degraded" and e["reason"] == "camera_unavailable" for e in tech.repair.events)
    assert res.read_aloud >= 1
    calls = [c.tool for c in res.calls]
    assert "watch_boot_current" not in calls  # no power without a camera-read supply
    assert all(json.loads(c.output).get("approved") is not True for c in res.calls if c.tool == "request_power_step")


def test_vision_timeout_falls_back_to_reading_aloud():
    res, tech = drive("vision_timeout")
    assert "vision_timeout" in res.degraded and res.read_aloud >= 1
    fallbacks = [e for e in tech.repair.events if e["event"] == "reading.fallback"]
    assert fallbacks and all(e["reason"] == "vision_timeout" for e in fallbacks)


def test_every_power_step_was_approved_by_the_gate_first():
    for scenario in SCENARIOS:
        res, _ = drive(scenario)
        approved = 0
        for c in res.calls:
            out = json.loads(c.output)
            if c.tool == "request_power_step" and out.get("approved"):
                approved += 1
            needs_power = c.tool == "watch_boot_current" or c.args.get("kind") == "port_current" or (
                c.tool == "record_spoken_reading" and c.args.get("confirm") and out.get("class") in ("cap_hot", "ic_hot", "none"))
            if needs_power and "error" not in out:
                assert approved >= 1, (scenario, c)
                approved -= 1
