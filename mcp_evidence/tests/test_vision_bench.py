"""The vision pipeline behind the engine (VisionBench), and every corner case of BUILD_GUIDE
"Corner cases → Vision and readings". Synthetic frames and a fixture Reader A: no API calls."""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import numpy as np
import pytest

from engine.session.repair import RepairSession
from mcp_evidence.fake_bench import SCENARIOS, FakeBench
from mcp_evidence.schema import EvidenceError, EvidenceRecord
from mcp_evidence.synth.dataset import make_sample
from mcp_evidence.vision.agreement import DisplaySpec, read_display
from mcp_evidence.vision.bench import VisionBench, meter_display
from mcp_evidence.vision.calibration import Roi
from mcp_evidence.vision.reader_a import FixtureAnswer, FixtureReaderA, ReadA


def run(coro):
    return asyncio.run(coro)


def bench(scenario="short_battery_line", **kw) -> VisionBench:
    return VisionBench(FakeBench(scenario, time_scale=0), time_scale=0.0, **kw)


async def started(vb: VisionBench, symptom="phone is dead") -> RepairSession:
    r = RepairSession("se_v", vb)
    await r.start_repair("demo", symptom, "removed")
    return r


def events(r: RepairSession, name: str) -> list[dict]:
    return [e for e in r.events if e["event"] == name]


# --- the normal path --------------------------------------------------------------------------


def test_supply_and_meter_are_read_through_the_camera():
    vb = bench()

    async def go():
        r = await started(vb)
        sup = await r.get_reading("supply")
        step = await r.next_step()
        met = await r.get_reading("meter", step.get("point"))
        return r, sup, step, met

    r, sup, step, met = run(go())
    assert sup["volts"] == 4.0 and sup["limit_a"] == 1.0 and sup["conf"] >= 0.9 and sup["ok"] is True
    assert vb.last["supply"].ok and all(f.status == "agreed" for f in vb.last["supply"].fields.values())
    assert met["point"] == step["point"] and "class" in met and met["conf"] >= 0.9
    cam = [e for e in r.evidence if e.kind == "meter"][-1]
    assert cam.source == "camera" and cam.value == float(vb.state.meter_text)


def test_frames_are_kept_with_the_evidence(tmp_path):
    vb = bench(frames_dir=tmp_path)
    rec = run(vb.read_supply("se_f"))
    assert isinstance(rec, EvidenceRecord) and rec.frame_paths and (tmp_path / "se_f").is_dir()


def test_the_gate_approves_on_an_agreed_camera_supply_read():
    vb = bench()

    async def go():
        r = await started(vb)
        return await r.request_power_step("connect_boot_cable")

    assert run(go())["approved"] is True


# --- corner cases: Vision and readings --------------------------------------------------------


def test_glare_on_the_display_asks_for_a_spoken_reading():
    vb = bench("glare_low_confidence")

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        return await r.get_reading("meter", step.get("point"))

    out = run(go())
    assert out.get("ask") == "read_aloud" or out.get("error") == "view_blocked"


@pytest.mark.parametrize("distortion", ["flicker", "mid_update"])
def test_flicker_and_mid_update_wait_for_two_stable_frames(distortion):
    """On labelled synthetic frames: a value is accepted only when two frames and both readers
    agree; never the old value of a changing display, never a flickering frame's digits."""
    wrong = 0
    accepted = 0
    for i in range(10):
        s = make_sample(7, i, "meter", distortion)
        q = np.array(s.fields["main"], np.float32)
        spec = DisplaySpec("meter", lambda img, q=q, g=np.array(s.glass, np.float32): {"display": Roi("d", g, kind="display").warp(img), "main": Roi("m", q).warp(img)})
        t = s.truth["main"]
        settled = "OL" if t.ol else f"{t.value:.{t.decimals}f}"
        shown = s.frame_truth["main"]

        def answer(frame_i, s=s, shown=shown):
            txt = shown[frame_i]
            if txt in ("mixed", "flicker"):
                return ReadA(False, None, None, "unknown", None, 0, 0.2)
            return ReadA(True, txt, None if txt == "OL" else float(txt), "OL" if txt == "OL" else s.extra["unit"], s.extra["mode"],
                         len(txt.split(".")[1]) if "." in txt else 0, 0.95)

        fx = FixtureReaderA(time_scale=0)
        for k in (1, 2):
            fx.register("meter", spec.crops(s.frames[k])["display"], FixtureAnswer({"main": answer(k)}, (0.0,)))

        async def nxt(k, s=s):
            return s.frames[k] if k < 3 else None

        res = run(read_display(spec, nxt, fx))
        f = res.fields["main"]
        if f.accepted:
            accepted += 1
            wrong += f.key != settled
    assert wrong == 0 and accepted >= 3


def test_decimal_point_disagreement_is_not_accepted():
    vb = bench()
    vb.state = replace(vb.state, meter_text="0.352")

    async def wrong_dp(display, crop, attempt=0):
        return {"main": ReadA(True, "3.52", 3.52, "diode_V", "diode", 2, 0.95)}

    vb.reader_a = type("A", (), {"read": staticmethod(wrong_dp)})()
    res = run(vb._read("meter", "diode"))
    assert not res.ok and res.fields["main"].status == "unclear" and res.a_calls == 2  # third frame, A asked again


def test_a_value_near_a_class_boundary_is_read_back(monkeypatch):
    vb = bench()

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        pid = step["point"]
        edge = next(b.hi for b in r.pack.point(pid).reference.bands if b.hi)

        async def near(session_id, point_id, expected_mode):
            return EvidenceRecord(session_id=session_id, kind="meter", point_id=point_id, mode="diode", value=round(edge * 1.02, 3),
                                  unit="diode_V", source="camera", simulated=True, confidence=0.95)

        monkeypatch.setattr(vb.truth, "read_meter", near)
        out = await r.get_reading("meter", pid)
        conf = await r.record_spoken_reading("haan", pid)
        return r, out, conf

    r, out, conf = run(go())
    assert out["status"] == "readback" and out["ask"] == "confirm"
    assert events(r, "readback")[-1]["why"] == "near_threshold" and "class" in conf
    assert [e for e in r.evidence if e.kind == "meter"][-1].confirmed


def test_meter_on_the_wrong_mode():
    vb = bench()

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        vb.state = replace(vb.state, knob="dcv")
        return await r.get_reading("meter", step["point"])

    out = run(go())
    assert out["error"] == "wrong_mode" and out["meter_on"] == "dcv" and out["set"] == "diode"
    assert "switch it to diode mode" in out["say"] or "diode mode pe karo" in out["say"]


def test_ol_is_valid_evidence_the_open_class(monkeypatch):
    vb = bench()

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()

        async def open_(session_id, point_id, expected_mode):
            return EvidenceRecord(session_id=session_id, kind="meter", point_id=point_id, mode="diode", value=None, open=True,
                                  unit="diode_V", source="camera", simulated=True, confidence=0.95)

        monkeypatch.setattr(vb.truth, "read_meter", open_)
        return await r.get_reading("meter", step["point"]), vb.state.meter_text

    out, shown = run(go())
    assert shown == "OL" and out["value"] == "OL" and out.get("class") == "open"


@pytest.mark.parametrize("value,text,unit,si", [(4700.0, "4.700", "kohm", 4700.0), (0.3521, "352.1", "mV", 0.3521), (352.0, "352.0", "ohm", 352.0)])
def test_auto_ranging_units_are_normalised(value, text, unit, si):
    mode = "ohm" if unit.endswith("ohm") else "dcv"
    assert meter_display(mode, value, False) == (text, unit)
    vb = bench()
    vb.state = replace(vb.state, meter_text=text, meter_unit=unit, meter_mode=mode)
    res = run(vb._read("meter", mode))
    assert res.ok and abs(res.fields["main"].si_value - si) < 1e-6


def test_camera_moved_pauses_camera_reads_and_asks_for_recalibration():
    vb = bench()
    vb.state = replace(vb.state, offset=(18, 0))

    async def go():
        r = await started(vb)
        levels = []
        vb.health.listeners.append(lambda lv, why: levels.append((lv, why)))
        out = await r.get_reading("supply")
        return r, out, levels

    r, out, levels = run(go())
    assert out["error"] == "camera_unavailable" and levels == [("spoken_only", "camera_moved")]
    assert r.degraded == "camera_unavailable"


def test_supply_current_above_its_own_limit_is_a_reader_error():
    vb = bench()
    vb.state = replace(vb.state, live_current="1.500", current_limit="1.00")
    rec = run(vb._read("supply"))
    assert rec.status == "implausible" and rec.reason == "reader_error"
    record = vb._supply_record("se", rec)
    assert all(f.confidence <= 0.3 for f in record.fields.values())  # the gate will block


def test_a_blank_or_dim_meter_asks_to_check_its_battery():
    vb = bench()

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        vb.state = replace(vb.state, meter_on=False)
        first = await r.get_reading("meter", step["point"])
        vb.state = replace(vb.state, meter_on=False)
        return first, await r.get_reading("meter", step["point"])

    first, second = run(go())
    assert first.get("ask") in ("read_aloud",) and second["error"] == "meter_blank" and second["ask"] == "check_meter_battery"


def test_a_hand_over_the_display_waits_then_asks_to_clear_the_view():
    vb = bench()

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        vb.state = replace(vb.state, hand=True)
        return await r.get_reading("meter", step["point"])

    out = run(go())
    assert out["error"] == "view_blocked" and out["ask"] == "clear_view"


def test_vision_api_down_degrades_to_reader_b_only_then_back():
    vb = bench()
    assert vb.oracle is not None
    vb.oracle.down = True

    async def go():
        r = await started(vb)
        levels = []
        vb.health.listeners.append(lambda lv, why: levels.append(lv))
        await r.get_reading("supply")
        await r.get_reading("supply")  # second failure: Reader B only
        sup = await r.get_reading("supply")
        gate = await r.request_power_step("connect_boot_cable")
        vb.oracle.down = False
        vb.health._last_probe = -1e9  # the next read probes Reader A
        await r.get_reading("supply")
        return r, levels, sup, gate

    r, levels, sup, gate = run(go())
    assert levels == ["reader_b_only", "full"]
    assert sup["conf"] <= 0.85  # one reader: below the gate's 0.9
    assert gate["approved"] is False and gate["reason"] in ("voltage_unclear", "limit_unclear")


def test_vision_timeout_hedges_then_times_out_and_degrades():
    vb = VisionBench(FakeBench("vision_timeout", time_scale=0), time_scale=0.001)
    run(vb._read("supply"))
    run(vb._read("supply"))
    assert vb.health.level == "reader_b_only" and vb.reader_a.inner.timeouts >= 2  # type: ignore[union-attr]


def test_camera_unplugged_is_camera_unavailable():
    vb = bench("camera_unavailable")
    out = run(vb.read_supply("se"))
    assert isinstance(out, EvidenceError) and out.error == "camera_unavailable" and vb.health.level == "spoken_only"


# --- the engine hears about the ladder ----------------------------------------------------------


def test_the_engine_tells_the_model_and_the_screen_when_the_ladder_changes():
    vb = bench()
    notes = []

    async def inject(msg):
        notes.append(msg["text"])

    async def go():
        r = RepairSession("se_l", vb, inject=inject)
        vb.health.listeners.append(lambda lv, why: asyncio.get_running_loop().create_task(r.on_vision_level(lv, why)))
        await r.start_repair("demo", "phone is dead", "removed")
        vb.oracle.down = True
        await r.get_reading("supply")
        await r.get_reading("supply")
        await asyncio.sleep(0)
        return r

    r = run(go())
    assert events(r, "vision.level")[-1]["level"] == "reader_b_only"
    assert any("Camera reading limited" in n for n in notes)


# --- pre-read ------------------------------------------------------------------------------------


def test_pre_read_answers_get_reading_from_the_cache():
    vb = VisionBench(FakeBench("short_battery_line", time_scale=0), time_scale=0.005)

    async def go():
        await vb.start()
        try:
            for _ in range(100):
                if vb.prereaders["supply"].get() is not None:
                    break
                await asyncio.sleep(0.05)
            t0 = time.perf_counter()
            rec = await vb.read_supply("se_p")
            ms = (time.perf_counter() - t0) * 1000
            skipped = vb.prereaders["supply"].skipped
            return rec, ms, skipped
        finally:
            await vb.stop()

    rec, ms, skipped = run(go())
    assert rec.fields["set_voltage"].value == 4.0 and ms < 100 and skipped > 0


# --- boot-current watch from frames ---------------------------------------------------------------


@pytest.mark.parametrize("scenario", sorted(s for s in SCENARIOS if not SCENARIOS[s].camera_down))
def test_boot_watch_classifies_the_pattern_from_frames(scenario):
    vb = bench(scenario)

    async def on(s):
        return False

    rec = run(vb.watch_boot_current("se", "on_connect", 1.0, on))
    assert isinstance(rec, EvidenceRecord) and rec.trace.pattern == vb.truth.truth("boot_pattern")
    assert len(rec.trace.samples) == int(vb.truth.pack.boot.thresholds.watch_s / 0.5) + 1  # every 0.5 s, none missed


def test_boot_watch_at_the_limit_triggers_the_engines_disconnect_now(monkeypatch):
    vb = bench()
    monkeypatch.setattr(vb.truth, "truth", lambda mid: "AT_LIMIT" if mid == "boot_pattern" else FakeBench.truth(vb.truth, mid))

    async def go():
        r = await started(vb)
        await r.get_reading("supply")
        step = await r.next_step()
        while step.get("measurement") != "boot_pattern" and step.get("step") not in ("fix", "unresolved"):
            if step.get("point"):
                await r.get_reading("meter", step["point"])
            elif step.get("ask"):
                await r.record_spoken_reading("cable theek hai" if "cable" in step["measurement"] else "kuch garam nahi", step["measurement"])
                await r.record_spoken_reading("haan", step["measurement"])
            step = await r.next_step()
        await r.request_power_step("connect_boot_cable")
        return await r.watch_boot_current("on_connect")

    out = run(go())
    assert out.get("urgent") == "disconnect_now" and out["pattern"] == "AT_LIMIT"
