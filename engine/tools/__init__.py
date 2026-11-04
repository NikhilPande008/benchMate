"""Tool router: the only door from the voice model to the bench.

Hard rules enforced here:
- Exactly the 8 coarse tools of CLAUDE.md rule 4; everything finer stays in the engine.
- Every argument set is validated (unknown fields rejected) before a handler runs.
- Safety first: a stopped session answers only explain and close_repair. Power-dependent
  operations check the power gate's approval inside the repair session, before they run.
- Every call has a timeout, and every output is one short JSON line (rule 6).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from engine.session.repair import RepairSession
from engine.safety import STOP_MESSAGE

ALLOWED_TOOL_NAMES = (
    "start_repair",
    "get_reading",
    "watch_boot_current",
    "record_spoken_reading",
    "next_step",
    "request_power_step",
    "explain",
    "close_repair",
)
MAX_EXPOSED_TOOLS = 8
MAX_OUTPUT_CHARS = 320
ALLOWED_WHEN_STOPPED = {"explain", "close_repair"}


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StartRepairArgs(ToolArgs):
    model: str | None = Field(None, description="phone model as the user said it")
    symptom: str | None = Field(None, description='"dead" or "not charging", as the user said it')
    battery: Literal["removed", "connected", "unknown"] | None = Field(None, description="only if the user said so")
    device: str | None = Field(None, description="what is on the bench, if it is not a phone board")


class GetReadingArgs(ToolArgs):
    kind: Literal["supply", "meter", "port_current"] = Field(
        description='"supply" = bench supply display, "meter" = multimeter, "port_current" = supply current while it feeds the charging port'
    )
    point_id: str | None = Field(None, description="the point being measured; default: the current step's point")


class WatchArgs(ToolArgs):
    trigger: Literal["on_connect", "on_power_press"] = Field(
        "on_connect", description='"on_connect" right after the boot cable is connected; "on_power_press" while the power key is pressed'
    )


class SpokenArgs(ToolArgs):
    text: str | None = Field(None, description="exactly what the user said, in their words")
    point_id: str | None = Field(None, description="the point or check it answers; default: the current step")
    confirm: bool = Field(False, description="true when the user confirms the value you read back")


class NoArgs(ToolArgs):
    pass


class PowerArgs(ToolArgs):
    step: Literal["connect_boot_cable", "press_power_key", "connect_port_supply"]


class ExplainArgs(ToolArgs):
    topic: str = Field(description="a fault, point, pattern or term (e.g. diode_mode, vbat), or 'why' for the current step")
    level: Literal["trainee", "expert"] | None = None


class CloseArgs(ToolArgs):
    result: Literal["fixed", "unresolved", "abandoned"]
    fault_id: str | None = None
    note: str | None = None


@dataclass(frozen=True)
class ToolContext:
    repair: RepairSession


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    handler: Handler
    timeout_s: float = 3.0

    def spec(self) -> dict[str, Any]:
        """Realtime API function-tool definition (plain JSON Schema, optional fields not required)."""
        schema = self.args_model.model_json_schema()
        props = {}
        for name, prop in schema.get("properties", {}).items():
            prop.pop("title", None)
            prop.pop("default", None)
            any_of = prop.pop("anyOf", None)
            if any_of:  # Optional[X] -> X
                prop.update(next(a for a in any_of if a.get("type") != "null"))
            props[name] = prop
        out = {"type": "object", "properties": props, "additionalProperties": False}
        if schema.get("required"):
            out["required"] = schema["required"]
        return {"type": "function", "name": self.name, "description": self.description, "parameters": out}


def _r(ctx: ToolContext) -> RepairSession:
    return ctx.repair


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            "start_repair",
            "Start a repair once you know the phone model and the symptom. Loads the model's data (or the general dead-phone flow) and returns the likely faults.",
            StartRepairArgs,
            lambda a, c: _r(c).start_repair(a.model, a.symptom, a.battery, a.device),
        ),
        Tool(
            "get_reading",
            "Read the bench supply, the multimeter, or the port current through the camera. Takes 1-3 s. Returns the value with its class and the updated ranking.",
            GetReadingArgs,
            lambda a, c: _r(c).get_reading(a.kind, a.point_id),
            timeout_s=7.0,
        ),
        Tool(
            "watch_boot_current",
            "Watch the supply current for a few seconds after an approved power step and classify the boot pattern.",
            WatchArgs,
            lambda a, c: _r(c).watch_boot_current(a.trigger),
            timeout_s=12.0,
        ),
        Tool(
            "record_spoken_reading",
            "Record a value or observation the user says aloud. First call returns a read-back; call again with confirm=true once the user confirms it.",
            SpokenArgs,
            lambda a, c: _r(c).record_spoken_reading(a.text, a.point_id, a.confirm),
        ),
        Tool(
            "next_step",
            "The engine's next step: what to measure and why, a power step to request, or the fix once a fault is clear.",
            NoArgs,
            lambda a, c: _r(c).next_step(),
        ),
        Tool(
            "request_power_step",
            "Ask the safety gate before any power step. Returns the exact sentence to say, or why it is blocked and what to set.",
            PowerArgs,
            lambda a, c: _r(c).request_power_step(a.step),
        ),
        Tool(
            "explain",
            "A short explanation of a fault, point, boot pattern or term from the model data, or why the current step was chosen.",
            ExplainArgs,
            lambda a, c: _r(c).explain(a.topic, a.level),
        ),
        Tool(
            "close_repair",
            "Close the repair and save the record. 'fixed' needs a confirming reading after the fix.",
            CloseArgs,
            lambda a, c: _r(c).close_repair(a.result, a.fault_id, a.note),
        ),
    ]
}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS.values()]


def tool_timeouts_ms() -> dict[str, int]:
    """Browser-side SDK timeouts: a little longer than the engine's, so the engine answers first."""
    return {name: int((t.timeout_s + 2.0) * 1000) for name, t in TOOLS.items()}


def one_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


async def run_tool(name: str, raw_args: Any, ctx: ToolContext, timeout_s: float | None = None) -> tuple[str, bool]:
    """Validate and run a tool. Returns (one-line JSON output, ok)."""
    tool = TOOLS.get(name)
    if tool is None:
        return one_line({"error": "unknown_tool"}), False
    try:
        if isinstance(raw_args, str):
            raw_args = json.loads(raw_args or "{}")
        args = tool.args_model.model_validate(raw_args or {})
    except (ValidationError, ValueError):
        return one_line({"error": "bad_args"}), False
    if ctx.repair.stopped and name not in ALLOWED_WHEN_STOPPED:
        return one_line({"error": "session_stopped", "say": STOP_MESSAGE[ctx.repair.lng]}), False
    try:
        result = await asyncio.wait_for(tool.handler(args, ctx), timeout=timeout_s or tool.timeout_s)
    except asyncio.TimeoutError:
        return one_line({"error": "timeout"}), False
    return one_line(result), "error" not in result
