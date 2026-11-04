"""The 8-tool router: fixed tool list, strict specs, one-line outputs, timeouts, safety first."""
from __future__ import annotations

import asyncio
import json

import pytest

from engine.session.repair import RepairSession
from engine.tools import (
    ALLOWED_TOOL_NAMES,
    ALLOWED_WHEN_STOPPED,
    MAX_EXPOSED_TOOLS,
    MAX_OUTPUT_CHARS,
    TOOLS,
    ToolContext,
    run_tool,
    tool_specs,
    tool_timeouts_ms,
)
from mcp_evidence.fake_bench import FakeBench

MINIMAL_ARGS = {
    "start_repair": {"model": "demo", "symptom": "dead", "battery": "removed"},
    "get_reading": {"kind": "supply"},
    "watch_boot_current": {},
    "record_spoken_reading": {"text": "0.4 volt", "point_id": "vbat_conn"},
    "next_step": {},
    "request_power_step": {"step": "connect_boot_cable"},
    "explain": {"topic": "diode_mode"},
    "close_repair": {"result": "abandoned"},
}


def ctx(scenario: str = "short_battery_line", **bench) -> ToolContext:
    return ToolContext(repair=RepairSession("se_t", FakeBench(scenario, time_scale=0, **bench)))


def call(c: ToolContext, name: str, args=None, timeout_s=None) -> tuple[dict, bool, str]:
    out, ok = asyncio.run(run_tool(name, {} if args is None else args, c, timeout_s))
    return json.loads(out), ok, out


# --- the tool list (CLAUDE.md rule 4) -----------------------------------------------------------


def test_exactly_the_eight_allowed_tools_in_order():
    names = [s["name"] for s in tool_specs()]
    assert len(ALLOWED_TOOL_NAMES) == MAX_EXPOSED_TOOLS == 8
    assert names == list(ALLOWED_TOOL_NAMES) == list(TOOLS)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


@pytest.mark.parametrize("spec", tool_specs(), ids=lambda s: s["name"])
def test_specs_are_strict_plain_function_tools(spec):
    assert spec["type"] == "function"
    assert 20 <= len(spec["description"]) <= 200
    params = spec["parameters"]
    assert params["type"] == "object" and params["additionalProperties"] is False
    assert set(params.get("required", [])) <= set(params["properties"])
    for node in _walk(params["properties"]):
        assert not {"title", "default", "anyOf", "$ref"} & set(node), (spec["name"], node)


def test_only_real_choices_are_required():
    required = {s["name"]: s["parameters"].get("required", []) for s in tool_specs()}
    assert required == {
        "start_repair": [], "get_reading": ["kind"], "watch_boot_current": [], "record_spoken_reading": [],
        "next_step": [], "request_power_step": ["step"], "explain": ["topic"], "close_repair": ["result"],
    }


def test_browser_timeouts_outlast_the_engine_and_the_slow_tools():
    ms = tool_timeouts_ms()
    assert set(ms) == set(ALLOWED_TOOL_NAMES)
    for name, tool in TOOLS.items():
        assert ms[name] > tool.timeout_s * 1000  # the engine answers first, with its own timeout line
    assert ms["get_reading"] > 4000 and ms["watch_boot_current"] > 10_000  # camera reads, boot watches


# --- validation -------------------------------------------------------------------------------


def test_unknown_tool():
    out, ok, _ = call(ctx(), "connect_power", {})
    assert not ok and out == {"error": "unknown_tool"}


@pytest.mark.parametrize(
    "name,args",
    [
        ("get_reading", {"kind": "scope"}),
        ("get_reading", {"kind": "meter", "volts": 5}),  # unknown field
        ("get_reading", {}),  # missing kind
        ("request_power_step", {"step": "connect_mains"}),
        ("close_repair", {"result": "maybe"}),
        ("watch_boot_current", {"trigger": "whenever"}),
        ("explain", {"topic": "x", "level": "guru"}),
        ("next_step", {"anything": 1}),
        ("get_reading", "{not json"),
        ("get_reading", '["kind"]'),
    ],
)
def test_bad_args_are_rejected_before_any_handler_runs(name, args):
    c = ctx()
    out, ok, _ = call(c, name, args)
    assert not ok and out == {"error": "bad_args"}
    assert c.repair.evidence == [] and c.repair.ledger is None


def test_args_may_arrive_as_a_json_string_or_empty_string():
    c = ctx()
    out, ok, _ = call(c, "start_repair", json.dumps(MINIMAL_ARGS["start_repair"]))
    assert ok and out["pack"] == "demo_model"
    out, ok, _ = call(c, "next_step", "")
    assert ok and out["step"] in ("measure", "power")


# --- every tool: happy path, one short JSON line -----------------------------------------------


@pytest.mark.parametrize("name", ALLOWED_TOOL_NAMES)
def test_every_tool_answers_with_one_short_json_line(name):
    c = ctx()
    for setup in ("start_repair", "get_reading"):  # a started repair with a fresh supply read
        if setup != name:
            call(c, setup, MINIMAL_ARGS[setup])
    if name == "watch_boot_current":
        call(c, "request_power_step", MINIMAL_ARGS["request_power_step"])
    out, ok, raw = call(c, name, MINIMAL_ARGS[name])
    assert "\n" not in raw and len(raw) <= MAX_OUTPUT_CHARS, raw
    assert ok, raw
    assert raw == json.dumps(out, separators=(",", ":"), ensure_ascii=False)


def test_hindi_outputs_stay_one_line_and_short():
    c = ctx()
    for _ in range(2):  # the session language switches after two Hindi turns
        asyncio.run(c.repair.on_user_transcript("battery nikal di hai", "hi"))
    assert c.repair.lng == "hi"
    call(c, "start_repair", MINIMAL_ARGS["start_repair"])
    call(c, "get_reading", {"kind": "supply"})
    for name in ("next_step", "request_power_step", "explain"):
        _, ok, raw = call(c, name, MINIMAL_ARGS[name])
        assert ok and "\n" not in raw and len(raw) <= MAX_OUTPUT_CHARS, raw


# --- timeouts --------------------------------------------------------------------------------


def test_timeout_returns_an_error_line():
    slow = ToolContext(repair=RepairSession("se_t", FakeBench(time_scale=1, meter_delay_s=0.5), vision_timeout_s=5))
    out, ok, _ = call(slow, "get_reading", {"kind": "meter"}, timeout_s=0.05)
    assert not ok and out == {"error": "timeout"}


def test_tool_timeouts_are_per_tool():
    assert TOOLS["get_reading"].timeout_s > TOOLS["next_step"].timeout_s
    assert TOOLS["watch_boot_current"].timeout_s > TOOLS["get_reading"].timeout_s


def test_vision_timeout_inside_the_tool_budget_asks_to_read_aloud():
    # the engine's own camera budget (4 s, scaled here) answers before the router's tool timeout
    c = ToolContext(repair=RepairSession("se_t", FakeBench("vision_timeout", time_scale=0.01), vision_timeout_s=0.03))
    call(c, "start_repair", MINIMAL_ARGS["start_repair"])
    out, ok, _ = call(c, "get_reading", {"kind": "meter", "point_id": "vbat_conn"})
    assert not ok and out == {"error": "vision_timeout", "point": "vbat_conn", "ask": "read_aloud"}


# --- safety first ------------------------------------------------------------------------------


def test_stopped_session_refuses_everything_but_explain_and_close():
    c = ctx()
    call(c, "start_repair", MINIMAL_ARGS["start_repair"])
    asyncio.run(c.repair.stop_session("battery or smoke reported"))
    for name in ALLOWED_TOOL_NAMES:
        out, ok, _ = call(c, name, MINIMAL_ARGS[name])
        if name in ALLOWED_WHEN_STOPPED:
            assert ok, (name, out)
        else:
            assert not ok and out["error"] == "session_stopped" and out["say"], name


class CountingBench(FakeBench):
    """Counts how often the bench is touched, to prove a refusal happened before it."""

    calls: int = 0

    async def watch_boot_current(self, *a, **k):
        self.calls += 1
        return await super().watch_boot_current(*a, **k)

    async def read_port_current(self, *a, **k):
        self.calls += 1
        return await super().read_port_current(*a, **k)


@pytest.mark.parametrize("name,args", [("watch_boot_current", {}), ("get_reading", {"kind": "port_current"})])
def test_power_measurements_need_a_gate_approval_before_the_bench_is_touched(name, args):
    bench = CountingBench(time_scale=0)
    c = ToolContext(repair=RepairSession("se_t", bench))
    call(c, "start_repair", MINIMAL_ARGS["start_repair"])
    call(c, "get_reading", {"kind": "supply"})
    out, ok, _ = call(c, name, args)
    assert not ok and out["error"] == "power_step_required"
    assert bench.calls == 0


def test_gate_runs_before_the_power_step_is_granted():
    c = ctx()
    call(c, "start_repair", {"model": "demo", "symptom": "dead"})  # battery not confirmed removed
    call(c, "get_reading", {"kind": "supply"})
    out, ok, _ = call(c, "request_power_step", {"step": "connect_boot_cable"})
    assert out["approved"] is False and out["reason"] == "battery_not_removed"
    assert c.repair.approvals.pending is None
    out, ok, _ = call(c, "watch_boot_current", {})
    assert out == {"error": "power_step_required", "step": "connect_boot_cable"}
