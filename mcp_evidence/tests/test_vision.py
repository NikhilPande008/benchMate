"""Vision pipeline units: frames and cameras, calibration and the moved check, Reader B, Reader A
(schema, request, hedge, cache), the agreement rule, the degradation ladder, pre-reads."""
from __future__ import annotations

import asyncio
import json

import cv2
import httpx
import numpy as np
import pytest

from mcp_evidence.synth.dataset import make_sample
from mcp_evidence.synth.displays import FieldSpec, MeterReading, random_style, render_meter, render_supply
from mcp_evidence.synth.segments import layout
from mcp_evidence.vision.agreement import DisplaySpec, check, decide, decide_b_only, read_display
from mcp_evidence.vision.calibration import Calibration, CalibrationStore, Roi
from mcp_evidence.vision.frames import FakeCamera, Frame, FrameRing, OpenCVCamera
from mcp_evidence.vision.health import VisionHealth
from mcp_evidence.vision.preread import change
from mcp_evidence.vision.reader_a import (
    SCHEMAS, CachedReaderA, FixtureAnswer, FixtureReaderA, HedgedReaderA, OpenAIReaderA, ReadA, parse_response, request_body,
)
from mcp_evidence.vision.reader_b import ReadB, estimate_slant, read_field
from mcp_evidence.vision.scene import BenchScene, InstrumentState


def run(coro):
    return asyncio.run(coro)


# --- frames and cameras ----------------------------------------------------------------------


def test_ring_keeps_two_seconds_and_finds_the_frame_150ms_earlier():
    ring = FrameRing(keep_ms=2000)
    img = np.zeros((4, 4, 3), np.uint8)
    for i in range(40):
        ring.push(Frame("c", i, i * 100.0, img))
    assert len(ring) == 21 and ring.latest().seq == 39
    assert ring.before(ring.latest()).seq == 37  # 200 ms vs 100 ms: 150 is nearer 100? |200-150|=|100-150| → first
    assert {f.seq for f in ring.since(3800)} == {38, 39}


def test_fake_camera_sequence_still_and_failure():
    imgs = [np.full((4, 4, 3), v, np.uint8) for v in (10, 20, 30)]
    cam = FakeCamera.sequence("c", imgs)
    got = [int(cam.grab_once().image[0, 0, 0]) for _ in range(5)]
    assert got == [10, 20, 30, 10, 20]
    still = FakeCamera.still("s", imgs[1])
    assert int(still.grab_once().image[0, 0, 0]) == 20 and int(still.grab_once().image[0, 0, 0]) == 20
    cam.fail()
    assert cam.grab_once() is None and cam.errors == 1


def test_camera_loop_fills_the_ring_and_next_frame_waits():
    async def go():
        cam = FakeCamera.still("c", np.zeros((8, 8, 3), np.uint8), fps=50)
        await cam.start()
        f = await cam.next_frame(-1, timeout_s=1.0)
        g = await cam.next_frame(f.seq, timeout_s=1.0)
        healthy = cam.healthy
        await cam.stop()
        return f, g, healthy, len(cam.ring)

    f, g, healthy, n = run(go())
    assert f is not None and g.seq > f.seq and healthy and n >= 2


def test_opencv_camera_reads_a_video_file(tmp_path):
    path = str(tmp_path / "clip.avi")
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"MJPG"), 10, (64, 48))
    if not w.isOpened():
        pytest.skip("no video writer backend")
    for v in range(5):
        w.write(np.full((48, 64, 3), v * 40, np.uint8))
    w.release()
    cam = OpenCVCamera("file", path)
    frames = [cam.grab_once() for _ in range(5)]
    assert all(f is not None and f.image.shape == (48, 64, 3) for f in frames)
    assert cam.grab_once() is None  # end of file, not looping
    assert OpenCVCamera("missing", str(tmp_path / "nope.avi")).grab_once() is None


# --- calibration ------------------------------------------------------------------------------


def test_roi_orders_corners_and_warps_to_a_fixed_height():
    r = Roi("f", np.array([(300, 50), (10, 60), (20, 160), (310, 150)], np.float32))
    assert r.corners[0].tolist() == [10, 60] and r.corners[2].tolist() == [310, 150]
    crop = r.warp(np.zeros((300, 400, 3), np.uint8))
    assert crop.shape[0] == 96 and 250 < crop.shape[1] < 330
    d = Roi("d", np.array([(0, 0), (1000, 0), (1000, 500), (0, 500)], np.float32), kind="display").warp(np.zeros((600, 1100, 3), np.uint8))
    assert d.shape[1] == 512


def test_calibration_round_trip_and_store(tmp_path):
    scene = BenchScene()
    cal = scene.calibration()
    assert {"supply", "meter", "supply.set_voltage", "supply.live_current", "supply.current_limit", "meter.main"} <= set(cal.rois)
    store = CalibrationStore(tmp_path)
    store.save(cal)
    back = store.load("overhead")
    assert back is not None and set(back.rois) == set(cal.rois) and back.reference is not None
    assert np.allclose(back.rois["meter.main"].corners, cal.rois["meter.main"].corners, atol=0.01)
    assert store.load("microscope") is None


@pytest.mark.parametrize("offset,moved", [((0, 0), False), ((2, 1), False), ((15, 0), True), ((0, -12), True)])
def test_camera_moved_check(offset, moved):
    scene = BenchScene()
    cal = scene.calibration()
    m = cal.moved(scene.render(InstrumentState(offset=offset)))
    assert m.moved is moved, m


def test_camera_moved_check_without_texture_fails_safe():
    cal = Calibration("c", {"x": Roi("x", np.array([(0, 0), (10, 0), (10, 10), (0, 10)], np.float32))})
    cal.set_reference(np.zeros((100, 100, 3), np.uint8))
    assert cal.moved(np.zeros((100, 100, 3), np.uint8)).moved is True


# --- Reader B ---------------------------------------------------------------------------------


def _meter_crop(text: str, family="upright", seed=3) -> np.ndarray:
    rng = np.random.default_rng(seed)
    st = random_style(rng, "lcd", family)
    r = render_meter(np.random.default_rng(seed + 1), st, MeterReading("diode", text, "diode_V", ol=text == "OL"),
                     FieldSpec(layout("OL " if text == "OL" else text, 4)))
    return Roi("m", r.fields["main"]).warp(r.image)


@pytest.mark.parametrize("text,key", [("0.352", "0.352"), ("1.074", "1.074"), ("12.34", "12.34"), ("OL", "OL"), ("4.0", "4.0"),
                                       ("-1.25", "-1.25"), ("598.6", "598.6")])
def test_reader_b_reads_clean_meter_displays(text, key):
    crop = _meter_crop(text)
    read_field(crop, 4)  # warm-up
    r = read_field(crop, 4)
    assert r.legible and r.key == key, (r.key, r.reason, r.debug)
    assert r.confidence > 0.9 and r.ms < 60


def test_reader_b_reads_an_italic_led_supply_row():
    rng = np.random.default_rng(5)
    st = random_style(rng, "led", "italic")
    specs = {k: FieldSpec(layout(t, 4)) for k, t in (("set_voltage", "4.00"), ("live_current", "0.352"), ("current_limit", "1.00"))}
    r = render_supply(np.random.default_rng(6), st, specs)
    got = {k: read_field(Roi(k, q).warp(r.image), 4).key for k, q in r.fields.items()}
    assert got == {"set_voltage": "4.00", "live_current": "0.352", "current_limit": "1.00"}


def test_reader_b_gives_every_segment_a_probability():
    r = read_field(_meter_crop("8.888"), 4)
    assert len(r.cells) == 4 and all(set(c.seg_p) == set("abcdefg") for c in r.cells)
    assert all(c.seg_p[s] > 0.9 for c in r.cells for s in "abcdefg")  # an 8 lights every segment
    r1 = read_field(_meter_crop("1.111"), 4)
    assert all(c.seg_p["a"] < 0.1 and c.seg_p["b"] > 0.9 for c in r1.cells)


def test_reader_b_never_reads_a_blank_or_covered_display():
    blank = read_field(_meter_crop("    "[:0] or " ", seed=4) if False else np.full((96, 300, 3), 190, np.uint8), 4)
    assert not blank.legible and blank.reason in ("low_contrast", "blank")
    s = next(make_sample(7, i, "meter", "hand") for i in range(40) if not make_sample(7, i, "meter", "hand").readable["main"])
    covered = [read_field(Roi("m", np.array(s.fields["main"], np.float32)).warp(f), 4) for f in s.frames]
    assert all(not r.legible or r.key is None for r in covered) or all(r.key == f"{s.truth['main'].value}" for r in covered)


def test_slant_estimate():
    img = np.zeros((96, 200), np.float32)
    for y in range(10, 86):
        x = 60 + (86 - y) * 0.15
        img[y, int(x) : int(x) + 9] = 1.0
    assert abs(estimate_slant(cv2.GaussianBlur(img, (0, 0), 1.0)) - 0.15) < 0.04


# --- Reader A ---------------------------------------------------------------------------------


def _strict(schema: dict) -> None:
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for v in schema["properties"].values():
            _strict(v)


@pytest.mark.parametrize("display", ["meter", "supply"])
def test_reader_a_schema_is_strict(display):
    _strict(SCHEMAS[display])
    body = request_body(display, np.zeros((200, 1024, 3), np.uint8), "gpt-6-luna")
    fmt = body["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["schema"] is SCHEMAS[display]
    content = body["input"][0]["content"]
    assert content[0]["type"] == "input_text" and content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/jpeg;base64,") and content[1]["detail"] == "high"
    assert body["reasoning"] == {"effort": "none"} and body["store"] is False


def test_reader_a_resizes_the_crop_to_512_wide():
    import base64

    body = request_body("meter", np.zeros((300, 1500, 3), np.uint8), "m")
    raw = base64.b64decode(body["input"][0]["content"][1]["image_url"].split(",", 1)[1])
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    assert img.shape[1] == 512 and img.shape[0] == 102


def test_parse_response_output_refusal_and_incomplete():
    field = {"legible": True, "text": "0.352", "value": 0.352, "unit": "diode_V", "mode_indicator": "diode", "decimals_shown": 3, "confidence": 0.93}
    ok = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(field)}]}]}
    r = parse_response("meter", ok, 900)["main"]
    assert r.key == "0.352" and r.mode == "diode" and r.ms == 900
    refusal = {"status": "completed", "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "no"}]}]}
    assert parse_response("meter", refusal, 1)["main"].error == "refusal"
    assert parse_response("meter", {"status": "incomplete", "output": []}, 1)["main"].error == "error"
    sup = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(
        {f: field | {"text": t, "value": float(t), "decimals_shown": 2} for f, t in (("set_voltage", "4.00"), ("live_current", "0.00"), ("current_limit", "1.00"))})}]}]}
    assert {k: v.key for k, v in parse_response("supply", sup, 1).items()} == {"set_voltage": "4.00", "live_current": "0.00", "current_limit": "1.00"}


def test_the_real_reader_a_is_disabled_and_sends_the_documented_request():
    crop = np.zeros((96, 300, 3), np.uint8)
    with pytest.raises(RuntimeError):
        run(OpenAIReaderA("sk-test").read("meter", crop))
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"], seen["auth"], seen["body"] = str(req.url), req.headers["authorization"], json.loads(req.content)
        field = {"legible": True, "text": "OL", "value": None, "unit": "OL", "mode_indicator": "diode", "decimals_shown": 0, "confidence": 0.9}
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(field)}]}]})

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await OpenAIReaderA("sk-test", client=client, enabled=True).read("meter", crop)

    out = run(go())
    assert out["main"].key == "OL" and seen["url"] == "https://api.openai.com/v1/responses" and seen["auth"] == "Bearer sk-test"
    assert seen["body"]["model"] == "gpt-6-luna"


def _ans(key="0.352", lat=(0.0,), raises=False):
    return FixtureAnswer({"main": ReadA(True, key, float(key), "diode_V", "diode", 3, 0.95)}, lat, raises)


@pytest.mark.parametrize(
    "lat,raises,error,hedged",
    [
        ((1.0,), False, None, False),  # answers in time
        ((3.0, 0.5), False, None, True),  # slow: the hedge wins at 2.5 + 0.5 s
        ((5.0, 5.0), False, "timeout", True),  # nothing by 4 s
        ((0.1, 0.1), True, "error", True),  # fails, retried once, fails again
    ],
)
def test_hedged_reader_a(lat, raises, error, hedged):
    fx = FixtureReaderA(default=lambda d, c: _ans(lat=lat, raises=raises), time_scale=0.01)
    h = HedgedReaderA(fx, time_scale=0.01)
    out = run(h.read("meter", np.zeros((10, 10, 3), np.uint8)))["main"]
    assert out.error == error and (out.hedged if error is None else True) == hedged
    assert out.ms <= 4300
    assert fx.calls == (2 if hedged else 1)


def test_cache_hits_identical_crops_only():
    fx = FixtureReaderA(default=lambda d, c: _ans(), time_scale=0)
    c = CachedReaderA(fx)
    a = _meter_crop("0.352")
    b = _meter_crop("0.353")
    run(c.read("meter", a))
    second = run(c.read("meter", a.copy()))["main"]
    assert second.cached and c.hits == 1 and fx.calls == 1
    run(c.read("meter", b))  # one digit changed: a new call
    assert fx.calls == 2


# --- the agreement rule -----------------------------------------------------------------------


def B(key, conf=0.97):
    if key is None:
        return ReadB(None, None, 0, False, False, 0.0, "invalid_glyph", [], 1.0)
    ol = key == "OL"
    dec = len(key.split(".")[1]) if "." in key else 0
    return ReadB(key, None if ol else float(key), dec, ol, True, conf, None, [], 1.0)


def A(key, conf=0.93):
    if key is None:
        return ReadA(False, None, None, "unknown", None, 0, 0.2)
    dec = len(key.split(".")[1]) if "." in key else 0
    return ReadA(True, key, None if key == "OL" else float(key), "OL" if key == "OL" else "V", "dcv", dec, conf)


@pytest.mark.parametrize(
    "b,a,key",
    [
        (["0.352", "0.352", "0.352"], [None, "0.352"], "0.352"),  # stable frames, both readers
        (["0.352", "0.352", "0.352"], [None, "3.52"], None),  # A's decimal point differs
        (["0.352", "0.352", "0.352"], [None, "3.52", "0.352"], "0.352"),  # A again on frame 2, agrees
        (["0.352", "0.352", "0.352"], [None, "3.52", "3.52"], None),  # still disagree → spoken
        ([None, "0.352", "0.352"], [None, "0.352"], "0.352"),  # flicker on frame 0
        (["0.352", None, "0.352"], [None, None, "0.352"], "0.352"),  # flicker on frame 1: frames 0 and 2
        (["0.352", "0.352", None], [None, "0.352"], None),  # flicker on the latest frame: not settled now
        (["0.341", "0.352", "0.360"], [None, "0.352", "0.360"], None),  # mid-update: never two equal frames
        (["0.000", "0.000", "0.003"], [None, "0.000"], None),  # the held-out case: changed on the latest frame
        (["0.000", "0.003", "0.003"], [None, "0.003"], "0.003"),  # settled on the new value
        (["0.352", "0.352", "0.352"], [None, None], None),  # Reader A illegible
        (["OL", "OL", "OL"], [None, "OL"], "OL"),  # OL is a valid reading
        (["0.352", "0.352", "0.352"], [None, A("0.352", 0.3)], None),  # Reader A unsure of itself
        (["0.352", "0.352"], [None, "0.352"], "0.352"),  # two frames: the pair ends at the latest
    ],
)
def test_decide(b, a, key):
    bb = [B(k) for k in b]
    aa = [x if isinstance(x, ReadA) else (A(x) if x else None) for x in a]
    assert decide(bb, aa)[0] == key


def test_low_confidence_b_frames_do_not_count():
    assert decide([B("0.352"), B("0.352", 0.5), B("0.352", 0.5)], [None, A("0.352")])[0] is None


def test_reader_b_only_needs_three_stable_frames():
    assert decide_b_only([B("4.00")] * 3) == "4.00"
    assert decide_b_only([B("4.00"), B("4.00"), B("4.01")]) is None
    assert decide_b_only([B("4.00")] * 2) is None


def _supply_spec_and_frames(texts=("4.00", "0.000", "1.00"), seed=8):
    rng = np.random.default_rng(seed)
    st = random_style(rng, "led", "upright")
    specs = {k: FieldSpec(layout(t, 4)) for k, t in zip(("set_voltage", "live_current", "current_limit"), texts)}
    r = render_supply(np.random.default_rng(seed + 1), st, specs)
    rois = {"display": Roi("display", r.glass, kind="display"), **{k: Roi(k, q) for k, q in r.fields.items()}}
    return DisplaySpec("supply", lambda img: {k: v.warp(img) for k, v in rois.items()}), r.image


def _fixture_for(texts):
    ans = {f: ReadA(True, t, float(t), "V" if f == "set_voltage" else "A", None, len(t.split(".")[1]), 0.95)
           for f, t in zip(("set_voltage", "live_current", "current_limit"), texts)}
    return FixtureReaderA(default=lambda d, c: FixtureAnswer(ans, (0.0,)), time_scale=0)


def test_read_display_accepts_an_agreed_supply():
    spec, img = _supply_spec_and_frames()

    async def nxt(i):
        return img

    res = run(read_display(spec, nxt, _fixture_for(("4.00", "0.000", "1.00"))))
    assert res.ok and res.a_calls == 1 and {k: f.key for k, f in res.fields.items()} == {"set_voltage": "4.00", "live_current": "0.000", "current_limit": "1.00"}
    assert all(f.confidence >= 0.9 and f.si_value is not None for f in res.fields.values())


def test_supply_current_above_its_own_limit_is_implausible():
    spec, img = _supply_spec_and_frames(("4.00", "1.50", "1.00"))

    async def nxt(i):
        return img

    res = run(read_display(spec, nxt, _fixture_for(("4.00", "1.50", "1.00"))))
    assert res.status == "implausible" and res.reason == "current_above_limit"


def test_reader_b_only_level_returns_values_for_confirmation():
    spec, img = _supply_spec_and_frames()

    async def nxt(i):
        return img

    res = run(read_display(spec, nxt, None, level="reader_b_only"))
    f = res.fields["set_voltage"]
    assert res.ok and f.status == "b_only" and f.needs_confirmation and f.confidence <= 0.85 and res.a_calls == 0


def test_meter_mode_and_range_checks_and_unit_normalisation():
    from mcp_evidence.vision.agreement import DisplayResult, FieldResult

    def meter(key, unit, mode, si):
        return DisplayResult("meter", {"main": FieldResult("main", "agreed", key, float(key) if key != "OL" else None, 1, key == "OL", unit, si)},
                             "unclear", "full", mode=mode)

    wrong = check(meter("0.4", "V", "dcv", 0.4), "diode")
    assert wrong.status == "wrong_mode" and "diode mode" in wrong.say["en"]
    assert check(meter("352.1", "mV", "dcv", 0.3521), "dcv").ok
    assert check(meter("5.9", "V", "diode", 5.9), "diode").status == "implausible"
    assert check(meter("OL", "OL", "diode", None), "diode").ok


# --- degradation ladder -------------------------------------------------------------------------


def test_degradation_ladder():
    t = [0.0]
    h = VisionHealth(clock=lambda: t[0])
    events = []
    h.listeners.append(lambda lv, why: events.append((lv, why)))
    h.a_result(1, 1, 1)
    assert h.level == "full"  # one failure
    h.a_result(1, 1, 1)
    assert h.level == "reader_b_only" and events[-1] == ("reader_b_only", "vision_api")
    assert h.use_reader_a() is False  # the first probe comes one interval later
    t[0] = 31.0
    assert h.use_reader_a() is True and h.use_reader_a() is False
    h.a_result(1, 0, 1)
    assert h.level == "full"
    h.camera(False, "camera_moved")
    assert h.level == "spoken_only" and events[-1] == ("spoken_only", "camera_moved")
    h.camera(True)
    assert h.level == "full" and [e[0] for e in events] == ["reader_b_only", "full", "spoken_only", "full"]


# --- pre-read change detection ----------------------------------------------------------------


def test_change_detection_sees_a_decimal_point_but_not_noise():
    a = cv2.cvtColor(_meter_crop("0.352"), cv2.COLOR_BGR2GRAY)
    noisy = np.clip(a.astype(np.float32) + np.random.default_rng(1).normal(0, 3, a.shape), 0, 255).astype(np.uint8)
    moved_dp = cv2.cvtColor(_meter_crop("03.52"), cv2.COLOR_BGR2GRAY)
    assert change(a, noisy) < 25 < change(a, moved_dp)
    assert change(None, a) == float("inf")
