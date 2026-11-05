"""Dev endpoints and what the browser gets for dev controls: scenario list and switch, live
session summary, per-tool timeouts, and short engine notes into the voice session."""
from __future__ import annotations

import json

from engine.tools import tool_timeouts_ms
from mcp_evidence.fake_bench import SCENARIOS


def _hello(ws, session_id, **extra):
    ws.send_json({"type": "hello", "session_id": session_id, "run_id": "run-dev", **extra})
    return ws.receive_json()


def _until(ws, pred):
    while True:
        msg = ws.receive_json()
        if pred(msg):
            return msg


def _tool(ws, cid, name, args=None):
    ws.send_json({"type": "tool_call", "call_id": cid, "name": name, "args": args or {}})
    return json.loads(_until(ws, lambda m: m["type"] == "tool_result")["output"])


def test_scenario_list(client, settings):
    data = client.get("/dev/scenarios").json()
    assert data["default"] == settings.fake_scenario
    assert [s["name"] for s in data["scenarios"]] == list(SCENARIOS)
    glare = next(s for s in data["scenarios"] if s["name"] == "glare_low_confidence")
    assert glare["robustness"] == ["meter_glare"] and glare["symptom"] == "dead"


def test_browser_config_carries_tool_timeouts_and_scenarios(client, settings):
    cfg = client.post("/realtime/client-secret", json={}).json()["config"]
    assert cfg["tool_timeouts_ms"] == tool_timeouts_ms()
    assert all(ms > 1500 for ms in cfg["tool_timeouts_ms"].values())  # the SDK default would cut readings off
    assert cfg["dev"] == {"scenarios": list(SCENARIOS), "scenario": settings.fake_scenario}


def test_live_session_summary_and_events(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev1", scenario="no_current")
        _tool(ws, "c1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        _tool(ws, "c2", "next_step")
        data = client.get("/dev/sessions/se_dev1").json()
        assert data["scenario"] == "no_current" and data["connected"] is True
        assert data["state"] == "probing" and data["pending"] == "vbat_conn"
        names = [e["event"] for e in data["events"]]
        assert names[0] == "repair.started" and "step.proposed" in names
        later = client.get(f"/dev/sessions/se_dev1?since={data['next_event']}").json()
        assert later["events"] == []
    assert client.get("/dev/sessions/se_dev1").json()["connected"] is False


def test_switch_scenario_over_http(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev2")
        resp = client.post("/dev/sessions/se_dev2/scenario", json={"scenario": "vision_timeout"})
        assert resp.json() == {"session_id": "se_dev2", "scenario": "vision_timeout"}
        assert _until(ws, lambda m: m.get("event") == "dev.scenario")["scenario"] == "vision_timeout"
        assert client.post("/dev/sessions/se_dev2/scenario", json={"scenario": "nope"}).status_code == 400
    assert client.post("/dev/sessions/se_missing/scenario", json={"scenario": "no_current"}).status_code == 404
    assert client.get("/dev/sessions/se_missing").status_code == 404


def test_dev_endpoints_can_be_turned_off(client, settings):
    settings.dev_endpoints = False
    assert client.get("/dev/scenarios").status_code == 404
    assert client.post("/realtime/client-secret", json={"use_pool": False}).json()["config"]["dev"] is None
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev3")
        ws.send_json({"type": "dev", "scenario": "no_current"})
        assert _until(ws, lambda m: m["type"] == "error")["detail"].startswith("unknown scenario")


def test_camera_loss_sends_a_short_non_urgent_engine_note(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev4", scenario="camera_unavailable")
        _tool(ws, "c1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        ws.send_json({"type": "tool_call", "call_id": "c2", "name": "get_reading", "args": {"kind": "supply"}})
        note = _until(ws, lambda m: m["type"] == "inject")
        assert note["urgent"] is False and note["text"].startswith("[engine] Camera unavailable")
        assert len(note["text"]) < 200


def test_apply_fix_makes_the_fake_board_healthy(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev5", scenario="no_current")
        _tool(ws, "c1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        assert _tool(ws, "c2", "get_reading", {"kind": "meter", "point_id": "vbat_conn"})["class"] == "open"
        assert client.post("/dev/sessions/se_dev5/fix").json() == {"session_id": "se_dev5", "fixed": True}
        assert _tool(ws, "c3", "get_reading", {"kind": "meter", "point_id": "vbat_conn"})["class"] == "normal"


def test_tool_spans_carry_args_and_the_one_line_output(client, settings):
    from engine.telemetry import load_spans, make_engine

    from .conftest import flush_telemetry

    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_dev6")
        out = _tool(ws, "c1", "explain", {"topic": "diode_mode"})
    flush_telemetry(client)
    spans = {s.name: json.loads(s.attrs) for s in load_spans(make_engine(settings.db_path), "run-dev", ["se_dev6"]) if s.name.startswith("tool_")}
    assert spans["tool_started"]["args"] == '{"topic":"diode_mode"}'
    assert json.loads(spans["tool_finished"]["output"]) == out


def test_cost_endpoint_prices_recorded_usage(client):
    from engine.telemetry.cost import response_cost

    sid = client.post("/realtime/client-secret", json={"run_id": "run-cost-1", "use_pool": False}).json()["session_id"]
    usage = {"in_text_tokens": 1500, "in_cached_text_tokens": 1000, "out_audio_tokens": 120, "out_text_tokens": 30}
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "session_id": sid, "run_id": "run-cost-1"})
        ws.receive_json()
        ws.send_json({"type": "spans", "spans": [
            {"name": "line_start", "t_ms": 1.0, "attrs": {"id": "t01", "clip_ms": 0}},
            {"name": "response_done", "t_ms": 2.0, "attrs": usage},
        ]})
        _tool(ws, "c1", "explain", {"topic": "vbat"})  # a round trip: the spans were received
    data = client.get("/dev/cost?run=run-cost").json()
    assert data["responses"] == 1 and data["lines"] == 1
    assert data["usd"] == round(response_cost("gpt-realtime-2.1-mini", usage), 4)
    assert client.get("/dev/cost?run=ru").status_code == 400
