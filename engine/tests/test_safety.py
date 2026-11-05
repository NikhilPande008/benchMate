from __future__ import annotations

import itertools
from datetime import timedelta

import pytest

from engine.safety import (
    APPROVED,
    MIN_CONFIDENCE,
    STALE_S,
    Approvals,
    AtLimitWatchdog,
    battery_report,
    check_power,
    device_out_of_scope,
)
from mcp_evidence.packs import load_pack
from mcp_evidence.schema import EvidenceRecord, FieldValue, utcnow

PACK = load_pack("demo_model")
SP = PACK.safe_power
NOW = utcnow()


def supply(volts, limit, v_conf=0.95, l_conf=0.95, age_s=1.0, live=0.0):
    return EvidenceRecord(
        session_id="se",
        kind="supply",
        source="camera",
        confidence=min(v_conf, l_conf),
        fields={
            "set_voltage": FieldValue(value=volts, unit="V", confidence=v_conf),
            "current_limit": FieldValue(value=limit, unit="A", confidence=l_conf),
            "live_current": FieldValue(value=live, unit="A", confidence=0.95),
        },
        created_at=NOW - timedelta(seconds=age_s),
    )


# --- table: every combination of voltage, limit, confidence and staleness -------------------

VOLTS = [None, 3.7, SP.voltage_min, 4.0, SP.voltage_max, 4.21, 5.0]
LIMITS = [None, 0.0, 1.0, SP.current_limit_max, 2.01, 3.0]
V_CONF = [0.95, MIN_CONFIDENCE, 0.89]
L_CONF = [0.95, MIN_CONFIDENCE, 0.89]
AGES = [0.0, 19.9, STALE_S, 45.0]


def expected_reason(v, lim, vc, lc, age):
    """The gate's documented order, restated."""
    if age >= STALE_S:
        return "stale_reading"
    if v is None:
        return "voltage_not_visible"
    if vc < MIN_CONFIDENCE:
        return "voltage_unclear"
    if lim is None or lim <= 0:
        return "limit_not_set"
    if lc < MIN_CONFIDENCE:
        return "limit_unclear"
    if not SP.voltage_min <= v <= SP.voltage_max:
        return "voltage_out_of_band"
    if lim > SP.current_limit_max:
        return "limit_too_high"
    return None


CASES = list(itertools.product(VOLTS, LIMITS, V_CONF, L_CONF, AGES))


@pytest.mark.parametrize("v,lim,vc,lc,age", CASES)
def test_power_gate_table(v, lim, vc, lc, age):
    res = check_power("connect_boot_cable", supply(v, lim, vc, lc, age), PACK, "removed", now=NOW)
    want = expected_reason(v, lim, vc, lc, age)
    assert res.reason == want
    assert res.approved is (want is None)
    assert set(res.say) == {"en", "hi"} and all(res.say.values())


def test_table_covers_every_outcome():
    reasons = {expected_reason(*c) for c in CASES}
    assert reasons == {
        None, "stale_reading", "voltage_not_visible", "voltage_unclear", "limit_not_set",
        "limit_unclear", "voltage_out_of_band", "limit_too_high",
    }
    assert len(CASES) == 7 * 6 * 3 * 3 * 4


# --- exact sentences and values to set -------------------------------------------------------


@pytest.mark.parametrize("step", list(APPROVED))
def test_approval_returns_the_exact_sentences(step):
    res = check_power(step, supply(4.0, 1.0), PACK, "removed", now=NOW)
    assert res.approved and res.say == APPROVED[step]


def test_boot_cable_sentence_is_fixed():
    assert APPROVED["connect_boot_cable"] == {"en": "Connect the boot cable now.", "hi": "Ab boot cable lagao."}


def test_voltage_block_says_the_value_to_set_and_the_value_read():
    res = check_power("connect_boot_cable", supply(5.0, 1.0), PACK, "removed", now=NOW)
    assert res.set_values == {"set_voltage": SP.voltage_target}
    assert res.say["en"] == "Set the supply to 4.0 volts. It reads 5.0 volts."
    assert res.say["hi"] == "Supply 4.0 volt pe set karo. Abhi 5.0 volt hai."


def test_limit_blocks_say_the_value_to_set():
    high = check_power("connect_boot_cable", supply(4.0, 3.0), PACK, "removed", now=NOW)
    assert high.reason == "limit_too_high" and high.set_values == {"current_limit": SP.current_limit_target}
    unset = check_power("connect_boot_cable", supply(4.0, None), PACK, "removed", now=NOW)
    assert unset.reason == "limit_not_set" and "1.0 amp" in unset.say["en"]


@pytest.mark.parametrize("battery", ["unknown", "connected"])
def test_battery_must_be_confirmed_removed(battery):
    res = check_power("connect_boot_cable", supply(4.0, 1.0), PACK, battery, now=NOW)
    assert res.reason == "battery_not_removed" and not res.approved


def test_other_blocks():
    assert check_power("connect_boot_cable", None, PACK, "removed").reason == "no_supply_reading"
    meter = EvidenceRecord(session_id="se", kind="meter", source="camera", confidence=0.99, value=4.0)
    assert check_power("connect_boot_cable", meter, PACK, "removed").reason == "no_supply_reading"
    assert check_power("connect_mains", supply(4.0, 1.0), PACK, "removed").reason == "unknown_step"
    stopped = check_power("connect_boot_cable", supply(4.0, 1.0), PACK, "removed", stopped="battery swollen")
    assert stopped.reason == "session_stopped"


def test_result_line_is_short():
    import json

    line = json.dumps(check_power("connect_boot_cable", supply(5.0, 3.0), PACK, "removed", now=NOW).line())
    assert len(line) < 300 and "\n" not in line


# --- approvals (lifetime rules in engine/tests/test_approval.py) ----------------------------


def test_approval_is_single_use():
    a = Approvals()
    a.grant("connect_boot_cable", (4.0, 1.0), NOW)
    assert a.consume("connect_boot_cable", NOW)
    assert not a.consume("connect_boot_cable", NOW)


def test_approval_is_for_one_step_and_lasts_20_s():
    a = Approvals()
    a.grant("connect_boot_cable", (4.0, 1.0), NOW)
    assert not a.valid("press_power_key", NOW)
    assert a.valid("connect_boot_cable", NOW + timedelta(seconds=19.9))
    assert not a.valid("connect_boot_cable", NOW + timedelta(seconds=20))


def test_barge_in_ends_a_pending_power_step():
    a = Approvals()
    a.grant("connect_boot_cable", (4.0, 1.0), NOW)
    ended = a.end("barge_in")
    assert ended and ended.step == "connect_boot_cable"
    assert not a.consume("connect_boot_cable", NOW)  # must be requested and checked again
    assert a.end("barge_in") is None


# --- scope: what the user says ---------------------------------------------------------------


@pytest.mark.parametrize(
    "text,report",
    [
        ("battery phooli hui hai", "danger"),
        ("the battery is swollen", "danger"),
        ("battery bahut garam hai", "danger"),
        ("बैटरी फूली हुई है", "danger"),
        ("board se dhuan aa raha hai", "danger"),
        ("I can see smoke", "danger"),
        ("battery garam nahi hai", None),
        ("dhuan nahi hai", None),
        ("battery nikal di hai", "removed"),
        ("battery removed, board on the boot cable", "removed"),
        ("phone ki battery hata di", "removed"),
        ("बैटरी निकाल दी", "removed"),
        ("no battery", "removed"),
        ("battery nahi nikali", "connected"),
        ("battery laga hua hai", "connected"),
        ("battery is connected", "connected"),
        ("battery connected nahi hai", "removed"),
        ("capacitor garam ho raha hai", None),
        ("supply four volt pe set hai", None),
        # Devanagari, as gpt-transcribe writes Hinglish speech (seen in the 4 Nov e2e run)
        ("सप्लाई सेट है, आगे बैटे ओ।", None),  # "aage" (ahead) is not "aag" (fire)
        ("सप्लाई सेट है, आगे बताओ", None),
        ("बोर्ड में आग लग गई", "danger"),
        ("धुआँ आ रहा है", "danger"),
        ("बैटरी गरम हो रही है", "danger"),
        ("बैटरी गरम नहीं है", None),
        ("डेमो फोन है, बिल्कुल डेड है, बैटरी निकल दी है।", "removed"),
        ("बैटरी निकाली हुई है", "removed"),
        ("बैटरी लगी हुई है", "connected"),
    ],
)
def test_battery_reports(text, report):
    assert battery_report(text) == report


@pytest.mark.parametrize(
    "device,refused",
    [("Samsung charger", "charger"), ("TV board", "tv"), ("laptop adapter", "adapter"), ("Redmi demo phone", None), ("phone not charging", None), (None, None)],
)
def test_device_scope(device, refused):
    assert device_out_of_scope(device) == refused


# --- at-limit watchdog -------------------------------------------------------------------------


def run_dog(samples, limit=1.0):
    dog = AtLimitWatchdog(limit_a=limit)
    return [t for t, c in samples if dog.feed(t, c)]


def test_watchdog_trips_after_more_than_three_seconds_at_the_limit():
    samples = [(i * 0.5, 0.99) for i in range(10)]  # 0.0 .. 4.5 s
    assert run_dog(samples) == [3.5]


def test_watchdog_does_not_trip_at_exactly_three_seconds():
    samples = [(i * 0.5, 0.99) for i in range(7)] + [(3.5, 0.2)]  # 0.0 .. 3.0 s at the limit
    assert run_dog(samples) == []


def test_watchdog_resets_when_current_drops():
    samples = [(i * 0.5, 0.99 if i != 4 else 0.3) for i in range(13)]
    assert run_dog(samples) == [6.0]  # the run restarts at 2.5 s; 5.5 s is exactly 3.0 s, not more


def test_watchdog_uses_the_limit_fraction():
    assert run_dog([(i * 0.5, 0.94) for i in range(12)]) == []
    assert run_dog([(i * 0.5, 0.95) for i in range(12)]) == [3.5]
