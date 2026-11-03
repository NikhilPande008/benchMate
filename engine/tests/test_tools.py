from __future__ import annotations

import asyncio
import json

import pytest

from engine.tools import (
    ALLOWED_TOOL_NAMES,
    MAX_EXPOSED_TOOLS,
    MAX_OUTPUT_CHARS,
    TOOLS,
    ToolContext,
    run_tool,
    tool_specs,
)

CTX = ToolContext(session_id="se_t", dummy_delay_s=0.0)


def test_at_most_eight_tools_from_the_fixed_list():
    names = [s["name"] for s in tool_specs()]
    assert len(names) <= MAX_EXPOSED_TOOLS
    assert set(names) <= set(ALLOWED_TOOL_NAMES)
    assert len(ALLOWED_TOOL_NAMES) == MAX_EXPOSED_TOOLS


def test_specs_are_strict_function_tools():
    for spec in tool_specs():
        assert spec["type"] == "function"
        assert spec["parameters"]["type"] == "object"
        assert spec["parameters"]["additionalProperties"] is False


@pytest.mark.parametrize("kind", ["supply", "meter"])
def test_outputs_are_one_short_json_line(kind):
    out, ok = asyncio.run(run_tool("get_reading", {"kind": kind}, CTX, timeout_s=1))
    assert ok
    assert "\n" not in out and len(out) <= MAX_OUTPUT_CHARS
    assert json.loads(out)["conf"] >= 0.9


def test_unknown_fields_are_rejected():
    out, ok = asyncio.run(run_tool("get_reading", {"kind": "meter", "volts": 5}, CTX, timeout_s=1))
    assert not ok and json.loads(out) == {"error": "bad_args"}


def test_timeout_returns_error_line():
    slow = ToolContext(session_id="se_t", dummy_delay_s=0.5)
    out, ok = asyncio.run(run_tool("get_reading", {"kind": "meter"}, slow, timeout_s=0.05))
    assert not ok and json.loads(out) == {"error": "timeout"}


def test_registry_names_match_specs():
    assert list(TOOLS) == [s["name"] for s in tool_specs()]
