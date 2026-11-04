from __future__ import annotations

import json
import time

from engine.telemetry import load_evidence, load_spans, load_transcripts, make_engine

from .conftest import flush_telemetry


def _hello(ws, session_id="se_test", **extra):
    ws.send_json({"type": "hello", "session_id": session_id, "run_id": "run-ws", **extra})
    ready = ws.receive_json()
    assert ready["type"] == "ready" and ready["session_id"] == session_id
    return ready


def _collect_until_result(ws):
    msgs = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == "tool_result":
            return msgs


def _tool(ws, call_id, name, args=None):
    ws.send_json({"type": "tool_call", "call_id": call_id, "name": name, "args": args or {}})
    msgs = _collect_until_result(ws)
    return json.loads(msgs[-1]["output"]), msgs


def _until(ws, pred):
    msgs = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if pred(msg):
            return msgs


def test_tool_call_round_trip_returns_one_json_line(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "tool_call", "call_id": "c1", "name": "get_reading", "args": {"kind": "meter"}, "turn": 2})
        msgs = _collect_until_result(ws)

    result = msgs[-1]
    assert result["call_id"] == "c1"
    assert "\n" not in result["output"]
    out = json.loads(result["output"])
    assert out["mode"] == "diode" and out["conf"] >= 0.9 and "value" in out
    assert {"type": "ui", "event": "tool.started", "name": "get_reading", "call_id": "c1"} in msgs


def test_args_may_arrive_as_json_string(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "tool_call", "call_id": "c2", "name": "get_reading", "args": '{"kind":"supply"}'})
        assert json.loads(_collect_until_result(ws)[-1]["output"])["kind"] == "supply"


def test_bad_args_and_unknown_tool_return_error_lines(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "tool_call", "call_id": "c3", "name": "get_reading", "args": {"kind": "scope"}})
        assert json.loads(_collect_until_result(ws)[-1]["output"]) == {"error": "bad_args"}
        ws.send_json({"type": "tool_call", "call_id": "c4", "name": "connect_power", "args": {}})
        assert json.loads(_collect_until_result(ws)[-1]["output"]) == {"error": "unknown_tool"}


def test_dummy_delay_is_configurable_per_connection(client, settings):
    settings.fake_time_scale = 1.0  # real fake-bench timing for this connection
    with client.websocket_connect("/ws") as ws:
        _hello(ws, dummy_delay_s=0.3)
        t0 = time.monotonic()
        ws.send_json({"type": "tool_call", "call_id": "c5", "name": "get_reading", "args": {"kind": "meter"}})
        _collect_until_result(ws)
        assert time.monotonic() - t0 >= 0.3


def test_hello_is_required(client):
    with client.websocket_connect("/ws") as ws:
        ws.send_json({"type": "tool_call", "call_id": "x", "name": "get_reading"})
        msg = ws.receive()
        assert msg["type"] == "websocket.close"


def test_browser_spans_and_engine_tool_spans_are_stored(client, settings):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, session_id="se_spans")
        ws.send_json({"type": "spans", "spans": [{"name": "speech_end", "t_ms": 1000.0, "turn": 1}]})
        ws.send_json({"type": "tool_call", "call_id": "c6", "name": "get_reading", "args": {"kind": "meter"}, "turn": 1})
        _collect_until_result(ws)
    flush_telemetry(client)

    spans = load_spans(make_engine(settings.db_path), "run-ws", ["se_spans"])
    got = {(s.name, s.source, s.turn) for s in spans}
    assert ("speech_end", "browser", 1) in got
    assert ("tool_started", "engine", 1) in got
    assert ("tool_finished", "engine", 1) in got


def test_crashing_tool_still_returns_a_result(client, monkeypatch):
    import engine.api.ws as ws_mod

    async def boom(*args, **kwargs):
        raise RuntimeError("camera exploded")

    monkeypatch.setattr(ws_mod, "run_tool", boom)
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "tool_call", "call_id": "c7", "name": "get_reading", "args": {"kind": "meter"}})
        assert json.loads(_collect_until_result(ws)[-1]["output"]) == {"error": "tool_failed"}


def test_transcripts_are_stored_with_language_tags(client, settings):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, session_id="se_tr")
        ws.send_json({"type": "transcript", "role": "user", "text": "मीटर पढ़ो।", "t_ms": 1.0, "item_id": "i1", "languages": ["hi"]})
        ws.send_json({"type": "transcript", "role": "assistant", "text": "Supply four volts dikha raha hai.", "t_ms": 2.0, "response_id": "r1", "phase": "final_answer"})
        ws.send_json({"type": "tool_call", "call_id": "c8", "name": "get_reading", "args": {"kind": "meter"}})
        _collect_until_result(ws)  # a round trip, so both transcripts were received
    flush_telemetry(client)
    rows = load_transcripts(make_engine(settings.db_path), "run-ws", ["se_tr"])
    assert [(r.role, r.lang_tag, r.lang_api) for r in rows] == [("user", "hi", "hi"), ("assistant", "hinglish", None)]


def test_repair_tools_work_end_to_end_over_the_socket(client, settings):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, session_id="se_rep", scenario="no_current")
        start, _ = _tool(ws, "r1", "start_repair", {"model": "demo phone", "symptom": "dead", "battery": "removed"})
        assert start["pack"] == "demo_model" and start["model_data"] is True
        step, _ = _tool(ws, "r2", "next_step")
        reading, _ = _tool(ws, "r3", "get_reading", {"kind": "meter", "point_id": step["point"]})
        assert reading["class"] == "open"  # the scripted open battery line
        fix, _ = _tool(ws, "r4", "next_step")
        assert fix["step"] == "fix" and fix["fault"] == "open_vbat_path"
    flush_telemetry(client)
    rows = load_evidence(make_engine(settings.db_path), "se_rep")
    assert [r.measurement_id for r in rows] == ["vbat_conn"]


def test_hello_selects_the_scenario_and_dev_message_switches_it(client):
    with client.websocket_connect("/ws") as ws:
        assert _hello(ws, scenario="not_charging")["scenario"] == "not_charging"
        ws.send_json({"type": "dev", "scenario": "glare_low_confidence"})
        assert _until(ws, lambda m: m["type"] in ("ui", "error"))[-1] == {"type": "ui", "event": "dev.scenario", "scenario": "glare_low_confidence"}
        ws.send_json({"type": "dev", "scenario": "no_such_thing"})
        assert _until(ws, lambda m: m["type"] == "error")[-1]["detail"].startswith("unknown scenario")
        _tool(ws, "d1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        out, _ = _tool(ws, "d2", "get_reading", {"kind": "meter", "point_id": "vbat_conn"})
        assert out == {"status": "unclear", "point": "vbat_conn", "ask": "read_aloud", "conf": out["conf"]} and out["conf"] < 0.9


def test_unknown_scenario_in_hello_falls_back_to_the_default(client, settings):
    with client.websocket_connect("/ws") as ws:
        assert _hello(ws, scenario="nope")["scenario"] == settings.fake_scenario


def test_barge_in_cancels_a_pending_power_approval(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        _tool(ws, "b1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        _tool(ws, "b2", "get_reading", {"kind": "supply"})
        gate, _ = _tool(ws, "b3", "request_power_step", {"step": "connect_boot_cable"})
        assert gate["approved"] is True
        ws.send_json({"type": "event", "name": "barge_in"})
        assert _until(ws, lambda m: m.get("event") == "power.cancelled")[-1]["step"] == "connect_boot_cable"
        out, _ = _tool(ws, "b4", "watch_boot_current")
        assert out == {"error": "power_step_required", "step": "connect_boot_cable"}


def test_battery_danger_in_a_user_transcript_injects_an_urgent_stop(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "transcript", "role": "user", "text": "battery phool gayi hai", "t_ms": 1.0})
        inject = _until(ws, lambda m: m["type"] == "inject")[-1]
        assert inject["urgent"] is True and "SAFETY STOP" in inject["text"]
        out, _ = _tool(ws, "s1", "next_step")
        assert out["error"] == "session_stopped"


def test_session_language_event_after_two_hindi_turns(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "transcript", "role": "user", "text": "read the meter please", "t_ms": 1.0})
        assert _until(ws, lambda m: m.get("event") == "session.lang")[-1]["lang"] == "en"
        for t in (2.0, 3.0):
            ws.send_json({"type": "transcript", "role": "user", "text": "meter padho, kitna dikha raha hai", "t_ms": t})
        assert _until(ws, lambda m: m.get("event") == "session.lang")[-1]["lang"] == "hi"
