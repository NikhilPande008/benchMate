"""Approval lifetime (CLAUDE.md rule 2): bound to one power action and the supply settings checked;
the sentence may be repeated while valid; it ends on user confirmation, after 20 s, when the supply
settings change, on barge-in, or when a different power action is approved."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import pytest

from engine.safety.approval import APPROVAL_TTL_S, CONFIRM_GRACE_S, Approvals, action_done, settings_of
from engine.session.repair import RepairSession
from mcp_evidence import fake_bench
from mcp_evidence.fake_bench import FakeBench
from mcp_evidence.schema import EvidenceRecord, FieldValue, utcnow

NOW = utcnow()
S = (4.0, 1.0)


def at(s: float):
    return NOW + timedelta(seconds=s)


def granted(step="connect_boot_cable", supply=S) -> Approvals:
    a = Approvals()
    a.grant(step, supply, NOW)
    return a


def test_the_lifetime_is_20_s():
    assert APPROVAL_TTL_S == 20.0


def test_valid_and_speakable_while_it_lasts_then_expired():
    a = granted()
    for s in (0, 5, 19.9):
        assert a.speakable(at(s)) is not None and a.valid("connect_boot_cable", at(s))
    assert a.speakable(at(20)) is None and a.pending is None
    assert a.ended[-1][1] == "expired"
    assert not a.consume("connect_boot_cable", at(20))


def test_a_new_turn_is_not_an_ending():
    """Nothing about a new user turn ends an approval; only the listed conditions do (the 4 Nov
    replay case: the gate's own sentence repeated one turn later)."""
    a = granted()
    assert a.confirm("kaunsa cable?", at(3)) is None
    assert a.confirm("ok", at(4)) is None
    assert a.speakable(at(6)) is not None


@pytest.mark.parametrize(
    "text,step,done",
    [
        ("lag gaya", "connect_boot_cable", True),
        ("haan cable laga diya", "connect_boot_cable", True),
        ("connected", "connect_boot_cable", True),
        ("okay, it is plugged in", "connect_boot_cable", True),
        ("ho gaya", "connect_boot_cable", True),
        ("done", "press_power_key", True),
        ("power button daba diya", "press_power_key", True),
        ("pressed it", "press_power_key", True),
        ("लगा दिया", "connect_boot_cable", True),
        ("केबल लग गया।", "connect_boot_cable", True),
        ("दबा दिया", "press_power_key", True),
        ("charging port mein laga diya", "connect_port_supply", True),
        # not a confirmation: not yet, negated, a question, an acknowledgement, a plan
        ("abhi nahi lagaya", "connect_boot_cable", False),
        ("not connected yet", "connect_boot_cable", False),
        ("lag gaya?", "connect_boot_cable", False),
        ("laga doon?", "connect_boot_cable", False),
        ("ok", "connect_boot_cable", False),
        ("haan", "connect_boot_cable", False),
        ("theek hai, lagata hoon", "connect_boot_cable", False),
        ("नहीं लगाया", "connect_boot_cable", False),
        ("pressed", "connect_boot_cable", False),  # the wrong action's word
        ("", "connect_boot_cable", False),
    ],
)
def test_action_done(text, step, done):
    assert action_done(text, step) is done


def test_user_confirmation_ends_the_sentence_but_lets_the_measurement_start():
    a = granted()
    assert a.confirm("lag gaya", at(18)) is not None
    assert a.ended[-1][1] == "confirmed"
    assert a.speakable(at(18.1)) is None  # the sentence is blocked again
    # the watch after "lag gaya" may still start, briefly, even past the 20 s
    assert a.valid("connect_boot_cable", at(18 + CONFIRM_GRACE_S - 0.1))
    assert a.consume("connect_boot_cable", at(21))
    assert a.ended[-1][1] == "used" and a.pending is None


def test_confirmation_grace_runs_out():
    a = granted()
    a.confirm("connected", at(15))
    assert a.valid("connect_boot_cable", at(24.9))  # past 20 s, within the grace
    assert not a.valid("connect_boot_cable", at(15 + CONFIRM_GRACE_S)) and a.ended[-1][1] == "expired"


def test_confirmation_after_expiry_does_nothing():
    a = granted()
    assert a.confirm("lag gaya", at(21)) is None and a.pending is None


def test_the_measurement_starting_ends_it():
    a = granted()
    assert a.consume("connect_boot_cable", at(2))
    assert a.speakable(at(2)) is None and a.ended[-1][1] == "used"


@pytest.mark.parametrize(
    "new,ends",
    [
        ((4.0, 1.0), False),  # same settings, any number of re-reads
        ((4.2, 1.0), True),  # voltage turned
        ((4.0, 0.5), True),  # limit turned
        ((None, 1.0), True),  # voltage not readable: can't be sure, so it ends
        ((4.0, None), True),
    ],
)
def test_supply_settings_change_ends_it(new, ends):
    a = granted()
    gone = a.on_supply(new)
    assert (gone is not None) is ends and (a.pending is None) is ends
    if ends:
        assert a.ended[-1][1] == "supply_changed"


def test_settings_are_compared_as_displayed():
    def rec(v, lim, live=0.0):
        return EvidenceRecord(session_id="se", kind="supply", source="camera", confidence=0.95, fields={
            "set_voltage": FieldValue(value=v, unit="V", confidence=0.95),
            "current_limit": FieldValue(value=lim, unit="A", confidence=0.95),
            "live_current": FieldValue(value=live, unit="A", confidence=0.95)})

    assert settings_of(rec(4.0, 1.0)) == settings_of(rec(4.0000001, 1.0, live=0.35))  # live current is not a setting
    assert settings_of(rec(4.0, 1.0)) != settings_of(rec(4.01, 1.0))
    assert settings_of(None) == (None, None)


def test_barge_in_ends_it():
    a = granted()
    assert a.end("barge_in") is not None and a.speakable(at(1)) is None
    assert a.ended[-1][1] == "barge_in"


def test_a_different_approval_supersedes_it():
    a = granted("connect_boot_cable")
    new, before = a.grant("press_power_key", S, at(3))
    assert before is not None and before[1] == "superseded" and before[0].step == "connect_boot_cable"
    assert a.pending is new and not a.valid("connect_boot_cable", at(3))


def test_the_same_action_checked_again_renews_it():
    a = granted()
    first = a.pending
    new, before = a.grant("connect_boot_cable", S, at(15))
    assert before == (first, "renewed") and new.id != first.id
    assert a.speakable(at(30)) is new  # 20 s from the new check


# --- in the repair session: power.ended events -----------------------------------------------


def run(coro):
    return asyncio.run(coro)


def started(scenario="short_battery_line") -> RepairSession:
    r = RepairSession("se_ap", FakeBench(scenario, time_scale=0))

    async def go():
        await r.start_repair("demo", "phone is dead", "removed")
        await r.get_reading("supply")

    run(go())
    return r


def ended(r: RepairSession) -> list[tuple[str, str]]:
    return [(e["step"], e["reason"]) for e in r.events if e["event"] == "power.ended"]


def test_session_confirmation_from_the_user_transcript():
    r = started()
    out = run(r.request_power_step("connect_boot_cable"))
    run(r.on_user_transcript("haan, lag gaya", "hinglish"))
    assert ended(r) == [("connect_boot_cable", "confirmed")]
    assert "pattern" in run(r.watch_boot_current("on_connect"))  # the watch still starts
    assert ended(r)[-1] == ("connect_boot_cable", "used")
    assert [e["id"] for e in r.events if e["event"] == "power.ended"] == [out["approval"]] * 2


def test_session_a_changed_supply_reading_ends_it(monkeypatch):
    r = started()
    run(r.request_power_step("connect_boot_cable"))
    run(r.get_reading("supply"))  # same settings
    assert ended(r) == []
    monkeypatch.setitem(fake_bench.SCENARIOS, "short_battery_line", replace(fake_bench.SCENARIOS["short_battery_line"], supply=(4.2, 1.0)))
    run(r.get_reading("supply"))
    assert ended(r) == [("connect_boot_cable", "supply_changed")]
    assert run(r.watch_boot_current("on_connect"))["error"] == "power_step_required"


def test_session_a_different_approval_supersedes():
    r = started()
    run(r.request_power_step("connect_boot_cable"))
    run(r.request_power_step("press_power_key"))
    assert ended(r) == [("connect_boot_cable", "superseded")]
    assert r.approvals.pending.step == "press_power_key"


def test_session_expiry_is_announced():
    r = started()
    r.approvals.ttl_s = 0.05

    async def go():
        await r.request_power_step("connect_boot_cable")
        await asyncio.sleep(0.2)

    run(go())
    assert ended(r) == [("connect_boot_cable", "expired")]


def test_session_violation_and_stop_end_it():
    r = started()
    run(r.request_power_step("connect_boot_cable"))
    r.on_safety_violation("press_power_key", "Press the power key now.")
    run(r.request_power_step("connect_boot_cable"))
    run(r.stop_session("test"))
    assert ended(r) == [("connect_boot_cable", "violation"), ("connect_boot_cable", "stopped")]
