"""WebSocket /ws: the browser relays tool calls here and receives UI events.

Browser -> engine
  {"type":"hello","session_id":..,"run_id":..,"dummy_delay_s":..}
  {"type":"tool_call","call_id":..,"name":..,"args":{..}|"..","turn":n}
  {"type":"spans","spans":[{"name":..,"t_ms":..,"turn":n,"attrs":{..}}, ..]}
  {"type":"transcript","role":"user"|"assistant","text":..,"t_ms":..,"item_id":..,"languages":[..]}
Engine -> browser
  {"type":"ready","session_id":..}
  {"type":"tool_result","call_id":..,"output":"<one JSON line>"}
  {"type":"ui","event":"tool.started"|"tool.finished",..}
  {"type":"error","detail":..}
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from engine.config import Settings
from engine.telemetry import SpanIn, TelemetryStore, TranscriptIn, now_ms
from engine.tools import ToolContext, one_line, run_tool

log = logging.getLogger("benchmate.ws")
router = APIRouter()


class Hello(BaseModel):
    type: str = "hello"
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    dummy_delay_s: float | None = Field(default=None, ge=0, le=10)


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


class Connection:
    def __init__(self, ws: WebSocket, settings: Settings, telemetry: TelemetryStore, hello: Hello):
        self.ws = ws
        self.settings = settings
        self.telemetry = telemetry
        self.hello = hello
        self._send_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.ws.send_json(msg)

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
        delay = self.hello.dummy_delay_s if self.hello.dummy_delay_s is not None else self.settings.dummy_delay_s
        ctx = ToolContext(session_id=self.hello.session_id, dummy_delay_s=delay)
        self.span("tool_started", call.turn, name=call.name, call_id=call.call_id)
        await self.send({"type": "ui", "event": "tool.started", "name": call.name, "call_id": call.call_id})
        try:
            output, ok = await run_tool(call.name, call.args, ctx, self.settings.tool_timeout_s)
        except Exception:  # the voice model must always get an answer, never a hang
            log.exception("tool %s crashed", call.name)
            output, ok = one_line({"error": "tool_failed"}), False
        self.span("tool_finished", call.turn, name=call.name, call_id=call.call_id, ok=ok)
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

    conn = Connection(ws, settings, telemetry, hello)
    await conn.send({"type": "ready", "session_id": hello.session_id})
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
                    telemetry.record_transcript(hello.session_id, TranscriptMsg.model_validate(msg), hello.run_id)
                else:
                    await conn.send({"type": "error", "detail": f"unknown message type {kind!r}"})
            except ValidationError as exc:
                await conn.send({"type": "error", "detail": f"invalid {kind} message: {exc.error_count()} error(s)"})
    except WebSocketDisconnect:
        pass
    finally:
        conn.cancel_all()
