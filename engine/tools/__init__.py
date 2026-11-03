"""Tool router: the only door from the voice model to the bench.

Hard rules enforced here:
- At most 8 tools are exposed to the voice model, from a fixed list of names.
- Every argument set is validated (unknown fields rejected) before a handler runs.
- Every output is one short JSON line.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

# The complete set of names the voice model may ever see (CLAUDE.md rule 4).
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
MAX_OUTPUT_CHARS = 240


class ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GetReadingArgs(ToolArgs):
    kind: Literal["supply", "meter"] = Field(description='"supply" for the bench power supply, "meter" for the multimeter')


@dataclass(frozen=True)
class ToolContext:
    session_id: str
    dummy_delay_s: float


Handler = Callable[[Any, ToolContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    args_model: type[ToolArgs]
    handler: Handler

    def spec(self) -> dict[str, Any]:
        """Realtime API function-tool definition."""
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        schema["additionalProperties"] = False
        return {"type": "function", "name": self.name, "description": self.description, "parameters": schema}


# Phase 1 stub: fixed values after a configurable delay, to exercise async tool calls and preambles.
# These numbers are placeholders, not reference readings.
_DUMMY_READINGS = {
    "supply": {"kind": "supply", "volts": 4.0, "amps": 0.0, "limit_a": 1.0, "conf": 0.97},
    "meter": {"kind": "meter", "value": 0.35, "unit": "V", "mode": "diode", "conf": 0.97},
}


async def _dummy_get_reading(args: GetReadingArgs, ctx: ToolContext) -> dict[str, Any]:
    await asyncio.sleep(ctx.dummy_delay_s)
    return _DUMMY_READINGS[args.kind]


TOOLS: dict[str, Tool] = {
    t.name: t
    for t in [
        Tool(
            name="get_reading",
            description=(
                "Read the bench power supply or the multimeter display through the camera. "
                "Takes 1-3 seconds. Returns one JSON line with the value, unit and confidence (conf)."
            ),
            args_model=GetReadingArgs,
            handler=_dummy_get_reading,
        ),
    ]
}


def tool_specs() -> list[dict[str, Any]]:
    return [t.spec() for t in TOOLS.values()]


def one_line(payload: dict[str, Any]) -> str:
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


async def run_tool(name: str, raw_args: Any, ctx: ToolContext, timeout_s: float) -> tuple[str, bool]:
    """Validate and run a tool. Returns (one-line JSON output, ok)."""
    tool = TOOLS.get(name)
    if tool is None:
        return one_line({"error": "unknown_tool"}), False
    try:
        if isinstance(raw_args, str):
            raw_args = json.loads(raw_args or "{}")
        args = tool.args_model.model_validate(raw_args)
    except (ValidationError, ValueError):
        return one_line({"error": "bad_args"}), False
    try:
        result = await asyncio.wait_for(tool.handler(args, ctx), timeout=timeout_s)
    except asyncio.TimeoutError:
        return one_line({"error": "timeout"}), False
    return one_line(result), True
