from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_evidence.boot import analyse, classify, features
from mcp_evidence.packs import (
    GENERAL_PACK,
    PACK_DIR,
    SCHEMA_VERSION,
    all_packs,
    classify_band,
    load_pack,
    model_lookup,
)
from mcp_evidence.packs import classify_keywords
from mcp_evidence.packs.schema import Pack
from mcp_evidence.schema import BootSample, BootTrace, EvidenceRecord

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import export_schemas  # noqa: E402

DEMO = load_pack("demo_model")


def test_both_packs_validate_and_are_versioned():
    ids = {p.pack_id for p in all_packs()}
    assert {"demo_model", GENERAL_PACK} <= ids
    for p in all_packs():
        assert p.schema_version == SCHEMA_VERSION


def test_committed_json_schemas_are_up_to_date():
    for name, schema in export_schemas.schemas().items():
        committed = (PACK_DIR / "schema" / name).read_text(encoding="utf-8")
        assert committed == export_schemas.render(schema), f"run scripts/export_schemas.py ({name})"


def _numbers_outside_placeholders(node, inside=False, path="$"):
    """Yield paths of numbers that are not inside an object marked placeholder: true."""
    if isinstance(node, dict):
        here = inside or node.get("placeholder") is True
        for k, v in node.items():
            if k in ("xy", "schema_version"):  # photo coordinates and the schema number are not measurements
                continue
            yield from _numbers_outside_placeholders(v, here, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _numbers_outside_placeholders(v, inside, f"{path}[{i}]")
    elif isinstance(node, (int, float)) and not isinstance(node, bool) and not inside:
        yield path


@pytest.mark.parametrize("pack_file", ["demo_model/pack.json", "general_dead_phone.json"])
def test_every_number_is_marked_placeholder(pack_file):
    raw = json.loads((PACK_DIR / pack_file).read_text(encoding="utf-8"))
    assert raw["placeholder"] is True
    assert list(_numbers_outside_placeholders(raw)) == []


def test_demo_pack_has_three_fault_families_and_about_six_points():
    assert {f.family for f in DEMO.faults} == {"short_battery_line", "no_current", "not_charging"}
    assert 5 <= len(DEMO.points) <= 8
    assert all(p.xy is not None for p in DEMO.points)


def test_general_flow_defines_all_boot_patterns_with_next_checks():
    general = load_pack(GENERAL_PACK)
    assert general.general
    assert {p.id for p in general.boot.patterns} == {"NO_CURRENT", "SHORT_IMMEDIATE", "RISE_THEN_DROP", "NORMAL_BOOT", "AT_LIMIT"}
    assert all(p.next_checks for p in general.boot.patterns)


@pytest.mark.parametrize("pack", all_packs(), ids=lambda p: p.pack_id)
def test_every_fault_predicts_every_point(pack):
    """Completeness rule (packs/README.md): a fault that leaves a point healthy must say "normal".
    A missing prediction counts as "no opinion" (1/K), which lets a fault that spells out its
    normal readings win on readings that say nothing about the real cause."""
    point_measurements = [m.id for m in pack.measurements if m.kind == "point"]
    for f in pack.faults:
        missing = [m for m in point_measurements if m not in f.predicts]
        assert missing == [], (f.id, missing)


@pytest.mark.parametrize("pack", all_packs(), ids=lambda p: p.pack_id)
def test_only_port_current_may_be_left_without_a_prediction(pack):
    """The rule beyond points: boot pattern and observations are predicted by every fault too.
    The one deliberate gap: a dead-phone fault has no opinion on charging-port current."""
    for f in pack.faults:
        missing = {m.id for m in pack.measurements if m.id not in f.predicts}
        assert missing <= {"port_current"}, (f.id, missing)
        if "port_current" in missing:
            assert f.family != "not_charging", f.id


def test_a_gap_in_the_predictions_picks_the_wrong_fault():
    """Why the rule exists. The board has a power key fault and every reading so far is normal.
    With power_key_line's "normal" predictions removed, those readings say nothing for it, and
    bad_boot_cable (which spells them out) takes the lead even though the cable reads OK."""
    from engine.ledger import Ledger, Observation

    raw = load_pack("demo_model").model_dump(by_alias=True)
    for f in raw["faults"]:
        if f["id"] == "power_key_line":  # keep only its distinguishing predictions
            f["predicts"] = {"pwr_key": "open", "boot_pattern": "NO_CURRENT"}
    gappy = Pack.model_validate(raw)
    evidence = [("boot_pattern", "NO_CURRENT"), ("cable_check", "ok"), ("vbat_conn", "normal"),
                ("fuse_coil", "normal"), ("vbat_cap_1", "normal")]

    def leader(pack):
        led = Ledger(pack, "dead")
        for i, (mid, out) in enumerate(evidence):
            led.add(Observation(f"e{i}", mid, out, 0.95))
        return led.leader()[0]

    assert leader(load_pack("demo_model")) == "power_key_line"
    assert leader(gappy) == "bad_boot_cable"


def test_pack_validation_rejects_inconsistent_predictions():
    raw = DEMO.model_dump(by_alias=True)
    raw["faults"][0]["predicts"]["vbat_conn"] = "melted"
    with pytest.raises(ValueError, match="not a class"):
        Pack.model_validate(raw)
    raw = DEMO.model_dump(by_alias=True)
    raw["faults"][0]["predicts"]["nope"] = "short"
    with pytest.raises(ValueError, match="unknown measurement"):
        Pack.model_validate(raw)
    raw = DEMO.model_dump(by_alias=True)
    raw["safe_power"]["voltage_target"] = 5.0
    with pytest.raises(ValueError, match="safe band"):
        Pack.model_validate(raw)


@pytest.mark.parametrize(
    "query,pack_id,matched",
    [("Redmi demo, dead", "demo_model", True), ("demo phone", "demo_model", True), ("Samsung A12", GENERAL_PACK, False), (None, GENERAL_PACK, False)],
)
def test_model_lookup(query, pack_id, matched):
    pack, ok = model_lookup(query)
    assert (pack.pack_id, ok) == (pack_id, matched)


@pytest.mark.parametrize(
    "value,is_open,cls",
    [(0.002, False, "short"), (0.049, False, "short"), (0.05, False, "low"), (0.35, False, "normal"), (0.6, False, "high"), (1.6, False, "open"), (None, True, "open")],
)
def test_point_bands(value, is_open, cls):
    assert classify_band(DEMO.point("vbat_conn").reference.bands, value, is_open) == cls


@pytest.mark.parametrize(
    "text,cls",
    [
        ("capacitor garam ho raha hai", "cap_hot"), ("IC is hot", "ic_hot"), ("kuch garam nahi hua", "none"),
        ("nothing hot", "none"), ("pata nahi", None),
        # said the way people say it (4 Nov e2e run): words in between the phrase's words
        ("Nothing is getting hot.", "none"), ("The capacitor is getting hot.", "cap_hot"),
        ("capacitor garam nahi ho raha", "none"), ("the IC is getting really hot", "ic_hot"),
        # mixed sentences ("not the capacitor, the IC is hot") can match the wrong class: the
        # read-back ("capacitor hot, sahi?") is what catches those
        ("hot", None),
    ],
)
def test_observation_keywords(text, cls):
    assert classify_keywords(DEMO.measurement("heat_check"), text) == cls


@pytest.mark.parametrize("text,cls", [("cable theek hai", "ok"), ("cable theek nahi hai", "faulty"), ("cable is loose", "faulty")])
def test_cable_check_negation_wins(text, cls):
    assert classify_keywords(DEMO.measurement("cable_check"), text) == cls


# --- boot classifier -------------------------------------------------------------------------


def trace(currents, limit=1.0, dt=0.5):
    return BootTrace(trigger="on_connect", limit_a=limit, samples=[BootSample(t_s=i * dt, current_a=c) for i, c in enumerate(currents)])


T = DEMO.boot.thresholds


@pytest.mark.parametrize(
    "currents,pattern",
    [
        ([0.0] * 9, "NO_CURRENT"),
        ([0.8] * 9, "SHORT_IMMEDIATE"),
        ([0.99] * 9, "AT_LIMIT"),  # 4 s at the limit
        ([0.0, 0.0, 0.05, 0.2, 0.25, 0.05, 0.0, 0.0, 0.0], "RISE_THEN_DROP"),
        ([0.0, 0.0, 0.05, 0.2, 0.25, 0.22, 0.2, 0.21, 0.2], "NORMAL_BOOT"),
    ],
)
def test_boot_patterns(currents, pattern):
    assert analyse(trace(currents), T).pattern == pattern


def test_at_limit_needs_more_than_the_threshold():
    just_under = [0.99] * 7 + [0.5, 0.5]  # 3.0 s at the limit, not more
    assert features(trace(just_under).samples, 1.0, T).at_limit_s == pytest.approx(3.0)
    assert classify(features(trace(just_under).samples, 1.0, T), T) != "AT_LIMIT"


def test_evidence_record_rejects_unknown_fields():
    with pytest.raises(ValueError):
        EvidenceRecord(session_id="se", kind="meter", source="camera", confidence=0.9, colour="red")


# --- healthy class (checks a fix) ---------------------------------------------------------------


@pytest.mark.parametrize("pack", all_packs(), ids=lambda p: p.pack_id)
def test_every_measurement_has_a_healthy_class_among_its_classes(pack):
    for m in pack.measurements:
        assert m.healthy in m.classes, m.id
    assert pack.measurement("port_current").healthy == "normal"  # "none" there is the fault


def test_observation_without_healthy_class_is_rejected():
    raw = load_pack("demo_model").model_dump(by_alias=True)
    obs_m = next(m for m in raw["measurements"] if m["kind"] == "observation")
    obs_m["healthy"] = None
    with pytest.raises(ValidationError, match="healthy class"):
        Pack.model_validate(raw)
    obs_m["healthy"] = "nope"
    with pytest.raises(ValidationError, match="not one of its classes"):
        Pack.model_validate(raw)
