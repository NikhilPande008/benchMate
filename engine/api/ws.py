"""WebSocket /ws: the browser relays tool calls here and receives UI events.

Browser -> engine
  {"type":"hello","session_id":..,"run_id":..,"dummy_delay_s":..,"scenario":..,"role":"voice"|"screen"}
      role "screen": a companion screen. It attaches to the session's live connection as a viewer
      (UI events and screen.state only), or opens the session itself if none is live.
      "replay": <session id> (with "speed", "max_gap_s"): play a logged session to this screen.
  {"type":"tool_call","call_id":..,"name":..,"args":{..}|"..","turn":n}
  {"type":"spans","spans":[{"name":..,"t_ms":..,"turn":n,"attrs":{..}}, ..]}
  {"type":"transcript","role":"user"|"assistant","text":..,"t_ms":..,"item_id":..,"languages":[..]}
  {"type":"event","name":"barge_in"}          user spoke over the agent: cancels a pending power approval
  {"type":"event","name":"safety_violation","kind":..,"text":..}   guardrail stopped an unapproved power instruction
  {"type":"dev","scenario":..}                 fake bench scenario (dev controls)
  {"type":"lang","lang":"hi"|"en"}             the screen's reply-language toggle
  {"type":"summary"}                           the page is about to rotate or reconnect the voice session
  {"type":"event","name":"rotated","reason":"turns"|"time"|"tokens"|"reconnect"}   it did
Engine -> browser
  {"type":"ready","session_id":..,"scenario":..}
  {"type":"tool_result","call_id":..,"output":"<one JSON line>"}
  {"type":"ui","event":"tool.started"|"tool.finished"|"ledger.updated"|"session.lang"|..,..}
  {"type":"ui","event":"screen.state","state":{..}}   the whole screen, after every change (debounced)
  {"type":"inject","urgent":bool,"text":..}    short engine message for the voice session
  {"type":"summary","text":..,"pause_ok":bool,"why":..}   the session summary, and whether now is a natural pause
  {"type":"error","detail":..}
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict, deque
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field, ValidationError

from engine.config import Settings
from engine.records import recall as recall_records
from engine.replay import run_replay
from engine.session.repair import RepairSession
from engine.telemetry import SpanIn, TelemetryStore, TranscriptIn, lang_tag, load_evidence, load_repair_state, now_ms
from engine.tools import ToolContext, one_line, run_tool
from mcp_evidence.fake_bench import SCENARIOS, FakeBench
from mcp_evidence.vision.bench import VisionBench
from mcp_evidence.schema import EvidenceRecord

log = logging.getLogger("benchmate.ws")
router = APIRouter()


class Hello(BaseModel):
    type: str = "hello"
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str | None = Field(default=None, max_length=64)
    dummy_delay_s: float | None = Field(default=None, ge=0, le=10)
    scenario: str | None = Field(default=None, max_length=64)
    role: str = Field(default="voice", pattern="^(voice|screen)$")
    replay: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_\-]{1,64}$")  # replay this logged session to the screen
    speed: float = Field(default=1.0, gt=0, le=1000)
    max_gap_s: float | None = Field(default=None, gt=0, le=3600)


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


def make_bench(settings: Settings, scenario: str, delay: float) -> FakeBench | VisionBench:
    """The evidence source for a connection: the fake bench, or the fake bench seen through the
    camera pipeline (BENCHMATE_EVIDENCE=vision)."""
    truth = FakeBench(scenario=scenario, meter_delay_s=delay, time_scale=settings.fake_time_scale)
    if settings.evidence != "vision":
        return truth
    if settings.reader_a == "openai":
        raise RuntimeError("Reader A 'openai' is disabled in Phase 3 (no API calls); see docs/PENDING_RUNS.md")
    return VisionBench(FakeBench(scenario=scenario, time_scale=0), time_scale=settings.fake_time_scale, frames_dir=settings.frames_dir)


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
        self.viewers: set[WebSocket] = set()
        self.transcript: deque[dict[str, Any]] = deque(maxlen=60)
        self._state_pending = False
        delay = hello.dummy_delay_s if hello.dummy_delay_s is not None else settings.dummy_delay_s
        scenario = hello.scenario if hello.scenario in SCENARIOS else settings.fake_scenario
        self.bench = make_bench(settings, scenario, delay)
        hooks: dict[str, Any] = dict(
            notify=self.send, inject=self.send, vision_timeout_s=settings.vision_timeout_s,
            persist=lambda table, row: telemetry.record_repair(table, row, hello.run_id),
            recall=lambda pack_id, symptom: asyncio.to_thread(recall_records, telemetry.engine, pack_id, symptom),
        )
        if stored:
            self.repair = RepairSession.restore(stored[0], stored[1], self.bench, **hooks)
        else:
            self.repair = RepairSession(hello.session_id, self.bench, **hooks)
        if isinstance(self.bench, VisionBench):
            # the degradation ladder (full → Reader B only → spoken only) reaches the screen and the model
            self.bench.health.listeners.append(lambda level, reason: self._spawn(self.repair.on_vision_level(level, reason)))

    def _spawn(self, coro: Any) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def send(self, msg: dict[str, Any]) -> None:
        async with self._send_lock:
            if self.connected:
                try:
                    await self.ws.send_json(msg)
                except (RuntimeError, WebSocketDisconnect):  # closed: late UI events have nowhere to go
                    log.debug("dropped %s after close", msg.get("type"))
            if msg.get("type") == "ui":
                self.telemetry.record_ui(self.hello.session_id, msg, self.hello.run_id)  # for replay
                for v in list(self.viewers):
                    try:
                        await v.send_json(msg)
                    except (RuntimeError, WebSocketDisconnect):
                        self.viewers.discard(v)
        if msg.get("type") == "ui" and msg.get("event") != "screen.state":
            self.state_soon()

    # --- the companion screen ------------------------------------------------------------------

    def screen_state(self) -> dict[str, Any]:
        st = self.repair.screen_state()
        st["transcript"] = list(self.transcript)
        st["scenario"] = self.bench.scenario
        st["camera"] = isinstance(self.bench, VisionBench)
        return st

    def state_soon(self, delay_s: float = 0.05) -> None:
        """One screen.state push for a burst of events."""
        if self._state_pending:
            return
        self._state_pending = True

        async def push() -> None:
            await asyncio.sleep(delay_s)
            self._state_pending = False
            await self.send({"type": "ui", "event": "screen.state", "state": self.screen_state()})

        try:
            self._spawn(push())
        except RuntimeError:
            self._state_pending = False

    def _line(self, role: str, text: str, **extra: Any) -> None:
        item = {"role": role, "text": text[:400], "t_ms": now_ms(), **extra}
        self.transcript.append(item)

    async def handle(self, msg: Any) -> dict[str, Any] | None:
        """One message from the browser (or the dev driver, which sends the same ones)."""
        settings, telemetry, hello = self.settings, self.telemetry, self.hello
        kind = msg.get("type") if isinstance(msg, dict) else None
        try:
            if kind == "tool_call":
                self.spawn_tool(ToolCall.model_validate(msg))
            elif kind == "spans":
                for span in SpanBatch.model_validate(msg).spans:
                    telemetry.record(hello.session_id, span, "browser", hello.run_id)
            elif kind == "transcript":
                tr = TranscriptMsg.model_validate(msg)
                telemetry.record_transcript(hello.session_id, tr, hello.run_id)
                tag = lang_tag(tr.text)
                self._line(tr.role, tr.text, lang=tag, phase=tr.phase)
                await self.send({"type": "ui", "event": "transcript", "role": tr.role, "text": tr.text[:400], "lang": tag})
                if tr.role == "user":
                    await self.repair.on_user_transcript(tr.text, tag)
            elif kind == "event" and msg.get("name") == "barge_in":
                self._line("event", "interrupted", what="barge_in")
                await self.send({"type": "ui", "event": "interruption", "what": "barge_in"})
                self.repair.on_barge_in()
            elif kind == "lang" and msg.get("lang") in ("hi", "en"):
                await self.repair.set_language(msg["lang"])
            elif kind == "summary":
                await self.send({"type": "summary", **self.repair.rotation_check()})
            elif kind == "event" and msg.get("name") == "rotated":
                reason = str(msg.get("reason", "turns"))[:20]
                self.span("rotated", 0, reason=reason, ms=msg.get("ms"))
                self.repair.on_rotated(reason)
            elif kind == "event" and msg.get("name") == "safety_violation":
                vk, vt = str(msg.get("kind", ""))[:40], str(msg.get("text", ""))[:300]
                self.span("safety_violation", 0, kind=vk, text=vt)
                self._line("event", f"power instruction stopped: {vt}", what="guardrail", kind=vk)
                self.repair.on_safety_violation(vk, vt)
            elif kind == "dev" and msg.get("scenario") in SCENARIOS and settings.dev_endpoints:
                await self.set_scenario(msg["scenario"])
            elif kind == "dev":
                await self.send({"type": "error", "detail": f"unknown scenario {msg.get('scenario')!r}"})
            else:
                await self.send({"type": "error", "detail": f"unknown message type {kind!r}"})
        except ValidationError as exc:
            await self.send({"type": "error", "detail": f"invalid {kind} message: {exc.error_count()} error(s)"})
            return {"error": "invalid", "count": exc.error_count()}
        return None

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

    async def _run_tool(self, call: ToolCall) -> str:
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
        return output

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

    connections = getattr(ws.app.state, "connections", None)
    if hello.replay:
        await run_replay(ws, telemetry, connections, hello.replay, hello.speed, hello.max_gap_s)
        return
    live = connections.get(hello.session_id) if connections is not None else None
    if hello.role == "screen" and live is not None and live.connected:
        await view(ws, live)
        return
    await telemetry.flush()  # a reconnect may follow writes that are still queued
    stored = await asyncio.to_thread(load_stored_repair, telemetry, hello.session_id)
    conn = Connection(ws, settings, telemetry, hello, stored)
    if connections is not None:
        connections.add(conn)
    ready: dict[str, Any] = {"type": "ready", "session_id": hello.session_id, "scenario": conn.bench.scenario}
    if stored:
        ready["restored"] = conn.repair.summary()
    await conn.send(ready)
    conn.state_soon(0.0)
    if stored and conn.repair.state in ("probing", "fix"):
        await conn.repair.note("The engine reconnected; the repair continues. Call next_step before the next instruction.")
    try:
        while True:
            await conn.handle(await ws.receive_json())
    except WebSocketDisconnect:
        pass
    finally:
        conn.connected = False
        conn.cancel_all()


async def view(ws: WebSocket, conn: Connection) -> None:
    """A second screen on a live session: it sees UI events and the screen state; it may set the
    reply language and (dev) the scenario, nothing else."""
    conn.viewers.add(ws)
    try:
        await ws.send_json({"type": "ready", "session_id": conn.hello.session_id, "scenario": conn.bench.scenario, "viewer": True})
        await ws.send_json({"type": "ui", "event": "screen.state", "state": conn.screen_state()})
        while True:
            msg = await ws.receive_json()
            if isinstance(msg, dict) and (msg.get("type") == "lang" or msg.get("type") == "dev"):
                await conn.handle(msg)
            elif isinstance(msg, dict) and msg.get("type") == "spans":
                continue
            else:
                await ws.send_json({"type": "error", "detail": "a screen viewer can only set the language"})
    except WebSocketDisconnect:
        pass
    finally:
        conn.viewers.discard(ws)
