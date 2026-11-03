from __future__ import annotations

import json
import time

from engine.telemetry import load_spans, load_transcripts, make_engine


def _hello(ws, session_id="se_test", **extra):
    ws.send_json({"type": "hello", "session_id": session_id, "run_id": "run-ws", **extra})
    assert ws.receive_json() == {"type": "ready", "session_id": session_id}


def _collect_until_result(ws):
    msgs = []
    while True:
        msg = ws.receive_json()
        msgs.append(msg)
        if msg["type"] == "tool_result":
            return msgs


def test_tool_call_round_trip_returns_one_json_line(client):
    with client.websocket_connect("/ws") as ws:
        _hello(ws)
        ws.send_json({"type": "tool_call", "call_id": "c1", "name": "get_reading", "args": {"kind": "meter"}, "turn": 2})
        msgs = _collect_until_result(ws)

    result = msgs[-1]
    assert result["call_id"] == "c1"
    assert "\n" not in result["output"]
    assert json.loads(result["output"])["kind"] == "meter"
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


def test_dummy_delay_is_configurable_per_connection(client):
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
    client.close()

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
    client.close()
    rows = load_transcripts(make_engine(settings.db_path), "run-ws", ["se_tr"])
    assert [(r.role, r.lang_tag, r.lang_api) for r in rows] == [("user", "hi", "hi"), ("assistant", "hinglish", None)]
