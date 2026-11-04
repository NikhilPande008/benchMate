"""Dev endpoints (BENCHMATE_DEV_ENDPOINTS, on by default; the engine binds to 127.0.0.1).

GET  /dev/scenarios                   fake bench scenarios and the default
GET  /dev/sessions/{id}               live repair summary + recent engine events (the e2e driver polls this)
POST /dev/sessions/{id}/scenario      {"scenario": name}: switch the fake bench of a live connection
POST /dev/sessions/{id}/fix           the technician replaced the part: the fake board reads healthy

Not for the voice model: none of this is a tool, and the browser only uses it in dev controls.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from engine.config import Settings
from mcp_evidence.fake_bench import SCENARIOS

router = APIRouter(prefix="/dev")
MAX_EVENTS = 50


def scenario_list() -> list[dict[str, Any]]:
    return [
        {"name": s.name, "symptom": s.symptom, "true_fault": s.true_fault,
         "robustness": [k for k in ("meter_glare", "camera_down", "vision_timeout") if getattr(s, k)] + (["glitch"] if s.glitch else [])}
        for s in SCENARIOS.values()
    ]


def _enabled(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    if not settings.dev_endpoints:
        raise HTTPException(404, "dev endpoints are off")
    return settings


def _connection(request: Request, session_id: str) -> Any:
    conn = request.app.state.connections.get(session_id)
    if conn is None:
        raise HTTPException(404, f"no repair session {session_id!r} on this engine")
    return conn


@router.get("/scenarios")
async def scenarios(request: Request) -> dict[str, Any]:
    settings = _enabled(request)
    return {"default": settings.fake_scenario, "scenarios": scenario_list()}


@router.get("/sessions/{session_id}")
async def session(session_id: str, request: Request, since: int = 0) -> dict[str, Any]:
    _enabled(request)
    conn = _connection(request, session_id)
    events = conn.repair.events
    start = max(since, len(events) - MAX_EVENTS)
    return {
        **conn.repair.summary(),
        "scenario": conn.bench.scenario,
        "connected": conn.connected,
        "events": events[start:],
        "next_event": len(events),
    }


class ScenarioBody(BaseModel):
    scenario: str


@router.post("/sessions/{session_id}/scenario")
async def set_scenario(session_id: str, body: ScenarioBody, request: Request) -> dict[str, Any]:
    _enabled(request)
    if body.scenario not in SCENARIOS:
        raise HTTPException(400, f"scenario must be one of {sorted(SCENARIOS)}")
    conn = _connection(request, session_id)
    await conn.set_scenario(body.scenario)
    return {"session_id": session_id, "scenario": body.scenario}


@router.post("/sessions/{session_id}/fix")
async def apply_fix(session_id: str, request: Request) -> dict[str, Any]:
    _enabled(request)
    conn = _connection(request, session_id)
    conn.bench.apply_fix()
    return {"session_id": session_id, "fixed": True}
