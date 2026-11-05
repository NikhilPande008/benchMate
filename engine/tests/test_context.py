"""Context manager: the session summary, its cadence, the natural-pause check, the summary request
the page makes before a rotation or a reconnect, and the repair id kept across voice sessions."""
from __future__ import annotations

import asyncio

from engine.session.context import MAX_SUMMARY_CHARS, SUMMARY_EVERY_TURNS, pause_ok, summary_text
from engine.session.repair import RepairSession
from engine.telemetry import load_sessions, make_engine
from mcp_evidence.fake_bench import FakeBench

from .conftest import flush_telemetry
from .test_ws import _hello, _until


def run(coro):
    return asyncio.run(coro)


async def repair(lang: str = "en") -> RepairSession:
    notes: list[dict] = []

    async def inject(msg):
        notes.append(msg)

    r = RepairSession("se_c", FakeBench(time_scale=0), inject=inject)
    r._notes = notes  # type: ignore[attr-defined]
    await r.set_language(lang)
    await r.start_repair("demo", "phone is dead", "removed")
    await r.get_reading("supply")
    step = await r.next_step()
    await r.get_reading("meter", step["point"])
    await r.next_step()
    return r


def test_summary_before_a_repair_asks_for_one():
    r = RepairSession("se_c0", FakeBench(time_scale=0))
    assert "start_repair" in summary_text(r)


def test_summary_holds_the_facts_and_stays_short():
    r = run(repair())
    t = summary_text(r)
    assert len(t) <= MAX_SUMMARY_CHARS
    assert "Demo phone" in t and "dead" in t and "battery: removed" in t and "English" in t
    assert "Shorted capacitor on the battery line" in t and "%" in t
    assert "supply 4.000 V, limit 1.000 A" in t and "vbat_conn" in t and "short" in t and "(camera)" in t
    assert "Next step: heat or rosin check" in t and "safety gate" in t
    assert t.endswith("Call next_step before the next instruction.")


def test_a_summary_note_every_few_user_turns():
    async def go():
        r = await repair()
        r._notes.clear()  # type: ignore[attr-defined]
        for i in range(SUMMARY_EVERY_TURNS * 2):
            await r.on_user_transcript(f"turn {i}", None)
        return [n for n in r._notes if n["text"].startswith("[engine] Session summary")]  # type: ignore[attr-defined]

    notes = run(go())
    assert len(notes) == 2 and all(n["urgent"] is False for n in notes)


def test_natural_pause():
    async def go():
        r = await repair()
        out = [pause_ok(r)]  # next_step asked the gate: an approval is live
        r.on_barge_in()
        out.append(pause_ok(r))
        await r.record_spoken_reading("kuch garam nahi", "heat_check")
        out.append(pause_ok(r))
        return out

    live, after_barge, readback = run(go())
    assert live == (False, "power_approval_live") and after_barge == (True, None) and readback == (False, "readback_waiting")


def test_the_summary_request_and_the_rotated_report_over_the_socket(client, settings):
    with client.websocket_connect("/ws") as ws:
        _hello(ws, "se_rot")
        ws.send_json({"type": "summary"})
        s = _until(ws, lambda m: m["type"] == "summary")[-1]
        assert s["pause_ok"] is True and "start_repair" in s["text"] and s["turns"] == 0
        ws.send_json({"type": "event", "name": "rotated", "reason": "turns", "ms": 2100})
        ev = _until(ws, lambda m: m.get("event") == "session.rotated")[-1]
        assert ev["reason"] == "turns" and ev["rotations"] == 1
        ws.send_json({"type": "event", "name": "rotated", "reason": "reconnect"})
        assert _until(ws, lambda m: m.get("event") == "session.rotated")[-1]["reconnects"] == 1


def test_a_rotated_voice_session_keeps_the_repair_session_id(client, settings):
    first = client.post("/realtime/client-secret", json={"model": "mini"}).json()
    again = client.post("/realtime/client-secret", json={"model": "mini", "session_id": first["session_id"]}).json()
    assert again["session_id"] == first["session_id"]
    assert client.post("/realtime/client-secret", json={"session_id": "../etc"}).status_code == 422
    flush_telemetry(client)
    rows = [r.id for r in load_sessions(make_engine(settings.db_path))]
    assert first["session_id"] in rows and any(r.startswith(first["session_id"] + "~") for r in rows)  # one row per voice session


def test_rotation_settings_reach_the_page(client):
    cfg = client.post("/realtime/client-secret", json={}).json()["config"]
    assert cfg["rotation"] == {"enabled": False, "max_turns": 25, "max_minutes": 30.0, "max_context_tokens": 24000}
    assert cfg["truncation"]["retention_ratio"] == 0.8 and cfg["truncation"]["token_limits"]["post_instructions"] == 8000
