"""RepairSession behind the tools: each operation's happy path, the safety rules around power,
read-back before the ledger, the fix check, degraded camera mode and the urgent watchdog."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from engine.safety import APPROVED, DISCONNECT_NOW, STOP_MESSAGE
from engine.session.repair import CONFIRMED_CONFIDENCE, SPOKEN_CONFIDENCE, RepairSession
from mcp_evidence import fake_bench
from mcp_evidence.fake_bench import FakeBench, Scenario


class Recorder:
    def __init__(self):
        self.ui: list[dict] = []
        self.injected: list[dict] = []
        self.rows: list[tuple[str, dict]] = []

    async def notify(self, msg):
        self.ui.append(msg)

    async def inject(self, msg):
        self.injected.append(msg)

    def persist(self, table, row):
        self.rows.append((table, row))


def session(scenario="short_battery_line", rec: Recorder | None = None, **kw) -> RepairSession:
    rec = rec or Recorder()
    return RepairSession("se_r", FakeBench(scenario, time_scale=0), notify=rec.notify, inject=rec.inject,
                         persist=rec.persist, **kw)


def run(coro):
    return asyncio.run(coro)


async def started(r: RepairSession, symptom="phone is dead", battery="removed", supply=True):
    await r.start_repair("demo phone", symptom, battery)
    if supply:
        await r.get_reading("supply")
    return r


def events(r: RepairSession, name: str) -> list[dict]:
    return [e for e in r.events if e["event"] == name]


# --- start_repair ------------------------------------------------------------------------------


def test_start_repair_loads_the_model_pack_and_ranks_priors():
    r = session()
    out = run(r.start_repair("Demo Phone", "dead", "removed"))
    assert out["pack"] == "demo_model" and out["model_data"] is True and out["symptom"] == "dead"
    assert out["top"][0][0] == "vbat_cap_short" and "ask" not in out
    assert r.state == "probing" and r.battery == "removed"


def test_no_model_asks_for_it_instead_of_starting():
    r = session()
    out = run(r.start_repair(None, "dead", "removed"))
    assert out["ask"] == "phone_model" and r.ledger is None
    assert run(r.start_repair("unknown", "dead", "removed"))["pack"] == "general_dead_phone"


@pytest.mark.parametrize("model", ["डेमो फोन", "Demo Phone", "redmi demo"])
def test_demo_model_found_in_both_scripts(model):
    assert run(session().start_repair(model, "dead", "removed"))["pack"] == "demo_model"


def test_unknown_model_gets_the_general_flow_and_asks_about_the_battery():
    out = run(session().start_repair("Some Unknown 9", "not charging"))
    assert (out["pack"], out["model_data"], out["symptom"]) == ("general_dead_phone", False, "not_charging")
    assert out["ask"] == "is_battery_removed"


@pytest.mark.parametrize("device", ["laptop charger", "TV board", "230v mains adapter"])
def test_non_phone_devices_are_refused(device):
    r = session()
    out = run(r.start_repair(None, "dead", device=device))
    assert out["refused"] is True and out["reason"] == "device_out_of_scope" and out["say"]
    assert r.ledger is None


# --- get_reading -------------------------------------------------------------------------------


def test_supply_reading_reports_values_and_gate_readiness():
    r = run(started(session(), supply=False))
    out = run(r.get_reading("supply"))
    assert out["kind"] == "supply" and (out["volts"], out["limit_a"]) == (4.0, 1.0)
    assert out["ok"] is True and out["conf"] >= 0.9
    assert r.supply is not None


def test_supply_out_of_band_says_what_to_set():
    fake_bench.SCENARIOS["hot_supply"] = Scenario("hot_supply", "vbat_cap_short", "dead", supply=(5.0, 1.0))
    try:
        r = run(started(session("hot_supply"), supply=False))
        out = run(r.get_reading("supply"))
        assert out["ok"] is False and out["set"] == {"set_voltage": 4.0}
    finally:
        del fake_bench.SCENARIOS["hot_supply"]


def test_meter_reading_defaults_to_the_current_step_point_and_updates_the_ledger():
    r = run(started(session()))
    step = run(r.next_step())
    out = run(r.get_reading("meter"))
    assert out["point"] == step["point"] == "vbat_conn"
    assert out["class"] == "short" and out["conf"] >= 0.9 and out["top"][0][0] == "vbat_cap_short"
    assert events(r, "ledger.updated")[-1]["measurement"] == "vbat_conn"


def test_unknown_point_without_a_current_step_is_an_error():
    r = run(started(session()))
    assert run(r.get_reading("meter", "not_a_point")) == {"error": "unknown_point", "point": "not_a_point"}


def test_made_up_point_falls_back_to_the_point_the_engine_asked_for():
    r = run(started(session()))
    run(r.next_step())  # vbat_conn
    out = run(r.get_reading("meter", "vbat_line"))
    assert out["point"] == "vbat_conn" and out["class"] == "short"


def test_observation_words_sent_with_the_previous_point_answer_the_pending_check():
    # seen in the 4 Nov e2e run: point_id "vbat_conn" (the last point) for "capacitor garam"
    r = run(started(session()))
    run(r.next_step())
    run(r.get_reading("meter"))
    run(r.next_step())
    rb = run(r.record_spoken_reading("Capacitor garam ho raha hai.", "vbat_conn"))
    assert rb["status"] == "readback" and rb["point"] == "heat_check"


def test_a_spoken_value_answers_the_waiting_point_unless_it_names_another():
    r = run(started(session("not_charging"), symptom="not charging"))
    run(r.next_step())
    run(r.get_reading("meter"))  # vbus_port normal
    assert run(r.next_step())["measurement"] == "chg_ic_in"
    rb = run(r.record_spoken_reading("0.02 volt aa raha hai", "vbus_port"))
    assert rb["point"] == "chg_ic_in" and rb["say"] == "zero point zero two volts, Charging IC input"
    run(r.record_spoken_reading("nahi"))  # not a confirmation: a new reading replaces the read-back
    rb = run(r.record_spoken_reading("VBUS pe 0.02 volt", "vbus_port"))
    assert rb["point"] == "vbus_port"


def test_a_number_for_the_named_point_still_goes_to_that_point():
    r = run(started(session()))
    run(r.next_step())
    run(r.get_reading("meter"))
    run(r.next_step())  # pending: heat check
    rb = run(r.record_spoken_reading("0.4 volt", "vbat_cap_1"))
    assert rb["point"] == "vbat_cap_1"


def test_made_up_point_in_a_spoken_observation_uses_the_pending_check():
    # seen in the 4 Nov e2e run: the model passed point_id "vbat_cap" for the heat check
    r = run(started(session()))
    run(r.next_step())
    run(r.get_reading("meter"))
    assert run(r.next_step())["measurement"] == "heat_check"
    rb = run(r.record_spoken_reading("Capacitor garam ho raha hai", "vbat_cap"))
    assert rb["status"] == "readback" and rb["point"] == "heat_check" and rb["say"] == "capacitor hot"


def test_low_confidence_camera_read_never_reaches_the_ledger():
    r = run(started(session("glare_low_confidence")))
    out = run(r.get_reading("meter", "vbat_conn"))
    assert out["status"] == "unclear" and out["ask"] == "read_aloud" and out["conf"] < 0.9
    assert r.ledger.history == [] and r.awaiting_spoken == "vbat_conn"
    assert events(r, "reading.fallback")[-1]["reason"] == "unclear"


# --- record_spoken_reading: read back, then confirm ------------------------------------------


def test_spoken_reading_is_read_back_before_it_counts():
    r = run(started(session()))
    run(r.next_step())
    rb = run(r.record_spoken_reading("zero point zero two volt"))
    assert rb == {"status": "readback", "say": "zero point zero two volts, Battery connector", "point": "vbat_conn",
                  "conf": SPOKEN_CONFIDENCE, "ask": "confirm"}
    assert r.ledger.history == []
    done = run(r.record_spoken_reading("haan"))
    assert done["recorded"] == "zero point zero two volts, Battery connector" and done["class"] == "short"
    assert done["conf"] == CONFIRMED_CONFIDENCE and r.ledger.history[-1].observation.confidence == CONFIRMED_CONFIDENCE


def test_spoken_reading_answers_the_point_awaiting_a_read_aloud():
    r = run(started(session("glare_low_confidence")))
    run(r.get_reading("meter", "vbat_cap_1"))
    rb = run(r.record_spoken_reading("OL aa raha hai"))
    assert rb["point"] == "vbat_cap_1" and rb["say"] == "OL, open, Large capacitor beside the battery connector"
    run(r.record_spoken_reading(confirm=True))
    assert r.awaiting_spoken is None and r.ledger.measured() == {"vbat_cap_1"}


@pytest.mark.parametrize(
    "text,expected",
    [
        ("kuch samajh nahi aaya", {"status": "not_understood", "ask": "repeat"}),
        ("forty volt", {"status": "check_value", "heard": "forty volts", "ask": "repeat"}),  # diode mode tops out ~3 V
    ],
)
def test_spoken_reading_that_cannot_count(text, expected):
    r = run(started(session()))
    assert run(r.record_spoken_reading(text, "vbat_conn")) == expected
    assert r.readback_pending is None


def test_spoken_reading_needs_a_point():
    r = run(started(session()))
    assert run(r.record_spoken_reading("0.4 volt")) == {"error": "which_point", "ask": "which_point"}
    assert run(r.record_spoken_reading("0.4 volt", "nowhere")) == {"error": "which_point", "ask": "which_point"}


def test_spoken_observation_uses_the_pack_keywords():
    r = run(started(session()))
    rb = run(r.record_spoken_reading("cable theek hai", "cable_check"))
    assert rb["status"] == "readback" and rb["say"] == "cable OK"
    assert run(r.record_spoken_reading(confirm=True))["class"] == "ok"
    bad = run(r.record_spoken_reading("pata nahi", "cable_check"))
    assert bad["status"] == "not_understood" and bad["options"] == ["faulty", "ok"]


def test_boot_pattern_cannot_be_spoken():
    r = run(started(session()))
    assert run(r.record_spoken_reading("short hai", "boot_pattern")) == {"error": "use_watch", "ask": "watch_boot_current"}


def test_a_powered_observation_needs_the_approval_at_confirmation():
    r = run(started(session()))
    run(r.record_spoken_reading("capacitor garam", "heat_check"))
    assert run(r.record_spoken_reading(confirm=True)) == {"error": "power_step_required", "step": "connect_boot_cable"}
    assert "heat_check" not in r.ledger.measured()


# --- next_step ---------------------------------------------------------------------------------


def test_next_step_names_point_mode_reason_and_the_tool_to_use():
    r = run(started(session()))
    out = run(r.next_step())
    assert out["step"] == "measure" and out["mode"] == "diode" and out["where"] and out["probes"]
    assert out["reason"].startswith("separates ")
    assert "use" not in out  # points: get_reading, or record_spoken_reading when the value is said


def test_a_watch_is_refused_while_another_check_is_waiting():
    r = run(started(session()))
    run(r.next_step())
    run(r.get_reading("meter"))
    step = run(r.next_step())  # the heat check, approved
    assert step["use"] == "record_spoken_reading" and r.approvals.pending
    assert run(r.watch_boot_current()) == {"error": "not_this_step", "measurement": "heat_check", "use": "record_spoken_reading"}
    assert r.approvals.pending is not None  # still there for the heat check


def test_a_power_step_comes_with_the_gates_answer():
    r = run(started(session()))
    run(r.next_step())
    run(r.get_reading("meter"))  # vbat_conn short
    out = run(r.next_step())
    assert out["step"] == "power" and out["power_step"] == "connect_boot_cable" and out["then"] == "measure"
    assert out["approved"] is True and out["say"] == APPROVED["connect_boot_cable"]
    assert "reason" not in out and "set" not in out
    assert r.approvals.pending.step == "connect_boot_cable" and events(r, "power.approved")


def test_a_blocked_power_step_says_why_and_grants_nothing():
    r = run(started(session(), battery=None))
    run(r.next_step())
    run(r.get_reading("meter"))
    out = run(r.next_step())
    assert out["step"] == "power" and out["approved"] is False and out["blocked"] == "battery_not_removed"
    assert out["say"]["hi"] == "Pehle battery nikaalo, phir batao."
    assert r.approvals.pending is None


def test_a_power_step_line_stays_short_in_hindi():
    from engine.tools import MAX_OUTPUT_CHARS, one_line

    r = session()
    run(r.on_user_transcript("battery nikal di hai", "hinglish"))
    run(started(r))
    run(r.next_step())
    run(r.get_reading("meter"))
    out = run(r.next_step())
    assert out["step"] == "power" and len(one_line(out)) <= MAX_OUTPUT_CHARS


def test_next_step_waits_for_a_pending_readback():
    r = run(started(session()))
    run(r.record_spoken_reading("0.4 volt", "vbat_conn"))
    assert run(r.next_step()) == {"step": "confirm_readback", "say": "zero point four volts, Battery connector",
                                  "use": "record_spoken_reading", "confirm": True}


@pytest.mark.parametrize("said", ["Haan, sahi hai.", "हाँ, सही है।", "Yes, that's right."])
def test_the_users_own_yes_confirms_when_the_model_asks_for_the_next_step(said):
    r = run(started(session()))
    run(r.next_step())
    run(r.record_spoken_reading("0.02 volt"))
    run(r.on_user_transcript(said, "hinglish"))
    out = run(r.next_step())
    assert out["recorded"].startswith("zero point zero two volts") and out["class"] == "short"
    assert out["step"] == "power" and r.readback_pending is None and "vbat_conn" in r.ledger.measured()


@pytest.mark.parametrize("said", ["nahi, 0.2 hai", "ruko", "zero point two"])
def test_anything_but_a_plain_yes_keeps_the_readback_pending(said):
    r = run(started(session()))
    run(r.next_step())
    run(r.record_spoken_reading("0.02 volt"))
    run(r.on_user_transcript(said, "hinglish"))
    assert run(r.next_step())["step"] == "confirm_readback" and r.ledger.history == []


def test_next_step_before_start_and_in_hindi():
    r = session()
    assert run(r.next_step()) == {"error": "start_repair_first"}
    run(r.on_user_transcript("battery nikal di", "hi"))
    run(started(r))
    out = run(r.next_step())
    assert "mein fark karega" in out["reason"] and out["where"] != r.pack.point("vbat_conn").label.en


# --- request_power_step: the gate before every power step ------------------------------------


def test_power_step_approved_with_the_exact_sentence():
    r = run(started(session()))
    out = run(r.request_power_step("connect_boot_cable"))
    assert out == {"approved": True, "step": "connect_boot_cable", "say": APPROVED["connect_boot_cable"]}
    assert r.approvals.pending.step == "connect_boot_cable"
    assert events(r, "power.approved")


@pytest.mark.parametrize(
    "setup,reason",
    [
        ({"battery": "connected"}, "battery_not_removed"),
        ({"battery": None}, "battery_not_removed"),
        ({"supply": False, "scenario": "camera_unavailable"}, "no_supply_reading"),
    ],
)
def test_power_step_blocked(setup, reason):
    r = session(setup.get("scenario", "short_battery_line"))
    run(started(r, battery=setup.get("battery", "removed"), supply=setup.get("supply", True)))
    out = run(r.request_power_step("connect_boot_cable"))
    assert out["approved"] is False and out["reason"] == reason and out["say"]["en"] and out["say"]["hi"]
    assert r.approvals.pending is None and events(r, "power.blocked")


def test_power_step_reads_the_supply_itself_when_there_is_no_reading():
    r = run(started(session(), supply=False))
    out = run(r.request_power_step("connect_boot_cable"))
    assert out["approved"] is True and r.supply is not None and r.supply.age_s() < 1
    assert [e.kind for e in r.evidence] == ["supply"]


def test_power_step_rereads_a_supply_reading_close_to_stale():
    r = run(started(session()))
    old = r.supply
    r.supply.created_at -= timedelta(seconds=16)  # still fresh for the gate, but not for long
    assert run(r.request_power_step("connect_boot_cable"))["approved"] is True
    assert r.supply is not old and r.supply.age_s() < 1


def test_fresh_supply_reading_is_not_reread():
    r = run(started(session()))
    old = r.supply
    run(r.request_power_step("connect_boot_cable"))
    assert r.supply is old


def test_power_step_blocked_on_a_stale_reading_when_the_camera_is_down():
    r = run(started(session()))
    r.supply.created_at -= timedelta(seconds=25)
    r.bench.set_scenario("camera_unavailable")
    out = run(r.request_power_step("connect_boot_cable"))
    assert out["approved"] is False and out["reason"] == "stale_reading"


def test_no_supply_read_for_a_step_the_battery_blocks_anyway():
    r = run(started(session(), battery="connected", supply=False))
    assert run(r.request_power_step("connect_boot_cable"))["reason"] == "battery_not_removed"
    assert r.evidence == []


def test_approval_is_single_use():
    r = run(started(session()))
    run(r.request_power_step("connect_boot_cable"))
    assert "pattern" in run(r.watch_boot_current())
    assert run(r.watch_boot_current()) == {"error": "power_step_required", "step": "connect_boot_cable"}


def test_approval_is_for_one_step_only():
    r = run(started(session("not_charging"), symptom="not charging"))
    run(r.request_power_step("connect_boot_cable"))
    assert run(r.get_reading("port_current")) == {"error": "power_step_required", "step": "connect_port_supply"}


def test_barge_in_cancels_the_pending_approval():
    r = run(started(session()))
    run(r.request_power_step("connect_boot_cable"))
    r.on_barge_in()
    assert r.approvals.pending is None and events(r, "power.cancelled")[-1]["step"] == "connect_boot_cable"
    assert run(r.watch_boot_current())["error"] == "power_step_required"


def test_barge_in_without_a_pending_step_is_harmless():
    r = run(started(session()))
    r.on_barge_in()
    assert events(r, "power.cancelled") == []


# --- watch_boot_current and the at-limit watchdog ---------------------------------------------


def test_watch_classifies_the_boot_pattern():
    r = run(started(session()))
    run(r.request_power_step("connect_boot_cable"))
    out = run(r.watch_boot_current("on_connect"))
    assert out["pattern"] == "SHORT_IMMEDIATE" and out["peak_a"] > 0.3 and out["meaning"]
    assert events(r, "watch.sample")


def test_current_at_the_limit_for_three_seconds_means_disconnect_now():
    r = run(started(session()))
    r.bench.truth = lambda mid: "AT_LIMIT" if mid == "boot_pattern" else "normal"  # type: ignore[method-assign]
    run(r.request_power_step("connect_boot_cable"))
    out = run(r.watch_boot_current())
    assert out["urgent"] == "disconnect_now" and out["say"] == DISCONNECT_NOW["en"] and out["pattern"] == "AT_LIMIT"
    samples = events(r, "watch.sample")
    assert samples[-1]["t_s"] > 3.0 and samples[-1]["t_s"] < r.pack.boot.thresholds.watch_s  # stopped early
    assert events(r, "urgent.disconnect")


def test_cancel_word_stops_a_running_watch():
    async def scenario():
        r = RepairSession("se_r", FakeBench(time_scale=1, sample_s=0.05, supply_delay_s=0))
        await started(r)
        await r.request_power_step("connect_boot_cable")
        task = asyncio.create_task(r.watch_boot_current())
        await asyncio.sleep(0.08)
        await r.on_user_transcript("ruko ruko", "hi")
        return await task, r

    out, r = run(scenario())
    assert out == {"status": "cancelled"} and events(r, "watch.cancelled")


# --- safety stop -----------------------------------------------------------------------------


def test_battery_danger_stops_the_session_and_injects_the_stop_sentence():
    rec = Recorder()
    r = run(started(session(rec=rec)))
    run(r.request_power_step("connect_boot_cable"))
    run(r.on_user_transcript("battery bahut garam ho gayi", "hinglish"))
    assert r.stopped and r.state == "stopped" and r.approvals.pending is None
    assert rec.injected[-1]["urgent"] is True and STOP_MESSAGE["hi"] in rec.injected[-1]["text"]
    assert run(r.request_power_step("connect_boot_cable"))["reason"] == "session_stopped"


def test_reassurance_does_not_stop_the_session():
    r = run(started(session()))
    run(r.on_user_transcript("battery garam nahi hai", "hinglish"))
    assert r.stopped is None


def test_battery_words_update_the_battery_state():
    r = run(started(session(), battery=None))
    run(r.on_user_transcript("battery nikal di hai", "hinglish"))
    assert r.battery == "removed" and events(r, "battery")[-1]["state"] == "removed"


# --- explain ---------------------------------------------------------------------------------


@pytest.mark.parametrize("topic", ["vbat_cap_short", "diode mode", "SHORT_IMMEDIATE", "ol"])
def test_explain_known_topics(topic):
    r = run(started(session()))
    out = run(r.explain(topic))
    assert out["text"]["en"] and out["text"]["hi"] and out["text"]["en"] != out["text"]["hi"]


def test_explain_why_uses_the_current_step_and_unknown_asks():
    r = run(started(session()))
    step = run(r.next_step())
    assert run(r.explain("why"))["text"]["en"] == step["reason"]
    assert run(r.explain("quantum flux")) == {"text": None, "ask": "which_topic"}


def test_explain_works_before_a_repair_starts():
    assert run(session().explain("diode_mode"))["text"]["en"]


def test_explain_gives_both_languages_within_the_output_limit():
    from engine.tools import MAX_OUTPUT_CHARS, one_line

    r = run(started(session()))
    for topic in [f.id for f in r.pack.faults] + list(r.pack.glossary) + [p.id for p in r.pack.boot.patterns]:
        for level in ("trainee", "expert"):
            out = run(r.explain(topic, level))
            assert set(out["text"]) == {"en", "hi"} and len(one_line(out)) <= MAX_OUTPUT_CHARS, topic


# --- the fix and close_repair ------------------------------------------------------------------


async def _to_fix(r: RepairSession) -> dict:
    await started(r)
    await r.next_step()
    await r.get_reading("meter")  # vbat_conn short
    await r.request_power_step("connect_boot_cable")
    await r.record_spoken_reading("capacitor garam", "heat_check")
    await r.record_spoken_reading(confirm=True)
    return await r.next_step()


def test_fix_proposal_names_the_reading_that_must_recover():
    r = session()
    fix = run(_to_fix(r))
    assert fix["step"] == "fix" and fix["fault"] == "vbat_cap_short" and fix["fix"] and fix["then_check"] == "vbat_conn"
    assert r.state == "fix" and events(r, "fix.proposed")[-1]["check"] == "vbat_conn"


def test_fixed_needs_a_healthy_reading_of_the_check_after_the_fix():
    r = session()
    run(_to_fix(r))
    assert run(r.close_repair("fixed")) == {"status": "confirm_first", "measure": "vbat_conn", "ask": "confirming_reading", "point": "vbat_conn"}
    still_short = run(r.get_reading("meter"))  # no fix applied on the bench yet
    assert still_short["fix_check"] == "fail" and "top" not in still_short
    assert run(r.close_repair("fixed"))["status"] == "confirm_first"
    r.bench.apply_fix()
    assert run(r.get_reading("meter"))["fix_check"] == "pass"
    closed = run(r.close_repair("fixed"))
    assert closed["outcome"] == "fixed" and closed["fault"] == "vbat_cap_short" and r.state == "closed"


def test_readings_after_the_fix_never_move_the_diagnosis():
    r = session()
    run(_to_fix(r))
    before = r.ledger.posterior
    r.bench.apply_fix()
    run(r.request_power_step("connect_boot_cable"))
    run(r.watch_boot_current())
    assert r.ledger.posterior == before


def test_close_unresolved_and_abandoned_save_a_record():
    rec = Recorder()
    r = run(started(session(rec=rec)))
    out = run(r.close_repair("unresolved", note="customer left"))
    assert out["outcome"] == "unresolved" and out["record"].startswith("rr_")
    (row,) = [row for t, row in rec.rows if t == "repair_records"]
    assert row["note"] == "customer left" and row["pack_id"] == "demo_model"


def test_close_before_start_is_an_error():
    assert run(session().close_repair("abandoned")) == {"error": "start_repair_first"}


# --- degraded camera mode ----------------------------------------------------------------------


def test_camera_down_means_degraded_mode_and_no_power_steps_offered():
    fake_bench.SCENARIOS["cam_down_short"] = Scenario("cam_down_short", "vbat_cap_short", "dead", camera_down=True)
    try:
        r = run(started(session("cam_down_short")))
        assert r.degraded == "camera_unavailable" and events(r, "degraded")
        said = {"short": "0.02 volt", "normal": "0.4 volt", "ok": "cable theek hai"}
        for _ in range(12):
            step = run(r.next_step())
            assert step.get("degraded") == "camera_unavailable"
            if step["step"] != "measure":
                break
            assert "power_step" not in step
            run(r.record_spoken_reading(said[r.bench.truth(step["measurement"])], step["measurement"]))
            run(r.record_spoken_reading(confirm=True))
        # capacitor vs power IC needs the powered heat check: without it the engine must not guess
        assert step["step"] == "unresolved"
        assert run(r.request_power_step("connect_boot_cable"))["approved"] is False
    finally:
        del fake_bench.SCENARIOS["cam_down_short"]


def test_degraded_mode_clears_when_the_camera_comes_back():
    r = run(started(session("camera_unavailable")))
    assert r.degraded == "camera_unavailable"
    r.bench.set_scenario("no_current")
    run(r.get_reading("supply"))
    assert r.degraded is None and events(r, "degraded")[-1]["reason"] is None


# --- summary (dev endpoint) ----------------------------------------------------------------------


def test_summary_reflects_the_session():
    r = run(started(session()))
    run(r.next_step())
    s = r.summary()
    assert s["state"] == "probing" and s["pack"] == "demo_model" and s["pending"] == "vbat_conn"
    assert s["battery"] == "removed" and s["evidence"] == 1  # the supply read
