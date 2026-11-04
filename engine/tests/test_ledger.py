from __future__ import annotations

import math
import random

import pytest

from engine.ledger import (
    CONFLICT_DROP,
    CONFLICT_OTHER,
    OTHER,
    STOP_SECOND,
    STOP_TOP,
    Ledger,
    Observation,
    candidates,
    information_gain,
    next_step,
)
from mcp_evidence.packs import load_pack
from mcp_evidence.packs.schema import Pack

DEMO = load_pack("demo_model")
GENERAL = load_pack("general_dead_phone")


def obs(i, mid, outcome, c=0.95):
    return Observation(f"ev{i}", mid, outcome, c)


def tiny_pack(other_prior=0.05, faults=None, measurements=None) -> Pack:
    """A two-fault pack for exact threshold tests."""
    base = DEMO.model_dump(by_alias=True)
    base["ledger"]["other_prior"] = other_prior
    base["measurements"] = measurements or [
        {"id": "m1", "kind": "observation", "healthy": "a", "placeholder": True, "cost": 1, "label": {"en": "m1", "hi": "m1"},
         "classes": ["a", "b"], "keywords": {"a": ["a"], "b": ["b"]}},
        {"id": "boot_pattern", "kind": "boot_pattern", "placeholder": True, "cost": 3,
         "label": {"en": "boot", "hi": "boot"}, "classes": list(base["measurements"][1]["classes"])},
    ]
    base["faults"] = faults or [
        {"id": "f1", "family": "general", "placeholder": True, "name": {"en": "F1", "hi": "F1"}, "priors": {"dead": 0.5},
         "predicts": {"m1": "a"}, "fix": {"en": "x", "hi": "x"}, "explain": {"trainee": {"en": "x", "hi": "x"}, "expert": {"en": "x", "hi": "x"}}},
        {"id": "f2", "family": "general", "placeholder": True, "name": {"en": "F2", "hi": "F2"}, "priors": {"dead": 0.5},
         "predicts": {"m1": "b"}, "fix": {"en": "x", "hi": "x"}, "explain": {"trainee": {"en": "x", "hi": "x"}, "expert": {"en": "x", "hi": "x"}}},
    ]
    base["points"] = []
    for pat in base["boot"]["patterns"]:
        pat["next_checks"] = []
    return Pack.model_validate(base)


# --- the update rule ------------------------------------------------------------------------


def test_priors_follow_the_symptom_and_reserve_other():
    led = Ledger(DEMO, "dead")
    assert led.posterior[OTHER] == pytest.approx(DEMO.ledger.other_prior)
    assert math.isclose(sum(led.posterior.values()), 1.0)
    assert led.posterior["vbat_cap_short"] > led.posterior["port_damaged"]
    nc = Ledger(DEMO, "not_charging")
    assert nc.posterior["port_damaged"] > nc.posterior["vbat_cap_short"]


def test_likelihood_formula():
    led = Ledger(DEMO, "dead")
    k = len(DEMO.measurement("vbat_conn").classes)
    assert led.likelihood("vbat_cap_short", "vbat_conn", "short", 0.8) == pytest.approx(0.8 + 0.2 / k)
    assert led.likelihood("vbat_cap_short", "vbat_conn", "normal", 0.8) == pytest.approx(0.2 / k)
    assert led.likelihood(OTHER, "vbat_conn", "short", 0.8) == pytest.approx(1 / k)
    assert led.likelihood("bad_boot_cable", "port_current", "none", 0.8) == pytest.approx(1 / 3)  # no prediction


@pytest.mark.parametrize("seed", range(40))
def test_probabilities_always_sum_to_one(seed):
    rng = random.Random(seed)
    led = Ledger(DEMO, rng.choice(["dead", "not_charging"]))
    for i in range(rng.randint(1, 15)):
        m = rng.choice(DEMO.measurements)
        snap = led.add(obs(i, m.id, rng.choice(m.classes), rng.uniform(0, 1)))
        assert math.isclose(sum(snap.after.values()), 1.0, abs_tol=1e-12)
        assert all(0 <= p <= 1 for p in snap.after.values())


@pytest.mark.parametrize("measurement,outcome,fault", [("vbat_conn", "short", "vbat_cap_short"), ("vbus_port", "open", "port_damaged"), ("pwr_key", "open", "power_key_line")])
def test_higher_confidence_moves_probability_more(measurement, outcome, fault):
    moves = []
    for c in (0.3, 0.5, 0.7, 0.85, 0.95):
        led = Ledger(DEMO, "dead")
        before = led.posterior[fault]
        moves.append(led.add(obs(0, measurement, outcome, c)).after[fault] - before)
    assert moves == sorted(moves) and moves[0] > 0


def test_zero_confidence_changes_nothing():
    led = Ledger(DEMO, "dead")
    before = led.posterior
    after = led.add(obs(0, "vbat_conn", "short", 0.0)).after
    assert all(after[k] == pytest.approx(before[k]) for k in before)


def test_same_evidence_same_order_same_result():
    seq = [obs(0, "vbat_conn", "short", 0.9), obs(1, "heat_check", "ic_hot", 0.8), obs(2, "cable_check", "ok", 0.7)]
    runs = []
    for _ in range(3):
        led = Ledger(DEMO, "dead")
        for o in seq:
            led.add(o)
        runs.append(led.posterior)
    assert runs[0] == runs[1] == runs[2]


def test_a_remeasure_supersedes_the_earlier_reading():
    led = Ledger(DEMO, "dead")
    led.add(obs(0, "vbat_conn", "normal"))
    snap = led.add(obs(1, "vbat_conn", "short"))
    fresh = Ledger(DEMO, "dead")
    fresh.add(obs(1, "vbat_conn", "short"))
    assert snap.superseded == "ev0"
    assert led.posterior == pytest.approx(fresh.posterior)
    assert [o.evidence_id for o in led.active] == ["ev1"]


def test_snapshot_records_what_moved():
    led = Ledger(DEMO, "dead")
    snap = led.add(obs(0, "vbat_conn", "short"))
    top_moves = dict(snap.moved(3))
    assert top_moves["vbat_cap_short"] > 0
    assert sum(snap.deltas.values()) == pytest.approx(0, abs=1e-12)
    assert snap.influence > 0 and snap.index == 0


def test_unknown_measurement_or_class_is_rejected():
    led = Ledger(DEMO, "dead")
    with pytest.raises(KeyError):
        led.add(obs(0, "nope", "short"))
    with pytest.raises(ValueError):
        led.add(obs(0, "vbat_conn", "melted"))


# --- stop rule: exactly at the thresholds ---------------------------------------------------


def _set_posterior(led: Ledger, post: dict[str, float]) -> None:
    led._posterior = dict(post)


@pytest.mark.parametrize(
    "top,second,expected",
    [
        (STOP_TOP, 0.05, False),  # not strictly above
        (STOP_TOP + 1e-9, 0.05, True),
        (0.9, STOP_SECOND, False),  # second not strictly below
        (0.89, STOP_SECOND - 1e-9, True),
    ],
)
def test_stop_rule_thresholds(top, second, expected):
    led = Ledger(tiny_pack(), "dead")
    _set_posterior(led, {"f1": top, "f2": second, OTHER: 1 - top - second})
    assert led.stop() is expected


def test_stop_never_proposes_other():
    led = Ledger(tiny_pack(), "dead")
    _set_posterior(led, {"f1": 0.02, "f2": 0.02, OTHER: 0.96})
    assert not led.stop()


# --- conflict rule: exactly at the thresholds ------------------------------------------------


def test_conflict_when_other_rises_above_threshold():
    # f1 and f2 predict opposite classes; a high-confidence reading of a class neither predicts
    # pushes "other" up. With priors 0.5/0.5 the posterior of other follows from the formula.
    meas = [
        {"id": "m1", "kind": "observation", "healthy": "a", "placeholder": True, "cost": 1, "label": {"en": "m1", "hi": "m1"},
         "classes": ["a", "b", "c"], "keywords": {"a": ["a"], "b": ["b"], "c": ["c"]}},
        {"id": "boot_pattern", "kind": "boot_pattern", "placeholder": True, "cost": 3,
         "label": {"en": "boot", "hi": "boot"}, "classes": ["NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT"]},
    ]

    def other_after(other_prior, c):
        led = Ledger(tiny_pack(other_prior=other_prior, measurements=meas), "dead")
        return led.add(obs(0, "m1", "c", c))

    snap = other_after(0.2, 0.95)
    assert snap.after[OTHER] > CONFLICT_OTHER and snap.conflict and snap.conflict.reason == "other_high"
    calm = other_after(0.02, 0.5)
    assert calm.after[OTHER] <= CONFLICT_OTHER and calm.conflict is None


def _leader_drop_case(c: float):
    led = Ledger(tiny_pack(), "dead")
    led.add(obs(0, "m1", "a", 0.6))  # f1 leads
    return led, led.add(obs(1, "m1", "b", c))  # supersedes: f1 drops


def test_conflict_when_a_confident_reading_drops_the_leader_by_half():
    led, snap = _leader_drop_case(0.95)
    drop = (snap.before["f1"] - snap.after["f1"]) / snap.before["f1"]
    assert drop > CONFLICT_DROP
    assert snap.conflict and snap.conflict.reason == "leader_drop"


def test_no_leader_drop_conflict_below_confidence_threshold():
    _, snap = _leader_drop_case(0.89)
    assert snap.conflict is None or snap.conflict.reason != "leader_drop"


def test_leader_drop_threshold_is_strict():
    led = Ledger(tiny_pack(), "dead")
    led._posterior = {"f1": 0.6, "f2": 0.35, OTHER: 0.05}

    def fake_compute(_):
        return {"f1": 0.3, "f2": 0.65, OTHER: 0.05}  # exactly 50% drop

    led._compute = fake_compute  # type: ignore[method-assign]
    snap = led.add(obs(5, "m1", "b", 0.95))
    assert snap.conflict is None


def test_conflict_names_the_most_influential_earlier_reading():
    led = Ledger(DEMO, "dead")
    led.add(obs(0, "cable_check", "ok", 0.6))  # weak
    led.add(obs(1, "vbat_conn", "short", 0.95))  # strong: makes the short faults lead
    snap = led.add(obs(2, "vbat_cap_1", "normal", 0.95))  # contradicts the leader
    assert snap.conflict is not None
    assert snap.conflict.remeasure_measurement == "vbat_conn"
    assert snap.conflict.remeasure_evidence == "ev1"
    assert not snap.stop


# --- next step: value of information per unit cost ------------------------------------------


def test_information_gain_is_zero_for_a_measurement_nobody_disagrees_on():
    led = Ledger(tiny_pack(), "dead")
    assert information_gain(led, "boot_pattern", 0.9) == pytest.approx(0.0, abs=1e-12)
    assert information_gain(led, "m1", 0.9) > 0


def test_next_step_prefers_cheap_informative_checks():
    led = Ledger(DEMO, "dead")
    step = next_step(led)
    by_id = {s.measurement_id: s for s in candidates(led)}
    assert step.score == max(s.score for s in by_id.values())
    assert by_id["boot_pattern"].gain_bits > 0
    assert step.measurement_id == "vbat_conn"  # cost 1 beats the costlier power-up
    assert step.point_id == "vbat_conn" and step.mode == "diode"


def test_cost_divides_the_gain():
    led = Ledger(DEMO, "dead")
    for s in candidates(led):
        assert s.score == pytest.approx(s.gain_bits / DEMO.measurement(s.measurement_id).cost)


def test_ties_go_to_the_expert_order():
    meas = [
        {"id": "first", "kind": "observation", "healthy": "a", "placeholder": True, "cost": 1, "label": {"en": "1", "hi": "1"},
         "classes": ["a", "b"], "keywords": {"a": ["a"], "b": ["b"]}},
        {"id": "second", "kind": "observation", "healthy": "a", "placeholder": True, "cost": 1, "label": {"en": "2", "hi": "2"},
         "classes": ["a", "b"], "keywords": {"a": ["a"], "b": ["b"]}},
        {"id": "boot_pattern", "kind": "boot_pattern", "placeholder": True, "cost": 3,
         "label": {"en": "boot", "hi": "boot"}, "classes": ["NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT"]},
    ]
    faults = [
        {"id": f, "family": "general", "placeholder": True, "name": {"en": f, "hi": f}, "priors": {"dead": 0.5},
         "predicts": {"first": cls, "second": cls}, "fix": {"en": "x", "hi": "x"},
         "explain": {"trainee": {"en": "x", "hi": "x"}, "expert": {"en": "x", "hi": "x"}}}
        for f, cls in (("f1", "a"), ("f2", "b"))
    ]
    led = Ledger(tiny_pack(measurements=meas, faults=faults), "dead")
    s = {c.measurement_id: c for c in candidates(led)}
    assert s["first"].score == pytest.approx(s["second"].score)
    assert next_step(led).measurement_id == "first"


def test_next_step_skips_what_is_measured_and_reports_power_needs():
    led = Ledger(DEMO, "dead")
    led.add(obs(0, "vbat_conn", "short"))
    step = next_step(led)
    assert step.measurement_id != "vbat_conn"
    power = {s.measurement_id: s.requires_power for s in candidates(led)}
    assert power["boot_pattern"] == "connect_boot_cable" and power["vbat_cap_1"] is None


def test_reason_names_the_two_faults_it_separates():
    led = Ledger(DEMO, "dead")
    led.add(obs(0, "vbat_conn", "short"))
    step = next_step(led)
    assert step.measurement_id == "heat_check"
    assert set(step.separates) == {"vbat_cap_short", "pmic_vbat_short"}
    assert step.reason(led, "en").startswith("separates ")
    assert "fark karega" in step.reason(led, "hi")


def test_no_step_when_nothing_is_left_to_learn():
    led = Ledger(tiny_pack(), "dead")
    led.add(obs(0, "m1", "a"))
    assert next_step(led) is None  # boot_pattern separates nothing here


# --- every fault family reaches its root cause on scripted evidence --------------------------


def _scripted_run(pack: Pack, fault_id: str, symptom: str, confidence: float = 0.95, max_steps: int = 10):
    fault = next(f for f in pack.faults if f.id == fault_id)
    led = Ledger(pack, symptom)
    path = []
    for i in range(max_steps):
        step = next_step(led)
        if step is None:
            break
        outcome = fault.predicts.get(step.measurement_id) or "normal"
        snap = led.add(obs(i, step.measurement_id, outcome, confidence))
        path.append(step.measurement_id)
        if snap.stop:
            break
    return led, path


@pytest.mark.parametrize("pack", [DEMO, GENERAL], ids=lambda p: p.pack_id)
def test_every_fault_reaches_its_root_cause(pack):
    for fault in pack.faults:
        symptom = max(fault.priors, key=fault.priors.get)
        led, path = _scripted_run(pack, fault.id, symptom)
        assert led.stop(), (fault.id, path)
        assert led.ranked()[0][0] == fault.id, (fault.id, path, led.ranked()[:3])
        assert len(path) <= 5, (fault.id, path)


@pytest.mark.parametrize(
    "family,faults",
    [
        ("short_battery_line", ["vbat_cap_short", "pmic_vbat_short"]),
        ("no_current", ["bad_boot_cable", "open_vbat_path", "power_key_line"]),
        ("not_charging", ["port_damaged", "charge_path_part", "cable_or_charger"]),
    ],
)
def test_the_three_families_are_all_in_the_demo_pack(family, faults):
    assert sorted(f.id for f in DEMO.faults if f.family == family) == sorted(faults)


def test_lower_confidence_needs_at_least_as_many_steps():
    for fault in DEMO.faults:
        symptom = max(fault.priors, key=fault.priors.get)
        _, high = _scripted_run(DEMO, fault.id, symptom, 0.95)
        led, low = _scripted_run(DEMO, fault.id, symptom, 0.7)
        assert len(low) >= len(high)
        assert led.ranked()[0][0] == fault.id


# --- conflict rule: a conflict is between readings ---------------------------------------------


def test_first_reading_never_raises_leader_drop():
    # the leader came from the priors alone; there is no earlier reading to blame
    for symptom, mid, outcome in [("dead", "vbat_conn", "open"), ("not_charging", "vbus_port", "normal")]:
        led = Ledger(DEMO, symptom)
        leader, p_before = led.leader()
        snap = led.add(obs(0, mid, outcome, 0.97))
        assert (p_before - snap.after[leader]) / p_before > CONFLICT_DROP, mid  # would have counted before
        assert snap.conflict is None, mid


def test_confident_reread_that_contradicts_names_the_same_measurement():
    led = Ledger(tiny_pack(), "dead")
    led.add(obs(0, "m1", "a", 0.6))
    snap = led.add(obs(1, "m1", "b", 0.95))
    assert snap.conflict and snap.conflict.reason == "leader_drop"
    assert (snap.conflict.remeasure_measurement, snap.conflict.remeasure_evidence) == ("m1", "ev0")


def test_requested_remeasure_settles_instead_of_raising_a_new_conflict():
    led = Ledger(DEMO, "dead")
    led.add(obs(0, "vbat_conn", "short", 0.95))
    first = led.add(obs(1, "vbat_cap_1", "normal", 0.95))
    assert first.conflict and first.conflict.remeasure_measurement == "vbat_conn"
    unflagged = Ledger(DEMO, "dead")
    unflagged.add(obs(0, "vbat_conn", "short", 0.95))
    unflagged.add(obs(1, "vbat_cap_1", "normal", 0.95))
    plain = unflagged.add(obs(2, "vbat_conn", "normal", 0.95))
    settled = led.add(Observation("ev2", "vbat_conn", "normal", 0.95, remeasure=True))
    assert plain.after == settled.after  # the flag never changes the maths
    assert plain.conflict and plain.conflict.reason == "leader_drop"
    assert settled.conflict is None


def test_remeasure_flag_does_not_hide_other_high():
    meas = [
        {"id": "m1", "kind": "observation", "healthy": "a", "placeholder": True, "cost": 1, "label": {"en": "m1", "hi": "m1"},
         "classes": ["a", "b", "c"], "keywords": {"a": ["a"], "b": ["b"], "c": ["c"]}},
        {"id": "boot_pattern", "kind": "boot_pattern", "placeholder": True, "cost": 3,
         "label": {"en": "boot", "hi": "boot"}, "classes": ["NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT"]},
    ]
    led = Ledger(tiny_pack(other_prior=0.2, measurements=meas), "dead")
    snap = led.add(Observation("ev0", "m1", "c", 0.95, remeasure=True))
    assert snap.conflict and snap.conflict.reason == "other_high"
