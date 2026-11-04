"""A repair session rebuilt from SQLite after a restart matches the live one: same evidence in the
same order, the same ledger (recomputed from the stored readings), the same next step."""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
import httpx

from engine.api.ws import load_stored_repair
from engine.main import create_app
from engine.session.repair import RepairSession
from engine.telemetry import TelemetryStore, load_snapshots
from mcp_evidence.fake_bench import FakeBench

from .conftest import FakeOpenAI


async def conflicting_session(store: TelemetryStore) -> RepairSession:
    """conflicting_reading up to just before the fix: a slipped probe reading, a conflict, a
    requested re-measure that supersedes it, a powered spoken observation."""
    r = RepairSession("se_p", FakeBench("conflicting_reading", time_scale=0),
                      persist=lambda t, row: store.record_repair(t, row, "run-p"))
    await r.on_user_transcript("battery nikal di, phone dead hai", "hinglish")
    await r.start_repair("demo phone", "dead")
    await r.get_reading("supply")
    for _ in range(4):
        step = await r.next_step()
        if step["step"] == "power":
            await r.request_power_step(step["power_step"])
            await r.record_spoken_reading("kuch garam nahi", step["measurement"])
            await r.record_spoken_reading(confirm=True)
        else:
            await r.get_reading("meter", step["point"])
    return r


async def live_then_restored(db) -> tuple[RepairSession, RepairSession]:
    store = TelemetryStore(db)
    await store.start()
    live = await conflicting_session(store)
    await store.stop()  # engine shuts down

    restarted = TelemetryStore(db)  # new process, same file
    stored = load_stored_repair(restarted, "se_p")
    assert stored is not None
    restored = RepairSession.restore(*stored, FakeBench("conflicting_reading", time_scale=0))
    return live, restored


def test_rebuilt_session_matches_the_live_one(tmp_path):
    live, restored = asyncio.run(live_then_restored(tmp_path / "p.sqlite"))
    assert live.conflicts == 1 and any(s.conflict for s in live.ledger.history)  # the case is non-trivial
    assert [e.model_dump(mode="json") for e in restored.evidence] == [e.model_dump(mode="json") for e in live.evidence]
    assert restored.ledger.posterior == live.ledger.posterior  # exact: same updates, same order
    assert [o.evidence_id for o in restored.ledger.active] == [o.evidence_id for o in live.ledger.active]
    assert [(s.after, s.stop, s.conflict) for s in restored.ledger.history] == [(s.after, s.stop, s.conflict) for s in live.ledger.history]
    assert restored.summary() == live.summary()
    assert restored.lng == live.lng == "hi" and restored.battery == "removed"
    assert restored.supply is not None and restored.supply.id == live.supply.id


def test_stored_snapshots_match_the_recomputed_ledger(tmp_path):
    live, restored = asyncio.run(live_then_restored(tmp_path / "p.sqlite"))
    rows = load_snapshots(TelemetryStore(tmp_path / "p.sqlite").engine, "se_p")
    assert [r.evidence_id for r in rows] == [s.observation.evidence_id for s in restored.ledger.history]
    for row, snap in zip(rows, restored.ledger.history):
        assert json.loads(row.after) == snap.after
        assert row.stop == snap.stop
        assert row.conflict == (snap.conflict.reason if snap.conflict else None)


def test_rebuilt_session_continues_with_the_same_next_step(tmp_path):
    async def both():
        live, restored = await live_then_restored(tmp_path / "p.sqlite")
        return await live.next_step(), await restored.next_step()

    a, b = asyncio.run(both())
    assert a == b and a["step"] == "measure" and a["measurement"] == "pwr_key"


def test_power_approvals_do_not_survive_a_restart(tmp_path):
    async def flow():
        store = TelemetryStore(tmp_path / "p.sqlite")
        await store.start()
        live = await conflicting_session(store)
        assert (await live.request_power_step("connect_boot_cable"))["approved"]
        await store.stop()
        stored = load_stored_repair(TelemetryStore(tmp_path / "p.sqlite"), "se_p")
        restored = RepairSession.restore(*stored, FakeBench("conflicting_reading", time_scale=0))
        return live, restored, await restored.watch_boot_current()

    live, restored, watch = asyncio.run(flow())
    assert live.approvals.pending is not None and restored.approvals.pending is None
    assert watch == {"error": "power_step_required", "step": "connect_boot_cable"}


def test_a_closed_repair_restores_as_closed(tmp_path):
    async def flow():
        store = TelemetryStore(tmp_path / "p.sqlite")
        await store.start()
        r = RepairSession("se_c", FakeBench(time_scale=0), persist=lambda t, row: store.record_repair(t, row))
        await r.start_repair("demo", "dead", "removed")
        await r.close_repair("abandoned")
        await store.stop()
        return load_stored_repair(TelemetryStore(tmp_path / "p.sqlite"), "se_c")

    state, evidence = asyncio.run(flow())
    assert state["state"] == "closed" and evidence == []


def test_no_stored_state_means_a_fresh_session(tmp_path):
    assert load_stored_repair(TelemetryStore(tmp_path / "p.sqlite"), "se_none") is None


def test_websocket_reconnect_after_an_engine_restart_resumes_the_repair(settings):
    def app():
        return create_app(settings, http_client=httpx.AsyncClient(transport=httpx.MockTransport(FakeOpenAI().handler)), prewarm=False)

    def tool(ws, cid, name, args=None):
        ws.send_json({"type": "tool_call", "call_id": cid, "name": name, "args": args or {}})
        while (msg := ws.receive_json())["type"] != "tool_result":
            pass
        return json.loads(msg["output"])

    hello = {"type": "hello", "session_id": "se_resume", "run_id": "run-r", "scenario": "short_battery_line"}
    with TestClient(app()) as c, c.websocket_connect("/ws") as ws:
        ws.send_json(hello)
        assert "restored" not in ws.receive_json()
        tool(ws, "a1", "start_repair", {"model": "demo", "symptom": "dead", "battery": "removed"})
        tool(ws, "a2", "next_step")
        first = tool(ws, "a3", "get_reading", {"kind": "meter"})
        assert first["class"] == "short"

    with TestClient(app()) as c, c.websocket_connect("/ws") as ws:  # a new engine process, same database
        ws.send_json(hello)
        ready = ws.receive_json()
        assert ready["restored"]["measured"] == ["vbat_conn"] and ready["restored"]["state"] == "probing"
        note = ws.receive_json()  # context for the voice model, not an instructions update
        assert note["type"] == "inject" and note["urgent"] is False and "repair continues" in note["text"]
        nxt = tool(ws, "b1", "next_step")
        assert nxt["step"] == "power" and nxt["measurement"] == "heat_check"  # continues, doesn't restart
