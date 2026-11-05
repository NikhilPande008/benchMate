"""The companion screen's engine side: screen.state pushes, viewers, the dev driver, transcripts and
interruptions as screen events, and the image and calibration routes (fake cameras, no API)."""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.main import create_app

from .test_ws import _hello, _until


@pytest.fixture
def vclient(settings, fake_openai, tmp_path):
    vs = settings.model_copy(update={"evidence": "vision", "frames_dir": tmp_path / "frames"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_openai.handler))
    with TestClient(create_app(vs, http_client=http, prewarm=False)) as c:
        yield c


def _state(ws) -> dict:
    return _until(ws, lambda m: m.get("event") == "screen.state")[-1]["state"]


def drive(client, sid, msg):
    r = client.post(f"/dev/sessions/{sid}/drive", json=msg)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_screen_state_on_connect_and_after_every_change(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_s1")
        first = _state(ws)
        assert first["state"] == "intake" and first["faults"] == [] and first["camera"] is False
        out = drive(client, "se_s1", {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
        assert out["output"]["pack"] == "demo_model"
        st = _state(ws)
        assert st["pack"]["id"] == "demo_model" and len(st["faults"]) >= 5 and abs(sum(f["p"] for f in st["faults"]) - 1) < 0.01
        drive(client, "se_s1", {"type": "tool_call", "name": "get_reading", "args": {"kind": "supply"}})
        drive(client, "se_s1", {"type": "tool_call", "name": "next_step", "args": {}})
        st = _state(ws)
        assert st["supply"]["fields"]["set_voltage"]["value"] == 4.0 and st["step"]["point"] == "vbat_conn"
        assert st["step"]["xy"] == [398, 210] and st["step"]["side"] == "top"


def test_transcripts_and_interruptions_reach_the_screen(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_s2")
        ws.send_json({"type": "transcript", "role": "user", "text": "meter padho please", "t_ms": 1})
        ev = _until(ws, lambda m: m.get("event") == "transcript")[-1]
        assert ev["lang"] == "hinglish" and ev["text"] == "meter padho please"
        ws.send_json({"type": "event", "name": "barge_in"})
        assert _until(ws, lambda m: m.get("event") == "interruption")[-1]["what"] == "barge_in"
        ws.send_json({"type": "event", "name": "safety_violation", "kind": "connect_boot_cable", "text": "Boot cable lagao."})
        st = _until(ws, lambda m: m.get("event") == "screen.state" and len(m["state"]["transcript"]) == 3)[-1]["state"]
        assert [l["role"] for l in st["transcript"]] == ["user", "event", "event"]
        assert st["transcript"][2]["what"] == "guardrail"


def test_a_second_screen_attaches_as_a_viewer(client):
    with client.websocket_connect("/ws") as voice:
        _hello(voice, "se_s3")
        with client.websocket_connect("/ws") as screen:
            screen.send_json({"type": "hello", "session_id": "se_s3", "role": "screen"})
            ready = screen.receive_json()
            assert ready["type"] == "ready" and ready["viewer"] is True
            assert _state(screen)["session_id"] == "se_s3"
            drive(client, "se_s3", {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
            assert _until(screen, lambda m: m.get("event") == "repair.started")
            screen.send_json({"type": "tool_call", "call_id": "x", "name": "next_step", "args": {}})
            assert _until(screen, lambda m: m["type"] == "error")[-1]["detail"].startswith("a screen viewer")
            screen.send_json({"type": "lang", "lang": "hi"})
            assert _until(voice, lambda m: m.get("event") == "session.lang")[-1]["lang"] == "hi"


def test_a_screen_alone_opens_the_session_and_the_dev_driver_runs_it(client):
    with client.websocket_connect("/ws") as screen:
        screen.send_json({"type": "hello", "session_id": "se_s4", "role": "screen"})
        assert screen.receive_json()["type"] == "ready"
        out = drive(client, "se_s4", {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
        assert out["output"]["pack"] == "demo_model"
        assert drive(client, "se_s4", {"type": "nonsense"}) == {"ok": True}  # an error goes to the page
    assert client.post("/dev/sessions/nope/drive", json={"type": "lang", "lang": "hi"}).status_code == 404


def test_microscope_and_reference_board_and_the_point_on_both(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_s5")
        for m in ({"name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}},
                  {"name": "get_reading", "args": {"kind": "supply"}}, {"name": "next_step", "args": {}}):
            drive(client, "se_s5", {"type": "tool_call", **m})
        r = client.get("/screen/frame/microscope.jpg?session=se_s5")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
        assert client.get("/screen/board/se_s5/top.jpg").status_code == 200
        bp = client.get("/screen/board-point?session=se_s5").json()
        assert bp["point"] == "vbat_conn" and bp["ref"] == [398, 210] and bp["aligned"] is True
        assert 0 < bp["live"][0] < bp["live_size"][0] and 0 < bp["live"][1] < bp["live_size"][1]
        assert client.get("/screen/frame/overhead.jpg?session=se_s5").status_code == 409  # fake bench: no camera
        assert client.get("/screen/frame/overhead.jpg?session=../x").status_code == 400


def test_vision_mode_frames_evidence_frames_and_calibration(vclient):
    c = vclient
    with c.websocket_connect("/ws") as ws:
        _hello(ws, "se_s6")
        drive(c, "se_s6", {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
        drive(c, "se_s6", {"type": "tool_call", "name": "get_reading", "args": {"kind": "supply"}})
        st = _until(ws, lambda m: m.get("event") == "screen.state" and any(e["kind"] == "supply" for e in m["state"]["evidence"]))[-1]["state"]
        assert st["camera"] is True
        ev = next(e for e in st["evidence"] if e["kind"] == "supply")
        assert ev["frame"] and c.get(ev["frame"]).content[:2] == b"\xff\xd8"
        assert c.get("/screen/evidence/se_s6/ev_nothere.jpg").status_code == 404
        assert c.get("/screen/frame/overhead.jpg?session=se_s6").status_code == 200
        cal = c.get("/screen/calibration?session=se_s6").json()
        roi = next(r for r in cal["rois"] if r["name"] == "supply.set_voltage")
        p = c.post("/screen/preview?session=se_s6", json={"name": roi["name"], "corners": roi["corners"], "digits": 4}).json()
        assert p["image"].startswith("data:image/jpeg;base64,") and p["reading"]["text"] == "4.00" and p["reading"]["legible"]
        moved = [[x + 6, y + 4] for x, y in roi["corners"]]
        saved = c.post("/screen/calibration?session=se_s6", json={"rois": [{"name": roi["name"], "corners": moved}]})
        assert saved.status_code == 200
        again = c.get("/screen/calibration?session=se_s6").json()
        assert next(r for r in again["rois"] if r["name"] == "supply.set_voltage")["corners"][0] == [round(moved[0][0], 1), round(moved[0][1], 1)]
        assert c.post("/screen/calibration?session=se_s6", json={"rois": [{"name": "nope", "corners": moved}]}).status_code == 400


def test_fault_injection_degrades_and_the_screen_shows_it(vclient):
    c = vclient
    with c.websocket_connect("/ws") as ws:
        _hello(ws, "se_s7")
        drive(c, "se_s7", {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
        assert c.post("/dev/sessions/se_s7/vision", json={"api_down": True}).json()["api_down"] is True
        for _ in range(2):
            drive(c, "se_s7", {"type": "tool_call", "name": "get_reading", "args": {"kind": "supply"}})
        st = _until(ws, lambda m: m.get("event") == "screen.state" and m["state"]["vision"]["level"] == "reader_b_only")[-1]["state"]
        assert st["vision"]["reason"] == "vision_api"
        r = c.post("/dev/sessions/se_s7/vision", json={"offset": [20, 0]})
        assert r.json()["state"]["offset"] == [20, 0]
        out = drive(c, "se_s7", {"type": "tool_call", "name": "get_reading", "args": {"kind": "supply"}})
        assert out["output"]["error"] == "camera_unavailable"
        st = _until(ws, lambda m: m.get("event") == "screen.state" and m["state"]["vision"]["level"] == "spoken_only")[-1]["state"]
        assert st["degraded"] == "camera_unavailable"
    assert json.loads(json.dumps(st))  # plain JSON all the way through
