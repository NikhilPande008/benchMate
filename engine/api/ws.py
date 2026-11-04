"""WebSocket /ws: the browser relays tool calls here and receives UI events.

Browser -> engine
  {"type":"hello","session_id":..,"run_id":..,"dummy_delay_s":..,"scenario":..}
  {"type":"tool_call","call_id":..,"name":..,"args":{..}|"..","turn":n}
  {"type":"spans","spans":[{"name":..,"t_ms":..,"turn":n,"attrs":{..}}, ..]}
  {"type":"transcript","role":"user"|"assistant","text":..,"t_ms":..,"item_id":..,"languages":[..]}
  {"type":"event","name":"barge_in"}          user spoke over the agent: cancels a pending power approval
  {"type":"dev","scenario":..}                 fake bench scenario (dev controls)
Engine -> browser
  {"type":"ready","session_id":..,"scenario":..}
  {"type":"tool_result","call_id":..,"output":"<one JSON line>"}
  {"type":"ui","event":"tool.started"|"tool.finished"|"ledger.updated"|"session.lang"|..,..}
  {"type":"inject","urgent":bool,"text":..}    short engine message for the voice session
  {"type":"error","detail":..}
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from engine.config import Settings
from engine.session.repair import RepairSession
from engine.telemetry import SpanIn, TelemetryStore, TranscriptIn, lang_tag, load_evidence, load_repair_state, now_ms
from engine.tools import ToolContext, one_line, run_tool
from mcp_evidence.fake_bench import SCENARIOS, FakeBench
from mcp_evidence.schema import EvidenceRecord

log = logging.getLogger("benchmate.ws")
router = APIRouter()


class Hello(BaseModel):
    type: str = "hello"
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    dummy_delay_s: float | None = Field(default=None, ge=0, le=10)
    scenario: str | None = Field(default=None, max_length=64)


class ToolCall(BaseModel):
    type: str = "tool_call"
    call_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] | str = Field(default_factory=dict)
    turn: int = 0


class TranscriptMsg(TranscriptIn):
    type: str = "transcript"


class SpanBatch(BaseModel):
    type: str = "spans"
    spans: list[SpanIn] = Field(max_length=200)


Stored = tuple[dict[str, Any], list[tuple[EvidenceRecord, str | None]]]


def load_stored_repair(telemetry: TelemetryStore, session_id: str) -> Stored | None:
    """A repair session saved by an earlier connection or engine run (worker thread)."""
    state = load_repair_state(telemetry.engine, session_id)
    if state is None:
        return None
    rows = load_evidence(telemetry.engine, session_id)
    return state, [(EvidenceRecord.model_validate_json(r.data), r.ledger_use) for r in rows]


class Connections(OrderedDict[str, "Connection"]):
    """Live and recent connections by session id, for the dev endpoints (newest kept)."""

    KEEP = 50

    def add(self, conn: "Connection") -> None:
        self[conn.hello.session_id] = conn
        self.move_to_end(conn.hello.session_id)
        while len(self) > self.KEEP:
            self.popitem(last=False)


class Connection:
    def __init__(self, ws: WebSocket, settings: Settings, telemetry: TelemetryStore, hello: Hello,
                 stored: Stored | None = None):
        self.ws = ws
        self.connected = True
        self.settings = settings
        self.telemetry = telemetry
        self.hello = hello
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        delay = hello.dummy_delay_s if hello.dummy_delay_s is not None else settings.dummy_delay_s
        scenario = hello.scenario if hello.scenario in SCENARIOS else settings.fake_scenario
        self.bench = FakeBench(scenario=scenario, meter_delay_s=delay, time_scale=settings.fake_time_scale)
        hooks: dict[str, Any] = dict(
            notify=self.send, inject=self.send, vision_timeout_s=settings.vision_timeout_s,
            persist=lambda table, row: telemetry.record_repair(table, row, hello.run_id),
        )
        if stored:
            self.repair = RepairSession.restore(stored[0], stored[1], self.bench, **hooks)
        else:
            self.repair = RepairSession(hello.session_id, self.bench, **hooks)

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            try:
                await self.ws.send_json(msg)
            except (RuntimeError, WebSocketDisconnect):  # closed: late UI events have nowhere to go
                log.debug("dropped %s after close", msg.get("type"))

    async def set_scenario(self, name: str) -> None:
        self.bench.set_scenario(name)
        await self.send({"type": "ui", "event": "dev.scenario", "scenario": name})

    def span(self, span_name: str, turn: int, **attrs: Any) -> None:
        self.telemetry.record(
            self.hello.session_id,
            SpanIn(name=span_name, t_ms=now_ms(), turn=turn, attrs=attrs or None),
            "engine",
            self.hello.run_id,
        )

    def spawn_tool(self, call: ToolCall) -> None:
        task = asyncio.create_task(self._run_tool(call))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_tool(self, call: ToolCall) -> None:
        ctx = ToolContext(repair=self.repair)
        args = call.args if isinstance(call.args, str) else one_line(call.args)
        self.span("tool_started", call.turn, name=call.name, call_id=call.call_id, args=args[:300])
        await self.send({"type": "ui", "event": "tool.started", "name": call.name, "call_id": call.call_id})
        try:
            output, ok = await run_tool(call.name, call.args, ctx)
        except Exception:  # the voice model must always get an answer, never a hang
            log.exception("tool %s crashed", call.name)
            output, ok = one_line({"error": "tool_failed"}), False
        self.span("tool_finished", call.turn, name=call.name, call_id=call.call_id, ok=ok, output=output)
        await self.send({"type": "tool_result", "call_id": call.call_id, "output": output})
        await self.send({"type": "ui", "event": "tool.finished", "name": call.name, "call_id": call.call_id, "ok": ok})

    def cancel_all(self) -> None:
        for t in list(self._tasks):
            t.cancel()


@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    settings: Settings = ws.app.state.settings
    telemetry: TelemetryStore = ws.app.state.telemetry
    await ws.accept()
    try:
        hello = Hello.model_validate(await ws.receive_json())
    except (ValidationError, ValueError, WebSocketDisconnect):
        await ws.close(code=1008)
        return

    await telemetry.flush()  # a reconnect may follow writes that are still queued
    stored = await asyncio.to_thread(load_stored_repair, telemetry, hello.session_id)
    conn = Connection(ws, settings, telemetry, hello, stored)
    connections = getattr(ws.app.state, "connections", None)
    if connections is not None:
        connections.add(conn)
    ready: dict[str, Any] = {"type": "ready", "session_id": hello.session_id, "scenario": conn.bench.scenario}
    if stored:
        ready["restored"] = conn.repair.summary()
    await conn.send(ready)
    if stored and conn.repair.state in ("probing", "fix"):
        await conn.repair.note("The engine reconnected; the repair continues. Call next_step before the next instruction.")
    try:
        while True:
            msg = await ws.receive_json()
            kind = msg.get("type") if isinstance(msg, dict) else None
            try:
                if kind == "tool_call":
                    conn.spawn_tool(ToolCall.model_validate(msg))
                elif kind == "spans":
                    for span in SpanBatch.model_validate(msg).spans:
                        telemetry.record(hello.session_id, span, "browser", hello.run_id)
                elif kind == "transcript":
                    tr = TranscriptMsg.model_validate(msg)
                    telemetry.record_transcript(hello.session_id, tr, hello.run_id)
                    if tr.role == "user":
                        await conn.repair.on_user_transcript(tr.text, lang_tag(tr.text))
                elif kind == "event" and msg.get("name") == "barge_in":
                    conn.repair.on_barge_in()
                elif kind == "dev" and msg.get("scenario") in SCENARIOS and settings.dev_endpoints:
                    await conn.set_scenario(msg["scenario"])
                elif kind == "dev":
                    await conn.send({"type": "error", "detail": f"unknown scenario {msg.get('scenario')!r}"})
                else:
                    await conn.send({"type": "error", "detail": f"unknown message type {kind!r}"})
            except ValidationError as exc:
                await conn.send({"type": "error", "detail": f"invalid {kind} message: {exc.error_count()} error(s)"})
    except WebSocketDisconnect:
        pass
    finally:
        conn.connected = False
        conn.cancel_all()
