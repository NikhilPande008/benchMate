"""Replay mode: a logged session played back through the companion screen, in order and at the
original pace (scaled by `speed`), with its evidence frames and board view, no voice, no API."""
from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from engine.main import create_app

from .test_ws import _until


@pytest.fixture
def vclient(settings, fake_openai, tmp_path):
    vs = settings.model_copy(update={"evidence": "vision", "frames_dir": tmp_path / "frames"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(fake_openai.handler))
    with TestClient(create_app(vs, http_client=http, prewarm=False)) as c:
        yield c


def drive(c, sid, msg):
    r = c.post(f"/dev/sessions/{sid}/drive", json=msg)
    assert r.status_code == 200, r.text
    return r.json()


def record_session(c, sid: str, gap_s: float = 0.0) -> list[dict]:
    """A short repair on a screen-only connection; returns the screen.state messages it got."""
    states = []
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "session_id": sid, "role": "screen"})
        assert ws.receive_json()["type"] == "ready"
        drive(c, sid, {"type": "transcript", "role": "user", "text": "Demo phone hai, dead hai", "t_ms": 1})
        drive(c, sid, {"type": "tool_call", "name": "start_repair", "args": {"model": "demo", "symptom": "dead", "battery": "removed"}})
        time.sleep(gap_s)
        drive(c, sid, {"type": "tool_call", "name": "get_reading", "args": {"kind": "supply"}})
        step = drive(c, sid, {"type": "tool_call", "name": "next_step", "args": {}})["output"]
        time.sleep(gap_s)
        drive(c, sid, {"type": "tool_call", "name": "get_reading", "args": {"kind": "meter", "point_id": step["point"]}})
        msgs = _until(ws, lambda m: m.get("event") == "screen.state" and m["state"]["meter"] is not None)
        states += [m["state"] for m in msgs if m.get("event") == "screen.state"]
        time.sleep(0.2)  # the last debounced pushes
    return states


def replay(c, source: str, speed: float = 1000.0):
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "session_id": "ignored", "role": "screen", "replay": source, "speed": speed})
        ready = ws.receive_json()
        t0 = time.perf_counter()
        msgs = _until(ws, lambda m: m.get("event") == "replay.done", limit=5000)
        return ready, msgs, time.perf_counter() - t0


def test_a_logged_session_replays_in_order_to_the_same_picture(vclient):
    c = vclient
    live = record_session(c, "se_rp1")
    ready, msgs, _ = replay(c, "se_rp1")
    assert ready["type"] == "ready" and ready["session_id"] == "rp_se_rp1" and ready["messages"] > 5
    states = [m["state"] for m in msgs if m.get("event") == "screen.state"]
    assert len(states) >= len(live) and states[-1]["meter"]["value"] == live[-1]["meter"]["value"]
    assert [s["state"] for s in states][0] == "intake" and states[-1]["state"] == "probing"
    events = [m.get("event") for m in msgs if m.get("type") == "ui"]
    assert "transcript" in events and "ledger.updated" in events and events[-1] == "replay.done"
    prog = [m for m in msgs if m.get("event") == "replay.progress"]
    assert prog and prog[-1]["t"] == prog[-1]["total"]
    assert states[-1]["transcript"][0]["text"] == "Demo phone hai, dead hai"


def test_replay_keeps_the_original_pace(vclient):
    c = vclient
    record_session(c, "se_rp2", gap_s=0.4)
    ready, _, real = replay(c, "se_rp2", speed=1.0)
    assert ready["seconds"] >= 0.8
    assert ready["seconds"] * 0.7 <= real <= ready["seconds"] + 1.5  # real speed
    _, _, fast = replay(c, "se_rp2", speed=20.0)
    assert fast < real / 3


def test_frames_and_the_board_during_a_replay(vclient):
    c = vclient
    states = record_session(c, "se_rp3")
    frame = next(e["frame"] for e in states[-1]["evidence"] if e["frame"])
    with c.websocket_connect("/ws") as ws:
        ws.send_json({"type": "hello", "session_id": "x", "role": "screen", "replay": "se_rp3", "speed": 1000})
        ws.receive_json()
        _until(ws, lambda m: m.get("event") == "replay.done", limit=5000)
        assert c.get(frame).content[:2] == b"\xff\xd8"  # the original connection is gone: served from the log
        assert c.get("/screen/board/rp_se_rp3/top.jpg").status_code == 200
        bp = c.get("/screen/board-point?session=rp_se_rp3").json()
        assert bp["point"] is not None and bp["aligned"] is True
    listed = c.get("/replays").json()["sessions"]
    assert any(s["session_id"] == "se_rp3" and s["messages"] > 5 for s in listed)


def test_replaying_an_unknown_session_ends_at_once(client):
    ready, msgs, _ = replay(client, "se_never")
    assert ready["messages"] == 0 and msgs[-1]["event"] == "replay.done"
