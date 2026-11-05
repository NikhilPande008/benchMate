"""Replay a logged session through the companion screen (the demo backup). No voice, no API.

The engine logs every UI message it sends to a screen (`ui_events`: events and screen.state
snapshots). A replay streams them to a screen again with the original gaps (speed 1 is real
speed; `speed` and `max_gap_s` shorten it). The screen's image routes keep working: evidence
frames come from disk, the board view from the session's pack and the step being replayed.

    open  /?screen=1&replay=<session id>&speed=1
    list  GET /replays
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from engine.telemetry import load_evidence, load_ui_events
from mcp_evidence.fake_bench import FakeBench
from mcp_evidence.packs import load_pack
from mcp_evidence.schema import EvidenceRecord

REPLAY_PREFIX = "rp_"


@dataclass
class ReplayRepair:
    """What the screen's image routes need from a session, rebuilt from the log as it plays."""

    pack: Any = None
    pending_step: Any = None
    evidence: list[EvidenceRecord] = field(default_factory=list)

    def follow(self, state: dict[str, Any]) -> None:
        pack = state.get("pack")
        if pack and (self.pack is None or self.pack.pack_id != pack["id"]):
            self.pack = load_pack(pack["id"])
        step = state.get("step")
        self.pending_step = SimpleNamespace(measurement_id=step["measurement"]) if step else None


@dataclass
class ReplayConnection:
    hello: Any
    source: str
    repair: ReplayRepair = field(default_factory=ReplayRepair)
    bench: Any = field(default_factory=lambda: FakeBench(time_scale=0))
    connected: bool = True


def load_messages(db, session_id: str) -> list[tuple[float, dict[str, Any]]]:
    return [(r.t_ms, json.loads(r.data)) for r in load_ui_events(db, session_id)]


async def run_replay(ws: WebSocket, telemetry, connections, source: str, speed: float = 1.0, max_gap_s: float | None = None) -> None:
    db = telemetry.engine
    await telemetry.flush()
    msgs = await asyncio.to_thread(load_messages, db, source)
    replay_id = REPLAY_PREFIX + source[:61]
    conn = ReplayConnection(SimpleNamespace(session_id=replay_id), source)
    rows = await asyncio.to_thread(load_evidence, db, source)
    conn.repair.evidence = [EvidenceRecord.model_validate_json(r.data) for r in rows]
    if connections is not None:
        connections[replay_id] = conn
    speed = max(0.05, min(speed, 1000.0))
    total = (msgs[-1][0] - msgs[0][0]) / 1000 if msgs else 0.0
    try:
        await ws.send_json({"type": "ready", "session_id": replay_id, "replay": source, "messages": len(msgs), "seconds": round(total, 1)})
        if not msgs:
            await ws.send_json({"type": "ui", "event": "replay.done", "messages": 0})
            return
        t0 = msgs[0][0]
        prev = t0
        for t, msg in msgs:
            gap = (t - prev) / 1000
            if max_gap_s is not None:
                gap = min(gap, max_gap_s)
            if gap > 0:
                await asyncio.sleep(gap / speed)
            prev = t
            if msg.get("event") == "screen.state":
                conn.repair.follow(msg.get("state") or {})
            await ws.send_json(msg)
            await ws.send_json({"type": "ui", "event": "replay.progress", "t": round((t - t0) / 1000, 1), "total": round(total, 1)})
        await ws.send_json({"type": "ui", "event": "replay.done", "messages": len(msgs)})
        while True:  # stay open so the screen keeps the last picture; ignore what it sends
            await ws.receive_json()
    except WebSocketDisconnect:
        pass
    finally:
        conn.connected = False
