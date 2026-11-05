"""Dev endpoints (BENCHMATE_DEV_ENDPOINTS, on by default; the engine binds to 127.0.0.1).

GET  /dev/scenarios                   fake bench scenarios and the default
GET  /dev/sessions/{id}               live repair summary + recent engine events (the e2e driver polls this)
POST /dev/sessions/{id}/scenario      {"scenario": name}: switch the fake bench of a live connection
POST /dev/sessions/{id}/fix           the technician replaced the part: the fake board reads healthy
GET  /dev/cost?run=<run id or prefix> API cost so far, from recorded usage (the harness budget stop)
POST /dev/sessions/{id}/drive         one browser message (tool_call, transcript, event, lang) as if the
                                      page had sent it; a tool call returns its output (UI tests, replay)
POST /dev/sessions/{id}/vision        fault injection on the fake camera scene (hand, glare, a bumped
                                      camera, a blank meter, the knob on another mode, the vision API down)

Not for the voice model: none of this is a tool, and the browser only uses it in dev controls.
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from engine.config import Settings
from engine.telemetry.cost import run_cost
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


@router.get("/cost")
async def cost(run: str, request: Request) -> dict[str, Any]:
    settings = _enabled(request)
    if len(run) < 3:
        raise HTTPException(400, "run must be a run id or a prefix of at least 3 characters")
    telemetry = request.app.state.telemetry
    await telemetry.flush()
    c = await asyncio.to_thread(run_cost, telemetry.engine, run, settings.transcription_model)
    return c.line()


@router.post("/sessions/{session_id}/drive")
async def drive(session_id: str, request: Request) -> dict[str, Any]:
    _enabled(request)
    conn = _connection(request, session_id)
    msg = await request.json()
    if not isinstance(msg, dict):
        raise HTTPException(400, "a JSON object, like the browser's messages")
    if msg.get("type") == "tool_call":
        from engine.api.ws import ToolCall

        msg.setdefault("call_id", f"dev_{len(conn.repair.events)}")
        output = await conn._run_tool(ToolCall.model_validate(msg))
        return {"output": json.loads(output)}
    err = await conn.handle(msg)
    return {"ok": err is None, **(err or {})}


class VisionFaults(BaseModel):
    hand: bool | None = None
    glare: bool | None = None
    offset: list[int] | None = None
    knob: str | None = None
    meter_on: bool | None = None
    api_down: bool | None = None
    api_slow: bool | None = None
    camera_fail: bool | None = None


@router.post("/sessions/{session_id}/vision")
async def vision_faults(session_id: str, body: VisionFaults, request: Request) -> dict[str, Any]:
    _enabled(request)
    from dataclasses import replace

    from mcp_evidence.vision.bench import VisionBench

    conn = _connection(request, session_id)
    vb = conn.bench
    if not isinstance(vb, VisionBench):
        raise HTTPException(409, "no camera in this session (BENCHMATE_EVIDENCE=fake)")
    upd = {k: v for k, v in body.model_dump().items() if v is not None and k in ("hand", "glare", "knob", "meter_on")}
    if body.offset is not None:
        upd["offset"] = tuple(body.offset[:2])
    if body.knob == "":
        upd["knob"] = None
    vb.state = replace(vb.state, **upd)
    if vb.oracle is not None:
        if body.api_down is not None:
            vb.oracle.down = body.api_down
        if body.api_slow is not None:
            vb.oracle.slow = body.api_slow
    if body.camera_fail is not None and vb.camera is not None:
        vb.camera.fail(body.camera_fail)
    return {"state": {k: getattr(vb.state, k) for k in ("hand", "glare", "offset", "knob", "meter_on")},
            "api_down": vb.oracle.down if vb.oracle else None, "level": vb.health.level}
